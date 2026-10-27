"""Add V0.8 field-mapping and standardized data pipeline tables."""
from alembic import op
import sqlalchemy as sa

revision = "0004_standardization_pipeline"
down_revision = "0003_module_hub"
branch_labels = None
depends_on = None


def _has(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _has("module_mapping_profiles"):
        op.create_table(
            "module_mapping_profiles",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("module", sa.String(40), nullable=False),
            sa.Column("name", sa.String(120), nullable=False, server_default="默认映射"),
            sa.Column("mapping_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("module", "name", name="uq_module_mapping_profile"),
        )
        op.create_index("ix_module_mapping_profiles_module", "module_mapping_profiles", ["module"])
        op.create_index("ix_module_mapping_profiles_active", "module_mapping_profiles", ["active"])

    if not _has("module_standard_records"):
        op.create_table(
            "module_standard_records",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("batch_id", sa.Integer(), sa.ForeignKey("import_batches.id", ondelete="CASCADE"), nullable=False),
            sa.Column("module", sa.String(40), nullable=False),
            sa.Column("row_no", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("period", sa.String(7), nullable=False, server_default=""),
            sa.Column("external_key", sa.String(255), nullable=False, server_default=""),
            sa.Column("standard_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(32), nullable=False, server_default="valid"),
            sa.Column("error_text", sa.String(1000), nullable=False, server_default=""),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("batch_id", "row_no", name="uq_module_standard_batch_row"),
        )
        for col in ("batch_id", "module", "period", "external_key", "status"):
            op.create_index(f"ix_module_standard_records_{col}", "module_standard_records", [col])


def downgrade() -> None:
    for name in ("module_standard_records", "module_mapping_profiles"):
        if _has(name):
            op.drop_table(name)
