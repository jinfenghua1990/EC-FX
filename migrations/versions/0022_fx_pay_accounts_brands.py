"""0022 分销商付款账户明细 + 品牌主数据。

用户 2026-10-28 要求：
1. 一个分销客户可以有多个付款账户，每条账户要能填税号、开户行/账号、
   户名/收款人、客户收款备注；
2. 品牌要单独管理（新增、改名、排序、停用/启用），分销商再从品牌清单里授权，
   不再只是从商品档案里临时聚合。

本迁移：
- 新建 `fx_distributor_pay_accounts`（明细表），把旧的 `fx_distributors.pay_accounts`
  逗号串拆成明细行，旧列保留做兼容并继续同步账户名；
- 新建 `fx_brands`，从现有商品/分销商品牌初始化，保留原有品牌数据。

Revision ID: 0022_fx_pay_accounts_brands
Revises: 0021_fx_distributor_pay_accounts
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0022_fx_pay_accounts_brands"
down_revision = "0021_fx_distributor_pay_accounts"
branch_labels = None
depends_on = None

_LOGO_BY_SHORT = {
    "格力高": "glico.png",
    "桂格": "quaker.png",
    "乐事": "lays.png",
    "瑞士莲": "lindt.png",
    "三养": "samyang.png",
    "伯纳天纯": "purenatural.png",
}


def _split_accounts(value: str) -> list[str]:
    import re
    out: list[str] = []
    for item in re.split(r"[,，、]", value or ""):
        name = item.strip()
        if name and name not in out:
            out.append(name)
    return out


def upgrade() -> None:
    op.create_table(
        "fx_distributor_pay_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("distributor_id", sa.Integer(), nullable=False),
        sa.Column("account_name", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("tax_no", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("bank_name", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("bank_branch", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("bank_account", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("account_holder", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("account_type", sa.String(length=20), nullable=False, server_default="company"),
        sa.Column("collection_note", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("is_default", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.ForeignKeyConstraint(["distributor_id"], ["fx_distributors.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_fx_distributor_pay_accounts_distributor_id",
        "fx_distributor_pay_accounts",
        ["distributor_id"],
    )

    op.create_table(
        "fx_brands",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("short_name", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("code", sa.String(length=60), nullable=False, server_default=""),
        sa.Column("logo", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("note", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.UniqueConstraint("name", name="uq_fx_brands_name"),
    )
    op.create_index("ix_fx_brands_name", "fx_brands", ["name"], unique=True)
    op.create_index("ix_fx_brands_active", "fx_brands", ["active"])

    conn = op.get_bind()

    # 旧付款账户（逗号串）拆成明细；第一行设为默认，方便老客户直接看到主账户。
    rows = conn.execute(sa.text("SELECT id, pay_accounts FROM fx_distributors")).mappings().all()
    for row in rows:
        names = _split_accounts(row["pay_accounts"] or "")
        for index, name in enumerate(names):
            conn.execute(
                sa.text(
                    "INSERT INTO fx_distributor_pay_accounts "
                    "(distributor_id, account_name, is_default) "
                    "VALUES (:distributor_id, :account_name, :is_default)"
                ),
                {"distributor_id": row["id"], "account_name": name, "is_default": 1 if index == 0 else 0},
            )

    # 从商品品牌初始化品牌主数据；分销商已授权但商品还没同步的品牌也一并建房。
    brand_names: list[str] = []
    product_rows = conn.execute(
        sa.text(
            "SELECT brand FROM fx_products "
            "WHERE active = 1 AND brand IS NOT NULL AND TRIM(brand) <> '' "
            "GROUP BY brand ORDER BY COUNT(*) DESC, brand"
        )
    ).scalars().all()
    for name in product_rows:
        name = (name or "").strip()
        if name and name not in brand_names:
            brand_names.append(name)
    dist_rows = conn.execute(sa.text("SELECT brands FROM fx_distributors")).scalars().all()
    for value in dist_rows:
        for name in _split_accounts(value or ""):
            if name not in brand_names:
                brand_names.append(name)
    for name in brand_names:
        short = name.split("/", 1)[0].strip()
        conn.execute(
            sa.text(
                "INSERT INTO fx_brands (name, short_name, logo, sort_order, active) "
                "VALUES (:name, :short_name, :logo, 0, 1)"
            ),
            {"name": name, "short_name": short, "logo": _LOGO_BY_SHORT.get(short, "")},
        )


def downgrade() -> None:
    op.drop_index("ix_fx_brands_active", table_name="fx_brands")
    op.drop_index("ix_fx_brands_name", table_name="fx_brands")
    op.drop_table("fx_brands")
    op.drop_index(
        "ix_fx_distributor_pay_accounts_distributor_id",
        table_name="fx_distributor_pay_accounts",
    )
    op.drop_table("fx_distributor_pay_accounts")
