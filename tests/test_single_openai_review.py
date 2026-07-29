from app.config import Settings
from app.opportunities import openai_review


def analysis(valid: bool) -> dict:
    return {
        "symbol": "NVDA",
        "status": "conditional_entry" if valid else "no_trade",
        "strategy": {"id": "volume_breakout", "reason": "اختبار"},
        "trend": "توافق صاعد متعدد الفريمات.",
        "market": {"regime": "bullish"},
        "quote": {"price": 100, "bid": 99.9, "ask": 100.1},
        "indicators": {"rsi": 55},
        "trade_plan": {"entry_from": 100.1, "stop": 98, "targets": [104]},
        "news": [],
        "data_quality": {"valid_for_plan": valid},
    }


def test_invalid_data_never_calls_openai(monkeypatch, db_session):
    monkeypatch.setattr(
        openai_review, "review_candidates",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call OpenAI")),
    )
    result = openai_review.review_single_analysis(
        db_session, Settings(openai_api_key="secret"), analysis(False)
    )
    assert result["status"] == "skipped_invalid_data"
    assert result["ai_calls"] == 0


def test_valid_data_enters_structured_openai_review(monkeypatch, db_session):
    review = openai_review.CandidateReview(
        symbol="NVDA", approved=True, strategy_id="volume_breakout",
        confidence_label="متوسطة", reasons_ar=["متوافق"],
        warnings_ar=[], analysis_summary_ar="مراجعة نقدية مكتملة",
    )
    monkeypatch.setattr(openai_review, "review_candidates", lambda *args, **kwargs: {"NVDA": review})
    result = openai_review.review_single_analysis(
        db_session, Settings(openai_api_key="secret"), analysis(True)
    )
    assert result["status"] == "completed"
    assert result["ai_calls"] == 1
    assert result["model_name"] == "gpt-5-mini"
