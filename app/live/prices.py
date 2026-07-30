"""In-memory live price book fed by an Alpaca websocket subscription.

The book is the only thing the web layer reads. Whether it is being fed by a
real socket, or is simply cold because streaming is disabled, callers see the
same shape and can always tell how old a price is.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# A price older than this is reported as stale rather than shown as live.
STALE_AFTER_SECONDS = 15


@dataclass(frozen=True)
class LivePrice:
    symbol: str
    price: float
    bid: float | None
    ask: float | None
    updated_at: datetime
    source: str

    def as_dict(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        age = max(0.0, (now - self.updated_at).total_seconds())
        return {
            "symbol": self.symbol,
            "price": round(self.price, 4),
            "bid": round(self.bid, 4) if self.bid is not None else None,
            "ask": round(self.ask, 4) if self.ask is not None else None,
            "updated_at": self.updated_at.isoformat(),
            "age_seconds": round(age, 1),
            "source": self.source,
            "stale": age > STALE_AFTER_SECONDS,
        }


@dataclass
class PriceBook:
    """Thread-safe latest-price-per-symbol store.

    Alpaca's SDK delivers callbacks on its own event loop thread while requests
    read from worker threads, so every access takes the lock.
    """

    _prices: dict[str, LivePrice] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _version: int = 0

    def record(
        self,
        symbol: str,
        *,
        price: float,
        bid: float | None = None,
        ask: float | None = None,
        updated_at: datetime | None = None,
        source: str = "trade",
    ) -> None:
        if price is None or price <= 0:
            return
        stamp = updated_at or datetime.now(timezone.utc)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        with self._lock:
            existing = self._prices.get(symbol)
            # Out-of-order websocket messages must not rewind a newer price.
            if existing is not None and stamp < existing.updated_at:
                return
            self._prices[symbol] = LivePrice(
                symbol=symbol,
                price=float(price),
                bid=bid if bid is not None else (existing.bid if existing else None),
                ask=ask if ask is not None else (existing.ask if existing else None),
                updated_at=stamp,
                source=source,
            )
            self._version += 1

    def snapshot(self, symbols: list[str] | None = None) -> dict:
        now = datetime.now(timezone.utc)
        with self._lock:
            version = self._version
            selected = (
                [self._prices[s] for s in symbols if s in self._prices]
                if symbols is not None
                else list(self._prices.values())
            )
        return {
            "version": version,
            "generated_at": now.isoformat(),
            "prices": [item.as_dict(now) for item in sorted(selected, key=lambda i: i.symbol)],
        }

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def clear(self) -> None:
        with self._lock:
            self._prices.clear()
            self._version = 0


price_book = PriceBook()


class AlpacaPriceStream:
    """Owns the Alpaca websocket lifecycle and writes into a PriceBook."""

    def __init__(self, api_key: str, secret_key: str, feed: str, symbols: list[str], book: PriceBook):
        self._api_key = api_key
        self._secret_key = secret_key
        self._feed = feed
        self._symbols = symbols
        self._book = book
        self._thread: threading.Thread | None = None
        self._stream = None
        self._started = False

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _build_stream(self):
        from alpaca.data.enums import DataFeed
        from alpaca.data.live import StockDataStream

        feed = DataFeed.SIP if self._feed.lower() == "sip" else DataFeed.IEX
        return StockDataStream(self._api_key, self._secret_key, feed=feed)

    async def _on_trade(self, trade) -> None:
        self._book.record(
            trade.symbol,
            price=float(trade.price),
            updated_at=trade.timestamp,
            source="trade",
        )

    async def _on_quote(self, quote) -> None:
        bid, ask = float(quote.bid_price or 0), float(quote.ask_price or 0)
        if bid <= 0 or ask <= 0:
            return
        self._book.record(
            quote.symbol,
            price=(bid + ask) / 2,
            bid=bid,
            ask=ask,
            updated_at=quote.timestamp,
            source="quote",
        )

    def _run(self) -> None:
        # The SDK's run() owns its own event loop, so it needs a dedicated
        # thread; uvicorn's loop must stay free to serve requests.
        asyncio.set_event_loop(asyncio.new_event_loop())
        try:
            self._stream = self._build_stream()
            self._stream.subscribe_trades(self._on_trade, *self._symbols)
            self._stream.subscribe_quotes(self._on_quote, *self._symbols)
            logger.info("live price stream connecting for %d symbols", len(self._symbols))
            self._stream.run()
        except Exception:
            # A dead stream must never take the web process down; REST quotes
            # remain the fallback and the book simply goes stale.
            logger.exception("live price stream stopped")

    def start(self) -> bool:
        if self._started or not self._symbols:
            return False
        self._started = True
        self._thread = threading.Thread(target=self._run, name="alpaca-price-stream", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._started = False
        stream = self._stream
        if stream is None:
            return
        try:
            stream.stop()
        except Exception:
            logger.warning("live price stream did not stop cleanly", exc_info=True)


_stream: AlpacaPriceStream | None = None


def start_price_stream(settings) -> bool:
    """Start streaming if enabled and credentials exist. Safe to call twice."""
    global _stream
    if _stream is not None and _stream.running:
        return False
    if not settings.live_prices_enabled:
        return False
    if not (settings.alpaca_api_key and settings.alpaca_api_secret):
        logger.info("live price stream skipped: no Alpaca credentials")
        return False
    symbols = settings.configured_sniper_symbols
    if not symbols:
        return False
    _stream = AlpacaPriceStream(
        settings.alpaca_api_key,
        settings.alpaca_api_secret,
        settings.alpaca_feed,
        symbols,
        price_book,
    )
    return _stream.start()


def stop_price_stream() -> None:
    global _stream
    if _stream is not None:
        _stream.stop()
        _stream = None


def stream_status() -> dict:
    return {
        "running": bool(_stream and _stream.running),
        "tracked_symbols": len(price_book.snapshot()["prices"]),
    }
