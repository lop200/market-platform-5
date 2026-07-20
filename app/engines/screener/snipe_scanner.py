"""Snipe scanner universe scan — new feature ("قنص اليوم"), not in the original SRS.

Reuses the existing screener's universe fetch and "active stocks" prefilter (same ~200
curated symbols, same top-50-by-dollar-volume filter as `daily_readiness`) rather than
introducing a second network call — CLAUDE.md rule 3 (cost gate discipline) means a
second live "most actives" fetch would need its own gate reservation for no real benefit,
and the owner confirmed reusing the existing prefilter is sufficient (2026-07-17).

Deterministic engine only (no LLM, CLAUDE.md rule 1). Returns plain dataclasses, not the
Pydantic API schema or any DB writes — `core/orchestrator.run_snipe_scan` owns turning
these into `SnipeStockCard`s (probabilities, visual-bar position) and persisting the
audit targets, matching how `run_universe_scan` stays a pure compute step.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from app.engines.deterministic.engine import run_deterministic_engine
from app.engines.deterministic.schemas import DeterministicAnalysis
from app.engines.screener.scanner import TOP_VOLUME_UNIVERSE, fetch_universe
from app.engines.screener.snipe_scoring import compute_snipe_score
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
    invalidation_price: float
    zone1_price: float | None  # nearest resistance by price ("المنطقة الفنية الأولى")
    zone2_price: float | None  # next resistance by price ("المنطقة الفنية الثانية")


def _daily_change_pct(daily: pd.DataFrame) -> float:
    if len(daily) < 2:
        return 0.0
    prev_close = float(daily["close"].iloc[-2])
    last_close = float(daily["close"].iloc[-1])
    if prev_close == 0:
        return 0.0
    return (last_close / prev_close - 1) * 100


def _build_candidate(symbol: str, analysis: DeterministicAnalysis, daily: pd.DataFrame) -> SnipeCandidate | None:
    invalidation = analysis.levels.invalidation
    if invalidation is None:
        return None  # no detected support -> no invalidation -> can't race zones vs it

    # `analysis.levels.resistances` is sorted by strength_score (levels.py), not price —
    # re-sort by price ascending to get "nearest" and "next" resistance zones.
    nearest_first = sorted(analysis.levels.resistances, key=lambda lv: lv.price)
    zone1_price = nearest_first[0].price if nearest_first else None
    zone2_price = nearest_first[1].price if len(nearest_first) > 1 else None

    score, reasons = compute_snipe_score(analysis, daily)

    return SnipeCandidate(
        symbol=symbol,
        analysis=analysis,
        daily=daily,
        score=score,
        reasons=reasons,
        daily_change_pct=_daily_change_pct(daily),
        invalidation_price=invalidation,
        zone1_price=zone1_price,
        zone2_price=zone2_price,
    )


def run_snipe_universe_scan(
    provider: MarketDataAdapter, symbols: list[str]
) -> tuple[list[SnipeCandidate], int]:
    """Fetch once, score the top-50-by-dollar-volume "active" subset, return the top 10.

    Returns (candidates, successfully_fetched_count) — the caller uses the fetched count
    to compute the actual cost-gate spend, same pattern as `scanner.run_universe_scan`.
    """
    fetched = fetch_universe(provider, symbols)

    analyses: dict[str, tuple[DeterministicAnalysis, pd.DataFrame]] = {}
    for symbol, daily in fetched.items():
        try:
            analysis = run_deterministic_engine(symbol, daily, data_quality="daily_only")
        except Exception:
            continue
        analyses[symbol] = (analysis, daily)

    by_volume = sorted(analyses.items(), key=lambda kv: kv[1][0].liquidity.dollar_volume, reverse=True)
    active_universe = by_volume[:TOP_VOLUME_UNIVERSE]

    candidates: list[SnipeCandidate] = []
    for symbol, (analysis, daily) in active_universe:
        candidate = _build_candidate(symbol, analysis, daily)
        if candidate is not None:
            candidates.append(candidate)

    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates[:SNIPE_CARDS_DISPLAYED], len(fetched)
