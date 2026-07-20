from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.engines.deterministic.liquidity import compute_liquidity
from app.engines.deterministic.volatility import compute_volatility, historical_volatility
from app.providers.base import Quote


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="B")


def _flat_ohlc(closes, volumes=None):
    n = len(closes)
    volumes = volumes or [1_000_000] * n
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes, "volume": volumes},
        index=_dates(n),
    )


def test_historical_volatility_zero_for_constant_growth():
    n = 80
    close = pd.Series([100 * (1.01**i) for i in range(n)], index=_dates(n))
    result = historical_volatility(close, 20)
    assert result.iloc[-1] == pytest.approx(0.0, abs=1e-6)


def test_historical_volatility_matches_independent_numpy_calc():
    rng = np.random.default_rng(1)
    n = 80
    close = pd.Series(100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))), index=_dates(n))
    result = historical_volatility(close, 20)

    log_returns = np.log(close / close.shift(1)).dropna()
    window_returns = log_returns.iloc[-20:]
    expected = window_returns.std(ddof=1) * np.sqrt(252) * 100
    assert result.iloc[-1] == pytest.approx(expected)


def test_atr_pct_relative_is_one_for_constant_range():
    n = 120
    close = pd.Series([100.0] * n, index=_dates(n))
    high = close + 1.0
    low = close - 1.0
    daily = pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": [1_000_000] * n})
    result = compute_volatility(daily)
    assert result.atr_pct_current == pytest.approx(result.atr_pct_avg_90d, rel=1e-6)
    assert result.atr_pct_relative == pytest.approx(1.0, rel=1e-6)


def test_liquidity_rvol_and_dollar_volume_on_volume_spike():
    n = 25
    volumes = [1_000_000] * (n - 1) + [4_000_000]
    closes = [50.0] * n
    daily = _flat_ohlc(closes, volumes)

    result = compute_liquidity(daily)
    expected_avg = float(np.mean(volumes[-20:]))
    assert result.avg_volume_20d == pytest.approx(expected_avg, rel=1e-6)
    assert result.rvol == pytest.approx(volumes[-1] / expected_avg, abs=1e-4)
    assert result.dollar_volume == pytest.approx(volumes[-1] * closes[-1], rel=1e-6)
    assert result.unusual_volume is True
    assert result.unusual_volume_direction == "flat"  # price didn't move despite the volume spike


def test_liquidity_normal_volume_is_not_unusual():
    n = 25
    volumes = [1_000_000] * n
    closes = [50.0] * n
    daily = _flat_ohlc(closes, volumes)
    result = compute_liquidity(daily)
    assert result.rvol == pytest.approx(1.0, rel=1e-6)
    assert result.unusual_volume is False
    assert result.unusual_volume_direction is None


def test_liquidity_unusual_volume_direction_up_on_price_rise():
    n = 25
    volumes = [1_000_000] * (n - 1) + [4_000_000]
    closes = [50.0] * (n - 1) + [55.0]
    daily = _flat_ohlc(closes, volumes)
    result = compute_liquidity(daily)
    assert result.unusual_volume is True
    assert result.unusual_volume_direction == "up"


def test_liquidity_spread_pct_from_quote():
    daily = _flat_ohlc([50.0] * 25)
    quote = Quote(symbol="TEST", price=50.0, bid=49.9, ask=50.1, volume=1000, as_of="2026-01-01T00:00:00Z", is_delayed=True)
    result = compute_liquidity(daily, quote=quote)
    expected_spread = (50.1 - 49.9) / 50.0 * 100
    assert result.spread_pct == pytest.approx(expected_spread, rel=1e-6)


def test_liquidity_spread_pct_none_without_quote():
    daily = _flat_ohlc([50.0] * 25)
    result = compute_liquidity(daily, quote=None)
    assert result.spread_pct is None
