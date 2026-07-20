from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models  # noqa: F401
from app.db.session import Base, get_db
from app.engines.llm.adapters.base import LLMAdapter, LLMResponse
from app.engines.options.greeks import theoretical_price
from app.main import app
from app.providers.base import MarketDataAdapter, Quote

VALID_CONTRACT_JSON = (
    '{"symbol": "NVDA", "option_type": "call", "strike": 130.0, "expiry": "%s", '
    '"contract_price": %.2f, "extraction_confidence": "high", "raw_notes": null}'
)


class FakeVisionAdapter(LLMAdapter):
    def __init__(self, canned_text: str):
        self.canned_text = canned_text
        self.provider_name_value = "fake"

    def generate(self, system_prompt, user_content, max_tokens):
        raise NotImplementedError

    def extract_json_from_image(self, system_prompt, user_prompt, image_bytes, media_type, max_tokens):
        return LLMResponse(text=self.canned_text, input_tokens=1000, output_tokens=80, cost_usd=0.003)

    def estimate_cost(self, input_tokens, max_output_tokens):
        return 0.003

    def count_tokens(self, text):
        return len(text) // 4

    @property
    def provider_name(self):
        return self.provider_name_value


class FakeMarketDataAdapter(MarketDataAdapter):
    def __init__(self, daily: pd.DataFrame):
        self._daily = daily

    def get_daily_ohlcv(self, symbol, lookback_days):
        return self._daily

    def get_intraday(self, symbol, interval):
        return None

    def get_quote(self, symbol):
        return Quote(symbol=symbol, price=150.0, bid=None, ask=None, volume=None, as_of="2026-01-01T00:00:00Z", is_delayed=True)

    def estimated_cost_per_call(self):
        return 0.0

    def is_market_open(self):
        return True

    @property
    def provider_name(self):
        return "fake_market_data"


def _trend_daily(n: int = 300, start: float = 100.0, end: float = 150.0) -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    close = pd.Series(np.linspace(start, end, n), index=idx)
    high = close + 1
    low = close - 1
    volume = pd.Series([2_000_000] * n, index=idx)
    return pd.DataFrame({"open": close, "high": high, "low": low, "close": close, "volume": volume})


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

    daily = _trend_daily()
    underlying_price = float(daily["close"].iloc[-1])
    expiry = (date.today() + timedelta(days=60)).isoformat()
    price = theoretical_price(underlying_price, 130.0, 60 / 365, 0.35, "call", 0.045)
    canned_json = VALID_CONTRACT_JSON % (expiry, price)

    monkeypatch.setattr("app.core.orchestrator.get_llm_adapter", lambda: FakeVisionAdapter(canned_json))
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: FakeMarketDataAdapter(daily))

    test_client = TestClient(app)
    yield test_client, canned_json
    app.dependency_overrides.clear()


def _auth_headers():
    from app.config import get_settings

    return {"X-API-Key": get_settings().api_key}


def test_analyze_option_image_success(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/analyze/option-image",
        headers=_auth_headers(),
        files={"file": ("contract.png", b"fake-png-bytes", "image/png")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "NVDA"
    assert body["option_type"] == "call"
    assert 0 <= body["greeks"]["delta"] <= 1.0
    assert body["cost_usd"] == pytest.approx(0.003)


def test_analyze_option_image_rejects_bad_content_type(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/analyze/option-image",
        headers=_auth_headers(),
        files={"file": ("contract.txt", b"not an image", "text/plain")},
    )
    assert response.status_code == 400


def test_analyze_option_image_requires_api_key(client):
    test_client, _ = client
    response = test_client.post(
        "/api/v1/analyze/option-image",
        files={"file": ("contract.png", b"fake-png-bytes", "image/png")},
    )
    assert response.status_code in (401, 422)  # missing header -> FastAPI 422, or explicit 401


def test_analyze_option_image_returns_422_on_bad_vision_json(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr("app.core.orchestrator.get_llm_adapter", lambda: FakeVisionAdapter("not json"))
    response = test_client.post(
        "/api/v1/analyze/option-image",
        headers=_auth_headers(),
        files={"file": ("contract.png", b"fake-png-bytes", "image/png")},
    )
    assert response.status_code == 422


def test_analyze_option_image_rejects_oversized_file(client):
    test_client, _ = client
    from app.api.routes_options import MAX_IMAGE_BYTES

    oversized = b"0" * (MAX_IMAGE_BYTES + 10)
    response = test_client.post(
        "/api/v1/analyze/option-image",
        headers=_auth_headers(),
        files={"file": ("contract.png", oversized, "image/png")},
    )
    assert response.status_code == 400


def test_analyze_option_image_rejected_by_cost_gate_kill_switch(client, monkeypatch):
    test_client, _ = client

    class AlwaysRejectGate:
        def __init__(self, *args, **kwargs):
            pass

        def check_and_reserve(self, **kwargs):
            from app.core.cost_gate import CostGateDecision

            return CostGateDecision(
                allowed=False, reason="Kill-Switch مفعّل يدوياً", ledger_id=None,
                daily_spent_usd=0, monthly_spent_usd=0, daily_cap_usd=1, monthly_cap_usd=20,
            )

    monkeypatch.setattr("app.core.orchestrator.CostGate", AlwaysRejectGate)
    response = test_client.post(
        "/api/v1/analyze/option-image",
        headers=_auth_headers(),
        files={"file": ("contract.png", b"fake-png-bytes", "image/png")},
    )
    assert response.status_code == 429


def test_analyze_option_image_returns_502_when_data_fetch_fails(client, monkeypatch):
    test_client, _ = client

    class FailingMarketDataAdapter(FakeMarketDataAdapter):
        def get_daily_ohlcv(self, symbol, lookback_days):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "app.core.orchestrator.get_market_data_provider",
        lambda: FailingMarketDataAdapter(_trend_daily()),
    )
    response = test_client.post(
        "/api/v1/analyze/option-image",
        headers=_auth_headers(),
        files={"file": ("contract.png", b"fake-png-bytes", "image/png")},
    )
    assert response.status_code == 502
