from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.opportunities.indicators import (
    calculate_indicators,
    current_session_bars,
    session_start,
)

# 2026-07-30 is a Thursday.
REGULAR = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)  # 14:00 New York
OVERNIGHT = datetime(2026, 7, 31, 6, 0, tzinfo=timezone.utc)  # 02:00 New York


def _intraday_until(end: datetime, periods: int, volume: int = 100) -> pd.DataFrame:
    """Candles running up to ``end``, the way the provider returns them."""
    index = pd.date_range(end=pd.Timestamp(end), periods=periods, freq="5min", tz="UTC")
    return _frame(index, volume)


def _intraday(start: str, periods: int, volume: int = 100) -> pd.DataFrame:
    index = pd.date_range(start, periods=periods, freq="5min", tz="UTC")
    return _frame(index, volume)


def _frame(index: pd.DatetimeIndex, volume: int) -> pd.DataFrame:
    periods = len(index)
    return pd.DataFrame(
        {
            "open": [10.0] * periods,
            "high": [10.5] * periods,
            "low": [9.5] * periods,
            "close": [10.2] * periods,
            "volume": [volume] * periods,
        },
        index=index,
    )


def test_session_start_tracks_the_four_and_twenty_boundaries():
    # 14:00 New York sits in the day that opened at 04:00.
    assert session_start(REGULAR).hour == 4
    assert session_start(REGULAR).day == 30
    # 02:00 New York belongs to the overnight session that opened at 20:00.
    assert session_start(OVERNIGHT).hour == 20
    assert session_start(OVERNIGHT).day == 30


def test_session_bars_exclude_earlier_days():
    # Several days of candles, of which only the tail belongs to today.
    frame = _intraday_until(REGULAR, periods=1000)
    current = current_session_bars(frame, REGULAR)
    assert len(current) < len(frame)
    assert current.index.min() >= session_start(REGULAR)


def test_relative_volume_measures_this_session_not_the_whole_frame():
    daily = pd.DataFrame({
        "open": [10.0] * 25, "high": [11.0] * 25, "low": [9.0] * 25,
        "close": [10.0] * 25, "volume": [10_000] * 25,
    })
    frame = _intraday_until(REGULAR, periods=1000, volume=100)
    indicators = calculate_indicators(daily, frame, now=REGULAR)
    session = current_session_bars(frame, REGULAR)
    assert len(session) > 1
    # Summing the whole frame would report several days of volume as one session.
    assert indicators["session_volume"] == float(session["volume"].sum())
    assert indicators["session_volume"] < float(frame["volume"].sum())
    assert indicators["relative_volume"] == indicators["session_volume"] / 10_000


def test_opening_range_is_absent_before_the_regular_session_opens():
    daily = pd.DataFrame({
        "open": [10.0] * 25, "high": [11.0] * 25, "low": [9.0] * 25,
        "close": [10.0] * 25, "volume": [10_000] * 25,
    })
    # Overnight candles only: 09:30 has not happened in this session.
    overnight = _intraday_until(OVERNIGHT, periods=60)
    indicators = calculate_indicators(daily, overnight, now=OVERNIGHT)
    assert indicators["opening_range_high"] is None
    assert indicators["opening_range_low"] is None
