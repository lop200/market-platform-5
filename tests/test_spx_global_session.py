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
