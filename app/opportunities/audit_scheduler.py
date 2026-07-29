from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from sqlalchemy import select

from app.db.models import OpportunityAudit, OpportunityEvent, StockOpportunity
from app.db.session import SessionLocal
from app.opportunities.audit import evaluate_timeline
from app.providers.factory import get_market_data_provider

logger = logging.getLogger(__name__)
_stop = Event()
_thread: Thread | None = None


def run_audit_cycle() -> int:
    db = SessionLocal()
    updated = 0
    try:
        now = datetime.now(timezone.utc)
        rows = db.scalars(
            select(StockOpportunity).where(
                StockOpportunity.issued_at >= now - timedelta(days=3)
            )
        ).all()
        provider = get_market_data_provider()
        for row in rows:
            age = now - _aware(row.issued_at)
            due = ["session"]
            if age >= timedelta(days=1):
                due.append("day_1")
            if age >= timedelta(days=2):
                due.append("day_2")
            existing = set(
                db.scalars(
                    select(OpportunityAudit.horizon).where(
                        OpportunityAudit.opportunity_id == row.id
                    )
                ).all()
            )
            due = [horizon for horizon in due if horizon not in existing]
            if not due:
                continue
            frame = provider.get_intraday(row.symbol, "5m")
            if frame is None or frame.empty:
                continue
            ticks = [(_aware(index.to_pydatetime()), float(price)) for index, price in frame["close"].items()]
            targets = [float(item["price"]) for item in row.result_json.get("targets", [])]
            if len(targets) < 2:
                continue
            outcome = evaluate_timeline(
                ticks, float(row.entry_from), float(row.entry_to),
                float(row.stop_loss), targets[0], targets[1],
            )
            for horizon in due:
                db.add(OpportunityAudit(
                    opportunity_id=row.id, horizon=horizon,
                    entry_triggered_at=outcome.entered_at,
                    highest_price=outcome.highest, lowest_price=outcome.lowest,
                    target_1_hit=outcome.target_1_hit, target_2_hit=outcome.target_2_hit,
                    stop_hit=outcome.stop_hit, outcome=outcome.outcome,
                ))
                updated += 1
            if outcome.entered_at:
                _add_event_once(db, row.id, "entry_triggered", float(row.entry_from), outcome.entered_at)
            if outcome.stop_hit:
                _add_event_once(db, row.id, "stop_hit", float(row.stop_loss), now)
            elif outcome.target_2_hit:
                _add_event_once(db, row.id, "target_2_hit", targets[1], now)
            elif outcome.target_1_hit:
                _add_event_once(db, row.id, "target_1_hit", targets[0], now)
        db.commit()
        return updated
    except Exception as exc:
        db.rollback()
        logger.warning("Opportunity audit cycle failed: %s", type(exc).__name__)
        return 0
    finally:
        db.close()


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _add_event_once(db, opportunity_id, event_type: str, price: float, occurred_at: datetime) -> None:
    exists = db.scalar(
        select(OpportunityEvent.id).where(
            OpportunityEvent.opportunity_id == opportunity_id,
            OpportunityEvent.event_type == event_type,
        ).limit(1)
    )
    if not exists:
        db.add(OpportunityEvent(
            opportunity_id=opportunity_id, event_type=event_type,
            price=price, occurred_at=occurred_at,
        ))


def _loop() -> None:
    while not _stop.wait(60):
        run_audit_cycle()


def start_audit_scheduler() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = Thread(target=_loop, name="opportunity-audit", daemon=True)
    _thread.start()


def stop_audit_scheduler() -> None:
    _stop.set()
