"""Screener scan orchestration: fetch the candidate universe once (threaded — I/O bound),
compute both screener views from the same fetched data, and rank each (SRS-adjacent new
feature, CLAUDE.md rule 1: deterministic engine only, no LLM anywhere in this module).
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd

from app.engines.deterministic.engine import run_deterministic_engine
from app.engines.deterministic.schemas import DeterministicAnalysis
from app.engines.screener.daily_readiness import compute_daily_readiness
from app.engines.screener.schemas import ScreenerCard, ScreenerResult
from app.engines.screener.weekly_structure import compute_weekly_structure
from app.providers.base import MarketDataAdapter
from app.static_data.us_symbols import US_SYMBOLS

MAX_FETCH_WORKERS = 20
LOOKBACK_DAYS = 300
MIN_BARS_REQUIRED = 30
TOP_VOLUME_UNIVERSE = 50  # "active stocks" prefilter for the daily screener
CARDS_DISPLAYED = 15


def _fetch_one(provider: MarketDataAdapter, symbol: str) -> pd.DataFrame | None:
    try:
        daily = provider.get_daily_ohlcv(symbol, lookback_days=LOOKBACK_DAYS)
    except Exception:
        return None
    if daily is None or len(daily) < MIN_BARS_REQUIRED:
        return None
    return daily


def fetch_universe(provider: MarketDataAdapter, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Threaded fetch — these are independent network I/O calls, not sequential paid calls
    (cost-gate reservation for the whole batch happens once, before this runs)."""
    fetched: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=MAX_FETCH_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, provider, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            daily = future.result()
            if daily is not None:
                fetched[symbol] = daily
    return fetched


_NO_SIGNAL_REASONS = {
    "لا توجد إشارات جاهزية بارزة اليوم",
    "لا توجد إشارات بنية أسبوعية قوية بارزة حالياً",
    "بيانات أسبوعية غير كافية لتقييم البنية",
}


def _build_conclusion(
    symbol: str,
    kind: str,
    last_close: float,
    score: float,
    reasons: list[str],
    key_level: float | None,
    key_level_type: str | None,
    invalidation: float | None,
) -> str:
    """A synthesized, descriptive paragraph — facts and conditions only, no directive
    wording (never "opportunity", "recommendation", or entry/exit/stop-loss instructions;
    CLAUDE.md rule 5). The reader draws their own conclusion from the stated facts."""
    label = "الجاهزية الفنية اليومية" if kind == "daily_readiness" else "البنية الفنية الأسبوعية"
    sentences = [f"{symbol} يتداول حالياً عند {last_close:.2f}$، بدرجة {label} {score:.0f} من 100."]

    real_reasons = [r for r in reasons if r not in _NO_SIGNAL_REASONS]
    if real_reasons:
        sentences.append("العوامل المرصودة ضمن البيانات: " + "، ".join(real_reasons) + ".")
    else:
        sentences.append("لم تُرصد عوامل إضافية بارزة ضمن هذه القراءة.")

    if key_level is not None and last_close:
        rel = "دعم" if key_level_type == "support" else "مقاومة"
        distance_pct = abs(last_close - key_level) / last_close * 100
        sentences.append(
            f"السعر يبعد حالياً {distance_pct:.1f}% عن أقرب مستوى فني مفصلي ({rel}) عند {key_level:.2f}."
        )
    if invalidation is not None:
        sentences.append(
            f"مستوى الإبطال الفني — الذي يُنهي صلاحية هذه القراءة عند كسره — يقع عند {invalidation:.2f}."
        )

    return " ".join(sentences)


def _build_card(symbol: str, kind: str, analysis: DeterministicAnalysis, score: float, reasons: list[str]) -> ScreenerCard:
    last_close = analysis.last_close
    nearest_support = max((lv.price for lv in analysis.levels.supports), default=None)
    nearest_resistance = min((lv.price for lv in analysis.levels.resistances), default=None)

    key_level: float | None = None
    key_level_type: str | None = None
    if nearest_support is not None and nearest_resistance is not None:
        if abs(last_close - nearest_support) <= abs(nearest_resistance - last_close):
            key_level, key_level_type = nearest_support, "support"
        else:
            key_level, key_level_type = nearest_resistance, "resistance"
    elif nearest_support is not None:
        key_level, key_level_type = nearest_support, "support"
    elif nearest_resistance is not None:
        key_level, key_level_type = nearest_resistance, "resistance"

    invalidation = analysis.levels.invalidation
    conclusion = _build_conclusion(
        symbol, kind, last_close, score, reasons, key_level, key_level_type, invalidation
    )

    return ScreenerCard(
        symbol=symbol,
        score=score,
        last_close=last_close,
        key_level=key_level,
        key_level_type=key_level_type,
        invalidation=invalidation,
        nearest_resistance=nearest_resistance,
        reasons=reasons,
        conclusion=conclusion,
    )


def run_universe_scan(
    provider: MarketDataAdapter, symbols: list[str] | None = None
) -> tuple[ScreenerResult, ScreenerResult, int]:
    """Fetch once, build both screener results from the same data.

    Returns (daily_readiness_result, weekly_structure_result, successfully_fetched_count) —
    the caller uses the fetched count to compute the actual cost-gate spend.
    """
    universe = symbols if symbols is not None else [s["symbol"] for s in US_SYMBOLS]
    fetched = fetch_universe(provider, universe)

    analyses: dict[str, tuple[DeterministicAnalysis, pd.DataFrame]] = {}
    for symbol, daily in fetched.items():
        try:
            analysis = run_deterministic_engine(symbol, daily, data_quality="daily_only")
        except Exception:
            continue
        analyses[symbol] = (analysis, daily)

    now = datetime.now(timezone.utc)

    # --- Daily readiness: prefilter to the top-N by dollar volume ("active stocks"),
    # then rank that subset by the readiness score. ---
    by_volume = sorted(analyses.items(), key=lambda kv: kv[1][0].liquidity.dollar_volume, reverse=True)
    active_universe = by_volume[:TOP_VOLUME_UNIVERSE]

    daily_cards = []
    for symbol, (analysis, _daily) in active_universe:
        score, reasons = compute_daily_readiness(analysis)
        daily_cards.append(_build_card(symbol, "daily_readiness", analysis, score, reasons))
    daily_cards.sort(key=lambda c: c.score, reverse=True)

    daily_result = ScreenerResult(
        kind="daily_readiness",
        generated_at=now,
        universe_size=len(universe),
        scanned_count=len(fetched),
        failed_count=len(universe) - len(fetched),
        cards=daily_cards[:CARDS_DISPLAYED],
    )

    # --- Weekly structure: whole scanned universe, ranked by weekly structure score. ---
    weekly_cards = []
    for symbol, (analysis, daily) in analyses.items():
        score, reasons = compute_weekly_structure(daily)
        weekly_cards.append(_build_card(symbol, "weekly_structure", analysis, score, reasons))
    weekly_cards.sort(key=lambda c: c.score, reverse=True)

    weekly_result = ScreenerResult(
        kind="weekly_structure",
        generated_at=now,
        universe_size=len(universe),
        scanned_count=len(fetched),
        failed_count=len(universe) - len(fetched),
        cards=weekly_cards[:CARDS_DISPLAYED],
    )

    return daily_result, weekly_result, len(fetched)
