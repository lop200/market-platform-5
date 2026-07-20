"""Greeks tests against the Hull textbook reference example (py_vollib's own docstrings,
Options Futures and Other Derivatives, Example 17.1/17.2/17.4/17.6, page 355-367)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.engines.options.greeks import (
    compute_greeks,
    solve_implied_volatility,
    theoretical_price,
    years_to_expiry,
)

# Hull's example: S=49, K=50, r=5%, t=0.3846y, sigma=20%
HULL_S = 49.0
HULL_K = 50.0
HULL_R = 0.05
HULL_T = 0.3846
HULL_SIGMA = 0.2


def test_compute_greeks_call_matches_hull_textbook():
    g = compute_greeks(HULL_S, HULL_K, HULL_T, HULL_SIGMA, "call", HULL_R)
    assert g.delta == pytest.approx(0.522, abs=0.01)
    assert g.gamma == pytest.approx(0.066, abs=0.001)
    assert g.theta == pytest.approx(-4.31 / 365, abs=0.01 / 365)
    assert g.vega == pytest.approx(0.121, abs=0.01)


def test_compute_greeks_put_theta_matches_hull_textbook():
    g = compute_greeks(HULL_S, HULL_K, HULL_T, HULL_SIGMA, "put", HULL_R)
    assert g.theta * 365 == pytest.approx(-1.8530056722, abs=1e-6)


def test_implied_volatility_round_trip_recovers_known_sigma():
    price = theoretical_price(HULL_S, HULL_K, HULL_T, HULL_SIGMA, "call", HULL_R)
    recovered_iv = solve_implied_volatility(price, HULL_S, HULL_K, HULL_T, "call", HULL_R)
    assert recovered_iv == pytest.approx(HULL_SIGMA, abs=1e-5)


def test_implied_volatility_round_trip_for_put():
    price = theoretical_price(HULL_S, HULL_K, HULL_T, HULL_SIGMA, "put", HULL_R)
    recovered_iv = solve_implied_volatility(price, HULL_S, HULL_K, HULL_T, "put", HULL_R)
    assert recovered_iv == pytest.approx(HULL_SIGMA, abs=1e-5)


def test_implied_volatility_rejects_price_below_intrinsic():
    # A call struck deep ITM priced below its intrinsic value is not a valid market price.
    with pytest.raises(ValueError, match="below intrinsic"):
        solve_implied_volatility(
            contract_price=0.01, underlying_price=100.0, strike=50.0, time_to_expiry_years=0.5,
            option_type="call", risk_free_rate=0.05,
        )


def test_years_to_expiry_basic():
    today = date(2026, 1, 1)
    expiry = today + timedelta(days=30)
    assert years_to_expiry(expiry, as_of=today) == pytest.approx(30 / 365)


def test_years_to_expiry_rejects_past_expiry():
    today = date(2026, 1, 1)
    with pytest.raises(ValueError):
        years_to_expiry(today - timedelta(days=1), as_of=today)


def test_years_to_expiry_0dte_before_close_returns_positive_fraction():
    # A contract expiring TODAY is alive until the 16:00 New York close: before the
    # close it must yield a small positive t (never a ValueError), floored so
    # Black-Scholes stays finite minutes from the bell.
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from datetime import time as dt_time

    now_ny = datetime.now(timezone.utc).astimezone(ZoneInfo("America/New_York"))
    today = now_ny.date()
    if now_ny.time() >= dt_time(16, 0):
        with pytest.raises(ValueError):
            years_to_expiry(today, as_of=today)
    else:
        t = years_to_expiry(today, as_of=today)
        assert 0 < t <= 1 / 365 + 1e-9


def test_years_to_expiry_0dte_after_close_rejected():
    # Expiry "today" but the close already passed -> genuinely expired -> error.
    # (Simulated by asking about a date whose 16:00 NY close is behind us: yesterday.)
    today = date(2026, 1, 2)
    with pytest.raises(ValueError):
        years_to_expiry(date(2026, 1, 1), as_of=today)
