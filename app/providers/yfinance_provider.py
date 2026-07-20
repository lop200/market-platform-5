"""yfinance adapter — DEV FALLBACK ONLY. Never used in production paths (CLAUDE.md rule).

Free, no API key required, but unofficial/unsupported — fine for local M0 smoke tests
and for building the deterministic engine (M1) without needing a paid subscription yet.
"""
from __future__ import annotations

from datetime import datetime, time, timezone

import pandas as pd
import yfinance as yf

from app.providers.base import MarketDataAdapter, Quote


class YFinanceProvider(MarketDataAdapter):
    provider_name_value = "yfinance"

    def get_daily_ohlcv(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        ticker = yf.Ticker(symbol)
        period_days = max(lookback_days + 5, 30)  # small buffer for weekends/holidays
        df = ticker.history(period=f"{period_days}d", interval="1d", auto_adjust=False)
        if df.empty:
            raise ValueError(f"no daily OHLCV returned for symbol '{symbol}'")
        df = df.rename(
            columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
        )[["open", "high", "low", "close", "volume"]]
        df.index.name = "date"
        return df.tail(lookback_days)

    def get_intraday(self, symbol: str, interval: str) -> pd.DataFrame | None:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period="5d", interval=interval, auto_adjust=False)
        if df.empty:
            return None
        df = df.rename(
            columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}
        )[["open", "high", "low", "close", "volume"]]
        df.index.name = "datetime"
        return df

    def get_quote(self, symbol: str) -> Quote:
        ticker = yf.Ticker(symbol)
        fast = ticker.fast_info
        price = fast.get("lastPrice") or fast.get("last_price")
        if price is None:
            raise ValueError(f"no quote available for symbol '{symbol}'")
        return Quote(
            symbol=symbol,
            price=float(price),
            bid=fast.get("bid"),
            ask=fast.get("ask"),
            volume=fast.get("lastVolume") or fast.get("last_volume"),
            as_of=datetime.now(timezone.utc).isoformat(),
            is_delayed=True,
        )

    def estimated_cost_per_call(self) -> float:
        return 0.0

    def is_market_open(self) -> bool:
        now_utc = datetime.now(timezone.utc)
        if now_utc.weekday() >= 5:
            return False
        # US regular session ~ 13:30-20:00 UTC (approximation, no holiday calendar in M0)
        return time(13, 30) <= now_utc.time() <= time(20, 0)

    @property
    def provider_name(self) -> str:
        return self.provider_name_value
