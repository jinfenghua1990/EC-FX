"""Expand legacy shipment source order number to 1024 characters."""
from alembic import op
import sqlalchemy as sa

revision = "0002_expand_legacy_src_order_no"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "legacy_shipment_rows",
            "src_order_no",
            existing_type=sa.String(length=160),
            type_=sa.String(length=1024),
            existing_nullable=False,
        )


def downgrade() -> None:
    # 不自动缩短，避免历史长订单号被截断。需要降级时恢复升级前数据库备份。
    pass
