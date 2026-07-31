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

# Alpaca serves each session on its own socket. The SDK only knows IEX and SIP,
# so the overnight feeds are reached by overriding the endpoint instead — and
# they sit under v1beta1, not v2. Probing the host directly: /v2/sip and
# /v2/iex accept the socket, while /v2/boats and /v2/overnight answer the
# handshake with HTTP 404 and only /v1beta1/* connect.
STREAM_HOST = "wss://stream.data.alpaca.markets"
STREAM_VERSIONS = {"sip": "v2", "iex": "v2", "boats": "v1beta1", "overnight": "v1beta1"}
OVERNIGHT_FEEDS = {"boats", "overnight"}


def stream_endpoint(feed: str) -> str:
    return f"{STREAM_HOST}/{STREAM_VERSIONS.get(feed, 'v2')}/{feed}"

# How often the supervisor re-checks which feed the current session needs.
FEED_CHECK_SECONDS = 30
# Pause before reconnecting so a rejected credential cannot spin the thread.
RECONNECT_SECONDS = 15


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


class _SdkLogCapture(logging.Handler):
    """Keep the SDK's connection errors, which never reach our except block.

    alpaca-py's run loop catches every exception, logs it, and retries. A bad
    endpoint, a rejected key, or an unentitled feed therefore looks identical
    to a quiet market: no error, no data. This is the only place that
    difference is visible.
    """

    SDK_LOGGERS = ("alpaca.data.live.websocket", "alpaca.data.live.stock")

    def __init__(self, stream: "AlpacaPriceStream"):
        super().__init__(level=logging.WARNING)
        self._stream = stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._stream.note_error(record.getMessage())
        except Exception:  # pragma: no cover - a broken handler must stay silent
            pass

    def attach(self) -> None:
        for name in self.SDK_LOGGERS:
            logging.getLogger(name).addHandler(self)

    def detach(self) -> None:
        for name in self.SDK_LOGGERS:
            logging.getLogger(name).removeHandler(self)


class AlpacaPriceStream:
    """Owns the Alpaca websocket lifecycle and writes into a PriceBook."""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        feed: str,
        symbols: list[str],
        book: PriceBook,
        *,
        overnight_feed: str = "boats",
    ):
        self._api_key = api_key
        self._secret_key = secret_key
        self._feed = feed.lower()
        self._overnight_feed = overnight_feed.lower()
        self._symbols = symbols
        self._book = book
        self._thread: threading.Thread | None = None
        self._stream = None
        self._started = False
        self._stopping = threading.Event()
        self._connected_feed: str | None = None
        self._last_error: str | None = None
        self._messages = 0
        self._capture = _SdkLogCapture(self)

    def note_error(self, message: str) -> None:
        self._last_error = message[:500]

    @property
    def running(self) -> bool:
        """True only once the socket is up and the subscription was sent.

        The SDK flips its own ``_running`` at that point. Reporting anything
        earlier — a live thread, a constructed client — tells the page to wait
        for prices that may never arrive.
        """
        if not (self._thread and self._thread.is_alive()):
            return False
        return bool(getattr(self._stream, "_running", False))

    @property
    def connected_feed(self) -> str | None:
        return self._connected_feed if self.running else None

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def messages_received(self) -> int:
        """Messages the socket delivered, before the book accepted or rejected them."""
        return self._messages

    def feed_for(self, now: datetime | None = None) -> str:
        """Match the provider: BOATS/Overnight only in the overnight session."""
        from app.options.market_clock import market_session

        session = market_session(now or datetime.now(timezone.utc)).code
        return self._overnight_feed if session == "overnight" else self._feed

    def _build_stream(self, feed: str):
        from alpaca.data.enums import DataFeed
        from alpaca.data.live import StockDataStream

        if feed in OVERNIGHT_FEEDS:
            return StockDataStream(
                self._api_key,
                self._secret_key,
                url_override=stream_endpoint(feed),
            )
        return StockDataStream(
            self._api_key,
            self._secret_key,
            feed=DataFeed.SIP if feed == "sip" else DataFeed.IEX,
        )

    async def _on_trade(self, trade) -> None:
        self._messages += 1
        self._book.record(
            trade.symbol,
            price=float(trade.price),
            updated_at=trade.timestamp,
            source="trade",
        )

    async def _on_quote(self, quote) -> None:
        self._messages += 1
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

    def _watch_session(self, feed: str, stream) -> None:
        """Drop the socket once the session calls for a different feed.

        The SDK's run() blocks for the life of the connection, so the only way
        to move from SIP to BOATS at 20:00 New York is to close it and let the
        supervisor reconnect.
        """
        while not self._stopping.wait(FEED_CHECK_SECONDS):
            if self.feed_for() != feed:
                logger.info("live price stream switching feed: %s is no longer current", feed)
                break
        try:
            stream.stop()
        except Exception:
            logger.warning("live price stream did not stop cleanly", exc_info=True)

    def _run(self) -> None:
        # The SDK's run() owns its own event loop, so it needs a dedicated
        # thread; uvicorn's loop must stay free to serve requests.
        asyncio.set_event_loop(asyncio.new_event_loop())
        self._capture.attach()
        while not self._stopping.is_set():
            feed = self.feed_for()
            try:
                stream = self._build_stream(feed)
                stream.subscribe_trades(self._on_trade, *self._symbols)
                stream.subscribe_quotes(self._on_quote, *self._symbols)
                self._stream = stream
                self._connected_feed = feed
                logger.info(
                    "live price stream connecting to %s for %d symbols", feed, len(self._symbols)
                )
                threading.Thread(
                    target=self._watch_session,
                    args=(feed, stream),
                    name="alpaca-price-feed-watch",
                    daemon=True,
                ).start()
                stream.run()
            except Exception as exc:
                # A dead socket must never take the web process down; REST
                # quotes remain the fallback and the book simply goes stale.
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("live price stream stopped")
            self._connected_feed = None
            self._stopping.wait(RECONNECT_SECONDS)

    def start(self) -> bool:
        if self._started or not self._symbols:
            return False
        self._started = True
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="alpaca-price-stream", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._started = False
        self._stopping.set()
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
        overnight_feed=settings.alpaca_overnight_feed,
    )
    return _stream.start()


def stop_price_stream() -> None:
    global _stream
    if _stream is not None:
        _stream.stop()
        _stream = None


def stream_status() -> dict:
    stream = _stream
    return {
        "running": bool(stream and stream.running),
        "feed": stream.connected_feed if stream else None,
        "requested_feed": stream.feed_for() if stream else None,
        "last_error": stream.last_error if stream else None,
        # Separates "the socket delivered nothing" from "the book rejected
        # everything it delivered" — they look identical from the price list.
        "messages_received": stream.messages_received if stream else 0,
        "tracked_symbols": len(price_book.snapshot()["prices"]),
    }
