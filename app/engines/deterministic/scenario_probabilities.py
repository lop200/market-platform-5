"""Inspectable scenario-evidence allocation for bullish/bearish/neutral readings.

These percentages are not historical win rates. They allocate 100 evidence points from
the deterministic strategy stack:

    regime 25 + moving-average structure 20 + MACD 15 + RSI 10
    + SMC 10 + weekly direction 20 = 100

Conflicting or weak readings are assigned to neutral instead of being forced into the
opposite direction. All arithmetic is pure Python; the LLM never creates these values.
"""
from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from app.engines.deterministic.schemas import DeterministicAnalysis, ScenarioProbabilities
from app.engines.screener.weekly_structure import MIN_WEEKLY_BARS, resample_weekly

REGIME_WEIGHT = 25.0
MA_STRUCTURE_WEIGHT = 20.0
MACD_WEIGHT = 15.0
RSI_WEIGHT = 10.0
SMC_WEIGHT = 10.0
WEEKLY_WEIGHT = 20.0
FORMULA_REF = "/docs/scoring#scenario-probabilities"


def _net_allocation(signals: Iterable[int]) -> tuple[float, float, float]:
    """Convert {-1, 0, +1} votes into conservative directional/neutral shares."""
    values = list(signals)
    if not values:
        return 0.0, 0.0, 1.0
    bullish = sum(value > 0 for value in values)
    bearish = sum(value < 0 for value in values)
    directional_edge = abs(bullish - bearish) / len(values)
    if bullish > bearish:
        return directional_edge, 0.0, 1.0 - directional_edge
    if bearish > bullish:
        return 0.0, directional_edge, 1.0 - directional_edge
    return 0.0, 0.0, 1.0


def _regime_allocation(label: str) -> tuple[float, float, float]:
    if label == "trending_up":
        return 1.0, 0.0, 0.0
    if label == "trending_down":
        return 0.0, 1.0, 0.0
    if label == "high_vol":
        return 0.2, 0.2, 0.6
    return 0.0, 0.0, 1.0


def _ma_allocation(analysis: DeterministicAnalysis) -> tuple[float, float, float]:
    price = analysis.last_close
    moving_averages = analysis.indicators.moving_averages
    signals = [
        0 if value is None else (1 if price > value else (-1 if price < value else 0))
        for value in (moving_averages.ema_20, moving_averages.ema_50, moving_averages.ema_200)
    ]
    return _net_allocation(signals)


def _macd_allocation(analysis: DeterministicAnalysis) -> tuple[float, float, float]:
    histogram = analysis.indicators.macd.histogram
    rising = analysis.indicators.macd.histogram_rising
    if histogram > 0:
        strength = 1.0 if rising else 0.6
        return strength, 0.0, 1.0 - strength
    if histogram < 0:
        # "rising" means a negative histogram is moving toward zero, so bearish
        # conviction is reduced rather than described as strengthening.
        strength = 0.6 if rising else 1.0
        return 0.0, strength, 1.0 - strength
    return 0.0, 0.0, 1.0


def _rsi_allocation(analysis: DeterministicAnalysis) -> tuple[float, float, float]:
    rsi = analysis.indicators.rsi_14
    if rsi >= 55:
        strength = min((rsi - 50) / 15, 1.0)
        return strength, 0.0, 1.0 - strength
    if rsi <= 45:
        strength = min((50 - rsi) / 15, 1.0)
        return 0.0, strength, 1.0 - strength
    return 0.0, 0.0, 1.0


def _smc_allocation(analysis: DeterministicAnalysis) -> tuple[float, float, float]:
    smc = analysis.smc
    signals: list[int] = []
    divergence = smc.rsi_divergence.type if smc.rsi_divergence.detected else None
    signals.append(1 if divergence == "bullish" else (-1 if divergence == "bearish" else 0))
    interpretation = smc.accumulation_distribution.interpretation
    signals.append(1 if interpretation == "accumulation" else (-1 if interpretation == "distribution" else 0))
    latest_block = max(smc.order_blocks, key=lambda block: block.bar_index) if smc.order_blocks else None
    signals.append(1 if latest_block and latest_block.direction == "bullish" else (-1 if latest_block else 0))
    return _net_allocation(signals)


def _weekly_allocation(daily: pd.DataFrame | None) -> tuple[float, float, float]:
    if daily is None or daily.empty:
        return 0.0, 0.0, 1.0
    weekly = resample_weekly(daily)
    if len(weekly) < MIN_WEEKLY_BARS:
        return 0.0, 0.0, 1.0
    close = weekly["close"]
    last_close = float(close.iloc[-1])
    ema20 = float(close.ewm(span=20, adjust=False).mean().iloc[-1])
    distance_pct = (last_close / ema20 - 1) * 100 if ema20 else 0.0
    if distance_pct > 0.25:
        return 1.0, 0.0, 0.0
    if distance_pct < -0.25:
        return 0.0, 1.0, 0.0
    return 0.0, 0.0, 1.0


def _rounded_percentages(values: list[float]) -> list[float]:
    rounded = [round(value, 1) for value in values]
    residual = round(100.0 - sum(rounded), 1)
    rounded[max(range(len(values)), key=values.__getitem__)] += residual
    return [round(value, 1) for value in rounded]


def compute_scenario_probabilities(
    analysis: DeterministicAnalysis, daily: pd.DataFrame | None = None
) -> ScenarioProbabilities:
    components = [
        (REGIME_WEIGHT, _regime_allocation(analysis.regime.label)),
        (MA_STRUCTURE_WEIGHT, _ma_allocation(analysis)),
        (MACD_WEIGHT, _macd_allocation(analysis)),
        (RSI_WEIGHT, _rsi_allocation(analysis)),
        (SMC_WEIGHT, _smc_allocation(analysis)),
        (WEEKLY_WEIGHT, _weekly_allocation(daily)),
    ]
    totals = [
        sum(weight * allocation[index] for weight, allocation in components)
        for index in range(3)
    ]
    bullish, bearish, neutral = _rounded_percentages(totals)
    return ScenarioProbabilities(
        bullish_pct=bullish,
        bearish_pct=bearish,
        neutral_pct=neutral,
        calibrated=False,
        formula_ref=FORMULA_REF,
    )
