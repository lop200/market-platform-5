"""add scan usage metrics

Revision ID: 6c84e91b40aa
Revises: 21ef0f0ea9fa
Create Date: 2026-07-29 06:10:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "6c84e91b40aa"
down_revision: str | None = "21ef0f0ea9fa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("stock_scan_runs") as batch:
        batch.add_column(sa.Column("api_requests", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("cache_hits", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("openai_calls", sa.Integer(), nullable=False, server_default="0"))
        batch.add_column(sa.Column("openai_cost_usd", sa.Numeric(8, 5), nullable=False, server_default="0"))
        batch.add_column(sa.Column("response_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("stock_scan_runs") as batch:
        batch.drop_column("response_ms")
        batch.drop_column("openai_cost_usd")
        batch.drop_column("openai_calls")
        batch.drop_column("cache_hits")
        batch.drop_column("api_requests")
