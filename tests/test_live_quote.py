from __future__ import annotations

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.orchestrator import get_live_quote
from app.db import models  # noqa: F401
from app.db.models import CostLedger
from app.db.session import Base, get_db
from app.main import app
from app.providers.base import MarketDataAdapter, Quote


class _CountingQuoteProvider(MarketDataAdapter):
    def __init__(self, price: float = 123.45):
        self.price = price
        self.calls = 0

    def get_daily_ohlcv(self, symbol, lookback_days):
        raise NotImplementedError

    def get_intraday(self, symbol, interval):
        return None

    def get_quote(self, symbol):
        self.calls += 1
        return Quote(symbol=symbol, price=self.price, bid=None, ask=None, volume=None, as_of="2026-01-01T00:00:00Z", is_delayed=False)

    def estimated_cost_per_call(self):
        return 0.0

    def is_market_open(self):
        return True

    @property
    def provider_name(self):
        return "fake_quote"


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
    from app.config import Settings

    return Settings(
        database_url="sqlite://", default_daily_cap_usd=1.00, default_monthly_cap_usd=5.00,
        cost_anomaly_calls_per_minute=10,
    )


def test_get_live_quote_caches_within_ttl(monkeypatch, db_session, test_settings):
    provider = _CountingQuoteProvider(price=100.0)
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: provider)

    first = get_live_quote(db_session, "NVDA", settings=test_settings)
    assert first["price"] == 100.0
    assert first["from_cache"] is False
    assert provider.calls == 1
    assert db_session.query(CostLedger).count() == 1

    provider.price = 105.0  # a "later" real price, shouldn't be seen while still cached
    second = get_live_quote(db_session, "NVDA", settings=test_settings)
    assert second["from_cache"] is True
    assert second["price"] == 100.0
    assert provider.calls == 1  # no new provider call
    assert db_session.query(CostLedger).count() == 1


def test_get_live_quote_goes_through_cost_gate(monkeypatch, db_session, test_settings):
    provider = _CountingQuoteProvider()
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: provider)
    get_live_quote(db_session, "AAPL", settings=test_settings)
    ledger = db_session.query(CostLedger).one()
    assert ledger.category == "market_data"
    assert ledger.provider == "fake_quote"


@pytest.fixture
def client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    provider = _CountingQuoteProvider(price=250.5)
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: provider)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_ui_quote_endpoint_returns_price(client):
    response = client.get("/ui/quote/NVDA")
    assert response.status_code == 200
    body = response.json()
    assert body["price"] == 250.5
    assert body["symbol"] == "NVDA"


def test_ui_quote_endpoint_rejects_invalid_symbol(client):
    response = client.get("/ui/quote/not-a-symbol!!")
    assert response.status_code == 200
    body = response.json()
    assert "error" in body
