from __future__ import annotations

import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Lock

from app.config import Settings, get_settings
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.models import CacheEntry, OpenAICallLog, ProviderHealth, StockCandidate, StockScanRun
from app.db import repository
from app.db.session import SessionLocal
from app.opportunities.scanner import scan_market
from app.opportunities.openai_review import review_single_analysis
from app.opportunities.scoring import finalize_scorecard_with_options
from app.options.service import analyze_options_after_stock
from app.providers.factory import get_market_data_provider, get_option_data_provider
from app.events.earnings import (
    attach_stock_earnings_context,
    event_for_symbol,
    get_earnings_snapshot,
)
from app.stocks.analysis import analyze_single_stock

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="stock-scan")
_lock = Lock()


def _active_job(db, key: str) -> StockScanRun | None:
    row = db.get(CacheEntry, key)
    if row is None:
        return None
    expiry = row.expires_at
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry <= datetime.now(timezone.utc):
        db.delete(row)
        db.commit()
        return None
    try:
        run_id = uuid.UUID(str(row.value.get("run_id")))
        run = db.get(StockScanRun, run_id)
    except Exception:
        run = None
    if run is None or run.status in {"completed", "failed"}:
        db.delete(row)
        db.commit()
        return None
    return run


def _create_claimed_run(db, key: str, *, task_type: str, symbols_total: int) -> tuple[StockScanRun, bool]:
    active = _active_job(db, key)
    if active is not None:
        return active, False
    run = StockScanRun(status="queued", task_type=task_type, symbols_total=symbols_total)
    db.add(run)
    db.flush()
    db.add(
        CacheEntry(
            key=key,
            value={"run_id": str(run.id), "kind": "background_job_lock"},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
        )
    )
    try:
        db.commit()
        db.refresh(run)
        return run, True
    except IntegrityError:
        db.rollback()
        active = _active_job(db, key)
        if active is None:
            raise
        return active, False


def _release_job_key(db, key: str, run_id) -> None:
    row = db.get(CacheEntry, key)
    if row and str((row.value or {}).get("run_id")) == str(run_id):
        db.delete(row)
        db.commit()


def create_scan(
    min_price: float | None = None,
    max_price: float | None = None,
    universe_limit: int | None = None,
) -> StockScanRun:
    job_key = f"job:market-scan:{min_price}:{max_price}:{universe_limit}"
    with _lock:
        db = SessionLocal()
        try:
            run, created = _create_claimed_run(
                db, job_key, task_type="market_scan", symbols_total=0
            )
            run_id = run.id
        finally:
            db.close()
        if created:
            _executor.submit(
                _execute_scan, run_id, min_price, max_price, universe_limit, job_key
            )
    db = SessionLocal()
    try:
        return db.get(StockScanRun, run_id)
    finally:
        db.close()


def create_symbol_analysis(symbol: str, *, refresh: bool = False) -> StockScanRun:
    symbol = symbol.upper()
    job_key = f"job:single-symbol:{symbol}"
    with _lock:
        db = SessionLocal()
        try:
            run, created = _create_claimed_run(
                db, job_key, task_type="single_symbol", symbols_total=1
            )
            run_id = run.id
        finally:
            db.close()
        if created:
            _executor.submit(_execute_symbol, run_id, symbol, refresh, job_key)
    db = SessionLocal()
    try:
        return db.get(StockScanRun, run_id)
    finally:
        db.close()


def _prepare_run(db, run_id):
    run = db.get(StockScanRun, run_id)
    if run is None:
        return None, None, None, None
    run.status = "running"
    run.started_at = datetime.now(timezone.utc)
    provider = get_market_data_provider()
    run.provider = provider.provider_name
    db.commit()
    return run, provider, time.perf_counter(), provider.telemetry_snapshot()


def _complete_run(db, run, provider, health_started, telemetry_before) -> None:
    telemetry_after = provider.telemetry_snapshot()
    run.status = "completed"
    run.progress_pct = 100
    run.completed_at = datetime.now(timezone.utc)
    run.api_requests = telemetry_after["api_requests"] - telemetry_before["api_requests"]
    run.cache_hits = telemetry_after["cache_hits"] - telemetry_before["cache_hits"]
    run.response_ms = int((time.perf_counter() - health_started) * 1000)
    run.openai_calls = db.scalar(
        select(func.count(OpenAICallLog.id)).where(
            OpenAICallLog.run_id == str(run.id),
            OpenAICallLog.status.in_(("completed", "failed")),
        )
    ) or 0
    run.openai_cost_usd = db.scalar(
        select(func.coalesce(func.sum(OpenAICallLog.estimated_cost_usd), 0)).where(
            OpenAICallLog.run_id == str(run.id),
            OpenAICallLog.status.in_(("completed", "failed")),
        )
    ) or 0
    db.add(ProviderHealth(
        provider=provider.provider_name, service_type="market_data", healthy=True,
        latency_ms=int((time.perf_counter() - health_started) * 1000),
    ))
    db.commit()


def _fail_run(db, run_id, exc) -> None:
    logger.exception("Background stock task failed: %s", type(exc).__name__)
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


def _execute_scan(
    run_id,
    min_price: float | None,
    max_price: float | None,
    universe_limit: int | None,
    job_key: str | None = None,
) -> None:
    db = SessionLocal()
    try:
        run, provider, health_started, telemetry_before = _prepare_run(db, run_id)
        if run is None or provider is None:
            return
        settings: Settings = get_settings()
        scan_market(
            db, provider, settings, run,
            min_price=min_price,
            max_price=max_price,
            universe_limit=universe_limit,
        )
        _complete_run(db, run, provider, health_started, telemetry_before)
    except Exception as exc:
        _fail_run(db, run_id, exc)
    finally:
        if job_key:
            _release_job_key(db, job_key, run_id)
        db.close()


def _execute_symbol(
    run_id, symbol: str, refresh: bool = False, job_key: str | None = None
) -> None:
    db = SessionLocal()
    try:
        run, provider, health_started, telemetry_before = _prepare_run(db, run_id)
        if run is None or provider is None:
            return
        if refresh:
            provider.invalidate_symbol_cache(symbol)
        settings = get_settings()
        analysis = analyze_single_stock(db, provider, settings, symbol)
        earnings_cache = get_earnings_snapshot(db)
        analysis["earnings"] = attach_stock_earnings_context(
            analysis,
            event_for_symbol(earnings_cache, symbol),
        )
        if (
            _earnings_prevents_entry(analysis)
            and analysis.get("status") == "conditional_entry"
        ):
            analysis["status"] = "no_trade"
            analysis["status_ar"] = "ممنوع دخول جديد لقرب إعلان الأرباح"
            analysis["trade_plan"] = None
            analysis["valid_for_minutes"] = 0
            analysis["is_expired"] = True
            analysis["warnings"] = list(
                dict.fromkeys(
                    [
                        *(analysis.get("warnings") or []),
                        "مخاطر إعلان الأرباح تمنع الدخول الجديد حاليًا.",
                    ]
                )
            )
        option_provider = None
        if settings.options_enabled and analysis.get("status") == "conditional_entry":
            try:
                option_provider = get_option_data_provider()
            except Exception:
                option_provider = None
        analysis["options"] = analyze_options_after_stock(
            analysis, settings, option_provider
        ).model_dump(mode="json")
        analysis["scorecard"] = finalize_scorecard_with_options(
            analysis.get("scorecard") or {}, analysis["options"]
        )
        candidate = StockCandidate(
            scan_run_id=run.id,
            symbol=symbol,
            accepted=True,
            numeric_score=0,
            exclusion_reasons=[],
            snapshot_json=analysis,
        )
        db.add(candidate)
        run.symbols_scanned = 1
        run.symbols_excluded = 0
        run.status = "reviewing" if analysis["data_quality"]["valid_for_plan"] else "running"
        db.commit()
        analysis["ai_review"] = review_single_analysis(
            db, settings, analysis, run_id=str(run.id)
        )
        analysis["system_usage"]["openai_calls"] = analysis["ai_review"]["ai_calls"]
        analysis["system_usage"]["openai_cost_usd"] = analysis["ai_review"]["ai_cost_estimate"]
        candidate.snapshot_json = analysis
        db.add(candidate)
        db.commit()
        _complete_run(db, run, provider, health_started, telemetry_before)
    except Exception as exc:
        _fail_run(db, run_id, exc)
    finally:
        if job_key:
            _release_job_key(db, job_key, run_id)
        db.close()


def _earnings_prevents_entry(analysis: dict) -> bool:
    """Missing earnings data is normal and must never break stock analysis."""
    earnings = analysis.get("earnings") or {}
    return bool(earnings.get("prevent_new_entry"))


def shutdown_jobs() -> None:
    _executor.shutdown(wait=False, cancel_futures=False)
