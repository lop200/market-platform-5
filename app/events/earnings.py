from __future__ import annotations

from datetime import date, timedelta

import httpx

from app.config import Settings


def fetch_earnings_calendar(settings: Settings, *, start: date | None = None) -> list[dict]:
    """Fetch bounded company-earnings data; callers persist it outside web rendering."""
    if settings.earnings_provider.lower() != "finnhub" or not settings.finnhub_api_key:
        return []
    first = start or date.today()
    last = first + timedelta(days=14)
    with httpx.Client(timeout=settings.external_timeout_seconds) as client:
        response = client.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": first.isoformat(), "to": last.isoformat(), "token": settings.finnhub_api_key},
        )
        response.raise_for_status()
    items = response.json().get("earningsCalendar") or []
    return [
        {
            "symbol": str(item.get("symbol") or "").upper(),
            "date": item.get("date"),
            "hour": item.get("hour") or "dmh",
            "eps_estimate": item.get("epsEstimate"),
            "revenue_estimate": item.get("revenueEstimate"),
            "quarter": item.get("quarter"),
            "year": item.get("year"),
            "risk_ar": "خطر فجوة سعرية وIV Crush — لا يُضمن الوقف عبر إعلان الأرباح",
        }
        for item in items[:100]
        if item.get("symbol") and item.get("date")
    ]

