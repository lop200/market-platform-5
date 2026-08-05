from __future__ import annotations

from app.config import Settings
from app.db.models import AIAnalysisLog
from app.opportunities.openai_review import fingerprint, review_candidates


def test_legacy_fingerprint_without_structured_result_is_not_treated_as_cache(db_session, monkeypatch):
    candidate = {"symbol": "TEST", "price": 3.2}
    db_session.add(AIAnalysisLog(
        symbol="TEST", prompt_version="stock-review-v1", model_name="gpt-5-mini",
        data_fingerprint=fingerprint(candidate), status="completed",
    ))
    db_session.commit()
    calls = []

    class FailingResponses:
        def parse(self, **_kwargs):
            calls.append("api")
            raise RuntimeError("test failure")

    monkeypatch.setattr(
        "app.opportunities.openai_review.OpenAI",
        lambda **kwargs: type("Client", (), {"responses": FailingResponses()})(),
    )
    assert review_candidates(db_session, Settings(openai_api_key="secret"), [candidate]) == {}
    assert calls == ["api"]
