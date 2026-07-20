from __future__ import annotations

import pytest

from app.engines.llm.report_engine import find_banned_phrases
from app.engines.screener.watchlist_status import compute_watchlist_status


def test_status_green_when_flat():
    status = compute_watchlist_status(
        reference_price=100.0, current_price=100.0, alert_threshold_pct=5.0,
        invalidation_price=None, hours_since_added=1.0, is_0dte=False, hours_to_expiry=None,
    )
    assert status.tier == 0
    assert status.code == "green"
    assert status.change_pct == 0.0


def test_status_orange_between_070_and_100_of_threshold():
    # decline 4% of a 5% threshold: 0.7*5=3.5 <= 4 < 5 -> orange (tier 2)
    status = compute_watchlist_status(
        reference_price=100.0, current_price=96.0, alert_threshold_pct=5.0,
        invalidation_price=None, hours_since_added=10.0, is_0dte=False, hours_to_expiry=None,
    )
    assert status.tier == 2
    assert status.code == "orange"
    assert status.decline_pct == pytest.approx(4.0)


def test_status_red_at_or_beyond_threshold():
    status = compute_watchlist_status(
        reference_price=100.0, current_price=94.0, alert_threshold_pct=5.0,
        invalidation_price=None, hours_since_added=10.0, is_0dte=False, hours_to_expiry=None,
    )
    assert status.tier == 3
    assert status.code == "red"


def test_status_black_at_double_threshold():
    status = compute_watchlist_status(
        reference_price=100.0, current_price=89.0, alert_threshold_pct=5.0,
        invalidation_price=None, hours_since_added=10.0, is_0dte=False, hours_to_expiry=None,
    )
    assert status.tier == 4
    assert status.code == "black"


def test_invalidation_breach_forces_red_even_under_threshold():
    # decline only 2% (well under the 5% threshold) but price is at/below invalidation
    status = compute_watchlist_status(
        reference_price=100.0, current_price=98.0, alert_threshold_pct=5.0,
        invalidation_price=99.0, hours_since_added=10.0, is_0dte=False, hours_to_expiry=None,
    )
    assert status.tier == 3
    assert status.invalidation_broken is True


def test_fast_speed_escalates_one_tier():
    # decline 3% -> yellow (tier 1) at a normal pace, but within 18 minutes of adding
    # that's 10%/hour, above the 5%/hour escalation trigger -> bumped to orange
    slow = compute_watchlist_status(
        reference_price=100.0, current_price=97.0, alert_threshold_pct=5.0,
        invalidation_price=None, hours_since_added=10.0, is_0dte=False, hours_to_expiry=None,
    )
    fast = compute_watchlist_status(
        reference_price=100.0, current_price=97.0, alert_threshold_pct=5.0,
        invalidation_price=None, hours_since_added=0.3, is_0dte=False, hours_to_expiry=None,
    )
    assert slow.tier == 1
    assert fast.tier == 2
    assert "سريعة" in fast.message


def test_speed_escalation_skipped_within_the_first_15_minutes():
    # same 6% decline that would normally be tier 3 (red); checked 5 minutes after
    # adding there isn't enough of a time base to judge "speed" yet, so no extra
    # escalation to black should happen from the speed component alone
    status = compute_watchlist_status(
        reference_price=100.0, current_price=94.0, alert_threshold_pct=5.0,
        invalidation_price=None, hours_since_added=0.08, is_0dte=False, hours_to_expiry=None,
    )
    assert status.tier == 3


def test_0dte_near_expiry_escalates_red_to_black():
    status = compute_watchlist_status(
        reference_price=100.0, current_price=94.0, alert_threshold_pct=5.0,
        invalidation_price=None, hours_since_added=10.0, is_0dte=True, hours_to_expiry=0.5,
    )
    assert status.tier == 4
    assert "الانتهاء" in status.message


def test_severe_theta_decay_escalates_red_to_black_even_without_0dte():
    status = compute_watchlist_status(
        reference_price=100.0, current_price=94.0, alert_threshold_pct=5.0,
        invalidation_price=None, hours_since_added=10.0, is_0dte=False, hours_to_expiry=None,
        daily_theta_decay_pct=-9.0,  # theta is negative for a long call; magnitude 9% >= 8% severe cutoff
    )
    assert status.tier == 4
    assert "Theta" in status.message


def test_mild_theta_decay_does_not_escalate():
    status = compute_watchlist_status(
        reference_price=100.0, current_price=94.0, alert_threshold_pct=5.0,
        invalidation_price=None, hours_since_added=10.0, is_0dte=False, hours_to_expiry=None,
        daily_theta_decay_pct=-2.0,
    )
    assert status.tier == 3


def test_0dte_far_from_expiry_stays_red_not_black():
    status = compute_watchlist_status(
        reference_price=100.0, current_price=94.0, alert_threshold_pct=5.0,
        invalidation_price=None, hours_since_added=10.0, is_0dte=True, hours_to_expiry=5.0,
    )
    assert status.tier == 3


def test_threshold_is_adjustable():
    # same 6% decline: tier 3 (red) at the default 5% threshold, but only tier 0 (green,
    # since 6% is below the 0.35*20=7% yellow band) once the owner raises the threshold to 20%
    tight = compute_watchlist_status(
        reference_price=100.0, current_price=94.0, alert_threshold_pct=5.0,
        invalidation_price=None, hours_since_added=10.0, is_0dte=False, hours_to_expiry=None,
    )
    loose = compute_watchlist_status(
        reference_price=100.0, current_price=94.0, alert_threshold_pct=20.0,
        invalidation_price=None, hours_since_added=10.0, is_0dte=False, hours_to_expiry=None,
    )
    assert tight.tier == 3
    assert loose.tier == 0


@pytest.mark.parametrize(
    "current_price,invalidation_price,is_0dte,hours_to_expiry",
    [
        (100.0, None, False, None),
        (97.0, None, False, None),
        (96.0, None, False, None),
        (94.0, None, False, None),
        (89.0, None, False, None),
        (98.0, 99.0, False, None),
        (94.0, None, True, 0.5),
        (101.0, None, False, None),
    ],
)
def test_all_status_messages_pass_banned_phrase_filter(current_price, invalidation_price, is_0dte, hours_to_expiry):
    status = compute_watchlist_status(
        reference_price=100.0, current_price=current_price, alert_threshold_pct=5.0,
        invalidation_price=invalidation_price, hours_since_added=10.0,
        is_0dte=is_0dte, hours_to_expiry=hours_to_expiry,
    )
    assert find_banned_phrases(status.message, "ar") == []
    assert find_banned_phrases(status.label, "ar") == []
