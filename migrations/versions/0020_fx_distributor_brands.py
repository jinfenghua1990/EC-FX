"""分销商新增「分销品牌」brands（一个分销商可授权多个品牌）。

用户 2026-10-28 要求：顶部切到某个品牌后，全站只处理这个品牌的数据；
如果某个客户同时分销多个品牌，就在分销商档案里把品牌都勾上，下单时才能看到这些品牌的货。
没勾任何品牌 = 还没录授权，选了具体品牌时它不出现（不默认成全品牌通吃）。

Revision ID: 0020_fx_distributor_brands
Revises: 0019_fx_distributor_tax_no
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0020_fx_distributor_brands"
down_revision = "0019_fx_distributor_tax_no"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fx_distributors",
        sa.Column("brands", sa.String(length=500), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("fx_distributors", "brands")
