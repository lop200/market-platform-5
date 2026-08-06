from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.config import Settings
from app.db.models import TradeIntent, TradingAuditLog
from app.trading.intent import create_trade_intent, intent_payload, size_intent
from app.trading.schemas import IntentSizingRequest

NOW = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)


def settings(**overrides):
    return Settings(database_url="sqlite://", **overrides)


def valid_analysis(**overrides):
    payload = {
        "symbol": "NVDA",
        "status": "conditional_entry",
        "data_quality": {"valid_for_plan": True},
        "quote": {"bid": 100.0, "ask": 100.5, "spread_pct": 0.5, "age_seconds": 2},
        "trade_plan": {
            "entry_from": 100.25,
            "stop": 98.0,
            "targets": [{"price": 104.0}, {"price": 107.0}],
            "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
            "position_size": {"shares": 5},
            "invalidation": ["كسر الدعم", "اتساع السبريد"],
        },
        "time_estimate": {"expected": "خلال 30–90 دقيقة"},
        "analysis_summary_ar": "زخم واتجاه وسيولة مستوفية للشروط الرقمية.",
        "options": {"stock_first_gate_passed": False, "ranked_contracts": []},
    }
    payload.update(overrides)
    return payload


def test_temporal_trade_intent_is_deterministic_after_stock_gate(db_session):
    created = create_trade_intent(db_session, valid_analysis(), settings(), now=NOW)
    payload = intent_payload(created, now=NOW)
    assert db_session.get(TradeIntent, created.id) is created
    assert payload["instrument_type"] == "stock"
    assert payload["quantity"] == 5
    assert payload["expected_holding_period"] == "scalp"
    assert payload["entry_valid_until"] == (NOW + timedelta(minutes=10)).isoformat()
    assert payload["signal_age_seconds"] == 2
    assert payload["execution_allowed"] is True
    assert payload["confirmation_required"] is True
    assert payload["estimated_fees_usd"]["total_usd"] > 0
    assert payload["estimated_net_profit_usd"] < payload["estimated_profit_usd"]
    assert db_session.query(TradingAuditLog).filter_by(event_type="intent_created").count() == 1


def test_no_trade_analysis_never_creates_stock_or_option_intent(db_session):
    with pytest.raises(HTTPException, match="لا توجد فرصة سهم صالحة"):
        create_trade_intent(
            db_session,
            valid_analysis(status="no_trade", trade_plan=None),
            settings(options_enabled=True),
            now=NOW,
        )


def test_expired_signal_never_creates_intent(db_session):
    analysis = valid_analysis()
    analysis["trade_plan"]["expires_at"] = (NOW - timedelta(seconds=1)).isoformat()
    with pytest.raises(HTTPException, match="انتهى وقت الدخول"):
        create_trade_intent(db_session, analysis, settings(), now=NOW)


def test_stale_or_wide_stock_quote_blocks_intent(db_session):
    stale = valid_analysis()
    stale["quote"]["age_seconds"] = 999
    with pytest.raises(HTTPException, match="السعر قديم"):
        create_trade_intent(db_session, stale, settings(), now=NOW)
    wide = valid_analysis()
    wide["quote"]["spread_pct"] = 10
    with pytest.raises(HTTPException, match="السبريد"):
        create_trade_intent(db_session, wide, settings(), now=NOW)


def test_options_remain_isolated_when_feature_flag_is_off(db_session):
    analysis = valid_analysis(options={
        "stock_first_gate_passed": True,
        "ranked_contracts": [{
            "actionable": True, "symbol": "NVDA260821C00100000", "dte": 15,
            "spread_pct": 4, "volume": 100, "open_interest": 500,
            "delta": .5, "gamma": .02, "theta": -.05, "vega": .1,
            "entry_price": 2.0, "target_1": 2.8, "stop_loss": 1.5,
            "recommended_contracts": 1,
        }],
    })
    created = create_trade_intent(db_session, analysis, settings(options_enabled=False), now=NOW)
    assert created.instrument_type == "stock"


def test_intent_sizing_uses_smaller_cash_and_stop_risk_limit(db_session):
    created = create_trade_intent(db_session, valid_analysis(), settings(), now=NOW)
    payload = size_intent(
        db_session,
        created,
        IntentSizingRequest(buying_power=10_000, risk_pct=1),
        settings(trading_max_order_value_usd=5_000),
        now=NOW,
    )
    # Cash permits 49 shares; $100 risk / $2.25 loss permits 44.
    assert payload["quantity"] == 44
    assert payload["estimated_loss_usd"] == 99.0
    assert payload["payload"]["sizing"]["estimated_only"] is True
    assert payload["payload"]["analysis"]["trend"] is None
    assert payload["estimated_net_profit_usd"] > 0


def test_portfolio_sizing_can_rescue_a_one_share_plan_that_does_not_cover_fees(db_session):
    created = create_trade_intent(db_session, valid_analysis(), settings(), now=NOW)
    created.quantity = 1
    created.take_profit = 101.5
    db_session.commit()
    assert intent_payload(created, now=NOW)["execution_allowed"] is False
    resized = size_intent(
        db_session,
        created,
        IntentSizingRequest(buying_power=10_000, risk_pct=1),
        settings(trading_max_order_value_usd=5_000),
        now=NOW,
    )
    assert resized["quantity"] == 44
    assert resized["execution_allowed"] is True


def test_manifest_v3_extension_uses_minimum_hosts_and_manual_confirmation():
    root = Path(__file__).resolve().parents[1] / "browser-extension"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["manifest_version"] == 3
    assert manifest["version"] == "0.4.0"
    assert set(manifest["permissions"]) == {"tabs", "storage", "scripting"}
    assert set(manifest["host_permissions"]) == {
        "https://market-platform-5.onrender.com/*",
        "https://app.sahmcapital.com/*",
    }
    sahm = (root / "content-sahm.js").read_text(encoding="utf-8")
    background = (root / "background.js").read_text(encoding="utf-8")
    mirsad = (root / "content-mirsad.js").read_text(encoding="utf-8")
    assert "إرسال الأمر الآن" in sahm
    assert "confirmMode" in sahm
    assert "document.cookie" not in sahm
    assert "Market Order" not in sahm
    assert "CONNECT_SAHM" in background
    assert "PING_SAHM" in background
    assert "marsad:connect-sahm" in mirsad
    assert 'event.data.type === "CONNECT_SAHM"' in mirsad
    assert 'source: "marsad-extension"' in mirsad
    assert "data-marsad-extension" in mirsad
    assert "bridge_ready" in sahm
    assert "diagnostics" in sahm
    assert "semanticInput" in sahm
    assert "firstVisible" in sahm
    assert "ensureTradePanel" in sahm
    assert "dedicatedSearch" in sahm
    assert ".some(visible)" in sahm
    assert "InputEvent" in sahm
    assert "typeSearch" in sahm
    assert "n-base-select-option" in sahm
    assert 'pressKey(search,"ArrowDown")' in sahm
    assert "chooseStock" in sahm
    assert "Attached Order" in sahm
    assert 'intent.instrument_type !== "stock"' in sahm
    assert "verifyFilledOrder(intent)" in sahm
    assert 'let stage="review"' in sahm
    assert 'if(stage==="review")' in sahm
    assert "finalSubmit.click()" in sahm
    assert "password.value" not in sahm
    assert "setInterval(publishSnapshot,10000)" in sahm
    assert "portfolio_captured_at" in background
    assert "previousState" in background
    assert 'state: "error"' in background
    assert "chrome.runtime.onInstalled" in background
    assert "stored.confirmMode !== false" in background
