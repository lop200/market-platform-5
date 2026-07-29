from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock

from app.config import Settings, get_settings
from app.db.models import ProviderHealth, StockCandidate, StockScanRun
from app.db.session import SessionLocal
from app.opportunities.market_regime import classify_market
from app.opportunities.scanner import build_opportunity, persist_opportunity, scan_market
from app.providers.factory import get_market_data_provider

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stock-scan")
_lock = Lock()


def create_scan(symbol: str | None = None) -> StockScanRun:
    with _lock:
        db = SessionLocal()
        try:
            run = StockScanRun(status="queued", symbols_total=1 if symbol else 0)
            db.add(run)
            db.commit()
            db.refresh(run)
            run_id = run.id
        finally:
            db.close()
        _executor.submit(_execute, run_id, symbol)
    db = SessionLocal()
    try:
        return db.get(StockScanRun, run_id)
    finally:
        db.close()


def _execute(run_id, symbol: str | None) -> None:
    db = SessionLocal()
    try:
        run = db.get(StockScanRun, run_id)
        if run is None:
            return
        run.status = "running"
        run.started_at = datetime.now(timezone.utc)
        db.commit()
        provider = get_market_data_provider()
        health_started = time.perf_counter()
        run.provider = provider.provider_name
        db.commit()
        settings: Settings = get_settings()
        if symbol:
            regime, _ = classify_market(provider)
            result, reasons, snapshot = build_opportunity(db, provider, settings, symbol, regime, run.id)
            db.add(StockCandidate(
                scan_run_id=run.id, symbol=symbol, accepted=result is not None,
                numeric_score=result.overall_score if result else 0,
                exclusion_reasons=reasons, snapshot_json=snapshot,
            ))
            if result:
                persist_opportunity(db, result, run.id)
            run.symbols_scanned = 1
            run.symbols_excluded = 0 if result else 1
        else:
            scan_market(db, provider, settings, run)
        run.status = "completed"
        run.progress_pct = 100
        run.completed_at = datetime.now(timezone.utc)
        db.add(ProviderHealth(
            provider=provider.provider_name, service_type="market_data", healthy=True,
            latency_ms=int((time.perf_counter() - health_started) * 1000),
        ))
        db.commit()
    except Exception as exc:
        logger.exception("Background stock scan failed: %s", type(exc).__name__)
        db.rollback()
        run = db.get(StockScanRun, run_id)
        if run:
            run.status = "failed"
            run.failure_reason = f"تعذر مزود البيانات ({type(exc).__name__})"
            run.completed_at = datetime.now(timezone.utc)
            db.add(ProviderHealth(
                provider=run.provider or "unknown", service_type="market_data",
                healthy=False, failure_reason=f"{type(exc).__name__}",
            ))
            db.commit()
    finally:
        db.close()


def shutdown_jobs() -> None:
    _executor.shutdown(wait=False, cancel_futures=False)
