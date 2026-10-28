"""商品新增「大类」字段 top_category（与「分类」category 并存，互不覆盖）。

用户 2026-10-28 要求：现有的分类要保留（后续有用），另外加一层更粗的「大类」，
值由用户自己在页面上维护；报表（对账单导出）里要带上大类/分类两列。

Revision ID: 0018_fx_product_top_category
Revises: 0017_fx_price_history
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0018_fx_product_top_category"
down_revision = "0017_fx_price_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fx_products",
        sa.Column("top_category", sa.String(length=100), nullable=False, server_default=""),
    )
    op.create_index("ix_fx_products_top_category", "fx_products", ["top_category"])


def downgrade() -> None:
    op.drop_index("ix_fx_products_top_category", table_name="fx_products")
    op.drop_column("fx_products", "top_category")
