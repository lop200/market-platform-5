"""Small persistence helpers shared by the cache and cost gates."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import CacheEntry, CostLedger, CostLimits


def get_or_create_cost_limits(db: Session, daily: float, monthly: float) -> CostLimits:
    row = db.get(CostLimits, 1)
    if row is None:
        row = CostLimits(id=1, daily_cap_usd=daily, monthly_cap_usd=monthly, kill_switch_on=False)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def sum_cost_since(db: Session, since: datetime) -> float:
    value = db.scalar(select(func.coalesce(func.sum(CostLedger.estimated_cost), 0)).where(CostLedger.occurred_at >= since))
    return float(value or 0)


def count_calls_since(db: Session, since: datetime) -> int:
    return int(db.scalar(select(func.count(CostLedger.id)).where(CostLedger.occurred_at >= since)) or 0)


def set_kill_switch(db: Session, enabled: bool) -> CostLimits:
    row = db.get(CostLimits, 1)
    if row is None:
        row = CostLimits(id=1, daily_cap_usd=1, monthly_cap_usd=20)
    row.kill_switch_on = enabled
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_cost_caps(db: Session, daily: float | None, monthly: float | None) -> CostLimits:
    row = db.get(CostLimits, 1)
    if row is None:
        row = CostLimits(id=1, daily_cap_usd=daily or 1, monthly_cap_usd=monthly or 20)
    if daily is not None:
        row.daily_cap_usd = daily
    if monthly is not None:
        row.monthly_cap_usd = monthly
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def record_estimated_cost(db: Session, **values) -> CostLedger:
    row = CostLedger(**values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def record_actual_cost(db: Session, ledger_id: int, actual_cost: float) -> CostLedger:
    row = db.get(CostLedger, ledger_id)
    if row is None:
        raise ValueError("cost ledger entry not found")
    row.actual_cost = actual_cost
    db.commit()
    db.refresh(row)
    return row


def cache_get(
    db: Session, key: str, *, now: datetime | None = None
) -> dict | None:
    row = db.get(CacheEntry, key)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if row is None:
        return None
    expiry = row.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= current:
        db.delete(row)
        db.commit()
        return None
    return row.value


def cache_get_any(db: Session, key: str) -> dict | None:
    """Return a saved value even after expiry for resilient stale fallbacks."""
    row = db.get(CacheEntry, key)
    return row.value if row is not None else None


def cache_set(db: Session, key: str, value: dict, expires_at: datetime) -> None:
    row = db.get(CacheEntry, key) or CacheEntry(key=key)
    row.value = value
    row.expires_at = expires_at
    db.add(row)
    db.commit()


def cache_delete(db: Session, key: str) -> None:
    row = db.get(CacheEntry, key)
    if row:
        db.delete(row)
        db.commit()


def cache_purge_expired(db: Session) -> int:
    result = db.execute(delete(CacheEntry).where(CacheEntry.expires_at <= datetime.now(timezone.utc)))
    db.commit()
    return int(result.rowcount or 0)
