"""Selects the configured MarketDataAdapter — the only place that knows concrete providers.

Switching providers is a single env var (MARKET_DATA_PROVIDER), never a code change (NFR-5).
"""
from __future__ import annotations

from functools import lru_cache

from app.config import get_settings
from app.providers.base import MarketDataAdapter


@lru_cache
def get_market_data_provider() -> MarketDataAdapter:
    settings = get_settings()
    name = settings.market_data_provider.lower()

    if name == "yfinance":
        from app.providers.yfinance_provider import YFinanceProvider

        return YFinanceProvider()

    if name == "alpaca":
        from app.providers.alpaca_provider import AlpacaProvider

        return AlpacaProvider(settings.alpaca_api_key, settings.alpaca_api_secret)

    if name == "finnhub":
        from app.providers.finnhub_provider import FinnhubProvider

        return FinnhubProvider(settings.finnhub_api_key)

    raise ValueError(f"unknown MARKET_DATA_PROVIDER '{settings.market_data_provider}'")
