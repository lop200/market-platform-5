"""GET /cost/today, GET /cost/month, POST /cost/limits (SRS section 8, 16)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import require_api_key
from app.core.cost_gate import CostGate
from app.db.session import get_db

router = APIRouter(prefix="/api/v1/cost", tags=["cost"], dependencies=[Depends(require_api_key)])


class UpdateLimitsRequest(BaseModel):
    daily_cap_usd: float | None = None
    monthly_cap_usd: float | None = None
    kill_switch_on: bool | None = None


@router.get("/today")
def get_today_spend(db: Session = Depends(get_db)) -> dict:
    return CostGate(db).spend_summary("today")


@router.get("/month")
def get_month_spend(db: Session = Depends(get_db)) -> dict:
    return CostGate(db).spend_summary("month")


@router.post("/limits")
def update_limits(body: UpdateLimitsRequest, db: Session = Depends(get_db)) -> dict:
    gate = CostGate(db)
    if body.kill_switch_on is not None:
        if body.kill_switch_on:
            gate.enable_kill_switch()
        else:
            gate.disable_kill_switch()
    if body.daily_cap_usd is not None or body.monthly_cap_usd is not None:
        gate.update_caps(body.daily_cap_usd, body.monthly_cap_usd)
    limits = gate.get_limits()
    return {
        "daily_cap_usd": float(limits.daily_cap_usd),
        "monthly_cap_usd": float(limits.monthly_cap_usd),
        "kill_switch_on": limits.kill_switch_on,
    }
