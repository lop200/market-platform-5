"""Contract Quality Score (SRS 11.2) — auto-picks the single best call contract for a
Snipe scanner card from a live options chain.

Primary data source (owner-confirmed 2026-07-18): Alpaca's options endpoints, using the
same paper-account keys already configured for stock data — no separate Tradier/Polygon
subscription needed. Alpaca's free "indicative" options feed (not the paid OPRA
real-time feed, which requires signing a separate market-data agreement) gives live
bid/ask/last-trade per contract via `OptionHistoricalDataClient`, and contract metadata
(strike/expiry/type/open interest) via `TradingClient.get_option_contracts`. It does not
expose implied volatility or per-contract daily volume, so IV is solved in-house from the
mid price via py_vollib (`solve_implied_volatility`, CLAUDE.md rule 1) and volume defaults
to 0 (open interest — the dominant, more stable term in the liquidity component below —
still carries full weight). Falls back to yfinance's free, no-key `option_chain()` only
when `MARKET_DATA_PROVIDER` is not `alpaca` (local dev without Alpaca keys) — same
"dev-tier" caveat as `providers/yfinance_provider.py`. No historical chain in either case,
so a true 252-session IV Rank (SRS 11.1) isn't computable — `iv_rank_proxy` below is a
declared, clearly-labeled approximation instead.

    ContractQualityScore = 35×liquidity + 25×(1 − spread_pct) + 20×delta_fit + 20×iv_rank_proxy

- liquidity: 0.7×open-interest saturation + 0.3×volume saturation (declared constants).
- spread_pct: (ask − bid) / mid, scaled so 0% -> 1.0 and >=15% -> 0.0.
- delta_fit: how close the contract's Delta is to 0.5 — a liquid, balanced middle ground
  between a cheap far-OTM lotto ticket and an expensive deep-ITM stock substitute. 1.0 at
  Delta=0.5, 0.0 at the edges.
- iv_rank_proxy: NOT a real percentile — approximated from the stock's own ATR% relative
  to its 90-day average (`Volatility.atr_pct_relative`, already computed by the
  deterministic engine), scaled so >=1.5x average maxes out.

No LLM anywhere in this module (CLAUDE.md rule 1) — pure arithmetic + py_vollib Greeks
(same library already used for the M2-B screenshot flow).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

import pandas as pd

from app.engines.options.greeks import Greeks, compute_greeks, solve_implied_volatility, years_to_expiry

TARGET_DTE_MIN = 14
TARGET_DTE_MAX = 45
TARGET_DTE_PREFERRED = 30
STRIKE_BAND_PCT = 0.15  # only evaluate strikes within +/-15% of the current underlying price
MAX_CONTRACTS_EVALUATED = 40

LIQUIDITY_OI_SATURATION = 500
LIQUIDITY_VOLUME_SATURATION = 200
SPREAD_PCT_SATURATION = 0.15
IV_RANK_PROXY_SATURATION = 1.5
MAX_PLAUSIBLE_IV = 3.0  # above this, treat the chain quote as stale/unreliable and skip it

QUALITY_WEIGHT_LIQUIDITY = 35
QUALITY_WEIGHT_SPREAD = 25
QUALITY_WEIGHT_DELTA_FIT = 20
QUALITY_WEIGHT_IV_RANK = 20


@dataclass(frozen=True)
class RankedContract:
    contract_symbol: str
    option_type: str
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


def _select_expiry(expirations: list[str], as_of: date) -> str | None:
    in_window: list[tuple[int, str]] = []
    all_future: list[tuple[int, str]] = []
    for exp_str in expirations:
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        dte = (exp_date - as_of).days
        if dte <= 0:
            continue
        all_future.append((dte, exp_str))
        if TARGET_DTE_MIN <= dte <= TARGET_DTE_MAX:
            in_window.append((dte, exp_str))
    if in_window:
        in_window.sort(key=lambda pair: abs(pair[0] - TARGET_DTE_PREFERRED))
        return in_window[0][1]
    if all_future:
        all_future.sort()
        return all_future[0][1]
    return None


def _safe_number(value, default: float = 0.0) -> float:
    """`yfinance` chain columns are pandas float64 and use NaN (not None) for missing
    data — NaN is truthy in Python, so a plain `value or default` silently lets it through."""
    if value is None:
        return default
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return default if value != value else value  # NaN != NaN


def _liquidity_component(open_interest: float | None, volume: float | None) -> float:
    oi = _safe_number(open_interest)
    vol = _safe_number(volume)
    return min(
        0.7 * min(oi / LIQUIDITY_OI_SATURATION, 1.0) + 0.3 * min(vol / LIQUIDITY_VOLUME_SATURATION, 1.0), 1.0
    )


def _spread_component(bid: float | None, ask: float | None) -> float | None:
    bid = _safe_number(bid, default=-1)
    ask = _safe_number(ask, default=-1)
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    spread_pct = (ask - bid) / mid
    return max(0.0, 1 - spread_pct / SPREAD_PCT_SATURATION)


def _delta_fit_component(delta_value: float) -> float:
    return max(0.0, 1 - abs(delta_value - 0.5) / 0.5)


def _iv_rank_proxy(atr_pct_relative: float) -> float:
    if atr_pct_relative != atr_pct_relative:  # NaN guard
        return 0.5
    return max(0.0, min(atr_pct_relative / IV_RANK_PROXY_SATURATION, 1.0))


def rank_best_call_contract(
    expirations: list[str],
    fetch_chain_calls,  # Callable[[str], pandas.DataFrame] — injected so this stays testable without network
    underlying_price: float,
    atr_pct_relative: float,
    risk_free_rate: float,
    as_of: date | None = None,
) -> RankedContract | None:
    """Pure ranking logic over an already-fetched chain — `fetch_chain_calls(expiry_str)`
    is the only network-touching call, kept as an injected callable so tests can fake it
    without a real yfinance round-trip."""
    as_of = as_of or datetime.now(timezone.utc).date()
    expiry_str = _select_expiry(expirations, as_of)
    if expiry_str is None:
        return None
    expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    time_to_expiry = years_to_expiry(expiry_date, as_of)

    calls = fetch_chain_calls(expiry_str)
    if calls is None or calls.empty:
        return None

    lower = underlying_price * (1 - STRIKE_BAND_PCT)
    upper = underlying_price * (1 + STRIKE_BAND_PCT)
    band = calls[(calls["strike"] >= lower) & (calls["strike"] <= upper)]
    if band.empty:
        band = calls
    band = band.head(MAX_CONTRACTS_EVALUATED)

    best: RankedContract | None = None
    for _, row in band.iterrows():
        iv = row.get("impliedVolatility")
        if iv is None or iv != iv or iv <= 0 or iv > MAX_PLAUSIBLE_IV:
            continue
        bid, ask = row.get("bid"), row.get("ask")
        spread_component = _spread_component(bid, ask)
        if spread_component is None:
            continue
        try:
            greeks = compute_greeks(underlying_price, float(row["strike"]), time_to_expiry, float(iv), "call", risk_free_rate)
        except Exception:
            continue

        liquidity_component = _liquidity_component(row.get("openInterest"), row.get("volume"))
        delta_fit = _delta_fit_component(float(greeks.delta))
        iv_rank = _iv_rank_proxy(atr_pct_relative)
        score = round(
            float(
                QUALITY_WEIGHT_LIQUIDITY * liquidity_component
                + QUALITY_WEIGHT_SPREAD * spread_component
                + QUALITY_WEIGHT_DELTA_FIT * delta_fit
                + QUALITY_WEIGHT_IV_RANK * iv_rank
            ),
            1,
        )

        reasons: list[str] = []
        if _safe_number(row.get("openInterest")) >= LIQUIDITY_OI_SATURATION:
            reasons.append("فائدة مفتوحة عالية")
        if spread_component >= 0.7:
            reasons.append("سبريد سعري ضيق نسبياً")
        if delta_fit >= 0.8:
            reasons.append("دلتا متوازنة قرب 0.5")

        contract_price = _safe_number(row.get("lastPrice"))
        if not contract_price and bid and ask:
            contract_price = (bid + ask) / 2
        contract_price = round(contract_price, 2)

        candidate = RankedContract(
            contract_symbol=str(row.get("contractSymbol", "")),
            option_type="call",
            strike=float(row["strike"]),
            expiry=expiry_date,
            contract_price=contract_price,
            bid=float(bid) if bid else None,
            ask=float(ask) if ask else None,
            open_interest=int(_safe_number(row.get("openInterest"))),
            volume=int(_safe_number(row.get("volume"))),
            implied_volatility=float(iv),
            quality_score=score,
            greeks=greeks,
            reasons=reasons or ["جودة عقد متوسطة ضمن النطاق المفحوص"],
        )
        if best is None or candidate.quality_score > best.quality_score:
            best = candidate

    return best


def fetch_yfinance_chain_calls(symbol: str, expiry_str: str):
    """The one real network call this module makes — isolated so `rank_best_call_contract`
    stays a pure function callers can unit-test without hitting yfinance."""
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    return ticker.option_chain(expiry_str).calls


def get_yfinance_expirations(symbol: str) -> list[str]:
    import yfinance as yf

    return list(yf.Ticker(symbol).options)


def _alpaca_trading_client():
    from alpaca.trading.client import TradingClient

    from app.config import get_settings

    settings = get_settings()
    return TradingClient(settings.alpaca_api_key, settings.alpaca_api_secret, paper=True)


def get_alpaca_expirations(symbol: str) -> list[str]:
    """Contract metadata via Alpaca's Trading API — same paper-account keys already used
    for stock data (owner-confirmed 2026-07-18), no extra subscription."""
    from alpaca.trading.enums import ContractType
    from alpaca.trading.requests import GetOptionContractsRequest

    client = _alpaca_trading_client()
    request = GetOptionContractsRequest(underlying_symbols=[symbol], type=ContractType.CALL)
    contracts = client.get_option_contracts(request).option_contracts
    return sorted({c.expiration_date.isoformat() for c in contracts})


def fetch_alpaca_chain_calls(
    symbol: str, expiry_str: str, underlying_price: float, risk_free_rate: float, as_of: date | None = None
):
    """The two real network calls this path makes — isolated (like `fetch_yfinance_chain_calls`)
    so `rank_best_call_contract` stays a pure function callers can unit-test without hitting
    Alpaca. Joins contract metadata (strike/open interest) with a live bid/ask/last-trade
    snapshot, then solves IV in-house since Alpaca's indicative feed doesn't supply it."""
    from alpaca.trading.enums import ContractType
    from alpaca.trading.requests import GetOptionContractsRequest

    client = _alpaca_trading_client()
    request = GetOptionContractsRequest(
        underlying_symbols=[symbol], type=ContractType.CALL, expiration_date=expiry_str
    )
    contracts = client.get_option_contracts(request).option_contracts
    if not contracts:
        return None

    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionSnapshotRequest

    from app.config import get_settings

    settings = get_settings()
    data_client = OptionHistoricalDataClient(settings.alpaca_api_key, settings.alpaca_api_secret)
    symbols = [c.symbol for c in contracts]
    snapshots = data_client.get_option_snapshot(OptionSnapshotRequest(symbol_or_symbols=symbols))

    as_of = as_of or datetime.now(timezone.utc).date()
    expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    time_to_expiry = years_to_expiry(expiry_date, as_of)

    rows = []
    for contract in contracts:
        snap = snapshots.get(contract.symbol)
        bid = snap.latest_quote.bid_price if snap and snap.latest_quote else None
        ask = snap.latest_quote.ask_price if snap and snap.latest_quote else None
        last_price = snap.latest_trade.price if snap and snap.latest_trade else None
        mid = (bid + ask) / 2 if bid and ask else last_price
        implied_vol = None
        if mid and mid > 0:
            try:
                implied_vol = solve_implied_volatility(
                    mid, underlying_price, float(contract.strike_price), time_to_expiry, "call", risk_free_rate
                )
            except ValueError:
                implied_vol = None
        rows.append(
            {
                "strike": float(contract.strike_price),
                "impliedVolatility": implied_vol,
                "bid": bid,
                "ask": ask,
                # Alpaca's indicative feed has no per-contract daily volume; open interest
                # (0.7 of the liquidity weight) still carries the signal.
                "openInterest": int(contract.open_interest) if contract.open_interest else 0,
                "volume": 0,
                "lastPrice": last_price,
                "contractSymbol": contract.symbol,
            }
        )
    return pd.DataFrame(rows)
