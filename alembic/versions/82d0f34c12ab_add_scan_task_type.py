"""Add task type to distinguish market scans from single-symbol jobs."""

from alembic import op
import sqlalchemy as sa

revision: str = "82d0f34c12ab"
down_revision: str | None = "6c84e91b40aa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stock_scan_runs") as batch:
        batch.add_column(
            sa.Column("task_type", sa.String(length=20), nullable=False, server_default="market_scan")
        )
        batch.create_index("ix_stock_scan_runs_task_type", ["task_type"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("stock_scan_runs") as batch:
        batch.drop_index("ix_stock_scan_runs_task_type")
        batch.drop_column("task_type")
