"""Probabilities computed from price behaviour, never from an opinion.

Every number here is arithmetic over inputs the platform already measures —
volatility, strike, spot, time remaining. Nothing is asked of a language model,
because a percentage a model invents looks identical to one that is earned and
a reader cannot tell them apart.

Two different questions get two different answers, and they are not
interchangeable:

* ``touch_probability`` — will the price reach a level at any moment before the
  horizon ends? This is what a target or a stop cares about.
* ``finish_in_the_money`` — will the price be past a level when the contract
  expires? This is what an option settlement cares about, and it is always the
  smaller number, because touching is easier than staying.

Both assume a driftless random walk: no view on direction, only on how far the
price usually travels in the time available. That assumption is what makes them
honest rather than predictive — they answer "how far can it move", not "which
way will it go".
"""
from __future__ import annotations

import math

TRADING_DAYS_PER_YEAR = 252
CALENDAR_DAYS_PER_YEAR = 365
# A 0DTE contract can have minutes left. Five minutes is the numerical floor
# that keeps the square root meaningful rather than collapsing to zero.
MIN_HORIZON_DAYS = 5 / (6.5 * 60)


def standard_normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def touch_probability(
    current_price: float,
    level_price: float,
    annual_volatility_pct: float,
    horizon_days: float,
) -> float:
    """Chance the price touches ``level_price`` at least once before the horizon.

    Reflection principle for a driftless walk: the chance of ever crossing a
    barrier is twice the chance of ending beyond it.
    """
    if current_price <= 0 or level_price <= 0 or annual_volatility_pct <= 0:
        return 0.0
    if level_price == current_price:
        return 1.0
    daily_sigma = annual_volatility_pct / 100 / math.sqrt(TRADING_DAYS_PER_YEAR)
    sigma_horizon = daily_sigma * math.sqrt(max(horizon_days, MIN_HORIZON_DAYS))
    if sigma_horizon <= 0:
        return 0.0
    z = abs(math.log(level_price / current_price)) / sigma_horizon
    return max(0.0, min(1.0, 2 * (1 - standard_normal_cdf(z))))


def finish_in_the_money(
    spot: float,
    strike: float,
    implied_volatility: float,
    days_to_expiry: float,
    is_call: bool,
) -> float:
    """Chance the contract expires in the money, as the market prices it.

    This is N(d2) from Black-Scholes, not delta. Delta is widely used as a
    shortcut for this and always overstates it, because delta is N(d1) and d1
    exceeds d2 by one volatility unit of time. On a two-day contract the gap is
    small; on a monthly one it is not.

    Rates and dividends are omitted: over the days this platform trades they
    move the answer by far less than the spread already does.
    """
    if spot <= 0 or strike <= 0 or implied_volatility <= 0:
        return 0.0
    years = max(days_to_expiry, MIN_HORIZON_DAYS) / CALENDAR_DAYS_PER_YEAR
    sigma_horizon = implied_volatility * math.sqrt(years)
    if sigma_horizon <= 0:
        return 0.0
    d2 = (math.log(spot / strike) - 0.5 * implied_volatility**2 * years) / sigma_horizon
    probability = standard_normal_cdf(d2 if is_call else -d2)
    return max(0.0, min(1.0, probability))


def as_percent(probability: float) -> int:
    """Whole percent. Decimals on a modelled probability imply a precision the
    model does not have."""
    return int(round(max(0.0, min(1.0, probability)) * 100))
