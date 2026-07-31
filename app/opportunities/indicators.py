from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd

NEW_YORK = "America/New_York"

# Share of a normal day's volume each window carries. The regular session
# dominates; pre- and post-market are thin and the overnight book thinner
# still. These are what make relative volume comparable at any hour: without
# them a partial session is measured against a whole day and can never reach 1.
DAY_SESSION_SHARES = (
    (time(4), time(9, 30), 0.02),
    (time(9, 30), time(16), 0.93),
    (time(16), time(20), 0.04),
)
OVERNIGHT_SHARE = 0.01
# Floor for the expected share so the first candle of a session cannot divide
# by something near zero and report an absurd multiple.
MIN_EXPECTED_SHARE = 0.002

# Relative volume is read over a trailing window rather than from the session
# open. At 04:00 or 09:30 sharp the session holds no volume at all, so a
# session-anchored reading is zero for every symbol and rejects the whole
# market on its busiest minute. An hour that simply spans the boundary is
# always measurable.
VOLUME_WINDOW_MINUTES = 60


def _clock_rates() -> tuple[tuple[int, int, float], ...]:
    """(start_minute, end_minute, volume per minute) across the whole clock."""
    windows = []
    for start, end, share in DAY_SESSION_SHARES:
        span = _minutes(end) - _minutes(start)
        windows.append((_minutes(start), _minutes(end), share / span))
    # The overnight book wraps midnight, so it is split into two clock pieces.
    overnight_rate = OVERNIGHT_SHARE / (8 * 60)
    windows.append((_minutes(time(20)), 24 * 60, overnight_rate))
    windows.append((0, _minutes(time(4)), overnight_rate))
    return tuple(windows)


def expected_share_between(start: datetime, end: datetime) -> float:
    """Share of an average day's volume expected between two moments."""
    minutes = max(0, int((pd.Timestamp(end) - pd.Timestamp(start)).total_seconds() // 60))
    if not minutes:
        return MIN_EXPECTED_SHARE
    cursor = _minutes(_as_eastern_timestamp(start).time())
    rates = _clock_rates()
    total = 0.0
    for _ in range(minutes):
        for window_start, window_end, rate in rates:
            if window_start <= cursor < window_end:
                total += rate
                break
        cursor = (cursor + 1) % (24 * 60)
    return max(total, MIN_EXPECTED_SHARE)


def _eastern(intraday: pd.DataFrame) -> pd.DataFrame | None:
    """Return the frame indexed in New York time, or None if it is not dated."""
    if not isinstance(intraday.index, pd.DatetimeIndex):
        return None
    localized = intraday.copy()
    if localized.index.tz is None:
        localized.index = localized.index.tz_localize("UTC")
    return localized.tz_convert(NEW_YORK)


def _as_eastern_timestamp(now: datetime | None) -> pd.Timestamp:
    stamp = pd.Timestamp.utcnow() if now is None else pd.Timestamp(now)
    if stamp.tz is None:
        stamp = stamp.tz_localize("UTC")
    return stamp.tz_convert(NEW_YORK)


def session_start(now: datetime | None = None) -> pd.Timestamp:
    """Start of the trading session that ``now`` falls in, New York time.

    Alpaca's day runs 04:00 to 20:00 with the overnight session either side,
    which is the same split the market clock uses to pick a data feed.
    """
    eastern = _as_eastern_timestamp(now)
    if eastern.time() < time(4):
        return (eastern - timedelta(days=1)).normalize() + timedelta(hours=20)
    if eastern.time() >= time(20):
        return eastern.normalize() + timedelta(hours=20)
    return eastern.normalize() + timedelta(hours=4)


def _minutes(value: time) -> int:
    return value.hour * 60 + value.minute


def expected_volume_share(now: datetime | None = None) -> float:
    """Fraction of an average day's volume expected since this session opened.

    Volume is compared against what a normal stock would have traded by this
    point, so 1.0 means "trading at its usual pace" at 09:35 as much as at
    15:55. Progress within a window is treated as linear, which is coarse but
    far closer than measuring a part-session against a full day.
    """
    eastern = _as_eastern_timestamp(now)
    clock = _minutes(eastern.time())

    if clock < _minutes(time(4)) or clock >= _minutes(time(20)):
        # The overnight book runs 20:00 to 04:00: eight hours either side of midnight.
        elapsed = (clock - _minutes(time(20))) % (24 * 60)
        return max(OVERNIGHT_SHARE * elapsed / (8 * 60), MIN_EXPECTED_SHARE)

    expected = 0.0
    for start, end, share in DAY_SESSION_SHARES:
        if clock >= _minutes(end):
            expected += share
        elif clock > _minutes(start):
            span = _minutes(end) - _minutes(start)
            expected += share * (clock - _minutes(start)) / span
    return max(expected, MIN_EXPECTED_SHARE)


def current_session_bars(intraday: pd.DataFrame, now: datetime | None = None) -> pd.DataFrame:
    """Bars belonging to the session in progress, indexed in New York time.

    The provider returns several days of candles per request. Reading "session
    volume" or "opening range" off that whole frame describes a week-old
    session, so every session-scoped figure is measured from here instead.
    """
    eastern = _eastern(intraday)
    if eastern is None:
        return intraday
    current = eastern[eastern.index >= session_start(now)]
    # Before the session's first candle prints there is nothing to measure;
    # the newest bar keeps the indicators defined instead of dividing by zero.
    return current if not current.empty else eastern.tail(1)


def _trailing_volume(
    intraday: pd.DataFrame, now: datetime | None = None
) -> tuple[float, float]:
    """Volume over the trailing window, and the share a normal day would trade.

    Returns the pair so the caller divides like with like: both cover exactly
    the same stretch of clock, whichever sessions it happens to straddle.
    """
    end = _as_eastern_timestamp(now)
    span = expected_share_between(end - timedelta(minutes=VOLUME_WINDOW_MINUTES), end)
    eastern = _eastern(intraday)
    if eastern is None:
        # An undated frame cannot be sliced by clock, but its tail still covers
        # the window: the scanner feeds 5m candles, so twelve of them is an hour.
        tail = intraday.tail(VOLUME_WINDOW_MINUTES // 5)
        return float(tail["volume"].astype(float).sum()), span
    if eastern.empty:
        return 0.0, span
    # Anchor on the last candle when the frame lags the clock, so a quiet feed
    # is not read as a symbol that stopped trading.
    end = min(end, eastern.index.max()) if eastern.index.max() < end else end
    start = end - timedelta(minutes=VOLUME_WINDOW_MINUTES)
    window = eastern[(eastern.index > start) & (eastern.index <= end)]
    return (
        float(window["volume"].astype(float).sum()),
        expected_share_between(start, end),
    )


def calculate_indicators(
    daily: pd.DataFrame, intraday: pd.DataFrame, now: datetime | None = None
) -> dict[str, float | None]:
    daily = daily.copy()
    intraday = intraday.copy()
    close = intraday["close"].astype(float)
    daily_close = daily["close"].astype(float)
    typical = (intraday["high"] + intraday["low"] + intraday["close"]) / 3
    volume = intraday["volume"].astype(float)
    vwap = (typical * volume).cumsum() / volume.cumsum().replace(0, float("nan"))
    change = close.diff()
    gain = change.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-change.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss.replace(0, float("nan"))
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (intraday["high"] - intraday["low"]).abs(),
            (intraday["high"] - prev_close).abs(),
            (intraday["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    session = current_session_bars(intraday, now)
    session_volume = float(session["volume"].astype(float).sum())
    avg_daily_volume = float(daily["volume"].tail(20).mean()) if len(daily) else 0
    window_volume, window_span = _trailing_volume(intraday, now)
    expected_volume = avg_daily_volume * window_span
    relative_volume = window_volume / expected_volume if expected_volume else 0
    regular = session.between_time("09:30", "15:59") if isinstance(session.index, pd.DatetimeIndex) else session.iloc[0:0]
    result = {
        "vwap": float(vwap.iloc[-1]),
        "ema9": float(close.ewm(span=9, adjust=False).mean().iloc[-1]),
        "ema20": float(close.ewm(span=20, adjust=False).mean().iloc[-1]),
        "ema50": float(close.ewm(span=50, adjust=False).mean().iloc[-1]),
        "ema200": float(daily_close.ewm(span=200, adjust=False).mean().iloc[-1])
        if len(daily_close) >= 200 else None,
        "rsi": float((100 - 100 / (1 + rs)).iloc[-1]),
        "macd": float(macd.iloc[-1]),
        "macd_signal": float(macd.ewm(span=9, adjust=False).mean().iloc[-1]),
        "atr": float(tr.rolling(14).mean().iloc[-1]),
        "relative_volume": float(relative_volume),
        "average_volume": avg_daily_volume,
        "support": float(intraday["low"].tail(30).min()),
        "resistance": float(intraday["high"].tail(30).max()),
        "session_volume": session_volume,
        # Both cover the trailing window, so their ratio is the relative volume.
        "window_volume": window_volume,
        "expected_window_volume": expected_volume,
        "session_high": float(session["high"].max()),
        "session_low": float(session["low"].min()),
        # The opening range is the first three candles of the regular session;
        # it does not exist before 09:30 and must not be inferred from one.
        "opening_range_high": float(regular["high"].head(3).max()) if len(regular) >= 3 else None,
        "opening_range_low": float(regular["low"].head(3).min()) if len(regular) >= 3 else None,
        "previous_day_high": float(daily["high"].iloc[-2]) if len(daily) >= 2 else None,
        "previous_day_low": float(daily["low"].iloc[-2]) if len(daily) >= 2 else None,
        "gap_pct": float((session["open"].iloc[0] / daily_close.iloc[-2] - 1) * 100)
        if len(daily_close) >= 2
        else 0,
        "momentum": float(close.pct_change(5).iloc[-1] * 100) if len(close) > 5 else 0,
        "volatility": float(close.pct_change().tail(30).std() * 100),
    }
    if isinstance(session.index, pd.DatetimeIndex):
        premarket = session.between_time("04:00", "09:29")
        result["premarket_high"] = float(premarket["high"].max()) if not premarket.empty else None
        result["premarket_low"] = float(premarket["low"].min()) if not premarket.empty else None
    else:
        result["premarket_high"] = None
        result["premarket_low"] = None
    return {key: (round(value, 4) if value is not None and pd.notna(value) else None) for key, value in result.items()}
