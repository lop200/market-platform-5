"""Read/write helpers over the DB models used by core services (cost gate, cache, and
now the M2 orchestrator's analysis/audit-target persistence, SRS 4.1 steps 7/10, 15.2).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import delete, desc, select
from sqlalchemy.orm import Session

from app.db.models import (
    AccuracyStats,
    Analysis,
    AuditResult,
    AuditTarget,
    CacheEntry,
    CostLedger,
    CostLimits,
    OptionContractAnalysisRecord,
    OptionWatchlistEvent,
    OptionWatchlistItem,
)
from app.engines.deterministic.schemas import Levels


# --- Cost limits ---

def get_or_create_cost_limits(db: Session, daily_cap_usd: float, monthly_cap_usd: float) -> CostLimits:
    limits = db.get(CostLimits, 1)
    if limits is None:
        limits = CostLimits(id=1, daily_cap_usd=daily_cap_usd, monthly_cap_usd=monthly_cap_usd, kill_switch_on=False)
        db.add(limits)
        db.commit()
        db.refresh(limits)
    return limits


def set_kill_switch(db: Session, on: bool) -> CostLimits:
    limits = db.get(CostLimits, 1)
    if limits is None:
        raise ValueError("cost_limits row does not exist yet")
    limits.kill_switch_on = on
    limits.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(limits)
    return limits


def update_cost_caps(db: Session, daily_cap_usd: float | None, monthly_cap_usd: float | None) -> CostLimits:
    limits = db.get(CostLimits, 1)
    if limits is None:
        raise ValueError("cost_limits row does not exist yet")
    if daily_cap_usd is not None:
        limits.daily_cap_usd = daily_cap_usd
    if monthly_cap_usd is not None:
        limits.monthly_cap_usd = monthly_cap_usd
    limits.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(limits)
    return limits


# --- Cost ledger ---

def record_estimated_cost(
    db: Session,
    *,
    category: str,
    provider: str,
    estimated_cost: float,
    analysis_id=None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> CostLedger:
    entry = CostLedger(
        category=category,
        provider=provider,
        analysis_id=analysis_id,
        estimated_cost=estimated_cost,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def record_actual_cost(db: Session, ledger_id: int, actual_cost: float) -> CostLedger:
    entry = db.get(CostLedger, ledger_id)
    if entry is None:
        raise ValueError(f"cost_ledger row {ledger_id} not found")
    entry.actual_cost = actual_cost
    db.commit()
    db.refresh(entry)
    return entry


def sum_cost_since(db: Session, since: datetime, *, use_estimated_fallback: bool = True) -> float:
    """Sum spend since a timestamp, preferring actual_cost and falling back to estimated_cost."""
    rows = db.scalars(select(CostLedger).where(CostLedger.occurred_at >= since)).all()
    total = 0.0
    for row in rows:
        if row.actual_cost is not None:
            total += float(row.actual_cost)
        elif use_estimated_fallback:
            total += float(row.estimated_cost)
    return total


def count_calls_since(db: Session, since: datetime, *, category: str | None = None) -> int:
    stmt = select(CostLedger).where(CostLedger.occurred_at >= since)
    if category is not None:
        stmt = stmt.where(CostLedger.category == category)
    return len(db.scalars(stmt).all())


# --- Option contract analyses (M2-B, Annex C-2) ---

def record_option_contract_analysis(
    db: Session,
    *,
    symbol: str,
    option_type: str,
    strike: float,
    expiry,
    contract_price_at_analysis: float,
    underlying_price: float,
    implied_volatility: float,
    analysis_json: dict,
    extraction_confidence: str,
    vision_provider: str,
    total_cost_usd: float,
    status: str,
) -> OptionContractAnalysisRecord:
    record = OptionContractAnalysisRecord(
        symbol=symbol,
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        contract_price_at_analysis=contract_price_at_analysis,
        underlying_price=underlying_price,
        implied_volatility=implied_volatility,
        analysis_json=analysis_json,
        extraction_confidence=extraction_confidence,
        vision_provider=vision_provider,
        total_cost_usd=total_cost_usd,
        status=status,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


# --- Stock analyses + audit targets (SRS 7.1, 4.1 steps 7/10, 15.2) ---

def create_analysis(
    db: Session,
    *,
    symbol: str,
    market_open: bool,
    data_provider: str,
    deterministic_json: dict,
    scores: dict,
    regime: str,
    status: str,
    kind: str = "single",
) -> Analysis:
    record = Analysis(
        symbol=symbol,
        market_open=market_open,
        data_provider=data_provider,
        deterministic_json=deterministic_json,
        scores=scores,
        regime=regime,
        status=status,
        kind=kind,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def update_analysis_report(
    db: Session,
    analysis_id: uuid.UUID,
    *,
    report_text_ar: str | None = None,
    report_text_en: str | None = None,
    llm_provider: str | None = None,
    llm_input_tokens: int | None = None,
    llm_output_tokens: int | None = None,
    total_cost_usd: float | None = None,
    status: str | None = None,
) -> Analysis:
    record = db.get(Analysis, analysis_id)
    if record is None:
        raise ValueError(f"analysis {analysis_id} not found")
    if report_text_ar is not None:
        record.report_text_ar = report_text_ar
    if report_text_en is not None:
        record.report_text_en = report_text_en
    if llm_provider is not None:
        record.llm_provider = llm_provider
    if llm_input_tokens is not None:
        record.llm_input_tokens = llm_input_tokens
    if llm_output_tokens is not None:
        record.llm_output_tokens = llm_output_tokens
    if total_cost_usd is not None:
        record.total_cost_usd = total_cost_usd
    if status is not None:
        record.status = status
    db.commit()
    db.refresh(record)
    return record


def get_analysis(db: Session, analysis_id: uuid.UUID) -> Analysis | None:
    return db.get(Analysis, analysis_id)


def get_analysis_history(db: Session, symbol: str, limit: int = 20) -> list[Analysis]:
    stmt = (
        select(Analysis)
        .where(Analysis.symbol == symbol)
        .order_by(desc(Analysis.requested_at))
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def create_audit_targets(
    db: Session,
    analysis_id: uuid.UUID,
    symbol: str,
    price_at_analysis: float,
    levels: Levels,
    primary_scenario: str = "neutral",
) -> list[AuditTarget]:
    """Extract verifiable levels into `audit_targets` at analysis time (SRS 15.2 step 1) —
    the scheduled self-audit job (M3) reads these later; nothing here runs that job.
    `primary_scenario` should come from the regime's directional bias (bullish/bearish/
    neutral), tagging which scenario these levels belong to for this analysis."""
    targets: list[AuditTarget] = []
    for support in levels.supports:
        targets.append(
            AuditTarget(
                analysis_id=analysis_id, symbol=symbol, price_at_analysis=price_at_analysis,
                level_type="support", level_value=support.price, scenario=primary_scenario,
            )
        )
    for resistance in levels.resistances:
        targets.append(
            AuditTarget(
                analysis_id=analysis_id, symbol=symbol, price_at_analysis=price_at_analysis,
                level_type="resistance", level_value=resistance.price, scenario=primary_scenario,
            )
        )
    if levels.invalidation is not None:
        targets.append(
            AuditTarget(
                analysis_id=analysis_id, symbol=symbol, price_at_analysis=price_at_analysis,
                level_type="invalidation", level_value=levels.invalidation, scenario=primary_scenario,
            )
        )
    if not targets:
        return []
    db.add_all(targets)
    db.commit()
    for target in targets:
        db.refresh(target)
    return targets


def create_snipe_audit_targets(
    db: Session,
    analysis_id: uuid.UUID,
    symbol: str,
    price_at_analysis: float,
    zone1_price: float | None,
    zone2_price: float | None,
    invalidation_price: float,
) -> list[AuditTarget]:
    """Extract the two target zones + invalidation for a Snipe scanner card (new feature,
    not in SRS) — mirrors `create_audit_targets` but for the fixed zone1/zone2/invalidation
    shape (`level_type` = "target_zone_1"/"target_zone_2"/"invalidation") the Snipe self-
    audit evaluator (`audit/self_audit.py::evaluate_snipe_targets`) reads. Both zones are
    resistances above price by construction, so the scenario is always "bullish"."""
    targets: list[AuditTarget] = []
    if zone1_price is not None:
        targets.append(
            AuditTarget(
                analysis_id=analysis_id, symbol=symbol, price_at_analysis=price_at_analysis,
                level_type="target_zone_1", level_value=zone1_price, scenario="bullish",
            )
        )
    if zone2_price is not None:
        targets.append(
            AuditTarget(
                analysis_id=analysis_id, symbol=symbol, price_at_analysis=price_at_analysis,
                level_type="target_zone_2", level_value=zone2_price, scenario="bullish",
            )
        )
    targets.append(
        AuditTarget(
            analysis_id=analysis_id, symbol=symbol, price_at_analysis=price_at_analysis,
            level_type="invalidation", level_value=invalidation_price, scenario="bullish",
        )
    )
    db.add_all(targets)
    db.commit()
    for target in targets:
        db.refresh(target)
    return targets


# --- Self-audit (SRS 15, M3) ---

def get_analyses_pending_audit(
    db: Session, horizon_days: int, requested_before: datetime, kind: str | None = None
) -> list[Analysis]:
    """Analyses old enough for this horizon (SRS 15.2 step 2) that haven't been audited
    at this specific horizon yet — a completed analysis gets audited once per horizon
    (5, then separately 10, then separately 20 for "single"; 1 and 5 for "snipe" cards —
    see `audit/self_audit.py::HORIZONS_BY_KIND`). `kind=None` matches any kind."""
    already_audited = select(AuditResult.analysis_id).where(AuditResult.horizon_days == horizon_days)
    stmt = (
        select(Analysis)
        .where(Analysis.status == "completed")
        .where(Analysis.requested_at <= requested_before)
        .where(Analysis.id.not_in(already_audited))
        .order_by(Analysis.requested_at)
    )
    if kind is not None:
        stmt = stmt.where(Analysis.kind == kind)
    return list(db.scalars(stmt).all())


def get_recent_snipe_audit_results(db: Session, horizon_days: int = 5, limit: int = 100) -> list[AuditResult]:
    """Most recent audited Snipe cards at a given horizon (default 5 sessions — the more
    decisive checkpoint), for the "دقة الماسح التاريخية" accuracy panel."""
    stmt = (
        select(AuditResult)
        .join(Analysis, AuditResult.analysis_id == Analysis.id)
        .where(Analysis.kind == "snipe")
        .where(AuditResult.horizon_days == horizon_days)
        .order_by(desc(AuditResult.checked_at))
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


def get_audit_targets_for_analysis(db: Session, analysis_id: uuid.UUID) -> list[AuditTarget]:
    stmt = select(AuditTarget).where(AuditTarget.analysis_id == analysis_id)
    return list(db.scalars(stmt).all())


def create_audit_result(
    db: Session,
    *,
    analysis_id: uuid.UUID,
    horizon_days: int,
    price_at_check: float,
    max_high_since: float,
    min_low_since: float,
    scenario_realized: str | None,
    levels_touched: dict | None,
    outcome_score: float | None,
    notes: str | None = None,
) -> AuditResult:
    record = AuditResult(
        analysis_id=analysis_id,
        horizon_days=horizon_days,
        price_at_check=price_at_check,
        max_high_since=max_high_since,
        min_low_since=min_low_since,
        scenario_realized=scenario_realized,
        levels_touched=levels_touched,
        outcome_score=outcome_score,
        notes=notes,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def upsert_accuracy_stats(db: Session, regime: str, horizon_days: int, outcome_score: float) -> AccuracyStats:
    """Incrementally roll the new outcome into the (regime, horizon) running average (SRS 15.2 step 4)."""
    stats = db.get(AccuracyStats, (regime, horizon_days))
    if stats is None:
        stats = AccuracyStats(
            regime=regime, horizon_days=horizon_days, total_audited=1, avg_outcome=outcome_score,
            last_updated=datetime.now(timezone.utc),
        )
        db.add(stats)
    else:
        new_total = stats.total_audited + 1
        prior_avg = float(stats.avg_outcome) if stats.avg_outcome is not None else 0.0
        stats.avg_outcome = (prior_avg * stats.total_audited + outcome_score) / new_total
        stats.total_audited = new_total
        stats.last_updated = datetime.now(timezone.utc)
    db.commit()
    db.refresh(stats)
    return stats


def get_accuracy_summary(db: Session) -> list[AccuracyStats]:
    stmt = select(AccuracyStats).order_by(AccuracyStats.regime, AccuracyStats.horizon_days)
    return list(db.scalars(stmt).all())


def get_audit_results_for_symbol(db: Session, symbol: str, limit: int = 50) -> list[tuple[AuditResult, str]]:
    """Returns (AuditResult, symbol) pairs, newest first, joined through Analysis."""
    stmt = (
        select(AuditResult, Analysis.symbol)
        .join(Analysis, AuditResult.analysis_id == Analysis.id)
        .where(Analysis.symbol == symbol)
        .order_by(desc(AuditResult.checked_at))
        .limit(limit)
    )
    return [(row[0], row[1]) for row in db.execute(stmt).all()]


# --- Option watchlist ("مراقب حالة قرائي", SRS-external, owner request 2026-07-19) ---

def create_watchlist_item(
    db: Session,
    *,
    underlying_symbol: str,
    option_type: str,
    strike: float,
    expiry,
    reference_price: float,
    alert_threshold_pct: float,
    invalidation_price: float | None,
) -> OptionWatchlistItem:
    record = OptionWatchlistItem(
        underlying_symbol=underlying_symbol,
        option_type=option_type,
        strike=strike,
        expiry=expiry,
        reference_price=reference_price,
        alert_threshold_pct=alert_threshold_pct,
        invalidation_price=invalidation_price,
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def get_watchlist_item(db: Session, item_id: uuid.UUID) -> OptionWatchlistItem | None:
    return db.get(OptionWatchlistItem, item_id)


def get_active_watchlist_items(db: Session) -> list[OptionWatchlistItem]:
    stmt = (
        select(OptionWatchlistItem)
        .where(OptionWatchlistItem.is_active.is_(True))
        .order_by(desc(OptionWatchlistItem.added_at))
    )
    return list(db.scalars(stmt).all())


def update_watchlist_status(
    db: Session,
    item_id: uuid.UUID,
    *,
    checked_at: datetime,
    price: float,
    status_tier: int,
    status_code: str,
    message: str,
) -> OptionWatchlistItem:
    record = db.get(OptionWatchlistItem, item_id)
    if record is None:
        raise ValueError(f"watchlist item {item_id} not found")
    record.last_checked_at = checked_at
    record.last_price = price
    record.last_status_tier = status_tier
    record.last_status_code = status_code
    record.last_message = message
    db.commit()
    db.refresh(record)
    return record


def deactivate_watchlist_item(db: Session, item_id: uuid.UUID) -> OptionWatchlistItem:
    record = db.get(OptionWatchlistItem, item_id)
    if record is None:
        raise ValueError(f"watchlist item {item_id} not found")
    record.is_active = False
    db.commit()
    db.refresh(record)
    return record


def create_watchlist_event(
    db: Session,
    watchlist_id: uuid.UUID,
    *,
    status_tier: int,
    status_code: str,
    price: float,
    change_pct: float,
    message: str,
) -> OptionWatchlistEvent:
    event = OptionWatchlistEvent(
        watchlist_id=watchlist_id, status_tier=status_tier, status_code=status_code,
        price=price, change_pct=change_pct, message=message,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return event


def get_watchlist_events(db: Session, watchlist_id: uuid.UUID, limit: int = 50) -> list[OptionWatchlistEvent]:
    stmt = (
        select(OptionWatchlistEvent)
        .where(OptionWatchlistEvent.watchlist_id == watchlist_id)
        .order_by(desc(OptionWatchlistEvent.occurred_at))
        .limit(limit)
    )
    return list(db.scalars(stmt).all())


# --- Cache ---

def cache_get(db: Session, key: str) -> dict | None:
    entry = db.get(CacheEntry, key)
    if entry is None:
        return None
    now = datetime.now(timezone.utc)
    expires_at = entry.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        return None
    return entry.value


def cache_set(db: Session, key: str, value: dict, expires_at: datetime) -> CacheEntry:
    entry = db.get(CacheEntry, key)
    if entry is None:
        entry = CacheEntry(key=key, value=value, expires_at=expires_at)
        db.add(entry)
    else:
        entry.value = value
        entry.expires_at = expires_at
    db.commit()
    db.refresh(entry)
    return entry


def cache_delete(db: Session, key: str) -> None:
    db.execute(delete(CacheEntry).where(CacheEntry.key == key))
    db.commit()


def cache_purge_expired(db: Session) -> int:
    now = datetime.now(timezone.utc)
    result = db.execute(delete(CacheEntry).where(CacheEntry.expires_at <= now))
    db.commit()
    return result.rowcount or 0
