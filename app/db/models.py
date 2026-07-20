"""SQLAlchemy models — full schema per SRS section 7.1 (+ cache table per 17.2).

Portable across SQLite (local dev) and PostgreSQL (production, SRS AD-5):
- UUID primary keys use SQLAlchemy 2.0's backend-agnostic `Uuid` type.
- JSON columns use JSONB on Postgres and JSON on SQLite via `.with_variant`.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base

JSONVariant = JSON().with_variant(JSONB(), "postgresql")


class Analysis(Base):
    """القلب: تحليل واحد كامل لرمز سهم. SRS 7.1."""

    __tablename__ = "analyses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    market_open: Mapped[bool] = mapped_column(Boolean, nullable=False)
    data_provider: Mapped[str] = mapped_column(String(30), nullable=False)
    llm_provider: Mapped[str | None] = mapped_column(String(30), nullable=True)

    deterministic_json: Mapped[dict] = mapped_column(JSONVariant, nullable=False)
    scores: Mapped[dict] = mapped_column(JSONVariant, nullable=False)
    regime: Mapped[str] = mapped_column(String(20), nullable=False)

    report_text_ar: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_text_en: Mapped[str | None] = mapped_column(Text, nullable=True)

    llm_input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    llm_output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    total_cost_usd: Mapped[float] = mapped_column(Numeric(8, 5), nullable=False, default=0)

    from_cache: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cache_source_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("analyses.id"), nullable=True
    )

    status: Mapped[str] = mapped_column(String(15), nullable=False)  # completed/llm_failed/data_failed

    # "single" = full single-symbol analysis (SRS 4.1 flow); "snipe" = lightweight
    # deterministic-only card from the Snipe scanner (no LLM, new feature not in SRS).
    # Self-audit uses this to pick the right horizon set and evaluation rubric.
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="single", server_default="single")

    audit_targets: Mapped[list["AuditTarget"]] = relationship(back_populates="analysis")


class AuditTarget(Base):
    """المستويات القابلة للتدقيق، تُستخرج وقت إنشاء التحليل. SRS 7.1, 15.2."""

    __tablename__ = "audit_targets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    analysis_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("analyses.id"), nullable=False)
    symbol: Mapped[str] = mapped_column(String(10), nullable=False)
    price_at_analysis: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    level_type: Mapped[str] = mapped_column(String(20), nullable=False)  # support/resistance/invalidation/zone_high/zone_low
    level_value: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    scenario: Mapped[str | None] = mapped_column(String(10), nullable=True)  # bullish/bearish/neutral
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    analysis: Mapped["Analysis"] = relationship(back_populates="audit_targets")


class AuditResult(Base):
    """نتائج التدقيق الذاتي. SRS 7.1, 15."""

    __tablename__ = "audit_results"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    analysis_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("analyses.id"), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)  # 5/10/20
    price_at_check: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    max_high_since: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    min_low_since: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    scenario_realized: Mapped[str | None] = mapped_column(String(10), nullable=True)  # bullish/bearish/neutral/mixed
    levels_touched: Mapped[dict | None] = mapped_column(JSONVariant, nullable=True)
    outcome_score: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class CostLedger(Base):
    """سجل كل استدعاء خارجي (بيانات/LLM) بتكلفته التقديرية والفعلية. SRS 7.1, 16."""

    __tablename__ = "cost_ledger"

    # Plain SQLite INTEGER PRIMARY KEY aliases the internal rowid and autoincrements;
    # BIGINT does not get that behavior, so it needs the sqlite variant to work locally.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    category: Mapped[str] = mapped_column(String(15), nullable=False)  # market_data/llm
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    analysis_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("analyses.id"), nullable=True)
    estimated_cost: Mapped[float] = mapped_column(Numeric(8, 5), nullable=False)
    actual_cost: Mapped[float | None] = mapped_column(Numeric(8, 5), nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[int | None] = mapped_column(Integer, nullable=True)


class CostLimits(Base):
    """إعدادات التحكم بالتكلفة — صف واحد فقط (id=1). SRS 7.1, 16.2."""

    __tablename__ = "cost_limits"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    daily_cap_usd: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=1.00)
    monthly_cap_usd: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=20.00)
    kill_switch_on: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AccuracyStats(Base):
    """سجل دقة تراكمي يحدّثه Self-Audit. SRS 7.1, 15.4."""

    __tablename__ = "accuracy_stats"

    regime: Mapped[str] = mapped_column(String(20), primary_key=True)
    horizon_days: Mapped[int] = mapped_column(Integer, primary_key=True)
    total_audited: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_outcome: Mapped[float | None] = mapped_column(Numeric(4, 3), nullable=True)
    last_updated: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OptionContractAnalysisRecord(Base):
    """Persisted M2-B option-contract screenshot analyses (Annex C-2).

    Separate from `analyses` (stock analyses) since the shape is different: no scores/
    regime, but Greeks/IV/expected-move/translated-levels bundled in `analysis_json`.
    """

    __tablename__ = "option_contract_analyses"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    symbol: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    option_type: Mapped[str] = mapped_column(String(4), nullable=False)  # call/put
    strike: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    expiry: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    contract_price_at_analysis: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    underlying_price: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    implied_volatility: Mapped[float] = mapped_column(Numeric(8, 6), nullable=False)
    analysis_json: Mapped[dict] = mapped_column(JSONVariant, nullable=False)
    extraction_confidence: Mapped[str] = mapped_column(String(10), nullable=False)
    vision_provider: Mapped[str] = mapped_column(String(30), nullable=False)
    total_cost_usd: Mapped[float] = mapped_column(Numeric(8, 5), nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(15), nullable=False)  # completed/vision_failed/data_failed


class OptionWatchlistItem(Base):
    """User-added "مراقب حالة قرائي" contract (new feature, not in SRS, owner request
    2026-07-19). Read-only status indicator — no trade execution anywhere near this
    table. One row per watched contract; `last_status_*` cache the most recent computed
    reading so the list endpoint doesn't need to replay history to render.
    """

    __tablename__ = "option_watchlist_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    underlying_symbol: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    option_type: Mapped[str] = mapped_column(String(4), nullable=False, default="call")
    strike: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    expiry: Mapped[Date] = mapped_column(Date, nullable=False)

    reference_price: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    # Adjustable "red tier" trigger (owner request default 5%) — see
    # engines/screener/watchlist_status.py for the full graduated-tier formula.
    alert_threshold_pct: Mapped[float] = mapped_column(Numeric(5, 2), nullable=False, default=5.0)
    invalidation_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)

    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_price: Mapped[float | None] = mapped_column(Numeric(12, 4), nullable=True)
    last_status_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 0=green .. 4=black
    last_status_code: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="1")

    events: Mapped[list["OptionWatchlistEvent"]] = relationship(back_populates="item")


class OptionWatchlistEvent(Base):
    """Every status change for a watched contract, timestamped — lets the user review how
    the reading evolved (owner request 2026-07-19, point 3)."""

    __tablename__ = "option_watchlist_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    watchlist_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("option_watchlist_items.id"), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    status_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    status_code: Mapped[str] = mapped_column(String(10), nullable=False)
    price: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    change_pct: Mapped[float] = mapped_column(Numeric(6, 2), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)

    item: Mapped["OptionWatchlistItem"] = relationship(back_populates="events")


class CacheEntry(Base):
    """جدول الكاش داخل Postgres/SQLite (بديل Redid للتوفير). SRS 17.2."""

    __tablename__ = "cache_entries"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONVariant, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
