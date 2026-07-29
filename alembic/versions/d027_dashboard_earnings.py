"""Add earnings calendar events.

Revision ID: d027_dashboard_earnings
Revises: 82d0f34c12ab
"""
from alembic import op
import sqlalchemy as sa

revision = "d027_dashboard_earnings"
down_revision = "82d0f34c12ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "earnings_calendar_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("symbol", sa.String(length=10), nullable=False),
        sa.Column("company_name", sa.String(length=150), nullable=False),
        sa.Column("announced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timing", sa.String(length=20), nullable=False),
        sa.Column("eps_estimate", sa.Numeric(12, 4), nullable=True),
        sa.Column("revenue_estimate", sa.Numeric(18, 2), nullable=True),
        sa.Column("previous_eps", sa.Numeric(12, 4), nullable=True),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_earnings_calendar_events_symbol", "earnings_calendar_events", ["symbol"])
    op.create_index("ix_earnings_calendar_events_announced_at", "earnings_calendar_events", ["announced_at"])


def downgrade() -> None:
    op.drop_index("ix_earnings_calendar_events_announced_at", table_name="earnings_calendar_events")
    op.drop_index("ix_earnings_calendar_events_symbol", table_name="earnings_calendar_events")
    op.drop_table("earnings_calendar_events")
