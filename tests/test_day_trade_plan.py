from __future__ import annotations

from datetime import datetime, timezone

from app.config import Settings
from app.opportunities.scanner import _session_exit
from app.opportunities.universe import select_scan_universe
from tests.test_scan_universe import _Provider


def utc(hour, minute=0, day=31):
    """A UTC moment; New York is four hours behind in July."""
    return datetime(2026, 7, day, hour, minute, tzinfo=timezone.utc)


def test_the_exit_deadline_lands_before_the_closing_bell():
    # 14:00 UTC is 10:00 New York, six hours before the 15:55 exit.
    exit_at, label, window, hours = _session_exit(utc(14))
    assert exit_at.astimezone(timezone.utc).hour == 19  # 15:55 New York
    assert "بتوقيت الرياض" in label
    assert "نفس الجلسة" in window
    assert 5.5 < hours < 6.5


def test_after_the_bell_the_deadline_rolls_to_the_next_session():
    # 21:00 UTC is 17:00 New York — today's exit has passed.
    today, _, _, _ = _session_exit(utc(14))
    tomorrow, _, _, hours = _session_exit(utc(21))
    assert tomorrow > today
    assert hours > 12


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
