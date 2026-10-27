"""fx_price_history 改价流水表：一级分销价每次变更留底（原价/新价/折扣率/标价快照/来源）。

Revision ID: 0017_fx_price_history
Revises: 0016_fx_order_shop
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0017_fx_price_history"
down_revision = "0016_fx_order_shop"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fx_price_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("old_value", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("new_value", sa.String(length=40), nullable=False, server_default=""),
        sa.Column("rate", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("list_price", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("source", sa.String(length=20), nullable=False, server_default="manual"),
        sa.Column("note", sa.String(length=300), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_fx_price_history_product_id", "fx_price_history", ["product_id"])
    op.create_index("ix_fx_price_history_sku", "fx_price_history", ["sku"])
    op.create_index("ix_fx_price_history_created_at", "fx_price_history", ["created_at"])


def downgrade() -> None:
    op.drop_table("fx_price_history")
