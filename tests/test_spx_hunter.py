from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.options.market_clock import market_session
from app.spx.engine import (
    directional_scenario,
    escape_reason,
    rank_contracts,
    technical_analysis,
)
from app.spx.review import review_spx
from app.spx.schemas import (
    Direction,
    SPXContract,
    SPXProviderCapabilities,
    SPXQuote,
    StrikeMode,
)
from app.spx.service import SPXHunterService

NOW = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)


def settings(**overrides) -> Settings:
    values = {
        "spx_enabled": True,
        "spx_allow_0dte": False,
        "spx_allow_1dte": False,
        "spx_max_spread_pct": 8,
        "spx_min_open_interest": 100,
        "spx_min_volume": 10,
        "spx_max_data_age_seconds": 30,
        "spx_ai_review_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def history(*, rising: bool = True) -> pd.DataFrame:
    end = 5500 if rising else 5400
    start = 5400 if rising else 5500
    closes = [start + (end - start) * i / 239 for i in range(240)]
    index = pd.date_range(end=NOW, periods=240, freq="min", tz="UTC")
    return pd.DataFrame({
        "open": closes,
        "high": [x + 1 for x in closes],
        "low": [x - 1 for x in closes],
        "close": closes,
        "volume": [1000 + i for i in range(240)],
    }, index=index)


def quote(price: float = 5501, age: int = 0) -> SPXQuote:
    stamp = NOW - timedelta(seconds=age)
    return SPXQuote(
        price=price, bid=price - .25, ask=price + .25,
        last_trade=price, quote_timestamp=stamp, trade_timestamp=stamp,
        source="test_fixture", is_realtime=True,
    )


def scenario(direction: Direction = Direction.CALL) -> dict:
    return {
        "entry": 5502, "stop": 5490,
        "targets": [5510, 5520, 5530] if direction == Direction.CALL else [5490, 5480, 5470],
        "risk_reward": 2,
    }


def contract(
    side: str = "call",
    *,
    dte: int = 3,
    strike: float = 5500,
    bid: float = 10,
    ask: float = 10.4,
    delta: float | None = .55,
    volume: int = 100,
    oi: int = 1000,
    age: int = 0,
) -> SPXContract:
    return SPXContract(
        symbol=f"SPXW26080{2+dte}{'C' if side == 'call' else 'P'}05500000",
        option_type=side,
        strike=strike,
        expiration=NOW + timedelta(days=dte),
        bid=bid, ask=ask, last=10.1, volume=volume, open_interest=oi,
        delta=delta if side == "call" else (-abs(delta) if delta is not None else None),
        gamma=.02, theta=-.5, vega=.3, iv=.2,
        quote_timestamp=NOW - timedelta(seconds=age),
        trade_timestamp=NOW, feed="opra",
    )


def test_spx_call_and_put_scenarios_are_deterministic():
    up = technical_analysis(history(rising=True), quote(5501))
    down = technical_analysis(history(rising=False), quote(5399))
    assert up["direction"] == "call"
    assert down["direction"] == "put"
    assert up["targets"][0] > up["entry"]
    assert down["targets"][0] < down["entry"]


def test_conflicting_signals_return_no_trade():
    technical = technical_analysis(history(rising=True), quote(5501))
    technical["direction"] = "none"
    direction, selected, label = directional_scenario(technical, [])
    assert direction == Direction.NONE
    assert selected is None
    assert label == "السوق متضارب"


def test_strong_opposing_fed_news_cancels_scenario():
    technical = technical_analysis(history(rising=True), quote(5501))
    direction, selected, label = directional_scenario(technical, [{
        "spx_impact_score": 95,
        "potential_direction_ar": "داعم للهبوط",
        "event_type": "fed",
    }])
    assert direction == Direction.NONE and selected is None
    assert label == "الخبر أقوى من التحليل"


def test_near_and_far_modes_use_separate_delta_ranges():
    session = market_session(NOW)
    near, _ = rank_contracts(
        [contract(delta=.55), contract(delta=.35, strike=5510)],
        direction=Direction.CALL, scenario=scenario(), underlying=5501,
        mode=StrikeMode.NEAR, settings=settings(), session=session, now=NOW,
    )
    far, _ = rank_contracts(
        [contract(delta=.55), contract(delta=.35, strike=5510)],
        direction=Direction.CALL, scenario=scenario(), underlying=5501,
        mode=StrikeMode.FAR, settings=settings(), session=session, now=NOW,
    )
    assert near and abs(near[0].delta) == .55
    assert far and abs(far[0].delta) == .35
    assert far[0].time_sensitivity == "مرتفعة"


def test_zero_and_one_dte_are_disabled_by_default():
    ranked, rejected = rank_contracts(
        [contract(dte=0), contract(dte=1), contract(dte=3)],
        direction=Direction.CALL, scenario=scenario(), underlying=5501,
        mode=StrikeMode.NEAR, settings=settings(), session=market_session(NOW), now=NOW,
    )
    assert len(ranked) == 1
    assert rejected["0dte_disabled"] == 1
    assert rejected["1dte_disabled"] == 1


def test_zero_dte_requires_manual_enablement():
    ranked, rejected = rank_contracts(
        [contract(dte=0)],
        direction=Direction.CALL, scenario=scenario(), underlying=5501,
        mode=StrikeMode.NEAR,
        settings=settings(spx_allow_0dte=True, spx_near_strike_delta_min=.45),
        session=market_session(NOW), now=NOW,
    )
    assert ranked and "0dte_disabled" not in rejected


def test_wide_spread_stale_missing_greeks_and_low_liquidity_are_rejected():
    missing = contract()
    missing.gamma = None
    ranked, rejected = rank_contracts(
        [
            contract(bid=5, ask=8),
            contract(age=31),
            missing,
            contract(volume=0, oi=0),
        ],
        direction=Direction.CALL, scenario=scenario(), underlying=5501,
        mode=StrikeMode.NEAR, settings=settings(), session=market_session(NOW), now=NOW,
    )
    assert not ranked
    assert rejected == {
        "wide_spread": 1,
        "stale_quote": 1,
        "missing_quote_or_greeks": 1,
        "low_liquidity": 1,
    }


def test_closed_market_never_makes_contract_actionable():
    closed = datetime(2026, 7, 30, 22, 0, tzinfo=timezone.utc)
    ranked, _ = rank_contracts(
        [contract(age=0)],
        direction=Direction.CALL, scenario=scenario(), underlying=5501,
        mode=StrikeMode.NEAR, settings=settings(),
        session=market_session(closed), now=NOW,
    )
    assert ranked and not ranked[0].actionable


def test_escape_for_closed_market_and_stale_data():
    regular = market_session(NOW)
    closed = market_session(datetime(2026, 7, 30, 22, 0, tzinfo=timezone.utc))
    assert escape_reason(technical={"direction": "call"}, session=closed, data_age=0, news=[], best=None, settings=settings()) == "سوق الخيارات مغلق"
    assert escape_reason(technical={"direction": "call"}, session=regular, data_age=31, news=[], best=None, settings=settings()) == "البيانات قديمة"


class NoUnderlyingProvider:
    provider_name = "test"

    def capabilities(self):
        return SPXProviderCapabilities(
            provider="test", checked_at=NOW, underlying_available=False,
            option_chain_available=True, opra_available=True, greeks_available=True,
            expirations_available=True, message_ar="بيانات SPX غير متاحة من المزود الحالي",
        )

    def get_quote(self):
        raise AssertionError("quote must not be fetched")

    def get_history(self):
        raise AssertionError("history must not be fetched")

    def get_chain(self, **kwargs):
        raise AssertionError("chain must not be fetched before valid SPX scenario")


def test_unavailable_spx_never_fetches_chain_or_substitutes_spy(db_session):
    result = SPXHunterService(
        db_session,
        settings(spx_underlying_provider="official"),
        NoUnderlyingProvider(),
    ).refresh()
    assert result["status"] == "underlying_unavailable"
    assert result["best_contract"] is None
    assert result["quote"] is None
    assert result["technical"] is None
    assert result["decision_ar"] == "اهرب الآن"


class BrokenProvider:
    def capabilities(self):
        raise RuntimeError("offline")


def test_provider_failure_is_safe_and_does_not_raise(db_session):
    result = SPXHunterService(db_session, settings(), BrokenProvider()).refresh()
    assert result["status"] == "provider_failed"
    assert result["best_contract"] is None


def test_openai_failure_or_missing_key_does_not_block_page(db_session):
    result = review_spx(db_session, settings(spx_ai_review_enabled=True, openai_api_key=None), {
        "contracts": [],
    })
    assert result["status"] == "not_configured"


def test_mobile_shell_has_no_horizontal_scroll_and_modes():
    html = TestClient(app).get("/spx").text
    assert 'dir="rtl"' in html
    assert 'name="viewport"' in html
    assert "overflow-x:hidden" in html
    assert "@media(max-width:500px)" in html
    assert 'id="near"' in html and 'id="far"' in html
    assert "قنّاص SPX" in html
    assert "اهرب الآن" in html
    assert "قنص مشروط" in html
    assert "مؤشر SPX الخارجي" in html
    assert '"symbol": "SP:SPX"' in html
    assert "s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js" in html
    assert "لا يدخل في حسابات القنص" in html


def test_spx_snapshot_endpoint_never_waits_for_provider():
    response = TestClient(app).get("/api/v1/spx?strike_mode=near")
    assert response.status_code == 200
    assert response.json()["paper_only"] is True
