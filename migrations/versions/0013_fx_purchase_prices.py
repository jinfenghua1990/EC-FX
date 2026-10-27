"""采购价改按「仓库」分组 + 商品加成本仓来源字段

为什么必须改
------------
0012 的算价逻辑是「全表取该 SKU 最近一条采购明细」，完全不看仓库和供应商。
实测发现这会算出错误结论：

瑞士莲软心黑巧 600g 有 51 条采购记录，88 / 95 / 96 / 98 / 103 / 104 六个价
**同期并存**，原因是它们分布在不同仓：
- 磐石-瑞士莲（京东专供)   41 次  398 万  ← 京东下单
- 聚小翻-磐石上海仓        10 次   11 万  ← 自家调拨

「取全表最新」把不同渠道的价混在一起，会得出「88 涨到 103」这种不存在的结论。

同时源数据本身也有错：CG202512090001 / CG202512090002 两张单把
「婚礼装 1.25kg」和「悠享装 200g」的单价互相录反了（41 / 165），
按仓取最新价能绕开，但仍需保留全量明细便于人工核对。

一、新增 fx_purchase_prices
--------------------------
按 (sku, warehouse_name) 存该仓最近一次正常采购价，附带该仓累计金额、次数、
价区间。is_primary 标记被选为成本基准的仓；excluded 标记无实物仓（虚拟仓/仅记录仓）。

二、fx_products 加三列
----------------------
- purchase_warehouse：主价的来源仓（页面悬浮提示「这个价哪来的」）
- purchase_provider： 主价的供应商
- cost_warehouse：    手工指定成本仓，空 = 自动选。
  各品牌取价逻辑不同（格力高单仓直采、瑞士莲多渠道），需要能按品牌统一下发口径。

选主仓的自动规则（见 wdt_purchase._pick_primary）
1. 商品填了 cost_warehouse → 直接用它（该仓没记录则退回自动规则并告警）
2. 仓库名包含该商品品牌关键词（如「红盒子-格力高磐石仓」对格力高）→ 优先
3. 否则取该 SKU 累计采购金额最大的非排除仓
4. 全部被排除时退回金额最大仓，并在结果里标记 warn
"""
import sqlalchemy as sa
from alembic import op

revision = "0013_fx_purchase_prices"
down_revision = "0012_fx_purchase_orders"
branch_labels = None
depends_on = None

_MONEY = sa.Numeric(18, 4)


def upgrade() -> None:
    op.create_table(
        "fx_purchase_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sku", sa.String(60), nullable=False),
        sa.Column("brand", sa.String(100), nullable=False, server_default=""),
        sa.Column("warehouse_name", sa.String(160), nullable=False, server_default=""),
        sa.Column("provider_name", sa.String(160), nullable=False, server_default=""),
        sa.Column("price", _MONEY, nullable=False, server_default="0"),
        sa.Column("purchase_date", sa.DateTime(), nullable=True),
        sa.Column("purchase_no", sa.String(40), nullable=False, server_default=""),
        sa.Column("total_amount", _MONEY, nullable=False, server_default="0"),
        sa.Column("times", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("min_price", _MONEY, nullable=False, server_default="0"),
        sa.Column("max_price", _MONEY, nullable=False, server_default="0"),
        sa.Column("is_primary", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("excluded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("exclude_reason", sa.String(200), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_fx_purchase_prices_sku", "fx_purchase_prices", ["sku"])
    op.create_index("ix_fx_purchase_prices_brand", "fx_purchase_prices", ["brand"])
    op.create_index("ix_fx_purchase_prices_warehouse", "fx_purchase_prices", ["warehouse_name"])
    op.create_index("ix_fx_purchase_prices_primary", "fx_purchase_prices", ["is_primary"])

    with op.batch_alter_table("fx_products") as batch:
        batch.add_column(sa.Column("purchase_warehouse", sa.String(160), nullable=False, server_default=""))
        batch.add_column(sa.Column("purchase_provider", sa.String(160), nullable=False, server_default=""))
        batch.add_column(sa.Column("cost_warehouse", sa.String(160), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("fx_products") as batch:
        batch.drop_column("cost_warehouse")
        batch.drop_column("purchase_provider")
        batch.drop_column("purchase_warehouse")
    for name in (
        "ix_fx_purchase_prices_primary",
        "ix_fx_purchase_prices_warehouse",
        "ix_fx_purchase_prices_brand",
        "ix_fx_purchase_prices_sku",
    ):
        op.drop_index(name, table_name="fx_purchase_prices")
    op.drop_table("fx_purchase_prices")
