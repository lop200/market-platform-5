from __future__ import annotations

import numpy as np
import pandas as pd

from app.engines.deterministic.indicators import atr as atr_fn
from app.engines.deterministic.smc import (
    compute_accumulation_distribution,
    detect_order_blocks,
    detect_rsi_divergence,
)


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="B")


def test_detect_rsi_divergence_bearish_higher_high_lower_rsi():
    highs = [100, 100, 100, 110, 120, 130, 120, 110, 100, 100, 100, 110, 125, 140, 125, 110, 100, 100, 100, 100]
    lows = [h - 5 for h in highs]
    closes = [h - 1 for h in highs]
    n = len(highs)
    daily = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1_000_000] * n},
        index=_dates(n),
    )
    rsi_series = pd.Series([50.0] * n, index=_dates(n))
    rsi_series.iloc[5] = 75.0  # RSI at the first (lower) price peak
    rsi_series.iloc[13] = 65.0  # RSI at the second (higher) price peak -> lower RSI = bearish divergence

    result = detect_rsi_divergence(daily, rsi_series, window=2)
    assert result.detected is True
    assert result.type == "bearish"


def test_detect_rsi_divergence_bullish_lower_low_higher_rsi():
    lows = [100, 100, 100, 90, 80, 70, 80, 90, 100, 100, 100, 90, 75, 60, 75, 90, 100, 100, 100, 100]
    highs = [low + 5 for low in lows]
    closes = [low + 1 for low in lows]
    n = len(lows)
    daily = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1_000_000] * n},
        index=_dates(n),
    )
    rsi_series = pd.Series([50.0] * n, index=_dates(n))
    rsi_series.iloc[5] = 25.0  # RSI at the first (higher) price trough
    rsi_series.iloc[13] = 35.0  # RSI at the second (lower) price trough -> higher RSI = bullish divergence

    result = detect_rsi_divergence(daily, rsi_series, window=2)
    assert result.detected is True
    assert result.type == "bullish"


def test_detect_rsi_divergence_none_for_flat_series():
    n = 20
    closes = [100.0] * n
    daily = pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [1_000_000] * n},
        index=_dates(n),
    )
    rsi_series = pd.Series([50.0] * n, index=_dates(n))
    result = detect_rsi_divergence(daily, rsi_series, window=2)
    assert result.detected is False
    assert result.type is None


def test_detect_order_blocks_finds_bullish_setup():
    n = 30
    closes = [100.0] * 20 + [98.0] + [102.0, 106.0, 111.0] + [111.0] * 6
    opens = closes.copy()
    opens[20] = 100.0  # bar 20 is a down candle: open 100 -> close 98
    highs = [c + 1 for c in closes]
    lows = [c - 1 for c in closes]
    volumes = [1_000_000] * 20 + [1_000_000] + [3_000_000, 3_000_000, 3_000_000] + [1_000_000] * 6
    daily = pd.DataFrame(
        {"open": opens, "high": highs, "low": lows, "close": closes, "volume": volumes}, index=_dates(n)
    )

    atr_series = atr_fn(daily["high"], daily["low"], daily["close"], period=14)
    order_blocks = detect_order_blocks(daily, atr_series, lookahead_bars=3, atr_multiple=2.0)

    assert len(order_blocks) >= 1
    ob = order_blocks[0]
    assert ob.direction == "bullish"
    assert ob.bar_index == 20
    assert ob.impulse_move_atr_multiple > 2.0


def test_detect_order_blocks_empty_when_no_impulsive_move():
    n = 30
    rng = np.random.default_rng(5)
    closes = 100 + rng.uniform(-0.5, 0.5, n)
    highs = closes + 0.6
    lows = closes - 0.6
    daily = pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": [1_000_000] * n},
        index=_dates(n),
    )
    atr_series = atr_fn(daily["high"], daily["low"], daily["close"], period=14)
    order_blocks = detect_order_blocks(daily, atr_series, lookahead_bars=3, atr_multiple=2.0)
    assert order_blocks == []


def test_accumulation_distribution_rising_when_closes_near_highs():
    n = 40
    base = np.linspace(100, 100, n)  # flat base price
    highs = base + 2.0
    lows = base - 2.0
    closes = highs - 0.1  # close consistently near the high -> MFM near +1
    volumes = [1_000_000] * n
    daily = pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes, "volume": volumes}, index=_dates(n))

    result = compute_accumulation_distribution(daily)
    assert result.slope_20d > 0
    assert result.interpretation == "accumulation"


def test_accumulation_distribution_falling_when_closes_near_lows():
    n = 40
    base = np.linspace(100, 100, n)
    highs = base + 2.0
    lows = base - 2.0
    closes = lows + 0.1  # close consistently near the low -> MFM near -1
    volumes = [1_000_000] * n
    daily = pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes, "volume": volumes}, index=_dates(n))

    result = compute_accumulation_distribution(daily)
    assert result.slope_20d < 0
    assert result.interpretation == "distribution"


def test_accumulation_distribution_neutral_when_closes_at_midpoint():
    n = 40
    base = np.linspace(100, 100, n)
    highs = base + 2.0
    lows = base - 2.0
    closes = (highs + lows) / 2  # close exactly at midpoint -> MFM == 0
    volumes = [1_000_000] * n
    daily = pd.DataFrame({"open": closes, "high": highs, "low": lows, "close": closes, "volume": volumes}, index=_dates(n))

    result = compute_accumulation_distribution(daily)
    assert result.interpretation == "neutral"
