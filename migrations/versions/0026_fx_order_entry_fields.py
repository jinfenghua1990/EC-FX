"""0026 下单页订单头字段：渠道 / 发货仓库 / 发货要求 / 配送方式 / 刮码。

用户 2026-10-28 要求下单界面把订单维度的事情一次性录全，不要再靠备注口口相传：
- 下单渠道：这单从哪个渠道来的（线下 / 团购 / 直播 / 电商…）；
- 发货仓库：从哪个仓出；
- 发货要求：给仓库的额外要求（打包方式、随货资料、时效等），自由文本；
- 配送方式：自提 / 送货 —— 影响是否要物流单号，不能默认；
- 刮码：客户要求刮掉条码（防窜货 / 防追溯），订单级开关，默认不刮。

与既有字段的分工（不重复、不覆盖）：
- `source_channel` 是**系统来源**（手工 / 旺店通 / 团单Excel），由程序写；
- `order_channel` 是**业务渠道**，由下单人填，两者不是一个概念。

Revision ID: 0026_fx_order_entry_fields
Revises: 0025_fx_order_fulfillment
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0026_fx_order_entry_fields"
down_revision = "0025_fx_order_fulfillment"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fx_orders",
        sa.Column("order_channel", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column(
        "fx_orders",
        sa.Column("ship_warehouse", sa.String(length=80), nullable=False, server_default=""),
    )
    op.add_column(
        "fx_orders",
        sa.Column("ship_note", sa.String(length=500), nullable=False, server_default=""),
    )
    op.add_column(
        "fx_orders",
        sa.Column("delivery_type", sa.String(length=16), nullable=False, server_default=""),
    )
    # 0 = 不刮码（默认），1 = 需要刮码。用 Integer 而不是 Boolean，SQLite 上最稳。
    op.add_column(
        "fx_orders",
        sa.Column("scratch_code", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    with op.batch_alter_table("fx_orders") as batch:
        for col in ("scratch_code", "delivery_type", "ship_note", "ship_warehouse", "order_channel"):
            batch.drop_column(col)
