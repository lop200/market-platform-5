from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.session import SessionLocal, get_db
from app.spx.schemas import StrikeMode
from app.spx.service import SPXHunterService

router = APIRouter(prefix="/api/v1/spx", tags=["spx"])
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="spx-hunter")


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
        SPXHunterService(db, get_settings()).refresh(strike_mode)
    except Exception:
        db.rollback()
    finally:
        db.close()


@router.post("/refresh")
def refresh_spx(strike_mode: StrikeMode = Query(default=StrikeMode.NEAR)) -> dict:
    _executor.submit(_refresh, strike_mode)
    return {
        "status": "queued",
        "strike_mode": strike_mode.value,
        "message_ar": "بدأ تحديث قنّاص SPX في الخلفية.",
    }
