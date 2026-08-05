from app.opportunities.scoring import (
    build_stock_scorecard,
    finalize_scorecard_with_options,
)


def stock_scorecard(*, rvol: float, spread: float = 0.08, dollars: float = 80_000_000):
    return build_stock_scorecard(
        indicators={"ema9": 105, "ema20": 103, "ema50": 100, "relative_volume": rvol},
        strategy_match_pct=95,
        strategy_checks=[{"passed": True}, {"passed": True}, {"passed": True}],
        spread_pct=spread,
        dollar_volume=dollars,
        quote_age_seconds=2,
        data_valid=True,
        timeframe_alignment={"1m": "صاعد", "5m": "صاعد", "1h": "صاعد", "daily": "صاعد"},
    )


def test_low_rvol_caps_confidence_even_when_every_timeframe_is_bullish():
    weak = stock_scorecard(rvol=0.35)
    strong = stock_scorecard(rvol=1.8)

    assert weak["trend_score"] == 100
    assert weak["volume_score"] == 25
    assert weak["stock_confidence_score"] <= 49
    assert weak["stock_confidence_score"] < strong["stock_confidence_score"]
    assert any("RVOL أقل من 0.5" in reason for reason in weak["caps_applied_ar"])


def test_spread_and_liquidity_materially_change_entry_and_risk_scores():
    liquid = stock_scorecard(rvol=1.4, spread=0.05, dollars=150_000_000)
    thin = stock_scorecard(rvol=1.4, spread=2.5, dollars=200_000)

    assert liquid["entry_conditions_score"] > thin["entry_conditions_score"]
    assert liquid["risk_score"] < thin["risk_score"]
    assert liquid["spread_score"] > thin["spread_score"]
    assert liquid["liquidity_score"] > thin["liquidity_score"]


def test_missing_options_keeps_final_confidence_incomplete_and_unscored():
    result = finalize_scorecard_with_options(stock_scorecard(rvol=1.8), None)

    assert result["trade_success_probability_pct"] is None
    assert result["trade_success_status"] == "unscored"
    assert result["options_quality_score"] is None
    assert result["final_confidence_score"] is None
    assert not result["evaluation_complete"]


def test_fresh_complete_opra_contract_completes_the_composite_score():
    options = {
        "ranked_contracts": [{
            "feed": "opra",
            "quote_age_seconds": 3,
            "iv": 0.42,
            "delta": 0.55,
            "theta": -0.08,
            "gamma": 0.03,
            "dte": 15,
            "options_quality_score": 82,
            "ranking_components": {"options_quality": 82},
        }]
    }
    result = finalize_scorecard_with_options(stock_scorecard(rvol=1.8), options)

    assert result["options_quality_score"] == 82
    assert result["final_confidence_score"] is not None
    assert result["evaluation_complete"]
    assert result["final_confidence_status"] == "complete"


def test_stale_or_missing_greeks_contract_cannot_complete_the_score():
    options = {
        "ranked_contracts": [{
            "feed": "opra", "quote_age_seconds": 90, "iv": 0.4,
            "delta": 0.5, "theta": -0.1, "gamma": None, "dte": 14,
            "options_quality_score": 95,
        }]
    }
    result = finalize_scorecard_with_options(stock_scorecard(rvol=1.8), options)

    assert result["final_confidence_score"] is None
    assert result["final_confidence_status"] == "incomplete_options_data"
