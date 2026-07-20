from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.engines.deterministic.indicators import compute_indicators
from app.engines.deterministic.levels import detect_levels
from app.engines.deterministic.liquidity import compute_liquidity
from app.engines.deterministic.schemas import (
    BollingerBands,
    Indicators,
    LevelStrength,
    Levels,
    Liquidity,
    MACDResult,
    MovingAverages,
)
from app.engines.deterministic.scoring import (
    _invalidation_distance_component,
    _ma_alignment_component,
    _macd_component,
    _proximity_component,
    _rsi_component,
    _rvol_component,
    _structure_component,
    compute_liquidity_score,
    compute_overall_confidence,
    compute_risk_score,
    compute_scores,
    compute_technical_score,
    compute_volatility_score,
)


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="B")


def _flat_ohlc(closes, volumes=None):
    n = len(closes)
    volumes = volumes or [1_000_000] * n
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": volumes}, index=_dates(n)
    )


def _make_indicators(**overrides) -> Indicators:
    base = dict(
        rsi_14=50.0,
        macd=MACDResult(macd_line=0.0, signal_line=0.0, histogram=0.0, histogram_rising=False),
        vwap=None,
        atr_14=2.0,
        atr_pct=2.0,
        adx_14=20.0,
        moving_averages=MovingAverages(sma_20=None, sma_50=None, sma_200=None, ema_20=None, ema_50=None, ema_200=None),
        bollinger=BollingerBands(upper=101.0, middle=100.0, lower=99.0, bandwidth=0.02, bandwidth_percentile_90d=None),
    )
    base.update(overrides)
    return Indicators(**base)


def _make_levels(supports=None, resistances=None, invalidation=None) -> Levels:
    return Levels(supports=supports or [], resistances=resistances or [], invalidation=invalidation)


def _make_liquidity(**overrides) -> Liquidity:
    base = dict(
        avg_volume_20d=1_000_000.0, rvol=1.0, dollar_volume=25_000_000.0, spread_pct=0.1,
        unusual_volume=False, unusual_volume_direction=None,
    )
    base.update(overrides)
    return Liquidity(**base)


# --- Technical Score components ---

def test_ma_alignment_component_full_and_none_and_partial():
    ind_full = _make_indicators(moving_averages=MovingAverages(sma_20=None, sma_50=None, sma_200=None, ema_20=95, ema_50=90, ema_200=80))
    assert _ma_alignment_component(ind_full, last_close=100) == pytest.approx(1.0)

    ind_none = _make_indicators()  # all MAs None
    assert _ma_alignment_component(ind_none, last_close=100) == pytest.approx(0.0)

    # only Close > EMA20 holds; EMA20 < EMA50 breaks the chain
    ind_partial = _make_indicators(moving_averages=MovingAverages(sma_20=None, sma_50=None, sma_200=None, ema_20=95, ema_50=97, ema_200=80))
    assert _ma_alignment_component(ind_partial, last_close=100) == pytest.approx(2 / 3)


@pytest.mark.parametrize(
    "rsi_value,expected",
    [(55, 1.0), (45, 1.0), (65, 1.0), (40, 0.5), (30, 0.5), (70, 0.5), (75, 0.5), (20, 0.0), (80, 0.0)],
)
def test_rsi_component_bands(rsi_value, expected):
    assert _rsi_component(rsi_value) == pytest.approx(expected)


def test_macd_component_states():
    rising = _make_indicators(macd=MACDResult(macd_line=1, signal_line=0.5, histogram=0.5, histogram_rising=True))
    falling = _make_indicators(macd=MACDResult(macd_line=1, signal_line=0.5, histogram=0.5, histogram_rising=False))
    negative = _make_indicators(macd=MACDResult(macd_line=-1, signal_line=-0.5, histogram=-0.5, histogram_rising=True))
    assert _macd_component(rising) == pytest.approx(1.0)
    assert _macd_component(falling) == pytest.approx(0.5)
    assert _macd_component(negative) == pytest.approx(0.0)


def test_proximity_component_exact_ratio():
    levels = _make_levels(
        supports=[LevelStrength(price=90.0, touches=2, last_touch_bars_ago=1, avg_volume_at_touches=1.0, strength_score=0.5)],
        resistances=[LevelStrength(price=110.0, touches=2, last_touch_bars_ago=1, avg_volume_at_touches=1.0, strength_score=0.5)],
    )
    # dist_to_support=5, dist_to_resistance=15, total=20 -> 15/20 = 0.75
    assert _proximity_component(levels, last_close=95.0) == pytest.approx(0.75)


def test_proximity_component_neutral_without_levels():
    assert _proximity_component(_make_levels(), last_close=100.0) == pytest.approx(0.5)


@pytest.mark.parametrize("rvol,expected", [(1.5, 1.0), (0.8, 1.0), (2.5, 1.0), (0.5, 0.0), (3.0, 0.0)])
def test_rvol_component_bands(rvol, expected):
    assert _rvol_component(rvol) == pytest.approx(expected)


def test_structure_component_bullish_hh_hl():
    # A clean zigzag uptrend: peak1=130 (idx5) -> trough1=110 (idx8) -> peak2=145 (idx12,
    # higher high) -> trough2=120 (idx15, higher low). High==Low==Close (flat bars) keeps
    # the swing-point arithmetic unambiguous.
    prices = [100, 110, 120, 125, 128, 130, 125, 118, 110, 118, 128, 138, 145, 138, 128, 120, 125, 130]
    daily = _flat_ohlc(prices)
    assert _structure_component(daily, window=2) == pytest.approx(1.0)


def test_structure_component_neutral_with_insufficient_swings():
    daily = _flat_ohlc([100.0] * 20)
    assert _structure_component(daily, window=2) == pytest.approx(0.5)


def test_compute_technical_score_within_bounds():
    n = 250
    close = pd.Series(np.linspace(100, 180, n), index=_dates(n))
    high = close + 1
    low = close - 1
    volume = pd.Series([2_000_000] * n, index=_dates(n))
    daily = pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": volume})
    indicators = compute_indicators(daily)
    levels = detect_levels(daily, atr_value=indicators.atr_14, current_price=float(close.iloc[-1]))
    liquidity = compute_liquidity(daily)
    score = compute_technical_score(daily, indicators, levels, liquidity)
    assert 0 <= score <= 100


# --- Volatility Score ---

def test_volatility_score_100_when_current_is_90d_max():
    n = 120
    close = pd.Series([100.0] * n, index=_dates(n))
    spread = pd.Series(np.linspace(1, 5, n), index=_dates(n))
    high = close + spread / 2
    low = close - spread / 2
    daily = pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": [1_000_000] * n})
    assert compute_volatility_score(daily) == pytest.approx(100.0)


def test_volatility_score_low_when_current_is_90d_min():
    n = 120
    close = pd.Series([100.0] * n, index=_dates(n))
    spread = pd.Series(np.linspace(5, 1, n), index=_dates(n))
    high = close + spread / 2
    low = close - spread / 2
    daily = pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": [1_000_000] * n})
    assert compute_volatility_score(daily) < 20.0


# --- Liquidity Score ---

def test_liquidity_score_exact_formula_with_spread():
    liquidity = _make_liquidity(dollar_volume=25_000_000.0, avg_volume_20d=500_000.0, spread_pct=0.0)
    # 40*0.5 + 30*0.5 + 30*(1-0) = 20 + 15 + 30 = 65
    assert compute_liquidity_score(liquidity) == pytest.approx(65.0)


def test_liquidity_score_neutral_spread_when_missing():
    liquidity = _make_liquidity(dollar_volume=25_000_000.0, avg_volume_20d=500_000.0, spread_pct=None)
    # 40*0.5 + 30*0.5 + 30*0.5 = 20 + 15 + 15 = 50
    assert compute_liquidity_score(liquidity) == pytest.approx(50.0)


def test_liquidity_score_saturates_at_caps():
    liquidity = _make_liquidity(dollar_volume=100_000_000.0, avg_volume_20d=5_000_000.0, spread_pct=1.0)
    # 40*1 + 30*1 + 30*(1-1) = 70
    assert compute_liquidity_score(liquidity) == pytest.approx(70.0)


# --- Risk Score ---

def test_invalidation_distance_component_safe_zone_is_zero():
    levels = _make_levels(invalidation=90.0)
    # ratio = |100-90|/5 = 2.0, within [1.5, 3.0] -> 0
    assert _invalidation_distance_component(levels, last_close=100.0, atr_14=5.0) == pytest.approx(0.0)


def test_invalidation_distance_component_too_close_is_risky():
    levels = _make_levels(invalidation=99.0)
    # ratio = |100-99|/5 = 0.2 -> (1.5-0.2)/1.5 = 0.8667
    assert _invalidation_distance_component(levels, last_close=100.0, atr_14=5.0) == pytest.approx(0.8667, abs=1e-3)


def test_invalidation_distance_component_too_far_is_capped_at_one():
    levels = _make_levels(invalidation=50.0)
    # ratio = |100-50|/5 = 10 -> (10-3)/3 = 2.33 -> capped to 1.0
    assert _invalidation_distance_component(levels, last_close=100.0, atr_14=5.0) == pytest.approx(1.0)


def test_invalidation_distance_component_neutral_when_missing():
    levels = _make_levels(invalidation=None)
    assert _invalidation_distance_component(levels, last_close=100.0, atr_14=5.0) == pytest.approx(0.5)


def test_compute_risk_score_within_bounds():
    daily = _flat_ohlc([100.0] * 120)
    indicators = _make_indicators(atr_14=2.0)
    levels = _make_levels(invalidation=95.0)
    liquidity = _make_liquidity()
    score = compute_risk_score(daily, indicators, levels, liquidity, volatility_score=50.0)
    assert 0 <= score <= 100


# --- Overall Confidence ---

def test_overall_confidence_exact_formula():
    assert compute_overall_confidence(technical_score=80.0, risk_score=50.0, data_quality="intraday") == pytest.approx(60.0)
    assert compute_overall_confidence(technical_score=80.0, risk_score=50.0, data_quality="daily_only") == pytest.approx(51.0)
    assert compute_overall_confidence(technical_score=80.0, risk_score=50.0, data_quality="delayed_15m") == pytest.approx(57.0)


def test_compute_scores_end_to_end_smoke():
    n = 300
    rng = np.random.default_rng(11)
    close = pd.Series(100 + np.cumsum(rng.normal(0.03, 1.0, n)), index=_dates(n))
    high = close + rng.uniform(0.1, 1.0, n)
    low = close - rng.uniform(0.1, 1.0, n)
    volume = pd.Series(rng.integers(1_000_000, 5_000_000, n), index=_dates(n))
    daily = pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": volume})

    indicators = compute_indicators(daily)
    levels = detect_levels(daily, atr_value=indicators.atr_14, current_price=float(close.iloc[-1]))
    liquidity = compute_liquidity(daily)
    scores = compute_scores(daily, indicators, levels, liquidity, data_quality="daily_only")

    for value in (scores.technical, scores.volatility, scores.liquidity, scores.risk):
        assert 0 <= value <= 100
    assert scores.overall_confidence >= 0
    assert scores.formulas_ref == "/docs/scoring"
