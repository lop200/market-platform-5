from __future__ import annotations

import pandas as pd
import pytest

from app.engines.deterministic.levels import detect_levels, find_swing_points


def _flat_ohlc(closes: list[float], volume: int = 1_000_000) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="B")
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": [volume] * len(closes)},
        index=idx,
    )


def test_find_swing_points_marks_obvious_peak_and_trough():
    # index:      0  1  2  3  4  5  6  7  8
    closes = [100, 99, 98, 95, 98, 100, 103, 100, 99]
    daily = _flat_ohlc(closes)
    # window=2 needs 2 bars of room on both sides, so the trough (idx 3) and peak (idx 6)
    # both have enough neighbors within this 9-bar series.
    swings = find_swing_points(daily["high"], daily["low"], window=2)
    assert swings["is_swing_low"].iloc[3] == True  # noqa: E712 (95 is the trough)
    assert swings["is_swing_high"].iloc[6] == True  # noqa: E712 (103 is the peak)
    assert swings["is_swing_low"].iloc[0] == False  # noqa: E712 (edge of window, not enough data)


def test_detect_levels_finds_repeated_support_and_resistance():
    # Triangle wave: troughs at 95, peaks at 105, repeated across 4 cycles.
    pattern = [100, 98, 96, 95, 97, 99, 101, 103, 105, 104, 102, 100]
    closes = (pattern * 4)[:40]
    daily = _flat_ohlc(closes)

    result = detect_levels(daily, atr_value=2.0, current_price=100.0, window=2, max_levels=3)

    assert any(abs(level.price - 95) < 1 for level in result.supports)
    assert any(abs(level.price - 105) < 1 for level in result.resistances)
    strongest_support = result.supports[0]
    assert strongest_support.touches >= 2
    # supports must be strictly below current price, resistances strictly above
    assert all(level.price < 100.0 for level in result.supports)
    assert all(level.price > 100.0 for level in result.resistances)


def test_detect_levels_ranks_by_strength_descending():
    pattern = [100, 98, 96, 95, 97, 99, 101, 103, 105, 104, 102, 100]
    closes = (pattern * 4)[:40]
    daily = _flat_ohlc(closes)
    result = detect_levels(daily, atr_value=2.0, current_price=100.0, window=2, max_levels=3)
    scores = [level.strength_score for level in result.supports]
    assert scores == sorted(scores, reverse=True)


def test_detect_levels_respects_max_levels_cap():
    pattern = [100, 98, 96, 95, 97, 99, 101, 103, 105, 104, 102, 100]
    closes = (pattern * 4)[:40]
    daily = _flat_ohlc(closes)
    result = detect_levels(daily, atr_value=2.0, current_price=100.0, window=2, max_levels=1)
    assert len(result.supports) <= 1
    assert len(result.resistances) <= 1


def test_detect_levels_on_flat_series_returns_no_levels():
    closes = [100.0] * 30
    daily = _flat_ohlc(closes)
    result = detect_levels(daily, atr_value=1.0, current_price=100.0, window=2, max_levels=3)
    assert result.supports == []
    assert result.resistances == []
