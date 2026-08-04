from __future__ import annotations

from app.providers.base import Quote


def resolve_volume_metrics(
    quote: Quote, indicators: dict | None = None
) -> tuple[int, float, str]:
    """Select one volume observation and derive dollar volume from that same value."""
    indicators = indicators or {}
    session_volume = int(indicators.get("session_volume") or 0)
    quote_volume = int(quote.volume or 0)
    volume = session_volume if session_volume > 0 else quote_volume
    source = "intraday_session_bars" if session_volume > 0 else "alpaca_snapshot_volume"
    return volume, round(float(quote.price) * volume, 2), source
