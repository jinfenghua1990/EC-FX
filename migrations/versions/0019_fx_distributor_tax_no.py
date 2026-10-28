"""分销商新增「税号」字段 tax_no。

用户 2026-10-28 要求：新建分销商时想把税号当识别码用；同时「区域」字段页面不再显示
（旧数据保留，不删列），左侧菜单里未实现的「分销商等级」「分销协议」两个占位项去掉。

Revision ID: 0019_fx_distributor_tax_no
Revises: 0018_fx_product_top_category
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0019_fx_distributor_tax_no"
down_revision = "0018_fx_product_top_category"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fx_distributors",
        sa.Column("tax_no", sa.String(length=80), nullable=False, server_default=""),
    )
    op.create_index("ix_fx_distributors_tax_no", "fx_distributors", ["tax_no"])


def downgrade() -> None:
    op.drop_index("ix_fx_distributors_tax_no", table_name="fx_distributors")
    op.drop_column("fx_distributors", "tax_no")
