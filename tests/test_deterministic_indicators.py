"""Indicator tests against hand-verifiable reference cases (SRS 23)."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.engines.deterministic.indicators import (
    adx,
    atr,
    bollinger_bands,
    compute_indicators,
    ema,
    macd,
    rsi,
    sma,
    true_range,
    vwap_session,
    wilder_smoothing,
)


def _dates(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-01-01", periods=n, freq="B")


def test_sma_matches_simple_average():
    close = pd.Series([1, 2, 3, 4, 5, 6], index=_dates(6), dtype=float)
    result = sma(close, 3)
    assert result.iloc[-1] == pytest.approx((4 + 5 + 6) / 3)
    assert np.isnan(result.iloc[1])  # not enough data yet


def test_ema_matches_manual_recursive_formula():
    prices = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19]
    close = pd.Series(prices, index=_dates(len(prices)), dtype=float)
    result = ema(close, 3)
    alpha = 2 / (3 + 1)
    expected = prices[0]
    expected_values = [expected]
    for p in prices[1:]:
        expected = alpha * p + (1 - alpha) * expected
        expected_values.append(expected)
    np.testing.assert_allclose(result.to_numpy(), expected_values)


def test_rsi_all_gains_is_100():
    close = pd.Series(range(1, 20), index=_dates(19), dtype=float)  # strictly increasing
    result = rsi(close, 14)
    assert result.iloc[-1] == pytest.approx(100.0)


def test_rsi_all_losses_is_0():
    close = pd.Series(range(19, 0, -1), index=_dates(19), dtype=float)  # strictly decreasing
    result = rsi(close, 14)
    assert result.iloc[-1] == pytest.approx(0.0)


def test_rsi_no_movement_is_50():
    close = pd.Series([100.0] * 20, index=_dates(20))
    result = rsi(close, 14)
    assert result.iloc[-1] == pytest.approx(50.0)


def test_true_range_simple_case():
    high = pd.Series([10.0, 12.0])
    low = pd.Series([8.0, 9.0])
    close = pd.Series([9.0, 11.0])
    tr = true_range(high, low, close)
    # bar 0: high-low = 2 (no prev close)
    assert tr.iloc[0] == pytest.approx(2.0)
    # bar 1: max(12-9=3, |12-9|=3, |9-9|=0) = 3
    assert tr.iloc[1] == pytest.approx(3.0)


def test_atr_constant_true_range_converges_to_that_value():
    n = 30
    # Constructed so every bar's true range is exactly 2.0
    close = pd.Series([100.0] * n, index=_dates(n))
    high = close + 1.0
    low = close - 1.0
    result = atr(high, low, close, period=14)
    assert result.iloc[-1] == pytest.approx(2.0)


def test_wilder_smoothing_seed_is_simple_average():
    values = pd.Series([1, 2, 3, 4, 5], dtype=float)
    result = wilder_smoothing(values, period=3)
    assert np.isnan(result.iloc[0])
    assert np.isnan(result.iloc[1])
    assert result.iloc[2] == pytest.approx((1 + 2 + 3) / 3)
    # next: (seed*(3-1) + value)/3
    assert result.iloc[3] == pytest.approx((result.iloc[2] * 2 + 4) / 3)


def test_macd_line_equals_ema_difference():
    close = pd.Series(np.linspace(100, 130, 60), index=_dates(60))
    result = macd(close, fast=12, slow=26, signal=9)
    expected_macd_line = ema(close, 12) - ema(close, 26)
    np.testing.assert_allclose(result["macd_line"].to_numpy(), expected_macd_line.to_numpy())
    expected_signal = ema(expected_macd_line, 9)
    np.testing.assert_allclose(result["signal_line"].to_numpy(), expected_signal.to_numpy())


def test_bollinger_bands_flat_series_has_zero_bandwidth():
    close = pd.Series([50.0] * 25, index=_dates(25))
    result = bollinger_bands(close, period=20, num_std=2.0)
    assert result["upper"].iloc[-1] == pytest.approx(50.0)
    assert result["lower"].iloc[-1] == pytest.approx(50.0)
    assert result["bandwidth"].iloc[-1] == pytest.approx(0.0)


def test_adx_strong_trend_is_higher_than_choppy_range():
    n = 60
    trending_close = pd.Series(np.linspace(100, 160, n), index=_dates(n))
    trending_high = trending_close + 1
    trending_low = trending_close - 1
    trending_adx = adx(trending_high, trending_low, trending_close, 14).iloc[-1]

    rng = np.random.default_rng(42)
    choppy_close = pd.Series(100 + rng.uniform(-0.5, 0.5, n), index=_dates(n))  # pure noise, no trend
    choppy_high = choppy_close + 1
    choppy_low = choppy_close - 1
    choppy_adx = adx(choppy_high, choppy_low, choppy_close, 14).iloc[-1]

    assert trending_adx > choppy_adx
    assert trending_adx > 20  # SRS 10.5 trending threshold


def test_vwap_session_resets_each_day():
    idx = pd.to_datetime(
        ["2026-01-01 09:30", "2026-01-01 09:31", "2026-01-02 09:30", "2026-01-02 09:31"]
    )
    # high == low == close on every bar so typical price == close (simplifies hand-checking).
    intraday = pd.DataFrame(
        {
            "open": [10.0, 12.0, 20.0, 24.0],
            "high": [10.0, 12.0, 20.0, 24.0],
            "low": [10.0, 12.0, 20.0, 24.0],
            "close": [10.0, 12.0, 20.0, 24.0],
            "volume": [100, 100, 100, 100],
        },
        index=idx,
    )
    result = vwap_session(intraday)
    # day 1, bar 2: cumulative vwap = (10*100 + 12*100) / 200 = 11.0
    assert result.iloc[1] == pytest.approx(11.0)
    # day 2 resets: cumulative vwap = (20*100 + 24*100) / 200 = 22.0, not blended with day 1
    assert result.iloc[2] == pytest.approx(20.0)
    assert result.iloc[3] == pytest.approx(22.0)


def test_compute_indicators_end_to_end_smoke():
    n = 300
    rng = np.random.default_rng(7)
    close = pd.Series(100 + np.cumsum(rng.normal(0.05, 1.0, n)), index=_dates(n))
    high = close + rng.uniform(0.1, 1.0, n)
    low = close - rng.uniform(0.1, 1.0, n)
    volume = pd.Series(rng.integers(1_000_000, 5_000_000, n), index=_dates(n))
    daily = pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": volume})

    result = compute_indicators(daily)
    assert 0 <= result.rsi_14 <= 100
    assert result.atr_14 > 0
    assert result.moving_averages.sma_200 is not None
    assert result.bollinger.upper >= result.bollinger.middle >= result.bollinger.lower
    assert result.vwap is None  # no intraday df passed
