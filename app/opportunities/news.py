from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

import httpx

from app.config import Settings
from app.opportunities.schemas import NewsItemData


def classify_news_text(headline: str) -> tuple[str, list[str]]:
    lower = headline.lower()
    risk_words = ("offering", "dilution", "reverse split", "bankruptcy", "going concern")
    flags = [word for word in risk_words if word in lower]
    if flags:
        return "سلبي قوي", flags
    if any(word in lower for word in ("approval", "contract award", "beats estimates")):
        return "إيجابي", []
    return "محايد", []


class NewsProvider(ABC):
    @abstractmethod
    def get_news(self, symbol: str) -> list[NewsItemData]: ...

    @property
    @abstractmethod
    def provider_name(self) -> str: ...


class NoNewsProvider(NewsProvider):
    provider_name = "غير متوفر"

    def get_news(self, symbol: str) -> list[NewsItemData]:
        return []


class FinnhubNewsProvider(NewsProvider):
    provider_name = "finnhub"

    def __init__(self, api_key: str, timeout: float):
        self.api_key = api_key
        self.timeout = timeout

    def get_news(self, symbol: str) -> list[NewsItemData]:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=5)
        response = httpx.get(
            "https://finnhub.io/api/v1/company-news",
            params={"symbol": symbol, "from": start.date().isoformat(), "to": now.date().isoformat(), "token": self.api_key},
            timeout=self.timeout,
        )
        response.raise_for_status()
        items = []
        for raw in response.json()[:10]:
            headline = str(raw.get("headline") or "")
            lower = headline.lower()
            classification, flags = classify_news_text(headline)
            items.append(
                NewsItemData(
                    headline=headline[:500],
                    source=str(raw.get("source") or "unknown")[:80],
                    published_at=datetime.fromtimestamp(raw.get("datetime", 0), timezone.utc),
                    url=raw.get("url"),
                    classification=classification,
                    is_official="sec" in lower or "company" in str(raw.get("source", "")).lower(),
                    risk_flags=flags,
                )
            )
        return items


def get_news_provider(settings: Settings) -> NewsProvider:
    if settings.news_provider.lower() == "finnhub" and settings.finnhub_api_key:
        return FinnhubNewsProvider(settings.finnhub_api_key, settings.external_timeout_seconds)
    return NoNewsProvider()
