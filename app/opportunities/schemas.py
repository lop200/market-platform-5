from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class OpportunityStatus(StrEnum):
    CONDITIONAL_ENTRY = "conditional_entry"
    WATCH = "watch"
    WAIT_CONFIRMATION = "wait_confirmation"
    EXPIRED = "expired"
    NO_TRADE = "no_trade"


class MarketRegime(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    CHOPPY = "choppy"
    HIGH_RISK = "high_risk"
    LOW_LIQUIDITY = "low_liquidity"
    UNSUITABLE = "unsuitable"


class EntryZone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    from_price: float = Field(alias="from")
    to: float


class Target(BaseModel):
    model_config = ConfigDict(extra="forbid")
    price: float
    label: str
    estimated_horizon: str


class OpportunityResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    symbol: str
    company_name: str
    status: OpportunityStatus
    strategy_id: str
    strategy_name_ar: str
    strategy_name_en: str
    market_regime: MarketRegime
    session: str
    current_price: float
    change_pct: float
    bid: float
    ask: float
    spread_pct: float
    volume: int = 0
    dollar_volume: float = 0
    relative_volume: float = 0
    data_source: str
    data_feed: str | None = None
    is_delayed: bool
    quote_timestamp: datetime
    quote_age_seconds: int
    last_trade: float | None = None
    last_trade_timestamp: datetime | None = None
    price_source: str = "unknown"
    external_price: float | None = None
    external_provider: str | None = None
    external_timestamp: datetime | None = None
    price_divergence_pct: float | None = None
    data_status: str = "unverified"
    entry_zone: EntryZone
    entry_trigger: str
    stop_loss: float
    stop_reason: str
    targets: list[Target]
    risk_reward: float
    valid_for_minutes: int
    expires_at: datetime
    # expires_at is how long the signal stays valid; these are when the trade
    # itself has to be closed. A day trade that is still open at the bell is no
    # longer the trade that was analysed.
    exit_by: datetime | None = None
    exit_by_ar: str = ""
    holding_window_ar: str = ""
    # Chance the first target is reached before that deadline, from the stock's
    # own volatility. Not a forecast of direction — see app/opportunities/probability.py.
    target_probability_pct: int = 0
    probability_basis_ar: str = ""
    # Percentage of the deterministic checklist satisfied by the selected
    # setup. This is confluence, not a historical win rate or profit promise.
    strategy_match_pct: int = Field(default=0, ge=0, le=100)
    strategy_classification_ar: str = ""
    strategy_setup_class_ar: str = ""
    strategy_checks: list[dict] = Field(default_factory=list)
    invalidation_conditions: list[str]
    technical_score: int = Field(ge=0, le=100)
    news_score: int = Field(ge=0, le=100)
    liquidity_score: int = Field(ge=0, le=100)
    overall_score: int = Field(ge=0, le=100)
    confidence_label: str
    reasons_ar: list[str]
    warnings_ar: list[str]
    news_summary_ar: str
    analysis_summary_ar: str
    suggested_shares: int = 0
    position_value_usd: float = 0
    max_loss_sar: float = 0
    capital_used_pct: float = 0
    estimated_profit_sar: list[float] = Field(default_factory=list)
    order_type: str = "limit"
    market_orders_allowed: bool = False
    bracket_required: bool = True
    max_risk_usd: float = 0
    spread_to_target_pct: float = 0
    expected_move_pct: float | None = None
    options: dict | None = None


class QualityDecision(BaseModel):
    accepted: bool
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class NewsItemData(BaseModel):
    headline: str
    source: str
    published_at: datetime
    url: str | None = None
    classification: str = "محايد"
    is_official: bool = False
    risk_flags: list[str] = Field(default_factory=list)


class ScanStartResponse(BaseModel):
    run_id: str
    status: str
    message_ar: str


class RiskSettingsInput(BaseModel):
    capital_sar: float = Field(gt=0, le=1_000_000)
    max_risk_pct: float = Field(gt=0, le=5)
    max_open_positions: int = Field(ge=1, le=20)
    daily_loss_limit_pct: float = Field(gt=0, le=10)
    currency: str = "SAR"
