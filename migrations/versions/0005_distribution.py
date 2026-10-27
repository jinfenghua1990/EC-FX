"""FX distribution module: distributors / products / orders.

升级为分销系统的第一版数据表。沿用本项目迁移风格：
新表通过 SQLAlchemy metadata 一次性创建，老表不动、数据不迁移。
"""
from alembic import op

revision = "0005_distribution"
down_revision = "0004_standardization_pipeline"
branch_labels = None
depends_on = None

_TABLES = ("fx_order_lines", "fx_orders", "fx_products", "fx_distributors")


def upgrade() -> None:
    from app.db import Base
    import app.models  # noqa: F401

    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    from sqlalchemy import inspect

    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    for name in _TABLES:
        if name in existing:
            op.drop_table(name)
