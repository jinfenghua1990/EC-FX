"""商品加「牌价」与「成本核对状态」两个字段

一、list_price（牌价，含税，按基本单位）
--------------------------------------
2026-10-27 用户明确：「牌价只有格力高想要，其他品牌不维护」。

牌价来源：品牌订货单 Excel（`20261027抖音红盒子订单.xls`）的
`盒（未税） × 1.13 = 每盒含税牌价`，也等于 `单价(含税) ÷ 规格`。
实测：百奇巧克力 47g 盒(未税) 5.1593 × 1.13 = 5.83，× 0.8 = 4.664，正好等于采购单实付价。

为什么不复用 base_price：
base_price 取旺店通 goods_query.php 的 wholesale_price，实测语义不统一 ——
- 百醇巧克力 48g：wholesale 5.05 = 采购价 5.05
- 轻松熊礼盒 194g：wholesale 29.00 vs 采购价 16.88（这里是牌价）
- 瑞士莲 600g：wholesale 88.00 vs 采购价 103.00（这里是旧价）
所以另有 list_price 专门承载牌价，只用于算分销折扣率（分销价 ÷ 牌价），不参与利润。

二、cost_status / cost_note（成本核对状态）
------------------------------------------
用户要求：「源单录反的，系统里标待核对」。cost_status="pending" 的商品不出毛利，
避免拿错价算利润。触发场景：CG202512090001 / CG202512090002 把
`7630023691089 婚礼装 1.25kg` 记成 41 元（应为 165）、`7630023691997 悠享装 200g`
记成 165 元（应为 41）。
"""
import sqlalchemy as sa
from alembic import op

revision = "0014_fx_product_list_price"
down_revision = "0013_fx_purchase_prices"
branch_labels = None
depends_on = None

_MONEY = sa.Numeric(18, 4)


def upgrade() -> None:
    with op.batch_alter_table("fx_products") as batch:
        batch.add_column(sa.Column("list_price", _MONEY, nullable=False, server_default="0"))
        batch.add_column(sa.Column("cost_status", sa.String(16), nullable=False, server_default=""))
        batch.add_column(sa.Column("cost_note", sa.String(300), nullable=False, server_default=""))
    op.create_index("ix_fx_products_cost_status", "fx_products", ["cost_status"])


def downgrade() -> None:
    op.drop_index("ix_fx_products_cost_status", table_name="fx_products")
    with op.batch_alter_table("fx_products") as batch:
        batch.drop_column("cost_note")
        batch.drop_column("cost_status")
        batch.drop_column("list_price")
