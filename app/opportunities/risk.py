from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionPlan:
    shares: int
    position_value_usd: float
    max_loss_sar: float
    capital_used_pct: float
    estimated_profit_sar: list[float]


def risk_reward(entry: float, stop: float, target: float) -> float:
    risk = abs(entry - stop)
    reward = abs(target - entry)
    return round(reward / risk, 2) if risk > 0 else 0.0


def position_size(
    capital_sar: float,
    risk_pct: float,
    entry: float,
    stop: float,
    targets: list[float],
    usd_sar_rate: float = 3.75,
) -> PositionPlan:
    capital_usd = capital_sar / usd_sar_rate
    risk_budget_usd = capital_usd * risk_pct / 100
    risk_per_share = abs(entry - stop)
    if risk_per_share <= 0:
        return PositionPlan(0, 0, 0, 0, [])
    by_risk = int(risk_budget_usd / risk_per_share + 1e-9)
    by_cash = int(capital_usd / entry)
    shares = max(0, min(by_risk, by_cash))
    value = shares * entry
    loss_sar = shares * risk_per_share * usd_sar_rate
    profits = [round(shares * abs(target - entry) * usd_sar_rate, 2) for target in targets]
    return PositionPlan(
        shares=shares,
        position_value_usd=round(value, 2),
        max_loss_sar=round(loss_sar, 2),
        capital_used_pct=round(value / capital_usd * 100, 1) if capital_usd else 0,
        estimated_profit_sar=profits,
    )
