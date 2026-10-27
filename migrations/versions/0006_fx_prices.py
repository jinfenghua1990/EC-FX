"""FX 经销商价格体系 + 订单推送记录列.

- 新表 fx_prices（经销商专属价）沿用本项目迁移风格：SQLAlchemy metadata 一次性创建。
- fx_orders 老表已有，create_all 不会补列，所以 push_status / push_message / pushed_at 用 add_column 补。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0006_fx_prices"
down_revision = "0005_distribution"
branch_labels = None
depends_on = None

_PUSH_COLUMNS = (
    ("push_status", sa.Column("push_status", sa.String(16), nullable=False, server_default="", default="")),
    ("push_message", sa.Column("push_message", sa.String(500), nullable=False, server_default="", default="")),
    ("pushed_at", sa.Column("pushed_at", sa.DateTime(), nullable=True)),
)


def upgrade() -> None:
    from app.db import Base
    import app.models  # noqa: F401

    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)

    existing = {col["name"] for col in inspect(bind).get_columns("fx_orders")}
    for name, column in _PUSH_COLUMNS:
        if name not in existing:
            op.add_column("fx_orders", column)


def downgrade() -> None:
    from sqlalchemy import inspect as _inspect

    bind = op.get_bind()
    existing_tables = set(_inspect(bind).get_table_names())
    if "fx_prices" in existing_tables:
        op.drop_table("fx_prices")
    existing = {col["name"] for col in _inspect(bind).get_columns("fx_orders")}
    for name, _column in _PUSH_COLUMNS:
        if name in existing:
            try:
                op.drop_column("fx_orders", name)
            except Exception:  # pragma: no cover - 老版 SQLite 不支持 drop column 时忽略
                pass
