"""Add V0.7 modular data hub tables.

Fresh databases may already contain these tables because the dynamic baseline creates
current metadata. Existing installations do not, so create each table only when absent.
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_module_hub"
down_revision = "0002_expand_legacy_src_order_no"
branch_labels = None
depends_on = None


def _has(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has("module_raw_records"):
        op.create_table(
            "module_raw_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("module", sa.String(40), nullable=False),
            sa.Column("row_no", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("period", sa.String(7), nullable=False, server_default=""),
            sa.Column("external_key", sa.String(255), nullable=False, server_default=""),
            sa.Column("raw_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_module_raw_records_batch_id", "module_raw_records", ["batch_id"])
        op.create_index("ix_module_raw_records_module", "module_raw_records", ["module"])
        op.create_index("ix_module_raw_records_period", "module_raw_records", ["period"])
        op.create_index("ix_module_raw_records_external_key", "module_raw_records", ["external_key"])

    if not _has("manual_adjustments"):
        op.create_table(
            "manual_adjustments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("module", sa.String(40), nullable=False, server_default="sales-outbound"),
            sa.Column("target_type", sa.String(24), nullable=False, server_default="order"),
            sa.Column("target_key", sa.String(255), nullable=False),
            sa.Column("period", sa.String(7), nullable=False, server_default=""),
            sa.Column("field_name", sa.String(80), nullable=False, server_default=""),
            sa.Column("operation", sa.String(24), nullable=False, server_default="add"),
            sa.Column("decimal_value", sa.Numeric(18,4), nullable=True),
            sa.Column("text_value", sa.String(1000), nullable=False, server_default=""),
            sa.Column("reason", sa.String(1000), nullable=False, server_default=""),
            sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        for col in ("module", "target_type", "target_key", "period", "active"):
            op.create_index(f"ix_manual_adjustments_{col}", "manual_adjustments", [col])

    if not _has("rebate_rules"):
        op.create_table(
            "rebate_rules",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("brand", sa.String(100), nullable=False, server_default="*"),
            sa.Column("channel", sa.String(60), nullable=False, server_default="*"),
            sa.Column("category", sa.String(80), nullable=False, server_default="品牌返利"),
            sa.Column("basis_field", sa.String(80), nullable=False, server_default="invoice_amount_untaxed"),
            sa.Column("rate", sa.Numeric(10,6), nullable=False, server_default="0"),
            sa.Column("tax_factor", sa.Numeric(10,6), nullable=False, server_default="1"),
            sa.Column("effective_from", sa.String(7), nullable=False, server_default=""),
            sa.Column("effective_to", sa.String(7), nullable=False, server_default=""),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("note", sa.String(1000), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
        )
        for col in ("brand", "channel", "effective_from", "effective_to", "active"):
            op.create_index(f"ix_rebate_rules_{col}", "rebate_rules", [col])


def downgrade() -> None:
    for name in ("rebate_rules", "manual_adjustments", "module_raw_records"):
        if _has(name):
            op.drop_table(name)
