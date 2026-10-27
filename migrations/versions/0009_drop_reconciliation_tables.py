"""清理核销系统：删除 15 张遗留空表。

核销业务（0610 母表算法 / 模块数据中心 / 平台结算对账 / 返利规则）已整体下线，
对应的模型、服务、接口、页面均已移除。这些表在本机库中全部为 0 行，
删除不影响任何现存数据。

**保留不动**：
- fx_distributors / fx_products / fx_orders / fx_order_lines / fx_prices —— 分销业务
- stockout_orders / stockout_items —— 销售出库，「经营总览」「发货明细」在用
- manual_adjustments —— 销售出库的手工调整记录

注意：本迁移为单向（downgrade 无法还原表结构），因为建表语句已随模型一起移除。
如需回到带核销表的结构，请先回滚代码到 0008 之前的版本，再执行 alembic downgrade。
"""
from alembic import op

revision = "0009_drop_reconciliation_tables"
down_revision = "0007_wdt_product_sync"
branch_labels = None
depends_on = None

# 按依赖倒序：先删引用方，再删被引用方（SQLite 下顺序不强制，但保持正确语义）
_DROP_TABLES: tuple[str, ...] = (
    "recon_results",            # 引用 settlement_rows / stockout_orders
    "bank_rows",                # 引用 import_batches
    "settlement_rows",          # 引用 import_batches
    "legacy_shipment_rows",     # 引用 import_batches
    "legacy_income_rows",
    "legacy_logistics_rows",
    "legacy_prepaid_rows",
    "legacy_inventory_rows",
    "legacy_purchase_rows",
    "legacy_rebate_rows",
    "module_standard_records",
    "module_raw_records",
    "module_mapping_profiles",
    "rebate_rules",
    "import_batches",           # 上游被引用方，最后删
)


def _existing_tables() -> set[str]:
    from sqlalchemy import inspect

    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _existing_tables()
    for table in _DROP_TABLES:
        if table in existing:
            op.drop_table(table)


def downgrade() -> None:
    raise RuntimeError(
        "0009 为单向迁移：核销表的建表定义已随 models.py 一起移除，无法就地重建。"
        "如需恢复，请先把代码回滚到 0008 之前的版本再执行降级。"
    )
