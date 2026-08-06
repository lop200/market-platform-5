from __future__ import annotations

from app.config import Settings


def speculative_stock(price: float | int | None, settings: Settings) -> bool:
    value = float(price or 0)
    return (
        settings.speculative_small_cap_enabled
        and settings.speculative_min_price <= value <= settings.speculative_max_price
    )


def allowed_stock_spread_pct(price: float | int | None, settings: Settings) -> float:
    if speculative_stock(price, settings) and float(price or 0) <= 5:
        return settings.speculative_max_spread_pct
    return settings.max_spread_pct


def required_strategy_match_pct(price: float | int | None, settings: Settings) -> int:
    return (
        settings.speculative_min_strategy_match_pct
        if speculative_stock(price, settings)
        else settings.min_strategy_match_pct
    )


def required_risk_reward(price: float | int | None, settings: Settings) -> float:
    return (
        settings.speculative_min_risk_reward
        if speculative_stock(price, settings)
        else settings.min_risk_reward
    )


def allowed_spread_to_target_pct(price: float | int | None, settings: Settings) -> float:
    return (
        settings.speculative_max_spread_to_target_pct
        if speculative_stock(price, settings)
        else settings.max_spread_to_target_pct
    )
