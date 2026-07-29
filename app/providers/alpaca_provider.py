"""Alpaca Market Data adapter — primary production provider.

Fully implemented but inert until ALPACA_API_KEY / ALPACA_API_SECRET are set in .env.
Uses the free IEX real-time feed tier. Raises a clear error if credentials are missing
rather than silently falling back — provider selection is explicit via
MARKET_DATA_PROVIDER (SRS NFR-5), never an implicit downgrade.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest

from app.config import get_settings
from app.providers.base import MarketDataAdapter, Quote


class AlpacaProvider(MarketDataAdapter):
    provider_name_value = "alpaca"

    def __init__(self, api_key: str | None, api_secret: str | None):
        if not api_key or not api_secret:
            raise ValueError(
                "Alpaca provider selected but ALPACA_API_KEY/ALPACA_API_SECRET are not set. "
                "Add them to .env, or set MARKET_DATA_PROVIDER=yfinance for local dev."
            )
        self._data_client = StockHistoricalDataClient(api_key, api_secret)
        self._trading_client = TradingClient(api_key, api_secret, paper=True)
        self._feed = get_settings().alpaca_feed

    def get_daily_ohlcv(self, symbol: str, lookback_days: int) -> pd.DataFrame:
        start = datetime.now(timezone.utc) - timedelta(days=int(lookback_days * 1.6) + 5)
        request = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=TimeFrame.Day, start=start, feed=self._feed
        )
        bars = self._data_client.get_stock_bars(request).df
        if bars.empty:
            raise ValueError(f"no daily OHLCV returned for symbol '{symbol}'")
        df = bars.reset_index(level=0, drop=True)[["open", "high", "low", "close", "volume"]]
        df.index.name = "date"
        return df.tail(lookback_days)

    @property
    def supports_batch_daily_ohlcv(self) -> bool:
        return True

    def get_daily_ohlcv_many(
        self, symbols: list[str], lookback_days: int
    ) -> dict[str, pd.DataFrame]:
        unique_symbols = list(dict.fromkeys(symbols))
        if not unique_symbols:
            return {}
        start = datetime.now(timezone.utc) - timedelta(days=int(lookback_days * 1.6) + 5)
        request = StockBarsRequest(
            symbol_or_symbols=unique_symbols,
            timeframe=TimeFrame.Day,
            start=start,
            feed=self._feed,
        )
        bars = self._data_client.get_stock_bars(request).df
        if bars.empty:
            return {}

        results: dict[str, pd.DataFrame] = {}
        if isinstance(bars.index, pd.MultiIndex):
            available = set(bars.index.get_level_values(0))
            for symbol in unique_symbols:
                if symbol not in available:
                    continue
                frame = bars.xs(symbol, level=0)[["open", "high", "low", "close", "volume"]]
                frame.index.name = "date"
                results[symbol] = frame.tail(lookback_days)
            return results

        if len(unique_symbols) == 1:
            frame = bars[["open", "high", "low", "close", "volume"]].copy()
            frame.index.name = "date"
            results[unique_symbols[0]] = frame.tail(lookback_days)
        return results

    def get_intraday(self, symbol: str, interval: str) -> pd.DataFrame | None:
        timeframe_map = {
            "1m": TimeFrame.Minute,
            "5m": TimeFrame(5, TimeFrame.Minute.unit),
            "15m": TimeFrame(15, TimeFrame.Minute.unit),
            "1h": TimeFrame.Hour,
        }
        timeframe = timeframe_map.get(interval)
        if timeframe is None:
            raise ValueError(f"unsupported intraday interval '{interval}'")
        start = datetime.now(timezone.utc) - timedelta(days=5)
        request = StockBarsRequest(
            symbol_or_symbols=symbol, timeframe=timeframe, start=start, feed=self._feed
        )
        bars = self._data_client.get_stock_bars(request).df
        if bars.empty:
            return None
        df = bars.reset_index(level=0, drop=True)[["open", "high", "low", "close", "volume"]]
        df.index.name = "datetime"
        return df

    def get_quote(self, symbol: str) -> Quote:
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self._feed)
        quotes = self._data_client.get_stock_latest_quote(request)
        q = quotes[symbol]
        trades = self._data_client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self._feed)
        )
        trade = trades.get(symbol)
        last = float(trade.price) if trade else None
        mid = (q.bid_price + q.ask_price) / 2 if q.bid_price and q.ask_price else q.ask_price or q.bid_price
        return Quote(
            symbol=symbol,
            price=float(last or mid),
            bid=float(q.bid_price) if q.bid_price else None,
            ask=float(q.ask_price) if q.ask_price else None,
            volume=None,
            as_of=q.timestamp.isoformat(),
            is_delayed=False,
            provider=self.provider_name,
            feed=self._feed,
            last_trade=last,
            trade_as_of=trade.timestamp.isoformat() if trade else None,
            bid_as_of=q.timestamp.isoformat(),
            ask_as_of=q.timestamp.isoformat(),
        )

    @property
    def supports_batch_quotes(self) -> bool:
        return True

    def get_quotes_many(self, symbols: list[str]) -> dict[str, Quote]:
        unique = list(dict.fromkeys(symbols))
        quotes = self._data_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=unique, feed=self._feed)
        )
        trades = self._data_client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=unique, feed=self._feed)
        )
        results: dict[str, Quote] = {}
        for symbol in unique:
            q = quotes.get(symbol)
            if q is None:
                continue
            trade = trades.get(symbol)
            last = float(trade.price) if trade else None
            mid = (q.bid_price + q.ask_price) / 2 if q.bid_price and q.ask_price else q.ask_price or q.bid_price
            if not last and not mid:
                continue
            results[symbol] = Quote(
                symbol=symbol,
                price=float(last or mid),
                bid=float(q.bid_price) if q.bid_price else None,
                ask=float(q.ask_price) if q.ask_price else None,
                volume=None,
                as_of=q.timestamp.isoformat(),
                is_delayed=False,
                provider=self.provider_name,
                feed=self._feed,
                last_trade=last,
                trade_as_of=trade.timestamp.isoformat() if trade else None,
                bid_as_of=q.timestamp.isoformat(),
                ask_as_of=q.timestamp.isoformat(),
            )
        return results

    def get_company_profile(self, symbol: str) -> dict:
        asset = self._trading_client.get_asset(symbol)
        return {"name": asset.name or symbol, "exchange": str(asset.exchange)}

    def list_active_us_symbols(self, limit: int = 1000) -> list[str]:
        request = GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
        assets = self._trading_client.get_all_assets(request)
        allowed = {"NASDAQ", "NYSE", "AMEX", "ARCA", "BATS"}
        return [
            asset.symbol
            for asset in assets
            if asset.tradable and str(asset.exchange).split(".")[-1].upper() in allowed
        ][:limit]

    def estimated_cost_per_call(self) -> float:
        # Free IEX tier: marginal cost per call is ~0 within plan limits (SRS 25.1).
        return 0.0

    def is_market_open(self) -> bool:
        clock = self._trading_client.get_clock()
        return bool(clock.is_open)

    @property
    def provider_name(self) -> str:
        return self.provider_name_value
