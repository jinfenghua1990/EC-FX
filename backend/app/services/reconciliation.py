from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session
from ..config import settings
from ..models import ReconResult, SettlementRow, StockoutOrder
from .module_hub import month_start


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    start = month_start(month)
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1)
    else:
        end = datetime(start.year, start.month + 1, 1)
    return start, end


def _expected(order: StockoutOrder) -> Decimal:
    return order.actual_receivable or order.receivable or order.paid or order.goods_total_amount or Decimal("0")


def run_reconciliation(db: Session, month: str) -> dict:
    start, end = _month_bounds(month)
    settlements = db.scalars(select(SettlementRow).where(SettlementRow.settle_date >= start, SettlementRow.settle_date < end)).all()
    orders = db.scalars(select(StockoutOrder).where(StockoutOrder.consign_time >= start, StockoutOrder.consign_time < end)).all()

    index: dict[str, list[StockoutOrder]] = defaultdict(list)
    for order in orders:
        for raw in (order.src_trade_no, order.src_tids, order.trade_no):
            for key in (raw or "").split(","):
                key = key.strip()
                if key:
                    index[key].append(order)

    settlement_ids = [r.id for r in settlements]
    if settlement_ids:
        db.execute(delete(ReconResult).where(ReconResult.settlement_row_id.in_(settlement_ids)))

    stats = defaultdict(int)
    for row in settlements:
        candidates = index.get(row.order_no, [])
        order = candidates[0] if len(candidates) == 1 else None
        method = "order_no_exact" if order else ""
        if not order and candidates:
            # 多个出库单命中同一平台单号时，选最接近结算净额的一单，并标记需复核。
            order = min(candidates, key=lambda x: abs(_expected(x) - row.net_amount))
            method = "order_no_multi_amount_nearest"
        if not order:
            status = "missing_stockout"
            expected = Decimal("0")
            diff = row.net_amount
            note = "结算单有记录，但销售出库未找到；可能为受限平台/API漏单或导出未补齐。"
        else:
            expected = _expected(order)
            diff = row.net_amount - expected
            if abs(diff) <= settings.recon_amount_tolerance:
                status = "matched"
                note = ""
            else:
                status = "amount_diff"
                note = "平台结算净额与旺店通应收存在差异，需核对平台费、退款、补贴或跨期。"
        db.add(ReconResult(
            settlement_row_id=row.id,
            stockout_order_id=order.id if order else None,
            status=status,
            match_method=method,
            expected_amount=expected,
            settled_amount=row.net_amount,
            difference=diff,
            note=note,
        ))
        stats[status] += 1
    db.commit()
    return {"month": month, "settlement_rows": len(settlements), **stats}


def profit_rows(db: Session, month: str) -> list[dict]:
    start, end = _month_bounds(month)
    orders = db.scalars(select(StockoutOrder).where(StockoutOrder.consign_time >= start, StockoutOrder.consign_time < end).order_by(StockoutOrder.consign_time.desc())).all()
    recon_by_order = {}
    results = db.execute(select(ReconResult, SettlementRow).join(SettlementRow, SettlementRow.id == ReconResult.settlement_row_id).where(ReconResult.stockout_order_id.is_not(None))).all()
    for recon, settlement in results:
        recon_by_order[recon.stockout_order_id] = (recon, settlement)

    data = []
    for order in orders:
        recon_pair = recon_by_order.get(order.id)
        settlement_net = recon_pair[1].net_amount if recon_pair else None
        revenue = settlement_net if settlement_net is not None else _expected(order)
        gross_profit = revenue - order.goods_total_cost - order.post_cost - order.package_cost
        margin = (gross_profit / revenue * Decimal("100")) if revenue else Decimal("0")
        data.append({
            "stockout_no": order.stockout_no,
            "platform_order_no": order.src_trade_no or order.src_tids,
            "shop_name": order.shop_name,
            "platform_id": order.platform_id,
            "consign_time": order.consign_time.isoformat(sep=" ") if order.consign_time else None,
            "revenue": float(revenue),
            "goods_cost": float(order.goods_total_cost),
            "post_cost": float(order.post_cost),
            "package_cost": float(order.package_cost),
            "gross_profit": float(gross_profit),
            "gross_margin_pct": float(margin),
            "recon_status": recon_pair[0].status if recon_pair else "unreconciled",
        })
    return data
