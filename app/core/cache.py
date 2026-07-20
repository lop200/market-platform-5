"""Cache layer (SRS 17). Cache-first: checked before any paid call (CLAUDE.md rule 8).

`CacheAdapter` is the abstract contract so the Postgres/SQLite-table implementation used
in M0 can be swapped for Redis later without touching any caller (SRS 17.2).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import repository


class CacheAdapter(ABC):
    @abstractmethod
    def get(self, key: str) -> dict | None:
        ...

    @abstractmethod
    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        ...

    @abstractmethod
    def delete(self, key: str) -> None:
        ...


class DBCacheAdapter(CacheAdapter):
    """Cache table inside the app's own database (SRS 17.2 — no separate Redis in M0)."""

    def __init__(self, db: Session):
        self.db = db

    def get(self, key: str) -> dict | None:
        return repository.cache_get(self.db, key)

    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        repository.cache_set(self.db, key, value, expires_at)

    def delete(self, key: str) -> None:
        repository.cache_delete(self.db, key)

    def purge_expired(self) -> int:
        return repository.cache_purge_expired(self.db)


def make_cache_key(*parts: str) -> str:
    return ":".join(str(p) for p in parts)


def _next_approx_market_open_seconds(now: datetime) -> int:
    """Approximate seconds until the next US market open (13:30 UTC, weekdays only).

    No holiday calendar in M0 — this only needs to be a reasonable upper bound so a
    closed-market report cache doesn't get recomputed needlessly (SRS 17.1). Real
    calendar-aware logic can be layered in later without changing the cache contract.
    """
    candidate = now.replace(hour=13, minute=30, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:  # Sat=5, Sun=6
        candidate += timedelta(days=1)
    return max(60, int((candidate - now).total_seconds()))


def report_cache_ttl_seconds(market_open: bool, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    if market_open:
        return settings.cache_ttl_report_market_open_seconds
    return _next_approx_market_open_seconds(datetime.now(timezone.utc))


def ohlcv_daily_cache_ttl_seconds(settings: Settings | None = None) -> int:
    return (settings or get_settings()).cache_ttl_ohlcv_daily_seconds


def quote_cache_ttl_seconds(settings: Settings | None = None) -> int:
    return (settings or get_settings()).cache_ttl_quote_seconds
