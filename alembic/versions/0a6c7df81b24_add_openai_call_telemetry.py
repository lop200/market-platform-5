"""add central OpenAI call telemetry

Revision ID: 0a6c7df81b24
Revises: f4b1c9e72a30
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0a6c7df81b24"
down_revision: str | None = "f4b1c9e72a30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "openai_call_logs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.String(length=80), nullable=False),
        sa.Column("operation", sa.String(length=60), nullable=False),
        sa.Column("model_name", sa.String(length=80), nullable=False),
        sa.Column("symbol", sa.String(length=255), nullable=True),
        sa.Column("symbols_json", sa.JSON(), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cached_tokens", sa.Integer(), nullable=False),
        sa.Column("total_tokens", sa.Integer(), nullable=False),
        sa.Column("estimated_cost_usd", sa.Numeric(10, 6), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=160), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_openai_call_logs_operation", "openai_call_logs", ["operation"])
    op.create_index("ix_openai_call_logs_symbol", "openai_call_logs", ["symbol"])
    op.create_index("ix_openai_call_logs_run_id", "openai_call_logs", ["run_id"])
    op.create_index("ix_openai_call_logs_status", "openai_call_logs", ["status"])
    op.create_index("ix_openai_call_logs_created_at", "openai_call_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_openai_call_logs_created_at", table_name="openai_call_logs")
    op.drop_index("ix_openai_call_logs_status", table_name="openai_call_logs")
    op.drop_index("ix_openai_call_logs_run_id", table_name="openai_call_logs")
    op.drop_index("ix_openai_call_logs_operation", table_name="openai_call_logs")
    op.drop_index("ix_openai_call_logs_symbol", table_name="openai_call_logs")
    op.drop_table("openai_call_logs")
