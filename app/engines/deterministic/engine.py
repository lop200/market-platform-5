"""Ties every deterministic module into the single JSON contract (SRS 10.6).

This is the only function the orchestrator (M2) should call to run the full
zero-cost deterministic analysis pipeline.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.engines.deterministic.indicators import compute_indicators
from app.engines.deterministic.levels import detect_levels
from app.engines.deterministic.liquidity import compute_liquidity
from app.engines.deterministic.regime import classify_regime
from app.engines.deterministic.schemas import DeterministicAnalysis
from app.engines.deterministic.scoring import DataQuality, compute_scores
from app.engines.deterministic.smc import compute_smc
from app.engines.deterministic.volatility import compute_volatility
from app.providers.base import Quote


def run_deterministic_engine(
    symbol: str,
    daily: pd.DataFrame,
    intraday: pd.DataFrame | None = None,
    quote: Quote | None = None,
    data_quality: DataQuality = "daily_only",
) -> DeterministicAnalysis:
    last_close = float(daily["close"].iloc[-1])

    indicators = compute_indicators(daily, intraday)
    levels = detect_levels(daily, atr_value=indicators.atr_14, current_price=last_close)
    volatility = compute_volatility(daily)
    liquidity = compute_liquidity(daily, quote)
    regime = classify_regime(daily, indicators, volatility)
    smc = compute_smc(daily)
    scores = compute_scores(daily, indicators, levels, liquidity, data_quality)

    return DeterministicAnalysis(
        symbol=symbol,
        as_of=datetime.now(timezone.utc),
        data_as_of=daily.index[-1].date(),
        data_quality=data_quality,
        last_close=last_close,
        indicators=indicators,
        levels=levels,
        volatility=volatility,
        liquidity=liquidity,
        regime=regime,
        smc=smc,
        scores=scores,
    )
