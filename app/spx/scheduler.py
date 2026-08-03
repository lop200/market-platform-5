"""Bounded background sampling for the synthetic SPX intraday direction."""
from __future__ import annotations

import logging
from threading import Event, Lock, Thread

from app.config import get_settings
from app.db.session import SessionLocal
from app.options.market_clock import spx_options_session
from app.spx.schemas import StrikeMode
from app.spx.service import SPXHunterService

logger = logging.getLogger(__name__)
_stop = Event()
_cycle_lock = Lock()
_thread: Thread | None = None


def run_spx_sampling_cycle() -> int:
    """Save one fresh OPRA-derived observation without spending an AI call."""
    settings = get_settings()
    if not (
        settings.spx_background_refresh_enabled
        and settings.spx_enabled
        and settings.options_enabled
        and settings.spx_synthetic_enabled
        and settings.alpaca_api_key
        and spx_options_session(allow_global=settings.spx_global_trading_hours)
    ):
        return 0
    if not _cycle_lock.acquire(blocking=False):
        return 0
    db = SessionLocal()
    try:
        SPXHunterService(db, settings).refresh(
            StrikeMode.NEAR, allow_ai_review=False
        )
        return 1
    except Exception as exc:
        db.rollback()
        logger.warning("SPX sampling cycle failed: %s", type(exc).__name__)
        return 0
    finally:
        db.close()
        _cycle_lock.release()


def _loop() -> None:
    while True:
        settings = get_settings()
        if _stop.wait(settings.spx_background_refresh_seconds):
            return
        run_spx_sampling_cycle()


def start_spx_scheduler() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = Thread(target=_loop, name="spx-sampler", daemon=True)
    _thread.start()


def stop_spx_scheduler() -> None:
    _stop.set()
