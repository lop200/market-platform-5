"""Historical volatility (20/60d) and ATR% vs its 90-day average (SRS 10.3)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.engines.deterministic.indicators import atr
from app.engines.deterministic.schemas import Volatility

TRADING_DAYS_PER_YEAR = 252


def historical_volatility(close: pd.Series, window: int) -> pd.Series:
    """Annualized stdev of daily log returns, expressed as a percentage."""
    log_returns = np.log(close / close.shift(1))
    return log_returns.rolling(window=window).std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR) * 100


def compute_volatility(daily: pd.DataFrame) -> Volatility:
    close = daily["close"]
    hv_20 = historical_volatility(close, 20).iloc[-1]
    hv_60 = historical_volatility(close, 60).iloc[-1]

    atr_series = atr(daily["high"], daily["low"], close, period=14)
    atr_pct_series = atr_series / close * 100
    atr_pct_current = atr_pct_series.iloc[-1]
    atr_pct_avg_90d = atr_pct_series.rolling(window=90).mean().iloc[-1]
    atr_pct_relative = (
        atr_pct_current / atr_pct_avg_90d if atr_pct_avg_90d and not np.isnan(atr_pct_avg_90d) else float("nan")
    )

    return Volatility(
        hv_20d=round(float(hv_20), 4),
        hv_60d=round(float(hv_60), 4),
        atr_pct_current=round(float(atr_pct_current), 4),
        atr_pct_avg_90d=round(float(atr_pct_avg_90d), 4),
        atr_pct_relative=round(float(atr_pct_relative), 4),
    )
