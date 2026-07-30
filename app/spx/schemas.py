from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class StrikeMode(StrEnum):
    NEAR = "near"
    FAR = "far"


class Direction(StrEnum):
    CALL = "call"
    PUT = "put"
    NONE = "none"


class SPXProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str
    checked_at: datetime
    underlying_available: bool = False
    option_chain_available: bool = False
    opra_available: bool = False
    greeks_available: bool = False
    expirations_available: bool = False
    zero_dte_available: bool = False
    one_dte_available: bool = False
    weekly_expirations_available: bool = False
    underlying_status: str = "unknown"
    options_status: str = "unknown"
    message_ar: str


class SPXQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = "SPX"
    price: float
    bid: float | None = None
    ask: float | None = None
    last_trade: float | None = None
    quote_timestamp: datetime
    trade_timestamp: datetime | None = None
    source: str
    is_realtime: bool


class SPXContract(BaseModel):
    model_config = ConfigDict(extra="ignore")
    symbol: str
    option_type: str
    strike: float
    expiration: datetime
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    volume: int | None = None
    open_interest: int | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    iv: float | None = None
    quote_timestamp: datetime | None = None
    trade_timestamp: datetime | None = None
    feed: str = "opra"
    root_symbol: str = "SPXW"
    settlement_type: str | None = None
    exercise_style: str | None = None


class SyntheticPairResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strike: float
    expiration: str
    settlement_type: str
    exercise_style: str
    call_symbol: str
    put_symbol: str
    call_mid: float
    put_mid: float
    call_spread_pct: float
    put_spread_pct: float
    pair_forward_value: float
    pair_spot_estimate: float | None = None
    lower_bound: float
    upper_bound: float
    quote_age_seconds: int
    quote_time_difference_seconds: float
    open_interest_score: int
    liquidity_score: int
    pair_quality_score: int
    weight: float


class SPXSyntheticValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    synthetic_forward_value: float | None = None
    synthetic_spot_estimate: float | None = None
    lower_bound: float | None = None
    upper_bound: float | None = None
    implied_range_width_points: float | None = None
    pairs_requested: int = 0
    pairs_used: int = 0
    rejected_pairs: int = 0
    outliers_removed: int = 0
    initial_pairs: int = 0
    refined_pairs: int = 0
    iterations: int = 0
    expiration_used: str | None = None
    settlement_type: str | None = None
    calculation_timestamp: datetime
    oldest_quote_age_seconds: int | None = None
    newest_quote_age_seconds: int | None = None
    median_quote_age_seconds: float | None = None
    max_pair_time_diff_seconds: float | None = None
    dispersion_points: float | None = None
    convergence_points: list[float] = Field(default_factory=list)
    confidence_score: int = Field(default=0, ge=0, le=100)
    data_quality_score: int = Field(default=0, ge=0, le=100)
    liquidity_score: int = Field(default=0, ge=0, le=100)
    dispersion_score: int = Field(default=0, ge=0, le=100)
    range_quality_score: int = Field(default=0, ge=0, le=100)
    source: str = "Alpaca OPRA Synthetic"
    provider_status: str
    status_message_ar: str
    rejection_reasons: dict[str, int] = Field(default_factory=dict)
    pairs: list[SyntheticPairResult] = Field(default_factory=list, max_length=15)
    risk_free_rate_used: float | None = None
    dividend_yield_used: float | None = None
    risk_free_rate_source: str | None = None
    dividend_yield_source: str | None = None
    risk_free_rate_updated_at: str | None = None
    dividend_yield_updated_at: str | None = None
    risk_free_rate_age_seconds: int | None = None
    dividend_yield_age_seconds: int | None = None
    spot_estimate_label_ar: str | None = None
    calculation_time_ms: int = 0


class RankedSPXContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    option_type: str
    strike: float
    expiration: str
    dte: int
    moneyness: str
    bid: float
    ask: float
    mid: float
    last: float | None
    spread: float
    spread_pct: float
    volume: int
    open_interest: int
    delta: float
    gamma: float
    theta: float
    vega: float
    iv: float
    break_even: float
    contract_cost: float
    distance_to_strike_pct: float
    entry: float
    premium_stop_conservative: float
    premium_stop_cautious: float
    target_scenarios: list[dict]
    suitability_score: int = Field(ge=0, le=100)
    liquidity_score: int = Field(ge=0, le=100)
    risk_score: int = Field(ge=0, le=100)
    ranking_components: dict[str, int]
    required_spx_move: float
    time_sensitivity: str
    quote_age_seconds: int
    actionable: bool


class SPXHunterResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generated_at: datetime
    status: str
    decision: str
    decision_ar: str
    reason_ar: str
    paper_only: bool = True
    strike_mode: StrikeMode
    capabilities: SPXProviderCapabilities
    market: dict
    quote: dict | None = None
    synthetic: SPXSyntheticValue | None = None
    technical: dict | None = None
    news: list[dict] = Field(default_factory=list)
    news_impact_score: int = 0
    direction: Direction = Direction.NONE
    scenario: dict | None = None
    best_contract: RankedSPXContract | None = None
    ranked_contracts: list[RankedSPXContract] = Field(default_factory=list, max_length=3)
    rejected_contracts: dict[str, int] = Field(default_factory=dict)
    ai_review: dict | None = None
    warnings_ar: list[str] = Field(default_factory=list)
    refresh_required: bool = False
