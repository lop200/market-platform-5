from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.advisor.schemas import AdvisorExplanation
from app.advisor.service import (
    INSUFFICIENT_MESSAGE,
    MARKET_ONLY_MESSAGE,
    advisor_context,
    ask_advisor,
)
from app.config import Settings
from app.db.models import StockCandidate, StockScanRun
from app.main import app


def advisor_settings(**overrides) -> Settings:
    values = {
        "financial_advisor_enabled": True,
        "financial_advisor_markets_only": True,
        "financial_advisor_max_input_chars": 500,
        "financial_advisor_max_contracts": 3,
        "financial_advisor_max_news_items": 5,
        "financial_advisor_cache_seconds": 60,
        "openai_api_key": "",
        "default_daily_cap_usd": 10,
        "default_monthly_cap_usd": 100,
    }
    values.update(overrides)
    return Settings(**values)


def save_analysis(db_session) -> None:
    run = StockScanRun(status="completed", task_type="symbol", provider="alpaca")
    db_session.add(run)
    db_session.flush()
    db_session.add(
        StockCandidate(
            scan_run_id=run.id,
            symbol="NVDA",
            accepted=True,
            numeric_score=82,
            snapshot_json={
                "symbol": "NVDA",
                "trend": "صاعد",
                "overall_score": 82,
                "quote": {"price": 181.25, "age_seconds": 4},
                "indicators": {"rsi": 58.4, "vwap": 180.9},
                "news": [
                    {"headline": f"خبر {index}", "source": "Finnhub", "age_seconds": 30}
                    for index in range(7)
                ],
                "earnings": {"remaining_hours": 96},
                "warnings_ar": ["Paper Trading فقط"],
                "options": {
                    "ranked_contracts": [
                        {
                            "symbol": f"NVDA260821C00{180 + index}000",
                            "option_type": "call",
                            "strike": 180 + index,
                            "score": 80 - index,
                        }
                        for index in range(5)
                    ],
                    "rejection_reasons": {"wide_spread": 2},
                    "warnings_ar": ["أعد فحص Bid/Ask قبل الدخول"],
                },
            },
        )
    )
    db_session.commit()


def test_advisor_rejects_topics_outside_markets(db_session):
    result = ask_advisor(
        db_session,
        advisor_settings(),
        question="اكتب لي وصفة عشاء",
        symbol="NVDA",
    )
    assert result["status"] == "out_of_scope"
    assert result["answer_ar"] == MARKET_ONLY_MESSAGE


def test_advisor_returns_safe_answer_when_platform_data_is_missing(db_session):
    result = ask_advisor(
        db_session,
        advisor_settings(),
        question="ما أفضل عقد للمراقبة؟",
        symbol="NVDA",
    )
    assert result["status"] == "insufficient_data"
    assert result["answer_ar"] == INSUFFICIENT_MESSAGE


def test_advisor_context_is_bounded_and_uses_market_clocks(db_session):
    save_analysis(db_session)
    context = advisor_context(db_session, advisor_settings(), "NVDA")
    assert context["selected_symbol"] == "NVDA"
    assert context["stock_price"] == 181.25
    assert context["stock_data_age_seconds"] == 4
    assert len(context["best_contracts"]) == 3
    assert len(context["important_news"]) == 5
    assert context["new_york_time"]
    assert context["riyadh_time"]
    assert context["session"]["stock_status"]


def test_advisor_uses_structured_output_without_inventing_contracts(
    db_session, monkeypatch
):
    save_analysis(db_session)
    expected_symbol = "NVDA260821C00180000"
    parsed = AdvisorExplanation(
        answer_ar="العقد الأول هو الأفضل للمراقبة وفق ترتيب المنصة.",
        conclusion_ar="انتظر تحقق شرط السهم وأعد فحص السبريد.",
        referenced_contract_symbols=[expected_symbol],
        risk_warnings_ar=["Paper Trading فقط"],
    )

    class FakeResponses:
        def parse(self, **kwargs):
            assert kwargs["text_format"] is AdvisorExplanation
            assert "CONTEXT" in kwargs["input"][1]["content"]
            return SimpleNamespace(output_parsed=parsed)

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    monkeypatch.setattr("app.advisor.service.OpenAI", FakeOpenAI)
    result = ask_advisor(
        db_session,
        advisor_settings(openai_api_key="test-key"),
        question="ما أفضل عقد للمراقبة؟",
        symbol="NVDA",
    )
    assert result["status"] == "completed"
    assert result["referenced_contract_symbols"] == [expected_symbol]
    assert result["paper_trading_only"] is True


def test_advisor_rejects_an_invented_contract_symbol(db_session, monkeypatch):
    save_analysis(db_session)
    parsed = AdvisorExplanation(
        answer_ar="عقد غير موجود.",
        conclusion_ar="لا تستخدمه.",
        referenced_contract_symbols=["INVENTED"],
    )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            self.responses = SimpleNamespace(
                parse=lambda **kwargs: SimpleNamespace(output_parsed=parsed)
            )

    monkeypatch.setattr("app.advisor.service.OpenAI", FakeOpenAI)
    result = ask_advisor(
        db_session,
        advisor_settings(openai_api_key="test-key"),
        question="هل الأفضل Call أو Put؟",
        symbol="NVDA",
    )
    assert result["status"] == "failed"
    assert result["answer_ar"] == INSUFFICIENT_MESSAGE


def test_home_contains_fixed_mobile_safe_advisor_widget():
    html = TestClient(app).get("/").text
    assert 'id="advisor"' in html
    assert "اسأل المستشار" in html
    assert 'maxlength="500"' in html
    assert "position:fixed" in html
    assert "overflow-x:hidden" in html
    assert "@media(max-width:719px)" in html
    assert "/api/v1/advisor/context" in html
    assert "/api/v1/advisor/ask" in html
    assert "function renderAdvisorAnswer" in html
    assert "advisorAnswer.innerHTML" not in html
