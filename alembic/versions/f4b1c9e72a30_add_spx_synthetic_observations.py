"""add SPX synthetic observations

Revision ID: f4b1c9e72a30
Revises: e7a842b93c10
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "f4b1c9e72a30"
down_revision: str | None = "e7a842b93c10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "spx_synthetic_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("forward_value", sa.Numeric(14, 4), nullable=False),
        sa.Column("spot_estimate", sa.Numeric(14, 4), nullable=True),
        sa.Column("lower_bound", sa.Numeric(14, 4), nullable=False),
        sa.Column("upper_bound", sa.Numeric(14, 4), nullable=False),
        sa.Column("pairs_used", sa.SmallInteger(), nullable=False),
        sa.Column("confidence_score", sa.SmallInteger(), nullable=False),
        sa.Column("data_quality_score", sa.SmallInteger(), nullable=False),
        sa.Column("expiration", sa.String(10), nullable=False),
        sa.Column("settlement_type", sa.String(30), nullable=False),
        sa.Column("source", sa.String(50), server_default="Alpaca OPRA Synthetic", nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_spx_synthetic_observations_observed_at"),
        "spx_synthetic_observations",
        ["observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_spx_synthetic_observations_observed_at"),
        table_name="spx_synthetic_observations",
    )
    op.drop_table("spx_synthetic_observations")
