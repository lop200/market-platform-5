from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.db.models import StockOpportunity
from app.opportunities.news import classify_news_text
from app.opportunities.scanner import expire_old_opportunities
from app.providers.base import MarketDataAdapter, Quote
from app.providers.resilient import ProviderCircuitOpen, ResilientMarketDataProvider


class FailingProvider(MarketDataAdapter):
    provider_name = "failing"
    def get_daily_ohlcv(self, symbol, lookback_days): raise RuntimeError("down")
    def get_intraday(self, symbol, interval): raise RuntimeError("down")
    def get_quote(self, symbol): raise RuntimeError("down")
    def estimated_cost_per_call(self): return 0
    def is_market_open(self): raise RuntimeError("down")


def test_circuit_breaker_fails_fast_without_hanging():
    provider = ResilientMarketDataProvider(
        FailingProvider(),
        Settings(external_max_retries=0, circuit_breaker_failures=1, circuit_breaker_reset_seconds=60),
    )
    with pytest.raises(RuntimeError):
        provider.get_quote("TEST")
    with pytest.raises(ProviderCircuitOpen):
        provider.get_quote("TEST")


def test_negative_financing_news_is_flagged():
    classification, flags = classify_news_text("Company announces public offering and dilution")
    assert classification == "سلبي قوي"
    assert "offering" in flags
    assert "dilution" in flags


def test_expired_opportunity_is_marked_and_not_left_live(db_session):
    now = datetime.now(timezone.utc)
    row = StockOpportunity(
        symbol="TEST", company_name="Test", status="conditional_entry",
        strategy_id="vwap_reclaim", market_regime="bullish",
        expires_at=now - timedelta(minutes=1), quote_timestamp=now - timedelta(minutes=10),
        price_at_analysis=3.2, entry_from=3.21, entry_to=3.25,
        stop_loss=3.0, risk_reward=2, overall_score=75,
        result_json={"status": "conditional_entry", "targets": []}, data_fingerprint="x" * 64,
    )
    db_session.add(row)
    db_session.commit()
    assert expire_old_opportunities(db_session) == 1
    db_session.refresh(row)
    assert row.status == "expired"
    assert row.result_json["status"] == "expired"
