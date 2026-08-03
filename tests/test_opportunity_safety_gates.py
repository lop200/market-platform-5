from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.opportunities.price_verification import verify_external_price
from app.opportunities.quality import evaluate_quote
from app.opportunities.scanner import resolve_inverse_etf_conflicts
from app.opportunities.schemas import MarketRegime, OpportunityResult
from app.providers.base import Quote


def _quote(**updates) -> Quote:
    now = datetime.now(timezone.utc).isoformat()
    values = dict(
        symbol="AMC", price=2.82, bid=2.81, ask=2.83, volume=1_000_000,
        as_of=now, is_delayed=False, provider="alpaca", feed="sip",
        last_trade=2.82, trade_as_of=now, bid_as_of=now, ask_as_of=now,
        bar_as_of=now, bar_close=2.82, price_source="latest_quote_mid",
    )
    values.update(updates)
    return Quote(**values)


def test_stale_bid_ask_and_trade_are_hard_rejected():
    stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    result = evaluate_quote(
        _quote(as_of=stale, trade_as_of=stale, bid_as_of=stale, ask_as_of=stale),
        Settings(max_quote_age_seconds=30),
    )
    assert not result.accepted
    assert any("قديم" in reason or "قديم" in reason for reason in result.reasons)


def test_external_price_divergence_becomes_data_conflict():
    result = verify_external_price(
        "AMC",
        _quote(),
        Settings(
            price_verification_enabled=True,
            price_verification_required=True,
            price_verification_max_divergence_pct=1.0,
        ),
        reference_price=3.02,
        reference_as_of=datetime.now(timezone.utc),
        reference_provider="independent-test",
    )
    assert not result.accepted
    assert result.status == "data_conflict"
    assert result.data_status == "data_conflict"
    assert result.divergence_pct > 1


def test_stale_external_price_is_not_mislabeled_as_data_conflict():
    result = verify_external_price(
        "NVDA",
        _quote(symbol="NVDA", price=197.16, bid=197.02, ask=197.30),
        Settings(
            price_verification_enabled=True,
            price_verification_required=True,
            price_verification_max_age_seconds=120,
        ),
        reference_price=200.79,
        reference_as_of=datetime.now(timezone.utc) - timedelta(hours=65),
        reference_provider="finnhub",
    )
    assert not result.accepted
    assert result.status == "stale"
    assert result.data_status == "external_stale"
    assert result.divergence_pct is None


def test_tqqq_sqqq_cannot_both_survive_same_scan_batch():
    tqqq = OpportunityResult.model_construct(
        symbol="TQQQ", strategy_id="volume_breakout", overall_score=82,
        status="conditional_entry", warnings_ar=[],
    )
    sqqq = OpportunityResult.model_construct(
        symbol="SQQQ", strategy_id="volume_breakout", overall_score=95,
        status="conditional_entry", warnings_ar=[],
    )
    kept, alternates = resolve_inverse_etf_conflicts(
        [tqqq, sqqq], MarketRegime.BULLISH, "bullish"
    )
    assert [item.symbol for item in kept] == ["TQQQ"]
    assert [item.symbol for item in alternates] == ["SQQQ"]
    assert alternates[0].status == "watch"
    assert "انقلاب اتجاه" in alternates[0].warnings_ar[0]
