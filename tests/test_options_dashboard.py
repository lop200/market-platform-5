from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.options.engine import rank_option_chain
from app.options.market_clock import market_session
from app.options.provider import parse_occ_symbol
from app.options.schemas import OptionType, RawOptionContract
from app.options.service import analyze_options_after_stock


NOW = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)


def stock(status: str = "conditional_entry", trend: str = "صاعد") -> dict:
    valid = status == "conditional_entry"
    bearish = trend == "هابط"
    return {
        "symbol": "AAPL",
        "status": status,
        "trend": trend,
        "quote": {
            "price": 200.0, "bid": 199.95, "ask": 200.05,
            "feed": "sip", "age_seconds": 2,
        },
        "data_quality": {"valid_for_plan": valid},
        "trade_plan": (
            {
                "entry_from": 200.0,
                "stop": 204.0 if bearish else 196.0,
                "targets": (
                    [{"price": 192.0}, {"price": 188.0}]
                    if bearish else [{"price": 208.0}, {"price": 212.0}]
                ),
                "risk_reward": 2.0,
                "valid_minutes": 10,
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=10)
                ).isoformat(),
            }
            if valid else None
        ),
    }


def contract(
    symbol: str,
    side: OptionType,
    *,
    expiry: date = date(2026, 8, 14),
    bid: float = 4.8,
    ask: float = 5.0,
    volume: int = 250,
    oi: int = 1200,
    quote_timestamp: datetime = NOW,
) -> RawOptionContract:
    return RawOptionContract(
        symbol=symbol,
        underlying_symbol="AAPL",
        option_type=side,
        strike=200,
        expiration=expiry,
        bid=bid,
        ask=ask,
        volume=volume,
        open_interest=oi,
        delta=.55 if side == OptionType.CALL else -.55,
        gamma=.03,
        theta=-.08,
        vega=.12,
        iv=.42,
        quote_timestamp=quote_timestamp,
        feed="opra",
    )


def enabled(**overrides) -> Settings:
    return Settings(
        options_enabled=True,
        options_min_dte=7,
        options_max_dte=30,
        options_max_quote_age_seconds=30,
        options_max_spread_pct=12,
        options_min_volume=10,
        options_min_open_interest=100,
        **overrides,
    )


def test_sip_and_opra_contract_symbol_are_explicit():
    root, expiry, side, strike = parse_occ_symbol("AAPL260814C00200000")
    assert (root, expiry, side, strike) == ("AAPL", date(2026, 8, 14), OptionType.CALL, 200)
    assert enabled().alpaca_feed == "sip"
    assert enabled().alpaca_options_feed == "opra"


def test_alpaca_chain_request_is_bounded_to_configured_dte(monkeypatch):
    from app.options.provider import AlpacaOptionProvider

    captured = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"snapshots": {}}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url, **kwargs):
            captured.update(kwargs["params"])
            return Response()

    monkeypatch.setattr("app.options.provider.httpx.Client", Client)
    provider = AlpacaOptionProvider(
        "key", "secret", min_dte=7, max_dte=30
    )
    assert provider.get_option_chain("QQQ") == []
    lower = date.fromisoformat(captured["expiration_date_gte"])
    upper = date.fromisoformat(captured["expiration_date_lte"])
    assert (upper - lower).days == 23
    assert captured["feed"] == "opra"
    assert captured["limit"] == 1000


def test_market_sessions_use_new_york_and_options_only_regular():
    pre = market_session(datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc))
    regular = market_session(NOW)
    after = market_session(datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc))
    assert pre.code == "pre_market" and not pre.options_actionable
    assert regular.code == "regular" and regular.options_actionable
    assert after.code == "after_hours" and not after.options_actionable
    assert regular.riyadh_time.tzinfo is not None


def test_holidays_and_early_closes_are_not_actionable_as_regular_options():
    holiday = market_session(datetime(2026, 12, 25, 16, 0, tzinfo=timezone.utc))
    good_friday = market_session(datetime(2026, 4, 3, 16, 0, tzinfo=timezone.utc))
    early_close_after = market_session(datetime(2026, 11, 27, 19, 0, tzinfo=timezone.utc))
    assert holiday.code == "holiday" and not holiday.options_actionable
    assert good_friday.code == "holiday" and not good_friday.options_actionable
    assert early_close_after.code == "after_hours" and not early_close_after.options_actionable


def test_dte_zero_one_and_outside_7_30_are_rejected():
    contracts = [
        contract("ZERO", OptionType.CALL, expiry=date(2026, 7, 30)),
        contract("ONE", OptionType.CALL, expiry=date(2026, 7, 31)),
        contract("SIX", OptionType.CALL, expiry=date(2026, 8, 5)),
        contract("THIRTYONE", OptionType.CALL, expiry=date(2026, 8, 30)),
        contract("VALID", OptionType.CALL, expiry=date(2026, 8, 14)),
    ]
    result = rank_option_chain(stock(), contracts, enabled(), now=NOW)
    assert [item.symbol for item in result.ranked_contracts] == ["VALID"]
    assert result.rejection_reasons["expired_or_0_1dte"] == 2
    assert result.rejection_reasons["dte"] == 2


def test_hard_gates_reject_bad_quotes_but_low_liquidity_is_soft_ranked():
    missing = contract("MISSING", OptionType.CALL)
    missing.delta = None
    result = rank_option_chain(
        stock(),
        [
            contract("WIDE", OptionType.CALL, bid=1, ask=2),
            contract("DRY", OptionType.CALL, volume=0, oi=3),
            contract("STALE", OptionType.CALL, quote_timestamp=datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)),
            missing,
        ],
        enabled(),
        now=NOW,
    )
    assert result.status == "monitoring"
    assert [item.symbol for item in result.ranked_contracts] == ["DRY"]
    assert not result.ranked_contracts[0].actionable
    assert any(
        "عدد العقود المفتوحة" in warning
        for warning in result.ranked_contracts[0].warnings_ar
    )
    assert result.rejection_reasons == {
        "wide_spread": 1,
        "stale_quote": 1,
        "missing_greeks": 1,
    }


class FailingIfCalled:
    provider_name = "fake"
    feed = "opra"

    def get_option_chain(self, symbol):
        raise AssertionError("option chain must not be called")


def test_no_trade_never_fetches_or_displays_a_contract():
    result = analyze_options_after_stock(stock("no_trade"), enabled(), FailingIfCalled())
    assert result.status == "no_trade"
    assert not result.stock_first_gate_passed
    assert result.ranked_contracts == []


def test_options_disabled_never_calls_provider_and_stock_remains_valid():
    result = analyze_options_after_stock(stock(), Settings(options_enabled=False), FailingIfCalled())
    assert result.status == "disabled"
    assert result.stock_first_gate_passed
    assert result.ranked_contracts == []


class BrokenOptions:
    provider_name = "broken"
    feed = "opra"

    def get_option_chain(self, symbol):
        raise RuntimeError("provider unavailable")


def test_options_api_failure_does_not_break_stock_analysis():
    result = analyze_options_after_stock(stock(), enabled(), BrokenOptions())
    assert result.status == "provider_failed"
    assert result.stock_first_gate_passed
    assert result.ranked_contracts == []


def test_call_and_put_include_deterministic_targets_risk_and_cost():
    call_result = rank_option_chain(
        stock(),
        [
            contract("AAPL260814C00200000", OptionType.CALL),
            contract("AAPL260814P00200000", OptionType.PUT),
        ],
        enabled(),
        now=NOW,
    )
    put_result = rank_option_chain(
        stock(trend="هابط"),
        [
            contract("AAPL260814C00200000", OptionType.CALL),
            contract("AAPL260814P00200000", OptionType.PUT),
        ],
        enabled(),
        now=NOW,
    )
    assert call_result.best_call and call_result.best_put
    assert put_result.best_put and put_result.best_call
    assert call_result.best_put.classification_ar in {"للمراقبة", "انتظر"}
    assert put_result.best_call.classification_ar in {"للمراقبة", "انتظر"}
    for item in (call_result.best_call, put_result.best_put):
        assert item.contract_cost == item.entry_price * 100
        assert item.target_2 > item.target_1 > item.mid > item.stop_loss
        assert 0 <= item.liquidity_score <= 100
        assert 0 <= item.suitability_score <= 100
        assert 0 <= item.risk_score <= 100
        assert item.feed == "opra"
        assert item.quote_age_seconds == 0
        assert item.paper_trading_only
        assert len(item.target_scenarios) == 3
        assert item.ranking_components["direction"] == 100
        assert item.entry_price != item.last
        assert item.intrinsic_value >= 0
        assert item.extrinsic_value >= 0
        assert item.volume_oi_ratio > 0
        assert item.exit_conditions_ar


def test_closed_pre_market_and_after_hours_never_show_entry_now():
    for instant in (
        datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 30, 21, 0, tzinfo=timezone.utc),
    ):
        stock_case = stock()
        stock_case["trade_plan"]["expires_at"] = (instant + timedelta(minutes=10)).isoformat()
        result = rank_option_chain(
            stock_case,
            [
                contract(
                    "AAPL260814C00200000",
                    OptionType.CALL,
                    quote_timestamp=instant,
                )
            ],
            enabled(),
            now=instant,
        )
        assert result.ranked_contracts
        best = result.ranked_contracts[0]
        assert not best.actionable
        assert "دخول مشروط بعد افتتاح السوق" in best.entry_instruction_ar
        assert "السوق مغلق" in best.status_badges_ar
        assert result.market["next_options_open_at"]


def test_delta_and_direction_are_soft_but_invalid_quote_and_deep_otm_are_hard():
    wrong_delta = contract("DELTA", OptionType.CALL)
    wrong_delta.delta = .2
    deep = contract("DEEP", OptionType.CALL)
    deep.strike = 225
    result = rank_option_chain(
        stock(),
        [
            contract("PUT", OptionType.PUT),
            contract("CROSSED", OptionType.CALL, bid=5, ask=4),
            wrong_delta,
            deep,
        ],
        enabled(),
        now=NOW,
    )
    assert result.status == "monitoring"
    assert result.rejection_reasons["invalid_quote"] == 1
    assert result.rejection_reasons["deep_otm"] == 1
    assert {item.symbol for item in result.ranked_contracts} == {"PUT", "DELTA"}
    assert any(
        "حساسية العقد لحركة السهم خارج النطاق" in warning
        for item in result.ranked_contracts
        for warning in item.warnings_ar
    )


def test_missing_quote_timestamp_is_stale_and_never_actionable():
    missing_time = contract("NO_TIME", OptionType.CALL)
    missing_time.quote_timestamp = None
    result = rank_option_chain(stock(), [missing_time], enabled(), now=NOW)
    assert result.status == "no_contract"
    assert result.rejection_reasons["stale_quote"] == 1
    assert result.market["options_status"] == "stale"


def test_mobile_laptop_shell_preserves_base_site_and_same_page_options():
    html = TestClient(app).get("/").text
    assert 'dir="rtl"' in html
    assert 'name="viewport"' in html
    assert "overflow-x:hidden" in html
    assert "@media(min-width:720px)" in html
    assert "@media(min-width:1100px)" in html
    for section_id in ("opportunities", "premarketOpportunities", "watchlist"):
        assert f'id="{section_id}"' in html
    for session_id in (
        "overnightHours", "premarketHours", "regularHours", "afterHours"
    ):
        assert f'id="{session_id}"' in html
    assert "الأسهم + أوبشن الشركات" in html
    assert "الأسهم فقط" in html
    stock_html = TestClient(app).get("/stocks/AAPL").text
    assert "overflow-x:hidden" in stock_html
    assert "@media(min-width:760px)" in stock_html
    assert 'id="options"' in stock_html


def test_dashboard_and_page_survive_without_openai_or_options_credentials():
    client = TestClient(app)
    assert client.get("/").status_code == 200
    response = client.get("/api/v1/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert body["paper_trading_only"] is True
    assert "market" in body and "feeds" in body
