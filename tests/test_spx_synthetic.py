from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import exp

import pytest

from app.config import Settings
from app.options.market_clock import market_session
from app.spx.schemas import SPXContract
from app.spx.synthetic import (
    SECONDS_PER_YEAR,
    calculate_synthetic_value,
    settlement_at,
    weighted_median,
)

NOW = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)
EXPIRATION = datetime(2026, 8, 7, tzinfo=timezone.utc)


def synthetic_settings(**overrides) -> Settings:
    values = {
        "options_enabled": True,
        "spx_enabled": True,
        "spx_paper_only": True,
        "spx_synthetic_enabled": True,
        "spx_synthetic_paper_only": True,
        "spx_synthetic_min_pairs": 5,
        "spx_synthetic_max_pairs": 15,
        "spx_synthetic_max_quote_age_seconds": 15,
        "spx_synthetic_max_pair_time_diff_seconds": 2,
        "spx_synthetic_max_spread_pct": 12,
        "spx_synthetic_max_dispersion_points": 5,
        "spx_synthetic_max_range_width_points": 15,
        "spx_synthetic_max_convergence_points": 2,
        "spx_synthetic_min_confidence_score": 0,
        "spx_synthetic_min_data_quality_score": 0,
        "spx_risk_free_rate": 0.05,
        "spx_risk_free_rate_updated_at": NOW.isoformat(),
        "spx_allow_spot_estimate": False,
    }
    values.update(overrides)
    return Settings(**values)


def contract(
    side: str,
    strike: float,
    mid: float,
    *,
    expiration: datetime = EXPIRATION,
    settlement_type: str = "PM_CASH",
    style: str = "european",
    age: float = 1,
    timestamp_offset: float = 0,
    spread: float = 0.2,
    bid: float | None = None,
    ask: float | None = None,
    feed: str = "opra",
) -> SPXContract:
    symbol_side = "C" if side == "call" else "P"
    quote_time = NOW - timedelta(seconds=age) + timedelta(
        seconds=timestamp_offset
    )
    return SPXContract(
        symbol=f"SPXW260807{symbol_side}{int(strike * 1000):08d}",
        option_type=side,
        strike=strike,
        expiration=expiration,
        bid=mid - spread / 2 if bid is None else bid,
        ask=mid + spread / 2 if ask is None else ask,
        volume=50,
        open_interest=500,
        quote_timestamp=quote_time,
        feed=feed,
        root_symbol="SPXW",
        settlement_type=settlement_type,
        exercise_style=style,
    )


def pairs(
    *,
    forward: float = 5500,
    count: int = 9,
    settings: Settings | None = None,
) -> list[SPXContract]:
    active_settings = settings or synthetic_settings()
    settlement = settlement_at(EXPIRATION.date(), "PM_CASH")
    years = (
        settlement.astimezone(timezone.utc) - NOW
    ).total_seconds() / SECONDS_PER_YEAR
    discount_factor = exp(active_settings.spx_risk_free_rate * years)
    strikes = [forward + (index - count // 2) * 5 for index in range(count)]
    result: list[SPXContract] = []
    for strike in strikes:
        difference = (forward - strike) / discount_factor
        call_mid = 35 + difference / 2
        put_mid = 35 - difference / 2
        result.extend(
            [
                contract("call", strike, call_mid),
                contract(
                    "put",
                    strike,
                    put_mid,
                    timestamp_offset=0.5,
                ),
            ]
        )
    return result


def calculate(items: list[SPXContract], **overrides):
    return calculate_synthetic_value(
        items,
        synthetic_settings(**overrides),
        market_session(NOW),
        now=NOW,
    )


def test_forward_and_bounds_are_calculated_from_bid_ask_mids():
    result = calculate(pairs())
    assert result.provider_status == "ready"
    assert result.synthetic_forward_value == pytest.approx(5500, abs=0.01)
    assert result.lower_bound < result.synthetic_forward_value
    assert result.upper_bound > result.synthetic_forward_value
    assert result.implied_range_width_points < 1
    assert 5 <= result.pairs_used <= 15
    assert result.source == "Alpaca OPRA Synthetic"


def test_spot_estimate_requires_fresh_rate_and_dividend_yield():
    disabled = calculate(pairs())
    assert disabled.synthetic_spot_estimate is None
    enabled_settings = synthetic_settings(
        spx_allow_spot_estimate=True,
        spx_dividend_yield=0.015,
        spx_dividend_yield_updated_at=NOW.isoformat(),
    )
    result = calculate_synthetic_value(
        pairs(settings=enabled_settings),
        enabled_settings,
        market_session(NOW),
        now=NOW,
    )
    settlement = settlement_at(EXPIRATION.date(), "PM_CASH")
    years = (
        settlement.astimezone(timezone.utc) - NOW
    ).total_seconds() / SECONDS_PER_YEAR
    expected = 5500 * exp(-(0.05 - 0.015) * years)
    assert result.synthetic_spot_estimate == pytest.approx(expected, abs=0.02)


def test_missing_dividend_yield_disables_spot_without_blocking_forward():
    result = calculate(
        pairs(),
        spx_allow_spot_estimate=True,
        spx_dividend_yield=None,
    )
    assert result.provider_status == "ready"
    assert result.synthetic_forward_value is not None
    assert result.synthetic_spot_estimate is None


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (
            lambda rows: setattr(
                rows[1], "expiration", EXPIRATION + timedelta(days=1)
            ),
            "missing_side",
        ),
        (
            lambda rows: setattr(rows[1], "settlement_type", "AM_CASH"),
            "missing_side",
        ),
        (
            lambda rows: setattr(
                rows[1],
                "quote_timestamp",
                rows[1].quote_timestamp + timedelta(seconds=4),
            ),
            "pair_time_diff",
        ),
        (lambda rows: setattr(rows[0], "ask", rows[0].bid - 0.1), "invalid_bid_ask"),
        (lambda rows: setattr(rows[0], "bid", None), "missing_bid_ask"),
        (
            lambda rows: setattr(
                rows[0],
                "quote_timestamp",
                NOW - timedelta(seconds=20),
            ),
            "pair_time_diff",
        ),
    ],
)
def test_invalid_pair_inputs_are_rejected(mutation, reason):
    items = pairs(count=5)
    mutation(items)
    result = calculate(items)
    assert result.provider_status in {"insufficient_pairs", "stale"}
    assert result.rejection_reasons.get(reason, 0) >= 1


def test_wide_spread_is_rejected():
    items = pairs(count=5)
    items[0].bid = 1
    items[0].ask = 10
    result = calculate(items)
    assert result.provider_status == "insufficient_pairs"
    assert result.rejection_reasons["wide_spread"] == 1


def test_fewer_than_five_pairs_are_rejected():
    result = calculate(pairs(count=4))
    assert result.provider_status == "insufficient_pairs"
    assert result.pairs_used == 0


def test_outlier_is_removed_and_weighted_median_is_robust():
    items = pairs(count=9)
    items.extend(
        [
            contract("call", 5600, 80),
            contract("put", 5600, 10, timestamp_offset=0.5),
        ]
    )
    result = calculate(items)
    assert result.provider_status == "ready"
    assert result.outliers_removed == 1
    assert result.synthetic_forward_value == pytest.approx(5500, abs=0.1)
    assert weighted_median([1, 2, 100], [10, 10, 1]) == 2


def test_wide_range_and_dispersion_stop_analysis():
    wide = calculate(pairs(), spx_synthetic_max_range_width_points=0.1)
    assert wide.provider_status == "wide_dispersion"
    dispersed_items = pairs(count=9)
    for index in range(0, len(dispersed_items), 2):
        dispersed_items[index].bid += index * 0.3
        dispersed_items[index].ask += index * 0.3
    dispersed = calculate(
        dispersed_items,
        spx_synthetic_max_dispersion_points=0.05,
    )
    assert dispersed.provider_status == "wide_dispersion"


def test_convergence_has_a_bounded_iteration_count():
    result = calculate(pairs())
    assert result.iterations <= 2
    assert len(result.convergence_points) <= 3


def test_closed_market_and_feature_flags_stop_calculation():
    closed_at = datetime(2026, 7, 30, 22, 0, tzinfo=timezone.utc)
    closed = calculate_synthetic_value(
        pairs(),
        synthetic_settings(),
        market_session(closed_at),
        now=closed_at,
    )
    assert closed.provider_status == "options_closed"
    disabled = calculate(pairs(), spx_synthetic_enabled=False)
    assert disabled.provider_status == "unavailable"
    options_disabled = calculate(pairs(), options_enabled=False)
    assert options_disabled.provider_status == "unavailable"


def test_synthetic_value_is_never_labeled_official():
    result = calculate(pairs())
    serialized = result.model_dump_json()
    assert "Synthetic" in result.source
    assert "Official SPX" not in serialized
    assert "SPX Direct Price" not in serialized
