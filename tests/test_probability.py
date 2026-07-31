from __future__ import annotations

import pytest

from app.opportunities.probability import (
    as_percent,
    finish_in_the_money,
    standard_normal_cdf,
    touch_probability,
)


def test_normal_cdf_matches_known_values():
    assert standard_normal_cdf(0) == pytest.approx(0.5)
    assert standard_normal_cdf(1.96) == pytest.approx(0.975, abs=0.001)
    assert standard_normal_cdf(-1.96) == pytest.approx(0.025, abs=0.001)


def test_at_the_money_expiry_is_a_coin_flip():
    # Spot on the strike, with the small drag of the variance term.
    probability = finish_in_the_money(100, 100, 0.30, 2, is_call=True)
    assert 0.45 < probability < 0.5


def test_further_out_of_the_money_is_always_less_likely():
    near = finish_in_the_money(100, 102, 0.30, 2, is_call=True)
    far = finish_in_the_money(100, 110, 0.30, 2, is_call=True)
    assert near > far
    assert far < 0.05


def test_a_call_and_its_put_cover_every_outcome():
    call = finish_in_the_money(100, 105, 0.35, 7, is_call=True)
    put = finish_in_the_money(100, 105, 0.35, 7, is_call=False)
    assert call + put == pytest.approx(1.0, abs=1e-9)


def test_more_time_and_more_volatility_both_help_a_long_shot():
    base = finish_in_the_money(100, 110, 0.30, 2, is_call=True)
    assert finish_in_the_money(100, 110, 0.30, 10, is_call=True) > base
    assert finish_in_the_money(100, 110, 0.60, 2, is_call=True) > base


def test_touching_a_level_is_easier_than_finishing_past_it():
    # The same barrier, the same horizon: touch must dominate settle.
    touch = touch_probability(100, 105, 40.0, 5)
    settle = finish_in_the_money(100, 105, 0.40, 5, is_call=True)
    assert touch > settle


def test_touch_is_symmetric_in_direction():
    up = touch_probability(100, 105, 40.0, 5)
    down = touch_probability(100, 100 * 100 / 105, 40.0, 5)
    assert up == pytest.approx(down, abs=0.02)


def test_a_level_already_reached_is_certain():
    assert touch_probability(100, 100, 40.0, 5) == 1.0


def test_degenerate_inputs_never_raise_or_exceed_the_bounds():
    for probability in (
        touch_probability(0, 105, 40.0, 5),
        touch_probability(100, 0, 40.0, 5),
        touch_probability(100, 105, 0, 5),
        finish_in_the_money(0, 100, 0.3, 2, True),
        finish_in_the_money(100, 100, 0, 2, True),
    ):
        assert 0.0 <= probability <= 1.0


def test_zero_dte_does_not_divide_by_zero():
    probability = finish_in_the_money(100, 100.5, 0.80, 0, is_call=True)
    assert 0.0 <= probability <= 1.0


def test_percent_is_whole_and_clamped():
    assert as_percent(0.6049) == 60
    assert as_percent(1.4) == 100
    assert as_percent(-1) == 0
