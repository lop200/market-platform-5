from __future__ import annotations

import hmac
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import TradingBridgeSnapshot
from app.db.session import get_db
from app.trading.bridge import SahmAdapter
from app.trading.engine import (
    execute_paper_order,
    fill_oco_order,
    get_or_create_account,
    preview_order,
    room_snapshot,
)
from app.trading.schemas import PaperOrderRequest, SahmBridgePayload

router = APIRouter(prefix="/api/v1/trading", tags=["trading-room"])


@router.get("/room")
def trading_room_snapshot(db: Session = Depends(get_db)) -> dict:
    settings = get_settings()
    if not settings.trading_room_enabled:
        raise HTTPException(404, "غرفة التداول غير مفعلة.")
    return room_snapshot(db, settings)


@router.post("/orders/preview")
def order_preview(request: PaperOrderRequest, db: Session = Depends(get_db)) -> dict:
    return preview_order(db, request, get_settings())


@router.post("/orders/paper")
def paper_order(request: PaperOrderRequest, db: Session = Depends(get_db)) -> dict:
    return execute_paper_order(db, request, get_settings())


@router.post("/orders/{order_id}/paper-fill")
def paper_oco_fill(order_id: uuid.UUID, quantity: int, db: Session = Depends(get_db)) -> dict:
    if quantity <= 0:
        raise HTTPException(422, "الكمية يجب أن تكون موجبة.")
    return fill_oco_order(db, order_id, quantity)


@router.post("/emergency-stop")
def emergency_stop(enabled: bool = True, db: Session = Depends(get_db)) -> dict:
    account = get_or_create_account(db, get_settings())
    account.emergency_stop = enabled
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
