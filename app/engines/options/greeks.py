"""Black-Scholes Greeks via py_vollib (SRS 11.1), used by M2-B and M5.

M2-B has no live options-chain subscription (that's evaluated in M5, CLAUDE.md). Instead
the contract's own observed price (read off the uploaded screenshot) is inverted into an
implied volatility via Black-Scholes, which is then fed back into the same model to get
Delta/Gamma/Theta/Vega. This needs only the underlying's live price (already available via
the M0/M1 market-data adapter) — no paid options data required.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from py_vollib.black_scholes import black_scholes
from py_vollib.black_scholes.greeks.analytical import delta, gamma, theta, vega
from py_vollib.black_scholes.implied_volatility import implied_volatility
from py_lets_be_rational.exceptions import AboveMaximumException, BelowIntrinsicException

OptionType = Literal["call", "put"]

_FLAG_MAP: dict[OptionType, str] = {"call": "c", "put": "p"}


@dataclass(frozen=True)
class Greeks:
    delta: float
    gamma: float
    theta: float  # dollars of value lost per calendar day (py_vollib already divides by 365)
    vega: float  # dollars of value change per 1 percentage point of IV (py_vollib already x0.01


_US_MARKET_TZ = ZoneInfo("America/New_York")
_US_MARKET_CLOSE = time(16, 0)
# Numerical floor for a 0DTE contract minutes from the bell — Black-Scholes Greeks blow
# up as t -> 0, and a 30-minute floor keeps them finite without materially changing them.
_MIN_YEARS_0DTE = 0.5 / (24 * 365)


def years_to_expiry(expiry: date, as_of: date | None = None) -> float:
    as_of = as_of or datetime.now().date()
    days = (expiry - as_of).days
    if days > 0:
        return days / 365.0
    if days == 0:
        # 0DTE: the contract is alive until the 16:00 America/New_York close of expiry
        # day — use the actual remaining fraction of the year, not an error (the
        # watchlist and snipe modules both legitimately track same-day contracts).
        close_utc = datetime.combine(expiry, _US_MARKET_CLOSE, tzinfo=_US_MARKET_TZ).astimezone(timezone.utc)
        remaining_years = (close_utc - datetime.now(timezone.utc)).total_seconds() / (365.0 * 24 * 3600)
        if remaining_years > 0:
            return max(remaining_years, _MIN_YEARS_0DTE)
    raise ValueError(f"expiry {expiry} is not in the future relative to {as_of}")


def solve_implied_volatility(
    contract_price: float,
    underlying_price: float,
    strike: float,
    time_to_expiry_years: float,
    option_type: OptionType,
    risk_free_rate: float,
) -> float:
    flag = _FLAG_MAP[option_type]
    try:
        return implied_volatility(
            contract_price, underlying_price, strike, time_to_expiry_years, risk_free_rate, flag
        )
    except BelowIntrinsicException as exc:
        raise ValueError(
            f"contract price {contract_price} is below intrinsic value — check the extracted "
            "strike/price from the screenshot"
        ) from exc
    except AboveMaximumException as exc:
        raise ValueError(f"contract price {contract_price} is above the theoretical maximum") from exc


def compute_greeks(
    underlying_price: float,
    strike: float,
    time_to_expiry_years: float,
    implied_vol: float,
    option_type: OptionType,
    risk_free_rate: float,
) -> Greeks:
    flag = _FLAG_MAP[option_type]
    args = (flag, underlying_price, strike, time_to_expiry_years, risk_free_rate, implied_vol)
    return Greeks(
        delta=delta(*args),
        gamma=gamma(*args),
        theta=theta(*args),
        vega=vega(*args),
    )


def theoretical_price(
    underlying_price: float,
    strike: float,
    time_to_expiry_years: float,
    implied_vol: float,
    option_type: OptionType,
    risk_free_rate: float,
) -> float:
    flag = _FLAG_MAP[option_type]
    return black_scholes(flag, underlying_price, strike, time_to_expiry_years, risk_free_rate, implied_vol)
