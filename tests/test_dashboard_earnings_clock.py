from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.db.models import EarningsCalendarEvent
from app.earnings.service import confidence_penalty, event_risk
from app.main import app
from app.markets.clock import market_clock


def test_market_clock_observes_holiday_and_next_open():
    clock = market_clock(datetime(2026, 7, 3, 15, tzinfo=timezone.utc))
    assert clock.session == "holiday"
    assert not clock.can_execute_stocks
    assert "2026-07-06" in clock.next_open_at


def test_market_clock_handles_regular_and_after_hours():
    regular = market_clock(datetime(2026, 7, 29, 15, tzinfo=timezone.utc))
    after = market_clock(datetime(2026, 7, 29, 21, tzinfo=timezone.utc))
    assert regular.session == "regular"
    assert regular.can_execute_stocks
    assert after.session == "after_hours"
    assert not after.can_execute_stocks


def test_earnings_risk_bands_are_deterministic():
    assert event_risk(5.9) == "critical"
    assert event_risk(23) == "very_high"
    assert event_risk(47) == "high"
    assert event_risk(167) == "elevated"
    assert confidence_penalty(5) > confidence_penalty(120)


def test_dashboard_returns_saved_earnings_without_provider_call(db_session, monkeypatch):
    now = datetime.now(timezone.utc)
    db_session.add(EarningsCalendarEvent(
        symbol="NVDA", company_name="NVIDIA", announced_at=now + timedelta(hours=20),
        timing="amc", eps_estimate=1.23, source="manual", verified=False,
    ))
    db_session.commit()

    from app.api import routes_dashboard
    monkeypatch.setattr(routes_dashboard, "get_settings", lambda: type("S", (), {
        "market_data_provider": "alpaca", "alpaca_feed": "sip",
        "paper_trading_only": True, "trading_kill_switch": True,
    })())
    payload = routes_dashboard.dashboard_snapshot(db_session)
    assert payload["connection"]["feed"] == "sip"
    assert payload["connection"]["paper_only"] is True
    assert payload["earnings"][0]["symbol"] == "NVDA"
    assert payload["earnings"][0]["risk"] == "very_high"


def test_home_has_2027_rtl_navigation_and_no_horizontal_overflow():
    html = TestClient(app).get("/").text
    assert 'dir="rtl"' in html
    assert "overflow-x:hidden" in html
    assert "ما قبل الافتتاح" in html
    assert "ما بعد الإغلاق" in html
    assert "إعلانات الشركات" in html
    assert "Paper Trading" in html
