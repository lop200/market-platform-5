from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd

NEW_YORK = "America/New_York"


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
    relative_volume = session_volume / avg_daily_volume if avg_daily_volume else 0
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
