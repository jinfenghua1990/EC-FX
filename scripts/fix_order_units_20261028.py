#!/usr/bin/env python3
"""把历史分销订单的「数量/单价」从箱口径统一成基本单位（件）。

背景（2026-10-28 由旺店通销售出库单反推确认）：
  历史订单是业务按「箱」录的：quantity = 箱数，unit_price = 元/箱。
  但系统其余环节（旺店通推送 num、出库对账、库存比对、新下单页）全部按基本单位「件」。
  结果是同一张单在系统里数量/单价相差一个箱规（如 36 倍），件数核对必然报错。

本脚本按商品档案的箱规做无损换算（小计金额一分不动）：
  quantity   ← quantity × 箱规
  unit_price ← 原小计 ÷ 新数量（4 位小数，与库字段精度一致）
  subtotal   保持不变

用法：
  python scripts/fix_order_units_20261028.py           # 预演，只打印不写库
  python scripts/fix_order_units_20261028.py --apply   # 实际写库
"""
from __future__ import annotations

import sys
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import FxOrder, FxProduct  # noqa: E402

Q4 = Decimal("0.0001")


def main() -> int:
    apply = "--apply" in sys.argv
    db = SessionLocal()
    changed_lines = changed_orders = skipped = 0
    try:
        products = {p.spec_no: p for p in db.scalars(select(FxProduct)).all()}
        for order in db.scalars(select(FxOrder).order_by(FxOrder.order_no)).all():
            touched = False
            for line in order.lines:
                prod = products.get(line.goods_no or line.spec_no)
                pack = int(prod.pack_qty or 0) if prod else 0
                qty = Decimal(line.quantity or 0)
                if pack <= 1 or qty <= 0:
                    skipped += 1
                    continue
                new_qty = qty * pack
                base = Decimal(line.subtotal or 0)
                if base <= 0:
                    base = qty * Decimal(line.unit_price or 0)
                new_price = (base / new_qty).quantize(Q4, rounding=ROUND_HALF_UP)
                print(
                    f"{order.order_no} {line.goods_no}: "
                    f"{qty} 箱 × {line.unit_price}/箱  →  {new_qty} 件 × {new_price}/件"
                    f"（箱规 {pack}，小计 {line.subtotal or base} 不变）"
                )
                if apply:
                    line.quantity = new_qty
                    line.unit_price = new_price
                changed_lines += 1
                touched = True
            if touched:
                changed_orders += 1
        if apply:
            db.commit()
        print(f"\n{'已写库' if apply else '预演（未写库）'}：订单 {changed_orders} 张，明细 {changed_lines} 行，跳过（无箱规）{skipped} 行")
        if not apply:
            print("确认无误后加 --apply 再跑一次")
        return 0
    except Exception as exc:  # pragma: no cover
        db.rollback()
        print(f"失败：{exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
