"""add analyses.kind column

Revision ID: 25e7c841098b
Revises: aa401616d6ae
Create Date: 2026-07-17 14:00:00.000000

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '25e7c841098b'
down_revision: str | None = 'aa401616d6ae'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        'analyses',
        sa.Column('kind', sa.String(length=10), nullable=False, server_default='single'),
    )


def downgrade() -> None:
    op.drop_column('analyses', 'kind')
