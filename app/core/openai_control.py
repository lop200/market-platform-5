from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.cost_gate import CostGate
from app.db import repository
from app.db.models import CacheEntry, OpenAICallLog


OPENAI_ENDPOINT = "/v1/responses"


@dataclass(frozen=True)
class OpenAICallOutcome:
    response: Any | None
    status: str
    reason: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    duration_ms: int = 0


def _usage_values(response: Any) -> tuple[int, int, int, int]:
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    details = getattr(usage, "input_tokens_details", None)
    cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
    total_tokens = int(
        getattr(usage, "total_tokens", 0) or input_tokens + output_tokens
    )
    return input_tokens, output_tokens, cached_tokens, total_tokens


def estimate_openai_cost(
    settings: Settings, input_tokens: int, output_tokens: int, cached_tokens: int = 0
) -> float:
    cached = min(max(0, cached_tokens), max(0, input_tokens))
    uncached = max(0, input_tokens - cached)
    return round(
        uncached * settings.openai_input_cost_per_million / 1_000_000
        + cached * settings.openai_cached_input_cost_per_million / 1_000_000
        + max(0, output_tokens) * settings.openai_output_cost_per_million / 1_000_000,
        8,
    )


def _record_call(
    db: Session,
    settings: Settings,
    *,
    operation: str,
    symbols: list[str],
    run_id: str | None,
    reason: str,
    status: str,
    response: Any | None,
    duration_ms: int,
) -> OpenAICallOutcome:
    input_tokens, output_tokens, cached_tokens, total_tokens = (
        _usage_values(response) if response is not None else (0, 0, 0, 0)
    )
    estimated = estimate_openai_cost(
        settings, input_tokens, output_tokens, cached_tokens
    )
    db.add(
        OpenAICallLog(
            endpoint=OPENAI_ENDPOINT,
            operation=operation,
            model_name=settings.openai_model,
            symbol=",".join(sorted(set(symbols))) or None,
            symbols_json=sorted(set(symbols)),
            run_id=str(run_id) if run_id else None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_tokens=cached_tokens,
            total_tokens=total_tokens,
            estimated_cost_usd=estimated,
            duration_ms=duration_ms,
            reason=reason[:160],
            status=status,
        )
    )
    db.commit()
    return OpenAICallOutcome(
        response=response,
        status=status,
        reason=reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        total_tokens=total_tokens,
        estimated_cost_usd=estimated,
        duration_ms=duration_ms,
    )


def execute_structured_response(
    db: Session,
    settings: Settings,
    *,
    client: Any,
    operation: str,
    symbols: list[str],
    run_id: str | None,
    reason: str,
    input_messages: list[dict],
    text_format: type,
    estimated_cost_usd: float = 0.02,
) -> OpenAICallOutcome:
    """The only Responses API execution path; logs metadata but never prompts."""
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_spent = db.scalar(
        select(func.coalesce(func.sum(OpenAICallLog.estimated_cost_usd), 0)).where(
            OpenAICallLog.created_at >= day_start,
            OpenAICallLog.status.in_(("completed", "failed")),
        )
    )
    if float(daily_spent or 0) + estimated_cost_usd > settings.openai_daily_budget_usd:
        return _record_call(
            db, settings, operation=operation, symbols=symbols, run_id=run_id,
            reason="openai_daily_budget_blocked", status="budget_blocked",
            response=None, duration_ms=0,
        )
    if estimated_cost_usd > settings.openai_operation_budget_usd:
        return _record_call(
            db, settings, operation=operation, symbols=symbols, run_id=run_id,
            reason="operation_budget_blocked", status="budget_blocked",
            response=None, duration_ms=0,
        )
    if run_id:
        spent = db.scalar(
            select(func.coalesce(func.sum(OpenAICallLog.estimated_cost_usd), 0)).where(
                OpenAICallLog.run_id == str(run_id),
                OpenAICallLog.status.in_(("completed", "failed")),
            )
        )
        if float(spent or 0) + estimated_cost_usd > settings.openai_scan_budget_usd:
            return _record_call(
                db, settings, operation=operation, symbols=symbols, run_id=run_id,
                reason="run_budget_blocked", status="budget_blocked",
                response=None, duration_ms=0,
            )
    gate = CostGate(db, settings).check_and_reserve(
        category="llm", provider="openai", estimated_cost=estimated_cost_usd
    )
    if not gate.allowed:
        return _record_call(
            db, settings, operation=operation, symbols=symbols, run_id=run_id,
            reason=f"cost_gate_blocked:{gate.reason or 'unknown'}",
            status="budget_blocked", response=None, duration_ms=0,
        )

    started = time.perf_counter()
    try:
        response = client.responses.parse(
            model=settings.openai_model,
            input=input_messages,
            text_format=text_format,
            max_output_tokens=settings.openai_max_output_tokens,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)
        outcome = _record_call(
            db, settings, operation=operation, symbols=symbols, run_id=run_id,
            reason=reason, status="completed", response=response,
            duration_ms=duration_ms,
        )
        if gate.ledger_id is not None:
            CostGate(db, settings).record_actual(
                gate.ledger_id, outcome.estimated_cost_usd
            )
        return outcome
    except Exception:
        duration_ms = int((time.perf_counter() - started) * 1000)
        outcome = _record_call(
            db, settings, operation=operation, symbols=symbols, run_id=run_id,
            reason=f"{reason}:sdk_failure", status="failed", response=None,
            duration_ms=duration_ms,
        )
        if gate.ledger_id is not None:
            CostGate(db, settings).record_actual(gate.ledger_id, 0.0)
        return outcome


def review_data_version(candidate: dict, bucket_seconds: int) -> str:
    quote = candidate.get("quote") or {}
    raw = (
        candidate.get("data_version")
        or quote.get("quote_timestamp")
        or quote.get("updated_at")
        or quote.get("timestamp")
    )
    if raw:
        try:
            stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            seconds = max(1, bucket_seconds)
            bucket = int(stamp.timestamp()) // seconds * seconds
            return datetime.fromtimestamp(bucket, timezone.utc).isoformat()
        except (TypeError, ValueError):
            return str(raw)[:80]
    stable = {
        "quote": quote,
        "indicators": candidate.get("indicators"),
        "trade_plan": candidate.get("trade_plan"),
    }
    return "content:" + hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode()
    ).hexdigest()[:20]


def review_cache_key(
    candidate: dict, settings: Settings, prompt_version: str, operation: str
) -> tuple[str, dict]:
    quote = candidate.get("quote") or {}
    symbol = str(candidate.get("symbol") or "UNKNOWN").upper()
    session = str(
        candidate.get("market_session")
        or quote.get("session")
        or quote.get("market_session")
        or "unknown"
    )
    version = review_data_version(candidate, settings.openai_review_cache_seconds)
    identity = {
        "symbol": symbol,
        "market_session": session,
        "data_version": version,
        "prompt_version": prompt_version,
        "model": settings.openai_model,
        "operation": operation,
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode()
    ).hexdigest()
    return f"openai-review:{digest}", identity


def get_review_cache(db: Session, key: str) -> tuple[dict | None, int | None]:
    value = repository.cache_get(db, key)
    if not value or value.get("status") != "completed" or not value.get("result"):
        return None, None
    saved_raw = value.get("saved_at")
    try:
        saved = datetime.fromisoformat(str(saved_raw).replace("Z", "+00:00"))
        if saved.tzinfo is None:
            saved = saved.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - saved).total_seconds()))
    except (TypeError, ValueError):
        age = None
    return value["result"], age


def reserve_review_key(
    db: Session, key: str, identity: dict, settings: Settings
) -> bool:
    now = datetime.now(timezone.utc)
    existing = db.get(CacheEntry, key)
    if existing is not None:
        expiry = existing.expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry > now:
            return False
        db.delete(existing)
        db.commit()
    try:
        db.add(
            CacheEntry(
                key=key,
                value={"status": "in_progress", "identity": identity},
                expires_at=now + timedelta(seconds=settings.openai_lock_seconds),
            )
        )
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def store_review_result(
    db: Session, key: str, identity: dict, result: dict, settings: Settings
) -> None:
    now = datetime.now(timezone.utc)
    repository.cache_set(
        db,
        key,
        {
            "status": "completed",
            "identity": identity,
            "result": result,
            "saved_at": now.isoformat(),
            "reason_ar": "إعادة استخدام مراجعة لنفس الرمز والجلسة وإصدار البيانات والنموذج.",
        },
        now + timedelta(seconds=settings.openai_review_cache_seconds),
    )


def store_review_failure(db: Session, key: str, identity: dict, settings: Settings) -> None:
    now = datetime.now(timezone.utc)
    repository.cache_set(
        db,
        key,
        {"status": "failed", "identity": identity, "saved_at": now.isoformat()},
        now + timedelta(seconds=settings.openai_failure_cooldown_seconds),
    )


def wait_for_review_result(
    db: Session, key: str, timeout_seconds: float
) -> tuple[dict | None, int | None]:
    deadline = time.monotonic() + max(0, timeout_seconds)
    while time.monotonic() < deadline:
        db.expire_all()
        result, age = get_review_cache(db, key)
        if result:
            return result, age
        value = repository.cache_get_any(db, key)
        if value and value.get("status") == "failed":
            return None, None
        time.sleep(0.05)
    return None, None
