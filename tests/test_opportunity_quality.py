from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.opportunities.quality import evaluate_quote
from app.providers.base import Quote


def quote(**overrides) -> Quote:
    values = {
        "symbol": "TEST", "price": 3.0, "bid": 2.99, "ask": 3.01,
        "volume": 1_000_000, "as_of": datetime.now(timezone.utc).isoformat(),
        "is_delayed": False, "provider": "alpaca", "feed": "sip",
    }
    values.update(overrides)
    return Quote(**values)


def test_data_quality_does_not_apply_scanner_price_range():
    settings = Settings()
    assert evaluate_quote(quote(price=1.99, bid=1.98, ask=2.00), settings).accepted
    assert evaluate_quote(quote(price=500, bid=499, ask=501), settings).accepted


def test_rejects_missing_book_and_wide_spread():
    settings = Settings(max_spread_pct=2)
    assert not evaluate_quote(quote(bid=None), settings).accepted
    decision = evaluate_quote(quote(bid=2.8, ask=3.2), settings)
    assert not decision.accepted
    assert any("السبريد" in reason for reason in decision.reasons)


def test_rejects_stale_quote_and_warns_for_iex():
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    decision = evaluate_quote(quote(as_of=old, feed="iex"), Settings(max_quote_age_seconds=60))
    assert not decision.accepted
    assert any("قديم" in reason for reason in decision.reasons)
    assert any("IEX" in warning for warning in decision.warnings)


def test_small_cap_speculative_profile_allows_only_a_bounded_four_percent_spread():
    settings = Settings(max_spread_pct=2.5, speculative_max_spread_pct=4)
    assert evaluate_quote(quote(price=2, bid=1.965, ask=2.035), settings).accepted
    rejected = evaluate_quote(quote(price=2, bid=1.95, ask=2.05), settings)
    assert not rejected.accepted
    assert any("السبريد" in reason for reason in rejected.reasons)
