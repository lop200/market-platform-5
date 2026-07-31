from __future__ import annotations

from fastapi.testclient import TestClient

from app.db.models import StockScanRun
from app.db.session import (
    database_backend,
    database_is_ephemeral,
    release_interrupted_runs,
)
from app.main import app


def test_health_reports_whether_results_survive_a_restart():
    body = TestClient(app).get("/api/v1/health").json()
    assert body["database"] == database_backend()
    assert body["results_survive_restart"] is not database_is_ephemeral()
    # Never leak the connection string, only the backend name.
    assert "://" not in body["database"]


def test_interrupted_runs_are_failed_not_left_running(db_session, monkeypatch):
    import app.db.session as session_module

    monkeypatch.setattr(session_module, "SessionLocal", lambda: db_session)
    monkeypatch.setattr(db_session, "close", lambda: None)

    running = StockScanRun(status="running", progress_pct=80)
    queued = StockScanRun(status="queued")
    done = StockScanRun(status="completed", progress_pct=100)
    db_session.add_all([running, queued, done])
    db_session.commit()

    assert release_interrupted_runs() == 2
    db_session.refresh(running)
    db_session.refresh(done)
    assert running.status == "failed"
    assert "إعادة تشغيل" in running.failure_reason
    # A finished run must not be touched.
    assert done.status == "completed"
