"""旺店通实时可发库存：严格按仓库编码 + SKU 规格编码查询，不依赖历史快照。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from ..models import FxOrder, FxProduct
from .wangdian import WangdianClient


class LiveInventoryError(ValueError):
    """无法确定真实库存时，阻止将缺失数据伪装成 0 或有货。"""


def _decimal(value: object, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (ValueError, InvalidOperation, TypeError) as exc:
        raise LiveInventoryError(f"旺店通 {label} 库存数量无效") from exc
    if not result.is_finite():
        raise LiveInventoryError(f"旺店通 {label} 库存数量无效")
    return result


def _warehouse(client: WangdianClient, warehouse_no: str) -> dict:
    code = str(warehouse_no or "").strip()
    if not code:
        raise LiveInventoryError("请先选择旺店通发货仓库")
    if not client.configured:
        raise LiveInventoryError("旺店通接口未配置，无法验证实时库存")
    records = client.warehouses()  # 仓库档案是实时来源，不能靠前端或硬编码列表兜底
    found = next((
        r for r in records
        if str(r.get("warehouse_no") or "").strip() == code
        and str(r.get("is_disabled") or "0") != "1"
    ), None)
    if found is None:
        raise LiveInventoryError(f"仓库 {code} 不在旺店通启用仓库中，请重新选择")
    return {"warehouse_no": code,
            "warehouse_name": str(found.get("name") or found.get("warehouse_name") or code)}


def _stock_row(rows: list[dict], warehouse_no: str, spec_no: str) -> dict:
    if not rows:
        # 精确检索成功但该仓无规格记录：可发 0；与网络失败不同。
        return {"available": Decimal("0"), "physical": Decimal("0"), "found": False}
    matched = [
        r for r in rows if str(r.get("warehouse_no") or "").strip() == warehouse_no
        and str(r.get("spec_no") or "").strip() == spec_no
    ]
    if len(matched) != len(rows):
        raise LiveInventoryError(f"旺店通返回了不属于 {warehouse_no}/{spec_no} 的库存，已停止核验")
    if len(matched) != 1:
        raise LiveInventoryError(f"旺店通返回 {warehouse_no}/{spec_no} 重复库存记录，不能擅自相加")
    row = matched[0]
    # 企业版官方字段实际拼写为 avaliable_num（少一个 a），部分版本用 available_num。
    # stock_num 是仓内实物库存，不能作为可发量兜底，否则占用量会被误认为可卖。
    raw = next((row[k] for k in ("avaliable_num", "available_num")
                if row.get(k) is not None and str(row[k]).strip() != ""), None)
    if raw is None:
        raise LiveInventoryError(f"旺店通未返回 {spec_no} 的可发库存字段")
    physical = _decimal(row.get("stock_num"), "实物")
    return {"available": max(_decimal(raw, "可发"), Decimal("0")),
            "physical": physical, "found": True}


def preview_live_inventory(db: Session, warehouse_no: str, lines: list[dict],
                           client: WangdianClient | None = None) -> dict:
    """查询指定仓库当前的 SKU 可发量。quantity=0 可用于下单页商品预览。

    同一 SKU 在多行出现时先合并需求，避免分别和全额库存比较而漏判超卖。
    此查询为只读即时核验，不会预占旺店通库存。
    """
    if not lines:
        raise LiveInventoryError("请提供要查询的商品")
    if len(lines) > 40:
        raise LiveInventoryError("单次实时库存查询最多 40 行，请缩小商品范围")
    client = client or WangdianClient()
    warehouse = _warehouse(client, warehouse_no)
    requested: list[dict] = []
    demands: dict[str, Decimal] = {}
    for line in lines:
        try:
            product_id = int(line.get("product_id") or 0)
        except (TypeError, ValueError) as exc:
            raise LiveInventoryError("商品编号无效") from exc
        product = db.get(FxProduct, product_id)
        if product is None or not product.active:
            raise LiveInventoryError(f"商品 {product_id} 不存在或已停用")
        spec = str(product.spec_no or product.sku or "").strip()
        if not spec:
            raise LiveInventoryError(f"商品 {product.sku} 缺少旺店通规格编码")
        qty = _decimal(line.get("quantity", 0), "下单")
        if qty < 0:
            raise LiveInventoryError("下单数量不得为负数")
        demands[spec] = demands.get(spec, Decimal("0")) + qty
        requested.append({"product_id": product_id, "spec_no": spec,
                          "sku": product.sku, "quantity": qty})

    if len(demands) > 40:
        raise LiveInventoryError("一次最多核验 40 个 SKU")
    stocks = {}
    for spec in demands:
        rows = client.stock_by_spec_warehouse(warehouse["warehouse_no"], spec)
        stocks[spec] = _stock_row(rows, warehouse["warehouse_no"], spec)

    items = []
    for entry in requested:
        detail = stocks[entry["spec_no"]]
        available = detail["available"]
        demand = demands[entry["spec_no"]]
        items.append({
            "product_id": entry["product_id"], "sku": entry["sku"],
            "spec_no": entry["spec_no"], "quantity": str(entry["quantity"]),
            "demand_total": str(demand),
            "physical": str(detail["physical"]), "available": str(available),
            "found": detail["found"], "enough": demand <= available,
        })
    return {
        **warehouse, "source": "wdt_live", "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": items, "enough": all(x["enough"] for x in items),
        "checked_skus": len(stocks),
    }


def verify_order_for_push(db: Session, order: FxOrder, client: WangdianClient) -> dict:
    """真正推单前重新查询旺店通库存；库存不足或接口异常均不允许假定充足。"""
    if not (order.wdt_warehouse_no or "").strip():
        raise LiveInventoryError("订单未指定旺店通仓库编码，无法实时核验库存")
    lines: list[dict] = []
    for line in order.lines or []:
        if not line.product_id:
            raise LiveInventoryError("订单存在没有商品档案的明细，无法按旺店通 SKU 查库存")
        lines.append({"product_id": line.product_id, "quantity": line.quantity})
    data = preview_live_inventory(db, order.wdt_warehouse_no, lines, client)
    if not data["enough"]:
        shorts = [
            f"{item['spec_no']} 需{item['demand_total']}、可发{item['available']}"
            for item in data["items"] if not item["enough"]
        ]
        raise LiveInventoryError(
            f"旺店通仓库 {data['warehouse_name']}（{order.wdt_warehouse_no}）库存不足："
            + "；".join(dict.fromkeys(shorts))
        )
    return data


def fulfillment_live_for_order(db: Session, order_id: int,
                               client: WangdianClient | None = None) -> dict:
    order = db.get(FxOrder, int(order_id))
    if order is None:
        raise LiveInventoryError("订单不存在")
    if not order.wdt_warehouse_no:
        raise LiveInventoryError("该订单没有旺店通仓库编码，请先在订单头选择真实仓库")
    data = preview_live_inventory(
        db, order.wdt_warehouse_no,
        [{"product_id": line.product_id, "quantity": line.quantity}
         for line in order.lines or []],
        client,
    )
    results = []
    for line in data["items"]:
        requested = Decimal(line["demand_total"])
        available = Decimal(line["available"])
        state = "满足库存" if line["enough"] else "库存不足"
        rate = round(float(min(available, requested) / requested * 100), 1) if requested > 0 else 100
        results.append({**line, "actual": line["available"], "demand": line["quantity"],
                        "state": state, "fulfill_rate": rate})
    return {
        "order_id": order.id, "order_no": order.order_no, "warehouse_no": data["warehouse_no"],
        "warehouse_name": data["warehouse_name"], "checked_at": data["checked_at"],
        "source": "wdt_live", "overall_state": "满足库存" if data["enough"] else "库存不足",
        "lines": results,
    }
