from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.opportunities.audit import evaluate_timeline
from app.opportunities.risk import position_size, risk_reward
from app.opportunities.schemas import MarketRegime
from app.opportunities.strategies import STRATEGY_REGISTRY, select_strategy


def test_risk_reward_and_position_size_respect_cash_and_risk():
    assert risk_reward(3.0, 2.8, 3.4) == 2.0
    plan = position_size(750, 1, 3.0, 2.8, [3.4, 3.6])
    assert plan.shares == 10
    assert plan.max_loss_sar == 7.5
    assert plan.position_value_usd == 30
    assert plan.estimated_profit_sar[0] == 15


def test_no_trade_is_first_class_market_safety_strategy():
    choice = select_strategy({}, 3.0, MarketRegime.HIGH_RISK)
    assert choice.strategy_id == "no_trade"
    assert "no_trade" in STRATEGY_REGISTRY


def test_oversold_reversal_is_disabled_in_bear_market():
    choice = select_strategy(
        {"rsi": 25, "support": 3, "relative_volume": 2, "vwap": 3.2},
        3.01,
        MarketRegime.BEARISH,
    )
    assert choice.strategy_id == "no_trade"


def test_strategy_reports_weighted_verification_not_a_win_rate():
    choice = select_strategy(
        {
            "relative_volume": 1.6, "vwap": 10.0, "resistance": 10.1,
            "rsi": 58, "momentum": 0.8, "ema9": 10.2, "ema20": 10.0,
            "ema50": 9.8, "macd": .2, "macd_signal": .1,
            "trend_15m_bullish": True,
        },
        10.1,
        MarketRegime.BULLISH,
    )
    assert choice.strategy_id == "volume_breakout"
    assert choice.match_pct == 100
    assert choice.classification_ar == "تحقق قوي جدًا"
    assert sum(item["weight"] for item in choice.checks) == 100


def test_small_cap_momentum_is_a_five_minute_speculative_setup():
    choice = select_strategy(
        {
            "relative_volume": .6, "vwap": 3.0, "momentum": .2,
            "rsi": 62, "support": 2.7, "resistance": 3.4,
        },
        3.02,
        MarketRegime.CHOPPY,
    )
    assert choice.strategy_id == "small_cap_momentum"
    assert choice.match_pct >= 40
    assert choice.valid_minutes == 5
    assert "small_cap_momentum" in STRATEGY_REGISTRY


def test_stop_before_target_never_counts_as_success():
    now = datetime.now(timezone.utc)
    outcome = evaluate_timeline(
        [(now, 3.05), (now + timedelta(minutes=1), 2.89), (now + timedelta(minutes=2), 3.5)],
        3.0, 3.1, 2.9, 3.3, 3.5,
    )
    assert outcome.stop_hit
    assert not outcome.target_1_hit
    assert outcome.outcome == "stopped"
