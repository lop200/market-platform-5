from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

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
    Scores,
    SMC,
    Volatility,
)
from app.engines.llm.devils_advocate import (
    NO_ALERTS_FALLBACK_AR,
    format_alerts_for_prompt,
    generate_devils_advocate_alerts,
)


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="B")


def _flat_daily(closes: list[float], volumes: list[int] | None = None) -> pd.DataFrame:
    n = len(closes)
    volumes = volumes or [1_000_000] * n
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": volumes}, index=_dates(n)
    )


def _make_analysis(**overrides) -> DeterministicAnalysis:
    base = dict(
        symbol="TEST",
        as_of=datetime.now(timezone.utc),
        data_as_of=date(2026, 1, 5),
        data_quality="daily_only",
        last_close=100.0,
        indicators=Indicators(
            rsi_14=50.0,
            macd=MACDResult(macd_line=0.0, signal_line=0.0, histogram=0.0, histogram_rising=False),
            vwap=None,
            atr_14=2.0,
            atr_pct=2.0,
            adx_14=20.0,
            moving_averages=MovingAverages(sma_20=None, sma_50=None, sma_200=None, ema_20=100.0, ema_50=None, ema_200=None),
            bollinger=BollingerBands(upper=102.0, middle=100.0, lower=98.0, bandwidth=0.04, bandwidth_percentile_90d=50.0),
        ),
        levels=Levels(supports=[], resistances=[], invalidation=None),
        volatility=Volatility(hv_20d=20.0, hv_60d=20.0, atr_pct_current=2.0, atr_pct_avg_90d=2.0, atr_pct_relative=1.0),
        liquidity=Liquidity(avg_volume_20d=1_000_000.0, rvol=1.0, dollar_volume=10_000_000.0, spread_pct=0.1, unusual_volume=False, unusual_volume_direction=None),
        regime=Regime(label="ranging", reasons=["test"]),
        smc=SMC(
            rsi_divergence=RSIDivergence(detected=False, type=None, description=None),
            order_blocks=[],
            accumulation_distribution=AccumulationDistribution(current_value=0.0, slope_20d=0.0, interpretation="neutral"),
        ),
        scores=Scores(technical=50.0, volatility=50.0, liquidity=70.0, risk=30.0, overall_confidence=50.0),
    )
    base.update(overrides)
    return DeterministicAnalysis(**base)


def test_no_alerts_for_calm_neutral_analysis():
    analysis = _make_analysis()
    daily = _flat_daily([100.0] * 20)
    alerts = generate_devils_advocate_alerts(daily, analysis)
    assert alerts == []
    assert format_alerts_for_prompt(alerts) == NO_ALERTS_FALLBACK_AR


def test_resistance_proximity_alert():
    analysis = _make_analysis(
        levels=Levels(
            supports=[],
            resistances=[LevelStrength(price=101.0, touches=3, last_touch_bars_ago=2, avg_volume_at_touches=1e6, strength_score=0.8)],
            invalidation=None,
        )
    )  # distance=1.0, atr=2.0 -> within 1xATR
    daily = _flat_daily([100.0] * 20)
    alerts = generate_devils_advocate_alerts(daily, analysis)
    assert any("مقاومة قوية قريبة" in a for a in alerts)


def test_rsi_extreme_low_and_high():
    daily = _flat_daily([100.0] * 20)
    low = generate_devils_advocate_alerts(daily, _make_analysis(indicators=_make_analysis().indicators.model_copy(update={"rsi_14": 20.0})))
    high = generate_devils_advocate_alerts(daily, _make_analysis(indicators=_make_analysis().indicators.model_copy(update={"rsi_14": 80.0})))
    assert any("تشبع بيعي" in a for a in low)
    assert any("تشبع شرائي" in a for a in high)


def test_volume_divergence_alert():
    # price rising over the last 5 bars, volume dropping vs the prior 5 bars
    closes = [100, 100, 100, 100, 100, 101, 102, 103, 104, 105]
    volumes = [2_000_000] * 5 + [1_000_000] * 5
    daily = _flat_daily(closes, volumes)
    analysis = _make_analysis(last_close=105.0)
    alerts = generate_devils_advocate_alerts(daily, analysis)
    assert any("تباعد حجمي" in a for a in alerts)


def test_ema20_extension_alert():
    analysis = _make_analysis(last_close=106.0)  # |106-100|=6 > 2*2=4
    daily = _flat_daily([100.0] * 20)
    alerts = generate_devils_advocate_alerts(daily, analysis)
    assert any("ممدود عن EMA20" in a for a in alerts)


def test_high_risk_score_alert():
    analysis = _make_analysis(scores=_make_analysis().scores.model_copy(update={"risk": 70.0}))
    daily = _flat_daily([100.0] * 20)
    alerts = generate_devils_advocate_alerts(daily, analysis)
    assert any("المخاطرة مرتفعة" in a for a in alerts)


def test_weak_liquidity_alert():
    analysis = _make_analysis(scores=_make_analysis().scores.model_copy(update={"liquidity": 30.0}))
    daily = _flat_daily([100.0] * 20)
    alerts = generate_devils_advocate_alerts(daily, analysis)
    assert any("سيولة ضعيفة" in a for a in alerts)


def test_compressed_volatility_alert():
    analysis = _make_analysis(
        indicators=_make_analysis().indicators.model_copy(
            update={"bollinger": BollingerBands(upper=101, middle=100, lower=99, bandwidth=0.01, bandwidth_percentile_90d=5.0)}
        )
    )
    daily = _flat_daily([100.0] * 20)
    alerts = generate_devils_advocate_alerts(daily, analysis)
    assert any("منضغط" in a for a in alerts)


def test_format_alerts_joins_with_bullets():
    formatted = format_alerts_for_prompt(["a", "b"])
    assert formatted == "- a\n- b"
