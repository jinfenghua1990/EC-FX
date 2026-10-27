"""采购单入库 + 商品档案加「采购价」三列

一、新增两张采购单表（采购单后续要跟商品/分销订单做关联，必须先在系统里有原单）
------------------------------------------------------------------------------
- fx_purchase_orders：采购单主表，以 purchase_no 唯一（旺店通 purchase_id 建索引）。
- fx_purchase_items：采购单明细，价格字段口径见下方注释。

二、fx_products 加三列
----------------------
- purchase_price：最近一次正常采购的含税单价（按基本单位），0 = 从未采购
- purchase_date： 该次采购单日期（审核时间优先）
- purchase_no：   该次采购单号，便于回旺店通溯源

为什么采购价必须独立成列
------------------------
商品档案接口（goods_query.php）只有 wholesale / retail / market / lowest 四个价，
wholesale_price 被当「品牌原价（含税）」= 牌价。但牌价 ≠ 成本：
格力高是牌价 8 折拿货（牌价含税 5.83/盒 → 实付 4.66/盒），且各品牌折扣率不同、
同品牌也会调价，**不能用统一系数从牌价反算成本**。

采购单明细里的 price / tax_price 就是真实下单价（含税、按基本单位、已折后）。
实测与本地 base_price 同口径：40 个有价 SKU 中 35 个精确命中，中位数比值 1.0003。

价格字段避坑
------------
不要用旺店通的 details_list[].purchase_price 当成本：瑞士莲 600g 采购单
price=103 而 purchase_price=92.42，比值 1.1145 既不是 13% 也不是任何固定税率。
本迁移把它原样存进 fx_purchase_items.purchase_price 仅供溯源，不参与计算。
"""
import sqlalchemy as sa
from alembic import op

revision = "0012_fx_purchase_orders"
down_revision = "0011_fx_product_tier1_price"
branch_labels = None
depends_on = None

_MONEY = sa.Numeric(18, 4)


def upgrade() -> None:
    op.create_table(
        "fx_purchase_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("wdt_purchase_id", sa.String(40), nullable=False, server_default=""),
        sa.Column("purchase_no", sa.String(40), nullable=False),
        sa.Column("purchase_outer_no", sa.String(60), nullable=False, server_default=""),
        sa.Column("provider_no", sa.String(60), nullable=False, server_default=""),
        sa.Column("provider_name", sa.String(160), nullable=False, server_default=""),
        sa.Column("warehouse_no", sa.String(60), nullable=False, server_default=""),
        sa.Column("warehouse_name", sa.String(160), nullable=False, server_default=""),
        sa.Column("purchaser_name", sa.String(80), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default=""),
        sa.Column("check_time", sa.DateTime(), nullable=True),
        sa.Column("created_time", sa.DateTime(), nullable=True),
        sa.Column("modified", sa.DateTime(), nullable=True),
        sa.Column("expect_arrive_time", sa.DateTime(), nullable=True),
        sa.Column("goods_count", _MONEY, nullable=False, server_default="0"),
        sa.Column("goods_amount", _MONEY, nullable=False, server_default="0"),
        sa.Column("tax_goods_amount", _MONEY, nullable=False, server_default="0"),
        sa.Column("post_fee", _MONEY, nullable=False, server_default="0"),
        sa.Column("other_fee", _MONEY, nullable=False, server_default="0"),
        sa.Column("remark", sa.String(1000), nullable=False, server_default=""),
        sa.Column("abnormal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("abnormal_reason", sa.String(200), nullable=False, server_default=""),
        sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_fx_purchase_orders_purchase_no", "fx_purchase_orders", ["purchase_no"], unique=True)
    op.create_index("ix_fx_purchase_orders_wdt_purchase_id", "fx_purchase_orders", ["wdt_purchase_id"])
    op.create_index("ix_fx_purchase_orders_provider_no", "fx_purchase_orders", ["provider_no"])
    op.create_index("ix_fx_purchase_orders_warehouse_no", "fx_purchase_orders", ["warehouse_no"])
    op.create_index("ix_fx_purchase_orders_status", "fx_purchase_orders", ["status"])
    op.create_index("ix_fx_purchase_orders_check_time", "fx_purchase_orders", ["check_time"])
    op.create_index("ix_fx_purchase_orders_abnormal", "fx_purchase_orders", ["abnormal"])

    op.create_table(
        "fx_purchase_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "order_id",
            sa.Integer(),
            sa.ForeignKey("fx_purchase_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rec_id", sa.String(40), nullable=False, server_default=""),
        sa.Column("spec_no", sa.String(80), nullable=False, server_default=""),
        sa.Column("goods_no", sa.String(80), nullable=False, server_default=""),
        sa.Column("goods_name", sa.String(255), nullable=False, server_default=""),
        sa.Column("spec_name", sa.String(160), nullable=False, server_default=""),
        sa.Column("brand_name", sa.String(100), nullable=False, server_default=""),
        sa.Column("base_unit", sa.String(20), nullable=False, server_default=""),
        sa.Column("purchase_unit", sa.String(40), nullable=False, server_default=""),
        sa.Column("unit_ratio", _MONEY, nullable=False, server_default="0"),
        sa.Column("num", _MONEY, nullable=False, server_default="0"),
        sa.Column("num2", _MONEY, nullable=False, server_default="0"),
        sa.Column("unit_price", _MONEY, nullable=False, server_default="0"),
        sa.Column("amount", _MONEY, nullable=False, server_default="0"),
        sa.Column("tax_amount", _MONEY, nullable=False, server_default="0"),
        sa.Column("tax_rate", _MONEY, nullable=False, server_default="0"),
        sa.Column("purchase_price", _MONEY, nullable=False, server_default="0"),
        sa.Column("last_price", _MONEY, nullable=False, server_default="0"),
        sa.Column("lowest_price", _MONEY, nullable=False, server_default="0"),
        sa.Column("arrive_num", _MONEY, nullable=False, server_default="0"),
        sa.Column("stockin_num", _MONEY, nullable=False, server_default="0"),
        sa.Column("warehouse_id", sa.String(40), nullable=False, server_default=""),
    )
    op.create_index("ix_fx_purchase_items_order_id", "fx_purchase_items", ["order_id"])
    op.create_index("ix_fx_purchase_items_spec_no", "fx_purchase_items", ["spec_no"])
    op.create_index("ix_fx_purchase_items_goods_no", "fx_purchase_items", ["goods_no"])

    with op.batch_alter_table("fx_products") as batch:
        batch.add_column(sa.Column("purchase_price", _MONEY, nullable=False, server_default="0"))
        batch.add_column(sa.Column("purchase_date", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("purchase_no", sa.String(40), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("fx_products") as batch:
        batch.drop_column("purchase_no")
        batch.drop_column("purchase_date")
        batch.drop_column("purchase_price")
    op.drop_index("ix_fx_purchase_items_goods_no", table_name="fx_purchase_items")
    op.drop_index("ix_fx_purchase_items_spec_no", table_name="fx_purchase_items")
    op.drop_index("ix_fx_purchase_items_order_id", table_name="fx_purchase_items")
    op.drop_table("fx_purchase_items")
    for name in (
        "ix_fx_purchase_orders_abnormal",
        "ix_fx_purchase_orders_check_time",
        "ix_fx_purchase_orders_status",
        "ix_fx_purchase_orders_warehouse_no",
        "ix_fx_purchase_orders_provider_no",
        "ix_fx_purchase_orders_wdt_purchase_id",
        "ix_fx_purchase_orders_purchase_no",
    ):
        op.drop_index(name, table_name="fx_purchase_orders")
    op.drop_table("fx_purchase_orders")
