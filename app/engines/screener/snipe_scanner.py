"""Directional universe scan for Today's Snipe."""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.engines.deterministic.engine import run_deterministic_engine
from app.engines.deterministic.schemas import DeterministicAnalysis
from app.engines.screener.scanner import TOP_VOLUME_UNIVERSE, fetch_universe
from app.engines.screener.snipe_scoring import (
    SnipeDirection,
    compute_snipe_score,
    infer_snipe_direction,
)
from app.providers.base import MarketDataAdapter

SNIPE_CARDS_DISPLAYED = 10


@dataclass
class SnipeCandidate:
    symbol: str
    analysis: DeterministicAnalysis
    daily: pd.DataFrame
    score: float
    reasons: list[str]
    daily_change_pct: float
    direction: SnipeDirection
    invalidation_price: float
    zone1_price: float | None
    zone2_price: float | None


def _daily_change_pct(daily: pd.DataFrame) -> float:
    if len(daily) < 2:
        return 0.0
    previous = float(daily["close"].iloc[-2])
    current = float(daily["close"].iloc[-1])
    return (current / previous - 1) * 100 if previous else 0.0


def _build_candidate(
    symbol: str, analysis: DeterministicAnalysis, daily: pd.DataFrame
) -> SnipeCandidate | None:
    """Build levels in the inferred direction; neutral/conflicting reads abstain."""
    direction = infer_snipe_direction(analysis, daily)
    if direction == "neutral":
        return None

    last_close = analysis.last_close
    supports = sorted(
        (level.price for level in analysis.levels.supports if level.price < last_close),
        reverse=True,
    )
    resistances = sorted(
        level.price for level in analysis.levels.resistances if level.price > last_close
    )

    if direction == "bullish":
        if not supports or not resistances:
            return None
        invalidation = supports[0]
        zones = resistances
    else:
        if not resistances or not supports:
            return None
        invalidation = resistances[0]
        zones = supports

    score, reasons = compute_snipe_score(analysis, daily)
    return SnipeCandidate(
        symbol=symbol,
        analysis=analysis,
        daily=daily,
        score=score,
        reasons=reasons,
        daily_change_pct=_daily_change_pct(daily),
        direction=direction,
        invalidation_price=invalidation,
        zone1_price=zones[0] if zones else None,
        zone2_price=zones[1] if len(zones) > 1 else None,
    )


def run_snipe_universe_scan(
    provider: MarketDataAdapter, symbols: list[str]
) -> tuple[list[SnipeCandidate], int]:
    """Fetch once, prefilter by dollar volume, then return directional candidates only."""
    fetched = fetch_universe(provider, symbols)
    analyses: dict[str, tuple[DeterministicAnalysis, pd.DataFrame]] = {}
    for symbol, daily in fetched.items():
        try:
            analysis = run_deterministic_engine(symbol, daily, data_quality="daily_only")
        except Exception:
            continue
        analyses[symbol] = (analysis, daily)

    by_volume = sorted(
        analyses.items(),
        key=lambda item: item[1][0].liquidity.dollar_volume,
        reverse=True,
    )
    candidates: list[SnipeCandidate] = []
    for symbol, (analysis, daily) in by_volume[:TOP_VOLUME_UNIVERSE]:
        candidate = _build_candidate(symbol, analysis, daily)
        if candidate is not None:
            candidates.append(candidate)

    candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    return candidates[:SNIPE_CARDS_DISPLAYED], len(fetched)
