"""0025 分销订单补齐标准字段：原始单号 / 收款 / 发货。

用户 2026-10-28 指出：分销订单页面只有内部单号和审核状态，
但标准订单至少还要有：
- 旺店通 / Excel 里的原始订单号（不是内部 FX 单号）；
- 款：已收到（收款状态、金额、日期）；
- 订单已发货（发货状态、时间、物流公司 / 单号）；
- 签收时间等业务信息。

订单级「付款公司」由并行迁移 0024_fx_order_pay_company 提供，本迁移接在它后面，
不重复建同义字段。

本迁移只加列并做安全回填：
- 历史订单的 `wdt_tid` 如果不是内部单号，回填到 `original_order_no`；
- 有旺店通单号的订单先按「已发货」标记（原表出库单号是发货证据）；
- 收款状态默认「未收到」，随后由源表回填脚本按实际付款日期修正，绝不凭空标记已收。

Revision ID: 0025_fx_order_fulfillment
Revises: 0024_fx_order_pay_company
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0025_fx_order_fulfillment"
down_revision = "0024_fx_order_pay_company"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fx_orders",
        sa.Column("original_order_no", sa.String(length=255), nullable=False, server_default=""),
    )
    op.add_column(
        "fx_orders",
        sa.Column("source_channel", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column(
        "fx_orders",
        sa.Column("payment_status", sa.String(length=16), nullable=False, server_default="unpaid"),
    )
    op.add_column(
        "fx_orders",
        sa.Column("paid_amount", sa.Numeric(18, 4), nullable=False, server_default="0"),
    )
    op.add_column("fx_orders", sa.Column("payment_date", sa.DateTime(), nullable=True))
    op.add_column(
        "fx_orders",
        sa.Column("shipping_status", sa.String(length=16), nullable=False, server_default="unshipped"),
    )
    op.add_column("fx_orders", sa.Column("shipped_at", sa.DateTime(), nullable=True))
    op.add_column(
        "fx_orders",
        sa.Column("logistics_company", sa.String(length=100), nullable=False, server_default=""),
    )
    op.add_column(
        "fx_orders",
        sa.Column("logistics_no", sa.String(length=120), nullable=False, server_default=""),
    )
    op.add_column("fx_orders", sa.Column("signed_at", sa.DateTime(), nullable=True))

    conn = op.get_bind()
    # 内部 FX 单号不能冒充原始单号；只有历史上从旺店通/Excel 带过来的 wdt_tid 才回填。
    conn.execute(
        sa.text(
            "UPDATE fx_orders SET original_order_no = wdt_tid "
            "WHERE COALESCE(wdt_tid, '') <> '' AND wdt_tid <> order_no"
        )
    )
    # 有旺店通出库单号 = 原表已有出库记录，先标已发货；发货时间无法从列里精确还原，留空等回填。
    conn.execute(
        sa.text(
            "UPDATE fx_orders SET shipping_status = 'shipped' "
            "WHERE COALESCE(wdt_tid, '') <> '' OR push_status = 'sent'"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE fx_orders SET source_channel = '团单Excel' "
            "WHERE remark LIKE '团单导入%'"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("fx_orders") as batch:
        for col in (
            "signed_at", "logistics_no", "logistics_company", "shipped_at", "shipping_status",
            "payment_date", "paid_amount", "payment_status", "source_channel", "original_order_no",
        ):
            batch.drop_column(col)
