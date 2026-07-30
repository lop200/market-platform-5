from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import repository
from app.db.models import StockOpportunity
from app.main import app
from app.news.classification import (
    apply_safety,
    classify_event,
    deduplicate,
)
from app.news.providers import (
    FinnhubCompanyNewsProvider,
    SecEdgarNewsProvider,
    XTrustedNewsProvider,
)
from app.news.schemas import NewsEvent
from app.news.service import UnifiedNewsService


NOW = datetime(2026, 7, 30, 18, 0, tzinfo=timezone.utc)


def event(
    headline: str,
    *,
    source_type: str = "finnhub",
    source_name: str = "Reuters",
    official: bool = False,
    reliability: int = 78,
    event_type: str = "other",
    sentiment: str = "neutral",
    impact: int = 70,
    symbol: str = "AAPL",
    minutes_ago: int = 5,
) -> NewsEvent:
    return NewsEvent(
        id=f"{source_type}-{headline}",
        source_type=source_type,
        source_name=source_name,
        source_url=f"https://example.com/{source_type}/{len(headline)}",
        published_at=NOW - timedelta(minutes=minutes_ago),
        received_at=NOW,
        age_seconds=minutes_ago * 60,
        headline=headline,
        summary=headline,
        symbols=[symbol] if symbol else [],
        event_type=event_type,
        sentiment=sentiment,
        impact_score=impact,
        reliability_score=reliability,
        urgency_score=impact,
        is_official=official,
    )


def test_official_sec_filing_is_normalized(monkeypatch):
    provider = SecEdgarNewsProvider(
        Settings(sec_news_enabled=True, sec_user_agent="Market Platform admin@example.com")
    )

    def fake_get(url):
        if url.endswith("company_tickers.json"):
            return {"0": {"ticker": "AAPL", "cik_str": 320193}}
        return {
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "filingDate": ["2026-07-30"],
                    "accessionNumber": ["0000320193-26-000001"],
                    "primaryDocument": ["aapl-8k.htm"],
                }
            }
        }

    monkeypatch.setattr(provider, "_get", fake_get)
    rows = provider.company("AAPL", now=NOW)
    assert rows[0].source_type == "sec"
    assert rows[0].is_official is True
    assert rows[0].reliability_score == 100
    assert rows[0].event_type == "sec_filing"


def test_finnhub_company_news_is_normalized(monkeypatch):
    provider = FinnhubCompanyNewsProvider(Settings(finnhub_api_key="key"))
    monkeypatch.setattr(provider, "_get", lambda *_args, **_kwargs: [{
        "headline": "Apple reports quarterly earnings",
        "summary": "Revenue and earnings were released.",
        "source": "Reuters",
        "url": "https://example.com/apple",
        "datetime": int(NOW.timestamp()),
    }])
    row = provider.company("AAPL", now=NOW)[0]
    assert row.symbols == ["AAPL"]
    assert row.event_type == "earnings"
    assert row.source_type == "finnhub"


def test_x_requires_token_allowlist_and_explicit_enablement():
    disabled = XTrustedNewsProvider(Settings(
        x_news_enabled=True,
        x_api_bearer_token="token",
        x_allowed_accounts="",
    ))
    trusted = XTrustedNewsProvider(Settings(
        x_news_enabled=True,
        x_api_bearer_token="token",
        x_allowed_accounts="federalreserve,secgov",
        x_daily_read_limit=10,
    ))
    assert disabled.enabled is False
    assert trusted.enabled is True
    assert "unknown_account" not in trusted.settings.configured_x_accounts


def test_untrusted_x_post_is_not_execution_grade():
    row = apply_safety(event(
        "Rumor about a takeover",
        source_type="x",
        source_name="@unknown",
        reliability=30,
        event_type="rumor",
        impact=90,
    ))
    assert row.prevent_entry is False
    assert row.raise_risk is True
    assert row.status_message_ar == "خبر غير مؤكد — لا يعتمد عليه للتنفيذ"


def test_duplicate_keeps_more_reliable_and_lists_confirmation():
    official = event(
        "Company announces public offering",
        source_type="sec",
        source_name="SEC EDGAR",
        official=True,
        reliability=100,
        event_type="offering",
        sentiment="negative",
        impact=97,
    )
    second = event(
        "Company announces a public offering",
        source_name="Reuters",
        event_type="offering",
        minutes_ago=6,
    )
    rows = deduplicate([second, official])
    assert len(rows) == 1
    assert rows[0].source_name == "SEC EDGAR"
    assert rows[0].confirming_sources[0]["source_name"] == "Reuters"


def test_conflicting_sources_are_marked():
    positive = event("Company updates guidance", sentiment="positive", event_type="guidance")
    negative = event(
        "Company updates its guidance",
        source_name="Bloomberg",
        sentiment="negative",
        event_type="guidance",
    )
    rows = deduplicate([positive, negative])
    assert len(rows) == 1
    assert rows[0].conflict_warning is True
    assert rows[0].status_message_ar == "مصادر متعارضة — انتظر التأكيد"


def test_python_classifies_offering_earnings_and_fed():
    assert classify_event("announces registered direct public offering") == "offering"
    assert classify_event("quarterly earnings and financial results") == "earnings"
    assert classify_event("Federal Reserve FOMC statement") == "fed"


def test_new_offering_invalidates_old_opportunity():
    row = event(
        "Company announces public offering",
        source_type="sec",
        official=True,
        reliability=100,
        event_type="offering",
        sentiment="negative",
        impact=97,
    )
    row = apply_safety(
        row,
        analysis_direction="bullish",
        analysis_issued_at=NOW - timedelta(hours=1),
    )
    assert row.prevent_entry is True
    assert row.invalidates_previous_analysis is True
    assert row.contradicts_technical_scenario is True


def test_new_strong_news_marks_saved_opportunity_for_reanalysis(db_session):
    opportunity = StockOpportunity(
        symbol="AAPL",
        company_name="Apple",
        status="conditional_entry",
        strategy_id="breakout",
        market_regime="bullish",
        issued_at=NOW - timedelta(hours=2),
        expires_at=NOW + timedelta(hours=1),
        quote_timestamp=NOW - timedelta(hours=2),
        price_at_analysis=200,
        entry_from=201,
        entry_to=202,
        stop_loss=195,
        risk_reward=2,
        overall_score=80,
        result_json={"status": "conditional_entry", "trade_plan": {"entry": 201}},
        data_fingerprint="n" * 64,
    )
    db_session.add(opportunity)
    db_session.commit()
    news = event(
        "Company announces public offering",
        source_type="sec",
        source_name="SEC EDGAR",
        official=True,
        reliability=100,
        event_type="offering",
        sentiment="negative",
        impact=97,
    )
    UnifiedNewsService(db_session, Settings())._store_selected([news])
    db_session.refresh(opportunity)
    assert opportunity.status == "needs_news_reanalysis"
    assert opportunity.result_json["trade_plan"] is None


def test_fed_news_enters_spx_context(db_session):
    service = UnifiedNewsService(db_session, Settings())
    fed = event(
        "Federal Reserve signals interest rate decision",
        source_type="x",
        source_name="@federalreserve",
        official=True,
        reliability=100,
        event_type="fed",
        impact=95,
        symbol="",
    )
    payload = {
        "items": [fed.model_dump(mode="json")],
        "updated_at": NOW.isoformat(),
        "source_status": {"x": "connected"},
        "usage": {},
    }
    repository.cache_set(
        db_session,
        "news:market:pulse",
        payload,
        NOW + timedelta(hours=1),
    )
    context = service.spx_context()
    assert context["items"][0]["spx_impact_score"] == 100
    assert context["items"][0]["potential_direction_ar"] == "غير واضح"


def test_provider_failures_return_empty_without_breaking_analysis(db_session):
    service = UnifiedNewsService(db_session, Settings())
    service.finnhub.company = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("down"))
    service.sec.company = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("down"))
    assert service.get_symbol_news("AAPL", force=True) == []
    service.finnhub.market = lambda: (_ for _ in ()).throw(RuntimeError("down"))
    snapshot = service.refresh_market(force=True)
    assert snapshot["items"] == []
    assert snapshot["last_error"]


def test_x_daily_limit_stops_reads(db_session):
    settings = Settings(
        x_news_enabled=True,
        x_api_bearer_token="token",
        x_allowed_accounts="federalreserve",
        x_daily_read_limit=1,
    )
    service = UnifiedNewsService(db_session, settings)
    repository.cache_set(
        db_session,
        f"news:usage:{datetime.now(timezone.utc).date().isoformat()}",
        {"x_reads": 1},
        datetime.now(timezone.utc) + timedelta(days=1),
    )
    service.finnhub.market = lambda: []
    called = {"x": 0}
    service.x.trusted_posts = lambda: called.__setitem__("x", called["x"] + 1) or []
    result = service.refresh_market(force=True)
    assert called["x"] == 0
    assert result["source_status"]["x"] == "disabled_or_budget"


def test_openai_is_not_required_for_news_or_spx(db_session):
    service = UnifiedNewsService(db_session, Settings(openai_api_key=None))
    service.finnhub.market = lambda: []
    result = service.refresh_market(force=True)
    assert result["usage"]["openai_calls"] == 0
    assert service.spx_context()["items"] == []


def test_news_and_spx_pages_are_rtl_and_responsive():
    client = TestClient(app)
    news = client.get("/news")
    spx = client.get("/spx")
    assert news.status_code == spx.status_code == 200
    assert 'dir="rtl"' in news.text and "overflow-x:hidden" in news.text
    assert "نبض السوق" in news.text
    assert "قنّاص SPX" in spx.text and "overflow-x:hidden" in spx.text
