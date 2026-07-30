from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EarningsEvent(BaseModel):
    """Normalized internal earnings model. Raw provider fields never leave the adapter."""

    model_config = ConfigDict(extra="forbid")

    symbol: str
    company_name: str | None = None
    earnings_date: date
    earnings_hour: str = "unknown"
    quarter: int | None = None
    fiscal_year: int | None = None
    eps_estimate: float | None = None
    eps_actual: float | None = None
    eps_previous: float | None = None
    revenue_estimate: float | None = None
    revenue_actual: float | None = None
    revenue_previous: float | None = None
    eps_surprise: float | None = None
    eps_surprise_percent: float | None = None
    revenue_surprise: float | None = None
    revenue_surprise_percent: float | None = None
    number_of_analysts: int | None = None
    market_cap: float | None = None
    sector: str | None = None
    source: str = "Finnhub"
    last_updated: datetime

    session_label: str
    event_time_new_york: datetime | None = None
    event_time_riyadh: datetime | None = None
    remaining_days: int | None = None
    remaining_hours: int | None = None
    remaining_minutes: int | None = None
    remaining_text_ar: str
    is_today: bool = False
    is_tomorrow: bool = False
    is_within_24h: bool | None = None
    is_within_48h: bool | None = None
    is_within_7d: bool = False
    has_passed: bool = False
    time_is_exact: bool = False
    appointment_status_ar: str

    result_status: str = "pending"
    result_label_ar: str = "لم تصدر النتائج"
    earnings_risk: str = "medium"
    earnings_risk_ar: str = "متوسطة"
    warning_required: bool = False
    prevent_new_entry: bool = False
    allow_normal_opportunity: bool = True
    post_earnings_enabled: bool = False
    iv_crush_warning: bool = False

    estimate_direction: str | None = None
    estimate_range_low: float | None = None
    estimate_range_high: float | None = None
    historical_average_move_percent: float | None = None
    implied_move_percent: float | None = None
    current_iv: float | None = None
    stock_move_after_announcement_percent: float | None = None
    next_session_move_percent: float | None = None
    result_released_at: datetime | None = None
    is_watchlist: bool = False
    scenarios: dict[str, dict[str, Any]] = Field(default_factory=dict)


class EarningsCalendarPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[dict] = Field(default_factory=list)
    updated_at: datetime | None = None
    expires_at: datetime | None = None
    source: str = "Finnhub"
    connection_status: str = "not_loaded"
    connection_label_ar: str = "لم يتم التحديث بعد"
    cache_hit: bool = False
    is_stale: bool = False
    last_success_at: datetime | None = None
    last_error: str | None = None
