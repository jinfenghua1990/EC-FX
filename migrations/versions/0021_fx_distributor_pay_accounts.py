"""分销商新增「付款账户」pay_accounts（一个客户可以有多个付款公司/账户）。

用户 2026-10-28 场景：同一个客户，钱可能从不同公司账户打过来（比如自己付、
关联公司代付、个人卡付）。客户档案只留一行，把各个付款方都记在付款账户里，
对账时按任一付款方都能找到这个客户。
配合「分销商合并」功能：把拆开的几行并成一个客户时，各行的付款方自动并入
目标客户的付款账户列表。

Revision ID: 0021_fx_distributor_pay_accounts
Revises: 0020_fx_distributor_brands
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0021_fx_distributor_pay_accounts"
down_revision = "0020_fx_distributor_brands"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fx_distributors",
        sa.Column("pay_accounts", sa.String(length=800), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("fx_distributors", "pay_accounts")
