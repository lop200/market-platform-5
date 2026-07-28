from __future__ import annotations

import json
import uuid

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models  # noqa: F401
from app.db.session import Base, get_db
from app.engines.llm.adapters.base import LLMAdapter, LLMResponse
from app.main import app
from app.providers.base import MarketDataAdapter, Quote


class ScriptedAdapter(LLMAdapter):
    def __init__(self, canned_text: str):
        self.canned_text = canned_text
        self.provider_name_value = "scripted"

    def generate(self, system_prompt, user_content, max_tokens):
        return LLMResponse(text=self.canned_text, input_tokens=900, output_tokens=250, cost_usd=0.004)

    def extract_json_from_image(self, *a, **k):
        raise NotImplementedError

    def estimate_cost(self, input_tokens, max_output_tokens):
        return 0.004

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
        return Quote(symbol=symbol, price=float(self._daily["close"].iloc[-1]), bid=None, ask=None, volume=None, as_of="2026-01-01T00:00:00Z", is_delayed=True)

    def estimated_cost_per_call(self):
        return 0.0

    def is_market_open(self):
        return True

    @property
    def provider_name(self):
        return "fake_market_data"


def _zigzag_daily(n: int = 250) -> pd.DataFrame:
    pattern = [100, 110, 120, 125, 128, 130, 125, 118, 110, 118, 128, 138, 145, 138, 128, 120, 125, 130]
    closes = (pattern * ((n // len(pattern)) + 2))[:n]
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    volume = pd.Series([2_000_000] * n, index=idx)
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "volume": volume}, index=idx)


def _valid_report_json(symbol: str) -> str:
    return json.dumps(
        {
            "tldr_ar": f"{symbol} تحليل فني موجز.",
            "scenario_bullish_ar": "x", "scenario_bearish_ar": "x", "scenario_neutral_ar": "x",
            "full_report_ar": f"تحليل {symbol} فني موجز.",
            "devils_advocate_ar": "لم يرصد المحرك إنذارات بنيوية بارزة — وهذا بحد ذاته يستدعي الحذر من الرضا الزائد.",
        },
        ensure_ascii=False,
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

    daily = _zigzag_daily()
    monkeypatch.setattr("app.core.orchestrator.get_llm_adapter", lambda: ScriptedAdapter(_valid_report_json("NVDA")))
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: FakeMarketDataAdapter(daily))

    yield TestClient(app)
    app.dependency_overrides.clear()


def _auth_headers():
    from app.config import get_settings

    return {"X-API-Key": get_settings().api_key}


def test_analyze_happy_path(client):
    response = client.post("/api/v1/analyze", headers=_auth_headers(), json={"symbol": "nvda", "lang": "ar"})
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "NVDA"
    assert body["from_cache"] is False
    assert "NVDA" in body["report_ar"]
    assert "إنذارات" in body["devils_advocate_ar"] or "الرضا" in body["devils_advocate_ar"]
    assert body["cost_usd"] == pytest.approx(0.004)
    probabilities = body["scenario_probabilities"]
    assert probabilities["bullish_pct"] + probabilities["bearish_pct"] + probabilities["neutral_pct"] == pytest.approx(100.0)
    assert probabilities["calibrated"] is False


def test_analyze_rejects_invalid_symbol(client):
    response = client.post("/api/v1/analyze", headers=_auth_headers(), json={"symbol": "###", "lang": "ar"})
    assert response.status_code == 400


def test_analyze_returns_422_when_symbol_has_no_market_data(client, monkeypatch):
    class FailingMarketDataAdapter(FakeMarketDataAdapter):
        def get_daily_ohlcv(self, symbol, lookback_days):
            raise ValueError(f"no daily OHLCV returned for symbol '{symbol}'")

    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: FailingMarketDataAdapter(_zigzag_daily()))
    response = client.post("/api/v1/analyze", headers=_auth_headers(), json={"symbol": "APPLE", "lang": "ar"})
    assert response.status_code == 422


def test_get_report_roundtrips_devils_advocate_split(client):
    analyze_response = client.post("/api/v1/analyze", headers=_auth_headers(), json={"symbol": "NVDA", "lang": "ar"})
    analysis_id = analyze_response.json()["analysis_id"]

    report_response = client.get(f"/api/v1/report/{analysis_id}", headers=_auth_headers())
    assert report_response.status_code == 200
    body = report_response.json()
    assert body["report_ar"] == analyze_response.json()["report_ar"]
    assert body["devils_advocate_ar"] == analyze_response.json()["devils_advocate_ar"]


def test_get_report_invalid_uuid_returns_400(client):
    response = client.get("/api/v1/report/not-a-uuid", headers=_auth_headers())
    assert response.status_code == 400


def test_get_report_missing_returns_404(client):
    response = client.get(f"/api/v1/report/{uuid.uuid4()}", headers=_auth_headers())
    assert response.status_code == 404


def test_get_history_returns_recent_analyses(client):
    client.post("/api/v1/analyze", headers=_auth_headers(), json={"symbol": "NVDA", "lang": "ar"})
    response = client.get("/api/v1/history", headers=_auth_headers(), params={"symbol": "NVDA", "limit": 5})
    assert response.status_code == 200
    body = response.json()
    assert len(body) >= 1
    assert body[0]["symbol"] == "NVDA"
