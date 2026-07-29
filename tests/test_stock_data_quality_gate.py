from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd

from app.config import Settings
from app.providers.base import Quote
from app.stocks.quality import evaluate_plan_data


def bars(age_seconds: int = 30) -> pd.DataFrame:
    index = pd.date_range(
        end=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
        periods=30,
        freq="5min",
    )
    return pd.DataFrame(
        {"open": 3.0, "high": 3.1, "low": 2.9, "close": 3.05, "volume": 100_000},
        index=index,
    )


def quote(**overrides) -> Quote:
    now = datetime.now(timezone.utc).isoformat()
    values = dict(
        symbol="TEST", price=3.05, bid=3.04, ask=3.06, volume=1_000_000,
        as_of=now, is_delayed=False, provider="fake", feed="sip",
        trade_as_of=now, bid_as_of=now, ask_as_of=now,
    )
    values.update(overrides)
    return Quote(**values)


def test_stale_quote_prevents_trade_plan():
    old = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    decision = evaluate_plan_data(
        quote(as_of=old, trade_as_of=old, bid_as_of=old, ask_as_of=old),
        bars(), Settings(max_quote_age_seconds=90), market_open=True,
    )
    assert not decision.valid_for_plan
    assert any("أقدم" in reason for reason in decision.reasons)


def test_wide_spread_and_closed_market_prevent_plan():
    decision = evaluate_plan_data(
        quote(bid=3.0, ask=3.5), bars(), Settings(max_spread_pct=2), market_open=False,
    )
    assert not decision.valid_for_plan
    assert any("السبريد" in reason for reason in decision.reasons)
    assert any("السوق مغلق" in reason for reason in decision.reasons)


def test_bid_and_ask_timestamp_skew_is_rejected():
    old = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    decision = evaluate_plan_data(
        quote(bid_as_of=old), bars(),
        Settings(max_quote_timestamp_skew_seconds=5), market_open=True,
    )
    assert not decision.valid_for_plan
    assert any("غير متزامن" in reason for reason in decision.reasons)
