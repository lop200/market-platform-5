"""Site-wide access lock (Phase 5, owner request 2026-07-20).

Covers: disabled-by-default behavior, redirect-to-/lock when enabled, main-code entry
with the MANDATORY consent checkbox, wrong-code rejection, health-check exemption,
X-API-Key exemption for programmatic API calls, and per-person DB codes including
immediate revocation.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.db.session import Base, get_db
from app.main import app
from app.security import site_lock

MAIN_CODE = "main-secret-code"
API_KEY = "test-api-key-long-random"


@pytest.fixture
def locked_client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    settings = Settings(access_code_main=MAIN_CODE, api_key=API_KEY, database_url="sqlite://")
    monkeypatch.setattr("app.security.site_lock.get_settings", lambda: settings)
    monkeypatch.setattr("app.api.routes_lock.get_settings", lambda: settings)
    monkeypatch.setattr("app.security.site_lock.SessionLocal", factory)

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app, follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_db, None)


def _login(client, code=MAIN_CODE, consent="yes", username="lop"):
    data = {"username": username, "code": code}
    if consent:
        data["consent"] = consent
    return client.post("/lock", data=data)


def test_lock_disabled_by_default_home_is_open():
    client = TestClient(app)
    assert client.get("/").status_code == 200


def test_locked_home_redirects_to_lock_page(locked_client):
    response = locked_client.get("/")
    assert response.status_code == 303
    assert response.headers["location"] == "/lock"


def test_lock_page_shows_consent_and_code_form(locked_client):
    response = locked_client.get("/lock")
    assert response.status_code == 200
    assert 'name="consent"' in response.text
    assert 'name="username"' in response.text
    assert 'name="code"' in response.text


def test_wrong_code_rejected(locked_client):
    response = _login(locked_client, code="nope")
    assert response.status_code == 401


def test_wrong_username_rejected(locked_client):
    response = _login(locked_client, username="someone-else")
    assert response.status_code == 401


def test_missing_consent_rejected_even_with_correct_code(locked_client):
    response = _login(locked_client, consent=None)
    assert response.status_code == 400


def test_main_code_with_consent_unlocks(locked_client):
    response = _login(locked_client)
    assert response.status_code == 303
    assert site_lock.COOKIE_NAME in response.cookies
    cookie_header = response.headers["set-cookie"].lower()
    assert "max-age=" not in cookie_header
    assert "expires=" not in cookie_header
    home = locked_client.get("/")
    assert home.status_code == 200


def test_locked_redirect_is_never_cached(locked_client):
    response = locked_client.get("/")
    assert response.headers["cache-control"] == "no-store"


def test_logout_clears_session(locked_client):
    _login(locked_client)
    response = locked_client.post("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/lock"
    assert locked_client.get("/").status_code == 303


def test_tampered_cookie_rejected(locked_client):
    locked_client.cookies.set(site_lock.COOKIE_NAME, "main|deadbeef")
    assert locked_client.get("/").status_code == 303


def test_health_check_stays_open(locked_client):
    assert locked_client.get("/api/v1/health").status_code == 200


def test_api_with_valid_key_bypasses_lock(locked_client):
    response = locked_client.get("/api/v1/cost/today", headers={"X-API-Key": API_KEY})
    # Passes the middleware; the route's own dependency then re-validates the key.
    assert response.status_code != 401 or "site locked" not in response.text


def test_ui_route_without_cookie_gets_401_json(locked_client):
    response = locked_client.get("/ui/watchlist/list")
    assert response.status_code == 401
    assert "site locked" in response.json()["detail"]


def test_person_code_add_use_revoke(locked_client):
    _login(locked_client)  # main session for admin

    add = locked_client.post("/lock/admin/add", data={"label": "أبو فهد"})
    assert add.status_code == 200
    # The freshly generated code is shown exactly once on the admin page.
    import re

    match = re.search(r"<code>([^<]+)</code>", add.text)
    assert match, "new code not shown"
    person_code = match.group(1)

    person = TestClient(app, follow_redirects=False)
    response = person.post(
        "/lock", data={"username": "أبو فهد", "code": person_code, "consent": "yes"}
    )
    assert response.status_code == 303
    assert person.get("/").status_code == 200

    # Revoke -> the person's existing cookie stops working immediately.
    admin_page = locked_client.get("/lock/admin")
    match = re.search(r'name="code_id" value="([^"]+)"', admin_page.text)
    assert match, "code id not found on admin page"
    revoke = locked_client.post("/lock/admin/revoke", data={"code_id": match.group(1)})
    assert revoke.status_code == 303
    assert person.get("/").status_code == 303  # redirected back to /lock


def test_admin_requires_main_code(locked_client):
    response = locked_client.get("/lock/admin")
    assert response.status_code == 303
    assert response.headers["location"] == "/lock"
