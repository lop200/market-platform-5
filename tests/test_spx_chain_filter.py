from __future__ import annotations

from app.spx.providers import AlpacaSPXProvider

settlement = AlpacaSPXProvider._settlement_type


def test_an_index_option_with_nothing_to_deliver_is_still_cash_settled():
    """SPXW settles in cash by construction, so its deliverables list is empty.

    Demanding a non-empty list rejected every contract in the chain and left
    the sniper reporting "OPRA unavailable" against a working feed.
    """
    row = {"style": "european", "deliverables": []}
    assert settlement(row, "SPXW") == "PM_CASH"
    assert settlement({"style": "european"}, "SPXW") == "PM_CASH"


def test_an_explicit_cash_deliverable_still_passes():
    row = {"style": "european", "deliverables": [{"type": "cash"}]}
    assert settlement(row, "SPXW") == "PM_CASH"


def test_anything_physically_settled_is_still_refused():
    row = {"style": "european", "deliverables": [{"type": "equity"}]}
    assert settlement(row, "SPXW") is None
    mixed = {"style": "european", "deliverables": [{"type": "cash"}, {"type": "equity"}]}
    assert settlement(mixed, "SPXW") is None


def test_the_american_style_and_other_roots_stay_out():
    assert settlement({"style": "american", "deliverables": []}, "SPXW") is None
    assert settlement({"style": "european", "deliverables": []}, "SPX") is None
    assert settlement({"style": "european", "deliverables": []}, "XSP") is None
