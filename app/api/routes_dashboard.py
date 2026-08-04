from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import NewsItem, StockCandidate, StockOpportunity, StockScanRun
from app.db.session import SessionLocal, get_db
from app.events.earnings import (
    calendar_stats,
    get_earnings_snapshot,
    refresh_earnings_cache,
)
from app.news.service import UnifiedNewsService
from app.options.market_clock import market_session, serialize_market_session

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="company-events")
_news_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="market-news")


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


def _watchlist_symbols(db: Session) -> set[str]:
    symbols = {
        str(item.get("symbol") or "").upper()
        for item in _latest_single_analyses(db)
        if item.get("symbol")
    }
    rows = db.scalars(
        select(StockOpportunity)
        .where(StockOpportunity.status != "expired")
        .order_by(StockOpportunity.issued_at.desc())
        .limit(20)
    ).all()
    symbols.update(
        str((row.result_json or {}).get("symbol") or "").upper()
        for row in rows
        if (row.result_json or {}).get("symbol")
    )
    return symbols


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
    earnings = get_earnings_snapshot(db)
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
        "market": serialize_market_session(clock),
        "feeds": {
            "stocks": settings.alpaca_feed if settings.market_data_provider == "alpaca" else settings.market_data_provider,
            "stocks_overnight": (
                settings.alpaca_overnight_feed
                if settings.market_data_provider == "alpaca" else None
            ),
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
        "earnings_meta": {
            "provider": "finnhub",
            "updated_at": earnings.get("updated_at"),
            "cache_seconds": settings.earnings_cache_seconds,
            "public_display_source": "Finnhub",
            "connection_status": earnings.get("connection_status"),
            "connection_label_ar": earnings.get("connection_label_ar"),
            "is_stale": earnings.get("is_stale", False),
            "last_success_at": earnings.get("last_success_at"),
            "last_error": earnings.get("last_error"),
            "cache_hit": earnings.get("cache_hit", False),
        },
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


@router.get("/earnings")
def earnings_snapshot(db: Session = Depends(get_db)) -> dict:
    """Return normalized cached Finnhub data only; never wait on Finnhub here."""
    payload = get_earnings_snapshot(db)
    watchlist = _watchlist_symbols(db)
    items = [
        {
            **item,
            "is_watchlist": str(item.get("symbol") or "").upper() in watchlist,
        }
        for item in payload.get("items", [])
    ]
    updated = payload.get("updated_at")
    age_seconds = None
    if updated:
        try:
            stamp = datetime.fromisoformat(str(updated).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age_seconds = max(
                0, int((datetime.now(timezone.utc) - stamp).total_seconds())
            )
        except (TypeError, ValueError):
            age_seconds = None
    return {
        "items": items,
        "stats": calendar_stats(items),
        "sectors": sorted(
            {str(item["sector"]) for item in items if item.get("sector")}
        ),
        "meta": {
            "source": "Finnhub",
            "updated_at": updated,
            "age_seconds": age_seconds,
            "connection_status": payload.get("connection_status"),
            "connection_label_ar": payload.get("connection_label_ar"),
            "is_stale": payload.get("is_stale", False),
            "last_success_at": payload.get("last_success_at"),
            "last_error": payload.get("last_error"),
            "cache_hit": payload.get("cache_hit", False),
        },
    }


def _refresh_earnings() -> None:
    db = SessionLocal()
    try:
        settings = get_settings()
        refresh_earnings_cache(
            db,
            settings,
            force=True,
            watchlist=_watchlist_symbols(db),
        )
    except Exception:
        db.rollback()
    finally:
        db.close()


@router.post("/refresh-events")
def refresh_events() -> dict:
    _executor.submit(_refresh_earnings)
    return {"status": "queued", "message_ar": "بدأ تحديث الأرباح في الخلفية"}


@router.get("/news")
def news_snapshot(db: Session = Depends(get_db)) -> dict:
    payload = UnifiedNewsService(db, get_settings()).market_snapshot()
    watchlist = _watchlist_symbols(db)
    items = list(payload.get("items", []))
    seen = {str(item.get("id")) for item in items}
    for analysis in _latest_single_analyses(db):
        symbol = str(analysis.get("symbol") or "").upper()
        for item in analysis.get("news", []):
            identifier = str(item.get("id") or "")
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            items.append({
                **item,
                "source_type": item.get("source_type") or "finnhub",
                "source_name": item.get("source") or "Finnhub",
                "symbols": [symbol],
                "market_scope": "company",
                "sentiment": item.get("impact") or "neutral",
                "is_official": bool(item.get("official")),
            })
    items.sort(
        key=lambda item: (
            item.get("impact_score") or -1,
            item.get("published_at", ""),
        ),
        reverse=True,
    )
    return {
        **payload,
        "items": [
            {
                **item,
                "is_watchlist": bool(
                    set(item.get("symbols") or []) & watchlist
                ),
            }
            for item in items[:100]
        ],
    }


@router.get("/spx")
def spx_news_context(db: Session = Depends(get_db)) -> dict:
    return UnifiedNewsService(db, get_settings()).spx_context()


def _refresh_news() -> None:
    db = SessionLocal()
    try:
        service = UnifiedNewsService(db, get_settings())
        service.ensure_default_sources()
        service.refresh_market(force=True)
    except Exception:
        db.rollback()
    finally:
        db.close()


@router.post("/refresh-news")
def refresh_news() -> dict:
    _news_executor.submit(_refresh_news)
    return {"status": "queued", "message_ar": "بدأ تحديث نبض السوق في الخلفية"}
