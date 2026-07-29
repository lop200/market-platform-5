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
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestBarRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
    StockSnapshotRequest,
)
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
        quotes = self._data_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self._feed)
        )
        trades = self._data_client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self._feed)
        )
        bars = self._data_client.get_stock_latest_bar(
            StockLatestBarRequest(symbol_or_symbols=symbol, feed=self._feed)
        )
        snapshots = self._data_client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=symbol, feed=self._feed)
        )
        return self._merge_realtime(symbol, quotes.get(symbol), trades.get(symbol), bars.get(symbol), snapshots.get(symbol))

    @staticmethod
    def _newer(primary, fallback):
        if primary is None:
            return fallback
        if fallback is None:
            return primary
        return primary if primary.timestamp >= fallback.timestamp else fallback

    @staticmethod
    def _session(timestamp: datetime) -> str:
        from zoneinfo import ZoneInfo

        eastern = timestamp.astimezone(ZoneInfo("America/New_York"))
        minute = eastern.hour * 60 + eastern.minute
        if 240 <= minute < 570:
            return "pre_market"
        if 570 <= minute < 960:
            return "regular"
        if 960 <= minute < 1200:
            return "after_hours"
        return "closed"

    def _merge_realtime(self, symbol, direct_quote, direct_trade, direct_bar, snapshot) -> Quote:
        q = self._newer(direct_quote, getattr(snapshot, "latest_quote", None))
        trade = self._newer(direct_trade, getattr(snapshot, "latest_trade", None))
        bar = self._newer(direct_bar, getattr(snapshot, "minute_bar", None))
        candidates = []
        if trade is not None:
            candidates.append((trade.timestamp, float(trade.price), "latest_trade"))
        if q is not None and q.bid_price and q.ask_price:
            candidates.append((q.timestamp, (float(q.bid_price) + float(q.ask_price)) / 2, "latest_quote_mid"))
        if bar is not None:
            candidates.append((bar.timestamp, float(bar.close), "latest_minute_bar"))
        if not candidates:
            raise ValueError(f"no realtime snapshot data returned for symbol '{symbol}'")
        newest_timestamp, price, source = max(candidates, key=lambda item: item[0])
        return Quote(
            symbol=symbol, price=price,
            bid=float(q.bid_price) if q is not None and q.bid_price else None,
            ask=float(q.ask_price) if q is not None and q.ask_price else None,
            volume=int(bar.volume) if bar is not None and bar.volume is not None else None,
            as_of=newest_timestamp.astimezone(timezone.utc).isoformat(),
            is_delayed=False, provider=self.provider_name, feed=self._feed,
            last_trade=float(trade.price) if trade is not None else None,
            trade_as_of=trade.timestamp.astimezone(timezone.utc).isoformat() if trade is not None else None,
            bid_as_of=q.timestamp.astimezone(timezone.utc).isoformat() if q is not None else None,
            ask_as_of=q.timestamp.astimezone(timezone.utc).isoformat() if q is not None else None,
            bar_as_of=bar.timestamp.astimezone(timezone.utc).isoformat() if bar is not None else None,
            bar_close=float(bar.close) if bar is not None else None,
            price_source=source,
            snapshot_as_of=newest_timestamp.astimezone(timezone.utc).isoformat(),
            # Session describes the current New York market phase. The source
            # timestamps remain separate so stale after-hours data can never
            # masquerade as a current pre-market quote.
            session=self._session(datetime.now(timezone.utc)),
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
        bars = self._data_client.get_stock_latest_bar(
            StockLatestBarRequest(symbol_or_symbols=unique, feed=self._feed)
        )
        snapshots = self._data_client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=unique, feed=self._feed)
        )
        results: dict[str, Quote] = {}
        for symbol in unique:
            try:
                results[symbol] = self._merge_realtime(
                    symbol,
                    quotes.get(symbol),
                    trades.get(symbol),
                    bars.get(symbol),
                    snapshots.get(symbol),
                )
            except ValueError:
                continue
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
