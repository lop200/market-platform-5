from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.cache import (
    DBCacheAdapter,
    _next_approx_market_open_seconds,
    make_cache_key,
    ohlcv_daily_cache_ttl_seconds,
    quote_cache_ttl_seconds,
    report_cache_ttl_seconds,
)
from app.db import repository


def test_set_and_get_roundtrip(db_session):
    cache = DBCacheAdapter(db_session)
    cache.set("report:NVDA", {"symbol": "NVDA", "price": 128.4}, ttl_seconds=60)
    assert cache.get("report:NVDA") == {"symbol": "NVDA", "price": 128.4}


def test_get_missing_key_returns_none(db_session):
    cache = DBCacheAdapter(db_session)
    assert cache.get("does:not:exist") is None


def test_expired_entry_returns_none(db_session):
    cache = DBCacheAdapter(db_session)
    expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    repository.cache_set(db_session, "stale:key", {"x": 1}, expires_at)
    assert cache.get("stale:key") is None


def test_delete_removes_entry(db_session):
    cache = DBCacheAdapter(db_session)
    cache.set("k", {"v": 1}, ttl_seconds=60)
    cache.delete("k")
    assert cache.get("k") is None


def test_purge_expired_only_removes_expired(db_session):
    cache = DBCacheAdapter(db_session)
    cache.set("fresh", {"v": 1}, ttl_seconds=3600)
    repository.cache_set(
        db_session, "stale", {"v": 2}, datetime.now(timezone.utc) - timedelta(seconds=1)
    )
    purged = cache.purge_expired()
    assert purged == 1
    assert cache.get("fresh") == {"v": 1}


def test_make_cache_key_joins_parts():
    assert make_cache_key("report", "NVDA", "ar") == "report:NVDA:ar"


def test_report_cache_ttl_market_open_uses_settings(test_settings):
    test_settings.cache_ttl_report_market_open_seconds = 900
    assert report_cache_ttl_seconds(True, test_settings) == 900


def test_report_cache_ttl_market_closed_is_positive(test_settings):
    ttl = report_cache_ttl_seconds(False, test_settings)
    assert ttl > 0


def test_next_approx_market_open_skips_weekend():
    # Saturday 2026-07-11 10:00 UTC -> next Monday 2026-07-13 13:30 UTC
    saturday = datetime(2026, 7, 11, 10, 0, tzinfo=timezone.utc)
    seconds = _next_approx_market_open_seconds(saturday)
    expected_open = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)
    assert seconds == int((expected_open - saturday).total_seconds())


def test_next_approx_market_open_same_day_before_open():
    weekday_morning = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)  # Tuesday
    seconds = _next_approx_market_open_seconds(weekday_morning)
    expected_open = weekday_morning.replace(hour=13, minute=30)
    assert seconds == int((expected_open - weekday_morning).total_seconds())


def test_next_approx_market_open_rolls_to_next_day_after_close():
    # Tuesday 2026-07-14 20:00 UTC (after the 13:30 open time) -> Wednesday 13:30 UTC
    late_evening = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)
    seconds = _next_approx_market_open_seconds(late_evening)
    expected_open = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
    assert seconds == int((expected_open - late_evening).total_seconds())


def test_ohlcv_and_quote_ttl_use_settings(test_settings):
    test_settings.cache_ttl_ohlcv_daily_seconds = 12345
    test_settings.cache_ttl_quote_seconds = 42
    assert ohlcv_daily_cache_ttl_seconds(test_settings) == 12345
    assert quote_cache_ttl_seconds(test_settings) == 42
