from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.opportunities.scanner import _session_exit
from app.opportunities.universe import select_scan_universe
from tests.test_scan_universe import _Provider


def utc(hour, minute=0, day=31):
    """A UTC moment; New York is four hours behind in July."""
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def test_the_regular_session_exits_before_its_bell():
    # 14:00 UTC is 10:00 New York, inside the regular session.
    exit_at, label, window, hours = _session_exit(utc(14))
    assert exit_at.astimezone(timezone.utc).hour == 19  # 15:55 New York
    assert "بتوقيت الرياض" in label
    assert "الجلسة الرسمية" in window
    assert 5.5 < hours < 6.5


def test_after_hours_exits_at_its_own_door_not_tomorrow_afternoon():
    """The old rule quoted a 20-hour "fast" trade held across the auction."""
    _, _, window, hours = _session_exit(utc(21))  # 17:00 New York
    assert "ما بعد الإغلاق" in window
    assert hours < 3.5


def test_time_left_never_reaches_zero():
    """It divides the probability horizon, so it cannot be zero."""
    _, _, _, hours = _session_exit(utc(19, 55))
    assert hours > 0


def test_a_low_price_cap_stops_leading_with_option_names():
    settings = Settings()
    provider = _Provider(ranked=["SNTI", "AITX", "NVDA"])
    cheap, inputs = select_scan_universe(provider, settings, 6, prefer_optionable=False)
    # Under a few dollars the options watchlist is the wrong ranking: those
    # names all trade far above the cap and would fill the deep pass.
    assert cheap[0] == "SNTI"
    assert inputs["optionable_first"] is False

    rich, rich_inputs = select_scan_universe(provider, settings, 6)
    assert rich[0] == "NVDA"
    assert rich_inputs["optionable_first"] is True


def _exit_at(month, day, hour, minute=0):
    """New York wall clock, which is what the session doors follow."""
    from app.options.market_clock import NEW_YORK
    from app.opportunities.scanner import _session_exit

    return _session_exit(datetime(2026, month, day, hour, minute, tzinfo=NEW_YORK))


def test_each_session_closes_at_its_own_door():
    # 2026-08-03 is a Monday.
    _, _, overnight, hours_night = _exit_at(8, 3, 0)
    assert "التداول الليلي" in overnight
    assert hours_night < 4.5  # the book shuts at 03:55, not this afternoon

    _, _, premarket, hours_pre = _exit_at(8, 3, 5)
    assert "ما قبل الافتتاح" in premarket
    assert hours_pre < 5

    _, _, regular, _ = _exit_at(8, 3, 10)
    assert "الجلسة الرسمية" in regular


def test_a_night_trade_is_never_carried_across_the_opening():
    """Holding through an auction is a different trade with gap risk."""
    exit_at, _, _, hours = _exit_at(8, 3, 0)
    from app.options.market_clock import NEW_YORK

    assert exit_at.astimezone(NEW_YORK).hour == 3  # 03:55, before the book shuts
    assert hours < 4.5


def test_the_weekend_offers_no_window_instead_of_a_twenty_hour_snipe():
    # Sunday: falling back to the regular bell quoted a 20-hour "fast" trade.
    _, _, window, hours = _exit_at(8, 2, 10)
    assert "لا توجد نافذة" in window
    assert hours == 0.05


def test_the_wild_band_is_offered_and_labelled_on_the_card():
    """The owner named this band; a card must carry the label, not just the
    filter that produced it — filters get changed, saved results outlive them."""
    from fastapi.testclient import TestClient

    from app.config import Settings
    from app.main import app

    html = TestClient(app).get("/").text
    assert 'value="wild"' in html
    assert "الأسهم المجنونة" in html
    assert "wild:[0.5,5]" in html
    assert "wild-flag" in html

    settings = Settings()
    # The floor is the point: a live scan surfaced stocks at seven cents.
    assert settings.wild_scan_min_price == 0.50
    assert settings.wild_scan_max_price == 5.0
