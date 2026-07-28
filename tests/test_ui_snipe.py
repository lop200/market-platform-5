from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models  # noqa: F401
from app.db.session import Base, get_db
from app.engines.llm.report_engine import find_banned_phrases
from app.legal.disclaimers import SNIPE_DISCLAIMER_AR
from app.main import app
from app.providers.base import MarketDataAdapter, Quote


class _VariedProvider(MarketDataAdapter):
    def __init__(self, data: dict[str, pd.DataFrame]):
        self._data = data

    def get_daily_ohlcv(self, symbol, lookback_days):
        return self._data[symbol]

    def get_intraday(self, symbol, interval):
        return None

    def get_quote(self, symbol):
        return Quote(symbol=symbol, price=100.0, bid=None, ask=None, volume=None, as_of="2026-01-01T00:00:00Z", is_delayed=True)

    def estimated_cost_per_call(self):
        return 0.0

    def is_market_open(self):
        return False

    @property
    def provider_name(self):
        return "fake_varied"


def _wiggly_daily(n: int, base_price: float, base_volume: float, seed: int) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    rng = np.random.default_rng(seed)
    closes = base_price + np.cumsum(rng.normal(0, base_price * 0.01, n))
    closes = np.clip(closes, base_price * 0.5, base_price * 1.5)
    highs = closes + np.abs(rng.normal(0, base_price * 0.005, n))
    lows = closes - np.abs(rng.normal(0, base_price * 0.005, n))
    return pd.DataFrame(
        {"open": closes, "high": highs, "low": lows, "close": closes, "volume": np.full(n, base_volume)}, index=idx
    )


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
    fake_symbols = [{"symbol": s, "name": s} for s in ["AAA", "BBB", "CCC"]]
    data = {s["symbol"]: _wiggly_daily(300, 100.0, 1_000_000.0, seed=i) for i, s in enumerate(fake_symbols)}
    provider = _VariedProvider(data)
    monkeypatch.setattr("app.core.orchestrator.US_SYMBOLS", fake_symbols)
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: provider)
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_ui_snipe_stocks_renders(client):
    response = client.get("/ui/screener/snipe/stocks")
    assert response.status_code == 200
    assert SNIPE_DISCLAIMER_AR in response.text
    assert "لا توجد بيانات كافية بعد" in response.text  # no audited history exists yet
    assert find_banned_phrases(response.text, "ar") == []
    assert "scenario-probabilities" in response.text
    assert "ليس نسبة نجاح تاريخية" in response.text
    assert "الاستراتيجية المستخدمة" in response.text


def test_ui_snipe_options_renders_auto_picked_contracts(monkeypatch, client):
    """Auto-picked contracts (owner request 2026-07-18) use yfinance's free chain, mocked
    here (same convention as the stock-data provider) so this stays a network-free test.
    The web route calls `run_snipe_options_scan(db)` with no settings override, so it
    falls back to the process-wide `get_settings()` — pin market_data_provider to
    yfinance so the real Alpaca-configured .env doesn't route this into the Alpaca
    branch (added 2026-07-18) instead of these fakes."""
    from datetime import date, timedelta

    from app.config import Settings

    expiry = (date.today() + timedelta(days=2)).isoformat()

    def fake_expirations(symbol):
        return [expiry]

    def fake_chain(symbol, expiry_str, option_type):
        return pd.DataFrame([
            {"contractSymbol": f"{symbol}{option_type.upper()}", "strike": 100.0, "bid": 0.74, "ask": 0.78, "lastPrice": 0.76,
             "openInterest": 800, "volume": 400, "impliedVolatility": 0.35},
        ])

    yfinance_settings = Settings(
        database_url="sqlite://",
        market_data_provider="yfinance",
        snipe_option_min_abs_delta=0.0,
        snipe_option_max_abs_delta=1.0,
        snipe_option_max_theta_decay_pct=1000.0,
    )
    monkeypatch.setattr("app.core.orchestrator.get_settings", lambda: yfinance_settings)
    monkeypatch.setattr("app.core.orchestrator.get_yfinance_expirations", fake_expirations)
    monkeypatch.setattr("app.core.orchestrator.fetch_yfinance_chain", fake_chain)

    response = client.get("/ui/screener/snipe/options")
    assert response.status_code == 200
    assert SNIPE_DISCLAIMER_AR in response.text
    assert "yfinance" in response.text  # data-source caveat always disclosed
    assert find_banned_phrases(response.text, "ar") == []
    assert "scenario-probabilities" in response.text
    assert "الاستراتيجية المستخدمة" in response.text


def test_home_page_shows_snipe_button(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "قنص اليوم" in response.text
    assert '/ui/screener/snipe/stocks' in response.text
