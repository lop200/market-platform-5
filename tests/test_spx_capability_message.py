from __future__ import annotations

from app.config import Settings
from app.spx.providers import AlpacaSPXProvider, _capability_message


def test_forbidden_index_names_the_subscription_and_the_etf_alternative():
    message = _capability_message(underlying=False, options=True, index_status=403)
    assert message == AlpacaSPXProvider.INDEX_SUBSCRIPTION_HINT
    # The reader must learn it is a subscription gap, not a broken key.
    assert "اشتراك منفصل" in message
    assert "SPY" in message


def test_other_index_failures_report_their_own_code():
    assert "500" in _capability_message(underlying=False, options=True, index_status=500)
    assert "اشتراك" not in _capability_message(
        underlying=False, options=True, index_status=500
    )


def test_working_index_and_chain_say_so():
    assert _capability_message(True, True, 200) == "بيانات SPX وعقوده متاحة من Alpaca."


def test_cheap_contracts_are_the_preferred_size():
    # Owner asked for cheap contracts with no floor; the ranker treats anything
    # at or under this as fully affordable.
    assert Settings().options_preferred_contract_cost_usd == 100.0
    assert Settings().options_max_contract_cost_usd == 500.0
