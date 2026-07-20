"""APScheduler wiring for the self-audit job (SRS 15, M3) — daily at 01:00 UTC (SRS 4.2).

Kept separate from `audit/self_audit.py` so the audit logic itself has zero dependency
on how/when it's triggered (the same function is called manually in tests).
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.audit.self_audit import run_self_audit_once
from app.db.session import SessionLocal

logger = logging.getLogger(__name__)

_scheduler: BackgroundScheduler | None = None


def _run_self_audit_job() -> None:
    db = SessionLocal()
    try:
        summary = run_self_audit_once(db)
        logger.info("self-audit run complete: %s", summary)
    except Exception:
        logger.exception("self-audit scheduled run failed")
    finally:
        db.close()


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(
        _run_self_audit_job, CronTrigger(hour=1, minute=0, timezone="UTC"), id="self_audit_daily"
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("self-audit scheduler started (daily at 01:00 UTC)")
    return scheduler


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
