"""旺店通采购单：拉取入库 + 从库内采购单计算商品采购价。

为什么单独做一条采集链路
------------------------
商品档案接口 `goods_query.php` 只有 wholesale / retail / market / lowest 四个价，
其中 `wholesale_price` 在本地被当作「品牌原价（含税）」= 牌价。但牌价不等于成本：
格力高是牌价 8 折拿货（牌价含税 5.83/盒 → 实付 4.66/盒）。
牌价与实付的折扣率各品牌不同、同一品牌也会调价，**不能用统一系数反算成本**。

采购单接口 `purchase_order_query.php` 的明细本来就存了真实下单价，
是唯一无需人工维护的成本来源。

两层结构（采购单后续要跟商品/分销订单做关联，所以原单必须先入库）
------------------------------------------------------------------
1. `sync_purchase_orders` —— 拉采购单 → 落 fx_purchase_orders / fx_purchase_items（幂等）
2. `refresh_product_purchase_prices` —— 从库内采购单算各 SKU 最新采购价 → 写 fx_products

清洗规则（都来自实测踩坑）
--------------------------
1. **标记非正常采购**：次品仓 / 备注含「残次、次品、报废、赠品…」的单据，
   价格明显偏离正常采购（例：CG202609290003「磐石-瑞士莲（京东专供-次品)」
   备注「残次42袋」，price=103 但性质是残次处理）。落库时打 abnormal 标记，
   算价时排除 —— 不删除，保留溯源。
2. **取最近一次有效采购价**，不做平均 —— 涨跌价要立即反映，平均会滞后。
3. **只认 price / tax_price**，不用 purchase_price（口径不稳定，见 models.py 注释）。
4. **同一 SKU 多价并存时保留区间**（min/max/次数），页面可提示「价格有波动」。
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.orm import Session

from ..models import FxProduct, FxPurchaseItem, FxPurchaseOrder, FxPurchasePrice
from .wangdian import WangdianClient

_FMT = "%Y-%m-%d %H:%M:%S"
MAX_WINDOW_DAYS = 30          # 接口硬限制
DEFAULT_LOOKBACK_MONTHS = 12  # 默认回扫 12 个月：覆盖一年内的采购价

# ------------------------------------------------------------------- 采购单状态码
# 旺店通 ERP 采购单状态（通用语义；90=已完成 为推断，待用户确认）。
# 用途：① 页面把原始码翻译成中文（用户问「采购单状态变化（如已收货）能不能识别」）；
#       ② 成本/入库聚合时据此判断「货到没到、价格生没生效」。
PURCHASE_ORDER_STATUS = {
    "10": "待审核",
    "20": "待采购",
    "22": "部分到货",
    "25": "部分到货(退货)",
    "30": "已到货",
    "40": "已结算",
    "50": "已作废",
    "60": "已关闭",
    "80": "待财务审核",
    "90": "已完成",
}
# 货已到、价格/库存可计入（用于「今日入库」归集）
PURCHASE_RECEIVED = {"30", "90"}
# 明确不能算成本：未审核/草稿(0)、待审核(10)、作废(50)、关闭(60)。其余状态谨慎计入。
# 2026-10-27 修正：旧逻辑用 status!="0" 过滤，但真实未生效态是 "10"(待审核)，
# 库里根本没有 "0"，导致 4 张待审核单被拿去算成本。这里把 0/10/50/60 一并排除。
PURCHASE_EXCLUDE_COST = {"0", "10", "50", "60"}


def purchase_status_label(code: str) -> str:
    return PURCHASE_ORDER_STATUS.get(_s(code), _s(code) or "未知")

# 判定「非正常采购」的关键词：命中仓库名 / 单据备注 / 供应商名 即标记 abnormal
_ABNORMAL_KEYWORDS = ("次品", "残次", "报废", "赠品", "样品", "试吃", "破损", "临期", "退货")


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value).strip() or "0")
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _dt(value: Any) -> datetime | None:
    """解析旺店通时间；未审核单的 "0000-00-00 00:00:00" 一律当空。"""
    text = _s(value)[:19]
    if not text or text.startswith("0000"):
        return None
    try:
        return datetime.strptime(text, _FMT)
    except ValueError:
        return None


def _abnormal_reason(order: dict) -> str:
    haystack = f"{_s(order.get('warehouse_name'))}|{_s(order.get('remark'))}|{_s(order.get('provider_name'))}"
    hits = [word for word in _ABNORMAL_KEYWORDS if word in haystack]
    return "/".join(hits)


def _time_windows(start_dt: datetime, end_dt: datetime, span_days: int = MAX_WINDOW_DAYS):
    """把区间切成首尾相接、互不重叠的窗口（闭区间，无缝隙）。"""
    cursor = end_dt
    while cursor > start_dt:
        begin = cursor - timedelta(days=span_days - 1)
        if begin < start_dt:
            begin = start_dt
        yield begin, cursor
        cursor = begin - timedelta(seconds=1)


def fetch_purchases(
    client: WangdianClient, start_dt: datetime, end_dt: datetime, page_size: int = 100
) -> tuple[list[dict], int]:
    """按 30 天窗口回扫采购单。返回 (orders, api_calls)。"""
    # end_time 不能晚于当前时间（报 2150），留 10 分钟余量
    latest = datetime.now() - timedelta(minutes=10)
    if end_dt > latest:
        end_dt = latest
    orders: list[dict] = []
    calls = 0
    for win_start, win_end in _time_windows(start_dt, end_dt):
        page_no = 0
        while True:
            payload = client._request(
                "purchase_order_query.php",
                {
                    "start_time": win_start.strftime(_FMT),
                    "end_time": win_end.strftime(_FMT),
                    "page_no": page_no,
                    "page_size": page_size,
                },
            )
            calls += 1
            page_rows = payload.get("purchase_list") or []
            orders.extend(page_rows)
            if not page_rows or len(page_rows) < page_size:
                break
            page_no += 1
            if page_no > 500:
                break
    return orders, calls


# ---------------------------------------------------------------- 原单入库
def upsert_purchase_orders(db: Session, orders: list[dict], dry_run: bool = False) -> dict[str, Any]:
    """把采购单 upsert 进 fx_purchase_orders / fx_purchase_items，幂等。"""
    created = updated = items_written = abnormal = 0
    now = datetime.utcnow()

    for raw in orders:
        purchase_no = _s(raw.get("purchase_no"))
        if not purchase_no:
            continue
        reason = _abnormal_reason(raw)
        is_abnormal = 1 if reason else 0
        if is_abnormal:
            abnormal += 1

        order = db.scalar(select(FxPurchaseOrder).where(FxPurchaseOrder.purchase_no == purchase_no))
        if order is None:
            order = FxPurchaseOrder(purchase_no=purchase_no)
            db.add(order)
            created += 1
        else:
            updated += 1
        order.wdt_purchase_id = _s(raw.get("purchase_id"))[:40]
        order.purchase_outer_no = _s(raw.get("purchase_outer_no"))[:60]
        order.provider_no = _s(raw.get("provider_no"))[:60]
        order.provider_name = _s(raw.get("provider_name"))[:160]
        order.warehouse_no = _s(raw.get("warehouse_no"))[:60]
        order.warehouse_name = _s(raw.get("warehouse_name"))[:160]
        order.purchaser_name = _s(raw.get("purchaser_name"))[:80]
        order.status = _s(raw.get("status"))[:16]
        order.check_time = _dt(raw.get("check_time"))
        order.created_time = _dt(raw.get("created"))
        order.modified = _dt(raw.get("modified"))
        order.expect_arrive_time = _dt(raw.get("expect_arrive_time"))
        order.goods_count = _d(raw.get("goods_count"))
        order.goods_amount = _d(raw.get("goods_amount"))
        order.tax_goods_amount = _d(raw.get("tax_goods_amount"))
        order.post_fee = _d(raw.get("post_fee"))
        order.other_fee = _d(raw.get("other_fee"))
        order.remark = _s(raw.get("remark"))[:1000]
        order.abnormal = is_abnormal
        order.abnormal_reason = reason[:200]
        order.raw_json = json.dumps(raw, ensure_ascii=False, default=str)
        order.synced_at = now

        if dry_run:
            continue
        # 明细整体重建（采购单明细可能被改数量/价格，逐条 diff 不划算）
        order.items.clear()
        db.flush()
        for detail in raw.get("details_list") or []:
            spec_no = _s(detail.get("spec_no"))
            if not spec_no:
                continue
            order.items.append(
                FxPurchaseItem(
                    rec_id=_s(detail.get("rec_id"))[:40],
                    spec_no=spec_no[:80],
                    goods_no=_s(detail.get("goods_no"))[:80],
                    goods_name=_s(detail.get("goods_name"))[:255],
                    spec_name=_s(detail.get("spec_name"))[:160],
                    brand_name=_s(detail.get("brand_name"))[:100],
                    base_unit=_s(detail.get("base_unit"))[:20],
                    purchase_unit=_s(detail.get("purchase_unit"))[:40],
                    unit_ratio=_d(detail.get("unit_ratio")),
                    num=_d(detail.get("num")),
                    num2=_d(detail.get("num2")),
                    unit_price=_d(detail.get("price") or detail.get("tax_price")),
                    amount=_d(detail.get("amount")),
                    tax_amount=_d(detail.get("tax_amount")),
                    tax_rate=_d(detail.get("tax")),
                    purchase_price=_d(detail.get("purchase_price")),
                    last_price=_d(detail.get("last_price")),
                    lowest_price=_d(detail.get("lowest_price")),
                    arrive_num=_d(detail.get("arrive_num")),
                    stockin_num=_d(detail.get("stockin_num")),
                    warehouse_id=_s(detail.get("warehouse_id"))[:40],
                )
            )
            items_written += 1

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return {
        "created": created,
        "updated": updated,
        "items": items_written,
        "abnormal_orders": abnormal,
        "total_orders": created + updated,
    }


# ---------------------------------------------------------------- 从库内算价
# 无实物仓：运营占位仓 / 只做记录不进出货的仓，价格不能当成本基准
_VIRTUAL_WAREHOUSE_KEYWORDS = ("虚拟", "仅记录", "无实物")


def _warehouse_excluded(name: str) -> tuple[bool, str]:
    for kw in _VIRTUAL_WAREHOUSE_KEYWORDS:
        if kw in (name or ""):
            return True, f"无实物仓（{kw}）"
    return False, ""


def _brand_tokens(brand: str) -> list[str]:
    """从品牌名里抽可用于匹配仓库名的关键词。

    「瑞士莲/lindt」→ ["瑞士莲", "lindt"]；「格力高/Glico」→ ["格力高", "glico"]。
    只取长度 ≥2 的片段，避免 "a" 这种把 - 所有仓库都命中。
    """
    parts = re.split(r"[/／|｜,，、\s]+", brand or "")
    return [p.strip() for p in parts if len(p.strip()) >= 2]


# 价格浮动容忍度（用户口径，2026-10-27）：
# 「采购单 = 开盘价，部分会有上下 1 元的浮动，这个是正常的」
# 「假如有一天特别便宜，说明当时拿了一个特价商品」
# 所以低于基准 1 元以上的单子是特价，不能当常规成本；高于基准 1 元以上才算真涨价。
PRICE_FLOAT_TOLERANCE = Decimal("1.00")


def _normal_price(recs: list[dict]) -> tuple[Decimal, list[Decimal]]:
    """从某仓该 SKU 的价格序列里定「正常价」，并把特价单挑出来。

    正常价 = 落在「中位数 − 1 元」以上的样本的中位数。
    用中位数而不是均价：特价单是偶发的，均价会被它拉低，中位数不会。

    样本数决定判断力（踩过的坑）：
    - 1 条：无从判断，直接当正常价
    - 2 条：**只有相差 2 倍以上才敢判特价**。否则 5.00 → 6.50 这种正常涨价
      会被误判成「6.50 是正常价、5.00 是特价」，成本反而算错。
    - 3 条以上：按「中位数 − 1 元」切分

    返回 (正常价, 特价单价格列表)。
    """
    prices = sorted(r["price"] for r in recs)
    if len(prices) == 1:
        return prices[0], []
    if len(prices) == 2:
        low, high = prices
        if low > 0 and high / low > 2:
            return high, [low]
        return prices[-1], []

    median = prices[len(prices) // 2]
    floor = median - PRICE_FLOAT_TOLERANCE
    normal = [p for p in prices if p >= floor]
    specials = [p for p in prices if p < floor]
    if not normal:
        return median, specials
    return normal[len(normal) // 2], specials


def _bucket_by_warehouse(records: list[dict]) -> dict[str, list[dict]]:
    """把采购明细按 (sku, 仓库) 归集，每仓取该仓的常规成本价。

    records 每条: {sku, warehouse, provider, date, price, amount, purchase_no}

    返回 {sku: [{warehouse, provider, price, date, purchase_no, times,
                 total_amount, min_price, max_price, normal_price,
                 special_count, special_note}, ...]}

    为什么必须分仓：同一 SKU 在不同仓的价格是并行存在的不同渠道价，
    混成一串再取「最新」会得出根本不存在的涨跌结论（瑞士莲 600g 的 88 与 103）。

    取价规则（成本基准 = 正常价，不是简单取最新）：
    - 最新一次在正常价 ±1 元内 → 用最新价（浮动而已，无所谓）
    - 最新一次比正常价低 1 元以上 → **特价拿货**，成本仍按正常价算，
      否则利润会被一次性便宜货虚高
    - 最新一次比正常价高 1 元以上 → 真涨价，用最新价（成本必须跟着涨）
    """
    cells: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in records:
        if not r.get("sku"):
            continue
        cells[(r["sku"], r.get("warehouse") or "")].append(r)

    by_sku: dict[str, list[dict]] = defaultdict(list)
    for (sku, wh), recs in cells.items():
        recs.sort(key=lambda x: x["date"])
        latest = recs[-1]
        prices = [x["price"] for x in recs]
        normal_price, specials = _normal_price(recs)

        cost_price = latest["price"]
        special_note = ""
        if specials and latest["price"] < normal_price - PRICE_FLOAT_TOLERANCE:
            # 最近一次就是特价单：成本按正常价，避免这一笔把利润算虚
            cost_price = normal_price
            special_note = (
                f"最近一次 {latest['price']} 低于正常价 {normal_price} 超过 "
                f"{PRICE_FLOAT_TOLERANCE} 元，按特价处理，成本采用正常价"
            )

        by_sku[sku].append(
            {
                "warehouse": wh,
                "provider": latest.get("provider") or "",
                "price": cost_price,
                "latest_price": latest["price"],
                "normal_price": normal_price,
                "date": latest["date"],
                "purchase_no": latest.get("purchase_no") or "",
                "times": len(recs),
                "total_amount": sum((x.get("amount") or Decimal("0") for x in recs), Decimal("0")),
                "min_price": min(prices),
                "max_price": max(prices),
                "special_count": len(specials),
                "special_note": special_note,
            }
        )
    return by_sku


def _pick_primary(
    whs: list[dict], brand: str = "", cost_warehouse: str = ""
) -> tuple[dict, str]:
    """从某 SKU 的各仓价里挑「成本基准仓」。

    规则（按优先级）：
    0. 商品手工指定了 cost_warehouse → 强制用该仓；该仓没有这个 SKU 的记录时
       退回自动规则，并返回告警（不静默改成别的仓，避免口径悄悄变了）
    1. 仓库名包含该商品品牌关键词（「红盒子-格力高磐石仓」之于格力高）
       —— 品牌专属仓就是该品牌的主进货仓，比金额更可靠
    2. 该 SKU 在该仓累计采购金额最大 —— 买得最多的渠道即主渠道
    3. 全仓被排除时仍返回金额最大仓，但带告警让人工确认
    """
    if not whs:
        raise ValueError("_pick_primary 需要至少一个仓的记录")

    if cost_warehouse:
        hit = next((e for e in whs if e["warehouse"] == cost_warehouse), None)
        if hit:
            return hit, ""
        fallback, _ = _pick_primary(whs, brand, "")
        return fallback, f"指定成本仓「{cost_warehouse}」无该商品采购记录，已退回自动选仓"

    usable = [e for e in whs if not e.get("excluded")]
    pool = usable or whs
    warn = "" if usable else "该商品只在无实物仓有采购记录，价格仅供参考"

    tokens = _brand_tokens(brand)
    if tokens:
        matched = [
            e for e in pool
            if any(t.lower() in (e["warehouse"] or "").lower() for t in tokens)
        ]
        if matched:
            return max(matched, key=lambda e: e["total_amount"]), warn

    return max(pool, key=lambda e: e["total_amount"]), warn


def _aggregate_records(
    records: list[dict],
    brand_of=None,
    cost_warehouse_of=None,
) -> dict[str, dict]:
    """按仓分组 + 选主仓，产出可直接写回 fx_products 的结构。

    返回 {sku: {price, date, purchase_no, provider, warehouse, times,
                min_price, max_price, varies, warehouses, warn, by_warehouse,
                special_count, suspect, suspect_reason}}
    """
    by_sku = _bucket_by_warehouse(records)
    result: dict[str, dict] = {}
    for sku, whs in by_sku.items():
        for entry in whs:
            excluded, reason = _warehouse_excluded(entry["warehouse"])
            entry["excluded"] = 1 if excluded else 0
            entry["exclude_reason"] = reason

        brand = brand_of(sku) if brand_of else ""
        cost_wh = cost_warehouse_of(sku) if cost_warehouse_of else ""
        primary, warn = _pick_primary(whs, brand=brand, cost_warehouse=cost_wh)
        for entry in whs:
            entry["is_primary"] = 1 if entry is primary else 0

        # varies = 价格是否真的变过：看所有仓的 min~max 总跨度。
        # 不能只看各仓「成本价」之间的差 —— 同仓内 5.00 涨到 7.50 会被漏掉。
        # 跨度 ≤1 元属正常浮动（用户口径），不报。
        all_low = min(e["min_price"] for e in whs)
        all_high = max(e["max_price"] for e in whs)
        result[sku] = {
            "spec_no": sku,
            "brand": brand,
            "price": primary["price"],
            "latest_price": primary.get("latest_price", primary["price"]),
            "normal_price": primary.get("normal_price", primary["price"]),
            "date": primary["date"],
            "purchase_no": primary["purchase_no"],
            "provider": primary["provider"],
            "warehouse": primary["warehouse"],
            "times": primary["times"],
            "min_price": all_low,
            "max_price": all_high,
            "varies": (all_high - all_low) > PRICE_FLOAT_TOLERANCE,
            "warehouses": len(whs),
            "special_count": sum(e.get("special_count", 0) for e in whs),
            "special_note": primary.get("special_note", ""),
            "warn": warn,
            "suspect": 0,
            "suspect_reason": "",
            "by_warehouse": whs,
        }

    _mark_suspect(result)
    return result


def _mark_suspect(result: dict[str, dict]) -> None:
    """标记可疑采购价：**同一个 SKU 在同一个仓**的价格极值比 > 3 倍。

    为什么只用「同仓内」这一个判据
    ------------------------------
    试过「同品牌 + 同净含量跨商品比价」，误报太多，已放弃：
    - 格力高云朵百奇 S40G 7.20 元 vs 菜园小饼 40g 1.69 元，差 4.3 倍，
      但两者是完全不同的产品线，价格本来就该差这么多；
    - 纸箱耗材的名字里带「轻松熊194g」「126g-2盒」，会被误当成同规格商品，
      0.54 元的纸箱跟 16.88 元的礼盒被判定成「相差 31 倍」。

    真正可靠的信号在单个 SKU 内部：用户口径是「±1 元浮动属正常」，
    同一个货、同一个仓出现 3 倍价差，只可能是源单录错或极端特价。

    只标记，不改价 —— 改价要人工判断（到底是录错还是真的特价）。
    """
    for sku, info in result.items():
        bad = [
            e for e in info["by_warehouse"]
            if e["min_price"] > 0 and e["max_price"] / e["min_price"] > 3
        ]
        if not bad:
            continue
        entry = max(bad, key=lambda x: x["max_price"] / x["min_price"])
        ratio = entry["max_price"] / entry["min_price"]
        info["suspect"] = 1
        info["suspect_reason"] = (
            f"同仓（{entry['warehouse']}）价格 {entry['min_price']}~{entry['max_price']}，"
            f"相差 {ratio:.1f} 倍，超出 ±{PRICE_FLOAT_TOLERANCE} 元正常浮动，疑源单录错"
        )


def aggregate_prices_from_orders(orders: list[dict]) -> dict[str, dict]:
    """从旺店通原始采购单（内存态）算各 spec_no 的采购价，按仓分组。

    dry_run 时用这条路径 —— 还没落库，不能查表，因此拿不到商品的品牌与
    手工指定仓，只能走「金额最大仓」这条自动规则。
    """
    records: list[dict] = []
    for order in orders:
        if _abnormal_reason(order):
            continue
        # 待审核(10)/作废(50)/关闭(60) 价格未生效，不能算成本
        if _s(order.get("status")) in PURCHASE_EXCLUDE_COST or not _s(order.get("status")):
            continue
        odate = _dt(order.get("check_time")) or _dt(order.get("created"))
        if odate is None:
            continue
        warehouse = _s(order.get("warehouse_name"))
        provider = _s(order.get("provider_name"))
        purchase_no = _s(order.get("purchase_no"))
        for detail in order.get("details_list") or []:
            spec_no = _s(detail.get("spec_no"))
            if not spec_no:
                continue
            price = _d(detail.get("price") or detail.get("tax_price"))
            if price <= 0:
                continue
            amount = _d(detail.get("amount")) or price * _d(detail.get("num"))
            records.append(
                {
                    "sku": spec_no,
                    "warehouse": warehouse,
                    "provider": provider,
                    "date": odate,
                    "price": price,
                    "amount": amount,
                    "purchase_no": purchase_no,
                }
            )
    return _aggregate_records(records)


def aggregate_purchase_prices(db: Session, months: int = DEFAULT_LOOKBACK_MONTHS) -> dict[str, dict]:
    """从库内采购单算各 SKU 的采购价，**按仓库分组**后再选成本仓。

    只统计 abnormal=0 的单；某 SKU 只有异常单时不产出记录（宁可留空也不写错价）。

    品牌关键词与手工指定仓从 fx_products 读，用来决定哪个仓算主仓
    （各品牌取价逻辑不同：格力高只认格力高仓，瑞士莲多渠道需人工定）。
    """
    since = datetime.now() - timedelta(days=max(1, int(months)) * 30)
    rows = db.execute(
        select(FxPurchaseOrder, FxPurchaseItem)
        .join(FxPurchaseItem, FxPurchaseItem.order_id == FxPurchaseOrder.id)
        .where(FxPurchaseOrder.abnormal == 0)
        .where(FxPurchaseOrder.status.notin_(PURCHASE_EXCLUDE_COST))  # 待审核/作废/关闭价未生效
        .where(
            or_(
                FxPurchaseOrder.check_time >= since,
                (FxPurchaseOrder.check_time.is_(None)) & (FxPurchaseOrder.created_time >= since),
            )
        )
    ).all()

    records: list[dict] = []
    for order, item in rows:
        if item.unit_price <= 0:
            continue
        odate = order.check_time or order.created_time
        if odate is None:
            continue
        records.append(
            {
                "sku": item.spec_no,
                "warehouse": order.warehouse_name or "",
                "provider": order.provider_name or "",
                "date": odate,
                "price": item.unit_price,
                "amount": item.amount or Decimal("0"),
                "purchase_no": order.purchase_no or "",
            }
        )

    # 品牌 / 手工成本仓：一次读全表（几百行），避免逐 SKU 查询
    specs = {r["sku"] for r in records}
    brand_map: dict[str, str] = {}
    costwh_map: dict[str, str] = {}
    if specs:
        for product in db.scalars(
            select(FxProduct).where(
                or_(FxProduct.sku.in_(list(specs)), FxProduct.spec_no.in_(list(specs)))
            )
        ).all():
            for key in (product.sku, product.spec_no):
                if key and key in specs:
                    brand_map.setdefault(key, product.brand or "")
                    costwh_map.setdefault(key, product.cost_warehouse or "")

    return _aggregate_records(
        records,
        brand_of=brand_map.get,
        cost_warehouse_of=costwh_map.get,
    )


def refresh_product_purchase_prices(
    db: Session,
    months: int = DEFAULT_LOOKBACK_MONTHS,
    dry_run: bool = False,
    apply_to_base_price: str = "none",
    prices: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """把采购单价写回 fx_products。

    apply_to_base_price 决定是否顺带修正 base_price（品牌原价）：
    - "none"（默认）：不动 base_price，只写采购价
    - "empty"：只补 base_price=0 的商品（补齐零价，不覆盖已维护的）
    - "overwrite"：强制用采购价覆盖 base_price

    prices 传入时直接用（dry_run 走内存聚合，此时库里还没采购单）。
    """
    if apply_to_base_price not in {"none", "empty", "overwrite"}:
        raise ValueError("apply_to_base_price 只支持 none / empty / overwrite")

    if prices is None:
        prices = aggregate_purchase_prices(db, months=months)
    specs = list(prices.keys())
    matched: dict[str, FxProduct] = {}
    for chunk_start in range(0, len(specs), 400):
        chunk = specs[chunk_start : chunk_start + 400]
        for row in db.scalars(
            select(FxProduct).where(or_(FxProduct.sku.in_(chunk), FxProduct.spec_no.in_(chunk)))
        ).all():
            for key in (row.sku, row.spec_no):
                if key and key in prices:
                    matched.setdefault(key, row)

    updated = base_filled = base_overwritten = price_changed = multi_warehouse = 0
    warned = 0
    samples: list[dict] = []
    price_rows: list[FxPurchasePrice] = []
    for spec_no, info in prices.items():
        product = matched.get(spec_no)
        if product is None:
            continue
        new_price = info["price"].quantize(Decimal("0.01"))
        old_price = _d(product.purchase_price)
        # 只有超过浮动容忍度才算「价格变了」：±1 元内的变化是正常浮动（用户口径），
        # 每次同步都报「N 个商品变价」会让真正的涨跌被淹没
        if old_price > 0 and abs(old_price - new_price) > PRICE_FLOAT_TOLERANCE:
            price_changed += 1
        base_now = _d(product.base_price)
        if info.get("warehouses", 1) > 1:
            multi_warehouse += 1
        if info.get("warn"):
            warned += 1

        if not dry_run:
            product.purchase_price = new_price
            product.purchase_date = info["date"]
            product.purchase_no = (info.get("purchase_no") or "")[:40]
            product.purchase_warehouse = (info.get("warehouse") or "")[:160]
            product.purchase_provider = (info.get("provider") or "")[:160]
            if apply_to_base_price == "overwrite":
                product.base_price = new_price
            elif apply_to_base_price == "empty" and base_now <= 0:
                product.base_price = new_price
        if apply_to_base_price == "overwrite":
            base_overwritten += 1
        elif apply_to_base_price == "empty" and base_now <= 0:
            base_filled += 1
        updated += 1

        # 各仓明细（供页面查看「这个 SKU 在不同仓分别什么价」）
        for entry in info.get("by_warehouse", []):
            price_rows.append(
                FxPurchasePrice(
                    sku=product.sku,
                    brand=(product.brand or "")[:100],
                    warehouse_name=(entry["warehouse"] or "")[:160],
                    provider_name=(entry.get("provider") or "")[:160],
                    price=_d(entry["price"]).quantize(Decimal("0.0001")),
                    purchase_date=entry["date"],
                    purchase_no=(entry.get("purchase_no") or "")[:40],
                    total_amount=_d(entry.get("total_amount")).quantize(Decimal("0.0001")),
                    times=int(entry.get("times") or 0),
                    min_price=_d(entry.get("min_price")).quantize(Decimal("0.0001")),
                    max_price=_d(entry.get("max_price")).quantize(Decimal("0.0001")),
                    is_primary=int(entry.get("is_primary") or 0),
                    excluded=int(entry.get("excluded") or 0),
                    exclude_reason=(entry.get("exclude_reason") or "")[:200],
                    updated_at=datetime.utcnow(),
                )
            )

        if len(samples) < 15:
            samples.append(
                {
                    "sku": product.sku,
                    "name": product.name[:28],
                    "purchase_price": str(new_price),
                    "base_price": str(base_now),
                    "gap": str((base_now - new_price).quantize(Decimal("0.01"))) if base_now > 0 else "",
                    "date": info["date"].strftime("%Y-%m-%d"),
                    "times": info["times"],
                    "warehouse": info.get("warehouse", ""),
                    "provider": info.get("provider", ""),
                    "warehouses": info.get("warehouses", 1),
                    "varies": info["varies"],
                }
            )

    # 明细价表全量重建：行数不多（百级），整表替换比逐条 upsert 简单且不会留脏行
    if not dry_run and price_rows:
        db.execute(delete(FxPurchasePrice))
        db.add_all(price_rows)

    if dry_run:
        db.rollback()
    else:
        db.commit()

    unmatched = [s for s in prices if s not in matched]
    return {
        "dry_run": dry_run,
        "apply_to_base_price": apply_to_base_price,
        "specs_with_price": len(prices),
        "updated": updated,
        "price_changed": price_changed,
        "base_filled": base_filled,
        "base_overwritten": base_overwritten,
        # 多仓 / 告警统计：一眼看出有多少商品存在跨仓价差、多少需要人工确认
        "multi_warehouse": multi_warehouse,
        "warned": warned,
        "price_rows": len(price_rows),
        "unmatched_count": len(unmatched),
        "unmatched_samples": unmatched[:20],
        "samples": samples,
    }


def list_purchase_prices(db: Session, sku: str = "", limit: int = 2000) -> list[dict]:
    """查询按仓分组的采购价（页面用来展示「这个商品在各仓分别什么价」）。"""
    stmt = select(FxPurchasePrice).order_by(
        FxPurchasePrice.sku, FxPurchasePrice.is_primary.desc(), FxPurchasePrice.total_amount.desc()
    )
    if sku.strip():
        stmt = stmt.where(FxPurchasePrice.sku == sku.strip())
    rows = db.scalars(stmt.limit(max(1, min(int(limit), 5000)))).all()
    return [
        {
            "sku": r.sku,
            "brand": r.brand,
            "warehouse": r.warehouse_name,
            "provider": r.provider_name,
            "price": str(_d(r.price).quantize(Decimal("0.01"))),
            "date": r.purchase_date.strftime("%Y-%m-%d") if r.purchase_date else "",
            "purchase_no": r.purchase_no,
            "total_amount": str(_d(r.total_amount).quantize(Decimal("0.01"))),
            "times": r.times,
            "min_price": str(_d(r.min_price).quantize(Decimal("0.01"))),
            "max_price": str(_d(r.max_price).quantize(Decimal("0.01"))),
            "is_primary": r.is_primary,
            "excluded": r.excluded,
            "exclude_reason": r.exclude_reason,
        }
        for r in rows
    ]


def set_cost_warehouse(db: Session, sku: str = "", brand: str = "", warehouse: str = "") -> dict[str, Any]:
    """指定成本仓：按 SKU 或按品牌统一下发（各品牌取价逻辑不同，需要能整批改）。

    warehouse 传空字符串 = 恢复自动选仓。
    改完要重新跑 refresh_product_purchase_prices 才会生效。
    """
    if not sku.strip() and not brand.strip():
        raise ValueError("必须指定 sku 或 brand 之一")

    stmt = select(FxProduct)
    if sku.strip():
        stmt = stmt.where(or_(FxProduct.sku == sku.strip(), FxProduct.spec_no == sku.strip()))
    else:
        stmt = stmt.where(FxProduct.brand.like(f"%{brand.strip()}%"))
    rows = db.scalars(stmt).all()
    for row in rows:
        row.cost_warehouse = warehouse.strip()[:160]
    db.commit()
    return {"updated": len(rows), "warehouse": warehouse.strip(), "scope": sku.strip() or brand.strip()}


# ---------------------------------------------------------------- 组合入口
def sync_purchase_orders(
    db: Session,
    months: int = DEFAULT_LOOKBACK_MONTHS,
    dry_run: bool = False,
    refresh_prices: bool = True,
    apply_to_base_price: str = "none",
    start: str = "",
    end: str = "",
    page_size: int = 100,
    client: WangdianClient | None = None,
) -> dict[str, Any]:
    """一次完成：拉采购单 → 入库 → （可选）刷新商品采购价。"""
    client = client or WangdianClient()
    if not client.configured:
        raise RuntimeError("旺店通凭证未配置：请在 .env 填写 WDT_SID / WDT_APPKEY / WDT_APPSECRET")

    now = datetime.now()
    end_dt = datetime.strptime(end.strip()[:19], _FMT) if end.strip() else now
    if start.strip():
        start_dt = datetime.strptime(start.strip()[:19], _FMT)
    else:
        start_dt = end_dt - timedelta(days=max(1, int(months)) * 30)

    orders, api_calls = fetch_purchases(client, start_dt, end_dt, page_size=page_size)
    result: dict[str, Any] = {
        "required": True,
        "range": {"start": start_dt.strftime("%Y-%m-%d"), "end": end_dt.strftime("%Y-%m-%d")},
        "fetched_orders": len(orders),
        "api_calls": api_calls,
        "dry_run": dry_run,
    }
    result.update(upsert_purchase_orders(db, orders, dry_run=dry_run))
    if refresh_prices:
        # dry_run 时库里还没有这批采购单，必须用内存里的原单聚合
        prices = aggregate_prices_from_orders(orders) if dry_run else None
        result["prices"] = refresh_product_purchase_prices(
            db,
            months=months,
            dry_run=dry_run,
            apply_to_base_price=apply_to_base_price,
            prices=prices,
        )
    return result


def list_purchase_orders(
    db: Session,
    month: str = "",
    keyword: str = "",
    abnormal: int = -1,
    limit: int = 200,
    brand: str = "",
) -> list[dict]:
    """采购单列表（供页面查询；后续关联商品/分销订单也从这里接）。

    品牌筛选（brand）
    -----------------
    品牌只落在明细 `fx_purchase_items.brand_name` 上，单据头没有品牌字段，
    所以只能筛「明细里含该品牌货品的采购单」（EXISTS 子查询）。
    命中之后，列表的数量/金额、明细弹窗的内容都收窄到该品牌 —— 否则一张
    混品牌采购单会把别的品牌的金额算进来，页面上怎么加都对不上。
    """
    brand = (brand or "").strip()
    stmt = select(FxPurchaseOrder).order_by(FxPurchaseOrder.check_time.desc(), FxPurchaseOrder.id.desc())
    if month:
        start = datetime.strptime(f"{month}-01", "%Y-%m-%d")
        end = (start + timedelta(days=32)).replace(day=1)
        stmt = stmt.where(FxPurchaseOrder.check_time >= start).where(FxPurchaseOrder.check_time < end)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                FxPurchaseOrder.purchase_no.like(like),
                FxPurchaseOrder.provider_name.like(like),
                FxPurchaseOrder.warehouse_name.like(like),
                # 单号/供应商之外，也要能按「品牌、货品名、商家编码」搜到单
                exists(
                    select(1)
                    .select_from(FxPurchaseItem)
                    .where(
                        FxPurchaseItem.order_id == FxPurchaseOrder.id,
                        or_(
                            FxPurchaseItem.brand_name.like(like),
                            FxPurchaseItem.goods_name.like(like),
                            FxPurchaseItem.spec_no.like(like),
                        ),
                    )
                ),
            )
        )
    if abnormal in (0, 1):
        stmt = stmt.where(FxPurchaseOrder.abnormal == abnormal)
    if brand:
        stmt = stmt.where(
            exists(
                select(1)
                .select_from(FxPurchaseItem)
                .where(
                    FxPurchaseItem.order_id == FxPurchaseOrder.id,
                    FxPurchaseItem.brand_name == brand,
                )
            )
        )
    rows = db.scalars(stmt.limit(max(1, min(int(limit), 1000)))).all()

    result: list[dict] = []
    for row in rows:
        items = db.scalars(select(FxPurchaseItem).where(FxPurchaseItem.order_id == row.id)).all()
        # 选了品牌：数量/金额/明细都只算这个品牌，和页面上的品牌视图保持一致
        scoped = [i for i in items if i.brand_name == brand] if brand else items
        if brand:
            goods_count = sum((i.num for i in scoped), Decimal("0"))
            goods_amount = sum((i.amount for i in scoped), Decimal("0"))
            tax_amount = sum((i.tax_amount for i in scoped), Decimal("0"))
        else:
            goods_count = row.goods_count
            goods_amount = row.goods_amount
            tax_amount = row.tax_goods_amount
        result.append(
            {
                "id": row.id,
                "purchase_no": row.purchase_no,
                "provider_name": row.provider_name,
                "warehouse_name": row.warehouse_name,
                "purchaser_name": row.purchaser_name,
                "status": row.status,
                "status_label": purchase_status_label(row.status),
                "check_time": row.check_time.strftime("%Y-%m-%d %H:%M") if row.check_time else "",
                "goods_count": str(goods_count),
                "goods_amount": str(goods_amount),
                "tax_goods_amount": str(tax_amount),
                "remark": row.remark,
                "abnormal": bool(row.abnormal),
                "abnormal_reason": row.abnormal_reason,
                "item_count": len(scoped),
                "brand_scoped": bool(brand),
                # 这张单涉及哪些品牌（选品牌时只看得到该品牌，列表多一列品牌维度）
                "brands": sorted({i.brand_name for i in scoped if i.brand_name}),
                "items": [
                    {
                        "spec_no": item.spec_no,
                        "goods_name": item.goods_name,
                        "spec_name": item.spec_name,
                        "brand_name": item.brand_name,
                        "num": str(item.num),
                        "num2": str(item.num2),
                        "purchase_unit": item.purchase_unit,
                        "unit_ratio": str(item.unit_ratio),
                        "unit_price": str(item.unit_price),
                        "amount": str(item.amount),
                        "last_price": str(item.last_price),
                        "lowest_price": str(item.lowest_price),
                    }
                    for item in scoped
                ],
            }
        )
    return result


def purchase_order_summary(db: Session, brand: str = "") -> dict[str, Any]:
    """顶部统计卡片。

    选了品牌就整套跟着品牌走：单数/异常数按「含该品牌货品的采购单」算；
    金额换成该品牌明细的含税合计（goods_amount 是不含税口径，
    跨品牌相加对不上，混品牌单还会把别的品牌的货算进来）。
    """
    brand = (brand or "").strip()
    stmt = select(FxPurchaseOrder)
    if brand:
        stmt = stmt.where(
            exists(
                select(1)
                .select_from(FxPurchaseItem)
                .where(
                    FxPurchaseItem.order_id == FxPurchaseOrder.id,
                    FxPurchaseItem.brand_name == brand,
                )
            )
        )
    rows = db.scalars(stmt).all()
    total_amount = sum((_d(r.goods_amount) for r in rows), Decimal("0"))
    total_tax = sum((_d(r.tax_goods_amount) for r in rows), Decimal("0"))
    abnormal = sum(1 for r in rows if r.abnormal)
    latest = max((r.check_time for r in rows if r.check_time), default=None)
    summary: dict[str, Any] = {
        "orders": len(rows),
        "abnormal_orders": abnormal,
        "goods_amount": str(total_amount.quantize(Decimal("0.01"))),
        "tax_goods_amount": str(total_tax.quantize(Decimal("0.01"))),
        "latest_check_time": latest.strftime("%Y-%m-%d %H:%M") if latest else "",
        "brand": brand,
    }
    if brand and rows:
        agg = db.execute(
            select(
                func.coalesce(func.sum(FxPurchaseItem.amount), 0),
                func.coalesce(func.sum(FxPurchaseItem.tax_amount), 0),
                func.coalesce(func.sum(FxPurchaseItem.num), 0),
            )
            .where(FxPurchaseItem.order_id.in_([r.id for r in rows]))
            .where(FxPurchaseItem.brand_name == brand)
        ).one()
        summary["brand_goods_amount"] = str(_d(agg[0]).quantize(Decimal("0.01")))
        summary["brand_tax_amount"] = str(_d(agg[1]).quantize(Decimal("0.01")))
        summary["brand_goods_count"] = str(_d(agg[2]))
    return summary


def purchase_brands(db: Session) -> list[dict[str, Any]]:
    """采购明细里真实出现过的品牌，给页面的品牌下拉用。

    用明细的品牌而不是商品档案的品牌：采购单里才会出现「磐石-瑞士莲（京东专供）」
    这类供货口径，跟档案里的品牌名不一定完全一致，下拉必须跟数据对得上。
    """
    rows = db.execute(
        select(FxPurchaseItem.brand_name, func.count(FxPurchaseItem.id))
        .where(FxPurchaseItem.brand_name != "")
        .group_by(FxPurchaseItem.brand_name)
        .order_by(func.count(FxPurchaseItem.id).desc(), FxPurchaseItem.brand_name.asc())
    ).all()
    return [{"name": name, "items": count} for name, count in rows]


def purchase_months(db: Session) -> list[str]:
    """有采购单的月份（YYYY-MM，倒序），给月份下拉用。

    只查 check_time 一列，不碰明细 —— 页面原来靠「再拉 1000 张单+全部明细」
    去凑月份，切一次品牌就多跑一次重查询，这里顺手替掉。
    """
    rows = db.scalars(
        select(FxPurchaseOrder.check_time).where(FxPurchaseOrder.check_time.isnot(None))
    ).all()
    return sorted({r.strftime("%Y-%m") for r in rows}, reverse=True)
