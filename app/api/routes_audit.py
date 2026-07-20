"""GET /audit/summary, GET /audit/{symbol} (SRS section 8) — active in M3."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.db import repository
from app.db.session import get_db

router = APIRouter(prefix="/api/v1", tags=["audit"], dependencies=[Depends(require_api_key)])


@router.get("/audit/summary")
def audit_summary(db: Session = Depends(get_db)) -> list[dict]:
    stats = repository.get_accuracy_summary(db)
    return [
        {
            "regime": s.regime,
            "horizon_days": s.horizon_days,
            "total_audited": s.total_audited,
            "avg_outcome": float(s.avg_outcome) if s.avg_outcome is not None else None,
            "last_updated": s.last_updated.isoformat() if s.last_updated else None,
        }
        for s in stats
    ]


@router.get("/audit/{symbol}")
def audit_for_symbol(
    symbol: str, limit: int = Query(default=50, le=200), db: Session = Depends(get_db)
) -> list[dict]:
    pairs = repository.get_audit_results_for_symbol(db, symbol.strip().upper(), limit=limit)
    return [
        {
            "analysis_id": str(result.analysis_id),
            "symbol": sym,
            "checked_at": result.checked_at.isoformat(),
            "horizon_days": result.horizon_days,
            "price_at_check": float(result.price_at_check),
            "max_high_since": float(result.max_high_since),
            "min_low_since": float(result.min_low_since),
            "scenario_realized": result.scenario_realized,
            "levels_touched": result.levels_touched,
            "outcome_score": float(result.outcome_score) if result.outcome_score is not None else None,
            "notes": result.notes,
        }
        for result, sym in pairs
    ]
