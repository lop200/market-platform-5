"""NYSE-aware market clock with New York as the source of truth."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

NEW_YORK = ZoneInfo("America/New_York")
RIYADH = ZoneInfo("Asia/Riyadh")


@dataclass(frozen=True)
class MarketClock:
    session: str
    label_ar: str
    ny_time: str
    riyadh_time: str
    market_open_at: str | None
    market_close_at: str | None
    next_open_at: str | None
    is_trading_day: bool
    is_early_close: bool
    can_execute_stocks: bool

    def as_dict(self) -> dict:
        return asdict(self)


LABELS = {
    "overnight": "تداول ليلي",
    "pre_market": "قبل الافتتاح",
    "regular": "السوق مفتوح",
    "after_hours": "بعد الإغلاق",
    "closed": "السوق مغلق",
    "holiday": "عطلة",
    "half_day": "إغلاق مبكر",
}


@lru_cache(maxsize=32)
def _schedule(start: date, end: date):
    return mcal.get_calendar("NYSE").schedule(start_date=start, end_date=end)


def _iso(value) -> str | None:
    return value.to_pydatetime().astimezone(timezone.utc).isoformat() if value is not None else None


def market_clock(now: datetime | None = None) -> MarketClock:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ny = now.astimezone(NEW_YORK)
    schedule = _schedule(ny.date() - timedelta(days=1), ny.date() + timedelta(days=10))
    today = schedule[schedule.index.date == ny.date()]
    is_trading_day = not today.empty
    market_open = today.iloc[0]["market_open"] if is_trading_day else None
    market_close = today.iloc[0]["market_close"] if is_trading_day else None
    regular_close = datetime.combine(ny.date(), time(16), NEW_YORK)
    early_close = bool(
        market_close is not None
        and market_close.to_pydatetime().astimezone(NEW_YORK) < regular_close
    )
    minute = ny.hour * 60 + ny.minute
    if not is_trading_day:
        session = "holiday" if ny.weekday() < 5 else "closed"
    elif market_open <= now <= market_close:
        session = "half_day" if early_close else "regular"
    elif 240 <= minute < 570:
        session = "pre_market"
    elif market_close is not None and market_close.to_pydatetime().astimezone(NEW_YORK) <= ny and minute < 1200:
        session = "after_hours"
    elif minute >= 1200 or minute < 240:
        session = "overnight"
    else:
        session = "closed"
    future = schedule[schedule["market_open"] > now]
    next_open = future.iloc[0]["market_open"] if not future.empty else None
    return MarketClock(
        session=session, label_ar=LABELS[session], ny_time=ny.isoformat(),
        riyadh_time=now.astimezone(RIYADH).isoformat(),
        market_open_at=_iso(market_open), market_close_at=_iso(market_close),
        next_open_at=_iso(next_open), is_trading_day=is_trading_day,
        is_early_close=early_close, can_execute_stocks=session in {"regular", "half_day"},
    )
