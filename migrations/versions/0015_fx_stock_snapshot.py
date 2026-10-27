"""新增库存快照表 fx_stock_snapshots（库存对接 / 满足率提示）

一、为什么需要这张表
--------------------
用户要在「销售出库单做好后」看到满足率提示：
    实际可用 = 昨天库存 + 今日入库
但旺店通 stock_query.php 只返回「当前」库存，没有历史。
要算「昨天库存」必须自己按日期拍快照：每天同步一次 → 存当天 closing stock，
算满足率时取「最新一张 snap_date < 今天」的快照当昨天库存。

二、单仓模式
-----------
用户明确只有一个仓库，所以所有聚合不区分仓库，快照 warehouse 统一留空。

三、今日入库的来源
-----------------
采购单明细的 stockin_num（已入库）/ arrive_num - stockin_num（到仓待入库），
仅取「已收货」状态且 check_time 为今天的采购单。详见 services/inventory.py。
"""
import sqlalchemy as sa
from alembic import op

revision = "0015_fx_stock_snapshot"
down_revision = "0014_fx_product_list_price"
branch_labels = None
depends_on = None

_MONEY = sa.Numeric(18, 4)


def upgrade() -> None:
    # SQLite 不支持 ALTER ADD 唯一约束，直接在建表时声明（CREATE TABLE 支持）
    op.create_table(
        "fx_stock_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("spec_no", sa.String(160), nullable=False, index=True),
        sa.Column("sku", sa.String(60), nullable=False, index=True),
        sa.Column("warehouse", sa.String(160), nullable=False, server_default=""),
        sa.Column("snap_date", sa.String(10), nullable=False, index=True),
        sa.Column("qty", _MONEY, nullable=False, server_default="0"),
        sa.Column("source", sa.String(20), nullable=False, server_default="wdt_stock_query"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("spec_no", "warehouse", "snap_date", name="uq_fx_stock_snap"),
    )


def downgrade() -> None:
    op.drop_table("fx_stock_snapshots")
