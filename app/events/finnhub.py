from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import Settings
from app.events.schemas import EarningsEvent

logger = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
RIYADH = ZoneInfo("Asia/Riyadh")

_BEFORE_CODES = {"bmo", "before", "before_market", "before market open"}
_AFTER_CODES = {"amc", "after", "after_market", "after market close"}


def safe_number(value: Any, *, digits: int = 4) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits)


def safe_integer(value: Any) -> int | None:
    number = safe_number(value, digits=0)
    return int(number) if number is not None else None


def _surprise(actual: float | None, estimate: float | None) -> tuple[float | None, float | None]:
    if actual is None or estimate is None:
        return None, None
    difference = round(actual - estimate, 4)
    percent = (
        round(difference / abs(estimate) * 100, 2)
        if estimate != 0 else None
    )
    return difference, percent


def classify_result(
    eps_actual: float | None,
    eps_estimate: float | None,
    revenue_actual: float | None,
    revenue_estimate: float | None,
) -> tuple[str, str]:
    """Classify EPS and revenue together; one metric alone is not enough."""
    if None in (eps_actual, eps_estimate, revenue_actual, revenue_estimate):
        return "incomplete", "البيانات غير مكتملة"
    eps_diff = float(eps_actual) - float(eps_estimate)
    revenue_diff = float(revenue_actual) - float(revenue_estimate)
    if eps_diff > 0 and revenue_diff > 0:
        return "beat", "تفوق على التوقعات"
    if eps_diff < 0 and revenue_diff < 0:
        return "miss", "أقل من التوقعات"
    if eps_diff == 0 and revenue_diff == 0:
        return "match", "مطابق للتوقعات"
    return "mixed", "نتائج مختلطة"


def _parse_exact_time(raw_hour: str, event_date: date) -> datetime | None:
    normalized = raw_hour.strip()
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            parsed = datetime.strptime(normalized, pattern).time()
            return datetime.combine(event_date, parsed, NEW_YORK)
        except ValueError:
            continue
    return None


def normalize_session(raw_hour: Any, event_date: date) -> tuple[str, str, datetime | None]:
    normalized = str(raw_hour or "").strip().lower()
    if normalized in _BEFORE_CODES:
        return "before_market", "قبل الافتتاح", None
    if normalized in _AFTER_CODES:
        return "after_market", "بعد الإغلاق", None
    exact = _parse_exact_time(normalized, event_date)
    if exact is not None:
        if exact.time() < clock_time(9, 30):
            return "before_market", "قبل الافتتاح", exact
        if exact.time() >= clock_time(16, 0):
            return "after_market", "بعد الإغلاق", exact
        return "unspecified", "وقت غير محدد", exact
    return "unspecified", "وقت غير محدد", None


def calculate_remaining(
    event_date: date,
    raw_hour: Any,
    *,
    now: datetime | None = None,
    results_available: bool = False,
) -> dict[str, Any]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    now_ny = current.astimezone(NEW_YORK)
    hour_code, session_label, event_ny = normalize_session(raw_hour, event_date)
    event_riyadh = event_ny.astimezone(RIYADH) if event_ny else None
    calendar_days = (event_date - now_ny.date()).days
    is_today = calendar_days == 0
    is_tomorrow = calendar_days == 1
    exact_seconds = (event_ny - now_ny).total_seconds() if event_ny else None
    has_passed = bool(
        results_available
        or calendar_days < 0
        or (exact_seconds is not None and exact_seconds < 0)
    )

    remaining_days: int | None = max(calendar_days, 0)
    remaining_hours: int | None = None
    remaining_minutes: int | None = None
    within_24: bool | None = None
    within_48: bool | None = None
    if exact_seconds is not None:
        absolute_seconds = abs(int(exact_seconds))
        remaining_days = absolute_seconds // 86_400
        remaining_hours = (absolute_seconds % 86_400) // 3_600
        remaining_minutes = (absolute_seconds % 3_600) // 60
        within_24 = not has_passed and exact_seconds <= 86_400
        within_48 = not has_passed and exact_seconds <= 172_800

    if has_passed:
        if event_ny is None:
            remaining_text = "تم الإعلان"
        else:
            elapsed = abs(int(exact_seconds or 0))
            if elapsed < 3_600:
                remaining_text = f"تم الإعلان منذ {max(1, elapsed // 60)} دقيقة"
            elif elapsed < 86_400:
                remaining_text = f"تم الإعلان منذ {elapsed // 3_600} ساعة"
            else:
                remaining_text = f"تم الإعلان منذ {elapsed // 86_400} يوم"
    elif is_today:
        suffix = (
            " قبل الافتتاح" if hour_code == "before_market"
            else " بعد الإغلاق" if hour_code == "after_market"
            else ""
        )
        if exact_seconds is not None and exact_seconds < 86_400:
            hours = int(exact_seconds) // 3_600
            minutes = (int(exact_seconds) % 3_600) // 60
            remaining_text = (
                f"متبقي {hours} ساعات و{minutes} دقيقة"
                if hours else f"متبقي {minutes} دقيقة"
            )
        else:
            remaining_text = f"الإعلان اليوم{suffix}"
    elif is_tomorrow:
        suffix = (
            " قبل الافتتاح" if hour_code == "before_market"
            else " بعد الإغلاق" if hour_code == "after_market"
            else ""
        )
        remaining_text = f"الإعلان غدًا{suffix}"
    elif calendar_days > 1:
        remaining_text = f"متبقي {calendar_days} أيام"
    else:
        remaining_text = "وقت الإعلان غير محدد"

    return {
        "earnings_hour": hour_code,
        "session_label": session_label,
        "event_time_new_york": event_ny,
        "event_time_riyadh": event_riyadh,
        "remaining_days": remaining_days,
        "remaining_hours": remaining_hours,
        "remaining_minutes": remaining_minutes,
        "remaining_text_ar": remaining_text,
        "is_today": is_today,
        "is_tomorrow": is_tomorrow,
        "is_within_24h": within_24,
        "is_within_48h": within_48,
        "is_within_7d": not has_passed and 0 <= calendar_days <= 7,
        "has_passed": has_passed,
        "time_is_exact": event_ny is not None,
    }


def earnings_risk(timing: dict[str, Any]) -> dict[str, Any]:
    if timing["has_passed"]:
        return {
            "earnings_risk": "post_earnings",
            "earnings_risk_ar": "ما بعد الإعلان",
            "warning_required": False,
            "prevent_new_entry": False,
            "allow_normal_opportunity": True,
            "post_earnings_enabled": True,
            "iv_crush_warning": False,
        }
    exact_hours = None
    if timing["time_is_exact"]:
        exact_hours = (
            (timing["remaining_days"] or 0) * 24
            + (timing["remaining_hours"] or 0)
            + (timing["remaining_minutes"] or 0) / 60
        )
    if exact_hours is not None and exact_hours < 6:
        level, label, block = "very_high", "مرتفعة جدًا", True
    elif timing["is_within_24h"] is True or timing["is_today"]:
        level, label, block = "very_high", "مرتفعة جدًا", True
    elif timing["is_within_48h"] is True or timing["is_tomorrow"]:
        level, label, block = "high", "مرتفعة", True
    elif timing["is_within_7d"]:
        level, label, block = "medium", "متوسطة", False
    else:
        level, label, block = "low", "منخفضة", False
    return {
        "earnings_risk": level,
        "earnings_risk_ar": label,
        "warning_required": level in {"medium", "high", "very_high"},
        "prevent_new_entry": block,
        "allow_normal_opportunity": not block,
        "post_earnings_enabled": False,
        "iv_crush_warning": level in {"medium", "high", "very_high"},
    }


def build_scenarios(
    *,
    support: float | None = None,
    resistance: float | None = None,
    stop: float | None = None,
    targets: list[float] | None = None,
) -> dict[str, dict[str, Any]]:
    targets = targets or []
    return {
        "positive": {
            "label_ar": "السيناريو الإيجابي",
            "conditions_ar": "EPS والإيرادات أعلى من المتوقع مع توجيه إيجابي عند توفره.",
            "breakout_level": resistance,
            "nearest_resistance": resistance,
            "next_target": targets[0] if targets else None,
        },
        "neutral": {
            "label_ar": "السيناريو المحايد",
            "conditions_ar": "نتائج قريبة من المتوقع وحركة داخل النطاق؛ انتظار تأكيد.",
            "support": support,
            "resistance": resistance,
            "action_ar": "انتظار تأكيد الاتجاه بعد استقرار الحركة.",
        },
        "negative": {
            "label_ar": "السيناريو السلبي",
            "conditions_ar": "EPS أو الإيرادات أقل من المتوقع مع كسر الدعم.",
            "breakdown_level": support,
            "invalidation_level": stop,
            "next_support": stop,
        },
    }


class FinnhubEarningsProvider:
    """Bounded Finnhub adapter that returns normalized EarningsEvent objects."""

    base_url = "https://finnhub.io/api/v1"
    provider_name = "Finnhub"

    def __init__(self, settings: Settings):
        if not settings.finnhub_api_key:
            raise ValueError("FINNHUB_API_KEY is not configured")
        self.settings = settings
        self._headers = {"X-Finnhub-Token": settings.finnhub_api_key}

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.settings.external_max_retries + 1):
            try:
                with httpx.Client(
                    timeout=self.settings.external_timeout_seconds,
                    headers=self._headers,
                ) as client:
                    response = client.get(f"{self.base_url}{path}", params=params)
                    response.raise_for_status()
                    return response.json()
            except Exception as exc:
                last_error = exc
                if attempt < self.settings.external_max_retries:
                    time.sleep(min(0.25 * (attempt + 1), 0.75))
        raise RuntimeError(f"Finnhub request failed: {type(last_error).__name__}")

    def _optional_get(self, path: str, params: dict[str, Any]) -> Any:
        try:
            return self._get(path, params)
        except Exception as exc:
            logger.info("Finnhub optional enrichment unavailable: %s", type(exc).__name__)
            return None

    @staticmethod
    def _importance(raw: dict[str, Any], watchlist: set[str]) -> tuple:
        symbol = str(raw.get("symbol") or "").upper()
        revenue = abs(safe_number(raw.get("revenueEstimate")) or 0)
        known_session = str(raw.get("hour") or "").lower() in (_BEFORE_CODES | _AFTER_CODES)
        has_estimates = (
            safe_number(raw.get("epsEstimate")) is not None
            and safe_number(raw.get("revenueEstimate")) is not None
        )
        return (
            0 if symbol in watchlist else 1,
            0 if raw.get("epsActual") is not None else 1,
            0 if has_estimates else 1,
            0 if known_session else 1,
            -revenue,
            symbol,
        )

    @staticmethod
    def _previous_eps(history: Any, event_date: date) -> float | None:
        if not isinstance(history, list):
            return None
        rows = []
        for item in history:
            try:
                period = date.fromisoformat(str(item.get("period")))
            except (TypeError, ValueError):
                continue
            if period < event_date:
                rows.append((period, safe_number(item.get("actual"))))
        rows.sort(reverse=True)
        return next((value for _, value in rows if value is not None), None)

    def fetch(
        self,
        *,
        start: date,
        end: date,
        now: datetime | None = None,
        watchlist: set[str] | None = None,
        enrichment_limit: int | None = None,
    ) -> list[EarningsEvent]:
        current = now or datetime.now(timezone.utc)
        watch = {item.upper() for item in (watchlist or set())}
        payload = self._get(
            "/calendar/earnings",
            {"from": start.isoformat(), "to": end.isoformat()},
        )
        raw_items = payload.get("earningsCalendar") if isinstance(payload, dict) else []
        raw_items = [item for item in (raw_items or []) if isinstance(item, dict)]
        raw_items.sort(
            key=lambda item: (
                str(item.get("date") or "9999-12-31"),
                *self._importance(item, watch),
            )
        )
        raw_items = raw_items[: self.settings.earnings_calendar_limit]

        effective_enrichment_limit = (
            self.settings.earnings_enrichment_limit
            if enrichment_limit is None
            else max(0, enrichment_limit)
        )
        enrich_candidates = sorted(
            raw_items,
            key=lambda item: self._importance(item, watch),
        )[:effective_enrichment_limit]
        enrich_symbols = {
            str(item.get("symbol") or "").upper() for item in enrich_candidates
        }
        profiles: dict[str, dict] = {}
        histories: dict[str, list] = {}
        for symbol in sorted(enrich_symbols):
            if not symbol:
                continue
            profile = self._optional_get("/stock/profile2", {"symbol": symbol})
            profiles[symbol] = profile if isinstance(profile, dict) else {}
            history = self._optional_get("/stock/earnings", {"symbol": symbol, "limit": 5})
            histories[symbol] = history if isinstance(history, list) else []

        normalized: list[EarningsEvent] = []
        for raw in raw_items:
            symbol = str(raw.get("symbol") or "").upper().strip()
            try:
                event_date = date.fromisoformat(str(raw.get("date")))
            except (TypeError, ValueError):
                continue
            if not symbol:
                continue
            eps_estimate = safe_number(raw.get("epsEstimate"))
            eps_actual = safe_number(raw.get("epsActual"))
            revenue_estimate = safe_number(raw.get("revenueEstimate"), digits=2)
            revenue_actual = safe_number(raw.get("revenueActual"), digits=2)
            has_estimate = eps_estimate is not None or revenue_estimate is not None
            raw_hour = raw.get("hour")
            hour_code, _, _ = normalize_session(raw_hour, event_date)
            if (
                hour_code == "unspecified"
                and not has_estimate
                and eps_actual is None
                and revenue_actual is None
            ):
                continue
            timing = calculate_remaining(
                event_date,
                raw_hour,
                now=current,
                results_available=eps_actual is not None or revenue_actual is not None,
            )
            risk = earnings_risk(timing)
            eps_surprise, eps_surprise_pct = _surprise(eps_actual, eps_estimate)
            revenue_surprise, revenue_surprise_pct = _surprise(
                revenue_actual, revenue_estimate
            )
            result_status, result_label = classify_result(
                eps_actual, eps_estimate, revenue_actual, revenue_estimate
            )
            profile = profiles.get(symbol, {})
            market_cap_millions = safe_number(profile.get("marketCapitalization"), digits=4)
            market_cap = (
                round(market_cap_millions * 1_000_000, 2)
                if market_cap_millions is not None else None
            )
            appointment_status = (
                "تم الإعلان"
                if timing["has_passed"]
                else "الموعد المتوقع"
            )
            normalized.append(
                EarningsEvent(
                    symbol=symbol,
                    company_name=str(profile.get("name") or "").strip() or None,
                    earnings_date=event_date,
                    quarter=safe_integer(raw.get("quarter")),
                    fiscal_year=safe_integer(raw.get("year")),
                    eps_estimate=eps_estimate,
                    eps_actual=eps_actual,
                    eps_previous=self._previous_eps(histories.get(symbol), event_date),
                    revenue_estimate=revenue_estimate,
                    revenue_actual=revenue_actual,
                    eps_surprise=eps_surprise,
                    eps_surprise_percent=eps_surprise_pct,
                    revenue_surprise=revenue_surprise,
                    revenue_surprise_percent=revenue_surprise_pct,
                    number_of_analysts=safe_integer(raw.get("numberOfAnalysts")),
                    market_cap=market_cap,
                    sector=str(profile.get("finnhubIndustry") or "").strip() or None,
                    last_updated=current,
                    appointment_status_ar=appointment_status,
                    result_status=result_status if timing["has_passed"] else "pending",
                    result_label_ar=result_label if timing["has_passed"] else "لم تصدر النتائج",
                    is_watchlist=symbol in watch,
                    scenarios=build_scenarios(),
                    **timing,
                    **risk,
                )
            )
        normalized.sort(
            key=lambda item: (
                item.earnings_date,
                0 if item.is_watchlist else 1,
                -(item.market_cap or 0),
                item.symbol,
            )
        )
        return normalized
