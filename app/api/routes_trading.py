from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import StockOpportunity, TradeIntent, TradingAuditLog, TradingBridgeSnapshot
from app.db.session import get_db
from app.trading.bridge import SahmAdapter
from app.trading.engine import (
    execute_paper_order,
    fill_oco_order,
    get_or_create_account,
    preview_order,
    room_snapshot,
)
from app.trading.intent import intent_from_opportunity, intent_from_run, intent_payload, size_intent
from app.trading.schemas import ExtensionExecutionEvent, IntentSizingRequest, PaperOrderRequest, SahmBridgePayload, TradeIntentEdit
from app.stocks.rules import allowed_stock_spread_pct

router = APIRouter(prefix="/api/v1/trading", tags=["trading-room"])


@router.get("/room")
def trading_room_snapshot(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    if not settings.trading_room_enabled:
        raise HTTPException(404, "غرفة التداول غير مفعلة.")
    return room_snapshot(db, settings)


@router.get("/today")
def today_opportunities(db: Session = Depends(get_db)) -> dict:
    """Return at most three already-saved opportunities; never starts a scan or calls OpenAI."""
    now = datetime.now(timezone.utc)
    rows = db.scalars(
        select(StockOpportunity)
        .where(StockOpportunity.status != "expired", StockOpportunity.expires_at > now)
        .order_by(StockOpportunity.overall_score.desc(), StockOpportunity.issued_at.desc())
        .limit(12)
    ).all()
    opportunities = []
    for row in rows:
        result = dict(row.result_json or {})
        quote_age = max(0, int((now - (row.quote_timestamp.replace(tzinfo=timezone.utc) if row.quote_timestamp.tzinfo is None else row.quote_timestamp.astimezone(timezone.utc))).total_seconds()))
        spread = float(result.get("spread_pct") or 100)
        settings = get_settings()
        if quote_age > settings.max_quote_age_seconds or spread > allowed_stock_spread_pct(row.price_at_analysis, settings):
            continue
        opportunities.append({
            "opportunity_id": str(row.id), "symbol": row.symbol,
            "current_price": float(row.price_at_analysis),
            "entry_zone": {"from": float(row.entry_from), "to": float(row.entry_to)},
            "direction": result.get("trend") or result.get("trade_direction") or "متذبذب",
            "decision": "دخول الآن" if row.status == "conditional_entry" else "انتظار",
            "historical_probability_pct": None,
            "historical_samples": 0,
            "holding_period": result.get("holding_window_ar") or "حسب التحليل",
            "entry_valid_until": row.expires_at.isoformat(),
            "score": row.overall_score,
            "target_probability_pct": result.get("target_probability_pct"),
            "valid_for_minutes": result.get("valid_for_minutes"),
        })
        if len(opportunities) == 3:
            break
    return {"opportunities": opportunities, "source": "saved_scan", "openai_calls": 0}


@router.post("/intents/from-run/{run_id}")
def create_intent_from_run(run_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    return intent_payload(intent_from_run(db, run_id, get_settings()))


@router.post("/intents/from-opportunity/{opportunity_id}")
def create_intent_from_saved_opportunity(opportunity_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    opportunity = db.get(StockOpportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(404, "الفرصة غير موجودة.")
    return intent_payload(intent_from_opportunity(db, opportunity, get_settings()))


@router.get("/intents/{intent_id}")
def get_intent(intent_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    intent = db.get(TradeIntent, intent_id)
    if intent is None:
        raise HTTPException(404, "نية التداول غير موجودة.")
    payload = intent_payload(intent)
    if payload["status"] == "expired" and intent.status != "expired":
        intent.status = "expired"
        db.commit()
    return payload


@router.put("/intents/{intent_id}")
def edit_intent(intent_id: uuid.UUID, edit: TradeIntentEdit, db: Session = Depends(get_db)) -> dict:
    intent = db.get(TradeIntent, intent_id)
    if intent is None:
        raise HTTPException(404, "نية التداول غير موجودة.")
    if intent_payload(intent)["status"] == "expired":
        raise HTTPException(409, "انتهى وقت الدخول؛ أعد التحليل.")
    if not edit.stop_loss < edit.limit_price < edit.take_profit:
        raise HTTPException(422, "لأمر الشراء يجب أن يكون الوقف أقل من Limit والهدف أعلى منه.")
    intent.quantity = edit.quantity
    intent.limit_price = edit.limit_price
    intent.take_profit = edit.take_profit
    intent.stop_loss = edit.stop_loss
    intent.time_in_force = edit.time_in_force
    db.add(TradingAuditLog(
        event_type="intent_edited", intent_id=intent.id,
        idempotency_key=intent.idempotency_key, status="ready",
        details_json={"quantity": edit.quantity, "limit_price": edit.limit_price, "take_profit": edit.take_profit, "stop_loss": edit.stop_loss},
    ))
    db.commit()
    db.refresh(intent)
    return intent_payload(intent)


@router.post("/intents/{intent_id}/size")
def size_trade_intent(
    intent_id: uuid.UUID,
    request: IntentSizingRequest,
    db: Session = Depends(get_db),
) -> dict:
    intent = db.get(TradeIntent, intent_id)
    if intent is None:
        raise HTTPException(404, "نية التداول غير موجودة.")
    return size_intent(db, intent, request, get_settings())


@router.post("/intents/{intent_id}/extension-preview")
def extension_preview(intent_id: uuid.UUID, db: Session = Depends(get_db)) -> dict:
    intent = db.get(TradeIntent, intent_id)
    if intent is None:
        raise HTTPException(404, "نية التداول غير موجودة.")
    payload = intent_payload(intent)
    if not payload["execution_allowed"]:
        raise HTTPException(409, "نية التداول منتهية أو غير قابلة للتنفيذ؛ أعد التحليل.")
    db.add(TradingAuditLog(
        event_type="extension_preview", intent_id=intent.id,
        idempotency_key=intent.idempotency_key, status="awaiting_user_confirmation",
        details_json={"symbol": intent.symbol, "instrument_type": intent.instrument_type},
    ))
    db.commit()
    return payload


@router.post("/intents/{intent_id}/extension-event")
def extension_event(
    intent_id: uuid.UUID,
    event: ExtensionExecutionEvent,
    db: Session = Depends(get_db),
) -> dict:
    intent = db.get(TradeIntent, intent_id)
    if intent is None:
        raise HTTPException(404, "نية التداول غير موجودة.")
    intent.status = event.status
    db.add(TradingAuditLog(
        event_type="extension_result", intent_id=intent.id,
        idempotency_key=intent.idempotency_key, status=event.status,
        details_json={
            "message": event.message,
            "filled_quantity": event.filled_quantity,
            "observed_at": event.observed_at.isoformat(),
        },
    ))
    db.commit()
    return {"accepted": True, "status": intent.status}


@router.post("/orders/preview")
def order_preview(request: PaperOrderRequest, db: Session = Depends(get_db)) -> dict:
    result = preview_order(db, request, get_settings())
    db.add(TradingAuditLog(
        event_type="paper_preview", idempotency_key=request.idempotency_key,
        status="previewed", details_json={
            "symbol": request.symbol, "instrument_type": request.instrument_type,
            "side": request.side, "quantity": request.quantity,
            "limit_price": request.limit_price,
        },
    ))
    db.commit()
    return result


@router.post("/orders/paper")
def paper_order(request: PaperOrderRequest, db: Session = Depends(get_db)) -> dict:
    result = execute_paper_order(db, request, get_settings())
    db.add(TradingAuditLog(
        event_type="paper_order", idempotency_key=request.idempotency_key,
        status=result["status"], details_json={
            "order_id": result["order_id"], "symbol": request.symbol,
            "filled_quantity": result.get("filled_quantity", 0),
            "duplicate": result.get("duplicate", False),
        },
    ))
    db.commit()
    return result


@router.post("/orders/{order_id}/paper-fill")
def paper_oco_fill(order_id: uuid.UUID, quantity: int, db: Session = Depends(get_db)) -> dict:
    if quantity <= 0:
        raise HTTPException(422, "الكمية يجب أن تكون موجبة.")
    return fill_oco_order(db, order_id, quantity)


@router.post("/emergency-stop")
def emergency_stop(enabled: bool = True, db: Session = Depends(get_db)) -> dict:
    account = get_or_create_account(db, get_settings())
    account.emergency_stop = enabled
    db.add(TradingAuditLog(
        event_type="emergency_stop", status="enabled" if enabled else "disabled",
        details_json={},
    ))
    db.commit()
    return {"enabled": account.emergency_stop, "paper_mode": True}


@router.post("/bridge/sahm/sync")
def sync_sahm_bridge(
    payload: SahmBridgePayload,
    x_bridge_token: str = Header(default=""),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    expected = settings.trading_bridge_token or ""
    if not settings.trading_bridge_enabled or not expected:
        raise HTTPException(503, "مرصاد Bridge غير مفعّل.")
    if not hmac.compare_digest(x_bridge_token.encode(), expected.encode()):
        raise HTTPException(401, "رمز Bridge غير صالح.")
    normalized = SahmAdapter().normalize_snapshot(payload.model_dump(mode="python"))
    snapshot = db.get(TradingBridgeSnapshot, "sahm")
    if snapshot is None:
        snapshot = TradingBridgeSnapshot(adapter="sahm", synced_at=normalized["captured_at"])
        db.add(snapshot)
    snapshot.connection_status = "connected"
    snapshot.account_json = normalized["account"]
    snapshot.positions_json = normalized["positions"]
    snapshot.orders_json = normalized["orders"]
    snapshot.quotes_json = normalized["quotes"]
    snapshot.synced_at = normalized["captured_at"].astimezone(timezone.utc)
    db.commit()
    return {"status": "accepted", "adapter": "sahm", "synced_at": snapshot.synced_at}
