from __future__ import annotations

from app.config import Settings
from app.db.models import AIAnalysisLog
from app.opportunities.openai_review import fingerprint, review_candidates


def test_unchanged_data_is_not_sent_to_openai(db_session, monkeypatch):
    candidate = {"symbol": "TEST", "price": 3.2}
    db_session.add(AIAnalysisLog(
        symbol="TEST", prompt_version="stock-review-v1", model_name="gpt-5-mini",
        data_fingerprint=fingerprint(candidate), status="completed",
    ))
    db_session.commit()
    monkeypatch.setattr(
        "app.opportunities.openai_review.OpenAI",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not instantiate client")),
    )
    assert review_candidates(db_session, Settings(openai_api_key="secret"), [candidate]) == {}
