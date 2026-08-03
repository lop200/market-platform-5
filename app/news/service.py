from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import repository
from app.db.models import NewsItem, OpportunityEvent, StockOpportunity, TrustedNewsSource
from app.news.classification import MARKET_EVENTS, apply_safety, deduplicate
from app.news.providers import (
    FinnhubCompanyNewsProvider,
    SecEdgarNewsProvider,
    XTrustedNewsProvider,
)
from app.news.schemas import NewsEvent, NewsSnapshot, NewsUsage

logger = logging.getLogger(__name__)

DEFAULT_SOURCES = [
    ("sec", "SEC EDGAR", None, "sec.gov", "regulator", 100, True),
    ("x", "Federal Reserve", "federalreserve", "x.com", "central_bank", 100, True),
    ("x", "SEC", "secgov", "x.com", "regulator", 100, True),
    ("x", "NYSE", "nyse", "x.com", "exchange", 98, True),
    ("x", "Nasdaq", "nasdaq", "x.com", "exchange", 98, True),
    ("x", "Cboe", "cboe", "x.com", "exchange", 98, True),
    ("finnhub", "Finnhub Company News", None, "finnhub.io", "news_agency", 78, False),
]
SPX_HEAVY = {"AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "BRK.B", "AVGO", "JPM", "LLY"}


class UnifiedNewsService:
    def __init__(
        self, db: Session, settings: Settings, *, now: datetime | None = None
    ):
        self.db = db
        self.settings = settings
        self.now = now
        self.finnhub = FinnhubCompanyNewsProvider(settings)
        self.sec = SecEdgarNewsProvider(settings)
        self.x = XTrustedNewsProvider(settings)

    def ensure_default_sources(self) -> None:
        if self.db.scalar(select(TrustedNewsSource.id).limit(1)):
            return
        for source_type, name, username, domain, category, score, official in DEFAULT_SOURCES:
            self.db.add(TrustedNewsSource(
                source_type=source_type,
                source_name=name,
                username=username,
                domain=domain,
                category=category,
                reliability_score=score,
                is_official=official,
                is_enabled=True,
                notes="مصدر افتراضي موثوق للمنصة",
            ))
        self.db.commit()

    def _usage(self) -> NewsUsage:
        key = f"news:usage:{datetime.now(timezone.utc).date().isoformat()}"
        payload = repository.cache_get_any(self.db, key) or {}
        return NewsUsage.model_validate(payload)

    def _save_usage(self, usage: NewsUsage) -> None:
        key = f"news:usage:{datetime.now(timezone.utc).date().isoformat()}"
        repository.cache_set(
            self.db,
            key,
            usage.model_dump(mode="json"),
            datetime.now(timezone.utc) + timedelta(days=2),
        )

    def _events(self, payload: dict | None) -> list[NewsEvent]:
        return [NewsEvent.model_validate(item) for item in (payload or {}).get("items", [])]

    @staticmethod
    def _serialize(events: list[NewsEvent]) -> list[dict]:
        return [item.model_dump(mode="json", exclude_none=False) for item in events]

    def _store_selected(self, events: list[NewsEvent]) -> None:
        existing = {
            (row.symbol, row.headline, row.source)
            for row in self.db.scalars(
                select(NewsItem).order_by(NewsItem.published_at.desc()).limit(300)
            ).all()
        }
        for event in events:
            for symbol in event.symbols or ["MARKET"]:
                key = (symbol, event.headline, event.source_name)
                if key in existing:
                    continue
                self.db.add(NewsItem(
                    symbol=symbol,
                    headline=event.headline,
                    source=event.source_name,
                    published_at=event.published_at,
                    url=event.source_url,
                    classification=event.event_type,
                    is_official=event.is_official,
                    risk_flags=event.risk_flags,
                ))
                existing.add(key)
        self._invalidate_opportunities(events)
        self.db.commit()

    def _invalidate_opportunities(self, events: list[NewsEvent]) -> None:
        by_symbol = {}
        for event in events:
            if event.impact_score < 75:
                continue
            for symbol in event.symbols:
                by_symbol.setdefault(symbol, []).append(event)
        if not by_symbol:
            return
        rows = self.db.scalars(
            select(StockOpportunity).where(
                StockOpportunity.symbol.in_(by_symbol),
                StockOpportunity.status == "conditional_entry",
            )
        ).all()
        for row in rows:
            issued = row.issued_at
            if issued.tzinfo is None:
                issued = issued.replace(tzinfo=timezone.utc)
            newer = [
                item for item in by_symbol.get(row.symbol, [])
                if item.published_at > issued
            ]
            if not newer:
                continue
            strongest = max(newer, key=lambda item: item.impact_score)
            row.status = "needs_news_reanalysis"
            result = dict(row.result_json or {})
            result.update({
                "status": "needs_news_reanalysis",
                "status_ar": "يحتاج إعادة تحليل بسبب خبر جديد",
                "trade_plan": None,
                "news_invalidation_event_id": strongest.id,
            })
            row.result_json = result
            self.db.add(OpportunityEvent(
                opportunity_id=row.id,
                event_type="news_invalidation",
                occurred_at=strongest.published_at,
                details_json={
                    "news_event_id": strongest.id,
                    "event_type": strongest.event_type,
                    "impact_score": strongest.impact_score,
                    "source_name": strongest.source_name,
                },
            ))

    def get_symbol_news(
        self,
        symbol: str,
        *,
        force: bool = False,
        direction: str | None = None,
        analysis_issued_at: datetime | None = None,
    ) -> list[NewsEvent]:
        symbol = symbol.upper()
        key = f"news:symbol:{symbol}"
        stale_key = f"{key}:last_success"
        usage = self._usage()
        if not force:
            cached = repository.cache_get(self.db, key)
            if cached:
                usage.cache_hits += 1
                self._save_usage(usage)
                return [
                    apply_safety(item, analysis_direction=direction, analysis_issued_at=analysis_issued_at)
                    for item in self._events(cached)
                ]
        events: list[NewsEvent] = []
        status: dict[str, str] = {}
        try:
            events.extend(self.finnhub.company(symbol))
            usage.news_api_calls += 1
            status["finnhub"] = "connected"
        except Exception as exc:
            status["finnhub"] = f"unavailable:{type(exc).__name__}"
        try:
            if self.sec.enabled:
                events.extend(self.sec.company(symbol))
                usage.sec_requests += 2
                status["sec"] = "connected"
            else:
                status["sec"] = "disabled"
        except Exception as exc:
            status["sec"] = f"unavailable:{type(exc).__name__}"
        cleaned = [
            apply_safety(item, analysis_direction=direction, analysis_issued_at=analysis_issued_at)
            for item in deduplicate(events)
            if item.reliability_score >= 60
        ]
        now = datetime.now(timezone.utc)
        if cleaned:
            payload = NewsSnapshot(
                items=self._serialize(cleaned),
                updated_at=now,
                source_status=status,
                usage=usage.model_dump(),
            ).model_dump(mode="json")
            expiry = now + timedelta(seconds=max(300, min(900, self.settings.news_cache_seconds)))
            repository.cache_set(self.db, key, payload, expiry)
            repository.cache_set(self.db, stale_key, payload, now + timedelta(days=2))
            self._store_selected(cleaned)
        else:
            stale = repository.cache_get_any(self.db, stale_key)
            if stale:
                cleaned = self._events(stale)
        self._save_usage(usage)
        return cleaned

    def refresh_market(self, *, force: bool = False) -> dict:
        key = "news:market:pulse"
        stale_key = f"{key}:last_success"
        usage = self._usage()
        if not force:
            cached = repository.cache_get(self.db, key)
            if cached:
                usage.cache_hits += 1
                self._save_usage(usage)
                return {**cached, "cache_hit": True}
        events: list[NewsEvent] = []
        status: dict[str, str] = {}
        errors: list[str] = []
        try:
            events.extend(self.finnhub.market())
            usage.news_api_calls += 1
            status["finnhub"] = "connected"
        except Exception as exc:
            status["finnhub"] = "unavailable"
            errors.append(f"Finnhub:{type(exc).__name__}")
        try:
            if self.x.enabled and usage.x_reads < self.settings.x_daily_read_limit:
                posts = self.x.trusted_posts()
                remaining = self.settings.x_daily_read_limit - usage.x_reads
                accepted = posts[:remaining]
                events.extend(accepted)
                usage.x_reads += len(accepted)
                status["x"] = "connected"
            else:
                status["x"] = "disabled_or_budget"
        except Exception as exc:
            status["x"] = "unavailable"
            errors.append(f"X:{type(exc).__name__}")
        cleaned = [
            apply_safety(item)
            for item in deduplicate(events)
            if item.reliability_score >= 60
        ]
        now = datetime.now(timezone.utc)
        payload = NewsSnapshot(
            items=self._serialize(cleaned),
            updated_at=now,
            source_status=status,
            usage=usage.model_dump(),
            last_error="، ".join(errors) or None,
        ).model_dump(mode="json")
        if cleaned:
            repository.cache_set(
                self.db, key, payload,
                now + timedelta(seconds=max(300, min(900, self.settings.news_cache_seconds))),
            )
            repository.cache_set(self.db, stale_key, payload, now + timedelta(days=2))
            self._store_selected(cleaned)
        else:
            stale = repository.cache_get_any(self.db, stale_key)
            if stale:
                payload = {
                    **stale,
                    "is_stale": True,
                    "last_error": "، ".join(errors) or "تعذر تحديث الأخبار",
                }
        self._save_usage(usage)
        return payload

    def market_snapshot(self) -> dict:
        payload = repository.cache_get(
            self.db, "news:market:pulse", now=self.now
        )
        if payload:
            return payload
        stale = repository.cache_get_any(self.db, "news:market:pulse:last_success")
        if stale:
            return {**stale, "is_stale": True}
        return NewsSnapshot(source_status={"finnhub": "not_loaded", "sec": "not_loaded", "x": "disabled"}).model_dump(mode="json")

    def spx_context(self) -> dict:
        snapshot = self.market_snapshot()
        events = self._events(snapshot)
        relevant = [
            item for item in events
            if item.event_type in MARKET_EVENTS
            or bool(set(item.symbols) & SPX_HEAVY)
            or (item.market_scope == "market" and item.impact_score >= 75)
        ]
        rows = []
        for item in relevant[:20]:
            direction = (
                "داعم للصعود" if item.sentiment == "positive"
                else "داعم للهبوط" if item.sentiment == "negative"
                else "متضارب" if item.conflict_warning
                else "غير واضح"
            )
            rows.append({
                **item.model_dump(mode="json"),
                "spx_impact_score": min(100, item.impact_score + (8 if item.event_type in MARKET_EVENTS else 0)),
                "potential_direction_ar": direction,
                "impact_valid_minutes": 240 if item.event_type in MARKET_EVENTS else 90,
                "scheduled_in_seconds": None,
            })
        return {
            "items": rows,
            "updated_at": snapshot.get("updated_at"),
            "source_status": snapshot.get("source_status", {}),
            "usage": snapshot.get("usage", {}),
            "disclaimer_ar": "اتجاه الخبر تقديري وليس توقعًا مؤكدًا لحركة SPX.",
        }
