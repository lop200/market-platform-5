from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.config import get_settings
from app.db.session import SessionLocal, get_db
from app.db.models import CacheEntry
from app.spx.schemas import StrikeMode
from app.spx.service import SPXHunterService

router = APIRouter(prefix="/api/v1/spx", tags=["spx"])
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spx-hunter")


def _spx_job_key(strike_mode: StrikeMode) -> str:
    return f"job:spx:{strike_mode.value}"


def _claim_spx_job(strike_mode: StrikeMode) -> bool:
    db = SessionLocal()
    key = _spx_job_key(strike_mode)
    now = datetime.now(timezone.utc)
    try:
        row = db.get(CacheEntry, key)
        if row is not None:
            expiry = row.expires_at
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry > now:
                return False
            db.delete(row)
            db.commit()
        db.add(
            CacheEntry(
                key=key,
                value={"status": "running", "kind": "background_job_lock"},
                expires_at=now + timedelta(minutes=15),
            )
        )
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False
    finally:
        db.close()


def _release_spx_job(strike_mode: StrikeMode) -> None:
    db = SessionLocal()
    try:
        row = db.get(CacheEntry, _spx_job_key(strike_mode))
        if row:
            db.delete(row)
            db.commit()
    finally:
        db.close()


@router.get("")
def spx_snapshot(
    strike_mode: StrikeMode = Query(default=StrikeMode.NEAR),
    db: Session = Depends(get_db),
) -> dict:
    """Saved result only: page rendering never waits on external providers."""
    return SPXHunterService(db, get_settings()).snapshot(strike_mode)


def _refresh(strike_mode: StrikeMode) -> None:
    db = SessionLocal()
    try:
        # This path is triggered by the explicit "refresh and analyze" button.
        # Keep scheduled refreshes cost-bounded, but let a deliberate user action
        # request a fresh bounded review of the current deterministic snapshot.
        SPXHunterService(db, get_settings()).refresh(
            strike_mode, allow_ai_review=True
        )
    except Exception:
        db.rollback()
    finally:
        db.close()


def _refresh_and_release(strike_mode: StrikeMode) -> None:
    try:
        _refresh(strike_mode)
    finally:
        _release_spx_job(strike_mode)


@router.post("/refresh")
def refresh_spx(strike_mode: StrikeMode = Query(default=StrikeMode.NEAR)) -> dict:
    if not _claim_spx_job(strike_mode):
        return {
            "status": "already_queued",
            "strike_mode": strike_mode.value,
            "message_ar": "تحديث SPX مماثل قيد التنفيذ؛ لم تُنشأ مهمة أو تكلفة جديدة.",
        }
    _executor.submit(_refresh_and_release, strike_mode)
    return {
        "status": "queued",
        "strike_mode": strike_mode.value,
        "message_ar": "بدأ تحديث قنّاص SPX في الخلفية.",
    }
