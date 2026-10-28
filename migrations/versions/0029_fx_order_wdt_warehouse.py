"""0029 分销订单的旺店通发货仓库。

用户 2026-10-28 要求：发货仓库不能是自由文本，必须从旺店通仓库档案里选，
而且这个页面只允许「红盒子-格力高磐石仓」（`HHZ-Glico`，排除次品仓）。
推送 `trade_push.php` 时要把仓库编号放进 `warehouse_no`。

`ship_warehouse` 继续保留，存给人看的中文仓库名（如「红盒子-格力高磐石仓」）；
`wdt_warehouse_no` 存旺店通仓库编号（`HHZ-Glico`），两者分开：
- 页面显示中文名，推送报文只认编号；
- 老订单没有编号时，推送回退 `.env` 的 `WDT_WAREHOUSE_NO` / 系统默认仓。

Revision ID: 0029_fx_order_wdt_warehouse
Revises: 0028_fx_payment_vouchers
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0029_fx_order_wdt_warehouse"
down_revision = "0028_fx_payment_vouchers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fx_orders",
        sa.Column("wdt_warehouse_no", sa.String(length=40), nullable=False, server_default=""),
    )


def downgrade() -> None:
    with op.batch_alter_table("fx_orders") as batch:
        batch.drop_column("wdt_warehouse_no")
