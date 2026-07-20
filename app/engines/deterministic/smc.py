"""SMC module (Annex C-3, built within M1): RSI Divergence, Order Blocks, Accumulation/Distribution.

The SRS names these three algorithms but (unlike section 12's scoring formulas) doesn't
pin exact thresholds — each function below states its declared rule up front so it stays
inspectable and calibratable, per the SRS's general "معادلة/قاعدة معلنة" principle.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.engines.deterministic.indicators import atr, rsi
from app.engines.deterministic.levels import find_swing_points
from app.engines.deterministic.schemas import AccumulationDistribution, OrderBlock, RSIDivergence, SMC

ORDER_BLOCK_IMPULSE_ATR_MULTIPLE = 2.0
ORDER_BLOCK_LOOKAHEAD_BARS = 3
ORDER_BLOCK_MAX_RESULTS = 5
AD_SLOPE_LOOKBACK_BARS = 20
AD_NORMALIZED_SLOPE_THRESHOLD = 0.05


def detect_rsi_divergence(daily: pd.DataFrame, rsi_series: pd.Series, window: int = 5) -> RSIDivergence:
    """Bearish: price makes a higher high while RSI makes a lower high (momentum weakening
    into new highs). Bullish: price makes a lower low while RSI makes a higher low. Checked
    against the two most recent swing highs/lows respectively (SRS Annex C-3)."""
    swings = find_swing_points(daily["high"], daily["low"], window=window)
    high_indices = [i for i in range(len(daily)) if swings["is_swing_high"].iloc[i]]
    low_indices = [i for i in range(len(daily)) if swings["is_swing_low"].iloc[i]]

    if len(high_indices) >= 2:
        i1, i2 = high_indices[-2], high_indices[-1]
        price_higher_high = daily["high"].iloc[i2] > daily["high"].iloc[i1]
        rsi_lower_high = rsi_series.iloc[i2] < rsi_series.iloc[i1]
        if price_higher_high and rsi_lower_high:
            return RSIDivergence(
                detected=True,
                type="bearish",
                description=(
                    f"السعر سجّل قمة أعلى ({daily['high'].iloc[i1]:.2f} -> {daily['high'].iloc[i2]:.2f}) "
                    f"بينما RSI سجّل قمة أدنى ({rsi_series.iloc[i1]:.1f} -> {rsi_series.iloc[i2]:.1f})"
                ),
            )

    if len(low_indices) >= 2:
        j1, j2 = low_indices[-2], low_indices[-1]
        price_lower_low = daily["low"].iloc[j2] < daily["low"].iloc[j1]
        rsi_higher_low = rsi_series.iloc[j2] > rsi_series.iloc[j1]
        if price_lower_low and rsi_higher_low:
            return RSIDivergence(
                detected=True,
                type="bullish",
                description=(
                    f"السعر سجّل قاعاً أدنى ({daily['low'].iloc[j1]:.2f} -> {daily['low'].iloc[j2]:.2f}) "
                    f"بينما RSI سجّل قاعاً أعلى ({rsi_series.iloc[j1]:.1f} -> {rsi_series.iloc[j2]:.1f})"
                ),
            )

    return RSIDivergence(detected=False, type=None, description=None)


def detect_order_blocks(
    daily: pd.DataFrame,
    atr_series: pd.Series,
    lookahead_bars: int = ORDER_BLOCK_LOOKAHEAD_BARS,
    atr_multiple: float = ORDER_BLOCK_IMPULSE_ATR_MULTIPLE,
    max_results: int = ORDER_BLOCK_MAX_RESULTS,
) -> list[OrderBlock]:
    """An order block is the last opposite-colored candle before an impulsive move
    (> `atr_multiple` x ATR over `lookahead_bars`) whose bars carry above-average volume."""
    avg_volume = daily["volume"].rolling(window=20).mean()
    is_down_candle = daily["close"] < daily["open"]
    is_up_candle = daily["close"] > daily["open"]

    results: list[OrderBlock] = []
    seen_bar_indices: set[int] = set()

    for i in range(len(daily) - lookahead_bars - 1, -1, -1):
        atr_i = atr_series.iloc[i]
        if pd.isna(atr_i) or atr_i == 0 or pd.isna(avg_volume.iloc[i]):
            continue

        move = float(daily["close"].iloc[i + lookahead_bars] - daily["close"].iloc[i])
        if abs(move) < atr_multiple * atr_i:
            continue

        direction = "bullish" if move > 0 else "bearish"
        impulse_avg_volume = daily["volume"].iloc[i + 1 : i + 1 + lookahead_bars].mean()
        if impulse_avg_volume <= avg_volume.iloc[i]:
            continue

        ob_index = None
        for j in range(i, -1, -1):
            if direction == "bullish" and is_down_candle.iloc[j]:
                ob_index = j
                break
            if direction == "bearish" and is_up_candle.iloc[j]:
                ob_index = j
                break
        if ob_index is None or ob_index in seen_bar_indices:
            continue
        seen_bar_indices.add(ob_index)

        results.append(
            OrderBlock(
                bar_index=ob_index,
                date=str(daily.index[ob_index].date()),
                direction=direction,
                high=float(daily["high"].iloc[ob_index]),
                low=float(daily["low"].iloc[ob_index]),
                impulse_move_atr_multiple=round(abs(move) / atr_i, 4),
            )
        )
        if len(results) >= max_results:
            break

    return results


def compute_accumulation_distribution(
    daily: pd.DataFrame, lookback: int = AD_SLOPE_LOOKBACK_BARS
) -> AccumulationDistribution:
    high, low, close, volume = daily["high"], daily["low"], daily["close"], daily["volume"]
    price_range = (high - low).replace(0, np.nan)
    money_flow_multiplier = ((close - low) - (high - close)) / price_range
    money_flow_multiplier = money_flow_multiplier.fillna(0.0)
    money_flow_volume = money_flow_multiplier * volume
    ad_line = money_flow_volume.cumsum()

    window = ad_line.tail(lookback)
    if len(window) >= 2:
        x = np.arange(len(window))
        slope = float(np.polyfit(x, window.to_numpy(), 1)[0])
    else:
        slope = 0.0

    avg_volume = float(volume.tail(lookback).mean()) if len(volume) else 0.0
    normalized_slope = slope / avg_volume if avg_volume else 0.0
    if normalized_slope > AD_NORMALIZED_SLOPE_THRESHOLD:
        interpretation = "accumulation"
    elif normalized_slope < -AD_NORMALIZED_SLOPE_THRESHOLD:
        interpretation = "distribution"
    else:
        interpretation = "neutral"

    return AccumulationDistribution(
        current_value=round(float(ad_line.iloc[-1]), 2),
        slope_20d=round(slope, 4),
        interpretation=interpretation,
    )


def compute_smc(daily: pd.DataFrame) -> SMC:
    rsi_series = rsi(daily["close"], 14)
    atr_series = atr(daily["high"], daily["low"], daily["close"], 14)

    return SMC(
        rsi_divergence=detect_rsi_divergence(daily, rsi_series),
        order_blocks=detect_order_blocks(daily, atr_series),
        accumulation_distribution=compute_accumulation_distribution(daily),
    )
