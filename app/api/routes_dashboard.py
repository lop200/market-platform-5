from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import repository
from app.db.models import NewsItem, StockCandidate, StockOpportunity, StockScanRun
from app.db.session import SessionLocal, get_db
from app.events.earnings import fetch_earnings_calendar
from app.options.market_clock import market_session

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="company-events")


def _latest_single_analyses(db: Session) -> list[dict]:
    runs = db.scalars(
        select(StockScanRun)
        .where(StockScanRun.task_type == "single_symbol", StockScanRun.status == "completed")
        .order_by(StockScanRun.completed_at.desc())
        .limit(8)
    ).all()
    results: list[dict] = []
    seen: set[str] = set()
    for run in runs:
        candidate = db.scalar(
            select(StockCandidate).where(StockCandidate.scan_run_id == run.id).limit(1)
        )
        snapshot = candidate.snapshot_json if candidate else None
        symbol = str((snapshot or {}).get("symbol") or "")
        if snapshot and symbol and symbol not in seen:
            seen.add(symbol)
            results.append(snapshot)
    return results


def _serialize_opportunity(row: StockOpportunity) -> dict:
    return {**(row.result_json or {}), "opportunity_id": str(row.id)}


@router.get("")
def dashboard_snapshot(db: Session = Depends(get_db)) -> dict:
    """Read only saved results so page rendering never waits on external services."""
    settings = get_settings()
    clock = market_session()
    opportunities = db.scalars(
        select(StockOpportunity)
        .where(StockOpportunity.status != "expired")
        .order_by(StockOpportunity.overall_score.desc(), StockOpportunity.issued_at.desc())
        .limit(12)
    ).all()
    analyses = _latest_single_analyses(db)
    news = db.scalars(select(NewsItem).order_by(NewsItem.published_at.desc()).limit(12)).all()
    earnings = repository.cache_get(db, "events:earnings:next14d") or {"items": []}
    near = [
        item for item in analyses
        if item.get("status") != "conditional_entry" and item.get("data_quality", {}).get("score", 0) >= 55
    ][:5]
    option_rows = [
        item.get("options") for item in analyses
        if (item.get("options") or {}).get("stock_first_gate_passed")
    ] + [
        (item.result_json or {}).get("options") for item in opportunities
        if ((item.result_json or {}).get("options") or {}).get("stock_first_gate_passed")
    ]
    calls = [
        item["best_call"] for item in option_rows if item and item.get("best_call")
    ]
    puts = [
        item["best_put"] for item in option_rows if item and item.get("best_put")
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market": {
            "session": clock.code,
            "label_ar": clock.label_ar,
            "options_open": clock.options_actionable,
            "new_york": clock.new_york_time.isoformat(),
            "riyadh": clock.riyadh_time.isoformat(),
        },
        "feeds": {
            "stocks": settings.alpaca_feed if settings.market_data_provider == "alpaca" else settings.market_data_provider,
            "options": settings.alpaca_options_feed if settings.options_enabled else "disabled",
        },
        "options_enabled": settings.options_enabled,
        "paper_trading_only": True,
        "opportunities": [_serialize_opportunity(item) for item in opportunities],
        "pre_market": [_serialize_opportunity(item) for item in opportunities if (item.result_json or {}).get("session") == "pre_market"],
        "regular": [_serialize_opportunity(item) for item in opportunities if (item.result_json or {}).get("session") in {"regular", "open", "mid_session", "close"}],
        "after_hours": [_serialize_opportunity(item) for item in opportunities if (item.result_json or {}).get("session") == "after_hours"],
        "near_opportunity": near,
        "best_stock": _serialize_opportunity(opportunities[0]) if opportunities else None,
        "best_call": max(calls, key=lambda item: item.get("ranking_score", 0), default=None),
        "best_put": max(puts, key=lambda item: item.get("ranking_score", 0), default=None),
        "analyses": analyses,
        "earnings": earnings.get("items", [])[:30],
        "news": [
            {
                "symbol": item.symbol,
                "headline": item.headline,
                "source": item.source,
                "published_at": item.published_at.isoformat(),
                "classification": item.classification,
                "risk_flags": item.risk_flags,
            }
            for item in news
        ],
    }


def _refresh_earnings() -> None:
    db = SessionLocal()
    try:
        settings = get_settings()
        items = fetch_earnings_calendar(settings)
        repository.cache_set(
            db,
            "events:earnings:next14d",
            {"items": items, "updated_at": datetime.now(timezone.utc).isoformat()},
            datetime.now(timezone.utc) + timedelta(seconds=settings.earnings_cache_seconds),
        )
    except Exception:
        db.rollback()
    finally:
        db.close()


@router.post("/refresh-events")
def refresh_events() -> dict:
    _executor.submit(_refresh_earnings)
    return {"status": "queued", "message_ar": "بدأ تحديث الأرباح في الخلفية"}
