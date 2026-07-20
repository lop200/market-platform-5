"""Avg volume, RVOL, dollar volume, spread% (SRS 10.4) + Unusual Volume (Annex C-5)."""
from __future__ import annotations

import pandas as pd

from app.engines.deterministic.schemas import Liquidity
from app.providers.base import Quote

UNUSUAL_RVOL_THRESHOLD = 2.5


def compute_liquidity(daily: pd.DataFrame, quote: Quote | None = None) -> Liquidity:
    volume = daily["volume"]
    close = daily["close"]

    avg_volume_20d = float(volume.rolling(window=20).mean().iloc[-1])
    today_volume = float(volume.iloc[-1])
    rvol = today_volume / avg_volume_20d if avg_volume_20d else 0.0
    dollar_volume = today_volume * float(close.iloc[-1])

    spread_pct: float | None = None
    if quote is not None and quote.bid and quote.ask:
        mid = (quote.bid + quote.ask) / 2
        if mid:
            spread_pct = round((quote.ask - quote.bid) / mid * 100, 4)

    unusual_volume = rvol > UNUSUAL_RVOL_THRESHOLD
    direction: str | None = None
    if unusual_volume:
        price_change = float(close.iloc[-1] - close.iloc[-2]) if len(close) > 1 else 0.0
        direction = "up" if price_change > 0 else "down" if price_change < 0 else "flat"

    return Liquidity(
        avg_volume_20d=round(avg_volume_20d, 2),
        rvol=round(rvol, 4),
        dollar_volume=round(dollar_volume, 2),
        spread_pct=spread_pct,
        unusual_volume=unusual_volume,
        unusual_volume_direction=direction,
    )
