"""RSI, MACD, VWAP, ATR, moving averages, Bollinger Bands, ADX — pure pandas/numpy (SRS 10.1).

Computed by hand rather than via pandas-ta: this machine's Python (3.14) is newer than
pandas-ta's numba dependency supports, and the SRS's own tech table (5.1) explicitly lists
manual calculation as an accepted alternative. Wilder smoothing is implemented explicitly
to match TradingView's RSI/ATR/ADX methodology (SRS 10.1 note, NFR-7).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.engines.deterministic.schemas import BollingerBands, Indicators, MACDResult, MovingAverages


def wilder_smoothing(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing: seed with the simple average of the first `period` values
    AFTER any leading NaNs (e.g. from a .diff() with no prior bar), then recursively
    smoothed_t = (smoothed_{t-1} * (period - 1) + value_t) / period.
    """
    values = series.to_numpy(dtype=float)
    result = np.full(values.shape, np.nan)
    valid_indices = np.flatnonzero(~np.isnan(values))
    if len(valid_indices) < period:
        return pd.Series(result, index=series.index)
    start = valid_indices[0]
    seed_end = start + period  # exclusive
    if seed_end > len(values):
        return pd.Series(result, index=series.index)
    result[seed_end - 1] = np.mean(values[start:seed_end])
    for i in range(seed_end, len(values)):
        result[i] = (result[i - 1] * (period - 1) + values[i]) / period
    return pd.Series(result, index=series.index)


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = wilder_smoothing(gain, period)
    avg_loss = wilder_smoothing(loss, period)

    rsi_values = pd.Series(np.nan, index=close.index)
    both_zero = (avg_gain == 0) & (avg_loss == 0)
    loss_zero_only = (avg_loss == 0) & ~both_zero
    normal = ~both_zero & ~loss_zero_only

    rsi_values[both_zero] = 50.0
    rsi_values[loss_zero_only] = 100.0
    rs = avg_gain[normal] / avg_loss[normal]
    rsi_values[normal] = 100 - (100 / (1 + rs))
    return rsi_values


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    ranges = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    )
    return ranges.max(axis=1)


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return wilder_smoothing(true_range(high, low, close), period)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return pd.DataFrame(
        {"macd_line": macd_line, "signal_line": signal_line, "histogram": histogram}
    )


def bollinger_bands(close: pd.Series, period: int = 20, num_std: float = 2.0) -> pd.DataFrame:
    middle = sma(close, period)
    std = close.rolling(window=period).std(ddof=0)  # population stdev, matches TradingView default
    upper = middle + num_std * std
    lower = middle - num_std * std
    bandwidth = (upper - lower) / middle
    return pd.DataFrame({"upper": upper, "middle": middle, "lower": lower, "bandwidth": bandwidth})


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    up_move = high.diff().fillna(0.0)
    down_move = (-low.diff()).fillna(0.0)
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=high.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=high.index
    )
    tr = true_range(high, low, close)

    smoothed_plus_dm = wilder_smoothing(plus_dm, period)
    smoothed_minus_dm = wilder_smoothing(minus_dm, period)
    smoothed_tr = wilder_smoothing(tr, period)

    plus_di = 100 * smoothed_plus_dm / smoothed_tr
    minus_di = 100 * smoothed_minus_dm / smoothed_tr
    di_sum = plus_di + minus_di
    dx = pd.Series(
        np.where(di_sum == 0, 0.0, 100 * (plus_di - minus_di).abs() / di_sum), index=high.index
    )
    return wilder_smoothing(dx, period)


def vwap_session(intraday: pd.DataFrame) -> pd.Series:
    """Cumulative typical-price VWAP, reset at each session boundary (SRS 10.1)."""
    typical_price = (intraday["high"] + intraday["low"] + intraday["close"]) / 3
    pv = typical_price * intraday["volume"]
    session_key = pd.Series(intraday.index).dt.normalize().to_numpy()
    cum_pv = pv.groupby(session_key).cumsum()
    cum_vol = intraday["volume"].groupby(session_key).cumsum()
    return cum_pv / cum_vol


def _bandwidth_percentile(bandwidth: pd.Series, lookback: int = 90) -> float | None:
    window = bandwidth.dropna().tail(lookback)
    if len(window) < 2:
        return None
    current = window.iloc[-1]
    return float((window <= current).sum() / len(window) * 100)


def compute_indicators(daily: pd.DataFrame, intraday: pd.DataFrame | None = None) -> Indicators:
    close, high, low = daily["close"], daily["high"], daily["low"]

    rsi_series = rsi(close, 14)
    macd_df = macd(close)
    atr_series = atr(high, low, close, 14)
    adx_series = adx(high, low, close, 14)
    bb_df = bollinger_bands(close, 20, 2.0)

    latest_close = float(close.iloc[-1])
    latest_hist = macd_df["histogram"].iloc[-1]
    prev_hist = macd_df["histogram"].iloc[-2] if len(macd_df) > 1 else np.nan

    vwap_value: float | None = None
    if intraday is not None and not intraday.empty:
        vwap_series = vwap_session(intraday)
        if not vwap_series.empty and not np.isnan(vwap_series.iloc[-1]):
            vwap_value = float(vwap_series.iloc[-1])

    def _last_or_none(series: pd.Series) -> float | None:
        value = series.iloc[-1]
        return None if pd.isna(value) else float(value)

    return Indicators(
        rsi_14=float(rsi_series.iloc[-1]),
        macd=MACDResult(
            macd_line=float(macd_df["macd_line"].iloc[-1]),
            signal_line=float(macd_df["signal_line"].iloc[-1]),
            histogram=float(latest_hist),
            histogram_rising=bool(not np.isnan(prev_hist) and latest_hist > prev_hist),
        ),
        vwap=vwap_value,
        atr_14=float(atr_series.iloc[-1]),
        atr_pct=float(atr_series.iloc[-1] / latest_close * 100),
        adx_14=float(adx_series.iloc[-1]),
        moving_averages=MovingAverages(
            sma_20=_last_or_none(sma(close, 20)),
            sma_50=_last_or_none(sma(close, 50)),
            sma_200=_last_or_none(sma(close, 200)),
            ema_20=_last_or_none(ema(close, 20)),
            ema_50=_last_or_none(ema(close, 50)),
            ema_200=_last_or_none(ema(close, 200)),
        ),
        bollinger=BollingerBands(
            upper=float(bb_df["upper"].iloc[-1]),
            middle=float(bb_df["middle"].iloc[-1]),
            lower=float(bb_df["lower"].iloc[-1]),
            bandwidth=float(bb_df["bandwidth"].iloc[-1]),
            bandwidth_percentile_90d=_bandwidth_percentile(bb_df["bandwidth"]),
        ),
    )
