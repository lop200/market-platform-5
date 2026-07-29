"""Alpaca Market Data adapter — primary production provider.

Fully implemented but inert until APCA_API_KEY_ID / APCA_API_SECRET_KEY are set.
Uses the free IEX real-time feed tier. Raises a clear error if credentials are missing
rather than silently falling back — provider selection is explicit via
MARKET_DATA_PROVIDER (SRS NFR-5), never an implicit downgrade.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
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

from app.providers.base import MarketDataAdapter, Quote

logger = logging.getLogger(__name__)


class AlpacaProvider(MarketDataAdapter):
    provider_name_value = "alpaca"

    def __init__(
        self,
        api_key: str | None,
        api_secret: str | None,
        *,
        data_base_url: str = "https://data.alpaca.markets",
        feed: str = "sip",
    ):
        if not api_key or not api_secret:
            raise ValueError(
                "Alpaca provider selected but APCA_API_KEY_ID/APCA_API_SECRET_KEY are not set. "
                "Add them to .env, or set MARKET_DATA_PROVIDER=yfinance for local dev."
            )
        self._api_key = api_key
        self._api_secret = api_secret
        self._data_base_url = data_base_url.rstrip("/")
        if self._data_base_url != "https://data.alpaca.markets":
            raise ValueError("ALPACA_DATA_BASE_URL must be https://data.alpaca.markets")
        self._data_client = StockHistoricalDataClient(
            api_key,
            api_secret,
            url_override=self._data_base_url,
        )
        self._trading_client = TradingClient(api_key, api_secret, paper=True)
        self._feed = feed.lower()

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
    def _parse_rfc3339(value: str | None) -> pd.Timestamp | None:
        if not value:
            return None
        parsed = pd.Timestamp(value)
        if parsed.tzinfo is None:
            parsed = parsed.tz_localize("UTC")
        return parsed.tz_convert("UTC")

    @staticmethod
    def _timestamp_age_seconds(value: pd.Timestamp | None, now: pd.Timestamp) -> int | None:
        if value is None:
            return None
        return max(0, int((now - value).total_seconds()))

    def debug_market_data(self, symbol: str, *, bypass_cache: bool = True) -> dict:
        symbol = symbol.upper()
        request_url = f"{self._data_base_url}/v2/stocks/{symbol}/snapshot"
        server_now = pd.Timestamp.now(tz="UTC")
        with httpx.Client(timeout=12.0) as client:
            response = client.get(
                request_url,
                params={"feed": self._feed},
                headers={
                    "APCA-API-KEY-ID": self._api_key,
                    "APCA-API-SECRET-KEY": self._api_secret,
                },
            )
        payload = response.json() if response.content else {}
        latest_trade = payload.get("latestTrade") or {}
        latest_quote = payload.get("latestQuote") or {}
        minute_bar = payload.get("minuteBar") or {}
        trade_time = self._parse_rfc3339(latest_trade.get("t"))
        quote_time = self._parse_rfc3339(latest_quote.get("t"))
        bar_time = self._parse_rfc3339(minute_bar.get("t"))
        candidates = [
            (trade_time, "latest_trade"),
            (quote_time, "latest_quote"),
            (bar_time, "minute_bar"),
        ]
        valid_candidates = [item for item in candidates if item[0] is not None]
        data_source = max(valid_candidates, key=lambda item: item[0])[1] if valid_candidates else None
        trade_age = self._timestamp_age_seconds(trade_time, server_now)
        quote_age = self._timestamp_age_seconds(quote_time, server_now)
        bar_age = self._timestamp_age_seconds(bar_time, server_now)
        session = self._session(server_now.to_pydatetime())
        active_session = session in {"pre_market", "regular"}
        quote_live = quote_age is not None and quote_age < 10
        trade_live = trade_age is not None and trade_age < 30
        bar_acceptable = bar_age is not None and bar_age < 120
        feed_is_sip = self._feed == "sip"
        live = (
            response.status_code < 400
            and feed_is_sip
            and (quote_live or trade_live)
            and (bar_acceptable or not active_session)
        )
        if response.status_code >= 400:
            diagnostic_status = "alpaca_error"
            diagnostic_error = payload.get("message") or f"Alpaca HTTP {response.status_code}"
        elif not feed_is_sip:
            diagnostic_status = "wrong_feed"
            diagnostic_error = "مصدر البيانات ليس SIP؛ لا يمكن إثبات بيانات السوق الأمريكي الكاملة."
        elif live:
            diagnostic_status = "live"
            diagnostic_error = None
        else:
            diagnostic_status = "stale"
            diagnostic_error = "بيانات SIP وصلت، لكن Quote وTrade لا يحققان حدود الحداثة المطلوبة."

        clean = {
            "symbol": symbol,
            "data_feed": self._feed,
            "server_now_utc": server_now.isoformat(),
            "market_session": session,
            "requested_feed": self._feed,
            "snapshot_request_url": f"{request_url}?feed={self._feed}",
            "alpaca_http_status": response.status_code,
            "latest_trade_price": latest_trade.get("p"),
            "latest_trade_timestamp": trade_time.isoformat() if trade_time is not None else None,
            "latest_trade_age_seconds": trade_age,
            "bid_price": latest_quote.get("bp"),
            "ask_price": latest_quote.get("ap"),
            "latest_quote_timestamp": quote_time.isoformat() if quote_time is not None else None,
            "latest_quote_age_seconds": quote_age,
            "minute_bar_close": minute_bar.get("c"),
            "minute_bar_timestamp": bar_time.isoformat() if bar_time is not None else None,
            "minute_bar_age_seconds": bar_age,
            "source_used_for_current_price": data_source,
            "status": diagnostic_status,
            "is_live": live,
            "diagnostic_error": diagnostic_error,
            "http_status": response.status_code,
            "latest_trade": {
                "price": latest_trade.get("p"),
                "timestamp": trade_time.isoformat() if trade_time is not None else None,
            },
            "latest_quote": {
                "bid_price": latest_quote.get("bp"),
                "ask_price": latest_quote.get("ap"),
                "timestamp": quote_time.isoformat() if quote_time is not None else None,
            },
            "minute_bar": {
                "close": minute_bar.get("c"),
                "timestamp": bar_time.isoformat() if bar_time is not None else None,
            },
            "calculated_trade_age_seconds": trade_age,
            "calculated_quote_age_seconds": quote_age,
            "calculated_bar_age_seconds": bar_age,
            "data_source": data_source,
        }
        logger.info("alpaca_market_data_debug %s", json.dumps(clean, ensure_ascii=False))
        return clean

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
