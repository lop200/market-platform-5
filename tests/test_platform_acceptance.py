from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parents[1]


def test_page_is_fast_shell_and_mobile_rtl():
    response = TestClient(app).get("/")
    assert response.status_code == 200
    html = response.text
    assert 'dir="rtl"' in html
    assert 'name="viewport"' in html
    assert "@media(min-width:720px)" in html
    assert "من الإشارة إلى القرار" in html
    assert "symbolCatalog" in html
    assert "NVIDIA" in html
    assert 'aria-autocomplete="list"' in html
    assert "صائد عقود الشركات" in html
    assert "حالة السوق" in html
    assert "OPENAI_API_KEY" not in html


def test_health_reports_openai_only():
    body = TestClient(app).get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["ai_provider"] == "openai"


def test_runtime_contains_no_legacy_ai_or_live_execution():
    forbidden = ("anthropic", "claude", "automated_live_execution")
    for path in (ROOT / "app").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".html"}:
            content = path.read_text(encoding="utf-8").lower()
            assert not any(term in content for term in forbidden), path


def test_options_are_feature_gated_and_dashboard_route_is_registered():
    paths = set(app.openapi()["paths"])
    assert "/api/v1/dashboard" in paths
    assert "/api/v1/opportunities/symbols/{symbol}" in paths
    assert "OPTIONS_ENABLED" in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_results_page_is_arabic_rtl_and_factual():
    response = TestClient(app).get("/results")
    assert response.status_code == 200
    assert 'dir="rtl"' in response.text
    assert "النتائج المسجلة فعليًا" in response.text
    assert "ليست Backtest" in response.text
