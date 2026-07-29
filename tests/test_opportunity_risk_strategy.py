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


def test_stop_before_target_never_counts_as_success():
    now = datetime.now(timezone.utc)
    outcome = evaluate_timeline(
        [(now, 3.05), (now + timedelta(minutes=1), 2.89), (now + timedelta(minutes=2), 3.5)],
        3.0, 3.1, 2.9, 3.3, 3.5,
    )
    assert outcome.stop_hit
    assert not outcome.target_1_hit
    assert outcome.outcome == "stopped"
