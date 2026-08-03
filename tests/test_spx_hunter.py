from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
from fastapi.testclient import TestClient

from app.config import Settings
from app.api import routes_spx
from app.db import repository
from app.db.models import SPXSyntheticObservation
from app.main import app
from app.options.market_clock import market_session
from app.spx.engine import (
    breakout_outlook,
    directional_scenario,
    escape_reason,
    rank_contracts,
    technical_analysis,
)
from app.spx import review as spx_review
from app.spx.review import SPXReview, review_spx
from app.spx.schemas import (
    Direction,
    SPXContract,
    SPXProviderCapabilities,
    SPXQuote,
    SPXSyntheticValue,
    StrikeMode,
)
from app.spx.service import SPXHunterService, build_synthetic_review_payload

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


def test_breakout_outlook_has_conditional_call_and_put_percentages():
    technical = technical_analysis(history(rising=False), quote(5399))
    outlook = breakout_outlook(
        technical, news_impact_score=88, data_quality_score=95
    )

    assert outlook["ready"] is True
    assert outlook["put_trigger"] < technical["price"]
    assert outlook["call_trigger"] > technical["price"]
    assert 35 <= outlook["call_probability_pct"] <= 88
    assert 35 <= outlook["put_probability_pct"] <= 88
    assert outlook["put_probability_pct"] > outlook["call_probability_pct"]
    assert outlook["estimate_type"] == "conditional_continuation"


def test_breakout_outlook_withholds_percentages_until_trend_ready():
    outlook = breakout_outlook({
        "trend_ready": False,
        "price": 5500,
        "support": 5498,
        "resistance": 5502,
        "expected_move": 2,
    })

    assert outlook["ready"] is False
    assert outlook["call_probability_pct"] is None
    assert outlook["put_probability_pct"] is None


def test_synthetic_intraday_series_builds_todays_direction(db_session):
    values = [5500.0, 5500.8, 5501.7, 5502.5, 5503.4]
    for index, value in enumerate(values):
        db_session.add(SPXSyntheticObservation(
            observed_at=NOW - timedelta(minutes=5 - index),
            forward_value=value,
            spot_estimate=None,
            lower_bound=value - .25,
            upper_bound=value + .25,
            pairs_used=10,
            confidence_score=85,
            data_quality_score=90,
            expiration="2026-08-05",
            settlement_type="PM_CASH",
            source="Alpaca OPRA Synthetic",
            payload_json={},
        ))
    db_session.commit()
    synthetic = SPXSyntheticValue(
        synthetic_forward_value=5504.3,
        lower_bound=5504.0,
        upper_bound=5504.6,
        pairs_used=10,
        expiration_used="2026-08-05",
        settlement_type="PM_CASH",
        calculation_timestamp=NOW,
        confidence_score=85,
        data_quality_score=90,
        source="Alpaca OPRA Synthetic",
        provider_status="ready",
        status_message_ar="جاهز",
    )
    result = SPXHunterService(db_session, settings())._synthetic_technical(synthetic)
    assert result["sample_size"] == 6
    assert result["trend_ready"] is True
    assert result["direction"] == "call"
    assert result["session_change_points"] == 4.3
    assert len(result["series"]) == 6

    payload = build_synthetic_review_payload(
        synthetic, result, None, [], []
    )
    assert payload["review_scope"] == "direction_only"
    assert payload["data_label"] == "estimated_synthetic_forward_not_official_spx"
    assert payload["technical_direction"]["direction"] == "call"
    assert len(payload["intraday_series"]) == 6
    assert payload["contracts"] == []


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


def test_openai_can_review_direction_without_a_contract(monkeypatch, db_session):
    parsed = SPXReview(
        approved=False,
        decision_ar="انتظر",
        explanation_ar="الاتجاه يحتاج تأكيدًا إضافيًا.",
        preferred_contract_symbol=None,
        contradictions_ar=["الزخم ضعيف"],
        risks_ar=["لا يوجد عقد مستوفٍ"],
    )

    class FakeResponses:
        def parse(self, **_kwargs):
            return SimpleNamespace(
                output_parsed=parsed,
                usage=SimpleNamespace(input_tokens=100, output_tokens=50),
            )

    fake_client = SimpleNamespace(responses=FakeResponses())
    monkeypatch.setattr(spx_review, "OpenAI", lambda **_kwargs: fake_client)
    result = review_spx(
        db_session,
        settings(spx_ai_review_enabled=True, openai_api_key="test-key"),
        {"review_scope": "direction_only", "contracts": []},
    )
    assert result["status"] == "completed"
    assert result["review_scope"] == "direction_only"
    assert result["preferred_contract_symbol"] is None


def test_snapshot_keeps_live_direction_but_blocks_stale_contracts(db_session):
    generated = datetime.now(timezone.utc) - timedelta(seconds=40)
    review = {
        "status": "completed",
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "reviewed_direction": "call",
        "decision_ar": "الاتجاه الصاعد مدعوم",
    }
    repository.cache_set(
        db_session,
        "spx:hunter:near",
        {
            "generated_at": generated.isoformat(),
            "status": "ready",
            "decision": "conditional_hunt",
            "decision_ar": "قنص مشروط",
            "reason_ar": "اختبار",
            "market": {"data_age_seconds": 0, "contracts_actionable": True},
            "technical": {"direction": "call", "trend_ready": True},
            "ai_review": review,
            "best_contract": {"symbol": "SPXW-TEST"},
            "ranked_contracts": [{"symbol": "SPXW-TEST"}],
        },
        datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    service = SPXHunterService(
        db_session,
        settings(
            spx_max_data_age_seconds=15,
            spx_direction_max_age_seconds=90,
        ),
    )
    result = service.snapshot(StrikeMode.NEAR)
    assert result["status"] == "monitoring"
    assert result["decision"] == "wait"
    assert result["technical"]["direction"] == "call"
    assert result["ai_review"] == review
    assert result["best_contract"] is None
    assert result["market"]["contracts_actionable"] is False
    assert service._preserved_ai_review(
        StrikeMode.NEAR, {"direction": "call"}
    ) == review


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
    assert "SPX المباشر — اتجاه اليوم" in html
    assert 'id="syntheticChart"' in html
    assert 'id="aiReview"' in html
    assert "احتمالات كسر SPX" in html
    assert "احتمال استمرار PUT" in html
    assert "احتمال استمرار CALL" in html
    assert "setInterval(load,10000)" in html
    assert "embed-widget-advanced-chart.js" not in html


def test_spx_snapshot_endpoint_never_waits_for_provider():
    response = TestClient(app).get("/api/v1/spx?strike_mode=near")
    assert response.status_code == 200
    assert response.json()["paper_only"] is True


def test_manual_spx_refresh_requests_bounded_ai_review(monkeypatch):
    calls = []

    class FakeDB:
        def rollback(self):
            raise AssertionError("refresh should not fail")

        def close(self):
            calls.append("closed")

    class FakeService:
        def __init__(self, db, app_settings):
            pass

        def refresh(self, strike_mode, *, allow_ai_review=False):
            calls.append((strike_mode, allow_ai_review))

    monkeypatch.setattr(routes_spx, "SessionLocal", FakeDB)
    monkeypatch.setattr(routes_spx, "SPXHunterService", FakeService)

    routes_spx._refresh(StrikeMode.NEAR)

    assert calls == [(StrikeMode.NEAR, True), "closed"]
