"""add trusted news sources

Revision ID: d3c91b7e4a20
Revises: 82d0f34c12ab
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d3c91b7e4a20"
down_revision: str | None = "82d0f34c12ab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "trusted_news_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_type", sa.String(length=30), nullable=False),
        sa.Column("source_name", sa.String(length=120), nullable=False),
        sa.Column("account_id", sa.String(length=100), nullable=True),
        sa.Column("username", sa.String(length=100), nullable=True),
        sa.Column("domain", sa.String(length=180), nullable=True),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("reliability_score", sa.SmallInteger(), nullable=False, server_default="50"),
        sa.Column("is_official", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_trusted_news_sources_source_type"), "trusted_news_sources", ["source_type"], unique=False)
    op.create_index(op.f("ix_trusted_news_sources_username"), "trusted_news_sources", ["username"], unique=False)
    op.create_index(op.f("ix_trusted_news_sources_domain"), "trusted_news_sources", ["domain"], unique=False)
    op.create_index(op.f("ix_trusted_news_sources_category"), "trusted_news_sources", ["category"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_trusted_news_sources_category"), table_name="trusted_news_sources")
    op.drop_index(op.f("ix_trusted_news_sources_domain"), table_name="trusted_news_sources")
    op.drop_index(op.f("ix_trusted_news_sources_username"), table_name="trusted_news_sources")
    op.drop_index(op.f("ix_trusted_news_sources_source_type"), table_name="trusted_news_sources")
    op.drop_table("trusted_news_sources")
