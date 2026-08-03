from __future__ import annotations

from types import SimpleNamespace

from app.api.routes_opportunities import _scan_breakdown
from app.db.models import StockCandidate, StockScanRun
from app.opportunities.scanner import _demote_rejected_candidate


def _review(**values):
    base = {
        "reasons_ar": ["السيولة الليلية لا تكفي لخطة منضبطة"],
        "warnings_ar": ["انتظار افتتاح الجلسة الرسمية"],
        "analysis_summary_ar": "المعطيات غير كافية.",
    }
    return SimpleNamespace(**{**base, **values})


def test_rejected_candidate_keeps_the_reviewers_reason():
    row = StockCandidate(
        symbol="NVDA", accepted=True, numeric_score=80,
        exclusion_reasons=[], snapshot_json={"stage": "candidate", "price": 197.0},
    )
    _demote_rejected_candidate(row, _review())
    assert row.accepted is False
    assert row.snapshot_json["stage"] == "analyzed"
    # The verdict leads; the reviewer's read follows as context. A live scan
    # showed a positive-sounding analysis printed beside a refusal.
    assert row.snapshot_json["watch_reason"].startswith("لم تعتمد المراجعة الذكية الدخول")
    assert "السيولة الليلية لا تكفي لخطة منضبطة" in row.snapshot_json["watch_reason"]
    assert row.snapshot_json["price"] == 197.0
    assert row.exclusion_reasons == ["لم تعتمد المراجعة الذكية الدخول"]


def test_rejection_without_reasons_still_explains_itself():
    row = StockCandidate(
        symbol="MSFT", accepted=True, numeric_score=70,
        exclusion_reasons=[], snapshot_json={"stage": "candidate"},
    )
    _demote_rejected_candidate(row, _review(reasons_ar=[], warnings_ar=[]))
    assert row.snapshot_json["watch_reason"].startswith("لم تعتمد المراجعة الذكية الدخول")
    assert row.snapshot_json["activation_condition"]


def test_demoting_a_missing_row_is_a_no_op():
    _demote_rejected_candidate(None, _review())


def test_rejected_candidate_reaches_the_watchlist(db_session):
    run = StockScanRun(status="completed", symbols_total=1, symbols_scanned=1, openai_calls=1)
    db_session.add(run)
    db_session.flush()
    row = StockCandidate(
        scan_run_id=run.id, symbol="NVDA", accepted=True, numeric_score=80,
        exclusion_reasons=[], snapshot_json={"stage": "candidate", "price": 197.0},
    )
    _demote_rejected_candidate(row, _review())
    db_session.add(row)
    db_session.commit()

    breakdown, watchlist = _scan_breakdown(db_session, run)
    assert breakdown["candidates"] == 0
    assert [item["symbol"] for item in watchlist] == ["NVDA"]
    assert watchlist[0]["reason"].startswith("لم تعتمد المراجعة الذكية الدخول")
