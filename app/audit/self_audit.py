"""Scheduled self-audit job (SRS 15) — activated in M3.

Daily: for each horizon (5, 10, 20 trading sessions), find analyses old enough that
haven't been audited at that horizon yet, fetch the realized price path since the
analysis, compare it against the analysis's `audit_targets`, compute a declared
Outcome Score (SRS 15.2), and roll it into `accuracy_stats` by (regime, horizon).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from sqlalchemy.orm import Session

from app.db import repository
from app.db.models import AuditTarget
from app.providers.factory import get_market_data_provider

logger = logging.getLogger(__name__)

# Horizon set per `Analysis.kind` — "single" (full analyses, SRS 15) checks 5/10/20
# trading sessions; "snipe" cards (new feature, not in SRS) check 1 and 5 sessions per
# the owner's spec ("بعد 1 و5 جلسات"). horizon=5 intentionally appears in both: the
# snipe scanner's touch-probability model (touch_probability.py) uses the same 5-session
# window, so the computed-vs-realized comparison lines up.
HORIZONS_BY_KIND: dict[str, tuple[int, ...]] = {"single": (5, 10, 20), "snipe": (1, 5)}
# Rough calendar-day cutoff so obviously-too-recent analyses aren't even queried; real
# eligibility is then confirmed against the actual trading calendar embedded in the
# fetched OHLCV index (same no-holiday-calendar approximation as core/cache.py).
CALENDAR_DAYS_PER_TRADING_DAY = 1.5


@dataclass(frozen=True)
class AuditOutcome:
    scenario_realized: str
    levels_touched: dict[str, bool]
    outcome_score: float
    invalidation_touched_first: bool


@dataclass(frozen=True)
class SnipeAuditOutcome:
    """Evaluation for a Snipe scanner card: did each target zone (resistance above price)
    get touched before the invalidation level (support-derived, below price)? Distinct
    from `AuditOutcome` because a snipe card has up to two upside zones racing against one
    invalidation, not one resistance/support pair per a bullish/bearish/neutral scenario."""

    zone1_touched: bool
    zone2_touched: bool
    invalidation_touched: bool
    zone1_touched_first: bool
    zone2_touched_first: bool
    outcome_score: float | None  # mean of touched-first flags over zones that exist; None if neither zone was defined
    scenario_realized: str  # "bullish" (a zone led), "bearish" (invalidation led), "neutral" (neither touched)


def _bar_index_on_or_after(daily: pd.DataFrame, target_date: date) -> int | None:
    target_ts = pd.Timestamp(target_date)
    positions = daily.index[daily.index >= target_ts]
    if len(positions) == 0:
        return None
    return daily.index.get_loc(positions[0])


def evaluate_targets(window: pd.DataFrame, targets: list[AuditTarget], scenario: str) -> AuditOutcome:
    """`window` is the realized OHLCV strictly after the analysis day through the horizon day."""
    resistances = [t for t in targets if t.level_type == "resistance"]
    supports = [t for t in targets if t.level_type == "support"]
    invalidations = [t for t in targets if t.level_type == "invalidation"]

    resistance_level = float(resistances[0].level_value) if resistances else None
    support_level = float(supports[0].level_value) if supports else None
    invalidation_level = float(invalidations[0].level_value) if invalidations else None

    resistance_touch_pos = support_touch_pos = invalidation_touch_pos = None
    for pos, (_, row) in enumerate(window.iterrows()):
        if resistance_level is not None and resistance_touch_pos is None and row["high"] >= resistance_level:
            resistance_touch_pos = pos
        if support_level is not None and support_touch_pos is None and row["low"] <= support_level:
            support_touch_pos = pos
        if invalidation_level is not None and invalidation_touch_pos is None and row["low"] <= invalidation_level:
            invalidation_touch_pos = pos

    resistance_touched = resistance_touch_pos is not None
    support_touched = support_touch_pos is not None
    invalidation_touched = invalidation_touch_pos is not None

    invalidation_touched_first = False
    if invalidation_touched:
        target_pos = resistance_touch_pos if scenario == "bullish" else support_touch_pos
        invalidation_touched_first = target_pos is None or invalidation_touch_pos <= target_pos

    scenario_realized = _determine_scenario_realized(scenario, resistance_touched, support_touched, invalidation_touched)
    outcome_score = _compute_outcome_score(scenario, scenario_realized, invalidation_touched_first)

    return AuditOutcome(
        scenario_realized=scenario_realized,
        levels_touched={
            "resistance": resistance_touched, "support": support_touched, "invalidation": invalidation_touched,
        },
        outcome_score=outcome_score,
        invalidation_touched_first=invalidation_touched_first,
    )


def _determine_scenario_realized(
    scenario: str, resistance_touched: bool, support_touched: bool, invalidation_touched: bool
) -> str:
    if scenario == "bullish":
        if resistance_touched and not invalidation_touched:
            return "bullish"
        if invalidation_touched:
            return "bearish"
        return "neutral"
    if scenario == "bearish":
        if support_touched and not resistance_touched:
            return "bearish"
        if resistance_touched:
            return "bullish"
        return "neutral"
    # neutral / ranging
    if not resistance_touched and not support_touched:
        return "neutral"
    if support_touched and not resistance_touched:
        return "bearish"
    if resistance_touched and not support_touched:
        return "bullish"
    return "mixed"


def _compute_outcome_score(scenario: str, scenario_realized: str, invalidation_touched_first: bool) -> float:
    """Declared, calibratable rubric (SRS 15.2, values re-tunable once real outcome data
    exists per SRS 15.4): scenario materialized without hitting invalidation = 1.0;
    invalidation hit first = 0.1; partial/mixed = 0.4-0.5; an expected ranging call that
    stayed neutral = 0.7.
    """
    if scenario == "neutral":
        return 0.2 if invalidation_touched_first else 0.7
    if invalidation_touched_first:
        return 0.1
    if scenario_realized == scenario:
        return 1.0
    if scenario_realized == "neutral":
        return 0.5
    if scenario_realized == "mixed":
        return 0.4
    return 0.2  # realized the opposite direction without a clean invalidation-first read


def evaluate_snipe_targets(window: pd.DataFrame, targets: list[AuditTarget]) -> SnipeAuditOutcome:
    """`window` is the realized OHLCV strictly after the analysis day through the horizon
    day, same convention as `evaluate_targets`."""
    zone1 = next((t for t in targets if t.level_type == "target_zone_1"), None)
    zone2 = next((t for t in targets if t.level_type == "target_zone_2"), None)
    invalidation = next((t for t in targets if t.level_type == "invalidation"), None)

    zone1_level = float(zone1.level_value) if zone1 is not None else None
    zone2_level = float(zone2.level_value) if zone2 is not None else None
    invalidation_level = float(invalidation.level_value) if invalidation is not None else None
    direction = next(
        (
            getattr(target, "scenario", None)
            for target in targets
            if getattr(target, "scenario", None) in {"bullish", "bearish"}
        ),
        "bullish",
    )

    zone1_touch_pos = zone2_touch_pos = invalidation_touch_pos = None
    for pos, (_, row) in enumerate(window.iterrows()):
        zone1_hit = (
            row["high"] >= zone1_level
            if direction == "bullish"
            else row["low"] <= zone1_level
        ) if zone1_level is not None else False
        zone2_hit = (
            row["high"] >= zone2_level
            if direction == "bullish"
            else row["low"] <= zone2_level
        ) if zone2_level is not None else False
        invalidation_hit = (
            row["low"] <= invalidation_level
            if direction == "bullish"
            else row["high"] >= invalidation_level
        ) if invalidation_level is not None else False
        if zone1_touch_pos is None and zone1_hit:
            zone1_touch_pos = pos
        if zone2_touch_pos is None and zone2_hit:
            zone2_touch_pos = pos
        if invalidation_touch_pos is None and invalidation_hit:
            invalidation_touch_pos = pos

    zone1_touched = zone1_touch_pos is not None
    zone2_touched = zone2_touch_pos is not None
    invalidation_touched = invalidation_touch_pos is not None

    zone1_touched_first = zone1_touched and (invalidation_touch_pos is None or zone1_touch_pos <= invalidation_touch_pos)
    zone2_touched_first = zone2_touched and (invalidation_touch_pos is None or zone2_touch_pos <= invalidation_touch_pos)

    per_zone_scores: list[float] = []
    if zone1_level is not None:
        per_zone_scores.append(1.0 if zone1_touched_first else 0.0)
    if zone2_level is not None:
        per_zone_scores.append(1.0 if zone2_touched_first else 0.0)
    outcome_score = sum(per_zone_scores) / len(per_zone_scores) if per_zone_scores else None

    if zone1_touched_first or zone2_touched_first:
        scenario_realized = direction
    elif invalidation_touched:
        scenario_realized = "bearish" if direction == "bullish" else "bullish"
    else:
        scenario_realized = "neutral"

    return SnipeAuditOutcome(
        zone1_touched=zone1_touched,
        zone2_touched=zone2_touched,
        invalidation_touched=invalidation_touched,
        zone1_touched_first=zone1_touched_first,
        zone2_touched_first=zone2_touched_first,
        outcome_score=outcome_score,
        scenario_realized=scenario_realized,
    )


def run_self_audit_once(db: Session, as_of: date | None = None) -> dict[int, int]:
    """Entry point for both the scheduler (M3) and manual/test invocation. Returns how
    many analyses were newly audited per horizon (summed across kinds — horizon 5 is
    shared by both "single" and "snipe")."""
    as_of = as_of or datetime.now(timezone.utc).date()
    market_data_provider = get_market_data_provider()
    all_horizons = sorted({horizon for horizons in HORIZONS_BY_KIND.values() for horizon in horizons})
    summary = {horizon: 0 for horizon in all_horizons}

    for kind, horizons in HORIZONS_BY_KIND.items():
        for horizon_days in horizons:
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=int(horizon_days * CALENDAR_DAYS_PER_TRADING_DAY) + 3
            )
            pending = repository.get_analyses_pending_audit(db, horizon_days, cutoff, kind=kind)

            for analysis in pending:
                targets = repository.get_audit_targets_for_analysis(db, analysis.id)
                if not targets:
                    continue  # nothing verifiable was extracted for this analysis

                try:
                    daily = market_data_provider.get_daily_ohlcv(
                        analysis.symbol, lookback_days=90 + horizon_days * 3
                    )
                except Exception:
                    logger.warning("self-audit: failed to fetch data for %s", analysis.symbol, exc_info=True)
                    continue

                start_idx = _bar_index_on_or_after(daily, analysis.requested_at.date())
                if start_idx is None:
                    continue
                horizon_idx = start_idx + horizon_days
                if horizon_idx >= len(daily):
                    continue  # not enough real trading bars yet — picked up on a later run

                window = daily.iloc[start_idx + 1 : horizon_idx + 1]

                if kind == "snipe":
                    snipe_outcome = evaluate_snipe_targets(window, targets)
                    repository.create_audit_result(
                        db,
                        analysis_id=analysis.id,
                        horizon_days=horizon_days,
                        price_at_check=float(window["close"].iloc[-1]),
                        max_high_since=float(window["high"].max()),
                        min_low_since=float(window["low"].min()),
                        scenario_realized=snipe_outcome.scenario_realized,
                        levels_touched={
                            "target_zone_1": snipe_outcome.zone1_touched,
                            "target_zone_2": snipe_outcome.zone2_touched,
                            "invalidation": snipe_outcome.invalidation_touched,
                            "zone1_touched_first": snipe_outcome.zone1_touched_first,
                            "zone2_touched_first": snipe_outcome.zone2_touched_first,
                        },
                        outcome_score=snipe_outcome.outcome_score,
                        notes=(
                            f"zone1_touched_first={snipe_outcome.zone1_touched_first} "
                            f"zone2_touched_first={snipe_outcome.zone2_touched_first}"
                        ),
                    )
                    # No accuracy_stats update for snipe cards — that table's (regime,
                    # horizon) key doesn't fit the two-zone shape; the Snipe scanner's
                    # own accuracy panel (repository.get_recent_snipe_audit_results,
                    # engines/screener/snipe_accuracy.py) reads `levels_touched` directly.
                else:
                    scenario = targets[0].scenario or "neutral"
                    outcome = evaluate_targets(window, targets, scenario)
                    repository.create_audit_result(
                        db,
                        analysis_id=analysis.id,
                        horizon_days=horizon_days,
                        price_at_check=float(window["close"].iloc[-1]),
                        max_high_since=float(window["high"].max()),
                        min_low_since=float(window["low"].min()),
                        scenario_realized=outcome.scenario_realized,
                        levels_touched=outcome.levels_touched,
                        outcome_score=outcome.outcome_score,
                        notes=f"invalidation_touched_first={outcome.invalidation_touched_first}",
                    )
                    repository.upsert_accuracy_stats(db, analysis.regime, horizon_days, outcome.outcome_score)

                summary[horizon_days] += 1

    return summary
