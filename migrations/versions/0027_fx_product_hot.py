"""0027 商品热销标记。

用户 2026-10-28 要求：商品档案的状态里增加「热销」，勾选商品后可批量标记；
商品档案默认排序把热销商品排在前面；分销下单页只展示热销商品。

`is_hot` 只表达「这个商品当前是否参加分销热销」，与 active（在售/停用）独立：
- 停用商品即使保留热销标记，也不会出现在下单页；
- 取消热销只改 is_hot，不动在售状态、价格、分类等资料。

Revision ID: 0027_fx_product_hot
Revises: 0026_fx_order_entry_fields
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0027_fx_product_hot"
down_revision = "0026_fx_order_entry_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fx_products",
        sa.Column("is_hot", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_fx_products_is_hot", "fx_products", ["is_hot"])


def downgrade() -> None:
    op.drop_index("ix_fx_products_is_hot", table_name="fx_products")
    op.drop_column("fx_products", "is_hot")
