from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import (
    OpportunityAudit,
    OpportunityTarget,
    PaperAccount,
    PaperOrder,
    StockOpportunity,
    TradingBridgeSnapshot,
    TradingPosition,
)
from app.live.prices import price_book
from app.options.market_clock import market_session
from app.trading.schemas import PaperOrderRequest


def _utc(value: datetime | None = None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _age_seconds(stamp: datetime, now: datetime | None = None) -> float:
    return max(0.0, (_utc(now) - _utc(stamp)).total_seconds())


def multiplier(instrument_type: str) -> int:
    return 100 if instrument_type == "option" else 1


def get_or_create_account(db: Session, settings: Settings) -> PaperAccount:
    account = db.get(PaperAccount, 1)
    if account is None:
        account = PaperAccount(
            id=1,
            cash=settings.trading_paper_starting_cash,
            buying_power=settings.trading_paper_starting_cash,
            realized_pnl_today=0,
            emergency_stop=False,
        )
        db.add(account)
        db.flush()
    return account


def _bridge_quote(db: Session, symbol: str) -> dict | None:
    snapshot = db.get(TradingBridgeSnapshot, "sahm")
    if snapshot is None:
        return None
    for quote in snapshot.quotes_json or []:
        if str(quote.get("symbol") or "").upper() == symbol:
            stamp = datetime.fromisoformat(str(quote["as_of"]).replace("Z", "+00:00"))
            bid, ask = float(quote["bid"]), float(quote["ask"])
            return {
                "symbol": symbol,
                "bid": bid,
                "ask": ask,
                "price": round((bid + ask) / 2, 4),
                "updated_at": stamp,
                "source": quote.get("source") or "Sahm Bridge",
            }
    return None


def current_quote(db: Session, symbol: str, instrument_type: str) -> dict | None:
    symbol = symbol.upper()
    if instrument_type == "stock":
        rows = price_book.snapshot([symbol])["prices"]
        if rows:
            row = rows[0]
            return {**row, "updated_at": datetime.fromisoformat(row["updated_at"])}
    return _bridge_quote(db, symbol)


def validate_quote(quote: dict | None, settings: Settings, now: datetime | None = None) -> dict:
    if quote is None:
        raise HTTPException(409, "لا توجد تسعيرة موثوقة متاحة للتنفيذ التجريبي.")
    age = _age_seconds(quote["updated_at"], now)
    if age > settings.trading_max_data_age_seconds:
        raise HTTPException(409, f"التسعيرة قديمة ({age:.1f} ثانية)، تم منع التنفيذ.")
    bid, ask = float(quote.get("bid") or 0), float(quote.get("ask") or 0)
    if bid <= 0 or ask <= 0 or ask < bid:
        raise HTTPException(409, "Bid/Ask غير صالحين، تم منع التنفيذ.")
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid * 100 if mid else 100
    if spread_pct > settings.max_spread_pct:
        raise HTTPException(409, "السبريد أعلى من الحد المقبول؛ تم منع التنفيذ.")
    return {
        **quote,
        "age_seconds": round(age, 1),
        "spread": round(ask - bid, 4),
        "spread_pct": round(spread_pct, 2),
    }


def preview_order(
    db: Session, request: PaperOrderRequest, settings: Settings, now: datetime | None = None
) -> dict:
    session = market_session(_utc(now))
    if not session.stock_actionable:
        raise HTTPException(409, "جلسة الأسهم مغلقة؛ تم منع التنفيذ.")
    if request.instrument_type == "option":
        if not settings.options_enabled:
            raise HTTPException(404, "تحليل الأوبشن معطل بواسطة OPTIONS_ENABLED.")
        if not session.options_actionable:
            raise HTTPException(409, "جلسة الأوبشن مغلقة؛ تم منع التنفيذ.")
    if request.instrument_type == "option" and request.limit_price <= 0:
        raise HTTPException(422, "Market Order للأوبشن غير مسموح.")
    quote = validate_quote(current_quote(db, request.symbol, request.instrument_type), settings, now)
    owned = db.scalar(
        select(TradingPosition).where(
            TradingPosition.symbol == request.symbol, TradingPosition.status == "open"
        )
    )
    if request.side == "sell" and (owned is None or owned.quantity < request.quantity):
        raise HTTPException(409, "الكمية المطلوبة للبيع أكبر من الكمية المملوكة.")
    units = multiplier(request.instrument_type)
    total = round(request.quantity * request.limit_price * units, 2)
    if total > settings.trading_max_order_value_usd:
        raise HTTPException(409, "قيمة الصفقة تتجاوز الحد الأقصى المضبوط.")
    max_loss = total
    if request.side == "buy" and request.stop_loss is not None:
        max_loss = round(max(0, request.limit_price - request.stop_loss) * request.quantity * units, 2)
    marketable = (
        request.limit_price >= float(quote["ask"])
        if request.side == "buy"
        else request.limit_price <= float(quote["bid"])
    )
    return {
        "paper_mode": True,
        "live_execution_enabled": False,
        "symbol": request.symbol,
        "side": request.side,
        "instrument_type": request.instrument_type,
        "quantity": request.quantity,
        "limit_price": request.limit_price,
        "total_value": total,
        "estimated_max_loss": max_loss,
        "marketable_now": marketable,
        "quote": {
            "bid": quote["bid"], "ask": quote["ask"], "price": quote["price"],
            "source": quote["source"], "age_seconds": quote["age_seconds"],
        },
        "oco_will_be_created": bool(
            request.side == "buy" and request.take_profit and request.stop_loss
        ),
        "warnings": ["محاكاة فقط — لن يُرسل أي أمر إلى منصة سهم."],
    }


def _upsert_position(
    db: Session, request: PaperOrderRequest, filled: int, fill_price: float, stamp: datetime
) -> TradingPosition | None:
    position = db.scalar(select(TradingPosition).where(TradingPosition.symbol == request.symbol))
    units = multiplier(request.instrument_type)
    if request.side == "buy":
        if position is None:
            position = TradingPosition(
                instrument_type=request.instrument_type,
                symbol=request.symbol,
                underlying_symbol=request.symbol[:10] if request.instrument_type == "stock" else request.symbol[:6].rstrip("0123456789"),
                quantity=filled,
                avg_price=fill_price,
                current_price=fill_price,
                quote_as_of=stamp,
                target_price=request.take_profit,
                stop_price=request.stop_loss,
                profit_protection_trigger_pct=request.profit_protection_trigger_pct,
                trailing_stop_pct=request.trailing_stop_pct,
                source="paper",
                status="open",
            )
            db.add(position)
        else:
            combined = position.quantity + filled
            position.avg_price = (
                float(position.avg_price) * position.quantity + fill_price * filled
            ) / combined
            position.quantity = combined
            position.current_price = fill_price
            position.quote_as_of = stamp
            position.target_price = request.take_profit or position.target_price
            position.stop_price = request.stop_loss or position.stop_price
            position.status = "open"
    else:
        if position is None or position.quantity < filled:
            raise HTTPException(409, "الكمية المنفذة تتجاوز الكمية المملوكة.")
        position.quantity -= filled
        position.current_price = fill_price
        position.quote_as_of = stamp
        if position.quantity == 0:
            position.status = "closed"
    db.flush()
    return position


def _create_oco(db: Session, parent: PaperOrder, request: PaperOrderRequest, filled: int) -> list[PaperOrder]:
    if request.side != "buy" or not (request.take_profit and request.stop_loss) or filled <= 0:
        return []
    group = uuid.uuid4()
    target = PaperOrder(
        client_order_id=f"{request.idempotency_key}:tp", parent_order_id=parent.id,
        oco_group_id=group, order_role="take_profit", instrument_type=request.instrument_type,
        symbol=request.symbol, side="sell", quantity=filled, filled_quantity=0,
        limit_price=request.take_profit, status="open",
    )
    stop = PaperOrder(
        client_order_id=f"{request.idempotency_key}:sl", parent_order_id=parent.id,
        oco_group_id=group, order_role="stop_loss", instrument_type=request.instrument_type,
        symbol=request.symbol, side="sell", quantity=filled, filled_quantity=0,
        limit_price=request.stop_loss, status="open",
    )
    db.add_all([target, stop])
    return [target, stop]


def _reduce_open_oco_after_manual_sell(db: Session, symbol: str, sold_quantity: int) -> None:
    children = db.scalars(
        select(PaperOrder).where(
            PaperOrder.symbol == symbol,
            PaperOrder.order_role.in_(["take_profit", "stop_loss"]),
            PaperOrder.status.in_(["open", "partially_filled"]),
        )
    ).all()
    for child in children:
        child.quantity = max(child.filled_quantity, child.quantity - sold_quantity)
        if child.quantity <= child.filled_quantity:
            child.status = "cancelled"


def execute_paper_order(
    db: Session, request: PaperOrderRequest, settings: Settings, now: datetime | None = None
) -> dict:
    existing = db.scalar(select(PaperOrder).where(PaperOrder.client_order_id == request.idempotency_key))
    if existing is not None:
        return {"order_id": str(existing.id), "status": existing.status, "duplicate": True, "paper_mode": True}
    account = get_or_create_account(db, settings)
    if account.emergency_stop:
        raise HTTPException(423, "إيقاف الطوارئ مفعل؛ الأوامر التجريبية متوقفة.")
    preview = preview_order(db, request, settings, now)
    if float(account.realized_pnl_today) <= -abs(settings.trading_daily_loss_limit_usd):
        raise HTTPException(423, "تم بلوغ حد الخسارة اليومي؛ الأوامر متوقفة.")
    existing_position = db.scalar(
        select(TradingPosition).where(
            TradingPosition.symbol == request.symbol,
            TradingPosition.status == "open",
        )
    )
    open_positions = db.scalar(
        select(func.count(TradingPosition.id)).where(TradingPosition.status == "open")
    ) or 0
    if request.side == "buy" and existing_position is None and open_positions >= settings.trading_max_open_positions:
        raise HTTPException(409, "بلغت الحد الأقصى للمراكز المفتوحة.")
    if request.side == "buy" and preview["total_value"] > float(account.buying_power):
        raise HTTPException(409, "القوة الشرائية غير كافية.")
    fill_qty = 0
    if preview["marketable_now"]:
        fill_qty = min(request.quantity, request.simulated_fill_quantity or request.quantity)
    stamp = _utc(now)
    order = PaperOrder(
        client_order_id=request.idempotency_key,
        order_role="entry",
        instrument_type=request.instrument_type,
        symbol=request.symbol,
        side=request.side,
        quantity=request.quantity,
        filled_quantity=fill_qty,
        limit_price=request.limit_price,
        take_profit=request.take_profit,
        stop_loss=request.stop_loss,
        trailing_stop_pct=request.trailing_stop_pct,
        status=("filled" if fill_qty == request.quantity else "partially_filled" if fill_qty else "pending"),
    )
    db.add(order)
    db.flush()
    if fill_qty:
        fill_price = request.limit_price
        position = _upsert_position(db, request, fill_qty, fill_price, stamp)
        amount = fill_qty * fill_price * multiplier(request.instrument_type)
        if request.side == "buy":
            account.cash = float(account.cash) - amount
            account.buying_power = float(account.buying_power) - amount
        else:
            account.cash = float(account.cash) + amount
            account.buying_power = float(account.buying_power) + amount
            account.realized_pnl_today = float(account.realized_pnl_today) + (
                fill_price - float(position.avg_price)
            ) * fill_qty * multiplier(request.instrument_type)
            _reduce_open_oco_after_manual_sell(db, request.symbol, fill_qty)
        _create_oco(db, order, request, fill_qty)
    db.commit()
    return {
        **preview,
        "order_id": str(order.id),
        "status": order.status,
        "filled_quantity": fill_qty,
        "duplicate": False,
    }


def fill_oco_order(db: Session, order_id: uuid.UUID, fill_quantity: int) -> dict:
    order = db.get(PaperOrder, order_id)
    if order is None or order.order_role not in {"take_profit", "stop_loss"}:
        raise HTTPException(404, "أمر OCO غير موجود.")
    if order.status not in {"open", "partially_filled"}:
        raise HTTPException(409, "أمر OCO غير قابل للتنفيذ.")
    remaining = order.quantity - order.filled_quantity
    fill = min(max(0, fill_quantity), remaining)
    position = db.scalar(select(TradingPosition).where(TradingPosition.symbol == order.symbol))
    if position is None or position.status != "open" or position.quantity < fill:
        raise HTTPException(409, "الكمية المملوكة لا تغطي تنفيذ OCO.")
    account = db.get(PaperAccount, 1)
    if account is None:
        raise HTTPException(409, "حساب Paper غير موجود.")
    fill_price = float(order.limit_price)
    amount = fill * fill_price * multiplier(order.instrument_type)
    account.cash = float(account.cash) + amount
    account.buying_power = float(account.buying_power) + amount
    account.realized_pnl_today = float(account.realized_pnl_today) + (
        fill_price - float(position.avg_price)
    ) * fill * multiplier(order.instrument_type)
    order.filled_quantity += fill
    order.status = "filled" if order.filled_quantity == order.quantity else "partially_filled"
    position.quantity -= fill
    if position.quantity == 0:
        position.status = "closed"
    siblings = db.scalars(
        select(PaperOrder).where(
            PaperOrder.oco_group_id == order.oco_group_id, PaperOrder.id != order.id,
            PaperOrder.status.in_(["open", "partially_filled"]),
        )
    ).all()
    if order.status == "filled":
        for sibling in siblings:
            sibling.status = "cancelled"
    else:
        for sibling in siblings:
            sibling.quantity = max(sibling.filled_quantity, sibling.quantity - fill)
            if sibling.quantity <= sibling.filled_quantity:
                sibling.status = "cancelled"
    db.commit()
    return {"order_id": str(order.id), "status": order.status, "filled_quantity": order.filled_quantity}


def historical_probability(db: Session, opportunity: StockOpportunity | None, minimum: int) -> dict:
    if opportunity is None:
        return {"status": "insufficient", "label_ar": "بيانات غير كافية", "samples": 0, "target_probability_pct": None, "stop_probability_pct": None}
    audits = db.scalars(
        select(OpportunityAudit)
        .join(StockOpportunity, StockOpportunity.id == OpportunityAudit.opportunity_id)
        .where(
            StockOpportunity.strategy_id == opportunity.strategy_id,
            OpportunityAudit.entry_triggered_at.is_not(None),
        )
    ).all()
    samples = len(audits)
    if samples < minimum:
        return {"status": "insufficient", "label_ar": "بيانات غير كافية", "samples": samples, "target_probability_pct": None, "stop_probability_pct": None}
    targets = sum(bool(item.target_1_hit) for item in audits)
    stops = sum(bool(item.stop_hit) for item in audits)
    return {
        "status": "scored", "label_ar": "من نتائج تاريخية مماثلة", "samples": samples,
        "target_probability_pct": round(targets / samples * 100, 1),
        "stop_probability_pct": round(stops / samples * 100, 1),
    }


def room_snapshot(db: Session, settings: Settings, now: datetime | None = None) -> dict:
    current = _utc(now)
    account = get_or_create_account(db, settings)
    bridge = db.get(TradingBridgeSnapshot, "sahm")
    bridge_age = _age_seconds(bridge.synced_at, current) if bridge else None
    bridge_fresh = bool(bridge and bridge_age <= settings.trading_max_data_age_seconds)
    positions = db.scalars(select(TradingPosition).where(TradingPosition.status == "open")).all()
    serialized = []
    unrealized = 0.0
    for item in positions:
        quote = current_quote(db, item.symbol, item.instrument_type)
        if quote and _age_seconds(quote["updated_at"], current) <= settings.trading_max_data_age_seconds:
            price, quote_stamp = float(quote["price"]), _utc(quote["updated_at"])
        else:
            price, quote_stamp = float(item.current_price), _utc(item.quote_as_of)
        pnl = (price - float(item.avg_price)) * item.quantity * multiplier(item.instrument_type)
        unrealized += pnl
        value = price * item.quantity * multiplier(item.instrument_type)
        serialized.append({
            "id": str(item.id), "instrument_type": item.instrument_type, "symbol": item.symbol,
            "quantity": item.quantity, "average_price": float(item.avg_price), "current_price": price,
            "market_value": round(value, 2), "pnl_usd": round(pnl, 2),
            "pnl_pct": round((price / float(item.avg_price) - 1) * 100, 2) if item.avg_price else 0,
            "target_price": float(item.target_price) if item.target_price is not None else None,
            "stop_price": float(item.stop_price) if item.stop_price is not None else None,
            "quote_age_seconds": round(_age_seconds(quote_stamp, current), 1), "source": item.source,
        })
    latest = db.scalar(select(StockOpportunity).order_by(StockOpportunity.issued_at.desc()).limit(1))
    targets = []
    if latest:
        targets = db.scalars(
            select(OpportunityTarget).where(OpportunityTarget.opportunity_id == latest.id).order_by(OpportunityTarget.sequence)
        ).all()
    quote = current_quote(db, latest.symbol, "stock") if latest else None
    current_price = float(quote["price"]) if quote else (float(latest.price_at_analysis) if latest else None)
    near = float(targets[0].price) if targets else None
    extended = float(targets[1].price) if len(targets) > 1 else None
    entry = float(latest.entry_from) if latest else None
    stop = float(latest.stop_loss) if latest else None
    distance = None
    if current_price is not None and entry is not None and near is not None and near != entry:
        distance = round(max(0, min(100, (current_price - entry) / (near - entry) * 100)), 1)
    result = dict(latest.result_json or {}) if latest else {}
    scorecard = result.get("scorecard") or result.get("opportunity_scorecard") or {}
    direction = result.get("trade_direction")
    if not direction and entry is not None and near is not None:
        direction = "صاعد" if near > entry else "هابط" if near < entry else "متذبذب"
    direction_reasons = result.get("direction_reasons") or result.get("reasons") or []
    if not direction_reasons and entry is not None and near is not None and stop is not None:
        direction_reasons = [
            ("الهدف القريب أعلى نقطة الدخول." if near > entry else "الهدف القريب أدنى نقطة الدخول."),
            "الدخول والهدف والوقف محسوبة من خطة التحليل الرقمية المحفوظة.",
        ]
    session = market_session(current)
    analysis = {
        "symbol": latest.symbol if latest else None,
        "entry": entry, "near_target": near, "extended_target": extended, "stop": stop,
        "technical_signal_score": scorecard.get("technical_signal_score") or (latest.overall_score if latest else None),
        "historical_probability": historical_probability(db, latest, settings.trading_min_backtest_samples),
        "target_progress_pct": distance,
        "direction": direction or "غير متاح",
        "direction_reasons": direction_reasons,
        "cancellation_conditions": result.get("cancellation_conditions") or result.get("exclusion_reasons") or [],
        "quote": ({
            "price": quote["price"], "bid": quote.get("bid"), "ask": quote.get("ask"),
            "source": quote.get("source"), "age_seconds": round(_age_seconds(quote["updated_at"], current), 1),
        } if quote else None),
    }
    paper_account = {
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
        "daily_pnl": round(float(account.realized_pnl_today) + unrealized, 2),
        "emergency_stop": account.emergency_stop,
        "source": "paper",
    }
    display_account = (
        {
            "cash": float(bridge.account_json.get("cash") or 0),
            "buying_power": float(bridge.account_json.get("buying_power") or 0),
            "daily_pnl": float(bridge.account_json.get("daily_pnl") or 0),
            "emergency_stop": account.emergency_stop,
            "source": "sahm_read_only",
        }
        if bridge_fresh else paper_account
    )
    return {
        "mode": "paper", "live_execution_enabled": False,
        "confirm_mode_available": settings.trading_confirm_mode_enabled,
        "risk_limits": {
            "max_order_value_usd": settings.trading_max_order_value_usd,
            "default_risk_pct": settings.default_risk_pct,
            "style": "intraday_sniper",
        },
        "bridge": {"marsad_status": "connected", "sahm_status": "connected" if bridge_fresh else "disconnected", "last_sync": bridge.synced_at.isoformat() if bridge else None, "age_seconds": round(bridge_age, 1) if bridge_age is not None else None},
        "account": display_account,
        "paper_account": paper_account,
        "market": {"session": session.code, "session_label_ar": session.label_ar, "options_open": session.options_actionable},
        "positions": serialized,
        "bridge_positions": bridge.positions_json if bridge_fresh else [],
        "analysis": analysis,
        "generated_at": current.isoformat(),
    }
