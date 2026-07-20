from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.cost_gate import CostGate
from app.core.orchestrator import run_screener_scan
from app.db import models  # noqa: F401
from app.db.models import CostLedger
from app.db.session import Base, get_db
from app.engines.deterministic.schemas import (
    AccumulationDistribution,
    BollingerBands,
    DeterministicAnalysis,
    Indicators,
    LevelStrength,
    Levels,
    Liquidity,
    MACDResult,
    MovingAverages,
    Regime,
    RSIDivergence,
    SMC,
    Scores,
    Volatility,
)
from app.engines.llm.report_engine import find_banned_phrases
from app.engines.screener.daily_readiness import compute_daily_readiness
from app.engines.screener.scanner import run_universe_scan
from app.engines.screener.weekly_structure import compute_weekly_structure
from app.legal.disclaimers import SCREENER_DISCLAIMER_AR
from app.main import app
from app.providers.base import MarketDataAdapter, Quote


def _analysis(
    *,
    rvol: float = 1.0,
    rsi: float = 50.0,
    histogram_rising: bool = False,
    last_close: float = 100.0,
    atr_14: float = 2.0,
    supports: list[float] | None = None,
    resistances: list[float] | None = None,
) -> DeterministicAnalysis:
    def _levels(prices: list[float] | None) -> list[LevelStrength]:
        return [
            LevelStrength(price=p, touches=3, last_touch_bars_ago=5, avg_volume_at_touches=1_000_000, strength_score=0.6)
            for p in (prices or [])
        ]

    return DeterministicAnalysis(
        symbol="TEST",
        as_of="2026-01-01T00:00:00Z",
        data_as_of="2026-01-01",
        data_quality="daily_only",
        last_close=last_close,
        indicators=Indicators(
            rsi_14=rsi,
            macd=MACDResult(macd_line=1.0, signal_line=0.5, histogram=0.5, histogram_rising=histogram_rising),
            vwap=None,
            atr_14=atr_14,
            atr_pct=2.0,
            adx_14=20.0,
            moving_averages=MovingAverages(sma_20=None, sma_50=None, sma_200=None, ema_20=None, ema_50=None, ema_200=None),
            bollinger=BollingerBands(upper=105, middle=100, lower=95, bandwidth=0.1, bandwidth_percentile_90d=None),
        ),
        levels=Levels(supports=_levels(supports), resistances=_levels(resistances), invalidation=90.0),
        volatility=Volatility(hv_20d=20.0, hv_60d=22.0, atr_pct_current=2.0, atr_pct_avg_90d=2.0, atr_pct_relative=1.0),
        liquidity=Liquidity(
            avg_volume_20d=1_000_000, rvol=rvol, dollar_volume=100_000_000, spread_pct=None,
            unusual_volume=rvol > 2.5, unusual_volume_direction="up" if rvol > 2.5 else None,
        ),
        regime=Regime(label="ranging", reasons=[]),
        smc=SMC(
            rsi_divergence=RSIDivergence(detected=False, type=None, description=None),
            order_blocks=[],
            accumulation_distribution=AccumulationDistribution(current_value=0.0, slope_20d=0.0, interpretation="neutral"),
        ),
        scores=Scores(technical=50.0, volatility=50.0, liquidity=50.0, risk=50.0, overall_confidence=50.0),
    )


def test_daily_readiness_high_score_hand_computed():
    analysis = _analysis(
        rvol=3.0, rsi=80.0, histogram_rising=True, last_close=100.0, atr_14=2.0,
        supports=[95.0], resistances=[110.0],
    )
    score, reasons = compute_daily_readiness(analysis)
    # 40*1.0 + 35*(0.6*1.0 + 0.4*1.0) + 25*max(0, 1 - 2.5/3) = 40 + 35 + 4.1667 = 79.2
    assert score == pytest.approx(79.2, abs=0.05)
    assert any("حجم غير طبيعي" in r for r in reasons)
    assert any("زخم MACD" in r for r in reasons)
    assert not any("قريب جداً" in r for r in reasons)


def test_daily_readiness_low_score_hand_computed():
    analysis = _analysis(rvol=0.5, rsi=50.0, histogram_rising=False, last_close=100.0, atr_14=2.0)
    score, reasons = compute_daily_readiness(analysis)
    # 40*(0.5/3) + 35*(0.6*0 + 0.4*0.4) + 25*0 = 6.6667 + 5.6 = 12.27
    assert score == pytest.approx(12.3, abs=0.05)
    assert reasons == ["لا توجد إشارات جاهزية بارزة اليوم"]


def test_daily_readiness_proximity_to_key_level():
    analysis = _analysis(rvol=1.0, rsi=50.0, last_close=100.0, atr_14=2.0, supports=[99.5])
    score, reasons = compute_daily_readiness(analysis)
    assert any("قريب جداً من مستوى فني مفصلي" in r for r in reasons)


def _weekly_daily(n_days: int, closes, volume) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n_days, freq="B")
    closes = np.asarray(closes, dtype=float)
    volume = np.asarray(volume, dtype=float)
    return pd.DataFrame(
        {"open": closes, "high": closes + 1, "low": closes - 1, "close": closes, "volume": volume}, index=idx
    )


def test_weekly_structure_strong_uptrend_scores_high():
    n = 140
    closes = np.linspace(100, 200, n)
    volume = np.full(n, 1_000_000.0)
    volume[-20:] = 3_000_000.0  # last ~4 weeks: much higher volume
    daily = _weekly_daily(n, closes, volume)
    score, reasons = compute_weekly_structure(daily)
    assert score > 90.0
    assert any("Higher Highs" in r for r in reasons)
    assert any("متصاعد" in r for r in reasons)


def test_weekly_structure_downtrend_scores_low():
    n = 140
    closes = np.linspace(200, 100, n)
    volume = np.full(n, 3_000_000.0)
    volume[-20:] = 1_000_000.0  # last ~4 weeks: lower volume
    daily = _weekly_daily(n, closes, volume)
    score, reasons = compute_weekly_structure(daily)
    assert score < 10.0


def test_weekly_structure_insufficient_history():
    daily = _weekly_daily(20, np.linspace(100, 110, 20), np.full(20, 1_000_000.0))
    score, reasons = compute_weekly_structure(daily)
    assert score == 0.0
    assert "بيانات أسبوعية غير كافية" in reasons[0]


def test_all_screener_reason_strings_pass_banned_phrase_filter():
    candidates = [
        "حجم غير طبيعي: 3.5× المعتاد",
        "حجم أعلى من المعتاد: 1.8×",
        "زخم MACD يتقوى",
        "قريب جداً من مستوى فني مفصلي",
        "لا توجد إشارات جاهزية بارزة اليوم",
        "فوق المتوسطين الأسبوعيين 20 و50",
        "فوق المتوسط الأسبوعي 20",
        "فوق أحد المتوسطين الأسبوعيين فقط",
        "قمم أسبوعية أعلى من سابقتها (Higher Highs)",
        "حجم تداول أسبوعي متصاعد",
        "لا توجد إشارات بنية أسبوعية قوية بارزة حالياً",
        "بيانات أسبوعية غير كافية لتقييم البنية",
        SCREENER_DISCLAIMER_AR,
    ]
    for text in candidates:
        assert find_banned_phrases(text, "ar") == []


class _VariedProvider(MarketDataAdapter):
    """Different OHLCV per symbol so ranking is meaningful."""

    def __init__(self, data: dict[str, pd.DataFrame]):
        self._data = data
        self.calls = 0

    def get_daily_ohlcv(self, symbol, lookback_days):
        self.calls += 1
        if symbol not in self._data:
            raise ValueError(f"no data for {symbol}")
        return self._data[symbol]

    def get_intraday(self, symbol, interval):
        return None

    def get_quote(self, symbol):
        return Quote(symbol=symbol, price=100.0, bid=None, ask=None, volume=None, as_of="2026-01-01T00:00:00Z", is_delayed=True)

    def estimated_cost_per_call(self):
        return 0.0

    def is_market_open(self):
        return False

    @property
    def provider_name(self):
        return "fake_varied"


def _flat_daily(n: int, base_price: float, base_volume: float) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    closes = np.full(n, base_price)
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": np.full(n, base_volume)}, index=idx
    )


def test_run_universe_scan_ranks_and_prefilters():
    data = {
        "HIGHVOL": _flat_daily(60, 100.0, 10_000_000.0),
        "LOWVOL": _flat_daily(60, 100.0, 100_000.0),
        "BADSYMBOL_TOO_SHORT": _flat_daily(5, 100.0, 1_000_000.0),  # under MIN_BARS_REQUIRED
    }
    provider = _VariedProvider(data)
    daily_result, weekly_result, fetched_count = run_universe_scan(provider, list(data.keys()))

    assert fetched_count == 2  # the too-short symbol is dropped
    assert daily_result.failed_count == 1
    daily_symbols = [c.symbol for c in daily_result.cards]
    assert "HIGHVOL" in daily_symbols and "LOWVOL" in daily_symbols
    weekly_symbols = [c.symbol for c in weekly_result.cards]
    assert "HIGHVOL" in weekly_symbols and "LOWVOL" in weekly_symbols


def test_screener_card_conclusion_is_descriptive_not_directive():
    """The conclusion paragraph must read as a factual synthesis, never label itself an
    "opportunity"/"recommendation", and never use entry/exit/stop-loss directive wording
    (CLAUDE.md rule 5) — the reader infers a conclusion, the system doesn't state one."""
    data = {"HIGHVOL": _flat_daily(60, 100.0, 10_000_000.0)}
    provider = _VariedProvider(data)
    daily_result, weekly_result, _ = run_universe_scan(provider, list(data.keys()))

    for result in (daily_result, weekly_result):
        assert len(result.cards) == 1
        conclusion = result.cards[0].conclusion
        assert conclusion  # non-empty
        assert "100.00$" in conclusion  # cites the actual price as a fact
        assert "فرصة" not in conclusion
        assert find_banned_phrases(conclusion, "ar") == []


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_settings():
    from app.config import Settings

    return Settings(
        database_url="sqlite://", default_daily_cap_usd=1.00, default_monthly_cap_usd=5.00,
        cost_anomaly_calls_per_minute=3,
    )


@pytest.fixture
def fake_universe():
    return [{"symbol": s, "name": s} for s in ["AAA", "BBB", "CCC", "DDD", "EEE"]]


def test_run_screener_scan_reserves_cost_gate_once_and_caches_both_kinds(
    monkeypatch, db_session, test_settings, fake_universe
):
    """A ~5-symbol scan must NOT trip the anomaly guard (cost_anomaly_calls_per_minute=3
    here) — it only would if the cost gate were (incorrectly) reserved once per symbol."""
    daily = _flat_daily(60, 100.0, 1_000_000.0)
    provider = _VariedProvider({s["symbol"]: daily for s in fake_universe})
    monkeypatch.setattr("app.core.orchestrator.US_SYMBOLS", fake_universe)
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: provider)

    result = run_screener_scan(db_session, "daily_readiness", settings=test_settings)
    assert result.from_cache is False
    assert len(result.cards) > 0

    ledger_count = db_session.query(CostLedger).count()
    assert ledger_count == 1  # ONE batch reservation, not one per symbol

    gate = CostGate(db_session, test_settings)
    assert gate.get_limits().kill_switch_on is False

    calls_after_first_scan = provider.calls
    assert calls_after_first_scan == len(fake_universe)

    # Second call, same kind -> cache hit, no new fetch, no new ledger row.
    cached_result = run_screener_scan(db_session, "daily_readiness", settings=test_settings)
    assert cached_result.from_cache is True
    assert cached_result.cached_minutes_ago is not None
    assert provider.calls == calls_after_first_scan
    assert db_session.query(CostLedger).count() == 1

    # The weekly view was cached by the SAME scan -> also a cache hit, still no new fetch.
    weekly_result = run_screener_scan(db_session, "weekly_structure", settings=test_settings)
    assert weekly_result.from_cache is True
    assert provider.calls == calls_after_first_scan
    assert db_session.query(CostLedger).count() == 1


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    fake_symbols = [{"symbol": s, "name": s} for s in ["AAA", "BBB", "CCC"]]
    daily = _flat_daily(60, 100.0, 1_000_000.0)
    provider = _VariedProvider({s["symbol"]: daily for s in fake_symbols})
    monkeypatch.setattr("app.core.orchestrator.US_SYMBOLS", fake_symbols)
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: provider)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_ui_screener_daily_renders(client):
    response = client.get("/ui/screener/daily")
    assert response.status_code == 200
    assert SCREENER_DISCLAIMER_AR in response.text
    assert find_banned_phrases(response.text, "ar") == []


def test_ui_screener_weekly_renders(client):
    response = client.get("/ui/screener/weekly")
    assert response.status_code == 200
    assert SCREENER_DISCLAIMER_AR in response.text
    assert find_banned_phrases(response.text, "ar") == []


def test_ui_screener_options_shows_not_configured(client):
    response = client.get("/ui/screener/options")
    assert response.status_code == 200
    assert SCREENER_DISCLAIMER_AR in response.text
    assert "Tradier" in response.text or "Polygon" in response.text
