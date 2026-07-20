"""Technical / Volatility / Liquidity / Risk / Overall-Confidence formulas (SRS 12).

Every score is an explicit, inspectable formula — the LLM never invents or recomputes any
of these numbers (CLAUDE.md rule 2). Where the SRS names a rule but doesn't pin an exact
constant (e.g. "قريب جداً أو بعيد جداً = أخطر" for the invalidation-distance risk
component), this module states its declared choice in a comment so it stays calibratable
per SRS 15.4 without being a hidden magic number.
"""
from __future__ import annotations

from typing import Literal

import pandas as pd

from app.engines.deterministic.indicators import atr as atr_fn
from app.engines.deterministic.levels import find_swing_points
from app.engines.deterministic.schemas import Indicators, Levels, Liquidity, Scores, Volatility

DataQuality = Literal["intraday", "delayed_15m", "daily_only"]

DATA_QUALITY_MULTIPLIER: dict[DataQuality, float] = {
    "intraday": 1.0,
    "delayed_15m": 0.95,
    "daily_only": 0.85,
}

# Risk component 2 (SRS 12.4): distance-to-invalidation / ATR is "too close" below this,
# "too far" above this — both ends are riskier than the middle of the range.
INVALIDATION_DISTANCE_SAFE_RANGE_ATR = (1.5, 3.0)


# ---------------------------------------------------------------------------
# Technical Score (SRS 12.1)
# ---------------------------------------------------------------------------

def _ma_alignment_component(indicators: Indicators, last_close: float) -> float:
    ma = indicators.moving_averages
    if ma.ema_20 is None or ma.ema_50 is None or ma.ema_200 is None:
        return 0.0
    checks = [last_close > ma.ema_20, ma.ema_20 > ma.ema_50, ma.ema_50 > ma.ema_200]
    return sum(checks) / len(checks)


def _rsi_component(rsi_14: float) -> float:
    if 45 <= rsi_14 <= 65:
        return 1.0
    if 30 <= rsi_14 < 45 or 65 < rsi_14 <= 75:
        return 0.5
    return 0.0


def _macd_component(indicators: Indicators) -> float:
    histogram = indicators.macd.histogram
    if histogram > 0:
        return 1.0 if indicators.macd.histogram_rising else 0.5
    return 0.0


def _proximity_component(levels: Levels, last_close: float) -> float:
    if not levels.supports or not levels.resistances:
        return 0.5  # unknown / not enough structure to judge -> neutral
    strongest_support = levels.supports[0].price
    strongest_resistance = levels.resistances[0].price
    dist_to_support = last_close - strongest_support
    dist_to_resistance = strongest_resistance - last_close
    total = dist_to_support + dist_to_resistance
    if total <= 0:
        return 0.5
    return dist_to_resistance / total


def _rvol_component(rvol: float) -> float:
    return 1.0 if 0.8 <= rvol <= 2.5 else 0.0


def _structure_component(daily: pd.DataFrame, window: int = 5) -> float:
    swings = find_swing_points(daily["high"], daily["low"], window=window)
    high_indices = [i for i in range(len(daily)) if swings["is_swing_high"].iloc[i]]
    low_indices = [i for i in range(len(daily)) if swings["is_swing_low"].iloc[i]]
    if len(high_indices) < 2 or len(low_indices) < 2:
        return 0.5

    higher_high = daily["high"].iloc[high_indices[-1]] > daily["high"].iloc[high_indices[-2]]
    higher_low = daily["low"].iloc[low_indices[-1]] > daily["low"].iloc[low_indices[-2]]
    lower_high = daily["high"].iloc[high_indices[-1]] < daily["high"].iloc[high_indices[-2]]
    lower_low = daily["low"].iloc[low_indices[-1]] < daily["low"].iloc[low_indices[-2]]

    if higher_high and higher_low:
        return 1.0
    if lower_low and lower_high:
        return 0.0
    return 0.5


def compute_technical_score(
    daily: pd.DataFrame, indicators: Indicators, levels: Levels, liquidity: Liquidity
) -> float:
    last_close = float(daily["close"].iloc[-1])
    score = (
        20 * _ma_alignment_component(indicators, last_close)
        + 15 * _rsi_component(indicators.rsi_14)
        + 15 * _macd_component(indicators)
        + 20 * _proximity_component(levels, last_close)
        + 15 * _rvol_component(liquidity.rvol)
        + 15 * _structure_component(daily)
    )
    return round(score, 2)


# ---------------------------------------------------------------------------
# Volatility Score (SRS 12.2)
# ---------------------------------------------------------------------------

def compute_volatility_score(daily: pd.DataFrame, lookback: int = 90) -> float:
    atr_series = atr_fn(daily["high"], daily["low"], daily["close"], period=14)
    atr_pct_series = (atr_series / daily["close"] * 100).dropna().tail(lookback)
    if len(atr_pct_series) < 2:
        return 50.0  # not enough history to rank -> neutral
    current = atr_pct_series.iloc[-1]
    percentile = float((atr_pct_series <= current).sum() / len(atr_pct_series) * 100)
    return round(percentile, 2)


# ---------------------------------------------------------------------------
# Liquidity Score (SRS 12.3)
# ---------------------------------------------------------------------------

def compute_liquidity_score(liquidity: Liquidity) -> float:
    dollar_volume_component = min(liquidity.dollar_volume / 50_000_000, 1.0)
    avg_volume_component = min(liquidity.avg_volume_20d / 1_000_000, 1.0)
    if liquidity.spread_pct is None:
        spread_component = 0.5  # not measured (no live quote) -> neutral, not assumed tight or wide
    else:
        spread_component = 1 - min(liquidity.spread_pct / 0.5, 1.0)
    score = 40 * dollar_volume_component + 30 * avg_volume_component + 30 * spread_component
    return round(score, 2)


# ---------------------------------------------------------------------------
# Risk Score (SRS 12.4)
# ---------------------------------------------------------------------------

def _invalidation_distance_component(levels: Levels, last_close: float, atr_14: float) -> float:
    if levels.invalidation is None or atr_14 <= 0:
        return 0.5  # no invalidation level to measure against -> neutral
    low, high = INVALIDATION_DISTANCE_SAFE_RANGE_ATR
    ratio = abs(last_close - levels.invalidation) / atr_14
    if low <= ratio <= high:
        return 0.0
    if ratio < low:
        return min((low - ratio) / low, 1.0)
    return min((ratio - high) / high, 1.0)


def compute_risk_score(
    daily: pd.DataFrame, indicators: Indicators, levels: Levels, liquidity: Liquidity, volatility_score: float
) -> float:
    last_close = float(daily["close"].iloc[-1])
    liquidity_score = compute_liquidity_score(liquidity)
    invalidation_component = _invalidation_distance_component(levels, last_close, indicators.atr_14)
    # Earnings/event proximity (SRS 12.4 component 4) requires an earnings calendar, which is
    # explicitly out of scope for the MVP (SRS 22-2). Fixed at 0 ("not checked") rather than
    # guessed — the report layer (M2) must state this is unexamined (SRS 13.3 rule 3).
    known_event_component = 0.0

    score = (
        35 * (volatility_score / 100)
        + 25 * invalidation_component
        + 20 * (1 - liquidity_score / 100)
        + 20 * known_event_component
    )
    return round(score, 2)


# ---------------------------------------------------------------------------
# Overall Confidence (SRS 12.5)
# ---------------------------------------------------------------------------

def compute_overall_confidence(technical_score: float, risk_score: float, data_quality: DataQuality) -> float:
    quality_multiplier = DATA_QUALITY_MULTIPLIER[data_quality]
    score = technical_score * (1 - risk_score / 200) * quality_multiplier
    return round(score, 2)


def compute_scores(
    daily: pd.DataFrame,
    indicators: Indicators,
    levels: Levels,
    liquidity: Liquidity,
    data_quality: DataQuality,
) -> Scores:
    technical = compute_technical_score(daily, indicators, levels, liquidity)
    volatility = compute_volatility_score(daily)
    liquidity_score = compute_liquidity_score(liquidity)
    risk = compute_risk_score(daily, indicators, levels, liquidity, volatility)
    overall_confidence = compute_overall_confidence(technical, risk, data_quality)

    return Scores(
        technical=technical,
        volatility=volatility,
        liquidity=liquidity_score,
        risk=risk,
        overall_confidence=overall_confidence,
    )
