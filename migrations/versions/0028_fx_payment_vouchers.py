"""0028 客户打款凭证（截图 / 回单）。

用户 2026-10-28 要求：客户付款后把打款凭证粘进系统，自动识别金额和日期，
放进「应收账款」，每个订单都能挂自己的凭证。

为什么单独一张表而不是给 fx_orders 加几个字段：
- 一笔订单可能分多次打款（先付 30%，发货前补齐），凭证是 1:N；
- 同一张凭证可能覆盖多张订单（客户一次转 5 万结 3 单），需要能改挂；
- 凭证原图要保留（财务追溯），二进制不进库，只存相对路径。

Revision ID: 0028_fx_payment_vouchers
Revises: 0027_fx_product_hot
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0028_fx_payment_vouchers"
down_revision = "0027_fx_product_hot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fx_payment_vouchers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("fx_orders.id", ondelete="SET NULL"), nullable=True),
        sa.Column("distributor_id", sa.Integer(), sa.ForeignKey("fx_distributors.id"), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("stored_path", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("mime", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("size", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("ocr_status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("ocr_engine", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("ocr_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("amount", sa.Numeric(18, 4), nullable=True),
        sa.Column("paid_at", sa.String(length=20), nullable=False, server_default=""),
        sa.Column("payer", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("pay_method", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("note", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("applied", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_fx_payment_vouchers_order_id", "fx_payment_vouchers", ["order_id"])
    op.create_index("ix_fx_payment_vouchers_distributor_id", "fx_payment_vouchers", ["distributor_id"])
    op.create_index("ix_fx_payment_vouchers_sha256", "fx_payment_vouchers", ["sha256"])
    op.create_index("ix_fx_payment_vouchers_ocr_status", "fx_payment_vouchers", ["ocr_status"])
    op.create_index("ix_fx_payment_vouchers_applied", "fx_payment_vouchers", ["applied"])


def downgrade() -> None:
    op.drop_table("fx_payment_vouchers")
