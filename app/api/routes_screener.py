"""GET /api/v1/screener/{kind} — new feature (Screener), not in the original SRS milestones.

Deterministic-only (no LLM, CLAUDE.md rule 1); the cost-gate/cache/ranking flow lives in
`core/orchestrator.run_screener_scan` (shared with the web UI screener tabs).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.core.orchestrator import CostLimitExceededError, run_screener_scan, run_snipe_options_scan, run_snipe_scan
from app.db.session import get_db
from app.legal.disclaimers import SCREENER_DISCLAIMER_AR, SNIPE_DISCLAIMER_AR

router = APIRouter(prefix="/api/v1/screener", tags=["screener"], dependencies=[Depends(require_api_key)])


@router.get("/daily")
def screener_daily(db: Session = Depends(get_db)) -> dict:
    try:
        result = run_screener_scan(db, "daily_readiness")
    except CostLimitExceededError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.reason) from exc
    payload = result.model_dump(mode="json")
    payload["disclaimer"] = SCREENER_DISCLAIMER_AR
    return payload


@router.get("/weekly")
def screener_weekly(db: Session = Depends(get_db)) -> dict:
    try:
        result = run_screener_scan(db, "weekly_structure")
    except CostLimitExceededError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.reason) from exc
    payload = result.model_dump(mode="json")
    payload["disclaimer"] = SCREENER_DISCLAIMER_AR
    return payload


@router.get("/snipe/stocks")
def screener_snipe_stocks(db: Session = Depends(get_db)) -> dict:
    try:
        result = run_snipe_scan(db)
    except CostLimitExceededError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.reason) from exc
    payload = result.model_dump(mode="json")
    payload["disclaimer"] = SNIPE_DISCLAIMER_AR
    return payload


@router.get("/snipe/options")
def screener_snipe_options(db: Session = Depends(get_db)) -> dict:
    """Auto-picked contracts from the free yfinance chain (owner-approved interim source,
    2026-07-18) — the paid Tradier/Polygon decision (SRS 9.3) is still open, so
    `data_source_note` on the payload always discloses this is an unofficial, possibly
    delayed/thin data source, not the eventual production feed."""
    try:
        result = run_snipe_options_scan(db)
    except CostLimitExceededError as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, exc.reason) from exc
    payload = result.model_dump(mode="json")
    payload["disclaimer"] = SNIPE_DISCLAIMER_AR
    return payload


@router.get("/options")
def screener_options() -> dict:
    """No bulk options-chain provider is configured yet (SRS 9.3, pending owner's choice of
    Tradier/Polygon/etc. — same blocker as the per-stock options tab). Returns a clear,
    honest status instead of fabricating contract data."""
    return {
        "status": "not_configured",
        "message": (
            "ماسح عقود الأوبشن عالية الجاهزية يحتاج مزود بيانات أوبشن مؤجل (مثل Tradier أو "
            "Polygon) لم يُفعَّل بعد بموافقة المالك."
        ),
        "disclaimer": SCREENER_DISCLAIMER_AR,
    }
