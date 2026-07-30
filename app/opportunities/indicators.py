from __future__ import annotations

import pandas as pd


def calculate_indicators(daily: pd.DataFrame, intraday: pd.DataFrame) -> dict[str, float | None]:
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
    avg_daily_volume = float(daily["volume"].tail(20).mean()) if len(daily) else 0
    session_volume = float(volume.sum())
    relative_volume = session_volume / avg_daily_volume if avg_daily_volume else 0
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
        "session_high": float(intraday["high"].max()),
        "session_low": float(intraday["low"].min()),
        "opening_range_high": float(intraday["high"].head(15).max()),
        "opening_range_low": float(intraday["low"].head(15).min()),
        "previous_day_high": float(daily["high"].iloc[-2]) if len(daily) >= 2 else None,
        "previous_day_low": float(daily["low"].iloc[-2]) if len(daily) >= 2 else None,
        "gap_pct": float((intraday["open"].iloc[0] / daily_close.iloc[-2] - 1) * 100)
        if len(daily_close) >= 2
        else 0,
        "momentum": float(close.pct_change(5).iloc[-1] * 100) if len(close) > 5 else 0,
        "volatility": float(close.pct_change().tail(30).std() * 100),
    }
    if isinstance(intraday.index, pd.DatetimeIndex):
        localized = intraday.copy()
        if localized.index.tz is None:
            localized.index = localized.index.tz_localize("UTC")
        eastern = localized.tz_convert("America/New_York")
        premarket = eastern.between_time("04:00", "09:29")
        result["premarket_high"] = float(premarket["high"].max()) if not premarket.empty else None
        result["premarket_low"] = float(premarket["low"].min()) if not premarket.empty else None
    else:
        result["premarket_high"] = None
        result["premarket_low"] = None
    return {key: (round(value, 4) if value is not None and pd.notna(value) else None) for key, value in result.items()}
