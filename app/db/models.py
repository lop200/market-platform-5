"""Database schema for stock opportunity discovery and its factual audit trail."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, Numeric, SmallInteger, String, Text, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base

JSONVariant = JSON().with_variant(JSONB(), "postgresql")


class Analysis(Base):
    __tablename__ = "analyses"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(10), index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    market_open: Mapped[bool] = mapped_column(Boolean)
    data_provider: Mapped[str] = mapped_column(String(30))
    llm_provider: Mapped[str | None] = mapped_column(String(30))
    deterministic_json: Mapped[dict] = mapped_column(JSONVariant)
    scores: Mapped[dict] = mapped_column(JSONVariant)
    regime: Mapped[str] = mapped_column(String(20))
    report_text_ar: Mapped[str | None] = mapped_column(Text)
    report_text_en: Mapped[str | None] = mapped_column(Text)
    llm_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Numeric(8, 5), default=0)
    from_cache: Mapped[bool] = mapped_column(Boolean, default=False)
    cache_source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("analyses.id"))
    status: Mapped[str] = mapped_column(String(15))
    kind: Mapped[str] = mapped_column(String(10), default="single")


class CostLedger(Base):
    __tablename__ = "cost_ledger"
    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    category: Mapped[str] = mapped_column(String(15))
    provider: Mapped[str] = mapped_column(String(30))
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("analyses.id"))
    estimated_cost: Mapped[float] = mapped_column(Numeric(8, 5))
    actual_cost: Mapped[float | None] = mapped_column(Numeric(8, 5))
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)


class CostLimits(Base):
    __tablename__ = "cost_limits"
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    daily_cap_usd: Mapped[float] = mapped_column(Numeric(8, 2), default=1)
    monthly_cap_usd: Mapped[float] = mapped_column(Numeric(8, 2), default=20)
    kill_switch_on: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AccessCode(Base):
    __tablename__ = "access_codes"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(String(80), unique=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = ({"sqlite_autoincrement": False},)


class CacheEntry(Base):
    __tablename__ = "cache_entries"
    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONVariant)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class StockScanRun(Base):
    __tablename__ = "stock_scan_runs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    task_type: Mapped[str] = mapped_column(String(20), default="market_scan", index=True)
    progress_pct: Mapped[int] = mapped_column(Integer, default=0)
    symbols_total: Mapped[int] = mapped_column(Integer, default=0)
    symbols_scanned: Mapped[int] = mapped_column(Integer, default=0)
    symbols_excluded: Mapped[int] = mapped_column(Integer, default=0)
    provider: Mapped[str | None] = mapped_column(String(30))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    api_requests: Mapped[int] = mapped_column(Integer, default=0)
    cache_hits: Mapped[int] = mapped_column(Integer, default=0)
    openai_calls: Mapped[int] = mapped_column(Integer, default=0)
    openai_cost_usd: Mapped[float] = mapped_column(Numeric(8, 5), default=0)
    response_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StockCandidate(Base):
    __tablename__ = "stock_candidates"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scan_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stock_scan_runs.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(10), index=True)
    accepted: Mapped[bool] = mapped_column(Boolean)
    numeric_score: Mapped[float] = mapped_column(Numeric(5, 2), default=0)
    exclusion_reasons: Mapped[list] = mapped_column(JSONVariant, default=list)
    snapshot_json: Mapped[dict] = mapped_column(JSONVariant, default=dict)


class StockOpportunity(Base):
    __tablename__ = "stock_opportunities"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    scan_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("stock_scan_runs.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(10), index=True)
    company_name: Mapped[str] = mapped_column(String(150), default="")
    status: Mapped[str] = mapped_column(String(30), index=True)
    strategy_id: Mapped[str] = mapped_column(String(40), index=True)
    market_regime: Mapped[str] = mapped_column(String(30))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    quote_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    price_at_analysis: Mapped[float] = mapped_column(Numeric(12, 4))
    entry_from: Mapped[float] = mapped_column(Numeric(12, 4))
    entry_to: Mapped[float] = mapped_column(Numeric(12, 4))
    stop_loss: Mapped[float] = mapped_column(Numeric(12, 4))
    risk_reward: Mapped[float] = mapped_column(Numeric(6, 2))
    overall_score: Mapped[int] = mapped_column(Integer)
    result_json: Mapped[dict] = mapped_column(JSONVariant)
    data_fingerprint: Mapped[str] = mapped_column(String(64), index=True)


class OpportunityTarget(Base):
    __tablename__ = "opportunity_targets"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stock_opportunities.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Numeric(12, 4))
    label: Mapped[str] = mapped_column(String(40))
    reached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OpportunityEvent(Base):
    __tablename__ = "opportunity_events"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stock_opportunities.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(30))
    price: Mapped[float | None] = mapped_column(Numeric(12, 4))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    details_json: Mapped[dict] = mapped_column(JSONVariant, default=dict)


class OpportunityAudit(Base):
    __tablename__ = "opportunity_audits"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("stock_opportunities.id"), index=True)
    horizon: Mapped[str] = mapped_column(String(20))
    entry_triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    highest_price: Mapped[float | None] = mapped_column(Numeric(12, 4))
    lowest_price: Mapped[float | None] = mapped_column(Numeric(12, 4))
    target_1_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    target_2_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str | None] = mapped_column(String(30))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NewsItem(Base):
    __tablename__ = "news_items"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(10), index=True)
    headline: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(80))
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    url: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str] = mapped_column(String(30))
    is_official: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_flags: Mapped[list] = mapped_column(JSONVariant, default=list)


class TrustedNewsSource(Base):
    __tablename__ = "trusted_news_sources"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_type: Mapped[str] = mapped_column(String(30), index=True)
    source_name: Mapped[str] = mapped_column(String(120))
    account_id: Mapped[str | None] = mapped_column(String(100))
    username: Mapped[str | None] = mapped_column(String(100), index=True)
    domain: Mapped[str | None] = mapped_column(String(180), index=True)
    category: Mapped[str] = mapped_column(String(30), index=True)
    reliability_score: Mapped[int] = mapped_column(SmallInteger, default=50)
    is_official: Mapped[bool] = mapped_column(Boolean, default=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str | None] = mapped_column(Text)


class MarketRegimeRecord(Base):
    __tablename__ = "market_regimes"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    regime: Mapped[str] = mapped_column(String(30), index=True)
    session: Mapped[str] = mapped_column(String(30))
    inputs_json: Mapped[dict] = mapped_column(JSONVariant)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProviderHealth(Base):
    __tablename__ = "provider_health"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    service_type: Mapped[str] = mapped_column(String(30))
    healthy: Mapped[bool] = mapped_column(Boolean)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserRiskSettings(Base):
    __tablename__ = "user_risk_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    capital_sar: Mapped[float] = mapped_column(Numeric(12, 2), default=750)
    max_risk_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=1)
    max_open_positions: Mapped[int] = mapped_column(Integer, default=2)
    daily_loss_limit_pct: Mapped[float] = mapped_column(Numeric(5, 2), default=3)
    currency: Mapped[str] = mapped_column(String(3), default="SAR")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AIAnalysisLog(Base):
    __tablename__ = "ai_analysis_logs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("stock_opportunities.id"))
    symbol: Mapped[str] = mapped_column(String(10), index=True)
    prompt_version: Mapped[str] = mapped_column(String(30))
    model_name: Mapped[str] = mapped_column(String(80))
    data_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(20))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Numeric(8, 5), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class OpenAICallLog(Base):
    """One row per actual SDK call; never stores prompts, payloads, or secrets."""

    __tablename__ = "openai_call_logs"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    endpoint: Mapped[str] = mapped_column(String(80), default="/v1/responses")
    operation: Mapped[str] = mapped_column(String(60), index=True)
    model_name: Mapped[str] = mapped_column(String(80))
    symbol: Mapped[str | None] = mapped_column(String(255), index=True)
    symbols_json: Mapped[list] = mapped_column(JSONVariant, default=list)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cached_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class PaperAccount(Base):
    """Single paper-only account. It never mirrors or authorizes a live broker account."""

    __tablename__ = "paper_accounts"
    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    cash: Mapped[float] = mapped_column(Numeric(14, 2), default=100_000)
    buying_power: Mapped[float] = mapped_column(Numeric(14, 2), default=100_000)
    realized_pnl_today: Mapped[float] = mapped_column(Numeric(14, 2), default=0)
    emergency_stop: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TradingPosition(Base):
    __tablename__ = "trading_positions"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    instrument_type: Mapped[str] = mapped_column(String(10), index=True)
    symbol: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    underlying_symbol: Mapped[str] = mapped_column(String(10), index=True)
    quantity: Mapped[int] = mapped_column(Integer)
    avg_price: Mapped[float] = mapped_column(Numeric(14, 4))
    current_price: Mapped[float] = mapped_column(Numeric(14, 4))
    quote_as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    target_price: Mapped[float | None] = mapped_column(Numeric(14, 4))
    stop_price: Mapped[float | None] = mapped_column(Numeric(14, 4))
    profit_protection_trigger_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
    trailing_stop_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
    source: Mapped[str] = mapped_column(String(20), default="paper")
    status: Mapped[str] = mapped_column(String(15), default="open", index=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PaperOrder(Base):
    __tablename__ = "paper_orders"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    client_order_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    parent_order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("paper_orders.id"), index=True)
    oco_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    order_role: Mapped[str] = mapped_column(String(20), default="entry")
    instrument_type: Mapped[str] = mapped_column(String(10))
    symbol: Mapped[str] = mapped_column(String(64), index=True)
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    limit_price: Mapped[float] = mapped_column(Numeric(14, 4))
    take_profit: Mapped[float | None] = mapped_column(Numeric(14, 4))
    stop_loss: Mapped[float | None] = mapped_column(Numeric(14, 4))
    trailing_stop_pct: Mapped[float | None] = mapped_column(Numeric(6, 2))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    reject_reason: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TradingBridgeSnapshot(Base):
    """Normalized broker view only; never stores Sahm passwords, OTPs, or cookies."""

    __tablename__ = "trading_bridge_snapshots"
    adapter: Mapped[str] = mapped_column(String(30), primary_key=True)
    connection_status: Mapped[str] = mapped_column(String(20), default="disconnected")
    account_json: Mapped[dict] = mapped_column(JSONVariant, default=dict)
    positions_json: Mapped[list] = mapped_column(JSONVariant, default=list)
    orders_json: Mapped[list] = mapped_column(JSONVariant, default=list)
    quotes_json: Mapped[list] = mapped_column(JSONVariant, default=list)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SPXHuntResult(Base):
    """Paper-only SPX analysis audit record; never represents an order."""

    __tablename__ = "spx_hunt_results"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    analyzed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    market_state: Mapped[str] = mapped_column(String(30))
    direction: Mapped[str] = mapped_column(String(20))
    decision: Mapped[str] = mapped_column(String(30))
    strike_mode: Mapped[str] = mapped_column(String(10), default="near")
    contract_symbol: Mapped[str | None] = mapped_column(String(40))
    strike: Mapped[float | None] = mapped_column(Numeric(12, 2))
    dte: Mapped[int | None] = mapped_column(Integer)
    entry: Mapped[float | None] = mapped_column(Numeric(12, 4))
    stop: Mapped[float | None] = mapped_column(Numeric(12, 4))
    targets_json: Mapped[list] = mapped_column(JSONVariant, default=list)
    confidence_score: Mapped[int] = mapped_column(SmallInteger, default=0)
    result_5m: Mapped[dict | None] = mapped_column(JSONVariant)
    result_15m: Mapped[dict | None] = mapped_column(JSONVariant)
    result_30m: Mapped[dict | None] = mapped_column(JSONVariant)
    session_result: Mapped[dict | None] = mapped_column(JSONVariant)
    max_profit: Mapped[float | None] = mapped_column(Numeric(12, 4))
    max_loss: Mapped[float | None] = mapped_column(Numeric(12, 4))
    trigger_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    stop_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    escape_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    payload_json: Mapped[dict] = mapped_column(JSONVariant, default=dict)


class SPXSyntheticObservation(Base):
    """A derived OPRA observation, explicitly separate from official index candles."""

    __tablename__ = "spx_synthetic_observations"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    forward_value: Mapped[float] = mapped_column(Numeric(14, 4))
    spot_estimate: Mapped[float | None] = mapped_column(Numeric(14, 4))
    lower_bound: Mapped[float] = mapped_column(Numeric(14, 4))
    upper_bound: Mapped[float] = mapped_column(Numeric(14, 4))
    pairs_used: Mapped[int] = mapped_column(SmallInteger)
    confidence_score: Mapped[int] = mapped_column(SmallInteger)
    data_quality_score: Mapped[int] = mapped_column(SmallInteger)
    expiration: Mapped[str] = mapped_column(String(10))
    settlement_type: Mapped[str] = mapped_column(String(30))
    source: Mapped[str] = mapped_column(String(50), default="Alpaca OPRA Synthetic")
    payload_json: Mapped[dict] = mapped_column(JSONVariant, default=dict)
