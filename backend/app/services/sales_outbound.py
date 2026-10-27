from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import StockoutItem, StockoutOrder
from .module_hub import effective_stockout_rows, month_start


def _month_range(month: str) -> tuple[datetime, datetime]:
    start = month_start(month)
    end = datetime(start.year + (1 if start.month == 12 else 0), 1 if start.month == 12 else start.month + 1, 1)
    return start, end


def _money(v: Any) -> float:
    try:
        return float(v or 0)
    except Exception:
        return 0.0


def _gross(d: dict[str, Any]) -> float:
    return _money(d.get("goods_total_amount")) - _money(d.get("goods_total_cost")) - _money(d.get("post_cost")) - _money(d.get("package_cost"))


def sales_outbound_facets(db: Session, month: str) -> dict[str, Any]:
    start, end = _month_range(month)
    rows = list(db.scalars(select(StockoutOrder).where(
        StockoutOrder.consign_time >= start,
        StockoutOrder.consign_time < end,
    )).all())
    def vals(name: str) -> list[str]:
        return sorted({str(getattr(x, name) or "").strip() for x in rows if str(getattr(x, name) or "").strip()})
    return {
        "month": month,
        "shops": vals("shop_name"),
        "platforms": vals("platform_id"),
        "statuses": vals("status"),
        "refund_statuses": vals("refund_status"),
    }


def sales_outbound_overview(db: Session, month: str) -> dict[str, Any]:
    effective_rows = effective_stockout_rows(db, month)
    raw = {
        "goods_count": 0.0,
        "goods_total_amount": 0.0,
        "goods_total_cost": 0.0,
        "post_cost": 0.0,
        "package_cost": 0.0,
        "receivable": 0.0,
        "actual_receivable": 0.0,
        "paid": 0.0,
    }
    eff = dict(raw)
    adjusted = excluded = 0
    shop_totals: dict[str, dict[str, float]] = {}
    platform_totals: dict[str, dict[str, float]] = {}
    for row in effective_rows:
        r, e = row["raw"], row["effective"]
        if row["adjustment_ids"]:
            adjusted += 1
        if row["excluded"]:
            excluded += 1
        for k in raw:
            raw[k] += _money(r.get(k))
            if not row["excluded"]:
                eff[k] += _money(e.get(k))
        if row["excluded"]:
            continue
        for bucket, key in ((shop_totals, str(e.get("shop_name") or "未命名店铺")), (platform_totals, str(e.get("platform_id") or "未识别平台"))):
            x = bucket.setdefault(key, {"orders": 0.0, "sales": 0.0, "cost": 0.0, "gross": 0.0})
            x["orders"] += 1
            x["sales"] += _money(e.get("goods_total_amount"))
            x["cost"] += _money(e.get("goods_total_cost"))
            x["gross"] += _gross(e)
    raw_gross = raw["goods_total_amount"] - raw["goods_total_cost"] - raw["post_cost"] - raw["package_cost"]
    eff_gross = eff["goods_total_amount"] - eff["goods_total_cost"] - eff["post_cost"] - eff["package_cost"]
    def normalize_bucket(bucket: dict[str, dict[str, float]]) -> list[dict[str, Any]]:
        rows = []
        for name, x in bucket.items():
            sales = x["sales"]
            rows.append({
                "name": name,
                "orders": int(x["orders"]),
                "sales": round(sales, 2),
                "cost": round(x["cost"], 2),
                "gross": round(x["gross"], 2),
                "gross_margin_pct": round((x["gross"] / sales * 100), 2) if sales else 0.0,
            })
        return sorted(rows, key=lambda x: abs(x["sales"]), reverse=True)
    return {
        "month": month,
        "orders": len(effective_rows),
        "adjusted_orders": adjusted,
        "excluded_orders": excluded,
        "raw_totals": {k: round(v, 2) for k, v in raw.items()},
        "effective_totals": {k: round(v, 2) for k, v in eff.items()},
        "raw_gross_profit": round(raw_gross, 2),
        "effective_gross_profit": round(eff_gross, 2),
        "raw_gross_margin_pct": round(raw_gross / raw["goods_total_amount"] * 100, 2) if raw["goods_total_amount"] else 0.0,
        "effective_gross_margin_pct": round(eff_gross / eff["goods_total_amount"] * 100, 2) if eff["goods_total_amount"] else 0.0,
        "shop_breakdown": normalize_bucket(shop_totals),
        "platform_breakdown": normalize_bucket(platform_totals),
    }


def sales_outbound_orders(
    db: Session,
    month: str,
    keyword: str = "",
    shop: str = "",
    platform: str = "",
    status: str = "",
    refund_status: str = "",
    adjustment: str = "all",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    start, end = _month_range(month)
    q = select(StockoutOrder).where(StockoutOrder.consign_time >= start, StockoutOrder.consign_time < end)
    if keyword:
        p = f"%{keyword.strip()}%"
        q = q.where(or_(
            StockoutOrder.stockout_no.ilike(p),
            StockoutOrder.trade_no.ilike(p),
            StockoutOrder.src_trade_no.ilike(p),
            StockoutOrder.src_tids.ilike(p),
            StockoutOrder.shop_name.ilike(p),
        ))
    if shop:
        q = q.where(StockoutOrder.shop_name == shop)
    if platform:
        q = q.where(StockoutOrder.platform_id == platform)
    if status:
        q = q.where(StockoutOrder.status == status)
    if refund_status:
        q = q.where(StockoutOrder.refund_status == refund_status)
    orders = list(db.scalars(q.order_by(StockoutOrder.consign_time.desc(), StockoutOrder.id.desc())).all())
    effective_map = {r["stockout_no"]: r for r in effective_stockout_rows(db, month)}
    rows: list[dict[str, Any]] = []
    for o in orders:
        e = effective_map.get(o.stockout_no)
        if not e:
            continue
        is_adjusted = bool(e["adjustment_ids"])
        is_excluded = bool(e["excluded"])
        if adjustment == "adjusted" and not is_adjusted:
            continue
        if adjustment == "unadjusted" and is_adjusted:
            continue
        if adjustment == "excluded" and not is_excluded:
            continue
        eff = e["effective"]
        raw = e["raw"]
        rows.append({
            "id": o.id,
            "stockout_no": o.stockout_no,
            "trade_no": o.trade_no,
            "src_trade_no": o.src_trade_no,
            "src_tids": o.src_tids,
            "shop_no": o.shop_no,
            "shop_name": eff.get("shop_name") or o.shop_name,
            "platform_id": eff.get("platform_id") or o.platform_id,
            "consign_time": o.consign_time.isoformat() if o.consign_time else None,
            "pay_time": o.pay_time.isoformat() if o.pay_time else None,
            "status": eff.get("status") or o.status,
            "refund_status": eff.get("refund_status") or o.refund_status,
            "raw": raw,
            "effective": eff,
            "excluded": is_excluded,
            "adjusted": is_adjusted,
            "adjustment_ids": e["adjustment_ids"],
            "effective_gross_profit": round(_gross(eff), 2),
        })
    total = len(rows)
    page = max(1, page)
    page_size = min(max(10, page_size), 5000)
    start_i = (page - 1) * page_size
    page_rows = rows[start_i:start_i + page_size]
    return {
        "month": month,
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": max(1, (total + page_size - 1) // page_size),
        "rows": page_rows,
    }


def sales_outbound_detail(db: Session, stockout_no: str) -> dict[str, Any]:
    order = db.scalar(select(StockoutOrder).where(StockoutOrder.stockout_no == stockout_no))
    if not order:
        raise ValueError("销售出库单不存在")
    month = order.consign_time.strftime("%Y-%m") if order.consign_time else datetime.utcnow().strftime("%Y-%m")
    eff = next((r for r in effective_stockout_rows(db, month) if r["stockout_no"] == stockout_no), None)
    items = list(db.scalars(select(StockoutItem).where(StockoutItem.order_id == order.id).order_by(StockoutItem.id)).all())
    return {
        "id": order.id,
        "month": month,
        "stockout_no": order.stockout_no,
        "stockout_id": order.stockout_id,
        "trade_no": order.trade_no,
        "src_trade_no": order.src_trade_no,
        "src_tids": order.src_tids,
        "shop_no": order.shop_no,
        "shop_name": order.shop_name,
        "platform_id": order.platform_id,
        "consign_time": order.consign_time.isoformat() if order.consign_time else None,
        "pay_time": order.pay_time.isoformat() if order.pay_time else None,
        "status": order.status,
        "refund_status": order.refund_status,
        "raw": eff["raw"] if eff else {},
        "effective": eff["effective"] if eff else {},
        "excluded": bool(eff and eff["excluded"]),
        "adjustment_ids": eff["adjustment_ids"] if eff else [],
        "items": [{
            "id": x.id,
            "rec_id": x.rec_id,
            "spec_no": x.spec_no,
            "barcode": x.barcode,
            "goods_no": x.goods_no,
            "goods_name": x.goods_name,
            "spec_name": x.spec_name,
            "src_tid": x.src_tid,
            "src_oid": x.src_oid,
            "goods_count": _money(x.goods_count),
            "sell_price": _money(x.sell_price),
            "total_amount": _money(x.total_amount),
            "share_amount": _money(x.share_amount),
            "cost_price": _money(x.cost_price),
            "share_post": _money(x.share_post),
            "refund_status": x.refund_status,
        } for x in items],
        "raw_json": order.raw_json,
    }
