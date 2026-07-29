"""Finnhub adapter — alternative primary provider to Alpaca (owner decision 2026-07-17).

Free tier, REST-only (no official heavy SDK needed — plain httpx calls).
support. Raises a clear error if the API key is missing, same fail-loud contract as
AlpacaProvider — provider selection is explicit via MARKET_DATA_PROVIDER (SRS NFR-5).

Known constraint: Finnhub's free tier restricts /stock/candle (OHLCV history) for US equities
on many accounts — daily/intraday calls may 403 even with a valid key. Confirm with the owner
before relying on this provider for anything beyond quotes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd

from app.providers.base import MarketDataAdapter, Quote

_BASE_URL = "https://finnhub.io/api/v1"

_RESOLUTION_MAP = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60", "1h": "60"}


class FinnhubProvider(MarketDataAdapter):
    provider_name_value = "finnhub"

    def __init__(self, api_key: str | None):
        if not api_key:
            raise ValueError(
                "Finnhub provider selected but FINNHUB_API_KEY is not set. "
                "Add it to .env, or set MARKET_DATA_PROVIDER=yfinance for local dev."
            )
        self._api_key = api_key
        self._client = httpx.Client(base_url=_BASE_URL, timeout=10.0)

    def _get(self, path: str, params: dict) -> dict:
        response = self._client.get(path, params={**params, "token": self._api_key})
        response.raise_for_status()
        return response.json()

    def get_daily_ohlcv(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=int(lookback_days * 1.6) + 5)
        data = self._get(
            "/stock/candle",
            {
                "symbol": symbol,
                "resolution": "D",
                "from": int(start.timestamp()),
                "to": int(end.timestamp()),
            },
        )
        if data.get("s") != "ok" or not data.get("c"):
            raise ValueError(f"no daily OHLCV returned for symbol '{symbol}' (status={data.get('s')})")
        df = pd.DataFrame(
            {
                "open": data["o"],
                "high": data["h"],
                "low": data["l"],
                "close": data["c"],
                "volume": data["v"],
            },
            index=pd.to_datetime(data["t"], unit="s", utc=True).date,
        )
        df.index.name = "date"
        return df.tail(lookback_days)

    def get_intraday(self, symbol: str, interval: str) -> pd.DataFrame | None:
        resolution = _RESOLUTION_MAP.get(interval)
        if resolution is None:
            raise ValueError(f"unsupported intraday interval '{interval}'")
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=5)
        data = self._get(
            "/stock/candle",
            {
                "symbol": symbol,
                "resolution": resolution,
                "from": int(start.timestamp()),
                "to": int(end.timestamp()),
            },
        )
        if data.get("s") != "ok" or not data.get("c"):
            return None
        df = pd.DataFrame(
            {
                "open": data["o"],
                "high": data["h"],
                "low": data["l"],
                "close": data["c"],
                "volume": data["v"],
            },
            index=pd.to_datetime(data["t"], unit="s", utc=True),
        )
        df.index.name = "datetime"
        return df

    def get_quote(self, symbol: str) -> Quote:
        data = self._get("/quote", {"symbol": symbol})
        price = data.get("c")
        if not price:
            raise ValueError(f"no quote available for symbol '{symbol}'")
        return Quote(
            symbol=symbol,
            price=float(price),
            bid=None,
            ask=None,
            volume=None,
            as_of=datetime.fromtimestamp(data["t"], tz=timezone.utc).isoformat()
            if data.get("t")
            else datetime.now(timezone.utc).isoformat(),
            is_delayed=False,
            provider=self.provider_name,
            feed="finnhub",
            last_trade=float(price),
        )

    def estimated_cost_per_call(self) -> float:
        # Free tier: marginal cost per call is ~0 within the 60 calls/minute limit (SRS 25.1).
        return 0.0

    def is_market_open(self) -> bool:
        data = self._get("/stock/market-status", {"exchange": "US"})
        return bool(data.get("isOpen"))

    @property
    def provider_name(self) -> str:
        return self.provider_name_value
