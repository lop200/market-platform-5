from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from app.core.cost_gate import CostGate
from app.core.orchestrator import CostLimitExceededError, DataFetchError, InvalidSymbolError, run_analysis
from app.db.models import AuditTarget, CostLedger
from app.engines.llm.adapters.base import LLMAdapter, LLMResponse
from app.providers.base import MarketDataAdapter, Quote


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2025-01-01", periods=n, freq="B")


def _zigzag_daily(n: int = 250) -> pd.DataFrame:
    pattern = [100, 110, 120, 125, 128, 130, 125, 118, 110, 118, 128, 138, 145, 138, 128, 120, 125, 130]
    closes = (pattern * ((n // len(pattern)) + 2))[:n]
    idx = _dates(n)
    volume = pd.Series([2_000_000] * n, index=idx)
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": volume}, index=idx
    )


class ScriptedAdapter(LLMAdapter):
    def __init__(self, canned_texts=None, raise_exc: Exception | None = None):
        self._responses = list(canned_texts or [])
        self._raise_exc = raise_exc
        self.calls = 0
        self.provider_name_value = "scripted"

    def generate(self, system_prompt, user_content, max_tokens):
        if self._raise_exc is not None:
            raise self._raise_exc
        text = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return LLMResponse(text=text, input_tokens=900, output_tokens=250, cost_usd=0.004)

    def extract_json_from_image(self, *a, **k):
        raise NotImplementedError

    def estimate_cost(self, input_tokens, max_output_tokens):
        return 0.004

    def count_tokens(self, text):
        return len(text) // 4

    @property
    def provider_name(self):
        return self.provider_name_value


class FakeMarketDataAdapter(MarketDataAdapter):
    def __init__(self, daily: pd.DataFrame, market_open: bool = True):
        self._daily = daily
        self._market_open = market_open
        self.get_daily_calls = 0

    def get_daily_ohlcv(self, symbol, lookback_days):
        self.get_daily_calls += 1
        return self._daily

    def get_intraday(self, symbol, interval):
        return None

    def get_quote(self, symbol):
        return Quote(symbol=symbol, price=float(self._daily["close"].iloc[-1]), bid=None, ask=None, volume=None, as_of="2026-01-01T00:00:00Z", is_delayed=True)

    def estimated_cost_per_call(self):
        return 0.0

    def is_market_open(self):
        return self._market_open

    @property
    def provider_name(self):
        return "fake_market_data"


def _valid_report_json(symbol: str, technical_score: float) -> str:
    return json.dumps(
        {
            "tldr_ar": f"{symbol} بدرجة فنية {technical_score:.0f}.",
            "scenario_bullish_ar": "كسر فوق المقاومة يفتح المجال للصعود.",
            "scenario_bearish_ar": "كسر تحت الدعم يفتح المجال للهبوط.",
            "scenario_neutral_ar": "البقاء ضمن النطاق الحالي.",
            "full_report_ar": f"تحليل {symbol} بدرجة فنية {technical_score:.0f}.",
            "devils_advocate_ar": "لم يرصد المحرك إنذارات بنيوية بارزة — وهذا بحد ذاته يستدعي الحذر من الرضا الزائد.",
        },
        ensure_ascii=False,
    )


@pytest.fixture
def patched(monkeypatch, db_session):
    daily = _zigzag_daily()
    market_provider = FakeMarketDataAdapter(daily)
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: market_provider)
    return db_session, market_provider, daily


def test_run_analysis_happy_path(monkeypatch, patched, test_settings):
    db_session, market_provider, daily = patched
    adapter = ScriptedAdapter([_valid_report_json("NVDA", 60.0)])
    monkeypatch.setattr("app.core.orchestrator.get_llm_adapter", lambda: adapter)

    response = run_analysis(db_session, "nvda", lang="ar", settings=test_settings)

    assert response.symbol == "NVDA"
    assert response.from_cache is False
    assert response.cost_usd == pytest.approx(0.004)
    assert response.report_ar is not None
    assert "NVDA" in response.report_ar
    assert response.disclaimer

    targets = db_session.query(AuditTarget).filter(AuditTarget.symbol == "NVDA").all()
    assert len(targets) >= 1


def test_run_analysis_second_call_hits_cache(monkeypatch, patched, test_settings):
    db_session, market_provider, daily = patched
    adapter = ScriptedAdapter([_valid_report_json("NVDA", 60.0)])
    monkeypatch.setattr("app.core.orchestrator.get_llm_adapter", lambda: adapter)

    first = run_analysis(db_session, "NVDA", lang="ar", settings=test_settings)
    assert first.from_cache is False
    calls_after_first = market_provider.get_daily_calls

    second = run_analysis(db_session, "NVDA", lang="ar", settings=test_settings)
    assert second.from_cache is True
    assert second.cost_usd == 0.0
    assert market_provider.get_daily_calls == calls_after_first  # no new fetch on cache hit


def test_run_analysis_recovers_from_stale_cache_schema(monkeypatch, patched, test_settings):
    """Regression test for a real incident: an older AnalyzeResponse cached before new
    required fields (indicators/data_provider/data_as_of) were added must not crash the
    request — it should be treated as a cache miss and a fresh analysis run instead."""
    from datetime import datetime, timezone

    from app.core.cache import DBCacheAdapter, make_cache_key, report_cache_ttl_seconds

    db_session, market_provider, daily = patched
    adapter = ScriptedAdapter([_valid_report_json("NVDA", 60.0)])
    monkeypatch.setattr("app.core.orchestrator.get_llm_adapter", lambda: adapter)

    stale_cached_response = {
        "analysis_id": "old-id", "symbol": "NVDA", "from_cache": False, "cached_minutes_ago": None,
        "regime": "ranging", "scores": {"technical": 50, "volatility": 50, "liquidity": 50, "risk": 50, "overall_confidence": 50},
        "score_formulas_ref": "/docs/scoring",
        "notable_levels": {"supports": [], "resistances": [], "invalidation": None},
        "report_ar": "old report", "report_en": None, "devils_advocate_ar": "old", "devils_advocate_en": None,
        "disclaimer": "old disclaimer", "cost_usd": 0.01, "market_open": True, "data_quality": "daily_only",
        # indicators/data_provider/data_as_of deliberately missing (pre-upgrade shape)
    }
    cache = DBCacheAdapter(db_session)
    cache.set(
        make_cache_key("report", "NVDA", "ar"),
        {"_cached_at": datetime.now(timezone.utc).isoformat(), "response": stale_cached_response},
        ttl_seconds=report_cache_ttl_seconds(True, test_settings),
    )

    response = run_analysis(db_session, "NVDA", lang="ar", settings=test_settings)
    assert response.from_cache is False  # treated as a miss, not a crash
    assert response.indicators is not None
    assert response.cost_usd > 0  # a real (fresh) analysis ran, not the free stale-cache path


def test_run_analysis_rejects_invalid_symbol(patched, test_settings):
    db_session, _, _ = patched
    with pytest.raises(InvalidSymbolError):
        run_analysis(db_session, "not a symbol!!", lang="ar", settings=test_settings)


def test_run_analysis_raises_when_cost_gate_blocks(monkeypatch, patched, test_settings):
    db_session, market_provider, daily = patched
    adapter = ScriptedAdapter([_valid_report_json("NVDA", 60.0)])
    monkeypatch.setattr("app.core.orchestrator.get_llm_adapter", lambda: adapter)

    gate = CostGate(db_session, test_settings)
    gate.enable_kill_switch()

    with pytest.raises(CostLimitExceededError):
        run_analysis(db_session, "NVDA", lang="ar", settings=test_settings)


def test_run_analysis_falls_back_when_llm_raises(monkeypatch, patched, test_settings):
    db_session, market_provider, daily = patched
    adapter = ScriptedAdapter(raise_exc=ConnectionError("network down"))
    monkeypatch.setattr("app.core.orchestrator.get_llm_adapter", lambda: adapter)

    response = run_analysis(db_session, "NVDA", lang="ar", settings=test_settings)
    assert response.report_ar is not None
    assert response.cost_usd == 0.0  # fallback path never charged anything


def test_run_analysis_handles_quote_fetch_failure_gracefully(monkeypatch, patched, test_settings):
    db_session, market_provider, daily = patched

    def _raise_quote(symbol):
        raise ConnectionError("quote endpoint down")

    monkeypatch.setattr(market_provider, "get_quote", _raise_quote)
    adapter = ScriptedAdapter([_valid_report_json("NVDA", 60.0)])
    monkeypatch.setattr("app.core.orchestrator.get_llm_adapter", lambda: adapter)

    response = run_analysis(db_session, "NVDA", lang="ar", settings=test_settings)
    assert response.data_quality == "daily_only"


def test_run_analysis_bad_symbol_never_reserves_llm_budget(monkeypatch, db_session, test_settings):
    """The core fix: a symbol that fails at the (free) market-data step must never touch
    the LLM cost gate at all — no llm-category ledger row, no adapter.generate() call."""

    class FailingMarketDataAdapter(FakeMarketDataAdapter):
        def get_daily_ohlcv(self, symbol, lookback_days):
            raise ValueError(f"no daily OHLCV returned for symbol '{symbol}'")

    market_provider = FailingMarketDataAdapter(pd.DataFrame())
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: market_provider)
    adapter = ScriptedAdapter([_valid_report_json("APPLE", 60.0)])
    monkeypatch.setattr("app.core.orchestrator.get_llm_adapter", lambda: adapter)

    with pytest.raises(DataFetchError):
        run_analysis(db_session, "APPLE", lang="ar", settings=test_settings)

    assert adapter.calls == 0  # the LLM was never even constructed/called
    llm_rows = db_session.query(CostLedger).filter(CostLedger.category == "llm").all()
    assert llm_rows == []
    # the market_data reservation itself IS recorded (SRS AD-4: no path bypasses the gate)
    data_rows = db_session.query(CostLedger).filter(CostLedger.category == "market_data").all()
    assert len(data_rows) == 1
    assert float(data_rows[0].actual_cost) == 0.0


def test_run_analysis_data_cost_gate_blocks_before_any_fetch(monkeypatch, db_session, test_settings):
    market_provider = FakeMarketDataAdapter(_zigzag_daily())
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: market_provider)
    CostGate(db_session, test_settings).enable_kill_switch()

    with pytest.raises(CostLimitExceededError):
        run_analysis(db_session, "NVDA", lang="ar", settings=test_settings)

    assert market_provider.get_daily_calls == 0  # blocked before the data call ever happened


def test_regime_to_scenario_mapping():
    from app.core.orchestrator import _regime_to_scenario

    assert _regime_to_scenario("trending_up") == "bullish"
    assert _regime_to_scenario("trending_down") == "bearish"
    assert _regime_to_scenario("ranging") == "neutral"
    assert _regime_to_scenario("high_vol") == "neutral"
