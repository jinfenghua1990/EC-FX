"""FX 商品档案接入旺店通：为 fx_products 补充旺店通字段。

沿用本项目迁移风格：不删列、不改老字段，只做增量补列。
对已有库用 add_column 补列；对全新建的库（create_all 已带齐列）则跳过，
因此升级脚本对「先建库」和「后升级」两种路径都安全、可重复执行。
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_wdt_product_sync"
down_revision = "0006_fx_prices"
branch_labels = None
depends_on = None

# (列名, 类型, 服务端默认值) —— 与 models.FxProduct 保持一致
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, str], ...] = (
    ("spec_no", sa.String(length=160), ""),
    ("goods_no", sa.String(length=80), ""),
    ("barcode", sa.String(length=60), ""),
    ("category", sa.String(length=100), ""),
    ("brand", sa.String(length=100), ""),
    ("source", sa.String(length=20), "manual"),
    ("wdt_synced_at", sa.DateTime(), None),
)

_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_fx_products_spec_no", "spec_no"),
    ("ix_fx_products_goods_no", "goods_no"),
    ("ix_fx_products_source", "source"),
)


def _existing_columns() -> set[str]:
    from sqlalchemy import inspect

    return {c["name"] for c in inspect(op.get_bind()).get_columns("fx_products")}


def _existing_indexes() -> set[str]:
    from sqlalchemy import inspect

    return {i["name"] for i in inspect(op.get_bind()).get_indexes("fx_products")}


def upgrade() -> None:
    from app.db import Base
    import app.models  # noqa: F401

    # 新库：直接把缺失的表建出来（已有表不会被动）
    Base.metadata.create_all(bind=op.get_bind())

    existing = _existing_columns()
    for name, type_, default in _COLUMNS:
        if name in existing:
            continue
        op.add_column(
            "fx_products",
            sa.Column(name, type_, nullable=True, server_default=default if default is not None else None),
        )

    # 补列后把 NULL 归位，避免视图层拿到 None
    for name, _, default in _COLUMNS:
        if default is None:
            continue
        op.execute(
            sa.text(f"UPDATE fx_products SET {name} = :val WHERE {name} IS NULL").bindparams(val=default)
        )

    idx = _existing_indexes()
    for index_name, column in _INDEXES:
        if index_name in idx:
            continue
        op.create_index(index_name, "fx_products", [column])


def downgrade() -> None:
    from sqlalchemy import inspect

    inspector = inspect(op.get_bind())
    idx = {i["name"] for i in inspector.get_indexes("fx_products")}
    for index_name, _ in _INDEXES:
        if index_name in idx:
            op.drop_index(index_name, table_name="fx_products")

    existing = {c["name"] for c in inspector.get_columns("fx_products")}
    for name, _, _ in _COLUMNS:
        if name in existing:
            op.drop_column("fx_products", name)
