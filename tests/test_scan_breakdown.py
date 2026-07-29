from app.api.routes_opportunities import _scan_breakdown
from app.db.models import StockCandidate, StockScanRun


def test_scan_breakdown_is_accounted_and_builds_watchlist(db_session):
    run = StockScanRun(
        status="completed", symbols_total=4, symbols_scanned=2,
        symbols_excluded=3, openai_calls=0,
    )
    db_session.add(run)
    db_session.flush()
    rows = [
        StockCandidate(
            scan_run_id=run.id, symbol="NVDA", accepted=False, numeric_score=55,
            exclusion_reasons=["لا توجد إشارة"],
            snapshot_json={
                "stage": "analyzed", "price": 180, "trend": "صاعد",
                "support": 175, "resistance": 185,
                "watch_reason": "لا توجد إشارة",
                "activation_condition": "اختراق المقاومة بحجم",
            },
        ),
        StockCandidate(
            scan_run_id=run.id, symbol="AAPL", accepted=True, numeric_score=80,
            exclusion_reasons=[], snapshot_json={"stage": "candidate"},
        ),
        StockCandidate(
            scan_run_id=run.id, symbol="MISS", accepted=False, numeric_score=0,
            exclusion_reasons=["تعذر جلب البيانات"], snapshot_json={"stage": "failed"},
        ),
        StockCandidate(
            scan_run_id=run.id, symbol="SKIP", accepted=False, numeric_score=0,
            exclusion_reasons=["خارج أفضل المرشحين"],
            snapshot_json={"stage": "skipped"},
        ),
    ]
    db_session.add_all(rows)
    db_session.commit()
    breakdown, watchlist = _scan_breakdown(db_session, run)
    assert breakdown["data_fetched"] == 2
    assert breakdown["data_failed"] == 1
    assert breakdown["skipped"] == 1
    assert breakdown["accounted_total"] == breakdown["universe_total"] == 4
    assert breakdown["invariant_ok"]
    assert watchlist[0]["symbol"] == "NVDA"


def test_home_defaults_to_all_prices_and_has_requested_presets():
    from fastapi.testclient import TestClient
    from app.main import app

    html = TestClient(app).get("/").text
    assert '<option value="all" selected>جميع الأسعار</option>' in html
    for label in ("أقل من 5$", "من 5$ إلى 20$", "من 20$ إلى 100$", "أكثر من 100$", "نطاق مخصص"):
        assert label in html
