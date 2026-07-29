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
    stock_actionable: bool
    options_actionable: bool
    new_york_time: datetime
    riyadh_time: datetime


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


def market_session(now: datetime | None = None) -> MarketSession:
    value = now or datetime.now(tz=NEW_YORK)
    if value.tzinfo is None:
        value = value.replace(tzinfo=NEW_YORK)
    eastern = value.astimezone(NEW_YORK)
    clock = eastern.time().replace(tzinfo=None)
    weekday_open = eastern.weekday() < 5 and eastern.date() not in nyse_holidays(eastern.year)
    regular_close = time(13) if is_early_close(eastern.date()) else time(16)
    if not weekday_open:
        code, label = "closed", "السوق مغلق"
    elif time(4) <= clock < time(9, 30):
        code, label = "pre_market", "ما قبل الافتتاح"
    elif time(9, 30) <= clock < regular_close:
        code, label = "regular", "الجلسة الرسمية"
    elif regular_close <= clock < time(20):
        code, label = "after_hours", "ما بعد الإغلاق"
    else:
        code, label = "closed", "السوق مغلق"
    return MarketSession(
        code=code,
        label_ar=label,
        stock_actionable=code in {"pre_market", "regular", "after_hours"},
        options_actionable=code == "regular",
        new_york_time=eastern,
        riyadh_time=eastern.astimezone(RIYADH),
    )
