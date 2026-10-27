"""销售出库手工调整：在旺店通 RAW 数据之外记录「生效值」的调整。

从原 module_hub.py 抽出的独立模块 —— 核销算账那部分已下线，
但销售出库的「调整 → 生效值 → 汇总」是真实在用功能，单独保留。
"""
from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ManualAdjustment, StockoutOrder

D = Decimal

# 可参与数值调整的出库单字段
NUMERIC_STOCKOUT_FIELDS = {
    "goods_count", "goods_total_amount", "goods_total_cost", "receivable",
    "actual_receivable", "paid", "post_cost", "package_cost",
}
# 可参与文本替换的字段
TEXT_STOCKOUT_FIELDS = {"shop_name", "shop_no", "platform_id", "status", "refund_status"}
ALLOWED_ADJUST_OPS = {"add", "replace", "multiply", "exclude", "include", "text_replace"}
ALLOWED_TARGET_TYPES = {"order", "shop", "platform", "month"}


def normalize_month(value: str) -> str:
    """把各种月份写法归一成 'YYYY-MM'（月份补零）。

    支持 '2026-9' / '2026-09' / '2026/9' / '2026.9' / '2026年9月'。
    无法识别时原样返回，避免破坏未知格式。
    """
    s = (value or "").strip()
    if not s:
        return s
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", s)
    if not m:
        m = re.fullmatch(r"(\d{4})\s*年\s*(\d{1,2})\s*月", s)
    if not m:
        return s
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"


def month_start(value: str) -> datetime:
    """返回某月 1 号 0 点；兼容 '2026-9' 这类非补零写法，避免 strptime 崩溃。"""
    return datetime.strptime(normalize_month(value) + "-01", "%Y-%m-%d")


def create_adjustment(db: Session, payload: dict[str, Any]) -> ManualAdjustment:
    target_type = str(payload.get("target_type") or "order")
    operation = str(payload.get("operation") or "add")
    field_name = str(payload.get("field_name") or "")
    if target_type not in ALLOWED_TARGET_TYPES:
        raise ValueError(f"target_type 仅支持: {', '.join(sorted(ALLOWED_TARGET_TYPES))}")
    if operation not in ALLOWED_ADJUST_OPS:
        raise ValueError(f"operation 仅支持: {', '.join(sorted(ALLOWED_ADJUST_OPS))}")
    if operation not in {"exclude", "include"} and field_name not in NUMERIC_STOCKOUT_FIELDS | TEXT_STOCKOUT_FIELDS:
        raise ValueError("不支持调整该字段")
    if operation in {"add", "replace", "multiply"} and field_name not in NUMERIC_STOCKOUT_FIELDS:
        raise ValueError("add/replace/multiply 只能用于金额/数量字段")
    if operation == "text_replace" and field_name not in TEXT_STOCKOUT_FIELDS:
        raise ValueError("text_replace 只能用于文本字段")
    row = ManualAdjustment(
        module="sales-outbound",
        target_type=target_type,
        target_key=str(payload.get("target_key") or "").strip(),
        period=str(payload.get("period") or "").strip(),
        field_name=field_name,
        operation=operation,
        decimal_value=(D(str(payload["decimal_value"])) if payload.get("decimal_value") not in (None, "") else None),
        text_value=str(payload.get("text_value") or ""),
        reason=str(payload.get("reason") or ""),
        active=1 if payload.get("active", True) else 0,
    )
    if not row.target_key:
        raise ValueError("target_key 不能为空")
    if not row.reason:
        raise ValueError("手工调整必须填写原因")
    if operation in {"add", "replace", "multiply"} and row.decimal_value is None:
        raise ValueError("数值调整必须填写 decimal_value")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_adjustments(db: Session, period: str = "") -> list[ManualAdjustment]:
    q = select(ManualAdjustment).where(ManualAdjustment.module == "sales-outbound")
    if period:
        q = q.where((ManualAdjustment.period == period) | (ManualAdjustment.period == ""))
    return list(db.scalars(q.order_by(ManualAdjustment.id.desc())).all())


def _matches_adjustment(adj: ManualAdjustment, order: StockoutOrder, month: str) -> bool:
    if not adj.active:
        return False
    if adj.period and adj.period != month:
        return False
    if adj.target_type == "order":
        return adj.target_key == order.stockout_no
    if adj.target_type == "shop":
        return adj.target_key == order.shop_name
    if adj.target_type == "platform":
        return adj.target_key == order.platform_id
    if adj.target_type == "month":
        return adj.target_key == month
    return False


def effective_stockout_rows(db: Session, month: str) -> list[dict[str, Any]]:
    start = month_start(month)
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1)
    else:
        end = datetime(start.year, start.month + 1, 1)
    orders = list(db.scalars(select(StockoutOrder).where(StockoutOrder.consign_time >= start, StockoutOrder.consign_time < end).order_by(StockoutOrder.id)).all())
    adjustments = list_adjustments(db, month)
    out = []
    for order in orders:
        raw = {
            "goods_count": D(str(order.goods_count or 0)),
            "goods_total_amount": D(str(order.goods_total_amount or 0)),
            "goods_total_cost": D(str(order.goods_total_cost or 0)),
            "receivable": D(str(order.receivable or 0)),
            "actual_receivable": D(str(order.actual_receivable or 0)),
            "paid": D(str(order.paid or 0)),
            "post_cost": D(str(order.post_cost or 0)),
            "package_cost": D(str(order.package_cost or 0)),
            "shop_name": order.shop_name,
            "shop_no": order.shop_no,
            "platform_id": order.platform_id,
            "status": order.status,
            "refund_status": order.refund_status,
        }
        eff = dict(raw)
        excluded = False
        applied: list[int] = []
        for adj in sorted((a for a in adjustments if _matches_adjustment(a, order, month)), key=lambda a: a.id):
            if adj.operation == "exclude":
                excluded = True
            elif adj.operation == "include":
                excluded = False
            elif adj.operation == "add" and adj.field_name in NUMERIC_STOCKOUT_FIELDS:
                eff[adj.field_name] = D(str(eff.get(adj.field_name) or 0)) + D(str(adj.decimal_value or 0))
            elif adj.operation == "replace" and adj.field_name in NUMERIC_STOCKOUT_FIELDS:
                eff[adj.field_name] = D(str(adj.decimal_value or 0))
            elif adj.operation == "multiply" and adj.field_name in NUMERIC_STOCKOUT_FIELDS:
                eff[adj.field_name] = D(str(eff.get(adj.field_name) or 0)) * D(str(adj.decimal_value or 1))
            elif adj.operation == "text_replace" and adj.field_name in TEXT_STOCKOUT_FIELDS:
                eff[adj.field_name] = adj.text_value
            applied.append(adj.id)
        out.append({
            "stockout_no": order.stockout_no,
            "consign_time": order.consign_time.isoformat() if order.consign_time else None,
            "raw": {k: float(v) if isinstance(v, Decimal) else v for k, v in raw.items()},
            "effective": {k: float(v) if isinstance(v, Decimal) else v for k, v in eff.items()},
            "excluded": excluded,
            "adjustment_ids": applied,
        })
    return out


def stockout_adjustment_summary(db: Session, month: str) -> dict[str, Any]:
    rows = effective_stockout_rows(db, month)
    numeric = sorted(NUMERIC_STOCKOUT_FIELDS)
    raw_totals = {k: D("0") for k in numeric}
    eff_totals = {k: D("0") for k in numeric}
    excluded = 0
    changed = 0
    for row in rows:
        if row["excluded"]:
            excluded += 1
        if row["adjustment_ids"]:
            changed += 1
        for k in numeric:
            raw_totals[k] += D(str(row["raw"].get(k) or 0))
            if not row["excluded"]:
                eff_totals[k] += D(str(row["effective"].get(k) or 0))
    return {
        "month": month, "orders": len(rows), "adjusted_orders": changed, "excluded_orders": excluded,
        "raw_totals": {k: float(v) for k, v in raw_totals.items()},
        "effective_totals": {k: float(v) for k, v in eff_totals.items()},
        "delta": {k: float(eff_totals[k] - raw_totals[k]) for k in numeric},
    }
