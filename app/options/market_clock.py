from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
RIYADH = ZoneInfo("Asia/Riyadh")


@dataclass(frozen=True)
class MarketSession:
    code: str
    label_ar: str
    options_status: str
    options_label_ar: str
    stock_actionable: bool
    options_actionable: bool
    new_york_time: datetime
    riyadh_time: datetime
    session_closes_at: datetime | None
    next_stock_open_at: datetime
    next_options_open_at: datetime
    early_close: bool
    holiday: bool


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    day = date(year, month, 1)
    return day + timedelta(days=(weekday - day.weekday()) % 7 + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    day = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
    return day - timedelta(days=(day.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def nyse_holidays(year: int) -> set[date]:
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    return {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        thanksgiving,
        _observed(date(year, 12, 25)),
    }


def is_early_close(day: date) -> bool:
    thanksgiving = _nth_weekday(day.year, 11, 3, 4)
    candidates = {
        thanksgiving + timedelta(days=1),
        date(day.year, 7, 3),
        date(day.year, 12, 24),
    }
    return day in candidates and day.weekday() < 5 and day not in nyse_holidays(day.year)


def _trading_day(day: date) -> bool:
    return day.weekday() < 5 and day not in nyse_holidays(day.year)


def _next_trading_day(day: date, *, include_today: bool = False) -> date:
    candidate = day if include_today else day + timedelta(days=1)
    while not _trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def _at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=NEW_YORK)


def market_session(now: datetime | None = None) -> MarketSession:
    value = now or datetime.now(tz=NEW_YORK)
    if value.tzinfo is None:
        value = value.replace(tzinfo=NEW_YORK)
    eastern = value.astimezone(NEW_YORK)
    clock = eastern.time().replace(tzinfo=None)
    weekday_open = _trading_day(eastern.date())
    early = weekday_open and is_early_close(eastern.date())
    regular_close = time(13) if early else time(16)
    holiday = eastern.date() in nyse_holidays(eastern.year)
    session_close: datetime | None
    if not weekday_open:
        code, label, session_close = (
            "holiday" if holiday else "closed",
            "عطلة" if holiday else "مغلق",
            None,
        )
    elif clock < time(4):
        code, label, session_close = "overnight", "تداول ليلي", _at(eastern.date(), time(4))
    elif time(4) <= clock < time(9, 30):
        code, label, session_close = "pre_market", "ما قبل الافتتاح", _at(eastern.date(), time(9, 30))
    elif time(9, 30) <= clock < regular_close:
        code, label, session_close = (
            "early_close" if early else "regular",
            "إغلاق مبكر" if early else "جلسة رسمية",
            _at(eastern.date(), regular_close),
        )
    elif regular_close <= clock < time(20):
        code, label, session_close = "after_hours", "ما بعد الإغلاق", _at(eastern.date(), time(20))
    else:
        code, label, session_close = "overnight", "تداول ليلي", _at(
            _next_trading_day(eastern.date()), time(4)
        )

    if weekday_open and clock < time(9, 30):
        next_options_open = _at(eastern.date(), time(9, 30))
    elif weekday_open and time(9, 30) <= clock < regular_close:
        next_options_open = _at(_next_trading_day(eastern.date()), time(9, 30))
    else:
        next_options_open = _at(_next_trading_day(eastern.date()), time(9, 30))
    if weekday_open and clock < time(4):
        next_stock_open = _at(eastern.date(), time(4))
    else:
        next_stock_open = _at(_next_trading_day(eastern.date()), time(4))
    options_open = weekday_open and time(9, 30) <= clock < regular_close
    options_status = "open" if options_open else "opens_later"
    options_label = "مفتوح" if options_open else "يفتح بعد مدة"
    return MarketSession(
        code=code,
        label_ar=label,
        options_status=options_status,
        options_label_ar=options_label,
        stock_actionable=code in {
            "overnight", "pre_market", "regular", "early_close", "after_hours"
        },
        options_actionable=options_open,
        new_york_time=eastern,
        riyadh_time=eastern.astimezone(RIYADH),
        session_closes_at=session_close,
        next_stock_open_at=next_stock_open,
        next_options_open_at=next_options_open,
        early_close=early,
        holiday=holiday,
    )


def serialize_market_session(session: MarketSession) -> dict:
    return {
        "stock_status": session.code,
        "stock_label_ar": session.label_ar,
        "options_status": session.options_status,
        "options_label_ar": session.options_label_ar,
        "stock_actionable": session.stock_actionable,
        "options_actionable": session.options_actionable,
        "new_york_time": session.new_york_time.isoformat(),
        "riyadh_time": session.riyadh_time.isoformat(),
        "session_closes_at": (
            session.session_closes_at.isoformat() if session.session_closes_at else None
        ),
        "next_stock_open_at": session.next_stock_open_at.isoformat(),
        "next_options_open_at": session.next_options_open_at.isoformat(),
        "early_close": session.early_close,
        "holiday": session.holiday,
    }
