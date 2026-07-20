"""add access_codes table for the site lock screen

Revision ID: c4f2a91d0e77
Revises: 081ad5be6c5c
Create Date: 2026-07-20 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'c4f2a91d0e77'
down_revision: str | None = '081ad5be6c5c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'access_codes',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('label', sa.String(length=80), nullable=False),
        sa.Column('code_hash', sa.String(length=64), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('label'),
    )


def downgrade() -> None:
    op.drop_table('access_codes')
