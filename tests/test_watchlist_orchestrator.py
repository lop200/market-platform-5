from __future__ import annotations

import uuid
from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.core.orchestrator import (
    add_watchlist_item,
    get_watchlist_item_events,
    list_watchlist_with_status,
    remove_watchlist_item,
)
from app.db.models import CostLedger
from app.db.session import Base
from app.engines.llm.report_engine import find_banned_phrases


@pytest.fixture
def db_session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = session_factory()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_settings():
    return Settings(
        database_url="sqlite://", default_daily_cap_usd=1.00, default_monthly_cap_usd=5.00,
        cost_anomaly_calls_per_minute=10, market_data_provider="yfinance",
    )


def _expiry_30d() -> str:
    return (date.today() + timedelta(days=30)).isoformat()


def test_add_watchlist_item_starts_green_with_initial_event(db_session, test_settings):
    item = add_watchlist_item(
        db_session, underlying_symbol="AAPL", option_type="call", strike=200.0,
        expiry=_expiry_30d(), reference_price=5.00, alert_threshold_pct=5.0,
    )
    assert item.status_code == "green"
    assert item.change_pct == 0.0
    assert item.worsened is False

    events = get_watchlist_item_events(db_session, uuid.UUID(item.id))
    assert len(events) == 1
    assert events[0].status_code == "green"
    assert find_banned_phrases(item.status_message, "ar") == []


class _FakeQuoteProvider:
    def __init__(self, price: float):
        self._price = price
        self.calls = 0

    def get_quote(self, symbol):
        from app.providers.base import Quote

        self.calls += 1
        return Quote(symbol=symbol, price=self._price, bid=None, ask=None, volume=None,
                     as_of="2026-01-01T00:00:00Z", is_delayed=True)


def test_list_watchlist_reserves_gate_once_and_flags_worsened_on_price_drop(monkeypatch, db_session, test_settings):
    item = add_watchlist_item(
        db_session, underlying_symbol="AAPL", option_type="call", strike=200.0,
        expiry=_expiry_30d(), reference_price=5.00, alert_threshold_pct=5.0,
    )

    def fake_chain(symbol, expiry_str):
        return pd.DataFrame([
            {"strike": 200.0, "bid": 4.6, "ask": 4.8, "lastPrice": 4.70,  # -6% vs 5.00 reference -> red
             "openInterest": 500, "volume": 100, "impliedVolatility": 0.3},
        ])

    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: _FakeQuoteProvider(190.0))
    monkeypatch.setattr("app.core.orchestrator.fetch_yfinance_chain_calls", fake_chain)

    items = list_watchlist_with_status(db_session, settings=test_settings)
    assert len(items) == 1
    refreshed = items[0]
    assert refreshed.last_price == pytest.approx(4.70)
    assert refreshed.status_code == "red"
    assert refreshed.worsened is True

    # EXACTLY one ledger row for the whole refresh — underlying quote fetches must ride
    # the batch reservation, not reserve per symbol. Regression for the live 2026-07-19
    # incident: per-symbol get_live_quote reservations accumulated to >=10 rows/minute
    # under ordinary 30s polling and auto-tripped the anomaly kill-switch.
    assert db_session.query(CostLedger).count() == 1

    # Second refresh at the same (now cached) price: tier unchanged -> no longer "worsened",
    # and no duplicate event should be logged for an unchanged status.
    items_again = list_watchlist_with_status(db_session, settings=test_settings)
    assert items_again[0].worsened is False
    assert db_session.query(CostLedger).count() == 2  # still just one batch row per poll
    events = get_watchlist_item_events(db_session, uuid.UUID(item.id))
    assert len(events) == 2  # initial "added" event + one status-change event (green -> red)


def test_add_watchlist_item_drops_invalidation_above_reference():
    """Regression: the delta+gamma level translation (iv_metrics.py) can produce an
    'invalidation' contract price ABOVE the reference for a far-OTM strike (observed
    live with a real XLF call) — that must never be used as a breach trigger, since it
    would force every added item straight to 'red' at add time regardless of price."""
    from app.db import repository as repo_module
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db.session import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False, autocommit=False)()
    try:
        item = add_watchlist_item(
            db, underlying_symbol="XLF", option_type="call", strike=56.0,
            expiry=_expiry_30d(), reference_price=0.46, alert_threshold_pct=5.0,
            invalidation_price=4.7253,  # nonsensical translated value, above the reference
        )
        assert item.status_code == "green"  # not falsely red from a bogus invalidation breach
        record = repo_module.get_watchlist_item(db, uuid.UUID(item.id))
        assert record.invalidation_price is None
    finally:
        db.close()


def test_add_watchlist_item_timestamps_are_tz_aware_in_output(db_session, test_settings):
    """Regression: SQLite drops tzinfo on round-trip even for timezone(True) columns —
    if the orchestrator serves that naive datetime straight through, the browser's
    `new Date(iso)` parses it as LOCAL time and the UI shows a shifted "last checked"
    clock. Every timestamp handed to the frontend must carry a UTC offset."""
    item = add_watchlist_item(
        db_session, underlying_symbol="AAPL", option_type="call", strike=200.0,
        expiry=_expiry_30d(), reference_price=5.00,
    )
    assert item.added_at.tzinfo is not None
    assert item.last_checked_at.tzinfo is not None

    events = get_watchlist_item_events(db_session, uuid.UUID(item.id))
    assert events[0].occurred_at.tzinfo is not None


def test_remove_watchlist_item_excludes_from_list(monkeypatch, db_session, test_settings):
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: _FakeQuoteProvider(200.0))
    monkeypatch.setattr(
        "app.core.orchestrator.fetch_yfinance_chain_calls",
        lambda symbol, expiry_str: pd.DataFrame([
            {"strike": 200.0, "bid": 4.9, "ask": 5.1, "lastPrice": 5.00,
             "openInterest": 500, "volume": 100, "impliedVolatility": 0.3},
        ]),
    )
    add_watchlist_item(
        db_session, underlying_symbol="AAPL", option_type="call", strike=200.0,
        expiry=_expiry_30d(), reference_price=5.00,
    )
    [item] = list_watchlist_with_status(db_session, settings=test_settings)
    remove_watchlist_item(db_session, uuid.UUID(item.id))
    assert list_watchlist_with_status(db_session, settings=test_settings) == []


def test_list_watchlist_empty_reserves_no_cost_gate(db_session, test_settings):
    assert list_watchlist_with_status(db_session, settings=test_settings) == []
    assert db_session.query(CostLedger).count() == 0


def test_invalidation_breach_shown_even_under_threshold(monkeypatch, db_session, test_settings):
    add_watchlist_item(
        db_session, underlying_symbol="AAPL", option_type="call", strike=200.0,
        expiry=_expiry_30d(), reference_price=5.00, alert_threshold_pct=20.0, invalidation_price=4.90,
    )

    def fake_chain(symbol, expiry_str):
        return pd.DataFrame([
            {"strike": 200.0, "bid": 4.75, "ask": 4.85, "lastPrice": 4.80,  # only -4% (well under the 20% threshold)
             "openInterest": 500, "volume": 100, "impliedVolatility": 0.3},
        ])

    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: _FakeQuoteProvider(195.0))
    monkeypatch.setattr("app.core.orchestrator.fetch_yfinance_chain_calls", fake_chain)

    [item] = list_watchlist_with_status(db_session, settings=test_settings)
    assert item.status_code == "red"  # invalidation (4.90) breached even though decline < threshold
