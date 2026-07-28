from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.engines.deterministic.schemas import (
    SMC,
    AccumulationDistribution,
    BollingerBands,
    DeterministicAnalysis,
    Indicators,
    LevelStrength,
    Levels,
    Liquidity,
    MACDResult,
    MovingAverages,
    RSIDivergence,
    Regime,
    Scores,
    Volatility,
)
from app.engines.llm.report_engine import find_banned_phrases
from app.engines.screener.snipe_scoring import compute_snipe_score


def _analysis(
    *,
    rvol: float = 1.0,
    rsi: float = 50.0,
    histogram_rising: bool = False,
    last_close: float = 100.0,
    atr_14: float = 2.0,
    supports: list[float] | None = None,
    resistances: list[float] | None = None,
    regime_label: str = "ranging",
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
        regime=Regime(label=regime_label, reasons=[]),
        smc=SMC(
            rsi_divergence=RSIDivergence(detected=False, type=None, description=None),
            order_blocks=[],
            accumulation_distribution=AccumulationDistribution(current_value=0.0, slope_20d=0.0, interpretation="neutral"),
        ),
        scores=Scores(technical=50.0, volatility=50.0, liquidity=50.0, risk=50.0, overall_confidence=50.0),
    )


def _trend_daily(n: int, start: float, end: float) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    closes = np.linspace(start, end, n)
    return pd.DataFrame(
        {"open": closes, "high": closes + 1, "low": closes - 1, "close": closes, "volume": np.full(n, 1_000_000.0)},
        index=idx,
    )


def test_snipe_score_high_hand_computed():
    analysis = _analysis(
        rvol=3.0, rsi=80.0, histogram_rising=True, last_close=100.0, atr_14=2.0,
        supports=[95.0], resistances=[110.0], regime_label="trending_up",
    )
    daily = _trend_daily(250, 50, 150)  # strong uptrend -> weekly direction "up"
    score, reasons = compute_snipe_score(analysis, daily)
    # 25*1 + 20*1 + 20*(1 - 2.5/3) + 20*1 + 15*0.5 = 75.8.
    assert score == pytest.approx(75.8, abs=0.05)
    assert any("استراتيجية استمرار الاتجاه متعدد الفريمات" in r for r in reasons)
    assert any("حجم غير طبيعي" in r for r in reasons)
    assert any("زخم MACD" in r for r in reasons)
    assert any("توافق" in r for r in reasons)


def test_snipe_score_low_hand_computed():
    analysis = _analysis(rvol=0.5, rsi=50.0, histogram_rising=False, last_close=100.0, atr_14=2.0, regime_label="ranging")
    daily = _trend_daily(20, 100, 101)  # too short for weekly history
    score, reasons = compute_snipe_score(analysis, daily)
    # Neutral momentum is not rewarded; unknown weekly/SMC evidence stays neutral.
    assert score == pytest.approx(20.8, abs=0.1)
    assert any("اتجاه غير مكتمل" in reason for reason in reasons)


def test_snipe_score_confluence_agreement_gives_full_component():
    analysis = _analysis(regime_label="trending_up")
    daily = _trend_daily(250, 50, 150)
    _, reasons = compute_snipe_score(analysis, daily)
    assert any("توافق بين الاتجاه اليومي والأسبوعي" in r for r in reasons)


def test_snipe_score_confluence_conflict_gives_zero_component():
    analysis = _analysis(regime_label="trending_down")
    daily = _trend_daily(250, 50, 150)  # weekly is up, daily regime is down -> conflict
    _, reasons = compute_snipe_score(analysis, daily)
    assert any("تعارض بين الاتجاه اليومي والأسبوعي" in r for r in reasons)


def test_snipe_score_insufficient_weekly_history_is_silent_on_confluence():
    analysis = _analysis(regime_label="trending_up")
    daily = _trend_daily(20, 100, 101)
    _, reasons = compute_snipe_score(analysis, daily)
    assert not any("توافق" in r or "تعارض" in r for r in reasons)


def test_all_snipe_score_reason_strings_pass_banned_phrase_filter():
    candidates = [
        "حجم غير طبيعي: 3.5× المعتاد",
        "حجم أعلى من المعتاد: 1.8×",
        "زخم MACD يتقوى",
        "قريب جداً من مستوى فني مفصلي",
        "توافق بين الاتجاه اليومي والأسبوعي",
        "تعارض بين الاتجاه اليومي والأسبوعي",
        "لا توجد إشارات جاهزية بارزة اليوم",
    ]
    for text in candidates:
        assert find_banned_phrases(text, "ar") == []
