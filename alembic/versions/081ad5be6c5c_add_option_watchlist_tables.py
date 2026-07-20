"""add option watchlist tables

Revision ID: 081ad5be6c5c
Revises: 25e7c841098b
Create Date: 2026-07-19 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '081ad5be6c5c'
down_revision: str | None = '25e7c841098b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'option_watchlist_items',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('underlying_symbol', sa.String(length=10), nullable=False),
        sa.Column('option_type', sa.String(length=4), nullable=False),
        sa.Column('strike', sa.Numeric(12, 4), nullable=False),
        sa.Column('expiry', sa.Date(), nullable=False),
        sa.Column('reference_price', sa.Numeric(12, 4), nullable=False),
        sa.Column('alert_threshold_pct', sa.Numeric(5, 2), nullable=False, server_default='5.0'),
        sa.Column('invalidation_price', sa.Numeric(12, 4), nullable=True),
        sa.Column('added_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_price', sa.Numeric(12, 4), nullable=True),
        sa.Column('last_status_tier', sa.Integer(), nullable=True),
        sa.Column('last_status_code', sa.String(length=10), nullable=True),
        sa.Column('last_message', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default='1'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_option_watchlist_items_underlying_symbol'), 'option_watchlist_items', ['underlying_symbol'], unique=False)

    op.create_table(
        'option_watchlist_events',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('watchlist_id', sa.Uuid(), nullable=False),
        sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.Column('status_tier', sa.Integer(), nullable=False),
        sa.Column('status_code', sa.String(length=10), nullable=False),
        sa.Column('price', sa.Numeric(12, 4), nullable=False),
        sa.Column('change_pct', sa.Numeric(6, 2), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(['watchlist_id'], ['option_watchlist_items.id']),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    op.drop_table('option_watchlist_events')
    op.drop_index(op.f('ix_option_watchlist_items_underlying_symbol'), table_name='option_watchlist_items')
    op.drop_table('option_watchlist_items')
