"""MarketDataAdapter — abstract contract any data provider must implement (SRS 5.3, 9.1).

No code outside this module (and the orchestrator) should import a concrete provider
directly; everything goes through this interface so providers are swappable (NFR-5).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    bid: float | None
    ask: float | None
    volume: int | None
    as_of: str  # ISO timestamp
    is_delayed: bool


@dataclass(frozen=True)
class OptionsChain:
    """Placeholder shape for M2/M5 — options adapters are out of scope for M0."""

    symbol: str
    expiry: str
    contracts: list[dict]


class MarketDataAdapter(ABC):
    """SRS 5.3 abstract data-provider contract."""

    @abstractmethod
    def get_daily_ohlcv(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        """Return a DataFrame indexed by date with columns: open, high, low, close, volume."""

    @property
    def supports_batch_daily_ohlcv(self) -> bool:
        """Whether this adapter can fetch several symbols without one request per symbol."""
        return False

    def get_daily_ohlcv_many(
        self, symbols: list[str], lookback_days: int
    ) -> dict[str, pd.DataFrame]:
        """Optional batch path. Callers must check ``supports_batch_daily_ohlcv`` first."""
        raise NotImplementedError("batch daily OHLCV is not supported by this provider")

    @abstractmethod
    def get_intraday(self, symbol: str, interval: str) -> pd.DataFrame | None:
        """Return intraday bars, or None if the provider/tier does not support them."""

    @abstractmethod
    def get_quote(self, symbol: str) -> Quote:
        ...

    def get_options_chain(self, symbol: str, expiry: str) -> OptionsChain:
        """Phase M2/M5. Not required in M0; default raises so callers fail loudly."""
        raise NotImplementedError("Options chain support lands in M2/M5")

    @abstractmethod
    def estimated_cost_per_call(self) -> float:
        """Marginal USD cost of one call, for the Cost Gate (SRS 16)."""

    @abstractmethod
    def is_market_open(self) -> bool:
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        ...
