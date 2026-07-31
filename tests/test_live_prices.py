from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api.routes_prices import HEARTBEAT_SECONDS, price_events
from app.live.prices import (
    STALE_AFTER_SECONDS,
    AlpacaPriceStream,
    PriceBook,
    price_book,
)
from app.main import app

NOW = datetime.now(timezone.utc)
# 2026-07-31 is a Thursday, so these land inside real trading sessions.
OVERNIGHT = datetime(2026, 7, 31, 6, 30, tzinfo=timezone.utc)  # 02:30 New York
REGULAR = datetime(2026, 7, 31, 15, 0, tzinfo=timezone.utc)  # 11:00 New York


@pytest.fixture(autouse=True)
def clean_book():
    price_book.clear()
    yield
    price_book.clear()


def test_book_reports_age_and_marks_stale_prices():
    book = PriceBook()
    book.record("SPY", price=550.25, bid=550.2, ask=550.3, updated_at=NOW)
    book.record(
        "QQQ",
        price=470.0,
        updated_at=NOW - timedelta(seconds=STALE_AFTER_SECONDS + 5),
    )
    rows = {item["symbol"]: item for item in book.snapshot()["prices"]}
    assert rows["SPY"]["stale"] is False
    assert rows["SPY"]["bid"] == 550.2
    assert rows["QQQ"]["stale"] is True
    assert rows["QQQ"]["age_seconds"] >= STALE_AFTER_SECONDS


def test_out_of_order_messages_never_rewind_a_newer_price():
    book = PriceBook()
    book.record("SPY", price=100.0, updated_at=NOW)
    book.record("SPY", price=99.0, updated_at=NOW - timedelta(seconds=5))
    assert book.snapshot()["prices"][0]["price"] == 100.0


def test_quote_only_update_keeps_bid_ask_then_trade_preserves_them():
    book = PriceBook()
    book.record("SPY", price=550.0, bid=549.9, ask=550.1, updated_at=NOW, source="quote")
    book.record("SPY", price=550.4, updated_at=NOW + timedelta(seconds=1), source="trade")
    row = book.snapshot()["prices"][0]
    assert row["source"] == "trade"
    assert row["price"] == 550.4
    assert (row["bid"], row["ask"]) == (549.9, 550.1)


def test_zero_and_negative_prices_are_ignored():
    book = PriceBook()
    book.record("SPY", price=0, updated_at=NOW)
    book.record("SPY", price=-3, updated_at=NOW)
    assert book.snapshot()["prices"] == []


def test_version_advances_only_on_accepted_updates():
    book = PriceBook()
    assert book.version == 0
    book.record("SPY", price=1.0, updated_at=NOW)
    assert book.version == 1
    book.record("SPY", price=0, updated_at=NOW)
    assert book.version == 1


def test_snapshot_endpoint_filters_by_symbol():
    price_book.record("SPY", price=550.0, updated_at=NOW)
    price_book.record("QQQ", price=470.0, updated_at=NOW)
    client = TestClient(app)
    body = client.get("/api/v1/prices", params={"symbols": "spy"}).json()
    assert [item["symbol"] for item in body["prices"]] == ["SPY"]
    assert body["stream"]["running"] is False
    assert "SPY" in body["tracked"]


def test_stream_emits_the_book_as_an_sse_frame():
    price_book.record("SPY", price=550.0, updated_at=NOW)
    frames = asyncio.run(_collect(["SPY"], max_frames=1))
    assert frames[0].startswith("data: ")
    payload = json.loads(frames[0][len("data: "):])
    assert payload["prices"][0]["symbol"] == "SPY"
    assert payload["prices"][0]["price"] == 550.0
    # The client reads stream state off every frame, not just the snapshot call.
    assert payload["stream"]["running"] is False


def test_stream_sends_a_heartbeat_when_nothing_changes():
    price_book.record("SPY", price=550.0, updated_at=NOW)
    # First frame is the book; with no further updates the rest are heartbeats.
    frames = asyncio.run(_collect(["SPY"], max_frames=3, interval=0.01, heartbeat_after=0.01))
    assert frames[0].startswith("data: ")
    assert frames[1:] == [": heartbeat\n\n", ": heartbeat\n\n"]


def _stream(feed="sip", overnight_feed="boats"):
    return AlpacaPriceStream(
        "key", "secret", feed, ["SPY"], PriceBook(), overnight_feed=overnight_feed
    )


def test_overnight_session_streams_the_overnight_feed():
    # SIP carries nothing before 04:00 New York, so the book would stay empty.
    assert _stream().feed_for(OVERNIGHT) == "boats"
    assert _stream().feed_for(REGULAR) == "sip"


def test_overnight_feed_connects_to_its_own_endpoint():
    # The SDK rejects any feed but IEX/SIP, so BOATS goes through the override.
    overnight = _stream()._build_stream("boats")
    assert overnight._endpoint == "wss://stream.data.alpaca.markets/v2/boats"
    assert _stream()._build_stream("sip")._endpoint.endswith("/v2/sip")


def test_status_reports_disconnected_until_a_socket_is_connected():
    stream = _stream()
    # A thread that is retrying a rejected key is not a live feed.
    assert stream.running is False
    assert stream.connected_feed is None


def test_snapshot_endpoint_reports_the_connected_feed():
    client = TestClient(app)
    body = client.get("/api/v1/prices").json()
    assert body["stream"]["running"] is False
    assert body["stream"]["feed"] is None
    assert body["stream"]["last_error"] is None


async def _collect(symbols, *, max_frames, interval=0.01, heartbeat_after=HEARTBEAT_SECONDS):
    return [
        frame
        async for frame in price_events(
            symbols, interval, max_frames=max_frames, heartbeat_after=heartbeat_after
        )
    ]


def test_status_surfaces_the_sdk_error_it_would_otherwise_swallow():
    import logging

    stream = _stream()
    stream._capture.attach()
    try:
        logging.getLogger("alpaca.data.live.websocket").error(
            "error during websocket communication: insufficient subscription"
        )
    finally:
        stream._capture.detach()
    assert "insufficient subscription" in stream.last_error


def test_running_follows_the_sdk_connection_flag_not_the_thread():
    stream = _stream()
    stream._thread = SimpleNamespace(is_alive=lambda: True)
    stream._stream = SimpleNamespace(_running=False)
    # A live thread with an unconnected socket is not a running feed.
    assert stream.running is False
    assert stream.connected_feed is None
    stream._stream = SimpleNamespace(_running=True)
    stream._connected_feed = "boats"
    assert stream.running is True
    assert stream.connected_feed == "boats"


def test_message_counter_separates_silence_from_rejection():
    stream = _stream()
    assert stream.messages_received == 0
    asyncio.run(stream._on_quote(SimpleNamespace(
        symbol="SPY", bid_price=0, ask_price=0, timestamp=NOW,
    )))
    # The book rejects a zero quote, but the socket did deliver it.
    assert stream.messages_received == 1
