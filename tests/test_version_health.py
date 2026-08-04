from fastapi.testclient import TestClient

from app.main import APP_VERSION, app


def test_version_health_exposes_build_identity():
    response = TestClient(app).get("/health/version")
    assert response.status_code == 200
    payload = response.json()
    assert payload["git_sha"]
    assert payload["build_time"]
    assert payload["app_version"] == APP_VERSION
    assert payload["environment"]
