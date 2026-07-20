from __future__ import annotations

import math

import pytest

from app.engines.screener.touch_probability import _standard_normal_cdf, estimate_touch_probability


def test_standard_normal_cdf_known_values():
    assert _standard_normal_cdf(0.0) == pytest.approx(0.5, abs=1e-6)
    assert _standard_normal_cdf(1.645) == pytest.approx(0.95, abs=0.001)
    assert _standard_normal_cdf(1.96) == pytest.approx(0.975, abs=0.001)


def test_probability_is_one_when_level_equals_price():
    assert estimate_touch_probability(100.0, 100.0, hv_20d_annualized_pct=30.0) == 1.0


def test_probability_decreases_with_distance():
    near = estimate_touch_probability(100.0, 102.0, hv_20d_annualized_pct=30.0)
    far = estimate_touch_probability(100.0, 130.0, hv_20d_annualized_pct=30.0)
    assert 0.0 < far < near < 1.0


def test_probability_increases_with_volatility():
    low_vol = estimate_touch_probability(100.0, 110.0, hv_20d_annualized_pct=15.0)
    high_vol = estimate_touch_probability(100.0, 110.0, hv_20d_annualized_pct=60.0)
    assert high_vol > low_vol


def test_zero_volatility_far_level_is_zero():
    assert estimate_touch_probability(100.0, 110.0, hv_20d_annualized_pct=0.0) == 0.0


def test_zero_volatility_same_level_is_one():
    assert estimate_touch_probability(100.0, 100.0, hv_20d_annualized_pct=0.0) == 1.0


def test_non_positive_prices_return_zero():
    assert estimate_touch_probability(0.0, 100.0, hv_20d_annualized_pct=30.0) == 0.0
    assert estimate_touch_probability(100.0, -5.0, hv_20d_annualized_pct=30.0) == 0.0


def test_hand_computed_z_1_645_gives_10_percent():
    # z = |ln(level/price)| / sigma_horizon = 1.645 -> p = 2*(1-Phi(1.645)) ~= 0.10
    price = 100.0
    hv_pct = 30.0
    horizon = 5
    daily_sigma = hv_pct / 100 / math.sqrt(252)
    sigma_horizon = daily_sigma * math.sqrt(horizon)
    level = price * math.exp(1.645 * sigma_horizon)
    p = estimate_touch_probability(price, level, hv_pct, horizon_days=horizon)
    assert p == pytest.approx(0.10, abs=0.005)
