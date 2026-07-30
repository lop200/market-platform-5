from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app
from app.options.engine import rank_option_chain
from app.options.schemas import OptionType, RawOptionContract
from app.options.service import analyze_options_after_stock
from app.options.sniper import ShortDTEOptionSniper


NOW = datetime(2026, 7, 30, 15, 0, tzinfo=timezone.utc)


def settings(**overrides) -> Settings:
    values = {
        "options_enabled": True,
        "options_paper_only": True,
        "options_scalp_mode": True,
        "options_scalp_min_dte": 0,
        "options_scalp_max_dte": 2,
        "options_scalp_fallback_max_dte": 7,
        "options_scalp_max_strikes_from_atm": 2,
        "options_min_dte": 7,
        "options_max_dte": 30,
        "options_max_quote_age_seconds": 30,
        "options_max_spread_pct": 12,
        "options_min_volume": 10,
        "options_min_open_interest": 100,
        "options_account_size_usd": 5_000,
        "options_max_contract_cost_usd": 500,
        "options_preferred_contract_cost_usd": 250,
    }
    values.update(overrides)
    return Settings(**values)


def stock(trend: str = "صاعد") -> dict:
    bearish = trend == "هابط"
    return {
        "symbol": "AAPL",
        "status": "conditional_entry",
        "trend": trend,
        "overall_score": 82,
        "quote": {
            "price": 200.0,
            "bid": 199.98,
            "ask": 200.02,
            "feed": "sip",
            "age_seconds": 1,
        },
        "data_quality": {"valid_for_plan": True},
        "indicators": {"relative_volume": 1.8},
        "strategy": {
            "name": "Breakout Retest",
            "trigger": "اختراق 200 والثبات فوقه مع حجم.",
        },
        "trade_plan": {
            "entry_from": 200.0,
            "stop": 204.0 if bearish else 196.0,
            "targets": (
                [{"price": 192.0}, {"price": 188.0}]
                if bearish
                else [{"price": 208.0}, {"price": 212.0}]
            ),
            "risk_reward": 2.0,
            "valid_minutes": 10,
            "expires_at": "2026-07-30T15:10:00+00:00",
        },
    }


def contract(
    symbol: str,
    *,
    dte: int,
    strike: float = 200,
    side: OptionType = OptionType.CALL,
    bid: float = 1.90,
    ask: float = 2.00,
    delta: float | None = None,
    quote_timestamp: datetime | None = NOW,
    volume: int = 250,
    open_interest: int = 1_000,
) -> RawOptionContract:
    return RawOptionContract(
        symbol=symbol,
        underlying_symbol="AAPL",
        option_type=side,
        strike=strike,
        expiration=date(2026, 7, 30) + timedelta(days=dte),
        bid=bid,
        ask=ask,
        volume=volume,
        open_interest=open_interest,
        delta=delta if delta is not None else (
            0.55 if side == OptionType.CALL else -0.55
        ),
        gamma=0.04,
        theta=-0.09,
        vega=0.11,
        iv=0.42,
        quote_timestamp=quote_timestamp,
        feed="opra",
    )


def test_sniper_prioritizes_zero_to_two_dte_and_same_direction():
    result = rank_option_chain(
        stock(),
        [
            contract("AAPL-0-C", dte=0),
            contract("AAPL-1-C", dte=1, strike=201, bid=1.45, ask=1.52),
            contract("AAPL-2-P", dte=2, side=OptionType.PUT),
            contract("AAPL-4-C", dte=4),
            contract("AAPL-14-C", dte=14),
        ],
        settings(),
        now=NOW,
    )
    assert result.scalp_summary["engine"] == "ShortDTEOptionSniper"
    assert result.scalp_summary["dte_stage"] == "primary"
    assert result.ranked_contracts
    assert all(item.dte <= 2 for item in result.ranked_contracts)
    assert all(item.option_type == OptionType.CALL for item in result.ranked_contracts)
    assert "العقود التي تنتهي اليوم قد تفقد معظم قيمتها خلال دقائق." in result.warnings_ar
    assert result.ranked_contracts[0].time_stop_minutes in {5, 10}


def test_sniper_uses_absolute_put_delta_and_falls_back_to_three_to_seven_dte():
    result = rank_option_chain(
        stock("هابط"),
        [
            contract(
                "AAPL-4-P",
                dte=4,
                side=OptionType.PUT,
                delta=-0.52,
            ),
            contract("AAPL-14-P", dte=14, side=OptionType.PUT, delta=-0.5),
        ],
        settings(),
        now=NOW,
    )
    assert result.ranked_contracts[0].symbol == "AAPL-4-P"
    assert result.scalp_summary["dte_stage"] == "short_fallback"
    assert any("3 إلى 7 أيام" in warning for warning in result.warnings_ar)


def test_strict_mode_refuses_monthly_instead_of_falling_back():
    """Strict mode must say "no hunt" rather than serving a weeks-out contract."""
    result = rank_option_chain(
        stock(),
        [contract("AAPL-21-C", dte=21), contract("AAPL-28-C", dte=28, strike=201)],
        settings(),
        now=NOW,
    )
    assert result.ranked_contracts == []
    assert result.scalp_summary["dte_stage"] == "no_short_dte"
    assert any("لا يوجد عقد مناسب" in warning for warning in result.warnings_ar)


def test_non_strict_mode_still_reaches_the_monthly_rung():
    result = rank_option_chain(
        stock(),
        [contract("AAPL-21-C", dte=21), contract("AAPL-28-C", dte=28, strike=201)],
        settings(options_scalp_strict=False),
        now=NOW,
    )
    assert [item.symbol for item in result.ranked_contracts][:1] == ["AAPL-21-C"]
    assert result.scalp_summary["dte_stage"] == "standard_fallback"


def test_strict_mode_still_prefers_primary_when_short_dte_exists():
    result = rank_option_chain(
        stock(),
        [contract("AAPL-1-C", dte=1), contract("AAPL-28-C", dte=28, strike=201)],
        settings(),
        now=NOW,
    )
    assert result.scalp_summary["dte_stage"] == "primary"
    assert [item.symbol for item in result.ranked_contracts] == ["AAPL-1-C"]


def test_sniper_never_accepts_cheap_bad_or_stale_quotes():
    stale = datetime(2026, 7, 30, 14, 59, tzinfo=timezone.utc)
    missing_greeks = contract("MISSING", dte=1)
    missing_greeks.gamma = None
    result = rank_option_chain(
        stock(),
        [
            contract("ZERO-BID", dte=0, bid=0, ask=0.05),
            contract("WIDE", dte=1, bid=0.05, ask=0.25),
            contract("STALE", dte=2, bid=0.10, ask=0.11, quote_timestamp=stale),
            missing_greeks,
        ],
        settings(),
        now=NOW,
    )
    assert result.ranked_contracts == []
    assert result.rejection_reasons["invalid_quote"] == 1
    assert result.rejection_reasons["wide_spread"] == 1
    assert result.rejection_reasons["stale_quote"] == 1
    assert result.rejection_reasons["missing_greeks"] == 1


def test_sniper_budget_gate_does_not_manufacture_a_cheap_contract():
    result = rank_option_chain(
        stock(),
        [contract("EXPENSIVE", dte=1, bid=7.9, ask=8.0)],
        settings(options_max_contract_cost_usd=300),
        now=NOW,
    )
    assert result.ranked_contracts == []
    assert result.rejection_reasons["over_budget"] == 1
    assert any("مناسب للميزانية" in warning for warning in result.warnings_ar)


def test_sniper_limits_universe_to_five_nearest_strikes():
    contracts = [
        contract(f"C-{strike}", dte=1, strike=strike)
        for strike in range(194, 207)
    ]
    universe = ShortDTEOptionSniper(settings()).select_universe(
        contracts, underlying_price=200.2, now=NOW
    )
    assert len(universe.allowed_strikes) == 5
    assert universe.atm_strike == 200
    assert set(item.strike for item in universe.contracts) == set(
        universe.allowed_strikes
    )


def test_sniper_modes_and_mobile_section_are_visible():
    result = rank_option_chain(
        stock(),
        [
            contract("ATM", dte=1, strike=200, bid=1.9, ask=2.0),
            contract("CHEAP", dte=1, strike=199, bid=1.0, ask=1.06),
            contract("OTM", dte=1, strike=201, bid=0.8, ask=0.85),
        ],
        settings(),
        now=NOW,
    )
    modes = result.scalp_summary["modes"]
    assert modes["near_safe"]
    assert modes["cheapest_acceptable"]
    assert any(item.sniper_mode_ar for item in result.ranked_contracts)
    html = TestClient(app).get("/stocks/AAPL").text
    assert "قنص العقود القصيرة" in html
    assert "الأقرب والأكثر أمانًا" in html
    assert "overflow-x:hidden" in html


def test_scalp_feature_flag_keeps_zero_dte_disabled_by_default():
    result = rank_option_chain(
        stock(),
        [contract("ZERO", dte=0), contract("STANDARD", dte=14)],
        settings(options_scalp_mode=False),
        now=NOW,
    )
    assert [item.symbol for item in result.ranked_contracts] == ["STANDARD"]
    assert result.scalp_summary == {}


def _valid_stock(symbol: str) -> dict:
    payload = stock()
    payload["symbol"] = symbol
    return payload


def test_symbol_without_short_term_options_is_refused_before_calling_opra():
    """Small caps have no short-dated chain; say so instead of calling OPRA."""

    class ExplodingProvider:
        def get_option_chain(self, *args, **kwargs):  # pragma: no cover
            raise AssertionError("OPRA must not be called for excluded symbols")

    result = analyze_options_after_stock(
        _valid_stock("FFAI"), settings(), ExplodingProvider()
    )
    assert result.status == "no_short_term_options"
    assert any("ليس له عقود أوبشن تنتهي خلال أيام" in warning for warning in result.warnings_ar)


def test_symbol_with_short_term_options_reaches_the_provider():
    captured: list[str] = []

    class RecordingProvider:
        def get_option_chain(self, symbol, price):
            captured.append(symbol)
            return [contract("SPY-1-C", dte=1)]

    result = analyze_options_after_stock(
        _valid_stock("SPY"), settings(), RecordingProvider()
    )
    assert captured == ["SPY"]
    assert result.status != "no_short_term_options"
