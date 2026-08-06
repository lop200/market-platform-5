"""add temporal trade intents and trading audit

Revision ID: d91f2c7a4b10
Revises: b7d2e8a91c40
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d91f2c7a4b10"
down_revision: str | None = "b7d2e8a91c40"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trade_intents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(80), nullable=False),
        sa.Column("opportunity_id", sa.Uuid(), nullable=True),
        sa.Column("analysis_run_id", sa.Uuid(), nullable=True),
        sa.Column("instrument_type", sa.String(10), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("underlying_symbol", sa.String(10), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("take_profit", sa.Numeric(14, 4), nullable=False),
        sa.Column("stop_loss", sa.Numeric(14, 4), nullable=False),
        sa.Column("time_in_force", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("entry_valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_holding_period", sa.String(20), nullable=False),
        sa.Column("expected_exit_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("force_exit_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_session", sa.String(30), nullable=False),
        sa.Column("signal_age_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("reason_ar", sa.String(300), nullable=False),
        sa.Column("cancellation_condition_ar", sa.String(500), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["analysis_run_id"], ["stock_scan_runs.id"]),
        sa.ForeignKeyConstraint(["opportunity_id"], ["stock_opportunities.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
    )
    for column in ("idempotency_key", "opportunity_id", "analysis_run_id", "symbol", "underlying_symbol", "entry_valid_until", "status"):
        op.create_index(f"ix_trade_intents_{column}", "trade_intents", [column], unique=column == "idempotency_key")
    op.create_table(
        "trading_audit_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(30), nullable=False),
        sa.Column("intent_id", sa.Uuid(), nullable=True),
        sa.Column("idempotency_key", sa.String(80), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["intent_id"], ["trade_intents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("event_type", "intent_id", "idempotency_key", "created_at"):
        op.create_index(f"ix_trading_audit_logs_{column}", "trading_audit_logs", [column])


def downgrade() -> None:
    for column in ("created_at", "idempotency_key", "intent_id", "event_type"):
        op.drop_index(f"ix_trading_audit_logs_{column}", table_name="trading_audit_logs")
    op.drop_table("trading_audit_logs")
    for column in ("status", "entry_valid_until", "underlying_symbol", "symbol", "analysis_run_id", "opportunity_id", "idempotency_key"):
        op.drop_index(f"ix_trade_intents_{column}", table_name="trade_intents")
    op.drop_table("trade_intents")
