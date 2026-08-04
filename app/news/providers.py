from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import Settings
from app.news.classification import classify_event, score_event
from app.news.entities import company_story_matches
from app.news.schemas import NewsEvent


def _id(source: str, key: Any) -> str:
    return hashlib.sha256(f"{source}:{key}".encode()).hexdigest()[:32]


def _clean(value: Any, limit: int = 2000) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def _event(
    *,
    source_type: str,
    source_name: str,
    source_url: str | None,
    published_at: datetime,
    headline: str,
    summary: str,
    symbols: list[str],
    official: bool,
    reliability: int | None,
    reliability_reason_ar: str = "",
    market_scope: str = "company",
    filing_form: str | None = None,
    received_at: datetime | None = None,
) -> NewsEvent:
    now = received_at or datetime.now(timezone.utc)
    published = published_at if published_at.tzinfo else published_at.replace(tzinfo=timezone.utc)
    event_type = classify_event(f"{headline} {summary}", filing_form=filing_form)
    if event_type == "other" and market_scope == "company" and symbols:
        event_type = "company_specific"
    sentiment, impact, urgency, flags, impact_reason = score_event(
        event_type, official=official, source_type=source_type, text=f"{headline} {summary}"
    )
    return NewsEvent(
        id=_id(source_type, source_url or f"{headline}:{published.isoformat()}"),
        source_type=source_type,
        source_name=_clean(source_name, 120) or source_type.upper(),
        source_url=source_url,
        published_at=published,
        received_at=now,
        age_seconds=max(0, int((now - published.astimezone(timezone.utc)).total_seconds())),
        headline=_clean(headline, 500),
        summary=_clean(summary, 1200),
        raw_text=_clean(summary, 2000),
        symbols=sorted({s.upper() for s in symbols if s}),
        market_scope=market_scope,
        event_type=event_type,
        sentiment=sentiment,
        impact_score=impact,
        reliability_score=reliability,
        urgency_score=urgency,
        score_status="scored" if impact is not None and reliability is not None else "unscored",
        impact_reason_ar=impact_reason,
        reliability_reason_ar=reliability_reason_ar,
        is_official=official,
        verified_at=now if official else None,
        risk_flags=flags,
    )


class FinnhubCompanyNewsProvider:
    name = "Finnhub"

    def __init__(self, settings: Settings):
        self.settings = settings

    def _get(self, path: str, params: dict) -> Any:
        if not self.settings.finnhub_api_key:
            return []
        with httpx.Client(
            timeout=self.settings.external_timeout_seconds,
            headers={"X-Finnhub-Token": self.settings.finnhub_api_key},
        ) as client:
            response = client.get(f"https://finnhub.io/api/v1/{path}", params=params)
            response.raise_for_status()
            return response.json()

    def company(self, symbol: str, now: datetime | None = None) -> list[NewsEvent]:
        current = now or datetime.now(timezone.utc)
        raw = self._get("company-news", {
            "symbol": symbol,
            "from": (current - timedelta(days=5)).date().isoformat(),
            "to": current.date().isoformat(),
        })
        results: list[NewsEvent] = []
        for item in (raw or [])[:30]:
            if not item.get("headline") or not item.get("datetime"):
                continue
            matches, match_reason = company_story_matches(
                symbol, item.get("headline") or "", item.get("summary") or "",
                related=item.get("related"),
            )
            if not matches:
                continue
            source = item.get("source") or "Finnhub"
            source_weight = {
                "reuters": 18, "associated press": 18, "bloomberg": 17,
                "wsj": 16, "cnbc": 13, "yahoo": 8,
            }.get(str(source).lower(), 5)
            reliability = 64 + source_weight
            evidence = [f"وزن سمعة الناشر {source_weight}/18"]
            if item.get("url"):
                reliability += 4
                evidence.append("رابط مصدر متاح")
            if item.get("summary"):
                reliability += 4
                evidence.append("ملخص قابل للفحص")
            row = _event(
                source_type="finnhub",
                source_name=source,
                source_url=item.get("url"),
                published_at=datetime.fromtimestamp(item.get("datetime") or 0, timezone.utc),
                headline=item.get("headline"),
                summary=item.get("summary"),
                symbols=[symbol],
                official=False,
                reliability=min(92, reliability),
                reliability_reason_ar="؛ ".join(evidence),
                received_at=current,
            )
            row.entity_match_reason = match_reason
            results.append(row)
        return results

    def market(self, now: datetime | None = None) -> list[NewsEvent]:
        current = now or datetime.now(timezone.utc)
        raw = self._get("news", {"category": "general", "minId": 0})
        return [
            _event(
                source_type="finnhub",
                source_name=item.get("source") or "Finnhub",
                source_url=item.get("url"),
                published_at=datetime.fromtimestamp(item.get("datetime") or 0, timezone.utc),
                headline=item.get("headline"),
                summary=item.get("summary"),
                symbols=[],
                official=False,
                reliability=70,
                reliability_reason_ar="خبر سوق عام من مزود مجمع؛ لا توجد مطابقة كيان شركة.",
                market_scope="market",
                received_at=current,
            )
            for item in (raw or [])[:40] if item.get("headline") and item.get("datetime")
        ]


class SecEdgarNewsProvider:
    name = "SEC EDGAR"

    def __init__(self, settings: Settings):
        self.settings = settings
        self.headers = {
            "User-Agent": settings.sec_user_agent or "",
            "Accept-Encoding": "gzip, deflate",
        }

    @property
    def enabled(self) -> bool:
        return bool(self.settings.sec_news_enabled and self.settings.sec_user_agent)

    def _get(self, url: str) -> Any:
        if not self.enabled:
            return {}
        with httpx.Client(timeout=self.settings.external_timeout_seconds, headers=self.headers) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.json()

    def company(self, symbol: str, now: datetime | None = None) -> list[NewsEvent]:
        if not self.enabled:
            return []
        current = now or datetime.now(timezone.utc)
        tickers = self._get("https://www.sec.gov/files/company_tickers.json")
        company = next(
            (row for row in tickers.values() if str(row.get("ticker") or "").upper() == symbol.upper()),
            None,
        )
        if not company:
            return []
        cik = str(company["cik_str"]).zfill(10)
        filing = self._get(f"https://data.sec.gov/submissions/CIK{cik}.json")
        recent = (filing.get("filings") or {}).get("recent") or {}
        results: list[NewsEvent] = []
        forms = recent.get("form") or []
        for index, form in enumerate(forms[:80]):
            if form not in {"8-K", "10-Q", "10-K", "S-1", "424B2", "424B3", "424B4", "424B5", "4"}:
                continue
            filed = (recent.get("filingDate") or [""])[index]
            accession = (recent.get("accessionNumber") or [""])[index]
            document = (recent.get("primaryDocument") or [""])[index]
            try:
                published = datetime.fromisoformat(f"{filed}T12:00:00+00:00")
            except ValueError:
                continue
            if (current - published).days > 7:
                continue
            accession_path = accession.replace("-", "")
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_path}/{document}"
            results.append(_event(
                source_type="sec",
                source_name="SEC EDGAR",
                source_url=url,
                published_at=published,
                headline=f"{symbol}: إيداع رسمي {form}",
                summary=f"إيداع {form} رسمي للشركة لدى هيئة الأوراق المالية الأمريكية.",
                symbols=[symbol],
                official=True,
                reliability=100,
                filing_form=form,
                received_at=current,
            ))
        return results[:20]


class XTrustedNewsProvider:
    name = "X API"

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def enabled(self) -> bool:
        return bool(
            self.settings.x_news_enabled
            and self.settings.x_api_bearer_token
            and self.settings.configured_x_accounts
            and self.settings.x_daily_read_limit > 0
        )

    def trusted_posts(self, now: datetime | None = None) -> list[NewsEvent]:
        if not self.enabled:
            return []
        current = now or datetime.now(timezone.utc)
        accounts = self.settings.configured_x_accounts
        with httpx.Client(
            timeout=self.settings.external_timeout_seconds,
            headers={"Authorization": f"Bearer {self.settings.x_api_bearer_token}"},
        ) as client:
            response = client.get(
                "https://api.x.com/2/users/by",
                params={"usernames": ",".join(accounts), "user.fields": "verified"},
            )
            response.raise_for_status()
            users = response.json().get("data") or []
            results: list[NewsEvent] = []
            for user in users:
                posts = client.get(
                    f"https://api.x.com/2/users/{user['id']}/tweets",
                    params={
                        "max_results": max(5, min(100, self.settings.x_max_posts_per_query)),
                        "exclude": "replies,retweets",
                        "tweet.fields": "created_at,lang",
                    },
                )
                posts.raise_for_status()
                for item in posts.json().get("data") or []:
                    text = _clean(item.get("text"), 500)
                    keywords = self.settings.configured_x_keywords
                    if keywords and not any(word in text.lower() for word in keywords):
                        continue
                    published = datetime.fromisoformat(str(item["created_at"]).replace("Z", "+00:00"))
                    results.append(_event(
                        source_type="x",
                        source_name=f"@{user['username']}",
                        source_url=f"https://x.com/{user['username']}/status/{item['id']}",
                        published_at=published,
                        headline=text,
                        summary=text,
                        symbols=[],
                        official=bool(user.get("verified")),
                        reliability=90 if user.get("verified") else 60,
                        market_scope="market",
                        received_at=current,
                    ))
            return results
