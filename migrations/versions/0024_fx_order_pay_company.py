"""0024 订单级「付款公司」字段。

用户 2026-10-28 定口径：
- **分销商 = 客户**，一个客户名下可以有多个公司 → 档案上的「付款账户」就是这个客户
  名下的公司集合，多个是正常的，不是脏数据；
- **付款跟订单走**：一个订单只有一个付款公司，一个订单里不可能出现多个付款公司。

原来的数据模型只把「付款公司」汇总成分销商上的一个列表，订单上不记付款方，
于是「这单到底谁付的钱」只能靠猜（按金额多数派之类），这是错的。
本迁移给 `fx_orders` 加 `pay_company` 列，一单一值。

历史订单的付款方无法从库里反推（当初没存），留空；重跑
`scripts/import_glico_orders.py` 会按原表的「付款公司」列重新灌满。

Revision ID: 0024_fx_order_pay_company
Revises: 0023_fx_distributor_delivery_address
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0024_fx_order_pay_company"
down_revision = "0023_fx_distributor_delivery_address"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fx_orders",
        sa.Column("pay_company", sa.String(length=160), nullable=False, server_default=""),
    )


def downgrade() -> None:
    with op.batch_alter_table("fx_orders") as batch:
        batch.drop_column("pay_company")
