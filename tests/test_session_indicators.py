from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.opportunities.indicators import (
    calculate_indicators,
    current_session_bars,
    expected_volume_share,
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
    # Relative volume reads the trailing hour, so both sides cover one window.
    assert indicators["relative_volume"] == round(
        indicators["window_volume"] / indicators["expected_window_volume"], 4
    )
    assert indicators["window_volume"] == 100 * 12  # twelve 5m candles in an hour


def test_expected_share_grows_through_the_day_and_completes_at_the_close():
    def share(hour, minute=0):
        return expected_volume_share(datetime(2026, 7, 30, hour, minute, tzinfo=timezone.utc))

    # UTC is four hours ahead of New York in July.
    premarket, open_bell, midday, close = share(12), share(13, 35), share(18), share(20)
    assert premarket < open_bell < midday < close
    # By the closing bell the regular session and pre-market are fully counted.
    assert close == pytest.approx(0.95)
    # A stock trading its normal pace reads 1.0 at any hour, not only at the close.
    assert share(13, 35) < 0.1


def test_expected_share_never_returns_zero():
    # 04:00 New York opens a window; dividing by its first instant must be safe.
    assert expected_volume_share(datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)) > 0
    assert expected_volume_share(datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc)) > 0


def test_overnight_pace_is_measured_against_the_overnight_book():
    # 02:00 New York is six of the overnight session's eight hours.
    share = expected_volume_share(OVERNIGHT)
    assert 0 < share < 0.01


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


def test_relative_volume_is_measurable_at_the_very_open():
    """The 04:00 and 09:30 bells are when a session holds no volume yet."""
    daily = pd.DataFrame({
        "open": [10.0] * 25, "high": [11.0] * 25, "low": [9.0] * 25,
        "close": [10.0] * 25, "volume": [100_000] * 25,
    })
    # 08:00 UTC is 04:00 New York exactly: pre-market's first second.
    bell = datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc)
    frame = _intraday_until(bell, periods=300, volume=500)
    indicators = calculate_indicators(daily, frame, now=bell)
    # A session-anchored reading would be zero here and reject every symbol.
    assert indicators["relative_volume"] > 0
    assert indicators["window_volume"] > 0


def test_expected_share_over_a_window_follows_the_clock():
    from app.opportunities.indicators import expected_share_between

    def window(hour, minutes=60):
        end = datetime(2026, 7, 30, hour, 0, tzinfo=timezone.utc)
        return expected_share_between(end - timedelta(minutes=minutes), end)

    # 14:00-15:00 UTC is 10:00-11:00 New York, inside the regular session.
    busy = window(15)
    # 08:00-09:00 UTC is 04:00-05:00 New York, thin pre-market.
    thin = window(9)
    assert busy > thin > 0
    # A whole day of windows must not exceed a whole day of volume.
    assert expected_share_between(
        datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 31, 8, 0, tzinfo=timezone.utc),
    ) <= 1.001
