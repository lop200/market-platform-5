"""add trading room paper tables

Revision ID: b7d2e8a91c40
Revises: 0a6c7df81b24
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b7d2e8a91c40"
down_revision: str | None = "0a6c7df81b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("cash", sa.Numeric(14, 2), nullable=False),
        sa.Column("buying_power", sa.Numeric(14, 2), nullable=False),
        sa.Column("realized_pnl_today", sa.Numeric(14, 2), nullable=False),
        sa.Column("emergency_stop", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "trading_positions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("instrument_type", sa.String(10), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("underlying_symbol", sa.String(10), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("avg_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("current_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("quote_as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column("target_price", sa.Numeric(14, 4), nullable=True),
        sa.Column("stop_price", sa.Numeric(14, 4), nullable=True),
        sa.Column("profit_protection_trigger_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column("trailing_stop_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("status", sa.String(15), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol"),
    )
    op.create_index("ix_trading_positions_instrument_type", "trading_positions", ["instrument_type"])
    op.create_index("ix_trading_positions_symbol", "trading_positions", ["symbol"], unique=True)
    op.create_index("ix_trading_positions_underlying_symbol", "trading_positions", ["underlying_symbol"])
    op.create_index("ix_trading_positions_status", "trading_positions", ["status"])
    op.create_table(
        "paper_orders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_order_id", sa.String(80), nullable=False),
        sa.Column("parent_order_id", sa.Uuid(), nullable=True),
        sa.Column("oco_group_id", sa.Uuid(), nullable=True),
        sa.Column("order_role", sa.String(20), nullable=False),
        sa.Column("instrument_type", sa.String(10), nullable=False),
        sa.Column("symbol", sa.String(64), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("filled_quantity", sa.Integer(), nullable=False),
        sa.Column("limit_price", sa.Numeric(14, 4), nullable=False),
        sa.Column("take_profit", sa.Numeric(14, 4), nullable=True),
        sa.Column("stop_loss", sa.Numeric(14, 4), nullable=True),
        sa.Column("trailing_stop_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reject_reason", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(["parent_order_id"], ["paper_orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_order_id"),
    )
    op.create_index("ix_paper_orders_client_order_id", "paper_orders", ["client_order_id"], unique=True)
    op.create_index("ix_paper_orders_parent_order_id", "paper_orders", ["parent_order_id"])
    op.create_index("ix_paper_orders_oco_group_id", "paper_orders", ["oco_group_id"])
    op.create_index("ix_paper_orders_symbol", "paper_orders", ["symbol"])
    op.create_index("ix_paper_orders_status", "paper_orders", ["status"])
    op.create_table(
        "trading_bridge_snapshots",
        sa.Column("adapter", sa.String(30), nullable=False),
        sa.Column("connection_status", sa.String(20), nullable=False),
        sa.Column("account_json", sa.JSON(), nullable=False),
        sa.Column("positions_json", sa.JSON(), nullable=False),
        sa.Column("orders_json", sa.JSON(), nullable=False),
        sa.Column("quotes_json", sa.JSON(), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("adapter"),
    )
    op.create_index("ix_trading_bridge_snapshots_synced_at", "trading_bridge_snapshots", ["synced_at"])


def downgrade() -> None:
    op.drop_index("ix_trading_bridge_snapshots_synced_at", table_name="trading_bridge_snapshots")
    op.drop_table("trading_bridge_snapshots")
    op.drop_index("ix_paper_orders_status", table_name="paper_orders")
    op.drop_index("ix_paper_orders_symbol", table_name="paper_orders")
    op.drop_index("ix_paper_orders_oco_group_id", table_name="paper_orders")
    op.drop_index("ix_paper_orders_parent_order_id", table_name="paper_orders")
    op.drop_index("ix_paper_orders_client_order_id", table_name="paper_orders")
    op.drop_table("paper_orders")
    op.drop_index("ix_trading_positions_status", table_name="trading_positions")
    op.drop_index("ix_trading_positions_underlying_symbol", table_name="trading_positions")
    op.drop_index("ix_trading_positions_symbol", table_name="trading_positions")
    op.drop_index("ix_trading_positions_instrument_type", table_name="trading_positions")
    op.drop_table("trading_positions")
    op.drop_table("paper_accounts")
