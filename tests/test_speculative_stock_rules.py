from app.config import Settings
from app.stocks.rules import (
    allowed_spread_to_target_pct,
    allowed_stock_spread_pct,
    required_risk_reward,
    required_strategy_match_pct,
)


def test_speculative_rules_are_relaxed_but_bounded_to_small_stocks():
    settings = Settings()
    assert allowed_stock_spread_pct(3, settings) == 4
    assert required_strategy_match_pct(3, settings) == 40
    assert required_risk_reward(3, settings) == 1.15
    assert allowed_spread_to_target_pct(3, settings) == 40
    assert allowed_stock_spread_pct(20, settings) == settings.max_spread_pct
    assert required_strategy_match_pct(20, settings) == settings.min_strategy_match_pct
