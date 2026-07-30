from datetime import datetime, timedelta, timezone

from app.api.routes_opportunities import _scan_breakdown
from app.config import Settings
from app.db.models import StockCandidate, StockScanRun
from app.opportunities.scanner import _fresh_for_scan
from app.providers.base import Quote


def overnight_quote(*, quote_seconds: int, bar_seconds: int) -> Quote:
    now = datetime.now(timezone.utc)
    quote_time = (now - timedelta(seconds=quote_seconds)).isoformat()
    bar_time = (now - timedelta(seconds=bar_seconds)).isoformat()
    return Quote(
        symbol="GRAB",
        price=3.35,
        bid=3.33,
        ask=3.36,
        volume=100,
        as_of=quote_time,
        is_delayed=False,
        provider="alpaca",
        feed="boats",
        session="overnight",
        bid_as_of=quote_time,
        ask_as_of=quote_time,
        bar_as_of=bar_time,
    )


def test_overnight_scanner_excludes_stale_or_inactive_symbols():
    settings = Settings(max_quote_age_seconds=90, max_candle_age_seconds=180)
    assert _fresh_for_scan(
        overnight_quote(quote_seconds=5, bar_seconds=60), settings
    )
    assert not _fresh_for_scan(
        overnight_quote(quote_seconds=190, bar_seconds=60_000), settings
    )


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
