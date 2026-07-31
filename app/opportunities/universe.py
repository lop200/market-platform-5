"""Decide which symbols are worth scanning before spending any data calls.

Scanning an arbitrary slice of every tradable US equity costs a quote and a
bar pull per symbol and almost never yields a candidate: the names that pass
the freshness and volume gates are the ones actually trading today. Asking the
provider for that ranking first is both cheaper and better targeted.
"""
from __future__ import annotations

import logging

from app.config import Settings
from app.providers.base import MarketDataAdapter

logger = logging.getLogger(__name__)

# Names carrying reliable liquidity in every session, including overnight.
# They anchor the universe when the provider cannot rank the market for us.
CORE_SYMBOLS = ("SPY", "QQQ", "IWM", "DIA")


def select_scan_universe(
    provider: MarketDataAdapter, settings: Settings, limit: int
) -> tuple[list[str], dict]:
    """Return the symbols to scan plus a record of where they came from.

    The record is stored on the scan run so an empty result can be explained
    without guessing which branch produced the list.
    """
    limit = max(1, int(limit))
    curated = list(CORE_SYMBOLS) + settings.configured_sniper_symbols + settings.configured_scan_symbols

    ranked: list[str] = []
    try:
        ranked = provider.list_most_active_symbols(limit)
    except Exception:
        # A screener outage must not cancel the scan; the curated list stands in.
        logger.warning("most-active screener unavailable; using the curated list", exc_info=True)

    if ranked:
        source = "most_active"
        symbols = ranked + [item for item in curated if item not in set(ranked)]
    else:
        source = "curated"
        symbols = curated

    symbols = list(dict.fromkeys(item.upper() for item in symbols))[:limit]
    return symbols, {
        "universe_source": source,
        "universe_size": len(symbols),
        "ranked_by_provider": len(ranked),
    }
