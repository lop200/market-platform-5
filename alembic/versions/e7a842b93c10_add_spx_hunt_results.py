"""add SPX hunt results

Revision ID: e7a842b93c10
Revises: d3c91b7e4a20
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "e7a842b93c10"
down_revision: str | None = "d3c91b7e4a20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "spx_hunt_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("market_state", sa.String(30), nullable=False),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("decision", sa.String(30), nullable=False),
        sa.Column("strike_mode", sa.String(10), server_default="near", nullable=False),
        sa.Column("contract_symbol", sa.String(40), nullable=True),
        sa.Column("strike", sa.Numeric(12, 2), nullable=True),
        sa.Column("dte", sa.Integer(), nullable=True),
        sa.Column("entry", sa.Numeric(12, 4), nullable=True),
        sa.Column("stop", sa.Numeric(12, 4), nullable=True),
        sa.Column("targets_json", sa.JSON(), nullable=False),
        sa.Column("confidence_score", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("result_5m", sa.JSON(), nullable=True),
        sa.Column("result_15m", sa.JSON(), nullable=True),
        sa.Column("result_30m", sa.JSON(), nullable=True),
        sa.Column("session_result", sa.JSON(), nullable=True),
        sa.Column("max_profit", sa.Numeric(12, 4), nullable=True),
        sa.Column("max_loss", sa.Numeric(12, 4), nullable=True),
        sa.Column("trigger_hit", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("stop_hit", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("escape_triggered", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_spx_hunt_results_analyzed_at"), "spx_hunt_results", ["analyzed_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_spx_hunt_results_analyzed_at"), table_name="spx_hunt_results")
    op.drop_table("spx_hunt_results")
