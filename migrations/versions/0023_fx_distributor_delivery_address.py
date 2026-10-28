"""0023 分销商独立「收货地址」字段。

用户 2026-10-28 要求：客户的收货地址要单独一个字段，不要跟开票要求之类的话一起塞在备注里
（原来的备注是「B2B团单导入｜收货：上海市松江区盛龙路755号7栋1楼、13817654550、林」）。

本迁移：
- `fx_distributors` 加 `delivery_address` 列；
- 旧备注里「收货：…」的地址回填到新字段；电话、收货人本来就在 联系人/电话 字段里，
  备注改回只留来源标记（送货方公司名不丢，留在备注里）。

Revision ID: 0023_fx_distributor_delivery_address
Revises: 0022_fx_pay_accounts_brands
Create Date: 2026-10-28
"""
from alembic import op
import sqlalchemy as sa

revision = "0023_fx_distributor_delivery_address"
down_revision = "0022_fx_pay_accounts_brands"
branch_labels = None
depends_on = None

# 收货信息整段：「上海威太贸易有限公司-送货地址：上海市松江区潮富路78号、186-1620-4493、王师傅」
_PHONE_RE = r"1[3-9]\d[\d-]{8,13}"


def _split_receipt(cell: str) -> tuple[str, str]:
    """把团单的收货信息整段拆成（地址, 送货公司）。

    地址 = 去掉「送货地址：」前缀、去掉电话段和像人名的段之后剩下的部分；
    送货公司 = 「送货地址：」前面那段公司名（没有就空）。
    """
    import re

    s = re.sub(r"\s+", "", cell or "")
    s = re.sub(r"[-—]*(?:自提|刮码)(?:[+＋](?:自提|刮码))*$", "", s).strip("-— ")
    shipper = ""
    m = re.search(r"^(.*?)[-—]?送货地址[:：]", s)
    if m:
        shipper = m.group(1).strip("-— ")
        s = s[m.end():]
    segs = [x.strip() for x in re.split(r"[、，]", s) if x.strip()]
    addr_parts = []
    for seg in segs:
        if re.search(_PHONE_RE, seg):
            continue
        # 收货人名：短、且不像地址（含路/号/栋/楼/地址字样的一律当人名丢掉）
        if len(seg) <= 12 and not re.search(r"\d{6,}|路|号|栋|楼|地址|区|镇|街|村", seg):
            continue
        addr_parts.append(seg)
    return "、".join(addr_parts), shipper


def upgrade() -> None:
    # 0007 里 create_all 会按当前模型建表，全新库上这一列已经在了，补列会撞名。
    # 所以先看有没有，没有才补。
    from sqlalchemy import inspect

    existing = {c["name"] for c in inspect(op.get_bind()).get_columns("fx_distributors")}
    if "delivery_address" not in existing:
        op.add_column(
            "fx_distributors",
            sa.Column("delivery_address", sa.String(length=500), nullable=False, server_default=""),
        )

    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, note FROM fx_distributors WHERE note LIKE '%收货：%'")
    ).mappings().all()
    for row in rows:
        note = row["note"] or ""
        head, _, cell = note.partition("收货：")
        addr, shipper = _split_receipt(cell)
        new_note = head.strip().rstrip("｜").strip()
        if shipper:
            new_note = (new_note + "｜送货方：" + shipper).strip("｜")
        conn.execute(
            sa.text("UPDATE fx_distributors SET delivery_address = :addr, note = :note WHERE id = :id"),
            {"addr": addr, "note": new_note, "id": row["id"]},
        )


def downgrade() -> None:
    with op.batch_alter_table("fx_distributors") as batch:
        batch.drop_column("delivery_address")
