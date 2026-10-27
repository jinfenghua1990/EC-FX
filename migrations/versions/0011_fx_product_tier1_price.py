"""给商品档案加「一级分销价」：fx_products.tier1_price

背景
----
商品原来只有一个价格 base_price（品牌原价 / 旺店通批发价，含税口径）。
现在要区分两个价：

- base_price：品牌给我们的单价（含税），是成本基准；
- tier1_price：我们卖给一级分销商的单价（含税），由人手工设定。

页面要能并排看「品牌原价 → 一级分销价 → 毛利」，所以必须各存一列，
不能拿 base_price 反算（加价率因品牌/商品不同而不同，不能统一系数）。

口径
----
- 金额 Decimal(12,2)，默认 0 = 还没定价，页面显示「待定」。
- 不做含税/未税换算：两个价都是含税价，毛利 = tier1_price - base_price，
  毛利率 = 毛利 / tier1_price。系统里不存税率，避免各月税率变化导致历史数据漂移。
"""
import sqlalchemy as sa
from alembic import op

revision = "0011_fx_product_tier1_price"
down_revision = "0010_fx_product_pack_spec"
branch_labels = None
depends_on = None

_TABLE = "fx_products"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column("tier1_price", sa.Numeric(12, 2), nullable=False, server_default="0"))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("tier1_price")
