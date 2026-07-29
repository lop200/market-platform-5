"""Swappable market-data provider contract."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    bid: float | None
    ask: float | None
    volume: int | None
    as_of: str
    is_delayed: bool
    provider: str = "unknown"
    feed: str | None = None
    last_trade: float | None = None
    relative_volume: float | None = None
    session: str = "regular"

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid

    @property
    def spread_pct(self) -> float | None:
        if not self.mid:
            return None
        return (self.ask - self.bid) / self.mid * 100

    @property
    def age_seconds(self) -> int:
        parsed = datetime.fromisoformat(self.as_of.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


class MarketDataAdapter(ABC):
    @abstractmethod
    def get_daily_ohlcv(self, symbol: str, lookback_days: int) -> pd.DataFrame: ...

    @property
    def supports_batch_daily_ohlcv(self) -> bool:
        return False

    def get_daily_ohlcv_many(
        self, symbols: list[str], lookback_days: int
    ) -> dict[str, pd.DataFrame]:
        return {symbol: self.get_daily_ohlcv(symbol, lookback_days) for symbol in symbols}

    @property
    def supports_batch_quotes(self) -> bool:
        return False

    def get_quotes_many(self, symbols: list[str]) -> dict[str, Quote]:
        return {symbol: self.get_quote(symbol) for symbol in symbols}

    @abstractmethod
    def get_intraday(self, symbol: str, interval: str) -> pd.DataFrame | None: ...

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote: ...

    def list_active_us_symbols(self, limit: int = 1000) -> list[str]:
        return []

    def get_company_profile(self, symbol: str) -> dict:
        return {}

    def telemetry_snapshot(self) -> dict[str, int]:
        return {"api_requests": 0, "cache_hits": 0, "requested_symbols": 0}

    @abstractmethod
    def estimated_cost_per_call(self) -> float: ...

    @abstractmethod
    def is_market_open(self) -> bool: ...

    @property
    @abstractmethod
    def provider_name(self) -> str: ...
