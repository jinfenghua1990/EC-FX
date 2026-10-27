"""分销订单新增 wdt_shop_no（推送旺店通的目标店铺）

为什么加在订单上
----------------
用户有多家旺店通店铺（实测 37 家），分销相关至少有
「红盒子-格力高-分销渠道(hhz-xxqd)」「红盒子-瑞士莲-分销(KH0001)」等。
下单时选定店铺并**记在订单上**，之后重推同一单永远进同一家店，
不会因为页面默认值变化而推错店铺；留空则回退系统默认配置（.env WDT_SHOP_NO）。
"""
import sqlalchemy as sa
from alembic import op

revision = "0016_fx_order_shop"
down_revision = "0015_fx_stock_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fx_orders", sa.Column("wdt_shop_no", sa.String(40), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("fx_orders", "wdt_shop_no")
