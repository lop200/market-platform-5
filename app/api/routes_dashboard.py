"""Fast dashboard APIs backed only by saved results."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import (
    EarningsCalendarEvent, NewsItem, ProviderHealth, StockOpportunity, StockScanRun,
)
from app.db.session import SessionLocal, get_db
from app.earnings.service import EarningsEvent, get_earnings_provider
from app.markets.clock import market_clock

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _event_payload(row: EarningsCalendarEvent, now: datetime) -> dict:
    return EarningsEvent(
        symbol=row.symbol, company_name=row.company_name,
        announced_at=_aware(row.announced_at), timing=row.timing,
        eps_estimate=float(row.eps_estimate) if row.eps_estimate is not None else None,
        revenue_estimate=float(row.revenue_estimate) if row.revenue_estimate is not None else None,
        previous_eps=float(row.previous_eps) if row.previous_eps is not None else None,
        source=row.source, source_url=row.source_url,
    ).as_dict(now)


@router.get("")
def dashboard_snapshot(db: Session = Depends(get_db)) -> dict:
    """Return instantly from saved state; never call an external provider here."""
    now = datetime.now(timezone.utc)
    settings = get_settings()
    opportunities = db.scalars(
        select(StockOpportunity)
        .where(StockOpportunity.expires_at > now)
        .order_by(StockOpportunity.overall_score.desc())
        .limit(5)
    ).all()
    events = db.scalars(
        select(EarningsCalendarEvent)
        .where(
            EarningsCalendarEvent.announced_at >= now - timedelta(hours=12),
            EarningsCalendarEvent.announced_at <= now + timedelta(days=7),
        )
        .order_by(EarningsCalendarEvent.announced_at)
        .limit(20)
    ).all()
    news = db.scalars(
        select(NewsItem).order_by(NewsItem.published_at.desc()).limit(8)
    ).all()
    latest_run = db.scalar(select(StockScanRun).order_by(StockScanRun.created_at.desc()).limit(1))
    health = db.scalar(select(ProviderHealth).order_by(ProviderHealth.checked_at.desc()).limit(1))
    event_payloads = [_event_payload(row, now) for row in events]
    opportunity_payloads = [
        {**row.result_json, "opportunity_id": str(row.id)} for row in opportunities
    ]
    return {
        "market": market_clock(now).as_dict(),
        "connection": {
            "connected": bool(health.healthy) if health else False,
            "provider": settings.market_data_provider,
            "feed": settings.alpaca_feed,
            "paper_only": settings.paper_trading_only,
            "kill_switch": settings.trading_kill_switch,
        },
        "metrics": {
            "opportunities": len(opportunity_payloads),
            "earnings_today": sum(
                1 for item in event_payloads
                if datetime.fromisoformat(item["announced_at"]).date() == now.date()
            ),
            "impactful_news": sum(
                1 for item in news if item.classification in {"high_risk", "negative", "إيجابي", "سلبي"}
            ),
            "last_update": latest_run.completed_at.isoformat() if latest_run and latest_run.completed_at else None,
            "data_quality": "SIP" if settings.alpaca_feed.lower() == "sip" else settings.alpaca_feed.upper(),
        },
        "opportunities": opportunity_payloads,
        "earnings": event_payloads,
        "news": [{
            "symbol": row.symbol, "headline": row.headline, "source": row.source,
            "published_at": _aware(row.published_at).isoformat(), "url": row.url,
            "classification": row.classification, "official": row.is_official,
            "risk_flags": row.risk_flags,
        } for row in news],
        "system_usage": {
            "symbols_requested": latest_run.symbols_total if latest_run else 0,
            "market_data_api_calls": latest_run.api_requests if latest_run else 0,
            "cache_hits": latest_run.cache_hits if latest_run else 0,
            "openai_calls": latest_run.openai_calls if latest_run else 0,
            "response_time_ms": latest_run.response_ms if latest_run else 0,
        },
    }


def _refresh_earnings() -> None:
    settings = get_settings()
    provider = get_earnings_provider(settings)
    now = datetime.now(timezone.utc)
    events = provider.fetch(now - timedelta(days=1), now + timedelta(days=8))
    db = SessionLocal()
    try:
        for event in events:
            existing = db.scalar(select(EarningsCalendarEvent).where(
                EarningsCalendarEvent.symbol == event.symbol,
                EarningsCalendarEvent.announced_at == event.announced_at,
                EarningsCalendarEvent.source == event.source,
            ).limit(1))
            row = existing or EarningsCalendarEvent()
            row.symbol, row.company_name = event.symbol, event.company_name
            row.announced_at, row.timing = event.announced_at, event.timing
            row.eps_estimate, row.revenue_estimate = event.eps_estimate, event.revenue_estimate
            row.previous_eps, row.source = event.previous_eps, event.source
            row.source_url, row.verified = event.source_url, event.source != "manual"
            db.add(row)
        db.commit()
    finally:
        db.close()


@router.post("/earnings/refresh")
def refresh_earnings(background_tasks: BackgroundTasks) -> dict:
    background_tasks.add_task(_refresh_earnings)
    return {"status": "queued", "message_ar": "بدأ تحديث تقويم الأرباح في الخلفية"}


class ManualEventInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z][A-Z0-9.-]{0,9}$")
    company_name: str = Field(min_length=1, max_length=150)
    announced_at: datetime
    timing: str = Field(pattern=r"^(bmo|amc|unknown)$")
    eps_estimate: float | None = None
    revenue_estimate: float | None = None
    source_url: str | None = None


@router.post("/earnings/manual")
def add_manual_event(payload: ManualEventInput, db: Session = Depends(get_db)) -> dict:
    row = EarningsCalendarEvent(
        symbol=payload.symbol, company_name=payload.company_name,
        announced_at=payload.announced_at, timing=payload.timing,
        eps_estimate=payload.eps_estimate, revenue_estimate=payload.revenue_estimate,
        source="manual", source_url=payload.source_url, verified=False,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": str(row.id), "status": "saved_for_review"}
