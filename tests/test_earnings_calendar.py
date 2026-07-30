from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import repository
from app.events.earnings import (
    LAST_SUCCESS_KEY,
    refresh_earnings_cache,
)
from app.events.finnhub import (
    FinnhubEarningsProvider,
    calculate_remaining,
    classify_result,
    normalize_session,
    safe_number,
)
from app.events.schemas import EarningsEvent
from app.main import app
from app.opportunities.jobs import _earnings_prevents_entry


NOW = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)


def _event(symbol: str = "AAPL") -> EarningsEvent:
    timing = calculate_remaining(date(2026, 7, 31), "amc", now=NOW)
    return EarningsEvent(
        symbol=symbol,
        company_name="Apple Inc",
        earnings_date=date(2026, 7, 31),
        eps_estimate=1.42,
        revenue_estimate=98_000_000_000,
        last_updated=NOW,
        appointment_status_ar="الموعد المتوقع",
        **timing,
    )


class _Provider:
    def __init__(self, events=None, error: Exception | None = None):
        self.events = events or []
        self.error = error
        self.calls = 0

    def fetch(self, **_kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        return self.events


def test_null_nan_and_infinity_are_cleaned():
    assert safe_number(None) is None
    assert safe_number("undefined") is None
    assert safe_number(float("nan")) is None
    assert safe_number(float("inf")) is None
    assert safe_number("1.23456") == 1.2346


def test_before_and_after_market_are_normalized():
    assert normalize_session("bmo", date(2026, 7, 30))[:2] == (
        "before_market",
        "قبل الافتتاح",
    )
    assert normalize_session("amc", date(2026, 7, 30))[:2] == (
        "after_market",
        "بعد الإغلاق",
    )


def test_exact_time_converts_new_york_to_riyadh_in_summer():
    timing = calculate_remaining(date(2026, 7, 30), "16:00", now=NOW)
    assert timing["event_time_new_york"].hour == 16
    assert timing["event_time_riyadh"].hour == 23
    assert timing["time_is_exact"] is True


def test_dst_conversion_changes_in_winter():
    winter = datetime(2026, 12, 1, 18, 0, tzinfo=timezone.utc)
    timing = calculate_remaining(date(2026, 12, 1), "16:00", now=winter)
    assert timing["event_time_riyadh"].hour == 0
    assert timing["event_time_riyadh"].date() == date(2026, 12, 2)


def test_today_tomorrow_unknown_time_and_passed():
    today = calculate_remaining(date(2026, 7, 30), "amc", now=NOW)
    tomorrow = calculate_remaining(date(2026, 7, 31), "bmo", now=NOW)
    unknown = calculate_remaining(date(2026, 8, 2), None, now=NOW)
    passed = calculate_remaining(date(2026, 7, 30), "13:00", now=NOW)
    assert today["is_today"] is True
    assert "اليوم" in today["remaining_text_ar"]
    assert tomorrow["is_tomorrow"] is True
    assert "غد" in tomorrow["remaining_text_ar"]
    assert unknown["time_is_exact"] is False
    assert unknown["event_time_new_york"] is None
    assert passed["has_passed"] is True


def test_result_classification_uses_eps_and_revenue():
    assert classify_result(1.2, 1.0, 120, 100)[0] == "beat"
    assert classify_result(0.8, 1.0, 80, 100)[0] == "miss"
    assert classify_result(1.0, 1.0, 100, 100)[0] == "match"
    assert classify_result(1.2, 1.0, 80, 100)[0] == "mixed"
    assert classify_result(1.2, 1.0, None, 100)[0] == "incomplete"


def test_finnhub_provider_returns_only_normalized_fields(monkeypatch):
    settings = Settings(
        finnhub_api_key="test-key",
        earnings_enrichment_limit=1,
        external_max_retries=0,
    )
    provider = FinnhubEarningsProvider(settings)

    def fake_get(path, params):
        if path == "/calendar/earnings":
            return {
                "earningsCalendar": [
                    {
                        "symbol": "AAPL",
                        "date": "2026-07-30",
                        "hour": "amc",
                        "quarter": 3,
                        "year": 2026,
                        "epsEstimate": 1.0,
                        "epsActual": 1.2,
                        "revenueEstimate": 100,
                        "revenueActual": 110,
                        "numberOfAnalysts": 28,
                        "internalFinnhubField": "must-not-leak",
                    }
                ]
            }
        if path == "/stock/profile2":
            return {
                "name": "Apple Inc",
                "finnhubIndustry": "Technology",
                "marketCapitalization": 3_000_000,
            }
        return [{"period": "2026-04-30", "actual": 0.95}]

    monkeypatch.setattr(provider, "_get", fake_get)
    events = provider.fetch(
        start=date(2026, 7, 30),
        end=date(2026, 7, 30),
        now=NOW,
    )
    dumped = events[0].model_dump()
    assert dumped["company_name"] == "Apple Inc"
    assert dumped["eps_surprise_percent"] == 20.0
    assert dumped["revenue_surprise_percent"] == 10.0
    assert dumped["result_status"] == "beat"
    assert "internalFinnhubField" not in dumped


def test_weekly_cache_avoids_duplicate_finnhub_calls(db_session):
    provider = _Provider([_event()])
    settings = Settings(
        earnings_cache_seconds=4 * 60 * 60,
        earnings_today_cache_seconds=60 * 60,
    )
    first = refresh_earnings_cache(
        db_session, settings, provider=provider, now=NOW
    )
    second = refresh_earnings_cache(
        db_session, settings, provider=provider, now=NOW + timedelta(minutes=5)
    )
    assert first["source"] == "Finnhub"
    assert second["cache_hit"] is True
    assert provider.calls == 2


def test_manual_refresh_bypasses_cache(db_session):
    provider = _Provider([_event()])
    settings = Settings()
    refresh_earnings_cache(db_session, settings, provider=provider, now=NOW)
    refresh_earnings_cache(
        db_session,
        settings,
        provider=provider,
        now=NOW + timedelta(minutes=1),
        force=True,
    )
    assert provider.calls == 4


def test_failure_uses_last_saved_data(db_session):
    settings = Settings()
    refresh_earnings_cache(
        db_session, settings, provider=_Provider([_event()]), now=NOW
    )
    payload = refresh_earnings_cache(
        db_session,
        settings,
        provider=_Provider(error=TimeoutError()),
        now=NOW + timedelta(hours=1),
        force=True,
    )
    assert payload["is_stale"] is True
    assert payload["items"][0]["symbol"] == "AAPL"
    assert payload["connection_label_ar"] == "آخر بيانات محفوظة"
    assert "test-key" not in str(payload)


def test_failure_without_saved_data_returns_safe_empty_payload(db_session):
    payload = refresh_earnings_cache(
        db_session,
        Settings(),
        provider=_Provider(error=RuntimeError("secret-token-value")),
        now=NOW,
        force=True,
    )
    assert payload["items"] == []
    assert payload["connection_status"] == "error"
    assert "secret-token-value" not in str(payload)
    assert repository.cache_get_any(db_session, LAST_SUCCESS_KEY) is None


def test_earnings_page_is_rtl_responsive_and_has_no_raw_json():
    client = TestClient(app)
    page = client.get("/earnings")
    assert page.status_code == 200
    assert 'dir="rtl"' in page.text
    assert "overflow-x:hidden" in page.text
    assert "JSON" not in page.text
    assert "توقعات ما قبل الإعلان" in page.text
    assert "هذه سيناريوهات تقديرية وليست توقعًا مؤكدًا." in page.text


def test_home_has_earnings_icon():
    client = TestClient(app)
    home = client.get("/").text
    assert 'href="/earnings"' in home
    assert "تقويم الإعلانات" in home


def test_missing_earnings_never_breaks_stock_analysis():
    assert _earnings_prevents_entry({"earnings": None}) is False
    assert _earnings_prevents_entry({}) is False
    assert _earnings_prevents_entry(
        {"earnings": {"prevent_new_entry": True}}
    ) is True
