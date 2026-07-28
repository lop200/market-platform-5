"""Deterministic 0-2 DTE option-contract ranking for Today's Snipe.

The ranker evaluates calls or puts only after the underlying direction is known. Hard
gates (DTE, premium, spread, open interest, delta, and theta) run before scoring so the
UI never has to hide an invalid contract after it has already been selected.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import pandas as pd

from app.engines.options.greeks import Greeks, compute_greeks, solve_implied_volatility, years_to_expiry

OptionType = Literal["call", "put"]

TARGET_DTE_MIN = 0
TARGET_DTE_MAX = 2
STRIKE_BAND_PCT = 0.15
MAX_CONTRACTS_EVALUATED = 80

LIQUIDITY_OI_SATURATION = 500
LIQUIDITY_VOLUME_SATURATION = 200
SPREAD_PCT_SATURATION = 0.15
IV_RANK_PROXY_SATURATION = 1.5
MAX_PLAUSIBLE_IV = 3.0

QUALITY_WEIGHT_LIQUIDITY = 35
QUALITY_WEIGHT_SPREAD = 25
QUALITY_WEIGHT_DELTA_FIT = 20
QUALITY_WEIGHT_IV_RANK = 20


@dataclass(frozen=True)
class RankedContract:
    contract_symbol: str
    option_type: OptionType
    strike: float
    expiry: date
    contract_price: float
    bid: float | None
    ask: float | None
    open_interest: int
    volume: int
    implied_volatility: float
    quality_score: float
    greeks: Greeks
    reasons: list[str]


def _select_expiries(
    expirations: list[str],
    as_of: date,
    min_dte: int = TARGET_DTE_MIN,
    max_dte: int = TARGET_DTE_MAX,
) -> list[str]:
    """Return eligible expiries only; never substitute a longer-dated contract."""
    selected: list[tuple[int, str]] = []
    for expiry_str in expirations:
        expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        dte = (expiry - as_of).days
        if min_dte <= dte <= max_dte:
            selected.append((dte, expiry_str))
    selected.sort(key=lambda item: item[0])
    return [expiry for _, expiry in selected]


def _safe_number(value, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return default if value != value else value


def _liquidity_component(open_interest: float | None, volume: float | None) -> float:
    oi = _safe_number(open_interest)
    vol = _safe_number(volume)
    return min(
        0.7 * min(oi / LIQUIDITY_OI_SATURATION, 1.0)
        + 0.3 * min(vol / LIQUIDITY_VOLUME_SATURATION, 1.0),
        1.0,
    )


def _spread_metrics(bid: float | None, ask: float | None) -> tuple[float, float, float] | None:
    bid_value = _safe_number(bid, default=-1)
    ask_value = _safe_number(ask, default=-1)
    if bid_value <= 0 or ask_value <= 0 or ask_value < bid_value:
        return None
    mid = (bid_value + ask_value) / 2
    spread_pct = (ask_value - bid_value) / mid
    component = max(0.0, 1 - spread_pct / SPREAD_PCT_SATURATION)
    return mid, spread_pct, component


def _delta_fit_component(delta_value: float) -> float:
    return max(0.0, 1 - abs(abs(delta_value) - 0.5) / 0.5)


def _iv_rank_proxy(atr_pct_relative: float) -> float:
    if atr_pct_relative != atr_pct_relative:
        return 0.5
    return max(0.0, min(atr_pct_relative / IV_RANK_PROXY_SATURATION, 1.0))


def _years_for_expiry(expiry: date, as_of: date) -> float:
    if expiry == as_of and as_of != datetime.now(timezone.utc).date():
        # Deterministic test/backtest fallback: half a calendar day remains.
        return 0.5 / 365.0
    return years_to_expiry(expiry, as_of)


def rank_best_contract(
    expirations: list[str],
    fetch_chain,
    underlying_price: float,
    atr_pct_relative: float,
    risk_free_rate: float,
    option_type: OptionType,
    as_of: date | None = None,
    *,
    min_dte: int = TARGET_DTE_MIN,
    max_dte: int = TARGET_DTE_MAX,
    max_premium: float = 1.0,
    max_spread_pct: float = SPREAD_PCT_SATURATION,
    min_open_interest: int = 100,
    min_abs_delta: float = 0.30,
    max_abs_delta: float = 0.65,
    max_theta_decay_pct: float = 40.0,
) -> RankedContract | None:
    """Rank all eligible expiries after applying every declared hard gate."""
    as_of = as_of or datetime.now(timezone.utc).date()
    selected_expiries = _select_expiries(expirations, as_of, min_dte, max_dte)
    best: RankedContract | None = None

    for expiry_str in selected_expiries:
        expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        try:
            time_to_expiry = _years_for_expiry(expiry, as_of)
        except ValueError:
            continue

        chain = fetch_chain(expiry_str)
        if chain is None or chain.empty:
            continue

        lower = underlying_price * (1 - STRIKE_BAND_PCT)
        upper = underlying_price * (1 + STRIKE_BAND_PCT)
        band = chain[(chain["strike"] >= lower) & (chain["strike"] <= upper)]
        if band.empty:
            band = chain
        band = band.head(MAX_CONTRACTS_EVALUATED)

        for _, row in band.iterrows():
            iv = _safe_number(row.get("impliedVolatility"), default=-1)
            if iv <= 0 or iv > MAX_PLAUSIBLE_IV:
                continue

            spread = _spread_metrics(row.get("bid"), row.get("ask"))
            if spread is None:
                continue
            mid, spread_pct, spread_component = spread
            if spread_pct > max_spread_pct:
                continue

            contract_price = round(mid, 2)
            if contract_price <= 0 or contract_price > max_premium:
                continue

            open_interest = int(_safe_number(row.get("openInterest")))
            if open_interest < min_open_interest:
                continue

            try:
                greeks = compute_greeks(
                    underlying_price,
                    float(row["strike"]),
                    time_to_expiry,
                    iv,
                    option_type,
                    risk_free_rate,
                )
            except Exception:
                continue

            abs_delta = abs(float(greeks.delta))
            if not min_abs_delta <= abs_delta <= max_abs_delta:
                continue
            theta_decay_pct = abs(float(greeks.theta)) / contract_price * 100
            if theta_decay_pct > max_theta_decay_pct:
                continue

            liquidity_component = _liquidity_component(open_interest, row.get("volume"))
            delta_fit = _delta_fit_component(float(greeks.delta))
            iv_rank = _iv_rank_proxy(atr_pct_relative)
            score = round(
                QUALITY_WEIGHT_LIQUIDITY * liquidity_component
                + QUALITY_WEIGHT_SPREAD * spread_component
                + QUALITY_WEIGHT_DELTA_FIT * delta_fit
                + QUALITY_WEIGHT_IV_RANK * iv_rank,
                1,
            )

            reasons = [
                f"انتهاء ضمن {max_dte} يوم",
                f"العلاوة ضمن {max_premium:.2f}$",
                f"السبريد {spread_pct * 100:.1f}%",
            ]
            if open_interest >= LIQUIDITY_OI_SATURATION:
                reasons.append("فائدة مفتوحة مرتفعة")
            if delta_fit >= 0.8:
                reasons.append("دلتا متوازنة قرب 0.5")

            candidate = RankedContract(
                contract_symbol=str(row.get("contractSymbol", "")),
                option_type=option_type,
                strike=float(row["strike"]),
                expiry=expiry,
                contract_price=contract_price,
                bid=float(row["bid"]),
                ask=float(row["ask"]),
                open_interest=open_interest,
                volume=int(_safe_number(row.get("volume"))),
                implied_volatility=iv,
                quality_score=score,
                greeks=greeks,
                reasons=reasons,
            )
            if best is None or candidate.quality_score > best.quality_score:
                best = candidate

    return best


def rank_best_call_contract(
    expirations: list[str],
    fetch_chain_calls,
    underlying_price: float,
    atr_pct_relative: float,
    risk_free_rate: float,
    as_of: date | None = None,
    **kwargs,
) -> RankedContract | None:
    """Backward-compatible wrapper; the Snipe flow uses ``rank_best_contract``."""
    return rank_best_contract(
        expirations,
        fetch_chain_calls,
        underlying_price,
        atr_pct_relative,
        risk_free_rate,
        "call",
        as_of,
        **kwargs,
    )


def fetch_yfinance_chain(symbol: str, expiry_str: str, option_type: OptionType):
    import yfinance as yf

    chain = yf.Ticker(symbol).option_chain(expiry_str)
    return chain.calls if option_type == "call" else chain.puts


def fetch_yfinance_chain_calls(symbol: str, expiry_str: str):
    return fetch_yfinance_chain(symbol, expiry_str, "call")


def get_yfinance_expirations(symbol: str) -> list[str]:
    import yfinance as yf

    return list(yf.Ticker(symbol).options)


def _alpaca_trading_client():
    from alpaca.trading.client import TradingClient

    from app.config import get_settings

    settings = get_settings()
    return TradingClient(settings.alpaca_api_key, settings.alpaca_api_secret, paper=True)


def get_alpaca_expirations(
    symbol: str,
    option_type: OptionType = "call",
    as_of: date | None = None,
    max_dte: int = TARGET_DTE_MAX,
) -> list[str]:
    """Request only the owner-approved expiry window, avoiding irrelevant pagination."""
    from alpaca.trading.enums import ContractType
    from alpaca.trading.requests import GetOptionContractsRequest

    as_of = as_of or datetime.now(timezone.utc).date()
    request = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        type=ContractType.CALL if option_type == "call" else ContractType.PUT,
        expiration_date_gte=as_of,
        expiration_date_lte=as_of + timedelta(days=max_dte),
    )
    contracts = _alpaca_trading_client().get_option_contracts(request).option_contracts
    return sorted({contract.expiration_date.isoformat() for contract in contracts})


def fetch_alpaca_chain(
    symbol: str,
    expiry_str: str,
    underlying_price: float,
    risk_free_rate: float,
    option_type: OptionType,
    as_of: date | None = None,
):
    from alpaca.trading.enums import ContractType
    from alpaca.trading.requests import GetOptionContractsRequest

    request = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        type=ContractType.CALL if option_type == "call" else ContractType.PUT,
        expiration_date=expiry_str,
    )
    contracts = _alpaca_trading_client().get_option_contracts(request).option_contracts
    if not contracts:
        return None

    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionSnapshotRequest
    from app.config import get_settings

    settings = get_settings()
    data_client = OptionHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_api_secret)
    snapshots = data_client.get_option_snapshot(
        OptionSnapshotRequest(symbol_or_symbols=[contract.symbol for contract in contracts])
    )

    as_of = as_of or datetime.now(timezone.utc).date()
    expiry = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    time_to_expiry = _years_for_expiry(expiry, as_of)
    rows = []
    for contract in contracts:
        snapshot = snapshots.get(contract.symbol)
        bid = snapshot.latest_quote.bid_price if snapshot and snapshot.latest_quote else None
        ask = snapshot.latest_quote.ask_price if snapshot and snapshot.latest_quote else None
        last_price = snapshot.latest_trade.price if snapshot and snapshot.latest_trade else None
        mid = (bid + ask) / 2 if bid and ask else last_price
        implied_vol = None
        if mid and mid > 0:
            try:
                implied_vol = solve_implied_volatility(
                    mid,
                    underlying_price,
                    float(contract.strike_price),
                    time_to_expiry,
                    option_type,
                    risk_free_rate,
                )
            except ValueError:
                implied_vol = None
        rows.append(
            {
                "strike": float(contract.strike_price),
                "impliedVolatility": implied_vol,
                "bid": bid,
                "ask": ask,
                "openInterest": int(contract.open_interest) if contract.open_interest else 0,
                "volume": 0,
                "lastPrice": last_price,
                "contractSymbol": contract.symbol,
            }
        )
    return pd.DataFrame(rows)


def fetch_alpaca_chain_calls(
    symbol: str,
    expiry_str: str,
    underlying_price: float,
    risk_free_rate: float,
    as_of: date | None = None,
):
    return fetch_alpaca_chain(
        symbol,
        expiry_str,
        underlying_price,
        risk_free_rate,
        "call",
        as_of=as_of,
    )
