"""Full analysis flow coordinator (SRS 4.1): cache check -> cost gate -> data -> deterministic
engine -> persist -> LLM report -> validate -> persist cost/report -> return.

This is the ONLY module allowed to call providers/LLM adapters directly, and it always
goes through core/cost_gate.py first (CLAUDE.md rule 3, SRS AD-4/16.3).
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, time, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.schemas import AnalyzeResponse, ChartBar, NotableLevels, PartialAnalyzeResponse, ScoresOut
from app.config import Settings, get_settings
from app.core.cache import DBCacheAdapter, make_cache_key, quote_cache_ttl_seconds, report_cache_ttl_seconds
from app.core.cost_gate import CostGate
from app.db import repository
from app.engines.deterministic.schemas import DeterministicAnalysis, Indicators
from app.engines.deterministic.engine import run_deterministic_engine
from app.engines.deterministic.plain_summary import build_plain_summary, compute_open_price
from app.engines.llm.adapters.factory import get_llm_adapter
from app.engines.llm.devils_advocate import generate_devils_advocate_alerts
from app.engines.llm.report_engine import (
    MAX_OUTPUT_TOKENS,
    ReportResult,
    build_fallback_report,
    combine_report_sections,
    generate_report,
)
from app.db.repository import record_option_contract_analysis
from app.engines.options.contract_analyzer import analyze_option_contract
from app.engines.options.contract_ranker import (
    fetch_alpaca_chain,
    fetch_yfinance_chain,
    get_alpaca_expirations,
    get_yfinance_expirations,
    rank_best_contract,
)
from app.engines.options.greeks import compute_greeks, years_to_expiry
from app.engines.options.iv_metrics import (
    daily_theta_decay_pct,
    reprice_contract_at_stock_level,
)
from app.engines.options.schemas import OptionContractAnalysis
from app.engines.options.vision_extraction import extract_contract_from_image
from app.engines.screener.scanner import run_universe_scan
from app.engines.screener.schemas import ScreenerResult
from app.engines.screener.snipe_accuracy import compute_snipe_accuracy_panel
from app.engines.screener.snipe_scanner import run_snipe_universe_scan
from app.engines.screener.snipe_schemas import (
    LevelProbability,
    SnipeOptionCard,
    SnipeOptionsScanResult,
    SnipeStockCard,
    SnipeStockScanResult,
    WatchlistEventOut,
    WatchlistItemOut,
)
from app.engines.screener.touch_probability import estimate_touch_probability
from app.engines.screener.watchlist_status import compute_watchlist_status
from app.legal.disclaimers import DISCLAIMER_AR, DISCLAIMER_EN
from app.providers.factory import get_market_data_provider
from app.static_data.us_symbols import US_SYMBOLS

logger = logging.getLogger(__name__)

SYMBOL_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z])?$")

VISION_MAX_TOKENS = 500
# Rough token estimate for the fixed prompt + a typical single-contract screenshot, used
# only to size the Cost Gate's pre-call estimate (SRS 16.1). Refined to the real usage
# once the response comes back via `record_actual`.
ESTIMATED_IMAGE_INPUT_TOKENS = 1200

# Rough pre-call token estimate for the Cost Gate's pre-check (SRS 16.1, 25.1 ballpark:
# ~3-4K compressed JSON+prompt tokens in, ~1.2-1.5K report tokens out). Refined to the
# real usage immediately after the call via `record_actual`.
ESTIMATED_LLM_INPUT_TOKENS = 3500


class InvalidSymbolError(ValueError):
    pass


class CostLimitExceededError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class DataFetchError(RuntimeError):
    """Symbol has valid format but the market-data provider couldn't return bars for it
    (typo, delisted, unsupported symbol, or a provider outage). Raised BEFORE any LLM
    cost-gate reservation is made — a bad symbol must fail free, not after paid spend."""

    pass


class VisionExtractionError(RuntimeError):
    """The vision model's response for an uploaded contract screenshot didn't parse into
    a valid contract (M2-B, Annex C-2)."""

    pass


def validate_symbol(symbol: str) -> str:
    normalized = symbol.strip().upper()
    if not SYMBOL_RE.match(normalized):
        raise InvalidSymbolError(f"invalid symbol format: '{symbol}'")
    return normalized


def _regime_to_scenario(regime_label: str) -> Literal["bullish", "bearish", "neutral"]:
    if regime_label == "trending_up":
        return "bullish"
    if regime_label == "trending_down":
        return "bearish"
    return "neutral"


def _to_api_response(
    analysis_id: uuid.UUID,
    symbol: str,
    from_cache: bool,
    regime: str,
    last_close: float,
    scores: ScoresOut,
    notable_levels: NotableLevels,
    indicators: Indicators,
    plain_summary,
    report: ReportResult,
    lang: Literal["ar", "en"],
    cost_usd: float,
    market_open: bool,
    data_quality: str,
    data_provider: str,
    data_as_of: str,
    chart_bars: list[ChartBar],
    cached_minutes_ago: float | None = None,
) -> AnalyzeResponse:
    disclaimer = DISCLAIMER_AR if lang == "ar" else DISCLAIMER_EN
    is_ar = lang == "ar"
    return AnalyzeResponse(
        analysis_id=str(analysis_id),
        symbol=symbol,
        from_cache=from_cache,
        cached_minutes_ago=cached_minutes_ago,
        regime=regime,
        last_close=last_close,
        scores=scores,
        notable_levels=notable_levels,
        indicators=indicators,
        plain_summary=plain_summary,
        chart_bars=chart_bars,
        tldr_ar=report.tldr if is_ar else None,
        tldr_en=report.tldr if not is_ar else None,
        scenario_bullish_ar=report.scenario_bullish if is_ar else None,
        scenario_bullish_en=report.scenario_bullish if not is_ar else None,
        scenario_bearish_ar=report.scenario_bearish if is_ar else None,
        scenario_bearish_en=report.scenario_bearish if not is_ar else None,
        scenario_neutral_ar=report.scenario_neutral if is_ar else None,
        scenario_neutral_en=report.scenario_neutral if not is_ar else None,
        report_ar=report.full_report if is_ar else None,
        report_en=report.full_report if not is_ar else None,
        devils_advocate_ar=report.devils_advocate_text if is_ar else None,
        devils_advocate_en=report.devils_advocate_text if not is_ar else None,
        disclaimer=disclaimer,
        cost_usd=cost_usd,
        market_open=market_open,
        data_quality=data_quality,
        data_provider=data_provider,
        data_as_of=data_as_of,
    )


def run_analysis(
    db: Session, symbol: str, lang: Literal["ar", "en"] = "ar", settings: Settings | None = None
) -> AnalyzeResponse:
    settings = settings or get_settings()
    symbol = validate_symbol(symbol)

    market_data_provider = get_market_data_provider()
    market_open = market_data_provider.is_market_open()

    # [3] Cache check (SRS 4.1 step 3, 17.1) — zero cost on a hit.
    cache = DBCacheAdapter(db)
    cache_key = make_cache_key("report", symbol, lang)
    cached = cache.get(cache_key)
    if cached is not None:
        try:
            cached_at = datetime.fromisoformat(cached["_cached_at"])
            age_minutes = round((datetime.now(timezone.utc) - cached_at).total_seconds() / 60, 1)
            response = AnalyzeResponse.model_validate(cached["response"])
            response.from_cache = True
            response.cached_minutes_ago = age_minutes
            response.cost_usd = 0.0
            return response
        except (ValidationError, KeyError):
            # A cached entry from an older AnalyzeResponse shape (schema evolved since it
            # was written) — treat it as a cache miss rather than crash, and drop the
            # stale entry so it doesn't keep failing on every subsequent request.
            cache.delete(cache_key)

    gate = CostGate(db, settings)

    # [4a] Cost Gate for the market-data call (SRS 16, AD-4: no path bypasses the gate,
    # even a free-tier one) — checked and fetched BEFORE any commitment to the expensive
    # LLM step. A bad symbol must fail here, for free, not after LLM budget is reserved.
    data_decision = gate.check_and_reserve(
        category="market_data",
        provider=market_data_provider.provider_name,
        estimated_cost=market_data_provider.estimated_cost_per_call(),
    )
    if not data_decision.allowed:
        raise CostLimitExceededError(data_decision.reason)

    try:
        daily = market_data_provider.get_daily_ohlcv(symbol, lookback_days=300)
        intraday = market_data_provider.get_intraday(symbol, "5m") if market_open else None
        try:
            quote = market_data_provider.get_quote(symbol)
        except Exception as quote_exc:
            logger.warning(
                "live quote fetch failed for %s via %s — analysis continues on daily close: %s",
                symbol, market_data_provider.provider_name, quote_exc,
            )
            quote = None
    except Exception as exc:
        gate.record_actual(data_decision.ledger_id, 0.0)
        raise DataFetchError(f"failed to fetch market data for '{symbol}': {exc}") from exc
    gate.record_actual(data_decision.ledger_id, market_data_provider.estimated_cost_per_call())

    # SRS 12.5: جودة_البيانات reflects whether the QUOTE itself is live or delayed
    # (a provider trait), not whether intraday chart bars happen to exist — those are
    # simply absent outside market hours regardless of provider freshness, and must not
    # cause a real-time provider like Alpaca to be mislabeled "مؤجل 15 دقيقة".
    if quote is None:
        data_quality = "daily_only"
    elif quote.is_delayed:
        data_quality = "delayed_15m"
    else:
        data_quality = "intraday"

    # [6] Deterministic Engine (SRS 4.1 step 6, zero external cost — pure Python).
    analysis = run_deterministic_engine(
        symbol, daily, intraday=intraday, quote=quote, data_quality=data_quality
    )

    # [7] Persist the deterministic JSON now, before the LLM call, so it survives an LLM
    # failure (CLAUDE.md rule 6, SRS 4.1 step 7).
    record = repository.create_analysis(
        db,
        symbol=symbol,
        market_open=market_open,
        data_provider=market_data_provider.provider_name,
        deterministic_json=analysis.model_dump(mode="json"),
        scores=analysis.scores.model_dump(),
        regime=analysis.regime.label,
        status="data_only",
    )
    repository.create_audit_targets(
        db, record.id, symbol, analysis.last_close, analysis.levels, _regime_to_scenario(analysis.regime.label)
    )

    # Build the response fields that DON'T depend on the LLM call now, before any LLM
    # spend — a structural bug here (e.g. a schema field mismatch) must fail for free,
    # not after paying for a report (this is exactly what caught a real stale-cache/
    # schema-drift bug once: keep validating early).
    scores = ScoresOut(**analysis.scores.model_dump(exclude={"formulas_ref"}))
    notable_levels = NotableLevels(
        supports=[lv.price for lv in analysis.levels.supports],
        resistances=[lv.price for lv in analysis.levels.resistances],
        invalidation=analysis.levels.invalidation,
    )
    plain_summary = build_plain_summary(analysis, compute_open_price(daily, intraday))

    # [4b] Cost Gate for the LLM call — only reserved now that we KNOW the symbol is
    # valid and the deterministic analysis actually succeeded.
    llm_adapter = get_llm_adapter()
    estimated_llm_cost = llm_adapter.estimate_cost(ESTIMATED_LLM_INPUT_TOKENS, MAX_OUTPUT_TOKENS)
    decision = gate.check_and_reserve(
        category="llm", provider=llm_adapter.provider_name, estimated_cost=estimated_llm_cost
    )
    if not decision.allowed:
        raise CostLimitExceededError(decision.reason)

    # [8]/[9] LLM Report Engine + numeric/wording validation (SRS 13, 14).
    alerts = generate_devils_advocate_alerts(daily, analysis)
    try:
        report_result = generate_report(llm_adapter, analysis, alerts, lang=lang)
    except Exception:
        # Hard failure (network, auth, etc.) — guaranteed-accurate zero-cost fallback.
        logger.exception("LLM report generation failed for %s — serving code-built fallback report", symbol)
        report_result = build_fallback_report(analysis, alerts, lang)
        report_result.llm_provider = None

    # [10] Record actual cost + persist report (SRS 4.1 step 10). The tldr/scenarios/full
    # report/devil's-advocate pieces are serialized into the one report_text_ar/en column
    # (SRS 7.1 has no separate column per field) and split back apart on read.
    gate.record_actual(decision.ledger_id, report_result.total_cost_usd)
    combined_text = combine_report_sections(report_result)
    repository.update_analysis_report(
        db,
        record.id,
        report_text_ar=combined_text if lang == "ar" else None,
        report_text_en=combined_text if lang == "en" else None,
        llm_provider=report_result.llm_provider,
        llm_input_tokens=report_result.total_input_tokens,
        llm_output_tokens=report_result.total_output_tokens,
        total_cost_usd=report_result.total_cost_usd,
        status="completed" if report_result.llm_provider is not None else "llm_failed",
    )

    chart_bars = [
        ChartBar(t=idx.strftime("%Y-%m-%d"), o=float(row["open"]), h=float(row["high"]), l=float(row["low"]), c=float(row["close"]))
        for idx, row in daily.tail(120).iterrows()
    ]

    response = _to_api_response(
        analysis_id=record.id,
        symbol=symbol,
        from_cache=False,
        regime=analysis.regime.label,
        last_close=analysis.last_close,
        scores=scores,
        notable_levels=notable_levels,
        indicators=analysis.indicators,
        plain_summary=plain_summary,
        report=report_result,
        lang=lang,
        cost_usd=report_result.total_cost_usd,
        market_open=market_open,
        data_quality=data_quality,
        data_provider=market_data_provider.provider_name,
        data_as_of=analysis.data_as_of.isoformat(),
        chart_bars=chart_bars,
    )

    # [11] Cache the finished response (SRS 17.1) — TTL depends on whether the market is open.
    ttl = report_cache_ttl_seconds(market_open, settings)
    cache.set(
        cache_key,
        {"_cached_at": datetime.now(timezone.utc).isoformat(), "response": response.model_dump(mode="json")},
        ttl_seconds=ttl,
    )

    return response


def run_analysis_deterministic_only(
    db: Session, symbol: str, lang: Literal["ar", "en"] = "ar", settings: Settings | None = None
) -> tuple[AnalyzeResponse | None, PartialAnalyzeResponse | None]:
    """Phase 1 of the web UI's progressive load (new, owner request 2026-07-18 — pages
    must open in under 5s even though the LLM step alone can take up to the SRS 13 budget
    of ~15s): everything through the deterministic engine, with NO LLM call.

    Returns `(cached_full_response, None)` on a cache hit — nothing is slow, so the page
    can render everything at once exactly like `run_analysis` always did. Returns
    `(None, partial)` on a cache miss — the deterministic phase is done and persisted
    (CLAUDE.md rule 6), and the caller (the web route) renders a page that fetches the
    narrative in the background via `run_analysis_narrative`.

    Deliberately does NOT call `run_analysis` internally and duplicates its first half —
    `run_analysis` (used by the paid JSON API `/api/v1/analyze`) is left completely
    untouched so this progressive-loading feature carries zero regression risk to that
    contract or its test suite.
    """
    settings = settings or get_settings()
    symbol = validate_symbol(symbol)

    market_data_provider = get_market_data_provider()
    market_open = market_data_provider.is_market_open()

    cache = DBCacheAdapter(db)
    cache_key = make_cache_key("report", symbol, lang)
    cached = cache.get(cache_key)
    if cached is not None:
        try:
            cached_at = datetime.fromisoformat(cached["_cached_at"])
            age_minutes = round((datetime.now(timezone.utc) - cached_at).total_seconds() / 60, 1)
            response = AnalyzeResponse.model_validate(cached["response"])
            response.from_cache = True
            response.cached_minutes_ago = age_minutes
            response.cost_usd = 0.0
            return response, None
        except (ValidationError, KeyError):
            cache.delete(cache_key)

    gate = CostGate(db, settings)
    data_decision = gate.check_and_reserve(
        category="market_data", provider=market_data_provider.provider_name,
        estimated_cost=market_data_provider.estimated_cost_per_call(),
    )
    if not data_decision.allowed:
        raise CostLimitExceededError(data_decision.reason)

    try:
        daily = market_data_provider.get_daily_ohlcv(symbol, lookback_days=300)
        intraday = market_data_provider.get_intraday(symbol, "5m") if market_open else None
        try:
            quote = market_data_provider.get_quote(symbol)
        except Exception as quote_exc:
            logger.warning(
                "live quote fetch failed for %s via %s — analysis continues on daily close: %s",
                symbol, market_data_provider.provider_name, quote_exc,
            )
            quote = None
    except Exception as exc:
        gate.record_actual(data_decision.ledger_id, 0.0)
        raise DataFetchError(f"failed to fetch market data for '{symbol}': {exc}") from exc
    gate.record_actual(data_decision.ledger_id, market_data_provider.estimated_cost_per_call())

    # SRS 12.5: جودة_البيانات reflects whether the QUOTE itself is live or delayed
    # (a provider trait), not whether intraday chart bars happen to exist — those are
    # simply absent outside market hours regardless of provider freshness, and must not
    # cause a real-time provider like Alpaca to be mislabeled "مؤجل 15 دقيقة".
    if quote is None:
        data_quality = "daily_only"
    elif quote.is_delayed:
        data_quality = "delayed_15m"
    else:
        data_quality = "intraday"

    analysis = run_deterministic_engine(symbol, daily, intraday=intraday, quote=quote, data_quality=data_quality)

    record = repository.create_analysis(
        db,
        symbol=symbol,
        market_open=market_open,
        data_provider=market_data_provider.provider_name,
        deterministic_json=analysis.model_dump(mode="json"),
        scores=analysis.scores.model_dump(),
        regime=analysis.regime.label,
        status="data_only",
    )
    repository.create_audit_targets(
        db, record.id, symbol, analysis.last_close, analysis.levels, _regime_to_scenario(analysis.regime.label)
    )

    scores = ScoresOut(**analysis.scores.model_dump(exclude={"formulas_ref"}))
    notable_levels = NotableLevels(
        supports=[lv.price for lv in analysis.levels.supports],
        resistances=[lv.price for lv in analysis.levels.resistances],
        invalidation=analysis.levels.invalidation,
    )
    plain_summary = build_plain_summary(analysis, compute_open_price(daily, intraday))
    chart_bars = [
        ChartBar(t=idx.strftime("%Y-%m-%d"), o=float(row["open"]), h=float(row["high"]), l=float(row["low"]), c=float(row["close"]))
        for idx, row in daily.tail(120).iterrows()
    ]

    partial = PartialAnalyzeResponse(
        analysis_id=str(record.id),
        symbol=symbol,
        regime=analysis.regime.label,
        last_close=analysis.last_close,
        scores=scores,
        notable_levels=notable_levels,
        indicators=analysis.indicators,
        plain_summary=plain_summary,
        chart_bars=chart_bars,
        market_open=market_open,
        data_quality=data_quality,
        data_provider=market_data_provider.provider_name,
        data_as_of=analysis.data_as_of.isoformat(),
    )
    return None, partial


def run_analysis_narrative(
    db: Session, analysis_id: uuid.UUID, lang: Literal["ar", "en"] = "ar", settings: Settings | None = None
) -> AnalyzeResponse:
    """Phase 2 of the progressive load: the LLM step only, for an analysis already
    persisted deterministic-only by `run_analysis_deterministic_only`. Re-hydrates the
    `DeterministicAnalysis` from the stored JSON (no re-computation) but does re-fetch the
    raw OHLCV frame (cost-gated like any other call, CLAUDE.md rule 3) since the Devil's
    Advocate check needs the actual bars, not just the finished JSON.

    Cache-first at the top (same `report:symbol:lang` key `run_analysis` uses) so a
    duplicate/racing fetch (e.g. two open tabs) never re-spends on the LLM call.
    """
    settings = settings or get_settings()
    record = repository.get_analysis(db, analysis_id)
    if record is None:
        raise ValueError(f"analysis {analysis_id} not found")
    symbol = record.symbol

    cache = DBCacheAdapter(db)
    cache_key = make_cache_key("report", symbol, lang)
    cached = cache.get(cache_key)
    if cached is not None:
        try:
            cached_at = datetime.fromisoformat(cached["_cached_at"])
            age_minutes = round((datetime.now(timezone.utc) - cached_at).total_seconds() / 60, 1)
            response = AnalyzeResponse.model_validate(cached["response"])
            response.from_cache = True
            response.cached_minutes_ago = age_minutes
            response.cost_usd = 0.0
            return response
        except (ValidationError, KeyError):
            cache.delete(cache_key)

    analysis = DeterministicAnalysis.model_validate(record.deterministic_json)
    market_data_provider = get_market_data_provider()
    gate = CostGate(db, settings)

    data_decision = gate.check_and_reserve(
        category="market_data", provider=market_data_provider.provider_name,
        estimated_cost=market_data_provider.estimated_cost_per_call(),
    )
    if not data_decision.allowed:
        raise CostLimitExceededError(data_decision.reason)
    try:
        daily = market_data_provider.get_daily_ohlcv(symbol, lookback_days=300)
    except Exception as exc:
        gate.record_actual(data_decision.ledger_id, 0.0)
        raise DataFetchError(f"failed to re-fetch market data for narrative: {exc}") from exc
    gate.record_actual(data_decision.ledger_id, market_data_provider.estimated_cost_per_call())

    llm_adapter = get_llm_adapter()
    estimated_llm_cost = llm_adapter.estimate_cost(ESTIMATED_LLM_INPUT_TOKENS, MAX_OUTPUT_TOKENS)
    decision = gate.check_and_reserve(
        category="llm", provider=llm_adapter.provider_name, estimated_cost=estimated_llm_cost
    )
    if not decision.allowed:
        raise CostLimitExceededError(decision.reason)

    alerts = generate_devils_advocate_alerts(daily, analysis)
    try:
        report_result = generate_report(llm_adapter, analysis, alerts, lang=lang)
    except Exception:
        logger.exception("LLM narrative generation failed for %s — serving code-built fallback report", symbol)
        report_result = build_fallback_report(analysis, alerts, lang)
        report_result.llm_provider = None

    gate.record_actual(decision.ledger_id, report_result.total_cost_usd)
    combined_text = combine_report_sections(report_result)
    repository.update_analysis_report(
        db,
        record.id,
        report_text_ar=combined_text if lang == "ar" else None,
        report_text_en=combined_text if lang == "en" else None,
        llm_provider=report_result.llm_provider,
        llm_input_tokens=report_result.total_input_tokens,
        llm_output_tokens=report_result.total_output_tokens,
        total_cost_usd=report_result.total_cost_usd,
        status="completed" if report_result.llm_provider is not None else "llm_failed",
    )

    scores = ScoresOut(**analysis.scores.model_dump(exclude={"formulas_ref"}))
    notable_levels = NotableLevels(
        supports=[lv.price for lv in analysis.levels.supports],
        resistances=[lv.price for lv in analysis.levels.resistances],
        invalidation=analysis.levels.invalidation,
    )
    plain_summary = build_plain_summary(analysis, compute_open_price(daily, None))
    chart_bars = [
        ChartBar(t=idx.strftime("%Y-%m-%d"), o=float(row["open"]), h=float(row["high"]), l=float(row["low"]), c=float(row["close"]))
        for idx, row in daily.tail(120).iterrows()
    ]

    response = _to_api_response(
        analysis_id=record.id,
        symbol=symbol,
        from_cache=False,
        regime=analysis.regime.label,
        last_close=analysis.last_close,
        scores=scores,
        notable_levels=notable_levels,
        indicators=analysis.indicators,
        plain_summary=plain_summary,
        report=report_result,
        lang=lang,
        cost_usd=report_result.total_cost_usd,
        market_open=record.market_open,
        data_quality=analysis.data_quality,
        data_provider=record.data_provider,
        data_as_of=analysis.data_as_of.isoformat(),
        chart_bars=chart_bars,
    )

    ttl = report_cache_ttl_seconds(record.market_open, settings)
    cache.set(
        cache_key,
        {"_cached_at": datetime.now(timezone.utc).isoformat(), "response": response.model_dump(mode="json")},
        ttl_seconds=ttl,
    )
    return response


def get_live_quote(db: Session, symbol: str, settings: Settings | None = None) -> dict:
    """Lightweight live-price poll for the stock-analysis page's 15-second ticker (new,
    owner request 2026-07-18) — a `get_quote()` call only, never a full re-analysis, so
    each poll stays cheap and fast. Still goes through cache-first + Cost Gate like any
    other market-data call (CLAUDE.md rules 3, 8); the 15s cache TTL (`cache_ttl_quote_
    seconds`) matches the client's poll interval so a single browser tab never double-
    reserves within one tick, while still refusing to reach the provider more often than
    the UI can even show.
    """
    settings = settings or get_settings()
    symbol = validate_symbol(symbol)
    market_data_provider = get_market_data_provider()

    cache = DBCacheAdapter(db)
    cache_key = make_cache_key("quote", symbol)
    cached = cache.get(cache_key)
    if cached is not None:
        return {**cached, "from_cache": True}

    gate = CostGate(db, settings)
    decision = gate.check_and_reserve(
        category="market_data", provider=market_data_provider.provider_name,
        estimated_cost=market_data_provider.estimated_cost_per_call(),
    )
    if not decision.allowed:
        raise CostLimitExceededError(decision.reason)

    try:
        quote = market_data_provider.get_quote(symbol)
    except Exception as exc:
        gate.record_actual(decision.ledger_id, 0.0)
        raise DataFetchError(f"failed to fetch live quote for '{symbol}': {exc}") from exc
    gate.record_actual(decision.ledger_id, market_data_provider.estimated_cost_per_call())

    result = {"symbol": symbol, "price": quote.price, "as_of": quote.as_of, "is_delayed": quote.is_delayed}
    cache.set(cache_key, result, ttl_seconds=quote_cache_ttl_seconds(settings))
    return {**result, "from_cache": False}


def run_screener_scan(
    db: Session, kind: Literal["daily_readiness", "weekly_structure"], settings: Settings | None = None
) -> ScreenerResult:
    """New feature (not in the original SRS milestones): rank a candidate universe by an
    explicit deterministic-only formula (CLAUDE.md rule 1 — no LLM anywhere in this path).

    Both screener views are computed from ONE fetch pass over the universe and cached
    together (SRS 17 cache-first pattern), so a cache hit on either kind serves free. The
    cost gate is reserved ONCE for the whole batch (not once per symbol) — reserving per
    symbol would trip the >N-calls/minute anomaly guard (SRS 16.1) and auto-kill-switch the
    whole platform on an ordinary ~200-symbol scan, which would be a self-inflicted outage,
    not a real anomaly.
    """
    settings = settings or get_settings()
    cache = DBCacheAdapter(db)
    cache_key = make_cache_key("screener", kind)
    cached = cache.get(cache_key)
    if cached is not None:
        try:
            cached_at = datetime.fromisoformat(cached["_cached_at"])
            age_minutes = round((datetime.now(timezone.utc) - cached_at).total_seconds() / 60, 1)
            result = ScreenerResult.model_validate(cached["result"])
            result.from_cache = True
            result.cached_minutes_ago = age_minutes
            return result
        except (ValidationError, KeyError):
            cache.delete(cache_key)

    market_data_provider = get_market_data_provider()
    universe = [s["symbol"] for s in US_SYMBOLS]

    gate = CostGate(db, settings)
    estimated_cost = market_data_provider.estimated_cost_per_call() * len(universe)
    decision = gate.check_and_reserve(
        category="market_data", provider=market_data_provider.provider_name, estimated_cost=estimated_cost
    )
    if not decision.allowed:
        raise CostLimitExceededError(decision.reason)

    try:
        daily_result, weekly_result, fetched_count = run_universe_scan(market_data_provider, universe)
    except Exception:
        gate.record_actual(decision.ledger_id, 0.0)
        raise
    gate.record_actual(decision.ledger_id, market_data_provider.estimated_cost_per_call() * fetched_count)

    ttl = settings.cache_ttl_screener_seconds
    now_iso = datetime.now(timezone.utc).isoformat()
    cache.set(
        make_cache_key("screener", "daily_readiness"),
        {"_cached_at": now_iso, "result": daily_result.model_dump(mode="json")},
        ttl_seconds=ttl,
    )
    cache.set(
        make_cache_key("screener", "weekly_structure"),
        {"_cached_at": now_iso, "result": weekly_result.model_dump(mode="json")},
        ttl_seconds=ttl,
    )

    return daily_result if kind == "daily_readiness" else weekly_result


def run_snipe_scan(db: Session, settings: Settings | None = None) -> SnipeStockScanResult:
    """"قنص اليوم" (Today's Snipe) — new feature, not in the original SRS milestones.

    Deterministic-only (CLAUDE.md rule 1): ranks the top-50-by-dollar-volume "active"
    subset of the candidate universe by the declared Snipe Score (engines/screener/
    snipe_scoring.py), attaches a statistical touch probability to each level (engines/
    screener/touch_probability.py), and — unlike the daily/weekly screener, which is
    cache-only — persists each of the top-10 cards as a lightweight `Analysis` (kind=
    "snipe") with its own `audit_targets` (CLAUDE.md rule 6), so the scheduled self-audit
    job can later verify whether each target zone touched before the invalidation level
    (audit/self_audit.py::evaluate_snipe_targets). Cache-first, single cost-gate
    reservation for the whole batch — same reasoning as `run_screener_scan`.
    """
    settings = settings or get_settings()
    cache = DBCacheAdapter(db)
    cache_key = make_cache_key("snipe", "stocks")
    cached = cache.get(cache_key)
    if cached is not None:
        try:
            cached_at = datetime.fromisoformat(cached["_cached_at"])
            age_minutes = round((datetime.now(timezone.utc) - cached_at).total_seconds() / 60, 1)
            result = SnipeStockScanResult.model_validate(cached["result"])
            result.from_cache = True
            result.cached_minutes_ago = age_minutes
            return result
        except (ValidationError, KeyError):
            cache.delete(cache_key)

    market_data_provider = get_market_data_provider()
    universe = [s["symbol"] for s in US_SYMBOLS]

    gate = CostGate(db, settings)
    estimated_cost = market_data_provider.estimated_cost_per_call() * len(universe)
    decision = gate.check_and_reserve(
        category="market_data", provider=market_data_provider.provider_name, estimated_cost=estimated_cost
    )
    if not decision.allowed:
        raise CostLimitExceededError(decision.reason)

    try:
        candidates, fetched_count = run_snipe_universe_scan(market_data_provider, universe)
    except Exception:
        gate.record_actual(decision.ledger_id, 0.0)
        raise
    gate.record_actual(decision.ledger_id, market_data_provider.estimated_cost_per_call() * fetched_count)

    market_open = market_data_provider.is_market_open()
    cards: list[SnipeStockCard] = []
    for candidate in candidates:
        analysis = candidate.analysis
        last_close = analysis.last_close
        hv_20d = analysis.volatility.hv_20d

        invalidation_prob = LevelProbability(
            price=candidate.invalidation_price,
            distance_pct=abs(last_close - candidate.invalidation_price) / last_close * 100,
            touch_probability_5d=estimate_touch_probability(last_close, candidate.invalidation_price, hv_20d),
        )
        zone1_prob = None
        if candidate.zone1_price is not None:
            zone1_prob = LevelProbability(
                price=candidate.zone1_price,
                distance_pct=abs(candidate.zone1_price - last_close) / last_close * 100,
                touch_probability_5d=estimate_touch_probability(last_close, candidate.zone1_price, hv_20d),
            )
        zone2_prob = None
        if candidate.zone2_price is not None:
            zone2_prob = LevelProbability(
                price=candidate.zone2_price,
                distance_pct=abs(candidate.zone2_price - last_close) / last_close * 100,
                touch_probability_5d=estimate_touch_probability(last_close, candidate.zone2_price, hv_20d),
            )

        plotted_levels = [
            value
            for value in (
                candidate.invalidation_price,
                candidate.zone1_price,
                candidate.zone2_price,
                last_close,
            )
            if value is not None
        ]
        lower_level, upper_level = min(plotted_levels), max(plotted_levels)
        span = upper_level - lower_level
        bar_price_pct = (
            max(0.0, min(100.0, (last_close - lower_level) / span * 100))
            if span > 0
            else 50.0
        )

        # Persist now — deterministic-only, zero cost, but still an "analysis" whose
        # audit targets the self-audit job must be able to find later (CLAUDE.md rule 6).
        record = repository.create_analysis(
            db,
            symbol=candidate.symbol,
            market_open=market_open,
            data_provider=market_data_provider.provider_name,
            deterministic_json=analysis.model_dump(mode="json"),
            scores=analysis.scores.model_dump(),
            regime=analysis.regime.label,
            status="completed",
            kind="snipe",
        )
        repository.create_snipe_audit_targets(
            db,
            record.id,
            candidate.symbol,
            last_close,
            candidate.zone1_price,
            candidate.zone2_price,
            candidate.invalidation_price,
            direction=candidate.direction,
        )

        cards.append(
            SnipeStockCard(
                symbol=candidate.symbol,
                direction=candidate.direction,
                last_close=last_close,
                daily_change_pct=candidate.daily_change_pct,
                readiness_score=candidate.score,
                reasons=candidate.reasons,
                invalidation=invalidation_prob,
                zone1=zone1_prob,
                zone2=zone2_prob,
                bar_price_pct=round(bar_price_pct, 1),
                atr_pct_relative=analysis.volatility.atr_pct_relative,
                hv_20d=analysis.volatility.hv_20d,
            )
        )

    accuracy = compute_snipe_accuracy_panel(db)

    result = SnipeStockScanResult(
        generated_at=datetime.now(timezone.utc),
        universe_size=len(universe),
        scanned_count=fetched_count,
        cards=cards,
        accuracy=accuracy,
    )

    cache.set(
        cache_key,
        {"_cached_at": datetime.now(timezone.utc).isoformat(), "result": result.model_dump(mode="json")},
        ttl_seconds=settings.cache_ttl_snipe_seconds,
    )

    return result


US_MARKET_CLOSE = time(16, 0)
US_MARKET_TZ = ZoneInfo("America/New_York")


def _as_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip even for `DateTime(timezone=True)` columns
    (same quirk `db/repository.py::cache_get` already works around) — everything written
    here is UTC, so a naive value read back is always UTC too."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _expiry_close_utc(expiry_date: date) -> datetime:
    """Approximate absolute expiry instant: standard US equity options expire at the
    4:00 PM America/New_York close (zoneinfo handles the EDT/EST DST switch correctly;
    the feed itself has no per-contract settlement time, so this is a declared
    approximation, not a live value)."""
    return datetime.combine(expiry_date, US_MARKET_CLOSE, tzinfo=US_MARKET_TZ).astimezone(timezone.utc)


def _translate_level(
    level: LevelProbability | None,
    *,
    strike: float,
    time_to_expiry_years: float,
    implied_volatility: float,
    option_type: str,
    risk_free_rate: float,
) -> LevelProbability | None:
    if level is None:
        return None
    translated_price = reprice_contract_at_stock_level(
        level.price,
        strike,
        time_to_expiry_years,
        implied_volatility,
        option_type,
        risk_free_rate,
    )
    return LevelProbability(
        price=translated_price,
        distance_pct=level.distance_pct,
        touch_probability_5d=level.touch_probability_5d,
    )


def _expiry_aligned_level(
    level: LevelProbability | None,
    *,
    current_price: float,
    hv_20d: float,
    horizon_days: float,
) -> LevelProbability | None:
    if level is None:
        return None
    return LevelProbability(
        price=level.price,
        distance_pct=level.distance_pct,
        touch_probability_5d=estimate_touch_probability(
            current_price, level.price, hv_20d, horizon_days=horizon_days
        ),
    )


# Risk-balance blend weight for the Snipe options tab's displayed score (owner request
# 2026-07-18): a contract can score well mechanically (liquid, tight spread, balanced
# delta) while still chasing a target that's statistically much less likely to hit than
# the invalidation — that combination must not read as a high "quality" score. Declared,
# inspectable formula (CLAUDE.md rule 2):
#
#   risk_balance_component = zone1_prob / (zone1_prob + invalidation_prob)   # 0-1
#   FinalScore = 0.7 x MechanicalQualityScore + 0.3 x (100 x risk_balance_component)
#
# risk_imbalanced=True (drives the UI's red "⚠️" badge) whenever the invalidation is
# strictly more likely to hit first than the first target zone — a blunter, unambiguous
# signal than the blended number alone, since a single score can still mask this.
RISK_BALANCE_WEIGHT = 0.3


def _risk_balance(zone1: LevelProbability | None, invalidation: LevelProbability | None) -> tuple[float, bool]:
    if zone1 is None or invalidation is None:
        return 0.5, False
    zone1_prob, invalidation_prob = zone1.touch_probability_5d, invalidation.touch_probability_5d
    total = zone1_prob + invalidation_prob
    component = zone1_prob / total if total > 0 else 0.5
    imbalanced = invalidation_prob > zone1_prob
    return component, imbalanced


def run_snipe_options_scan(db: Session, settings: Settings | None = None) -> SnipeOptionsScanResult:
    """Pick at most three direction-matched 0-2 DTE contracts inside the existing page."""
    settings = settings or get_settings()
    stock_result = run_snipe_scan(db, settings)

    cache = DBCacheAdapter(db)
    cache_key = make_cache_key("snipe", "options_v2")
    cached = cache.get(cache_key)
    if cached is not None:
        try:
            cached_at = datetime.fromisoformat(cached["_cached_at"])
            age_minutes = round((datetime.now(timezone.utc) - cached_at).total_seconds() / 60, 1)
            result = SnipeOptionsScanResult.model_validate(cached["result"])
            result.from_cache = True
            result.cached_minutes_ago = age_minutes
            return result
        except (ValidationError, KeyError):
            cache.delete(cache_key)

    use_alpaca_options = settings.market_data_provider.lower() == "alpaca" and bool(
        settings.alpaca_api_key and settings.alpaca_api_secret
    )
    options_provider_label = "alpaca_options" if use_alpaca_options else "yfinance_options"
    logger.info(
        "snipe options scan using %s (MARKET_DATA_PROVIDER=%s, alpaca keys %s)",
        options_provider_label, settings.market_data_provider,
        "present" if (settings.alpaca_api_key and settings.alpaca_api_secret) else "MISSING",
    )

    gate = CostGate(db, settings)
    decision = gate.check_and_reserve(category="market_data", provider=options_provider_label, estimated_cost=0.0)
    if not decision.allowed:
        raise CostLimitExceededError(decision.reason)

    cards: list[SnipeOptionCard] = []
    skipped: list[str] = []
    try:
        for stock_card in stock_result.cards:
            option_type = "call" if stock_card.direction == "bullish" else "put"
            try:
                if use_alpaca_options:
                    expirations = get_alpaca_expirations(
                        stock_card.symbol,
                        option_type=option_type,
                        max_dte=settings.snipe_option_max_dte,
                    )
                    best = rank_best_contract(
                        expirations,
                        lambda expiry_str, sym=stock_card.symbol, price=stock_card.last_close, kind=option_type: fetch_alpaca_chain(
                            sym,
                            expiry_str,
                            underlying_price=price,
                            risk_free_rate=settings.risk_free_rate,
                            option_type=kind,
                        ),
                        underlying_price=stock_card.last_close,
                        atr_pct_relative=stock_card.atr_pct_relative,
                        risk_free_rate=settings.risk_free_rate,
                        option_type=option_type,
                        max_dte=settings.snipe_option_max_dte,
                        max_premium=settings.snipe_option_max_premium,
                        max_spread_pct=settings.snipe_option_max_spread_pct,
                        min_open_interest=settings.snipe_option_min_open_interest,
                        min_abs_delta=settings.snipe_option_min_abs_delta,
                        max_abs_delta=settings.snipe_option_max_abs_delta,
                        max_theta_decay_pct=settings.snipe_option_max_theta_decay_pct,
                    )
                else:
                    expirations = get_yfinance_expirations(stock_card.symbol)
                    best = rank_best_contract(
                        expirations,
                        lambda expiry_str, sym=stock_card.symbol, kind=option_type: fetch_yfinance_chain(
                            sym, expiry_str, kind
                        ),
                        underlying_price=stock_card.last_close,
                        atr_pct_relative=stock_card.atr_pct_relative,
                        risk_free_rate=settings.risk_free_rate,
                        option_type=option_type,
                        max_dte=settings.snipe_option_max_dte,
                        max_premium=settings.snipe_option_max_premium,
                        max_spread_pct=settings.snipe_option_max_spread_pct,
                        min_open_interest=settings.snipe_option_min_open_interest,
                        min_abs_delta=settings.snipe_option_min_abs_delta,
                        max_abs_delta=settings.snipe_option_max_abs_delta,
                        max_theta_decay_pct=settings.snipe_option_max_theta_decay_pct,
                    )
            except Exception as chain_exc:
                logger.warning(
                    "options chain lookup failed for %s via %s — symbol skipped: %s",
                    stock_card.symbol, options_provider_label, chain_exc,
                )
                best = None

            if best is None:
                skipped.append(stock_card.symbol)
                continue

            now = datetime.now(timezone.utc)
            dte_days = max(0, (best.expiry - now.date()).days)
            is_0dte = dte_days == 0
            expiry_close_utc = _expiry_close_utc(best.expiry)
            remaining_hours = max(0.0, (expiry_close_utc - now).total_seconds() / 3600)
            hours_to_expiry = remaining_hours if is_0dte else None
            probability_horizon_days = (
                min(1.0, max(remaining_hours / 6.5, 5 / (6.5 * 60)))
                if is_0dte
                else float(max(dte_days, 1))
            )
            expiry_invalidation = _expiry_aligned_level(
                stock_card.invalidation,
                current_price=stock_card.last_close,
                hv_20d=stock_card.hv_20d,
                horizon_days=probability_horizon_days,
            )
            expiry_zone1 = _expiry_aligned_level(
                stock_card.zone1,
                current_price=stock_card.last_close,
                hv_20d=stock_card.hv_20d,
                horizon_days=probability_horizon_days,
            )
            expiry_zone2 = _expiry_aligned_level(
                stock_card.zone2,
                current_price=stock_card.last_close,
                hv_20d=stock_card.hv_20d,
                horizon_days=probability_horizon_days,
            )
            risk_balance_component, risk_imbalanced = _risk_balance(
                expiry_zone1, expiry_invalidation
            )
            final_score = round(
                (1 - RISK_BALANCE_WEIGHT) * best.quality_score + RISK_BALANCE_WEIGHT * (100 * risk_balance_component), 1
            )
            reasons = list(best.reasons)
            reasons.insert(
                0,
                "اتجاه صاعد متوافق → Call"
                if option_type == "call"
                else "اتجاه هابط متوافق → Put",
            )
            if risk_imbalanced:
                reasons.append(
                    "احتمال الإبطال أعلى من المنطقة الأولى ضمن عمر العقد — خُفّضت الدرجة"
                )

            time_to_expiry = years_to_expiry(best.expiry, now.date())
            translation_args = {
                "strike": best.strike,
                "time_to_expiry_years": time_to_expiry,
                "implied_volatility": best.implied_volatility,
                "option_type": option_type,
                "risk_free_rate": settings.risk_free_rate,
            }
            spread_pct = (
                (best.ask - best.bid) / ((best.ask + best.bid) / 2)
                if best.bid and best.ask
                else 1.0
            )

            card = SnipeOptionCard(
                    symbol=stock_card.symbol,
                    option_type=option_type,
                    strike=best.strike,
                    expiry=best.expiry.isoformat(),
                    contract_price=best.contract_price,
                    bid=float(best.bid),
                    ask=float(best.ask),
                    spread_pct=round(spread_pct, 4),
                    premium_total=round(best.contract_price * 100, 2),
                    quality_score=final_score,
                    mechanical_quality_score=best.quality_score,
                    risk_balance_component=round(risk_balance_component, 3),
                    risk_imbalanced=risk_imbalanced,
                    delta=float(best.greeks.delta),
                    gamma=float(best.greeks.gamma),
                    theta=float(best.greeks.theta),
                    vega=float(best.greeks.vega),
                    daily_theta_decay_pct=daily_theta_decay_pct(float(best.greeks.theta), best.contract_price),
                    invalidation=_translate_level(expiry_invalidation, **translation_args),
                    zone1=_translate_level(expiry_zone1, **translation_args),
                    zone2=_translate_level(expiry_zone2, **translation_args),
                    reasons=reasons,
                    dte_days=dte_days,
                    is_0dte=is_0dte,
                    expiry_close_utc=expiry_close_utc,
                    hours_to_expiry=hours_to_expiry,
                    probability_horizon_days=round(probability_horizon_days, 3),
                )
            cards.append(card)
            repository.create_snipe_option_signal(
                db,
                symbol=card.symbol,
                option_type=card.option_type,
                strike=card.strike,
                expiry=datetime.combine(best.expiry, time.min),
                underlying_price=stock_card.last_close,
                bid=card.bid,
                ask=card.ask,
                mid_price=card.contract_price,
                score=card.quality_score,
                signal_json=card.model_dump(mode="json"),
            )
    except Exception:
        gate.record_actual(decision.ledger_id, 0.0)
        raise
    gate.record_actual(decision.ledger_id, 0.0)

    cards.sort(key=lambda card: card.quality_score, reverse=True)
    cards = cards[:3]
    data_source_note = (
        "بيانات Alpaca indicative المجانية للاختبار وليست OPRA الرسمية؛ الأسعار قد تكون "
        "معدّلة أو متأخرة، لذلك الدرجات قراءات رياضية تعليمية وليست نسب نجاح تاريخية."
        if use_alpaca_options
        else
        "بيانات yfinance غير الرسمية قد تكون متأخرة؛ تستخدم هنا للتطوير فقط."
    )
    selection_policy = (
        f"اختيار مسبق داخل المحرك: اتجاه Call/Put أولاً، انتهاء 0–{settings.snipe_option_max_dte} يوم، "
        f"علاوة ≤ {settings.snipe_option_max_premium:.2f}$، سبريد ≤ "
        f"{settings.snipe_option_max_spread_pct * 100:.0f}%، وفائدة مفتوحة ≥ "
        f"{settings.snipe_option_min_open_interest}. يعرض أفضل 3 فقط وقد لا يعرض أي عقد."
    )
    result = SnipeOptionsScanResult(
        generated_at=datetime.now(timezone.utc), cards=cards, skipped_symbols=skipped,
        data_source_note=data_source_note, selection_policy=selection_policy,
    )
    cache.set(
        cache_key,
        {"_cached_at": datetime.now(timezone.utc).isoformat(), "result": result.model_dump(mode="json")},
        ttl_seconds=settings.cache_ttl_snipe_seconds,
    )
    return result


# --- Options watchlist ("مراقب حالة قرائي" — graduated read-only status indicator,
# owner request 2026-07-19, supersedes an earlier hard "-5% sell alert" design from the
# same conversation). No trade execution anywhere in this section — every function here
# only reads a price and classifies it (engines/screener/watchlist_status.py). ---

WATCHLIST_CONTRACT_CACHE_TTL_SECONDS = 60  # matches the frontend's intended poll cadence


def _watchlist_options_provider(settings: Settings) -> tuple[bool, str]:
    use_alpaca_options = settings.market_data_provider.lower() == "alpaca" and bool(
        settings.alpaca_api_key and settings.alpaca_api_secret
    )
    return use_alpaca_options, "alpaca_options" if use_alpaca_options else "yfinance_options"


def _fetch_watchlist_contract_quote(
    db: Session, item, settings: Settings, use_alpaca_options: bool, underlying_price: float
) -> tuple[float, float, float] | None:
    """(contract_price, delta, theta) for one watched contract's current chain row, or
    None if it can't be located this refresh (e.g. contract rolled off the feed). Cache-
    first per contract with its own short TTL, so a burst of watchlist polls (or several
    browser tabs) only reaches the provider once per interval — the same discipline as
    `get_live_quote`'s per-symbol quote cache."""
    cache = DBCacheAdapter(db)
    cache_key = make_cache_key("watchlist_contract", str(item.id))
    cached = cache.get(cache_key)
    if cached is not None:
        return cached["price"], cached["delta"], cached["theta"]

    expiry_str = item.expiry.isoformat()
    try:
        if use_alpaca_options:
            chain = fetch_alpaca_chain(
                item.underlying_symbol, expiry_str, underlying_price=underlying_price,
                risk_free_rate=settings.risk_free_rate, option_type=item.option_type,
            )
        else:
            chain = fetch_yfinance_chain(
                item.underlying_symbol, expiry_str, item.option_type
            )
    except Exception as chain_exc:
        logger.warning(
            "watchlist chain refresh failed for %s %s %s — status shows stale/unknown this poll: %s",
            item.underlying_symbol, item.strike, expiry_str, chain_exc,
        )
        return None
    if chain is None or chain.empty:
        return None
    row = chain[chain["strike"] == float(item.strike)]
    if row.empty:
        return None
    row = row.iloc[0]
    iv = row.get("impliedVolatility")
    if iv is None or iv != iv or iv <= 0:
        return None
    time_to_expiry = years_to_expiry(item.expiry, datetime.now(timezone.utc).date())
    try:
        greeks = compute_greeks(
            underlying_price,
            float(item.strike),
            time_to_expiry,
            float(iv),
            item.option_type,
            settings.risk_free_rate,
        )
    except Exception:
        return None
    bid, ask, last_price = row.get("bid"), row.get("ask"), row.get("lastPrice")
    price = (float(bid) + float(ask)) / 2 if bid and ask else None
    if not price and last_price and last_price == last_price:
        price = float(last_price)
    if not price:
        return None
    price = round(price, 2)
    result = {"price": price, "delta": float(greeks.delta), "theta": float(greeks.theta)}
    cache.set(cache_key, result, ttl_seconds=WATCHLIST_CONTRACT_CACHE_TTL_SECONDS)
    return price, float(greeks.delta), float(greeks.theta)


def _watchlist_item_out(item, status, worsened: bool) -> WatchlistItemOut:
    return WatchlistItemOut(
        id=str(item.id), underlying_symbol=item.underlying_symbol, option_type=item.option_type,
        strike=float(item.strike), expiry=item.expiry.isoformat(), reference_price=float(item.reference_price),
        alert_threshold_pct=float(item.alert_threshold_pct),
        invalidation_price=float(item.invalidation_price) if item.invalidation_price is not None else None,
        added_at=_as_utc(item.added_at), last_checked_at=_as_utc(item.last_checked_at) if item.last_checked_at else None,
        last_price=float(item.last_price) if item.last_price is not None else None,
        status_tier=status.tier, status_code=status.code, status_emoji=status.emoji,
        status_mood_emoji=status.mood_emoji, status_label=status.label, status_message=status.message,
        change_pct=status.change_pct, is_0dte=item.expiry == datetime.now(timezone.utc).date(),
        hours_to_expiry=(
            max(0.0, (_expiry_close_utc(item.expiry) - datetime.now(timezone.utc)).total_seconds() / 3600)
            if item.expiry == datetime.now(timezone.utc).date() else None
        ),
        worsened=worsened,
    )


def add_watchlist_item(
    db: Session, *, underlying_symbol: str, option_type: str, strike: float, expiry: str,
    reference_price: float, alert_threshold_pct: float = 5.0, invalidation_price: float | None = None,
) -> WatchlistItemOut:
    symbol = validate_symbol(underlying_symbol)
    expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
    # The card's displayed invalidation is a delta+gamma Taylor translation of a stock-
    # level price into contract-price space (iv_metrics.translate_stock_level_to_contract_
    # price) — reliable near the current price, but its own docstring warns the gamma term
    # can dominate and produce a nonsensical *higher* contract price for a level that's far
    # from the underlying (observed live: an XLF call showing a $4.73 "invalidation" above
    # a $0.46 contract price). For a call, invalidation must be a worse (lower) contract
    # price than the reference — anything else is a translation artifact, not a real
    # invalidation, so it's dropped rather than used as a false breach trigger.
    if invalidation_price is not None and invalidation_price >= reference_price:
        invalidation_price = None
    record = repository.create_watchlist_item(
        db, underlying_symbol=symbol, option_type=option_type, strike=strike, expiry=expiry_date,
        reference_price=reference_price, alert_threshold_pct=alert_threshold_pct,
        invalidation_price=invalidation_price,
    )
    is_0dte = expiry_date == datetime.now(timezone.utc).date()
    hours_to_expiry = (
        max(0.0, (_expiry_close_utc(expiry_date) - datetime.now(timezone.utc)).total_seconds() / 3600)
        if is_0dte else None
    )
    status = compute_watchlist_status(
        reference_price=reference_price, current_price=reference_price, alert_threshold_pct=alert_threshold_pct,
        invalidation_price=invalidation_price, hours_since_added=0.0, is_0dte=is_0dte, hours_to_expiry=hours_to_expiry,
    )
    now = datetime.now(timezone.utc)
    record = repository.update_watchlist_status(
        db, record.id, checked_at=now, price=reference_price, status_tier=status.tier,
        status_code=status.code, message=status.message,
    )
    repository.create_watchlist_event(
        db, record.id, status_tier=status.tier, status_code=status.code, price=reference_price,
        change_pct=status.change_pct, message="أُضيف للمراقبة — هذا السعر هو المرجع",
    )
    return _watchlist_item_out(record, status, worsened=False)


def remove_watchlist_item(db: Session, item_id: uuid.UUID) -> None:
    repository.deactivate_watchlist_item(db, item_id)


def get_watchlist_item_events(db: Session, item_id: uuid.UUID) -> list[WatchlistEventOut]:
    events = repository.get_watchlist_events(db, item_id)
    return [
        WatchlistEventOut(
            occurred_at=_as_utc(e.occurred_at), status_tier=e.status_tier, status_code=e.status_code,
            price=float(e.price), change_pct=float(e.change_pct), message=e.message,
        )
        for e in events
    ]


def _get_underlying_price_under_batch_reservation(
    db: Session, symbol: str, settings: Settings
) -> float | None:
    """Underlying quote for one watched contract, WITHOUT its own cost-gate reservation —
    the caller (`list_watchlist_with_status`) has already reserved once for the whole
    refresh batch, and that reservation covers these fetches (CLAUDE.md rule 3 is
    satisfied at the batch boundary, same precedent as `run_screener_scan`'s one
    reservation for a ~200-symbol pass). Calling `get_live_quote` here instead would add
    one ledger row per watched symbol per poll, which is exactly the calls/minute pattern
    the anomaly guard auto-kill-switches on — a self-inflicted outage from healthy code
    (observed live 2026-07-19: 2 items + 30s polling + page navigations tripped the guard).
    Shares `get_live_quote`'s per-symbol cache key, so the stock page's ticker and this
    refresh never double-fetch within one quote TTL."""
    cache = DBCacheAdapter(db)
    cache_key = make_cache_key("quote", symbol)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached["price"]
    try:
        provider = get_market_data_provider()
        quote = provider.get_quote(symbol)
    except Exception as quote_exc:
        logger.warning("underlying quote fetch failed for %s: %s", symbol, quote_exc)
        return None
    result = {"symbol": symbol, "price": quote.price, "as_of": quote.as_of, "is_delayed": quote.is_delayed}
    cache.set(cache_key, result, ttl_seconds=quote_cache_ttl_seconds(settings))
    return quote.price


def list_watchlist_with_status(db: Session, settings: Settings | None = None) -> list[WatchlistItemOut]:
    """Refresh + classify every active watched contract. ONE cost-gate reservation for
    the whole batch — underlying quotes AND contract chains included (CLAUDE.md rule 3,
    same reasoning as `run_snipe_options_scan`): reserving per item or per quote would
    accumulate ledger rows toward the calls/minute anomaly guard and auto-kill-switch
    the platform from an ordinary watchlist poll."""
    settings = settings or get_settings()
    items = repository.get_active_watchlist_items(db)
    if not items:
        return []

    use_alpaca_options, options_provider_label = _watchlist_options_provider(settings)
    gate = CostGate(db, settings)
    decision = gate.check_and_reserve(category="market_data", provider=options_provider_label, estimated_cost=0.0)
    if not decision.allowed:
        raise CostLimitExceededError(decision.reason)

    out: list[WatchlistItemOut] = []
    try:
        for item in items:
            underlying_price = _get_underlying_price_under_batch_reservation(db, item.underlying_symbol, settings)
            if underlying_price is not None:
                fetched = _fetch_watchlist_contract_quote(db, item, settings, use_alpaca_options, underlying_price)
            else:
                fetched = None

            now = datetime.now(timezone.utc)
            if fetched is None:
                # Keep the last known reading rather than dropping the card — a transient
                # fetch failure shouldn't erase the user's watch state.
                if item.last_status_tier is None:
                    continue
                status = compute_watchlist_status(
                    reference_price=float(item.reference_price), current_price=float(item.last_price or item.reference_price),
                    alert_threshold_pct=float(item.alert_threshold_pct),
                    invalidation_price=float(item.invalidation_price) if item.invalidation_price is not None else None,
                    hours_since_added=max((now - _as_utc(item.added_at)).total_seconds() / 3600, 0.0),
                    is_0dte=item.expiry == now.date(),
                    hours_to_expiry=(
                        max(0.0, (_expiry_close_utc(item.expiry) - now).total_seconds() / 3600)
                        if item.expiry == now.date() else None
                    ),
                )
                out.append(_watchlist_item_out(item, status, worsened=False))
                continue

            price, _delta, theta = fetched
            theta_decay_pct_value = daily_theta_decay_pct(theta, price)
            hours_since_added = max((now - _as_utc(item.added_at)).total_seconds() / 3600, 0.0)
            is_0dte = item.expiry == now.date()
            hours_to_expiry = (
                max(0.0, (_expiry_close_utc(item.expiry) - now).total_seconds() / 3600) if is_0dte else None
            )
            status = compute_watchlist_status(
                reference_price=float(item.reference_price), current_price=price,
                alert_threshold_pct=float(item.alert_threshold_pct),
                invalidation_price=float(item.invalidation_price) if item.invalidation_price is not None else None,
                hours_since_added=hours_since_added, is_0dte=is_0dte, hours_to_expiry=hours_to_expiry,
                daily_theta_decay_pct=theta_decay_pct_value,
            )
            worsened = item.last_status_tier is not None and status.tier > item.last_status_tier
            changed = status.tier != item.last_status_tier

            item = repository.update_watchlist_status(
                db, item.id, checked_at=now, price=price, status_tier=status.tier,
                status_code=status.code, message=status.message,
            )
            if changed:
                repository.create_watchlist_event(
                    db, item.id, status_tier=status.tier, status_code=status.code, price=price,
                    change_pct=status.change_pct, message=status.message,
                )
            out.append(_watchlist_item_out(item, status, worsened=worsened))
    except Exception:
        gate.record_actual(decision.ledger_id, 0.0)
        raise
    gate.record_actual(decision.ledger_id, 0.0)
    return out


def run_option_image_analysis(
    db: Session, image_bytes: bytes, media_type: str, settings: Settings | None = None
) -> OptionContractAnalysis:
    """M2-B flow (Annex C-2): vision extraction -> underlying data -> Greeks/Expected Move
    -> report. Shared by the JSON API route and the web UI's option tab so the cost-gate
    discipline lives in exactly one place (CLAUDE.md rule 3).

    Note: unlike the stock flow, the vision call MUST happen first here — it's the only
    way to learn the symbol to fetch data for, so there's no free pre-check to move ahead
    of it. The market-data step (after vision) still goes through its own gate check first.
    """
    settings = settings or get_settings()
    gate = CostGate(db, settings)
    llm_adapter = get_llm_adapter()

    estimated_vision_cost = llm_adapter.estimate_cost(ESTIMATED_IMAGE_INPUT_TOKENS, VISION_MAX_TOKENS)
    vision_decision = gate.check_and_reserve(
        category="llm", provider=llm_adapter.provider_name, estimated_cost=estimated_vision_cost
    )
    if not vision_decision.allowed:
        raise CostLimitExceededError(vision_decision.reason)

    try:
        extracted, llm_response = extract_contract_from_image(
            llm_adapter, image_bytes, media_type, VISION_MAX_TOKENS
        )
    except ValueError as exc:
        gate.record_actual(vision_decision.ledger_id, 0.0)
        raise VisionExtractionError(str(exc)) from exc
    gate.record_actual(vision_decision.ledger_id, llm_response.cost_usd)

    market_data_provider = get_market_data_provider()
    data_cost = market_data_provider.estimated_cost_per_call()
    data_decision = gate.check_and_reserve(
        category="market_data", provider=market_data_provider.provider_name, estimated_cost=data_cost
    )
    if not data_decision.allowed:
        raise CostLimitExceededError(data_decision.reason)

    try:
        daily = market_data_provider.get_daily_ohlcv(extracted.symbol, lookback_days=300)
    except Exception as exc:
        gate.record_actual(data_decision.ledger_id, 0.0)
        raise DataFetchError(f"failed to fetch market data for '{extracted.symbol}': {exc}") from exc
    gate.record_actual(data_decision.ledger_id, data_cost)

    result = analyze_option_contract(
        extracted, daily, settings.risk_free_rate, vision_cost_usd=llm_response.cost_usd
    )

    record_option_contract_analysis(
        db,
        symbol=result.symbol,
        option_type=result.option_type,
        strike=result.strike,
        expiry=datetime.combine(result.expiry, datetime.min.time()),
        contract_price_at_analysis=result.contract_price_at_analysis,
        underlying_price=result.underlying_price,
        implied_volatility=result.implied_volatility,
        analysis_json=result.model_dump(mode="json"),
        extraction_confidence=result.extraction_confidence,
        vision_provider=llm_adapter.provider_name,
        total_cost_usd=result.cost_usd,
        status="completed",
    )

    return result
