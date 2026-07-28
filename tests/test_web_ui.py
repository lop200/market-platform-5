from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.cost_gate import CostGate
from app.db import models  # noqa: F401
from app.db.session import Base, get_db
from app.engines.llm.adapters.base import LLMAdapter, LLMResponse
from app.main import app
from app.providers.base import MarketDataAdapter, Quote


class ScriptedAdapter(LLMAdapter):
    def __init__(self, canned_text: str):
        self.canned_text = canned_text

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
        return "scripted"


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
            "devils_advocate_ar": "لم يرصد المحرك إنذارات بنيوية بارزة.",
        },
        ensure_ascii=False,
    )


class FakeVisionAdapter(LLMAdapter):
    def __init__(self, canned_text: str):
        self.canned_text = canned_text

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
        return "fake_vision"


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


def test_index_page_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "منصة الذكاء التحليلي" in response.text
    assert "المصروف اليومي" in response.text


def test_ui_analyze_renders_report(client):
    """Phase 1 (deterministic-only, progressive load): price/regime/plain-summary render
    immediately; the LLM narrative is a background fetch (see test below), not present in
    this first HTML response."""
    response = client.post("/ui/analyze", data={"symbol": "NVDA", "lang": "ar"})
    assert response.status_code == 200
    assert "NVDA" in response.text
    assert "trending_up" in response.text or "ranging" in response.text or "high_vol" in response.text or "trending_down" in response.text
    assert "جارٍ تحضير الشرح" in response.text
    assert 'data-needs-narrative="true"' in response.text
    assert "scenario-probabilities" in response.text
    assert "ليس نسبة نجاح تاريخية" in response.text


def test_ui_analyze_narrative_endpoint_returns_llm_text(client):
    """Phase 2: the page's own JS fetches this endpoint right after phase 1; it must
    return the LLM-generated fields (and persist/cache them, covered in orchestrator
    tests) — this test just checks the web route wiring end to end."""
    first = client.post("/ui/analyze", data={"symbol": "NVDA", "lang": "ar"})
    marker = 'data-analysis-id="'
    start = first.text.index(marker) + len(marker)
    analysis_id = first.text[start:first.text.index('"', start)]

    response = client.get(f"/ui/analyze/narrative/{analysis_id}", params={"lang": "ar"})
    assert response.status_code == 200
    body = response.json()
    assert "error" not in body
    assert body["devils_advocate_ar"] == "لم يرصد المحرك إنذارات بنيوية بارزة."


def test_ui_analyze_shows_indicator_explanations(client):
    response = client.post("/ui/analyze", data={"symbol": "NVDA", "lang": "ar"})
    assert response.status_code == 200
    assert "indicator-card" in response.text
    assert "RSI (14)" in response.text
    assert "تشبع شرائي" in response.text


def test_ui_analyze_shows_error_for_invalid_symbol(client):
    response = client.post("/ui/analyze", data={"symbol": "###", "lang": "ar"})
    assert response.status_code == 200
    assert "error-banner" in response.text


def test_ui_analyze_shows_error_when_cost_gate_blocks(client):
    engine_db = next(client.app.dependency_overrides[get_db]())
    CostGate(engine_db).enable_kill_switch()
    response = client.post("/ui/analyze", data={"symbol": "NVDA", "lang": "ar"})
    assert response.status_code == 200
    assert "error-banner" in response.text
    assert "Kill-Switch" in response.text


def test_ui_analyze_option_renders_result(client, monkeypatch):
    from datetime import date, timedelta

    from app.engines.options.greeks import theoretical_price

    daily = _zigzag_daily()
    underlying_price = float(daily["close"].iloc[-1])
    expiry = (date.today() + timedelta(days=60)).isoformat()
    price = theoretical_price(underlying_price, 130.0, 60 / 365, 0.35, "call", 0.045)
    canned_json = (
        '{"symbol": "NVDA", "option_type": "call", "strike": 130.0, "expiry": "%s", '
        '"contract_price": %.2f, "extraction_confidence": "high", "raw_notes": null}'
    ) % (expiry, price)

    monkeypatch.setattr("app.core.orchestrator.get_llm_adapter", lambda: FakeVisionAdapter(canned_json))
    monkeypatch.setattr("app.core.orchestrator.get_market_data_provider", lambda: FakeMarketDataAdapter(daily))

    response = client.post(
        "/ui/analyze-option", files={"file": ("contract.png", b"fake-png-bytes", "image/png")}
    )
    assert response.status_code == 200
    assert "NVDA" in response.text
    assert "Call" in response.text
    assert "tab-option active" in response.text or 'id="tab-option" class="tab-panel active"' in response.text


def test_ui_analyze_option_shows_error_for_bad_content_type(client):
    response = client.post(
        "/ui/analyze-option", files={"file": ("contract.txt", b"not an image", "text/plain")}
    )
    assert response.status_code == 200
    assert "error-banner" in response.text
    assert "نوع صورة غير مدعوم" in response.text


def test_ui_analyze_option_shows_error_for_bad_vision_json(client, monkeypatch):
    monkeypatch.setattr("app.core.orchestrator.get_llm_adapter", lambda: FakeVisionAdapter("not json"))
    response = client.post(
        "/ui/analyze-option", files={"file": ("contract.png", b"fake-png-bytes", "image/png")}
    )
    assert response.status_code == 200
    assert "error-banner" in response.text
    assert "تعذّر قراءة بيانات العقد" in response.text
