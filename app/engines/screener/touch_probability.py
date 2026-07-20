"""Statistical touch-probability for a price level — new module for the Snipe scanner
(not in SRS section 12, declared here per CLAUDE.md rule 2). Pure arithmetic, no LLM
(CLAUDE.md rule 1), no new dependency (standard-normal CDF via `math.erf`).

Model: reflection-principle first-passage probability for a driftless Brownian motion
touching a single barrier within a fixed horizon. Starting at the current price, with
daily volatility sigma derived from the already-computed 20-day annualized historical
volatility (`Volatility.hv_20d`, expressed as a percentage e.g. 35.0 = 35%):

    x = ln(level_price / current_price)
    sigma_horizon = (hv_20d_annualized / 100 / sqrt(252)) * sqrt(horizon_days)
    z = |x| / sigma_horizon
    p = clip(2 * (1 - Phi(z)), 0, 1)            # Phi = standard normal CDF

The horizon is fixed at 5 trading sessions by default so the probability shown on a
Snipe card is directly comparable to the self-audit's 5-session snipe check (SRS-adjacent,
audit/self_audit.py HORIZONS_BY_KIND) — the "computed probability" and the "did it
actually happen" measurement use the same window.
"""
from __future__ import annotations

import math

DEFAULT_HORIZON_DAYS = 5
TRADING_DAYS_PER_YEAR = 252


def _standard_normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def estimate_touch_probability(
    current_price: float,
    level_price: float,
    hv_20d_annualized_pct: float,
    horizon_days: int = DEFAULT_HORIZON_DAYS,
) -> float:
    """Probability the price touches `level_price` at least once within `horizon_days`
    trading sessions, under a driftless-random-walk assumption. Returns 0-1.

    `hv_20d_annualized_pct` is `Volatility.hv_20d` as-is (a percentage, e.g. 35.0 = 35%)."""
    if current_price <= 0 or level_price <= 0:
        return 0.0
    if level_price == current_price:
        return 1.0

    daily_sigma = max(hv_20d_annualized_pct, 0.0) / 100 / math.sqrt(TRADING_DAYS_PER_YEAR)
    sigma_horizon = daily_sigma * math.sqrt(max(horizon_days, 1))
    if sigma_horizon <= 0:
        return 0.0

    z = abs(math.log(level_price / current_price)) / sigma_horizon
    probability = 2 * (1 - _standard_normal_cdf(z))
    return max(0.0, min(1.0, probability))
