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
    assert "مسح السوق واستخراج الفرص" in html
    assert "OPENAI_API_KEY" not in html


def test_health_reports_openai_only():
    body = TestClient(app).get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["ai_provider"] == "openai"


def test_runtime_contains_no_removed_derivatives_or_legacy_ai():
    forbidden = ("anthropic", "claude", "0dte", "greeks", "/options")
    for path in (ROOT / "app").rglob("*"):
        if path.is_file() and path.suffix in {".py", ".html"}:
            content = path.read_text(encoding="utf-8").lower()
            assert not any(term in content for term in forbidden), path


def test_no_removed_route_is_registered():
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    assert not any("option" in path.lower() for path in paths)
