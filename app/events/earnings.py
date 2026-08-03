from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.config import Settings
from app.db import repository
from app.events.finnhub import (
    FinnhubEarningsProvider,
    build_scenarios,
    calculate_remaining,
    earnings_risk,
)
from app.events.schemas import EarningsCalendarPayload

logger = logging.getLogger(__name__)

WEEK_CACHE_KEY = "events:earnings:finnhub:window"
TODAY_CACHE_KEY = "events:earnings:finnhub:today"
LAST_SUCCESS_KEY = "events:earnings:finnhub:last_success"
STATUS_CACHE_KEY = "events:earnings:finnhub:status"
LEGACY_CACHE_KEY = "events:earnings:next14d"


def _clean_error(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return "إعداد Finnhub غير مكتمل"
    name = type(exc).__name__
    if name in {"ConnectTimeout", "ReadTimeout", "TimeoutException"}:
        return "انتهت مهلة الاتصال بـFinnhub"
    return f"تعذر تحديث Finnhub ({name})"


def _serialize(events) -> list[dict]:
    return [
        event.model_dump(mode="json", exclude_none=False)
        for event in events
    ]


def fetch_earnings_calendar(
    settings: Settings,
    *,
    start: date | None = None,
    end: date | None = None,
    now: datetime | None = None,
    watchlist: set[str] | None = None,
) -> list[dict]:
    """Compatibility wrapper returning normalized data, never raw Finnhub JSON."""
    first = start or date.today()
    last = end or first + timedelta(days=14)
    provider = FinnhubEarningsProvider(settings)
    return _serialize(
        provider.fetch(start=first, end=last, now=now, watchlist=watchlist)
    )


def refresh_earnings_cache(
    db: Session,
    settings: Settings,
    *,
    force: bool = False,
    now: datetime | None = None,
    watchlist: set[str] | None = None,
    provider: FinnhubEarningsProvider | None = None,
) -> dict:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if not force:
        cached = repository.cache_get(db, WEEK_CACHE_KEY, now=current)
        if cached:
            logger.info("earnings cache_hit")
            return {**cached, "cache_hit": True}

    try:
        adapter = provider or FinnhubEarningsProvider(settings)
        enrichment_total = settings.earnings_enrichment_limit
        past_events = adapter.fetch(
            start=current.date() - timedelta(days=7),
            end=current.date(),
            now=current,
            watchlist=watchlist,
            enrichment_limit=min(8, enrichment_total),
        )
        future_events = adapter.fetch(
            start=current.date(),
            end=current.date() + timedelta(days=14),
            now=current,
            watchlist=watchlist,
            enrichment_limit=max(0, enrichment_total - min(8, enrichment_total)),
        )
        unique_events = {}
        for event in [*future_events, *past_events]:
            key = (event.symbol, event.earnings_date)
            existing = unique_events.get(key)
            if existing is None or (
                event.has_passed and not existing.has_passed
            ):
                unique_events[key] = event
        events = sorted(
            unique_events.values(),
            key=lambda item: (
                item.earnings_date,
                0 if item.is_watchlist else 1,
                -(item.market_cap or 0),
                item.symbol,
            ),
        )
        items = _serialize(events)
        expiry = current + timedelta(seconds=settings.earnings_cache_seconds)
        payload = EarningsCalendarPayload(
            items=items,
            updated_at=current,
            expires_at=expiry,
            connection_status="connected",
            connection_label_ar="متصل بـFinnhub",
            cache_hit=False,
            is_stale=False,
            last_success_at=current,
        ).model_dump(mode="json")
        today_items = [
            item for item in items
            if item.get("earnings_date") == current.astimezone(
                ZoneInfo("America/New_York")
            ).date().isoformat()
        ]
        repository.cache_set(db, WEEK_CACHE_KEY, payload, expiry)
        repository.cache_set(
            db,
            TODAY_CACHE_KEY,
            {
                "items": today_items,
                "updated_at": current.isoformat(),
                "source": "Finnhub",
            },
            current + timedelta(seconds=settings.earnings_today_cache_seconds),
        )
        repository.cache_set(
            db,
            LAST_SUCCESS_KEY,
            payload,
            current + timedelta(days=30),
        )
        repository.cache_set(
            db,
            STATUS_CACHE_KEY,
            {
                "connection_status": "connected",
                "last_success_at": current.isoformat(),
                "last_error": None,
            },
            current + timedelta(days=30),
        )
        # Keep the old consumer key synchronized until every caller is migrated.
        repository.cache_set(db, LEGACY_CACHE_KEY, payload, expiry)
        return payload
    except Exception as exc:
        db.rollback()
        message = _clean_error(exc)
        logger.warning("Finnhub earnings refresh failed: %s", type(exc).__name__)
        stale = repository.cache_get_any(db, LAST_SUCCESS_KEY)
        status = {
            "connection_status": "error",
            "connection_label_ar": "آخر بيانات محفوظة",
            "last_error": message,
            "last_success_at": (stale or {}).get("last_success_at"),
        }
        repository.cache_set(
            db,
            STATUS_CACHE_KEY,
            status,
            current + timedelta(days=30),
        )
        if stale:
            return {
                **stale,
                **status,
                "cache_hit": True,
                "is_stale": True,
            }
        return EarningsCalendarPayload(
            items=[],
            updated_at=None,
            expires_at=None,
            connection_status="error",
            connection_label_ar="تعذر الاتصال بـFinnhub",
            cache_hit=False,
            is_stale=False,
            last_success_at=None,
            last_error=message,
        ).model_dump(mode="json")


def get_earnings_snapshot(db: Session) -> dict:
    cached = repository.cache_get(db, WEEK_CACHE_KEY)
    if cached:
        logger.info("earnings cache_hit")
        return {**cached, "cache_hit": True}
    stale = repository.cache_get_any(db, LAST_SUCCESS_KEY)
    status = repository.cache_get_any(db, STATUS_CACHE_KEY) or {}
    if stale:
        return {
            **stale,
            **status,
            "cache_hit": True,
            "is_stale": True,
            "connection_label_ar": "آخر بيانات محفوظة",
        }
    return EarningsCalendarPayload(
        connection_status=status.get("connection_status", "not_loaded"),
        connection_label_ar=status.get(
            "connection_label_ar", "لم يتم تحديث Finnhub بعد"
        ),
        last_error=status.get("last_error"),
    ).model_dump(mode="json")


def event_for_symbol(payload: dict, symbol: str) -> dict | None:
    normalized = symbol.upper()
    candidates = [
        item for item in payload.get("items", [])
        if str(item.get("symbol") or "").upper() == normalized
    ]
    future = [item for item in candidates if not item.get("has_passed")]
    return (future or candidates or [None])[0]


def attach_stock_earnings_context(
    analysis: dict,
    event: dict | None,
    *,
    now: datetime | None = None,
) -> dict | None:
    if not event:
        return None
    current = now or datetime.now(timezone.utc)
    try:
        event_date = date.fromisoformat(str(event.get("earnings_date")))
    except (TypeError, ValueError):
        return None
    timing = calculate_remaining(
        event_date,
        event.get("earnings_hour"),
        now=current,
        results_available=(
            event.get("eps_actual") is not None
            or event.get("revenue_actual") is not None
        ),
    )
    risk = earnings_risk(timing)
    indicators = analysis.get("indicators") or {}
    plan = analysis.get("trade_plan") or {}
    scenarios = build_scenarios(
        support=indicators.get("support"),
        resistance=indicators.get("resistance"),
        stop=plan.get("stop"),
        targets=[
            item.get("price") for item in plan.get("targets", [])
            if item.get("price") is not None
        ],
    )
    return {
        **event,
        **timing,
        **risk,
        "scenarios": scenarios,
        "risk_message_ar": (
            "يجب منع الدخول الجديد لقرب إعلان الأرباح."
            if risk["prevent_new_entry"]
            else "الفرصة تحتاج تحذير أرباح وإدارة مخاطر إضافية."
            if risk["warning_required"]
            else "يسمح بتحليل فني عادي مع استمرار مراقبة موعد الأرباح."
        ),
        "scenario_disclaimer_ar": "هذه سيناريوهات تقديرية وليست توقعًا مؤكدًا.",
    }


def calendar_stats(items: list[dict]) -> dict[str, int]:
    return {
        "today": sum(bool(item.get("is_today")) for item in items),
        "tomorrow": sum(bool(item.get("is_tomorrow")) for item in items),
        "watchlist": sum(bool(item.get("is_watchlist")) for item in items),
        "before_market": sum(
            item.get("earnings_hour") == "before_market" for item in items
        ),
        "after_market": sum(
            item.get("earnings_hour") == "after_market" for item in items
        ),
        "reported": sum(bool(item.get("has_passed")) for item in items),
    }
