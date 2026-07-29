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
    data_source: str
    data_feed: str | None = None
    is_delayed: bool
    quote_timestamp: datetime
    quote_age_seconds: int
    entry_zone: EntryZone
    entry_trigger: str
    stop_loss: float
    stop_reason: str
    targets: list[Target]
    risk_reward: float
    valid_for_minutes: int
    expires_at: datetime
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
