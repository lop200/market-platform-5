"""Earnings providers and deterministic event-risk classification."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from app.config import Settings


@dataclass(frozen=True)
class EarningsEvent:
    symbol: str
    company_name: str
    announced_at: datetime
    timing: str
    eps_estimate: float | None = None
    revenue_estimate: float | None = None
    previous_eps: float | None = None
    source: str = "manual"
    source_url: str | None = None

    def as_dict(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        announced = self.announced_at
        if announced.tzinfo is None:
            announced = announced.replace(tzinfo=timezone.utc)
        hours = (announced - now).total_seconds() / 3600
        return {
            **asdict(self), "announced_at": announced.isoformat(),
            "hours_remaining": round(hours, 1), "risk": event_risk(hours),
            "gap_warning": hours <= 168, "confidence_penalty": confidence_penalty(hours),
        }


def event_risk(hours: float) -> str:
    if hours < 0:
        return "post_event"
    if hours < 6:
        return "critical"
    if hours < 24:
        return "very_high"
    if hours < 48:
        return "high"
    if hours < 168:
        return "elevated"
    return "normal"


def confidence_penalty(hours: float) -> int:
    return 35 if 0 <= hours < 6 else 25 if hours < 24 else 15 if hours < 48 else 8 if hours < 168 else 0


class EarningsProvider(ABC):
    provider_name = "unknown"

    @abstractmethod
    def fetch(self, start: datetime, end: datetime) -> list[EarningsEvent]: ...


class ManualEarningsProvider(EarningsProvider):
    provider_name = "manual"

    def fetch(self, start: datetime, end: datetime) -> list[EarningsEvent]:
        return []


class FinnhubEarningsProvider(EarningsProvider):
    provider_name = "finnhub"

    def __init__(self, api_key: str, timeout: float = 12):
        self.api_key, self.timeout = api_key, timeout

    def fetch(self, start: datetime, end: datetime) -> list[EarningsEvent]:
        response = requests.get(
            "https://finnhub.io/api/v1/calendar/earnings",
            params={"from": start.date().isoformat(), "to": end.date().isoformat(), "token": self.api_key},
            timeout=self.timeout,
        )
        response.raise_for_status()
        events = []
        for row in response.json().get("earningsCalendar", []):
            events.append(EarningsEvent(
                symbol=row["symbol"], company_name=row.get("symbol", ""),
                announced_at=datetime.fromisoformat(row["date"]).replace(tzinfo=timezone.utc),
                timing=row.get("hour") or "unknown", eps_estimate=row.get("epsEstimate"),
                revenue_estimate=row.get("revenueEstimate"), previous_eps=row.get("epsActual"),
                source=self.provider_name,
            ))
        return events


class FMPEarningsProvider(EarningsProvider):
    provider_name = "fmp"

    def __init__(self, api_key: str, timeout: float = 12):
        self.api_key, self.timeout = api_key, timeout

    def fetch(self, start: datetime, end: datetime) -> list[EarningsEvent]:
        response = requests.get(
            "https://financialmodelingprep.com/stable/earnings-calendar",
            params={"from": start.date().isoformat(), "to": end.date().isoformat(), "apikey": self.api_key},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return [
            EarningsEvent(
                symbol=row["symbol"], company_name=row.get("symbol", ""),
                announced_at=datetime.fromisoformat(row["date"]).replace(tzinfo=timezone.utc),
                timing=row.get("time") or "unknown", eps_estimate=row.get("epsEstimated"),
                revenue_estimate=row.get("revenueEstimated"), previous_eps=row.get("eps"),
                source=self.provider_name,
            )
            for row in response.json() if row.get("symbol") and row.get("date")
        ]


class AlphaVantageEarningsProvider(EarningsProvider):
    provider_name = "alpha_vantage"

    def __init__(self, api_key: str, timeout: float = 12):
        self.api_key, self.timeout = api_key, timeout

    def fetch(self, start: datetime, end: datetime) -> list[EarningsEvent]:
        response = requests.get(
            "https://www.alphavantage.co/query",
            params={"function": "EARNINGS_CALENDAR", "horizon": "3month", "apikey": self.api_key},
            timeout=self.timeout,
        )
        response.raise_for_status()
        lines = response.text.splitlines()
        if len(lines) < 2:
            return []
        headers, events = lines[0].split(","), []
        for line in lines[1:]:
            row = dict(zip(headers, line.split(",")))
            if not row.get("symbol") or not row.get("reportDate"):
                continue
            announced = datetime.fromisoformat(row["reportDate"]).replace(tzinfo=timezone.utc)
            if start <= announced <= end:
                events.append(EarningsEvent(
                    symbol=row["symbol"], company_name=row.get("name") or row["symbol"],
                    announced_at=announced, timing="unknown",
                    eps_estimate=float(row["estimate"]) if row.get("estimate") else None,
                    source=self.provider_name,
                ))
        return events


def get_earnings_provider(settings: Settings) -> EarningsProvider:
    name = settings.earnings_provider.lower()
    if name == "finnhub" and settings.finnhub_api_key:
        return FinnhubEarningsProvider(settings.finnhub_api_key, settings.external_timeout_seconds)
    if name == "fmp" and settings.fmp_api_key:
        return FMPEarningsProvider(settings.fmp_api_key, settings.external_timeout_seconds)
    if name in {"alpha_vantage", "alphavantage"} and settings.alpha_vantage_api_key:
        return AlphaVantageEarningsProvider(settings.alpha_vantage_api_key, settings.external_timeout_seconds)
    return ManualEarningsProvider()
