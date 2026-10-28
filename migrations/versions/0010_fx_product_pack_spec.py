"""给商品档案加「箱规」：fx_products.pack_qty / pack_unit

背景
----
分销报价与整箱订货都需要知道「1 箱 = 多少件」。旺店通商品档案里本来就有这个信息，
放在货品级的 aux_unit（换算数量）与 aux_unit_name（名称，形如「36/箱」）里，
但之前的商品同步只落了 unit_name（基本单位，如「件」），箱规没进本地库。

口径
----
- pack_qty：1 箱包含多少个基础单位（unit）；0 表示未维护，页面显示「—」。
- pack_unit：箱规单位，默认「箱」（旺店通文案里可能写「36/箱」，只取斜杠后的字）。

默认值 0 / '箱' 让老数据直接可用：历史 347 个商品都是「未维护箱规」，
重新点一次「从旺店通同步」即可按旺店通档案回填，不需要手工补。
"""
import sqlalchemy as sa
from alembic import op

revision = "0010_fx_product_pack_spec"
down_revision = "0009_drop_reconciliation_tables"
branch_labels = None
depends_on = None

_TABLE = "fx_products"


def upgrade() -> None:
    # 0007 里 create_all 会按当前模型把 fx_products 整张表建出来，箱规两列已经在里面；
    # 老库（先有表、后升级）才需要补列。两种路径都要能跑通，所以先看有没有。
    from sqlalchemy import inspect

    existing = {c["name"] for c in inspect(op.get_bind()).get_columns(_TABLE)}
    with op.batch_alter_table(_TABLE) as batch:
        if "pack_qty" not in existing:
            batch.add_column(sa.Column("pack_qty", sa.Integer(), nullable=False, server_default="0"))
        if "pack_unit" not in existing:
            batch.add_column(sa.Column("pack_unit", sa.String(length=20), nullable=False, server_default="箱"))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("pack_unit")
        batch.drop_column("pack_qty")
