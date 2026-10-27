"""库存对接 + 满足率提示（单仓模式）。

用户工作流
----------
很多时候是「今天等货到了，才推销售出库单」。所以要在「出库单做好后」给一个提示：
    实际可用 = 昨天库存 + 今日入库(已入库)
    与这张单的需求量比 → 三态：
      - 满足库存（绿）：实际可用 >= 需求，现在就能发
      - 到仓待入库（橙）：实际可用 < 需求，但 实际可用 + 今日到仓待入库(已到货未入库) >= 需求
                           → 等今天这批货入完库就能发（正是用户「等今天货到才推」的节奏）
      - 库存不足（红）：即使加上到仓待入库也不够，发不了

数据来源
--------
- 昨天库存：fx_stock_snapshots（每天从旺店通 stock_query 拍一张快照，snap_date 为当天）
- 今日入库：采购单明细的 stockin_num（已入库）与 arrive_num - stockin_num（到仓待入库），
           仅取「已收货」状态(status ∈ PURCHASE_RECEIVED)且 check_time 为今天的单。
- 需求：fx_order_lines.quantity（这张出库单每行要发多少）

单仓模式：所有聚合不区分仓库（用户明确只有 1 个仓库），快照统一 warehouse=""。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    FxOrder,
    FxOrderLine,
    FxProduct,
    FxPurchaseItem,
    FxPurchaseOrder,
    FxStockSnapshot,
)
from .wangdian import WangdianClient
from .wdt_purchase import PURCHASE_RECEIVED

# 满足率三态
STATE_OK = "满足库存"
STATE_ARRIVING = "到仓待入库"
STATE_SHORT = "库存不足"
STATE_NONE = "无需求"

# 状态严重度（越大越糟），用于算整单总状态
_SEVERITY = {STATE_NONE: 0, STATE_OK: 1, STATE_ARRIVING: 2, STATE_SHORT: 3}


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value).strip() if value is not None else "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


# ---------------------------------------------------------------- 库存快照
def sync_stock_snapshot(
    db: Session,
    client: WangdianClient | None = None,
    snap_date: str = "",
    lookback_days: int = 120,
    dry_run: bool = False,
) -> dict[str, Any]:
    """从旺店通 stock_query 拉当前库存，存为 snap_date 当天的快照（单仓聚合）。

    时间窗口限制：接口单次 ≤30 天，所以把 lookback_days 切成若干 30 天窗逐个拉，
    按 spec_no 聚合（同一 SKU 取当前库存值，多窗出现的取最后一次）。

    单仓模式：warehouse 统一存 ""，不区分仓库。
    """
    client = client or WangdianClient()
    if not client.configured:
        raise RuntimeError("旺店通未配置：请在 .env 填写 WDT_SID / WDT_APPKEY / WDT_APPSECRET")

    snap_date = snap_date or datetime.now().strftime("%Y-%m-%d")
    end = datetime.now()
    start = end - timedelta(days=max(1, lookback_days))

    # 切成 ≤30 天的窗（闭区间、首尾相接）
    windows: list[tuple[datetime, datetime]] = []
    cur_end = end
    while cur_end > start:
        cur_begin = cur_end - timedelta(days=29)
        if cur_begin < start:
            cur_begin = start
        windows.append((cur_begin, cur_end))
        cur_end = cur_begin - timedelta(seconds=1)

    agg: dict[str, Decimal] = {}
    calls = 0
    for w_begin, w_end in windows:
        rows = client.stock_query(w_begin, w_end, page_size=100)
        calls += 1
        for r in rows:
            spec = _s(r.get("spec_no"))
            if not spec:
                continue
            qty = _d(r.get("stock_num") or r.get("available_num") or r.get("num"))
            agg[spec] = qty  # 当前库存，多窗出现取最后一次即可

    # spec_no → sku 反查（便于页面反查商品）
    sku_map: dict[str, str] = {}
    if agg:
        for p in db.scalars(select(FxProduct).where(FxProduct.spec_no.in_(list(agg.keys())))).all():
            if p.spec_no:
                sku_map[p.spec_no] = p.sku or ""

    rows_written = len(agg)
    if not dry_run:
        # 同日快照幂等：先清再写（单仓，warehouse=""）
        db.execute(
            FxStockSnapshot.__table__.delete().where(
                FxStockSnapshot.snap_date == snap_date, FxStockSnapshot.warehouse == ""
            )
        )
        db.flush()
        db.add_all(
            [
                FxStockSnapshot(
                    spec_no=spec,
                    sku=sku_map.get(spec, ""),
                    warehouse="",
                    snap_date=snap_date,
                    qty=qty,
                    source="wdt_stock_query",
                )
                for spec, qty in agg.items()
            ]
        )
        db.commit()

    return {
        "ok": True,
        "dry_run": dry_run,
        "snap_date": snap_date,
        "warehouse": "（单仓）",
        "api_calls": calls,
        "skus": rows_written,
        "total_qty": str(sum(agg.values())),
    }


def yesterday_stock(db: Session, spec_no: str, as_of: str = "") -> Decimal:
    """昨天库存 = 最新一张 snap_date < as_of 的快照库存（单仓）。"""
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    if not spec_no:
        return Decimal("0")
    row = db.scalar(
        select(FxStockSnapshot)
        .where(FxStockSnapshot.spec_no == spec_no)
        .where(FxStockSnapshot.snap_date < as_of)
        .order_by(FxStockSnapshot.snap_date.desc())
    )
    return _d(row.qty) if row else Decimal("0")


def today_inbound(db: Session, spec_no: str, as_of: str = "") -> tuple[Decimal, Decimal]:
    """今日入库：仅取「已收货」且 check_time 为今天的采购单明细。

    返回 (已入库数量 stockin_num, 到仓待入库数量 arrive_num - stockin_num)。
    单仓模式不区分仓库。
    """
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    if not spec_no:
        return Decimal("0"), Decimal("0")
    rows = db.execute(
        select(FxPurchaseItem)
        .join(FxPurchaseOrder, FxPurchaseItem.order_id == FxPurchaseOrder.id)
        .where(FxPurchaseOrder.status.in_(PURCHASE_RECEIVED))
        .where(FxPurchaseItem.spec_no == spec_no)
        .where(func.date(FxPurchaseOrder.check_time) == as_of)
    ).all()
    stockin = Decimal("0")
    pending = Decimal("0")
    for (item,) in rows:
        si = _d(item.stockin_num)
        ar = _d(item.arrive_num)
        stockin += si
        pend = ar - si
        if pend > 0:
            pending += pend
    return stockin, pending


# ---------------------------------------------------------------- 满足率三态
def _line_state(stock_y: Decimal, stockin: Decimal, pending: Decimal, demand: Decimal) -> tuple[str, int]:
    actual = stock_y + stockin
    if demand <= 0:
        return STATE_NONE, 0
    if actual >= demand:
        return STATE_OK, 0
    if actual + pending >= demand:
        return STATE_ARRIVING, 0
    return STATE_SHORT, 0


def fulfillment_for_order(db: Session, order_id: int, as_of: str = "") -> dict[str, Any]:
    """算一张出库单的满足率三态（按行 + 整单汇总）。"""
    as_of = as_of or datetime.now().strftime("%Y-%m-%d")
    order = db.get(FxOrder, order_id)
    if order is None:
        raise ValueError(f"订单不存在：{order_id}")
    lines = db.scalars(
        select(FxOrderLine).where(FxOrderLine.order_id == order_id)
    ).all()

    line_results: list[dict[str, Any]] = []
    worst = STATE_NONE
    for line in lines:
        spec = _s(line.spec_no)
        demand = _d(line.quantity)
        stock_y = yesterday_stock(db, spec, as_of)
        stockin, pending = today_inbound(db, spec, as_of)
        actual = stock_y + stockin
        state, _ = _line_state(stock_y, stockin, pending, demand)
        rate = (min(actual, demand) / demand * 100) if demand > 0 else 100
        if _SEVERITY[state] > _SEVERITY[worst]:
            worst = state
        line_results.append(
            {
                "spec_no": spec,
                "goods_name": _s(line.goods_name),
                "spec_name": _s(line.spec_name),
                "demand": str(demand),
                "yesterday_stock": str(stock_y),
                "today_stockin": str(stockin),
                "today_pending": str(pending),
                "actual": str(actual),
                "state": state,
                "fulfill_rate": round(float(rate), 1),
            }
        )

    return {
        "order_id": order_id,
        "order_no": order.order_no,
        "as_of": as_of,
        "overall_state": worst,
        "lines": line_results,
    }


def inventory_summary(db: Session) -> dict[str, Any]:
    """库存快照概况：最新快照日期、SKU 数、是否有昨日基线。"""
    latest = db.scalar(
        select(FxStockSnapshot.snap_date)
        .where(FxStockSnapshot.warehouse == "")
        .order_by(FxStockSnapshot.snap_date.desc())
    )
    today = datetime.now().strftime("%Y-%m-%d")
    has_baseline = False
    baseline_date = ""
    if latest:
        baseline_date = latest
        if latest < today:
            has_baseline = True
    return {
        "latest_snap_date": latest or "",
        "has_yesterday_baseline": has_baseline,
        "baseline_date": baseline_date,
        "today": today,
        "single_warehouse": True,
    }


def stock_overview(db: Session) -> dict[str, Any]:
    """最新快照的各规格现货数量（按 spec_no），给下单页显示可发货底数。

    无快照返回空表——下单页据此显示「未同步库存」而不是编一个 0。
    """
    latest = db.scalar(
        select(FxStockSnapshot.snap_date)
        .where(FxStockSnapshot.warehouse == "")
        .order_by(FxStockSnapshot.snap_date.desc())
    )
    if not latest:
        return {"snap_date": "", "qty": {}}
    rows = db.execute(
        select(FxStockSnapshot.spec_no, FxStockSnapshot.qty).where(
            FxStockSnapshot.snap_date == latest,
            FxStockSnapshot.warehouse == "",
        )
    ).all()
    return {"snap_date": latest, "qty": {spec: float(qty or 0) for spec, qty in rows}}
