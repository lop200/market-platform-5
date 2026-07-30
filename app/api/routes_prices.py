"""Live price endpoints: a snapshot read and a server-sent event stream."""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from app.config import get_settings
from app.live.prices import price_book, stream_status

router = APIRouter(prefix="/api/v1/prices", tags=["prices"])

# Keeps a proxy from closing an idle connection while the market is quiet.
HEARTBEAT_SECONDS = 20


def _requested_symbols(symbols: str | None) -> list[str] | None:
    if not symbols:
        return None
    parsed = [item.strip().upper() for item in symbols.split(",") if item.strip()]
    return parsed or None


@router.get("")
def price_snapshot(symbols: str | None = Query(default=None)) -> dict:
    settings = get_settings()
    return {
        **price_book.snapshot(_requested_symbols(symbols)),
        "stream": stream_status(),
        "tracked": settings.configured_sniper_symbols,
        "poll_ms": settings.live_prices_poll_ms,
    }


async def price_events(
    selected: list[str] | None,
    interval: float,
    *,
    max_frames: int | None = None,
    heartbeat_after: float = HEARTBEAT_SECONDS,
):
    """Yield an SSE frame whenever the book changes; heartbeat when it does not.

    ``max_frames`` bounds the generator so it can be driven from a test; the
    endpoint itself leaves it unset and runs until the client disconnects.
    """
    last_version = -1
    idle = 0.0
    frames = 0
    while max_frames is None or frames < max_frames:
        version = price_book.version
        if version != last_version:
            last_version = version
            idle = 0.0
            payload = price_book.snapshot(selected)
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        else:
            idle += interval
            if idle >= heartbeat_after:
                idle = 0.0
                yield ": heartbeat\n\n"
        frames += 1
        if max_frames is not None and frames >= max_frames:
            return
        await asyncio.sleep(interval)


@router.get("/stream")
async def price_stream(symbols: str | None = Query(default=None)) -> StreamingResponse:
    settings = get_settings()
    return StreamingResponse(
        price_events(
            _requested_symbols(symbols),
            max(0.25, settings.live_prices_poll_ms / 1000),
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
