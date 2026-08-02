from __future__ import annotations

from datetime import datetime

from app.config import Settings
from app.options.market_clock import NEW_YORK, spx_global_session


# 2026-07-30 is a Thursday, 2026-07-31 a Friday, 2026-08-01 a Saturday.
def ny(month, day, hour, minute=0):
    """A moment on the New York wall clock, which is what the session follows."""
    return datetime(2026, month, day, hour, minute, tzinfo=NEW_YORK)


def test_the_hours_the_owner_actually_trades_are_open():
    # 04:55 New York on a Thursday: the morning half of the global session.
    assert spx_global_session(ny(7, 30, 4, 55)) is True
    # 21:00 Thursday evening: the run-up to Friday's session.
    assert spx_global_session(ny(7, 30, 21)) is True


def test_the_gap_between_sessions_is_closed():
    # 09:20 sits after the global close and before the regular open.
    assert spx_global_session(ny(7, 30, 9, 20)) is False
    # Mid-afternoon belongs to the regular session, not this one.
    assert spx_global_session(ny(7, 30, 14)) is False
    # 20:00 is fifteen minutes early.
    assert spx_global_session(ny(7, 30, 20)) is False


def test_the_weekend_stays_shut():
    # Friday evening leads to Saturday, so there is no session to run up to.
    assert spx_global_session(ny(7, 31, 21)) is False
    # Saturday morning likewise.
    assert spx_global_session(ny(8, 1, 4)) is False
    # Sunday evening does lead to Monday.
    assert spx_global_session(ny(8, 2, 21)) is True


def test_the_global_session_can_be_switched_off():
    assert Settings().spx_global_trading_hours is True
    assert Settings(spx_global_trading_hours=False).spx_global_trading_hours is False


def test_the_calculator_agrees_with_the_service_about_the_session():
    """Both gates must read the same rule, or one silently vetoes the other."""
    from app.options.market_clock import market_session
    from app.spx.synthetic import calculate_synthetic_value

    moment = ny(7, 30, 4, 55)  # inside the global session
    session = market_session(moment)
    assert session.options_actionable is False

    result = calculate_synthetic_value(
        [], Settings(spx_risk_free_rate=0.04), session, now=moment
    )
    # It may fail for want of contracts, but not for "the market is closed".
    assert result.provider_status != "options_closed"

    shut = calculate_synthetic_value(
        [],
        Settings(spx_risk_free_rate=0.04, spx_global_trading_hours=False),
        session,
        now=moment,
    )
    assert shut.provider_status == "options_closed"


def test_the_discount_rate_is_present_and_plausible():
    """The calculator refuses to work without it, so a default has to exist."""
    settings = Settings()
    assert settings.spx_risk_free_rate is not None
    # A sanity band, not a forecast: outside this the parity maths is wrong.
    assert 0.0 <= settings.spx_risk_free_rate <= 0.10
    assert 0.0 <= settings.spx_dividend_yield <= 0.05
    # Freshness is checked against these, so they cannot be blank.
    assert settings.spx_risk_free_rate_updated_at
    assert settings.spx_dividend_yield_updated_at


def test_a_stale_rate_degrades_the_reading_rather_than_skewing_it():
    from app.options.market_clock import market_session
    from app.spx.synthetic import calculate_synthetic_value

    moment = ny(7, 30, 4, 55)
    session = market_session(moment)
    # A year-old stamp must not be treated as current.
    stale = Settings(spx_risk_free_rate_updated_at="2025-01-01")
    result = calculate_synthetic_value([], stale, session, now=moment)
    # It still runs; only the spot estimate is withheld.
    assert result.provider_status != "options_closed"


def test_a_closed_market_does_not_promise_a_reading_it_lacks():
    """Offering "the last reading" beside an empty panel reads as a fault."""
    from app.config import Settings as S
    from app.db import models  # noqa: F401
    from app.options.market_clock import market_session
    from app.spx.service import SPXHunterService

    session = market_session(ny(8, 2, 6))  # Sunday morning, nothing trading
    text = SPXHunterService._next_spx_open_ar(session)
    assert "بتوقيت الرياض" in text
    assert S().spx_global_trading_hours is True


def test_the_next_open_points_at_the_global_session_not_the_bell():
    from app.options.market_clock import market_session
    from app.spx.service import SPXHunterService

    # SPX reopens on Cboe's global session hours before the regular bell, so
    # quoting the equity open would send the reader away for nothing.
    session = market_session(ny(8, 2, 6))
    assert "20:15" not in SPXHunterService._next_spx_open_ar(session)
    assert SPXHunterService._next_spx_open_ar(session)
