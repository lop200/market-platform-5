from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db.models import CacheEntry, OpenAICallLog, StockCandidate, StockScanRun
from app.db.session import Base, get_db
from app.api.routes_prices import price_events
from app.main import app
from app.opportunities import openai_review
from app.opportunities.openai_review import CandidateReview, ReviewBatch, review_candidates
from app.opportunities.jobs import _create_claimed_run


def candidate(session: str = "regular", timestamp: str = "2026-08-05T15:00:10+00:00") -> dict:
    return {
        "symbol": "NVDA",
        "status": "conditional_entry",
        "strategy_id": "volume_breakout",
        "strategy_reason": "اختراق مؤكد محليًا",
        "trend": "صاعد",
        "market_regime": "bullish",
        "quote": {
            "price": 180, "bid": 179.95, "ask": 180.05, "spread_pct": .06,
            "quote_timestamp": timestamp, "market_session": session,
            "feed": "sip", "age_seconds": 2,
        },
        "indicators": {
            "rsi": 57, "macd": 1.2, "relative_volume": 1.8,
            "ema9": 179, "ema20": 177, "ema50": 172,
        },
        "trade_plan": {
            "direction": "long", "entry_from": 180.1, "stop": 177,
            "targets": [{"price": 186}], "risk_reward": 1.9,
        },
        "ranked_option_contracts": [],
    }


class CountingOpenAI:
    calls = 0
    delay = 0.0
    fail = False
    init_kwargs: list[dict] = []
    parse_kwargs: list[dict] = []

    def __init__(self, **kwargs):
        self.init_kwargs.append(kwargs)
        self.responses = self

    def parse(self, **kwargs):
        type(self).calls += 1
        type(self).parse_kwargs.append(kwargs)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("synthetic failure")
        parsed = ReviewBatch(reviews=[CandidateReview(
            symbol="NVDA", approved=True, strategy_id="volume_breakout",
            confidence_label="متوسطة", reasons_ar=["متوافق"], warnings_ar=[],
            analysis_summary_ar="مراجعة محفوظة",
        )])
        return SimpleNamespace(
            output_parsed=parsed,
            usage=SimpleNamespace(
                input_tokens=900,
                output_tokens=120,
                total_tokens=1020,
                input_tokens_details=SimpleNamespace(cached_tokens=100),
            ),
        )


def reset_fake(*, delay: float = 0, fail: bool = False):
    CountingOpenAI.calls = 0
    CountingOpenAI.delay = delay
    CountingOpenAI.fail = fail
    CountingOpenAI.init_kwargs = []
    CountingOpenAI.parse_kwargs = []


def settings(**overrides) -> Settings:
    values = dict(
        openai_api_key="test-key",
        openai_review_cache_seconds=45,
        openai_lock_seconds=5,
        openai_failure_cooldown_seconds=2,
        openai_daily_budget_usd=1,
        openai_operation_budget_usd=.08,
        openai_scan_budget_usd=.20,
    )
    values.update(overrides)
    return Settings(**values)


def test_refresh_reuses_structured_review_and_central_log(monkeypatch, db_session):
    reset_fake()
    monkeypatch.setattr(openai_review, "OpenAI", CountingOpenAI)
    first_meta, second_meta = {}, {}

    first = review_candidates(db_session, settings(), [candidate()], metadata=first_meta)
    second = review_candidates(db_session, settings(), [candidate()], metadata=second_meta)

    assert first["NVDA"].analysis_summary_ar == second["NVDA"].analysis_summary_ar
    assert CountingOpenAI.calls == 1
    assert first_meta["api_calls"] == 1
    assert second_meta["api_calls"] == 0
    assert second_meta["status"] == "cached"
    log = db_session.scalar(select(OpenAICallLog))
    assert log.endpoint == "/v1/responses"
    assert log.operation == "stock_candidate_review"
    assert log.model_name == settings().openai_model
    assert log.symbols_json == ["NVDA"]
    assert (log.input_tokens, log.output_tokens, log.cached_tokens, log.total_tokens) == (900, 120, 100, 1020)
    assert log.estimated_cost_usd > 0
    assert log.duration_ms >= 0
    assert log.reason == "bounded_review_after_local_filtering"
    assert CountingOpenAI.parse_kwargs[0]["max_output_tokens"] == settings().openai_max_output_tokens


def test_concurrent_identical_requests_make_one_openai_call(monkeypatch, tmp_path):
    reset_fake(delay=.2)
    monkeypatch.setattr(openai_review, "OpenAI", CountingOpenAI)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'openai-lock.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    barrier = threading.Barrier(2)
    results: list[dict] = []

    def worker():
        db = sessions()
        try:
            barrier.wait()
            results.append(review_candidates(db, settings(), [candidate()]))
        finally:
            db.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not any(thread.is_alive() for thread in threads)
    assert CountingOpenAI.calls == 1
    assert len(results) == 2
    assert all(result["NVDA"].analysis_summary_ar == "مراجعة محفوظة" for result in results)
    db = sessions()
    try:
        assert db.scalar(select(func.count(OpenAICallLog.id))) == 1
    finally:
        db.close()


def test_shortlist_candidates_are_sent_in_one_batch(monkeypatch, db_session):
    reset_fake()
    monkeypatch.setattr(openai_review, "OpenAI", CountingOpenAI)
    other = candidate()
    other["symbol"] = "AAPL"

    review_candidates(db_session, settings(), [candidate(), other])

    assert CountingOpenAI.calls == 1
    log = db_session.scalar(select(OpenAICallLog))
    assert log.symbols_json == ["AAPL", "NVDA"]


def test_cache_never_crosses_session_and_expired_entry_is_not_used(monkeypatch, db_session):
    reset_fake()
    monkeypatch.setattr(openai_review, "OpenAI", CountingOpenAI)
    review_candidates(db_session, settings(), [candidate("regular")])
    review_candidates(db_session, settings(), [candidate("after_hours")])
    assert CountingOpenAI.calls == 2
    review_candidates(
        db_session, settings(),
        [candidate("regular", "2026-08-05T15:01:10+00:00")],
    )
    assert CountingOpenAI.calls == 3

    cache_rows = db_session.scalars(
        select(CacheEntry).where(CacheEntry.key.like("openai-review:%"))
    ).all()
    for row in cache_rows:
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()
    review_candidates(db_session, settings(), [candidate("regular")])
    assert CountingOpenAI.calls == 4


def test_failure_cooldown_prevents_unbounded_application_retries(monkeypatch, db_session):
    reset_fake(fail=True)
    monkeypatch.setattr(openai_review, "OpenAI", CountingOpenAI)

    assert review_candidates(db_session, settings(openai_max_retries=2), [candidate()]) == {}
    assert review_candidates(db_session, settings(openai_max_retries=2), [candidate()]) == {}
    assert CountingOpenAI.calls == 1
    assert CountingOpenAI.init_kwargs[0]["max_retries"] == 2
    logs = db_session.scalars(select(OpenAICallLog)).all()
    assert len(logs) == 1 and logs[0].status == "failed"


def test_read_and_status_endpoints_never_instantiate_openai(monkeypatch, db_session):
    def forbidden(**_kwargs):
        raise AssertionError("read-only endpoint must not instantiate OpenAI")

    monkeypatch.setattr(openai_review, "OpenAI", forbidden)
    monkeypatch.setattr("app.spx.review.OpenAI", forbidden)
    run = StockScanRun(status="completed", task_type="market_scan", symbols_total=1)
    db_session.add(run)
    db_session.flush()
    db_session.add(StockCandidate(
        scan_run_id=run.id, symbol="NVDA", accepted=False,
        numeric_score=0, exclusion_reasons=[], snapshot_json={"symbol": "NVDA"},
    ))
    db_session.commit()

    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    try:
        urls = (
            "/api/v1/opportunities/latest",
            "/api/v1/dashboard",
            "/api/v1/prices",
            f"/api/v1/opportunities/scans/{run.id}",
            f"/api/v1/opportunities/stocks/jobs/{run.id}",
        )
        before = db_session.scalar(select(func.count(OpenAICallLog.id))) or 0
        for url in urls:
            assert client.get(url).status_code == 200
        asyncio.run(anext(price_events(None, .001, max_frames=1)))
        after = db_session.scalar(select(func.count(OpenAICallLog.id))) or 0
        assert after == before == 0
    finally:
        app.dependency_overrides.clear()


def test_operation_budget_blocks_before_sdk_call(monkeypatch, db_session):
    reset_fake()
    monkeypatch.setattr(openai_review, "OpenAI", CountingOpenAI)
    meta = {}
    assert review_candidates(
        db_session,
        settings(openai_operation_budget_usd=.001),
        [candidate()],
        metadata=meta,
    ) == {}
    assert CountingOpenAI.calls == 0
    assert meta["status"] == "budget_blocked"
    log = db_session.scalar(select(OpenAICallLog))
    assert log.status == "budget_blocked"


def test_run_and_daily_budgets_block_before_sdk_call(monkeypatch, db_session):
    reset_fake()
    monkeypatch.setattr(openai_review, "OpenAI", CountingOpenAI)
    run_id = "11111111-1111-1111-1111-111111111111"
    db_session.add(OpenAICallLog(
        endpoint="/v1/responses", operation="market_scan_review",
        model_name="gpt-5-mini", symbol="AAPL", symbols_json=["AAPL"],
        run_id=run_id, input_tokens=0, output_tokens=0, cached_tokens=0,
        total_tokens=0, estimated_cost_usd=.19, duration_ms=1,
        reason="seed", status="completed",
    ))
    db_session.commit()
    meta = {}
    review_candidates(
        db_session, settings(), [candidate()], run_id=run_id, metadata=meta
    )
    assert CountingOpenAI.calls == 0
    assert meta["status"] == "budget_blocked"

    reset_fake()
    daily_meta = {}
    review_candidates(
        db_session,
        settings(openai_daily_budget_usd=.20),
        [candidate(timestamp="2026-08-05T15:02:10+00:00")],
        metadata=daily_meta,
    )
    assert CountingOpenAI.calls == 0
    assert daily_meta["status"] == "budget_blocked"


def test_identical_symbol_job_lock_returns_the_existing_run(db_session):
    first, created_first = _create_claimed_run(
        db_session, "job:single-symbol:NVDA",
        task_type="single_symbol", symbols_total=1,
    )
    second, created_second = _create_claimed_run(
        db_session, "job:single-symbol:NVDA",
        task_type="single_symbol", symbols_total=1,
    )

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
