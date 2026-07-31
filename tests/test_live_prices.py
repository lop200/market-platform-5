from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.routes_prices import HEARTBEAT_SECONDS, price_events
from app.live.prices import STALE_AFTER_SECONDS, PriceBook, price_book
from app.main import app

NOW = datetime.now(timezone.utc)


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


async def _collect(symbols, *, max_frames, interval=0.01, heartbeat_after=HEARTBEAT_SECONDS):
    return [
        frame
        async for frame in price_events(
            symbols, interval, max_frames=max_frames, heartbeat_after=heartbeat_after
        )
    ]
