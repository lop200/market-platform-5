from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app


def test_health_endpoint_reports_configured_providers():
    client = TestClient(app)
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    settings = get_settings()
    assert body["status"] == "ok"
    assert body["market_data_provider"] == settings.market_data_provider
    assert body["llm_provider"] == settings.llm_provider
