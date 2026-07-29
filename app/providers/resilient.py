from __future__ import annotations

import time
from dataclasses import replace
from threading import Lock

import pandas as pd

from app.config import Settings
from app.providers.base import MarketDataAdapter, Quote


class ProviderCircuitOpen(RuntimeError):
    pass


class ResilientMarketDataProvider(MarketDataAdapter):
    """Retry, circuit-breaker, and type-specific TTL cache around any provider."""

    def __init__(self, inner: MarketDataAdapter, settings: Settings):
        self.inner = inner
        self.settings = settings
        self._cache: dict[str, tuple[float, object]] = {}
        self._failures = 0
        self._opened_at = 0.0
        self._lock = Lock()
        self._api_requests = 0
        self._cache_hits = 0
        self._requested_symbols = 0

    @property
    def provider_name(self) -> str:
        return self.inner.provider_name

    @property
    def supports_batch_daily_ohlcv(self) -> bool:
        return self.inner.supports_batch_daily_ohlcv

    def _call(self, key: str, ttl: int, operation):
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            with self._lock:
                self._cache_hits += 1
            return cached[1]
        if self._failures >= self.settings.circuit_breaker_failures:
            if now - self._opened_at < self.settings.circuit_breaker_reset_seconds:
                if cached:
                    return self._stale(cached[1])
                raise ProviderCircuitOpen("market data circuit is open")
            self._failures = 0
        last_error = None
        for attempt in range(self.settings.external_max_retries + 1):
            try:
                with self._lock:
                    self._api_requests += 1
                value = operation()
                with self._lock:
                    self._cache[key] = (now + ttl, value)
                    self._failures = 0
                return value
            except Exception as exc:
                last_error = exc
                if attempt < self.settings.external_max_retries:
                    time.sleep(min(0.15 * (2**attempt), 0.5))
        with self._lock:
            self._failures += 1
            self._opened_at = time.monotonic()
        if cached:
            return self._stale(cached[1])
        raise last_error

    @staticmethod
    def _stale(value):
        return replace(value, is_delayed=True) if isinstance(value, Quote) else value

    def get_quote(self, symbol: str) -> Quote:
        with self._lock:
            self._requested_symbols += 1
        return self._call(
            f"quote:{symbol}", self.settings.cache_ttl_quote_seconds,
            lambda: self.inner.get_quote(symbol),
        )

    def get_daily_ohlcv(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        return self._call(
            f"daily:{symbol}:{lookback_days}", self.settings.cache_ttl_ohlcv_daily_seconds,
            lambda: self.inner.get_daily_ohlcv(symbol, lookback_days),
        )

    def get_daily_ohlcv_many(self, symbols: list[str], lookback_days: int) -> dict[str, pd.DataFrame]:
        if not self.inner.supports_batch_daily_ohlcv:
            return {symbol: self.get_daily_ohlcv(symbol, lookback_days) for symbol in symbols}
        with self._lock:
            self._requested_symbols += len(symbols)
        return self._call(
            f"daily-many:{','.join(symbols)}:{lookback_days}",
            self.settings.cache_ttl_ohlcv_daily_seconds,
            lambda: self.inner.get_daily_ohlcv_many(symbols, lookback_days),
        )

    @property
    def supports_batch_quotes(self) -> bool:
        return self.inner.supports_batch_quotes

    def get_quotes_many(self, symbols: list[str]) -> dict[str, Quote]:
        unique = list(dict.fromkeys(symbols))
        if not self.inner.supports_batch_quotes:
            return {symbol: self.get_quote(symbol) for symbol in unique}
        with self._lock:
            self._requested_symbols += len(unique)
        return self._call(
            f"quotes-many:{','.join(unique)}",
            self.settings.cache_ttl_quote_seconds,
            lambda: self.inner.get_quotes_many(unique),
        )

    def get_intraday(self, symbol: str, interval: str) -> pd.DataFrame | None:
        ttl = self.settings.intraday_cache_seconds * (3 if interval == "15m" else 1)
        return self._call(
            f"intraday:{symbol}:{interval}", ttl,
            lambda: self.inner.get_intraday(symbol, interval),
        )

    def list_active_us_symbols(self, limit: int = 1000) -> list[str]:
        return self._call("universe", 86_400, lambda: self.inner.list_active_us_symbols(limit))

    def get_company_profile(self, symbol: str) -> dict:
        return self._call(
            f"profile:{symbol}", 86_400,
            lambda: self.inner.get_company_profile(symbol),
        )

    def telemetry_snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "api_requests": self._api_requests,
                "cache_hits": self._cache_hits,
                "requested_symbols": self._requested_symbols,
            }

    def estimated_cost_per_call(self) -> float:
        return self.inner.estimated_cost_per_call()

    def is_market_open(self) -> bool:
        return self._call("market-open", 15, self.inner.is_market_open)
