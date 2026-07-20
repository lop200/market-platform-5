from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.audit.self_audit import run_self_audit_once
from app.db import models  # noqa: F401
from app.db import repository
from app.db.session import Base, get_db
from app.engines.deterministic.schemas import LevelStrength, Levels
from app.main import app
from app.providers.base import MarketDataAdapter, Quote


class FakeMarketDataAdapter(MarketDataAdapter):
    def __init__(self, daily: pd.DataFrame):
        self._daily = daily

    def get_daily_ohlcv(self, symbol, lookback_days):
        return self._daily

    def get_intraday(self, symbol, interval):
        return None

    def get_quote(self, symbol):
        return Quote(symbol=symbol, price=100.0, bid=None, ask=None, volume=None, as_of="2026-01-01T00:00:00Z", is_delayed=True)

    def estimated_cost_per_call(self):
        return 0.0

    def is_market_open(self):
        return True

    @property
    def provider_name(self):
        return "fake"


def _synthetic_daily(periods=90, start="2025-12-01") -> pd.DataFrame:
    idx = pd.bdate_range(start=start, periods=periods)
    return pd.DataFrame(
        {"open": [100.0] * periods, "high": [107.0] * periods, "low": [99.0] * periods,
         "close": [100.0] * periods, "volume": [1_000_000] * periods},
        index=idx,
    )


def _auth_headers():
    from app.config import get_settings

    return {"X-API-Key": get_settings().api_key}


@pytest.fixture
def client_with_audit_data(monkeypatch):
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

    db = session_factory()
    record = repository.create_analysis(
        db, symbol="NVDA", market_open=True, data_provider="fake", deterministic_json={},
        scores={"technical": 50, "volatility": 50, "liquidity": 50, "risk": 50, "overall_confidence": 50},
        regime="trending_up", status="data_only",
    )
    record.requested_at = datetime(2025, 12, 8, tzinfo=timezone.utc)
    db.commit()
    repository.update_analysis_report(db, record.id, report_text_ar="x", llm_provider="fake", llm_input_tokens=1, llm_output_tokens=1, total_cost_usd=0.01, status="completed")
    levels = Levels(
        supports=[LevelStrength(price=95.0, touches=1, last_touch_bars_ago=1, avg_volume_at_touches=1, strength_score=1)],
        resistances=[LevelStrength(price=105.0, touches=1, last_touch_bars_ago=1, avg_volume_at_touches=1, strength_score=1)],
        invalidation=93.0,
    )
    repository.create_audit_targets(db, record.id, "NVDA", price_at_analysis=100.0, levels=levels, primary_scenario="bullish")
    db.close()

    daily = _synthetic_daily()
    monkeypatch.setattr("app.audit.self_audit.get_market_data_provider", lambda: FakeMarketDataAdapter(daily))

    audit_db = session_factory()
    run_self_audit_once(audit_db, as_of=date(2026, 1, 20))
    audit_db.close()

    yield TestClient(app)
    app.dependency_overrides.clear()


def test_audit_summary_reflects_recorded_outcomes(client_with_audit_data):
    response = client_with_audit_data.get("/api/v1/audit/summary", headers=_auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3  # horizons 5, 10, 20 all eligible in one run
    for row in body:
        assert row["regime"] == "trending_up"
        assert row["total_audited"] == 1
        assert row["avg_outcome"] == pytest.approx(1.0)


def test_audit_for_symbol_returns_results(client_with_audit_data):
    response = client_with_audit_data.get("/api/v1/audit/NVDA", headers=_auth_headers())
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3
    assert all(row["symbol"] == "NVDA" for row in body)
    assert all(row["scenario_realized"] == "bullish" for row in body)


def test_audit_for_unknown_symbol_returns_empty_list(client_with_audit_data):
    response = client_with_audit_data.get("/api/v1/audit/ZZZZ", headers=_auth_headers())
    assert response.status_code == 200
    assert response.json() == []
