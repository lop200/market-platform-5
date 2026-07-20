from __future__ import annotations

import pytest

from app.engines.options.iv_metrics import (
    compute_expected_move,
    daily_theta_decay_pct,
    translate_stock_level_to_contract_price,
)


def test_expected_move_exact_formula():
    result = compute_expected_move(underlying_price=100.0, implied_vol=0.3, time_to_expiry_years=0.25)
    # pct = 0.3 * sqrt(0.25) = 0.3 * 0.5 = 0.15 -> $15 on $100
    assert result.pct_of_price == pytest.approx(15.0)
    assert result.dollar_amount == pytest.approx(15.0)
    assert result.upper_bound == pytest.approx(115.0)
    assert result.lower_bound == pytest.approx(85.0)


def test_translate_stock_level_up_move():
    price = translate_stock_level_to_contract_price(
        stock_level_price=110.0, current_underlying_price=100.0, current_contract_price=5.0,
        delta=0.5, gamma=0.02,
    )
    # delta*10 + 0.5*gamma*100 = 5 + 1 = 6 -> 5 + 6 = 11.0
    assert price == pytest.approx(11.0)


def test_translate_stock_level_down_move():
    price = translate_stock_level_to_contract_price(
        stock_level_price=90.0, current_underlying_price=100.0, current_contract_price=5.0,
        delta=0.5, gamma=0.02,
    )
    # delta*(-10) + 0.5*gamma*100 = -5 + 1 = -4 -> 5 - 4 = 1.0
    assert price == pytest.approx(1.0)


def test_translate_stock_level_floors_at_zero():
    price = translate_stock_level_to_contract_price(
        stock_level_price=0.0, current_underlying_price=100.0, current_contract_price=5.0,
        delta=0.1, gamma=0.0,
    )
    # delta*(-100) = -10 -> 5 - 10 = -5 -> floored to 0
    assert price == 0.0


def test_daily_theta_decay_pct():
    assert daily_theta_decay_pct(theta=-0.05, current_contract_price=2.0) == pytest.approx(-2.5)


def test_daily_theta_decay_pct_none_for_zero_price():
    assert daily_theta_decay_pct(theta=-0.05, current_contract_price=0.0) is None
