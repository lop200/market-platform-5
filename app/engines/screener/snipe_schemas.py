"""Pydantic output contracts for Today's Snipe."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.engines.deterministic.schemas import ScenarioProbabilities


class LevelProbability(BaseModel):
    price: float
    distance_pct: float
    touch_probability_5d: float  # retained API name; horizon is declared by the parent card


class SnipeStockCard(BaseModel):
    symbol: str
    direction: str
    last_close: float
    daily_change_pct: float
    readiness_score: float
    reasons: list[str]
    invalidation: LevelProbability
    zone1: LevelProbability | None
    zone2: LevelProbability | None
    bar_price_pct: float
    atr_pct_relative: float
    hv_20d: float
    scenario_probabilities: ScenarioProbabilities


class SnipeOptionCard(BaseModel):
    symbol: str
    option_type: str
    strike: float
    expiry: str
    contract_price: float
    bid: float
    ask: float
    spread_pct: float
    premium_total: float
    quality_score: float
    mechanical_quality_score: float
    risk_balance_component: float
    risk_imbalanced: bool
    delta: float
    gamma: float
    theta: float
    vega: float
    daily_theta_decay_pct: float | None
    invalidation: LevelProbability | None
    zone1: LevelProbability | None
    zone2: LevelProbability | None
    reasons: list[str]
    dte_days: int
    is_0dte: bool
    expiry_close_utc: datetime
    hours_to_expiry: float | None
    probability_horizon_days: float
    scenario_probabilities: ScenarioProbabilities


class WatchlistAddRequest(BaseModel):
    underlying_symbol: str
    option_type: str = "call"
    strike: float
    expiry: str
    reference_price: float
    alert_threshold_pct: float = 5.0
    invalidation_price: float | None = None


class WatchlistEventOut(BaseModel):
    occurred_at: datetime
    status_tier: int
    status_code: str
    price: float
    change_pct: float
    message: str


class WatchlistItemOut(BaseModel):
    id: str
    underlying_symbol: str
    option_type: str
    strike: float
    expiry: str
    reference_price: float
    alert_threshold_pct: float
    invalidation_price: float | None
    added_at: datetime
    last_checked_at: datetime | None
    last_price: float | None
    status_tier: int
    status_code: str
    status_emoji: str
    status_mood_emoji: str
    status_label: str
    status_message: str
    change_pct: float
    is_0dte: bool
    hours_to_expiry: float | None
    worsened: bool = False


class SnipeOptionsScanResult(BaseModel):
    generated_at: datetime
    cards: list[SnipeOptionCard]
    skipped_symbols: list[str]
    formulas_ref: str = "/docs/scoring#snipe-options"
    data_source_note: str
    selection_policy: str
    from_cache: bool = False
    cached_minutes_ago: float | None = None


class SnipeAccuracyPanel(BaseModel):
    sample_size: int
    zone1_hit_rate: float | None
    zone2_hit_rate: float | None
    horizon_days: int = 5


class SnipeStockScanResult(BaseModel):
    generated_at: datetime
    universe_size: int
    scanned_count: int
    cards: list[SnipeStockCard]
    accuracy: SnipeAccuracyPanel
    formulas_ref: str = "/docs/scoring#snipe"
    from_cache: bool = False
    cached_minutes_ago: float | None = None
