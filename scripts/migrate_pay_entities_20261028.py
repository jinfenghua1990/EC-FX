# -*- coding: utf-8 -*-
"""2026-10-28 付款抬头重构迁移（一次性，可重复执行=幂等）。

做什么：
1. 建新表 fx_pay_entities（付款抬头主档）+ fx_distributor_pay_links（分销商↔抬头多对多）；
2. 分销商编号 D001… 重编为 H-01…（按现有编码顺序一一对应）；
3. 把旧表 fx_distributor_pay_accounts 里的账户按「名称」去重合并进抬头主档，
   同名抬头（如湖南正祥医药有限公司在多个客户名下的重复记录）只留一条主档，
   再按原归属建关联——数据不丢，只是不再重复存。
4. 同步旧 pay_accounts 逗号串列（兼容旧代码/旧导入模板）。

旧表 fx_distributor_pay_accounts 原样保留，作历史快照，不再写入。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.db import Base, engine, SessionLocal  # noqa: E402
from app.models import FxPayEntity, FxDistributorPayLink, FxDistributor  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

ENTITY_FIELDS = ("tax_no", "bank_name", "bank_branch", "bank_account", "account_holder", "account_type", "note")


def _split_names(value: str) -> list[str]:
    """逗号/顿号/中文逗号分隔 -> 去重非空列表。"""
    import re
    out: list[str] = []
    for item in re.split(r"[,，、]", str(value or "")):
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


def main() -> None:
    # 1) 建表（存在则跳过）
    Base.metadata.create_all(engine, tables=[
        FxPayEntity.__table__, FxDistributorPayLink.__table__,
    ])

    db = SessionLocal()
    try:
        # 2) 编号重编：D001→H-01、D002→H-02…（先全部读出来再写，避免中途撞唯一键）
        rows = db.scalars(select(FxDistributor)).all()
        rows.sort(key=lambda r: r.code or "")
        mapping = {}
        for i, row in enumerate(rows, start=1):
            new_code = f"H-{i:02d}"
            if row.code != new_code:
                mapping[row.code] = new_code
                row.code = new_code
        db.commit()
        print(f"[code] 重编 {len(mapping)} 条: {mapping or '（无需变更）'}")

        # 3) 抬头主档：从旧明细表按名称去重合并（只取仍存在的分销商的行）
        dist_ids = {d.id for d in db.scalars(select(FxDistributor)).all()}
        old_rows = db.execute(text(
            "SELECT id, distributor_id, account_name, tax_no, bank_name, bank_branch,"
            " bank_account, account_holder, account_type, collection_note, is_default, note"
            " FROM fx_distributor_pay_accounts ORDER BY distributor_id, id"
        )).mappings().all()

        entities: dict[str, FxPayEntity] = {e.name: e for e in db.scalars(select(FxPayEntity)).all()}
        merged_fields = 0
        for old in old_rows:
            if old["distributor_id"] not in dist_ids:
                continue  # 明细挂在已删除/已合并的分销商上：跳过，以主表 pay_accounts 串为准
            name = (old["account_name"] or "").strip()
            if not name:
                continue
            ent = entities.get(name)
            if ent is None:
                ent = FxPayEntity(name=name)
                db.add(ent)
                entities[name] = ent
            # 已有字段不覆盖，空字段用旧值补全（非空信息优先补进去）
            for f in ENTITY_FIELDS:
                new_val = (old[f] or "").strip() if f != "account_type" else (old[f] or "company")
                if getattr(ent, f) == "" and new_val and new_val != "company":
                    setattr(ent, f, new_val)
                    merged_fields += 1
            ent.account_type = ent.account_type or "company"
        db.flush()

        # 3b) 现存分销商的 pay_accounts 逗号串是最终事实来源（可能比旧明细表新）：
        #     串里有、主档还没有的抬头，补建主档。
        for dist in db.scalars(select(FxDistributor)).all():
            for name in _split_names(dist.pay_accounts):
                if name not in entities:
                    ent = FxPayEntity(name=name)
                    db.add(ent)
                    entities[name] = ent
        db.flush()

        # 4) 关联重建：以现存分销商的 pay_accounts 串为准整组重建（先清旧关联，防悬挂）。
        #    旧明细里的默认标记/收款备注在「分销商仍存在且名称对得上」时保留。
        legacy_meta: dict[tuple[int, str], dict] = {}
        for old in old_rows:
            if old["distributor_id"] in dist_ids and (old["account_name"] or "").strip():
                legacy_meta[(old["distributor_id"], old["account_name"].strip())] = old
        db.execute(text("DELETE FROM fx_distributor_pay_links"))
        link_count = 0
        for dist in db.scalars(select(FxDistributor)).all():
            names = _split_names(dist.pay_accounts)
            for i, name in enumerate(names):
                ent = entities.get(name)
                if ent is None:
                    continue
                meta = legacy_meta.get((dist.id, name), {})
                db.add(FxDistributorPayLink(
                    distributor_id=dist.id,
                    entity_id=ent.id,
                    is_default=1 if i == 0 else int(meta.get("is_default") or 0),
                    collection_note=meta.get("collection_note") or "",
                    sort_order=i,
                ))
                link_count += 1
        db.commit()

        # 5) 同步旧逗号串列（按关联顺序）
        for dist in db.scalars(select(FxDistributor)).all():
            names = [l.entity.name for l in sorted(dist.pay_links, key=lambda l: l.sort_order)]
            dist.pay_accounts = ",".join(names)[:800]
        db.commit()

        n_ent = len(entities)
        n_link = db.scalar(select(text("COUNT(*)")).select_from(text("fx_distributor_pay_links"))) or 0
        print(f"[entity] 抬头主档 {n_ent} 条（补全字段 {merged_fields} 处），关联 {n_link} 条（新增 {link_count}）")
        print("[done] 迁移完成")
    finally:
        db.close()


if __name__ == "__main__":
    main()
