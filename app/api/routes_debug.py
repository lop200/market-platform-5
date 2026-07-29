from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Query

from app.providers.factory import get_market_data_provider

router = APIRouter(prefix="/api/debug", tags=["market-data-debug"])


@router.get("/market-data/{symbol}")
def market_data_debug(
    symbol: str,
    bypass_cache: bool = Query(default=True),
) -> dict:
    symbol = symbol.upper().strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,9}", symbol):
        raise HTTPException(422, "invalid stock symbol")
    try:
        return get_market_data_provider().debug_market_data(
            symbol,
            bypass_cache=bypass_cache,
        )
    except NotImplementedError as exc:
        raise HTTPException(501, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            502,
            {
                "message": "Alpaca market-data diagnostic failed",
                "error_type": type(exc).__name__,
            },
        ) from exc
