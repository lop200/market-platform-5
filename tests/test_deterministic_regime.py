from __future__ import annotations

import numpy as np
import pandas as pd

from app.engines.deterministic.indicators import compute_indicators
from app.engines.deterministic.regime import classify_regime
from app.engines.deterministic.schemas import Indicators, MACDResult, MovingAverages, BollingerBands, Volatility
from app.engines.deterministic.volatility import compute_volatility


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="B")


def _trend_df(n: int, start: float, end: float) -> pd.DataFrame:
    close = pd.Series(np.linspace(start, end, n), index=_dates(n))
    high = close + 1
    low = close - 1
    volume = pd.Series([2_000_000] * n, index=_dates(n))
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": volume})


def test_classify_regime_trending_up():
    daily = _trend_df(300, 100, 220)
    indicators = compute_indicators(daily)
    volatility = compute_volatility(daily)
    regime = classify_regime(daily, indicators, volatility)
    assert regime.label == "trending_up"
    assert len(regime.reasons) == 3


def test_classify_regime_trending_down():
    daily = _trend_df(300, 220, 100)
    indicators = compute_indicators(daily)
    volatility = compute_volatility(daily)
    regime = classify_regime(daily, indicators, volatility)
    assert regime.label == "trending_down"
    assert len(regime.reasons) == 3


def test_classify_regime_ranging_for_sideways_noise():
    n = 300
    rng = np.random.default_rng(3)
    close = pd.Series(100 + rng.uniform(-1, 1, n), index=_dates(n))  # no persistent direction
    high = close + 0.5
    low = close - 0.5
    volume = pd.Series([2_000_000] * n, index=_dates(n))
    daily = pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": volume})

    indicators = compute_indicators(daily)
    volatility = compute_volatility(daily)
    regime = classify_regime(daily, indicators, volatility)
    assert regime.label == "ranging"


def test_classify_regime_high_vol_overrides_trend():
    daily = _trend_df(300, 100, 220)  # would otherwise classify as trending_up
    indicators = compute_indicators(daily)
    forced_high_vol = Volatility(
        hv_20d=50.0, hv_60d=40.0, atr_pct_current=10.0, atr_pct_avg_90d=5.0, atr_pct_relative=2.0
    )
    regime = classify_regime(daily, indicators, forced_high_vol)
    assert regime.label == "high_vol"


def test_classify_regime_ranging_when_moving_averages_missing():
    daily = _trend_df(300, 100, 220)
    volatility = compute_volatility(daily)
    indicators_missing_ma = Indicators(
        rsi_14=50.0,
        macd=MACDResult(macd_line=0.0, signal_line=0.0, histogram=0.0, histogram_rising=False),
        vwap=None,
        atr_14=1.0,
        atr_pct=1.0,
        adx_14=25.0,
        moving_averages=MovingAverages(sma_20=None, sma_50=None, sma_200=None, ema_20=None, ema_50=None, ema_200=None),
        bollinger=BollingerBands(upper=101.0, middle=100.0, lower=99.0, bandwidth=0.02, bandwidth_percentile_90d=None),
    )
    regime = classify_regime(daily, indicators_missing_ma, volatility)
    assert regime.label == "ranging"
    assert "بيانات غير كافية" in regime.reasons[0]
