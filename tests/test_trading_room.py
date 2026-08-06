from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db.models import PaperOrder, TradeIntent, TradingBridgeSnapshot, TradingPosition
from app.live.prices import price_book
from app.main import app
from app.api import routes_trading
from app.trading.bridge import SahmAdapter
from app.trading.engine import execute_paper_order, fill_oco_order, preview_order, room_snapshot
from app.trading.intent import create_trade_intent, intent_payload
from app.trading.schemas import PaperOrderRequest, SahmBridgePayload

NOW = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_price_book():
    price_book.clear()
    yield
    price_book.clear()


def settings(**overrides):
    return Settings(
        database_url="sqlite://",
        trading_paper_starting_cash=100_000,
        trading_max_data_age_seconds=15,
        trading_min_backtest_samples=20,
        **overrides,
    )


def stock_request(**overrides):
    payload = {
        "idempotency_key": "paper-nvda-0001",
        "side": "buy",
        "instrument_type": "stock",
        "symbol": "NVDA",
        "quantity": 10,
        "limit_price": 101.0,
        "take_profit": 106.0,
        "stop_loss": 98.0,
        "profit_protection_trigger_pct": 60,
        "trailing_stop_pct": 2,
    }
    payload.update(overrides)
    return PaperOrderRequest(**payload)


def fresh_stock_quote(stamp=NOW):
    price_book.record("NVDA", price=100.5, bid=100.0, ask=101.0, updated_at=stamp, source="quote")


def test_trading_room_is_rtl_responsive_and_live_execution_is_disabled():
    html = TestClient(app).get("/trading-room").text
    assert 'dir="rtl"' in html
    assert "مرصاد — واجهة التداول" in html
    assert "overflow-x:hidden" in html
    assert "PAPER MODE" in html
    assert "تنفيذ في سهم" in html
    assert "إرسال الأمر الآن" in html
    assert "ربط منصة سهم" in html
    assert 'window.open("https://app.sahmcapital.com/"' in html
    assert 'source:"marsad-page",type:"CONNECT_SAHM"' in html
    assert "symbolCatalog" in html
    assert "/api/v1/opportunities/scans" in html
    assert "احسب الكمية من رصيد سهم" in html
    assert "renderObservation" in html
    assert "صافي الربح المقدر" in html
    assert "احتمال لمس الهدف" in html
    assert "?refresh=true" in html
    assert "اقتناص مضاربي" in html
    assert "sniperBudget" in html
    assert 'all_prices:"false"' in html
    assert "target_probability_pct" in html
    assert "سهمان على الأقل" in html
    assert "renderWatchlist" in html
    assert 'value="5">5% — محفظة صغيرة' in html
    assert "minShares=small?2:10" in html
    assert "سبريد أقصى 4%" in html
    assert "@media(max-width:760px)" in html
    assert 'href="/trading-room"' in TestClient(app).get("/").text


def test_empty_room_never_invents_historical_probability(db_session):
    payload = room_snapshot(db_session, settings(), now=NOW)
    history = payload["analysis"]["historical_probability"]
    assert history["status"] == "insufficient"
    assert history["label_ar"] == "بيانات غير كافية"
    assert history["samples"] == 0
    assert history["target_probability_pct"] is None
    assert payload["live_execution_enabled"] is False
    assert payload["risk_limits"] == {
        "max_order_value_usd": 5_000,
        "default_risk_pct": 1.0,
        "style": "intraday_sniper",
    }


def test_fresh_paper_buy_creates_position_and_linked_oco(db_session):
    fresh_stock_quote()
    result = execute_paper_order(db_session, stock_request(), settings(), now=NOW)
    assert result["status"] == "filled"
    assert result["paper_mode"] is True
    position = db_session.scalar(select(TradingPosition).where(TradingPosition.symbol == "NVDA"))
    assert position.quantity == 10
    children = db_session.scalars(
        select(PaperOrder).where(PaperOrder.parent_order_id == uuid.UUID(result["order_id"]))
    ).all()
    assert {item.order_role for item in children} == {"take_profit", "stop_loss"}
    assert {item.quantity for item in children} == {10}


def test_partial_fill_protects_only_filled_quantity(db_session):
    fresh_stock_quote()
    request = stock_request(idempotency_key="paper-partial-0001", simulated_fill_quantity=4)
    result = execute_paper_order(db_session, request, settings(), now=NOW)
    assert result["status"] == "partially_filled"
    children = db_session.scalars(
        select(PaperOrder).where(PaperOrder.parent_order_id == uuid.UUID(result["order_id"]))
    ).all()
    assert len(children) == 2
    assert {item.quantity for item in children} == {4}


def test_idempotency_returns_same_order_without_duplicate_position(db_session):
    fresh_stock_quote()
    request = stock_request()
    first = execute_paper_order(db_session, request, settings(), now=NOW)
    second = execute_paper_order(db_session, request, settings(), now=NOW)
    assert second["duplicate"] is True
    assert second["order_id"] == first["order_id"]
    assert db_session.scalar(select(TradingPosition).where(TradingPosition.symbol == "NVDA")).quantity == 10


def test_stale_price_blocks_preview_and_execution(db_session):
    fresh_stock_quote(NOW - timedelta(seconds=30))
    with pytest.raises(HTTPException, match="قديمة"):
        preview_order(db_session, stock_request(), settings(), now=NOW)


def test_selling_more_than_owned_is_blocked(db_session):
    fresh_stock_quote()
    execute_paper_order(db_session, stock_request(), settings(), now=NOW)
    with pytest.raises(HTTPException, match="أكبر من الكمية المملوكة"):
        preview_order(
            db_session,
            stock_request(idempotency_key="paper-sell-0002", side="sell", quantity=11, limit_price=100),
            settings(),
            now=NOW,
        )


def test_filled_oco_cancels_its_sibling(db_session):
    fresh_stock_quote()
    result = execute_paper_order(db_session, stock_request(), settings(), now=NOW)
    children = db_session.scalars(
        select(PaperOrder).where(PaperOrder.parent_order_id == uuid.UUID(result["order_id"]))
    ).all()
    target = next(item for item in children if item.order_role == "take_profit")
    stop = next(item for item in children if item.order_role == "stop_loss")
    fill_oco_order(db_session, target.id, 10)
    db_session.refresh(stop)
    assert stop.status == "cancelled"
    assert db_session.scalar(select(TradingPosition).where(TradingPosition.symbol == "NVDA")).status == "closed"


def test_partial_oco_fill_reduces_sibling_and_position_without_overselling(db_session):
    fresh_stock_quote()
    result = execute_paper_order(db_session, stock_request(), settings(), now=NOW)
    children = db_session.scalars(
        select(PaperOrder).where(PaperOrder.parent_order_id == uuid.UUID(result["order_id"]))
    ).all()
    target = next(item for item in children if item.order_role == "take_profit")
    stop = next(item for item in children if item.order_role == "stop_loss")
    fill_oco_order(db_session, target.id, 4)
    db_session.refresh(stop)
    position = db_session.scalar(select(TradingPosition).where(TradingPosition.symbol == "NVDA"))
    assert target.status == "partially_filled"
    assert stop.status == "open"
    assert stop.quantity == 6
    assert position.quantity == 6


def test_manual_partial_sell_reduces_both_open_oco_children(db_session):
    fresh_stock_quote()
    result = execute_paper_order(db_session, stock_request(), settings(), now=NOW)
    sell = stock_request(
        idempotency_key="manual-sell-0001", side="sell", quantity=4,
        limit_price=100, take_profit=None, stop_loss=None,
    )
    execute_paper_order(db_session, sell, settings(), now=NOW)
    children = db_session.scalars(
        select(PaperOrder).where(PaperOrder.parent_order_id == uuid.UUID(result["order_id"]))
    ).all()
    assert {item.quantity for item in children} == {6}


def test_option_requires_limit_price_at_schema_boundary():
    response = TestClient(app).post(
        "/api/v1/trading/orders/preview",
        json={"idempotency_key": "option-order-001", "side": "buy", "instrument_type": "option", "symbol": "NVDA270115C00200000", "quantity": 1},
    )
    assert response.status_code == 422


def test_no_live_order_endpoint_exists():
    response = TestClient(app).post("/api/v1/trading/orders/live", json={})
    assert response.status_code == 404


def test_sahm_adapter_rejects_password_otp_and_cookie_material():
    base = {"cash": 1000, "buying_power": 1000, "captured_at": NOW, "positions": [], "orders": [], "quotes": []}
    for forbidden in ("password", "otp", "cookie", "access_token"):
        with pytest.raises(ValueError, match="authentication material"):
            SahmAdapter().normalize_snapshot({**base, forbidden: "secret"})
    with pytest.raises(ValueError, match="authentication material"):
        SahmAdapter().normalize_snapshot({**base, "orders": [{"cookie": "secret"}]})


def test_bridge_sync_requires_its_separate_shared_token(monkeypatch, db_session):
    bridge_settings = settings(trading_bridge_enabled=True, trading_bridge_token="bridge-test-token")
    monkeypatch.setattr(routes_trading, "get_settings", lambda: bridge_settings)
    payload = SahmBridgePayload(
        cash=1000, buying_power=1200, daily_pnl=25, positions=[], orders=[], quotes=[], captured_at=NOW
    )
    with pytest.raises(HTTPException, match="غير صالح"):
        routes_trading.sync_sahm_bridge(payload, x_bridge_token="wrong", db=db_session)
    accepted = routes_trading.sync_sahm_bridge(
        payload, x_bridge_token="bridge-test-token", db=db_session
    )
    assert accepted["status"] == "accepted"
    stored = db_session.get(TradingBridgeSnapshot, "sahm")
    assert stored.account_json == {"cash": 1000.0, "buying_power": 1200.0, "daily_pnl": 25.0}


def test_stale_bridge_snapshot_is_not_shown_as_connected(db_session):
    db_session.add(
        TradingBridgeSnapshot(
            adapter="sahm", connection_status="connected", account_json={}, positions_json=[{"symbol": "AAPL"}],
            orders_json=[], quotes_json=[], synced_at=NOW - timedelta(seconds=40),
        )
    )
    db_session.commit()
    payload = room_snapshot(db_session, settings(), now=NOW)
    assert payload["bridge"]["sahm_status"] == "disconnected"
    assert payload["bridge_positions"] == []
    assert payload["account"]["source"] == "paper"


def test_fresh_bridge_portfolio_is_read_only_and_drives_display_balance(db_session):
    db_session.add(
        TradingBridgeSnapshot(
            adapter="sahm", connection_status="connected",
            account_json={"cash": 2500, "buying_power": 4000, "daily_pnl": 75},
            positions_json=[{"instrument_type": "stock", "symbol": "AAPL", "underlying_symbol": "AAPL", "quantity": 2, "average_price": 200, "current_price": 205}],
            orders_json=[], quotes_json=[], synced_at=NOW - timedelta(seconds=2),
        )
    )
    db_session.commit()
    payload = room_snapshot(db_session, settings(), now=NOW)
    assert payload["bridge"]["sahm_status"] == "connected"
    assert payload["account"] == {
        "cash": 2500.0, "buying_power": 4000.0, "daily_pnl": 75.0,
        "emergency_stop": False, "source": "sahm_read_only",
    }
    assert payload["paper_account"]["source"] == "paper"
    assert payload["bridge_positions"][0]["symbol"] == "AAPL"
