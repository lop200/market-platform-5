from __future__ import annotations

import re
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import OpportunityAudit, StockCandidate, StockOpportunity, StockScanRun, UserRiskSettings
from app.db.session import get_db
from app.opportunities.jobs import create_scan
from app.opportunities.scanner import expire_old_opportunities
from app.opportunities.schemas import RiskSettingsInput, ScanStartResponse

router = APIRouter(prefix="/api/v1/opportunities", tags=["stock-opportunities"])
_requests: dict[str, deque[float]] = defaultdict(deque)


def rate_limit(request: Request) -> None:
    key = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _requests[key]
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    if len(bucket) >= 20:
        raise HTTPException(429, "تم تجاوز حد الطلبات؛ حاول بعد دقيقة")
    bucket.append(now)


def _run_payload(run: StockScanRun) -> dict:
    return {
        "run_id": str(run.id), "status": run.status, "progress_pct": run.progress_pct,
        "symbols_total": run.symbols_total, "symbols_scanned": run.symbols_scanned,
        "symbols_excluded": run.symbols_excluded, "provider": run.provider,
        "failure_reason": run.failure_reason, "started_at": run.started_at,
        "completed_at": run.completed_at, "created_at": run.created_at,
    }


@router.post("/scans", response_model=ScanStartResponse, dependencies=[Depends(rate_limit)])
def start_scan() -> ScanStartResponse:
    run = create_scan()
    return ScanStartResponse(run_id=str(run.id), status=run.status, message_ar="بدأ مسح السوق في الخلفية")


@router.post("/symbols/{symbol}", response_model=ScanStartResponse, dependencies=[Depends(rate_limit)])
def analyze_symbol(symbol: str) -> ScanStartResponse:
    symbol = symbol.upper().strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
        raise HTTPException(422, "رمز السهم غير صالح")
    run = create_scan(symbol)
    return ScanStartResponse(run_id=str(run.id), status=run.status, message_ar=f"بدأ تحليل {symbol} في الخلفية")


@router.post("/{opportunity_id}/refresh", response_model=ScanStartResponse, dependencies=[Depends(rate_limit)])
def refresh_opportunity(opportunity_id: str, db: Session = Depends(get_db)) -> ScanStartResponse:
    try:
        opportunity_uuid = uuid.UUID(opportunity_id)
    except ValueError:
        raise HTTPException(422, "معرف الفرصة غير صالح")
    row = db.get(StockOpportunity, opportunity_uuid)
    if not row:
        raise HTTPException(404, "الفرصة غير موجودة")
    run = create_scan(row.symbol)
    return ScanStartResponse(run_id=str(run.id), status=run.status, message_ar=f"بدأ تحديث {row.symbol} فقط")


@router.get("/scans/{run_id}")
def scan_status(run_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(422, "معرف المهمة غير صالح")
    run = db.get(StockScanRun, run_uuid)
    if not run:
        raise HTTPException(404, "مهمة المسح غير موجودة")
    opportunities = db.scalars(
        select(StockOpportunity).where(StockOpportunity.scan_run_id == run.id).order_by(StockOpportunity.overall_score.desc())
    ).all()
    payload = _run_payload(run)
    payload["opportunities"] = [item.result_json for item in opportunities]
    if run.status == "completed" and not opportunities:
        payload["message_ar"] = "لا توجد فرص مناسبة حاليًا"
    return payload


@router.get("/latest")
def latest(db: Session = Depends(get_db)) -> dict:
    expire_old_opportunities(db)
    run = db.scalar(select(StockScanRun).order_by(StockScanRun.created_at.desc()).limit(1))
    rows = db.scalars(
        select(StockOpportunity).order_by(StockOpportunity.issued_at.desc()).limit(get_settings().max_results)
    ).all()
    return {
        "scan": _run_payload(run) if run else None,
        "opportunities": [row.result_json for row in rows],
        "message_ar": "لا توجد فرص مناسبة حاليًا" if not rows else None,
    }


@router.get("/results/summary")
def results_summary(db: Session = Depends(get_db)) -> dict:
    total = db.scalar(select(func.count(StockOpportunity.id))) or 0
    audited = db.scalar(select(func.count(OpportunityAudit.id))) or 0
    entered = db.scalar(select(func.count(OpportunityAudit.id)).where(OpportunityAudit.entry_triggered_at.is_not(None))) or 0
    target1 = db.scalar(select(func.count(OpportunityAudit.id)).where(OpportunityAudit.target_1_hit.is_(True))) or 0
    target2 = db.scalar(select(func.count(OpportunityAudit.id)).where(OpportunityAudit.target_2_hit.is_(True))) or 0
    stopped = db.scalar(select(func.count(OpportunityAudit.id)).where(OpportunityAudit.stop_hit.is_(True))) or 0
    def pct(value: int) -> float:
        return round(value / audited * 100, 1) if audited else 0
    return {
        "opportunities": total, "audited_results": audited,
        "entry_trigger_rate": pct(entered), "target_1_rate": pct(target1),
        "target_2_rate": pct(target2), "stop_rate": pct(stopped),
        "note_ar": "هذه نتائج مسجلة فعليًا وليست Backtest أو ضمانًا للأداء.",
    }


@router.get("/risk-settings")
def get_risk_settings(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    row = db.get(UserRiskSettings, 1)
    if not row:
        row = UserRiskSettings(
            id=1, capital_sar=settings.default_capital_sar,
            max_risk_pct=settings.default_risk_pct,
            max_open_positions=settings.max_open_positions,
            daily_loss_limit_pct=settings.default_daily_loss_pct,
            currency="SAR",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    return {
        "capital_sar": float(row.capital_sar), "max_risk_pct": float(row.max_risk_pct),
        "max_open_positions": row.max_open_positions,
        "daily_loss_limit_pct": float(row.daily_loss_limit_pct), "currency": row.currency,
    }


@router.put("/risk-settings", dependencies=[Depends(rate_limit)])
def update_risk_settings(payload: RiskSettingsInput, db: Session = Depends(get_db)) -> dict:
    row = db.get(UserRiskSettings, 1) or UserRiskSettings(id=1)
    row.capital_sar = payload.capital_sar
    row.max_risk_pct = payload.max_risk_pct
    row.max_open_positions = payload.max_open_positions
    row.daily_loss_limit_pct = payload.daily_loss_limit_pct
    row.currency = payload.currency.upper()
    row.updated_at = datetime.now(timezone.utc)
    db.add(row)
    db.commit()
    return {"status": "saved"}
