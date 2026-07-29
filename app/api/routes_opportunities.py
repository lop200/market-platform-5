from __future__ import annotations

import re
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import OpportunityAudit, StockCandidate, StockOpportunity, StockScanRun, UserRiskSettings
from app.db.session import get_db
from app.opportunities.jobs import create_scan, create_symbol_analysis
from app.opportunities.scanner import expire_old_opportunities
from app.opportunities.schemas import OpportunityStatus, RiskSettingsInput, ScanStartResponse

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
        "api_requests": run.api_requests, "cache_hits": run.cache_hits,
        "openai_calls": run.openai_calls,
        "openai_cost_usd": float(run.openai_cost_usd or 0),
        "response_ms": run.response_ms,
    }


def _scan_breakdown(db: Session, run: StockScanRun) -> tuple[dict, list[dict]]:
    rows = db.scalars(
        select(StockCandidate)
        .where(StockCandidate.scan_run_id == run.id)
        .order_by(StockCandidate.numeric_score.desc())
    ).all()
    counts = {
        "universe_total": run.symbols_total,
        "data_fetched": 0,
        "data_failed": 0,
        "skipped": 0,
        "technically_rejected": 0,
        "candidates": 0,
        "sent_to_openai": int(run.openai_calls or 0),
        "final_opportunities": 0,
    }
    reasons: dict[str, int] = defaultdict(int)
    watchlist: list[dict] = []
    for row in rows:
        snapshot = row.snapshot_json or {}
        stage = snapshot.get("stage")
        if stage == "failed":
            counts["data_failed"] += 1
        elif stage == "skipped":
            counts["skipped"] += 1
        else:
            counts["data_fetched"] += 1
            if row.accepted:
                counts["candidates"] += 1
            else:
                counts["technically_rejected"] += 1
        for reason in row.exclusion_reasons or []:
            reasons[reason] += 1
        if stage == "analyzed" and len(watchlist) < 10:
            watchlist.append({
                "symbol": row.symbol,
                "score": float(row.numeric_score or 0),
                "price": snapshot.get("price"),
                "change_pct": snapshot.get("change_pct"),
                "trend": snapshot.get("trend") or "غير مكتمل",
                "liquidity": snapshot.get("liquidity"),
                "volatility": snapshot.get("volatility"),
                "support": snapshot.get("support"),
                "resistance": snapshot.get("resistance"),
                "reason": snapshot.get("watch_reason") or (
                    row.exclusion_reasons[0] if row.exclusion_reasons else "لم تكتمل الإشارة"
                ),
                "activation_condition": snapshot.get("activation_condition")
                or "انتظار اكتمال شروط الاستراتيجية",
            })
    counts["final_opportunities"] = db.scalar(
        select(func.count(StockOpportunity.id)).where(StockOpportunity.scan_run_id == run.id)
    ) or 0
    counts["accounted_total"] = counts["data_fetched"] + counts["data_failed"] + counts["skipped"]
    counts["invariant_ok"] = counts["accounted_total"] == counts["universe_total"]
    counts["exclusion_reasons"] = [
        {"reason": reason, "count": count}
        for reason, count in sorted(reasons.items(), key=lambda item: item[1], reverse=True)
    ]
    return counts, watchlist


def _opportunity_payload(row: StockOpportunity) -> dict:
    return {**row.result_json, "opportunity_id": str(row.id)}


def build_results_summary(db: Session) -> dict:
    total = db.scalar(select(func.count(StockOpportunity.id))) or 0
    audited = db.scalar(select(func.count(OpportunityAudit.id))) or 0
    entered = db.scalar(select(func.count(OpportunityAudit.id)).where(OpportunityAudit.entry_triggered_at.is_not(None))) or 0
    target1 = db.scalar(select(func.count(OpportunityAudit.id)).where(OpportunityAudit.target_1_hit.is_(True))) or 0
    target2 = db.scalar(select(func.count(OpportunityAudit.id)).where(OpportunityAudit.target_2_hit.is_(True))) or 0
    stopped = db.scalar(select(func.count(OpportunityAudit.id)).where(OpportunityAudit.stop_hit.is_(True))) or 0
    average_rr = db.scalar(select(func.avg(StockOpportunity.risk_reward))) or 0

    strategy_rows = db.execute(
        select(StockOpportunity.strategy_id, func.count(StockOpportunity.id))
        .group_by(StockOpportunity.strategy_id)
        .order_by(func.count(StockOpportunity.id).desc())
    ).all()
    regime_rows = db.execute(
        select(StockOpportunity.market_regime, func.count(StockOpportunity.id))
        .group_by(StockOpportunity.market_regime)
        .order_by(func.count(StockOpportunity.id).desc())
    ).all()
    session_counts: dict[str, int] = defaultdict(int)
    for issued_at in db.scalars(select(StockOpportunity.issued_at)).all():
        aware = issued_at.replace(tzinfo=timezone.utc) if issued_at.tzinfo is None else issued_at
        eastern = aware.astimezone(ZoneInfo("America/New_York"))
        minutes = eastern.hour * 60 + eastern.minute
        session = (
            "قبل السوق" if minutes < 570 else
            "الافتتاح" if minutes < 600 else
            "منتصف الجلسة" if minutes < 900 else
            "الإغلاق" if minutes <= 960 else
            "بعد الإغلاق"
        )
        session_counts[session] += 1

    def pct(value: int) -> float:
        return round(value / audited * 100, 1) if audited else 0

    return {
        "opportunities": total,
        "audited_results": audited,
        "entry_trigger_rate": pct(entered),
        "target_1_rate": pct(target1),
        "target_2_rate": pct(target2),
        "stop_rate": pct(stopped),
        "average_risk_reward": round(float(average_rr), 2),
        "by_strategy": [{"name": name, "count": count} for name, count in strategy_rows],
        "by_market_regime": [{"name": name, "count": count} for name, count in regime_rows],
        "by_session": [{"name": name, "count": count} for name, count in session_counts.items()],
        "note_ar": "هذه نتائج مسجلة فعليًا وليست Backtest أو ضمانًا للأداء.",
    }


@router.post("/scans", response_model=ScanStartResponse, dependencies=[Depends(rate_limit)])
def start_scan(
    all_prices: bool = Query(default=True),
    min_price: float | None = Query(default=None, ge=0),
    max_price: float | None = Query(default=None, gt=0),
    universe_limit: int = Query(default=1000, ge=1, le=5000),
) -> ScanStartResponse:
    if not all_prices and min_price is not None and max_price is not None and min_price > max_price:
        raise HTTPException(422, "الحد الأدنى للسعر يجب ألا يتجاوز الحد الأعلى")
    run = create_scan(
        min_price=None if all_prices else min_price,
        max_price=None if all_prices else max_price,
        universe_limit=universe_limit,
    )
    return ScanStartResponse(run_id=str(run.id), status=run.status, message_ar="بدأ مسح السوق في الخلفية")


@router.post("/symbols/{symbol}", response_model=ScanStartResponse, dependencies=[Depends(rate_limit)])
def analyze_symbol(symbol: str) -> ScanStartResponse:
    symbol = symbol.upper().strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
        raise HTTPException(422, "رمز السهم غير صالح")
    run = create_symbol_analysis(symbol)
    return ScanStartResponse(run_id=str(run.id), status=run.status, message_ar=f"بدأ تحليل {symbol} في الخلفية")


@router.post("/{opportunity_id}/refresh", response_model=ScanStartResponse, dependencies=[Depends(rate_limit)])
def refresh_opportunity(opportunity_id: str, db: Session = Depends(get_db)) -> ScanStartResponse:
    try:
        opportunity_uuid = uuid.UUID(opportunity_id)
    except ValueError:
        raise HTTPException(422, "معرف التحليل غير صالح")
    row = db.get(StockOpportunity, opportunity_uuid)
    if not row:
        raise HTTPException(404, "التحليل غير موجود")
    run = create_symbol_analysis(row.symbol)
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
    payload["breakdown"], payload["watchlist"] = _scan_breakdown(db, run)
    payload["opportunities"] = [_opportunity_payload(item) for item in opportunities]
    if run.status == "completed" and not opportunities:
        payload["message_ar"] = "لا توجد نتائج مستوفية للشروط حاليًا"
    return payload


@router.get("/stocks/jobs/{run_id}")
def symbol_analysis_status(run_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        run_uuid = uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(422, "معرف المهمة غير صالح")
    run = db.get(StockScanRun, run_uuid)
    if not run:
        raise HTTPException(404, "مهمة التحليل غير موجودة")
    payload = _run_payload(run)
    candidate = db.scalar(
        select(StockCandidate).where(StockCandidate.scan_run_id == run.id).limit(1)
    )
    payload["analysis"] = candidate.snapshot_json if candidate else None
    return payload


@router.get("/latest")
def latest(db: Session = Depends(get_db)) -> dict:
    expire_old_opportunities(db)
    run = db.scalar(
        select(StockScanRun)
        .where(StockScanRun.task_type == "market_scan")
        .order_by(StockScanRun.created_at.desc())
        .limit(1)
    )
    rows = db.scalars(
        select(StockOpportunity)
        .where(StockOpportunity.status != OpportunityStatus.EXPIRED.value)
        .order_by(StockOpportunity.issued_at.desc())
        .limit(get_settings().max_results)
    ).all()
    breakdown, watchlist = _scan_breakdown(db, run) if run else ({}, [])
    return {
        "scan": _run_payload(run) if run else None,
        "breakdown": breakdown,
        "watchlist": watchlist,
        "opportunities": [_opportunity_payload(row) for row in rows],
        "message_ar": "لا توجد نتائج مستوفية للشروط حاليًا" if not rows else None,
    }


@router.get("/results/summary")
def results_summary(db: Session = Depends(get_db)) -> dict:
    return build_results_summary(db)


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
