from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.core.cost_gate import CostGate
from app.core.orchestrator import _risk_balance, run_snipe_scan
from app.engines.screener.snipe_schemas import LevelProbability
from app.db import models  # noqa: F401
from app.db.models import AuditTarget, Analysis, CostLedger
from app.db.session import Base, get_db
from app.legal.disclaimers import SNIPE_DISCLAIMER_AR
from app.main import app
from app.providers.base import MarketDataAdapter, Quote


class _VariedProvider(MarketDataAdapter):
    def __init__(self, data: dict[str, pd.DataFrame]):
        self._data = data
        self.calls = 0

    def get_daily_ohlcv(self, symbol, lookback_days):
        self.calls += 1
        if symbol not in self._data:
            raise ValueError(f"no data for {symbol}")
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

    # Pinned to yfinance (not whatever MARKET_DATA_PROVIDER happens to be in the real
    # .env) so the options-chain tests below exercise their injected
    # get_yfinance_expirations/fetch_yfinance_chain_calls fakes deterministically,
    # instead of the orchestrator's Alpaca branch hitting the real network.
    return Settings(
        database_url="sqlite://", default_daily_cap_usd=1.00, default_monthly_cap_usd=5.00,
        cost_anomaly_calls_per_minute=3, market_data_provider="yfinance",
    )


@pytest.fixture
def fake_universe():
    return [{"symbol": s, "name": s} for s in [f"SYM{i}" for i in range(6)]]


def test_run_snipe_scan_reserves_cost_gate_once_and_persists_audit_targets(
    monkeypatch, db_session, test_settings, fake_universe
):
    data = {s["symbol"]: _wiggly_daily(300, 100.0, 1_000_000.0, seed=i) for i, s in enumerate(fake_universe)}
    provider = _VariedProvider(data)
    monkeypatch.setattr("app.core.orchestrator.US_SYMBOLS", fake_universe)
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: provider)

    result = run_snipe_scan(db_session, settings=test_settings)
    assert result.from_cache is False
    assert len(result.cards) > 0

    ledger_count = db_session.query(CostLedger).count()
    assert ledger_count == 1  # ONE batch reservation, not one per symbol

    gate = CostGate(db_session, test_settings)
    assert gate.get_limits().kill_switch_on is False

    snipe_analyses = db_session.query(Analysis).filter(Analysis.kind == "snipe").count()
    assert snipe_analyses == len(result.cards)
    assert db_session.query(AuditTarget).count() >= len(result.cards) * 2  # invalidation + at least zone1 each

    calls_after_first_scan = provider.calls
    assert calls_after_first_scan == len(fake_universe)

    # Second call -> cache hit: no new fetch, no new ledger row, no duplicate persistence.
    cached_result = run_snipe_scan(db_session, settings=test_settings)
    assert cached_result.from_cache is True
    assert cached_result.cached_minutes_ago is not None
    assert provider.calls == calls_after_first_scan
    assert db_session.query(CostLedger).count() == 1
    assert db_session.query(Analysis).filter(Analysis.kind == "snipe").count() == snipe_analyses


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


@pytest.fixture
def auth_headers():
    return {"X-API-Key": get_settings().api_key}


def test_api_screener_snipe_stocks_endpoint(client, auth_headers):
    response = client.get("/api/v1/screener/snipe/stocks", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["disclaimer"] == SNIPE_DISCLAIMER_AR
    assert "cards" in body and "accuracy" in body


def test_api_screener_snipe_options_endpoint_returns_ranked_contracts(monkeypatch, client, auth_headers, test_settings):
    """The auto-pick options tab (owner request 2026-07-18) uses yfinance's free chain,
    isolated behind `get_yfinance_expirations`/`fetch_yfinance_chain_calls` so this stays
    a network-free unit test, same monkeypatch convention as the stock-data provider.
    Hitting the endpoint via TestClient (not calling the orchestrator function directly)
    means `run_snipe_options_scan` falls back to the process-wide `get_settings()` — pin
    it to `test_settings` (market_data_provider="yfinance") so the real Alpaca-configured
    .env can't route this test into the Alpaca branch instead of these fakes."""
    from datetime import date, timedelta

    import pandas as pd

    expiry = (date.today() + timedelta(days=30)).isoformat()

    def fake_expirations(symbol):
        return [expiry]

    def fake_chain(symbol, expiry_str):
        return pd.DataFrame([
            {"contractSymbol": f"{symbol}CALL", "strike": 100.0, "bid": 4.9, "ask": 5.0, "lastPrice": 4.95,
             "openInterest": 800, "volume": 400, "impliedVolatility": 0.35},
        ])

    monkeypatch.setattr("app.core.orchestrator.get_settings", lambda: test_settings)
    monkeypatch.setattr("app.core.orchestrator.get_yfinance_expirations", fake_expirations)
    monkeypatch.setattr("app.core.orchestrator.fetch_yfinance_chain_calls", fake_chain)

    response = client.get("/api/v1/screener/snipe/options", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["disclaimer"] == SNIPE_DISCLAIMER_AR
    assert "yfinance" in body["data_source_note"]
    assert len(body["cards"]) > 0
    card = body["cards"][0]
    assert card["strike"] == 100.0
    assert card["expiry"] == expiry
    assert "delta" in card and "theta" in card


# --- Risk-balance scoring adjustment (owner request 2026-07-18) ---

def _lp(prob: float) -> LevelProbability:
    return LevelProbability(price=100.0, distance_pct=1.0, touch_probability_5d=prob)


def test_risk_balance_flags_invalidation_more_likely_than_target():
    component, imbalanced = _risk_balance(zone1=_lp(0.01), invalidation=_lp(0.43))
    assert imbalanced is True
    assert component == pytest.approx(0.01 / 0.44, abs=1e-6)  # heavily skewed toward invalidation


def test_risk_balance_not_flagged_when_target_more_likely():
    component, imbalanced = _risk_balance(zone1=_lp(0.77), invalidation=_lp(0.0))
    assert imbalanced is False
    assert component == pytest.approx(1.0)


def test_risk_balance_neutral_when_levels_missing():
    component, imbalanced = _risk_balance(zone1=None, invalidation=_lp(0.3))
    assert imbalanced is False
    assert component == pytest.approx(0.5)


def test_risk_balance_neutral_when_both_probabilities_zero():
    component, imbalanced = _risk_balance(zone1=_lp(0.0), invalidation=_lp(0.0))
    assert imbalanced is False
    assert component == pytest.approx(0.5)


def test_snipe_options_card_score_is_pulled_down_by_bad_risk_balance(monkeypatch, db_session, test_settings, fake_universe):
    """Reproduces the reported VZ-style case: mechanically fine contract (high liquidity,
    tight spread, balanced delta) but the invalidation is far more likely than the first
    target zone -> the displayed score must drop below the mechanical score, and the
    imbalance flag/reason must be set."""
    from datetime import date, timedelta

    from app.core.orchestrator import run_snipe_options_scan

    data = {s["symbol"]: _wiggly_daily(300, 100.0, 5_000_000.0, seed=i) for i, s in enumerate(fake_universe)}
    provider = _VariedProvider(data)
    monkeypatch.setattr("app.core.orchestrator.US_SYMBOLS", fake_universe)
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: provider)

    stock_result = run_snipe_scan(db_session, settings=test_settings)
    assert len(stock_result.cards) > 0

    expiry = (date.today() + timedelta(days=30)).isoformat()
    monkeypatch.setattr("app.core.orchestrator.get_yfinance_expirations", lambda symbol: [expiry])
    monkeypatch.setattr(
        "app.core.orchestrator.fetch_yfinance_chain_calls",
        lambda symbol, expiry_str: pd.DataFrame([
            {"contractSymbol": f"{symbol}CALL", "strike": 100.0, "bid": 4.9, "ask": 5.0, "lastPrice": 4.95,
             "openInterest": 800, "volume": 400, "impliedVolatility": 0.35},
        ]),
    )

    # Force a bad risk balance on every stock card's levels regardless of the real scan
    # output, so this test doesn't depend on which symbol happens to be imbalanced today
    # (synthesizing a zone1 for any card that didn't naturally get one).
    for card in stock_result.cards:
        if card.zone1 is None:
            card.zone1 = LevelProbability(price=card.last_close * 1.05, distance_pct=5.0, touch_probability_5d=0.01)
        else:
            card.zone1.touch_probability_5d = 0.01
        card.invalidation.touch_probability_5d = 0.43
    monkeypatch.setattr("app.core.orchestrator.run_snipe_scan", lambda db, settings=None: stock_result)

    options_result = run_snipe_options_scan(db_session, settings=test_settings)
    assert len(options_result.cards) > 0
    for card in options_result.cards:
        assert card.risk_imbalanced is True
        assert card.quality_score < card.mechanical_quality_score
        assert any("الإبطال" in r for r in card.reasons)
