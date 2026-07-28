"""Expected Move and stock-level -> contract-price translation (SRS 11.1, Annex C-2/M2-B).

IV Rank/Percentile (SRS 11.1) needs a 252-session IV history from a live options chain,
which M2-B doesn't have (that's M5, pending the options-data-provider decision). Both
functions here only need the single implied vol solved from the uploaded screenshot.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import sqrt


@dataclass(frozen=True)
class ExpectedMove:
    dollar_amount: float
    pct_of_price: float
    upper_bound: float
    lower_bound: float


def compute_expected_move(
    underlying_price: float, implied_vol: float, time_to_expiry_years: float
) -> ExpectedMove:
    """One-standard-deviation move over the option's remaining life: IV x price x sqrt(t)."""
    pct = implied_vol * sqrt(time_to_expiry_years)
    dollar_amount = underlying_price * pct
    return ExpectedMove(
        dollar_amount=round(dollar_amount, 4),
        pct_of_price=round(pct * 100, 4),
        upper_bound=round(underlying_price + dollar_amount, 4),
        lower_bound=round(underlying_price - dollar_amount, 4),
    )


def translate_stock_level_to_contract_price(
    stock_level_price: float,
    current_underlying_price: float,
    current_contract_price: float,
    delta: float,
    gamma: float,
) -> float:
    """Second-order (delta + gamma) Taylor estimate of the contract's price if the
    underlying reaches `stock_level_price`. A translation, not a prediction — the further
    the level is from the current price, the less reliable this local approximation gets.
    """
    price_change = stock_level_price - current_underlying_price
    estimated_change = delta * price_change + 0.5 * gamma * price_change**2
    return round(max(0.0, current_contract_price + estimated_change), 4)


def daily_theta_decay_pct(theta: float, current_contract_price: float) -> float | None:
    """Today's theta as a percentage of the current contract price (0 if price is 0)."""
    if current_contract_price <= 0:
        return None
    return round(theta / current_contract_price * 100, 4)


def reprice_contract_at_stock_level(
    stock_level_price: float,
    strike: float,
    time_to_expiry_years: float,
    implied_vol: float,
    option_type: str,
    risk_free_rate: float,
    *,
    remaining_time_fraction: float = 0.5,
) -> float:
    """Black-Scholes reprice at a level, with explicit elapsed-time assumption.

    This replaces the local Delta/Gamma Taylor approximation for Snipe cards. The latter
    can reverse direction for large moves when gamma dominates, which is unacceptable
    for short-dated contracts.
    """
    from app.engines.options.greeks import theoretical_price

    minimum_years = 5 / (365 * 24 * 60)
    remaining_years = max(time_to_expiry_years * remaining_time_fraction, minimum_years)
    price = theoretical_price(
        stock_level_price,
        strike,
        remaining_years,
        implied_vol,
        option_type,
        risk_free_rate,
    )
    return round(max(0.0, price), 4)
