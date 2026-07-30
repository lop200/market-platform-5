from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx

from app.options.schemas import OptionType, RawOptionContract

OCC_PATTERN = re.compile(r"^(?P<root>[A-Z]{1,6})(?P<expiry>\d{6})(?P<side>[CP])(?P<strike>\d{8})$")


class OptionDataProvider(Protocol):
    provider_name: str
    feed: str

    def get_option_chain(
        self, symbol: str, underlying_price: float | None = None
    ) -> list[RawOptionContract]: ...


def parse_occ_symbol(contract_symbol: str) -> tuple[str, date, OptionType, float]:
    match = OCC_PATTERN.fullmatch(contract_symbol)
    if not match:
        raise ValueError("invalid OCC option symbol")
    expiry = datetime.strptime(match.group("expiry"), "%y%m%d").date()
    option_type = OptionType.CALL if match.group("side") == "C" else OptionType.PUT
    return match.group("root"), expiry, option_type, int(match.group("strike")) / 1000


class AlpacaOptionProvider:
    provider_name = "alpaca"

    def __init__(
        self,
        api_key: str | None,
        api_secret: str | None,
        *,
        feed: str = "opra",
        base_url: str = "https://data.alpaca.markets",
        timeout_seconds: float = 12,
        min_dte: int = 7,
        max_dte: int = 30,
        strike_window_pct: float = 8.0,
    ):
        if not api_key or not api_secret:
            raise ValueError("Alpaca credentials are required for OPRA options data")
        self.api_key = api_key
        self.api_secret = api_secret
        self.feed = feed.lower()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.min_dte = min_dte
        self.max_dte = max_dte
        self.strike_window_pct = strike_window_pct

    def get_option_chain(
        self, symbol: str, underlying_price: float | None = None
    ) -> list[RawOptionContract]:
        url = f"{self.base_url}/v1beta1/options/snapshots/{symbol.upper()}"
        market_date = datetime.now(ZoneInfo("America/New_York")).date()
        params: dict[str, str | int | float] = {
            "feed": self.feed,
            "limit": 1000,
            "expiration_date_gte": (
                market_date + timedelta(days=self.min_dte)
            ).isoformat(),
            "expiration_date_lte": (
                market_date + timedelta(days=self.max_dte)
            ).isoformat(),
        }
        if underlying_price and underlying_price > 0:
            band = underlying_price * self.strike_window_pct / 100
            params["strike_price_gte"] = round(max(0.01, underlying_price - band), 2)
            params["strike_price_lte"] = round(underlying_price + band, 2)
        with httpx.Client(timeout=self.timeout_seconds) as client:
            response = client.get(
                url,
                params=params,
                headers={
                    "APCA-API-KEY-ID": self.api_key,
                    "APCA-API-SECRET-KEY": self.api_secret,
                },
            )
            response.raise_for_status()
        snapshots = response.json().get("snapshots") or {}
        contracts: list[RawOptionContract] = []
        for contract_symbol, snapshot in snapshots.items():
            try:
                root, expiry, option_type, strike = parse_occ_symbol(contract_symbol)
                quote = snapshot.get("latestQuote") or {}
                trade = snapshot.get("latestTrade") or {}
                daily = snapshot.get("dailyBar") or {}
                greeks = snapshot.get("greeks") or {}
                quote_timestamp = quote.get("t")
                trade_timestamp = trade.get("t")
                parse_time = lambda value: (
                    datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
                )
                contracts.append(RawOptionContract(
                    symbol=contract_symbol,
                    underlying_symbol=root,
                    option_type=option_type,
                    strike=strike,
                    expiration=expiry,
                    bid=quote.get("bp"),
                    ask=quote.get("ap"),
                    last=trade.get("p"),
                    volume=daily.get("v"),
                    open_interest=snapshot.get("openInterest"),
                    delta=greeks.get("delta"),
                    gamma=greeks.get("gamma"),
                    theta=greeks.get("theta"),
                    vega=greeks.get("vega"),
                    iv=snapshot.get("impliedVolatility"),
                    quote_timestamp=parse_time(quote_timestamp),
                    trade_timestamp=parse_time(trade_timestamp),
                    feed=self.feed,
                ))
            except (TypeError, ValueError):
                continue
        return contracts
