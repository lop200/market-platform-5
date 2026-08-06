from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import StockCandidate, StockOpportunity, TradeIntent, TradingAuditLog
from app.options.market_clock import market_session


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _parsed_time(value: Any, fallback: datetime) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    try:
        return _utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return fallback


def _holding_period(analysis: dict) -> tuple[str, timedelta]:
    text = " ".join(
        str(value or "")
        for value in (
            analysis.get("holding_window_ar"),
            (analysis.get("time_estimate") or {}).get("expected"),
            (analysis.get("time_estimate") or {}).get("base_case"),
        )
    )
    if any(token in text for token in ("دقيقة", "دقائق", "30", "90")):
        return "scalp", timedelta(minutes=90)
    if any(token in text for token in ("يومين", "ثلاثة", "خمسة", "2", "3", "4", "5")):
        return "short", timedelta(days=3)
    if any(token in text for token in ("أكثر من خمسة", "سوينغ", "swing")):
        return "swing", timedelta(days=7)
    return "day_trade", timedelta(hours=6, minutes=30)


def _option_candidate(analysis: dict, settings: Settings, now: datetime) -> dict | None:
    if not settings.options_enabled or not market_session(now).options_actionable:
        return None
    options = analysis.get("options") or {}
    if not options.get("stock_first_gate_passed"):
        return None
    for contract in options.get("ranked_contracts") or []:
        if not contract.get("actionable"):
            continue
        if int(contract.get("dte") or -1) < settings.options_min_dte:
            continue
        if float(contract.get("spread_pct") or 100) > settings.options_max_spread_pct:
            continue
        if int(contract.get("volume") or 0) < settings.options_min_volume:
            continue
        if int(contract.get("open_interest") or 0) < settings.options_min_open_interest:
            continue
        if any(contract.get(key) is None for key in ("delta", "gamma", "theta", "vega")):
            continue
        return contract
    return None


def _validate_stock_gate(analysis: dict, settings: Settings, now: datetime) -> None:
    if analysis.get("status") != "conditional_entry" or not (analysis.get("trade_plan") or analysis.get("entry_zone")):
        raise HTTPException(409, "لا توجد فرصة سهم صالحة؛ لن يتم إنشاء أمر سهم أو عقد أوبشن.")
    quality = analysis.get("data_quality") or {}
    if quality and not quality.get("valid_for_plan", False):
        raise HTTPException(409, "بيانات التحليل غير كافية أو غير صالحة للتنفيذ.")
    quote = analysis.get("quote") or {}
    bid, ask = float(quote.get("bid") or analysis.get("bid") or 0), float(quote.get("ask") or analysis.get("ask") or 0)
    age = float(quote.get("age_seconds") if quote.get("age_seconds") is not None else analysis.get("quote_age_seconds") or 10**9)
    spread_pct = float(quote.get("spread_pct") if quote.get("spread_pct") is not None else analysis.get("spread_pct") or 100)
    if bid <= 0 or ask < bid:
        raise HTTPException(409, "Bid/Ask غير صالحين؛ تم منع إنشاء نية التداول.")
    if age > settings.max_quote_age_seconds:
        raise HTTPException(409, "السعر قديم؛ أعد التحليل قبل التنفيذ.")
    if spread_pct > settings.max_spread_pct:
        raise HTTPException(409, "السبريد أعلى من الحد المقبول؛ لا توجد فرصة قابلة للتنفيذ.")
    expires_at = _parsed_time((analysis.get("trade_plan") or {}).get("expires_at") or analysis.get("expires_at"), now)
    if expires_at <= now:
        raise HTTPException(409, "انتهى وقت الدخول؛ أعد التحليل.")


def create_trade_intent(
    db: Session,
    analysis: dict,
    settings: Settings,
    *,
    analysis_run_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> TradeIntent:
    current = _utc(now)
    _validate_stock_gate(analysis, settings, current)
    plan = analysis.get("trade_plan") or {}
    entry_zone = analysis.get("entry_zone") or {}
    targets = plan.get("targets") or analysis.get("targets") or []
    stock_entry = float(plan.get("entry_from") or entry_zone.get("from") or entry_zone.get("from_price"))
    stock_stop = float(plan.get("stop") or analysis.get("stop_loss"))
    stock_target = float((targets[0] or {}).get("price"))
    contract = _option_candidate(analysis, settings, current)
    instrument_type = "option" if contract else "stock"
    symbol = str(contract.get("symbol") if contract else analysis.get("symbol")).upper()
    entry = float(contract.get("entry_price") if contract else stock_entry)
    target = float(contract.get("target_1") if contract else stock_target)
    stop = float(contract.get("stop_loss") if contract else stock_stop)
    quantity = int(contract.get("recommended_contracts") or 1) if contract else max(1, int((plan.get("position_size") or {}).get("shares") or analysis.get("suggested_shares") or 1))
    entry_valid_until = _parsed_time(plan.get("expires_at") or analysis.get("expires_at"), current)
    holding, duration = _holding_period(analysis)
    session = market_session(current)
    expected_exit = current + duration
    if holding == "day_trade":
        expected_exit = session.regular_stock_close_at.astimezone(timezone.utc) - timedelta(minutes=10)
    explicit_exit = analysis.get("exit_by")
    if explicit_exit:
        expected_exit = min(expected_exit, _parsed_time(explicit_exit, expected_exit))
    force_exit = expected_exit
    quote = analysis.get("quote") or {}
    signal_age = int(quote.get("age_seconds") if quote.get("age_seconds") is not None else analysis.get("quote_age_seconds") or 0)
    intent = TradeIntent(
        idempotency_key=f"marsad-{uuid.uuid4()}",
        opportunity_id=opportunity_id,
        analysis_run_id=analysis_run_id,
        instrument_type=instrument_type,
        symbol=symbol,
        underlying_symbol=str(analysis.get("symbol") or symbol).upper(),
        side="buy",
        quantity=quantity,
        limit_price=entry,
        take_profit=target,
        stop_loss=stop,
        time_in_force="day",
        created_at=current,
        entry_valid_until=entry_valid_until,
        expected_holding_period=holding,
        expected_exit_at=expected_exit,
        force_exit_at=force_exit,
        market_session=session.code,
        signal_age_seconds=signal_age,
        status="ready",
        decision="enter_now",
        reason_ar=str(analysis.get("analysis_summary_ar") or (analysis.get("strategy") or {}).get("reason") or "فرصة رقمية مستوفية لشروط مرصاد."),
        cancellation_condition_ar="؛ ".join(plan.get("invalidation") or analysis.get("invalidation_conditions") or ["قدم السعر أو اتساع السبريد أو تغير سياق السوق"]),
        payload_json={
            "stock_first_gate_passed": True,
            "quote": quote,
            "contract": contract,
            "options_enabled": settings.options_enabled,
            "paper_mode": True,
            "confirmation_required": True,
        },
    )
    db.add(intent)
    db.flush()
    db.add(TradingAuditLog(
        event_type="intent_created", intent_id=intent.id,
        idempotency_key=intent.idempotency_key, status="ready",
        details_json={"symbol": symbol, "instrument_type": instrument_type, "analysis_run_id": str(analysis_run_id) if analysis_run_id else None},
    ))
    db.commit()
    db.refresh(intent)
    return intent


def intent_payload(intent: TradeIntent, now: datetime | None = None) -> dict:
    current = _utc(now)
    expired = _utc(intent.entry_valid_until) <= current
    if expired and intent.status == "ready":
        intent.status = "expired"
    units = 100 if intent.instrument_type == "option" else 1
    cost = float(intent.limit_price) * intent.quantity * units
    profit = abs(float(intent.take_profit) - float(intent.limit_price)) * intent.quantity * units
    loss = abs(float(intent.limit_price) - float(intent.stop_loss)) * intent.quantity * units
    return {
        "id": str(intent.id), "idempotency_key": intent.idempotency_key,
        "instrument_type": intent.instrument_type, "symbol": intent.symbol,
        "underlying_symbol": intent.underlying_symbol, "side": intent.side,
        "quantity": intent.quantity, "limit_price": float(intent.limit_price),
        "take_profit": float(intent.take_profit), "stop_loss": float(intent.stop_loss),
        "time_in_force": intent.time_in_force, "created_at": _utc(intent.created_at).isoformat(),
        "entry_valid_until": _utc(intent.entry_valid_until).isoformat(),
        "expected_holding_period": intent.expected_holding_period,
        "expected_exit_at": _utc(intent.expected_exit_at).isoformat(),
        "force_exit_at": _utc(intent.force_exit_at).isoformat(),
        "market_session": intent.market_session, "signal_age_seconds": intent.signal_age_seconds,
        "status": "expired" if expired else intent.status, "decision": intent.decision,
        "reason_ar": intent.reason_ar, "cancellation_condition_ar": intent.cancellation_condition_ar,
        "estimated_cost_usd": round(cost, 2), "estimated_profit_usd": round(profit, 2),
        "estimated_loss_usd": round(loss, 2), "execution_allowed": not expired and intent.status == "ready",
        "confirmation_required": True, "payload": intent.payload_json or {},
    }


def intent_from_run(db: Session, run_id: uuid.UUID, settings: Settings) -> TradeIntent:
    candidate = db.scalar(select(StockCandidate).where(StockCandidate.scan_run_id == run_id).order_by(StockCandidate.id.desc()))
    if candidate is None:
        raise HTTPException(409, "لم يكتمل التحليل بعد أو لا توجد بيانات تحليل.")
    return create_trade_intent(db, dict(candidate.snapshot_json or {}), settings, analysis_run_id=run_id)


def intent_from_opportunity(db: Session, opportunity: StockOpportunity, settings: Settings) -> TradeIntent:
    analysis = dict(opportunity.result_json or {})
    analysis.setdefault("symbol", opportunity.symbol)
    analysis.setdefault("status", opportunity.status)
    analysis.setdefault("quote_age_seconds", max(0, int((_utc() - _utc(opportunity.quote_timestamp)).total_seconds())))
    analysis.setdefault("expires_at", _utc(opportunity.expires_at).isoformat())
    analysis.setdefault("entry_zone", {"from": float(opportunity.entry_from), "to": float(opportunity.entry_to)})
    analysis.setdefault("stop_loss", float(opportunity.stop_loss))
    return create_trade_intent(db, analysis, settings, opportunity_id=opportunity.id, analysis_run_id=opportunity.scan_run_id)
