"""DIY 自选字段导出：明细（一行一个商品）与汇总（自选维度 x 指标）。

字段来源：
- 订单列：stockout_orders 表结构化列；
- 仓库/物流/地址/订单类型：订单 raw_json（旺店通原始报文，同步时已落库）；
- 品牌：明细 raw_json / 商品品类缓存；
- 品类：goods_query.php 同步到本地缓存文件（spec_no -> class_name）。
缺失字段一律留空，绝不编造。
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi.responses import StreamingResponse
from sqlalchemy import cast, func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from ..config import settings
from ..db import engine as _engine
from ..models import StockoutItem, StockoutOrder
from .wangdian import WangdianClient


def _json_text_get(col, key: str):
    """从 JSON 文本列提取顶层字符串字段，方言自适应。

    生产/群晖是 PostgreSQL（raw_json::jsonb ->> key）；本地离线版是 SQLite
    （json_extract）。查询在编译期才绑定方言，所以按 engine 方言分派。
    """
    if _engine.dialect.name == "sqlite":
        return func.json_extract(col, f"$.{key}")
    return cast(col, JSONB).op("->>")(key)

PLATFORM_NAMES = {"39": "拼多多", "75": "快手", "69": "抖音", "101": "得物"}
_options_cache: dict[tuple[str, str], tuple[float, dict[str, list[str]]]] = {}

# 仓库名口径映射：旺店通原始仓库名 -> 财务台账口径（《发货明细账》所用名称）。
# 同一仓库在两个系统里叫法不同，导出/筛选统一按台账口径展示，便于与台账逐仓对账。
# 映射关系已用 2026 年 1-4 月逐月订单数交叉验证（两边数量完全一致）。
WAREHOUSE_ALIAS = {
    "磐石-瑞士莲（京东专供)": "磐石电子商务-瑞士莲仓",
    "磐石-瑞士莲（京东专供-次品)": "磐石电子商务-瑞士莲仓(次品)",
    "红盒子-格力高磐石仓-次品": "红盒子-格力高磐石-残次仓",
    "红盒子-瑞士莲磐石仓": "红盒子-瑞士莲磐石仓(京东专供)",
}
WAREHOUSE_ALIAS_REV = {v: k for k, v in WAREHOUSE_ALIAS.items()}


def wh_out(name: str) -> str:
    """原始仓库名 -> 台账口径（导出/下拉展示用）。"""
    if not name:
        return name
    return WAREHOUSE_ALIAS.get(name, name)


def wh_in(name: str) -> str:
    """台账口径 -> 原始仓库名（筛选入参用），已支持直接传原名。"""
    if not name:
        return name
    return WAREHOUSE_ALIAS_REV.get(name, name)


def _cache_file() -> Path:
    """容器内 /app/backend 只读，缓存写到可写的数据目录；本地开发落到 backend 目录。"""
    data_root = Path(_s(settings.system_update_data_root) or "/app/data")
    try:
        data_root.mkdir(parents=True, exist_ok=True)
        probe = data_root / ".write_test"
        probe.write_text("1", "utf-8")
        probe.unlink()
        return data_root / "_wdt_goods_class.json"
    except Exception:
        return Path(__file__).resolve().parent.parent / "_wdt_goods_class.json"


def _s(v: Any) -> str:
    if v is None:
        return ""
    text = str(v).strip()
    return "" if text in {"None", "null"} else text


def _money(v: Any) -> float:
    try:
        return round(float(v or 0), 2)
    except Exception:
        return 0.0


def _fmt_dt(v: Any) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return _s(v)


def platform_name(v: Any) -> str:
    key = _s(v)
    return PLATFORM_NAMES.get(key, key)


# ---------------------------------------------------------------- 字段目录
# level: order=订单级, item=商品明细级; sum_dim=True 表示可作汇总维度
ORDER_FIELDS: list[dict[str, Any]] = [
    {"group": "订单基础", "key": "stockout_no", "label": "出库单号", "level": "order", "sum_dim": True},
    {"group": "订单基础", "key": "trade_no", "label": "系统订单号", "level": "order"},
    {"group": "订单基础", "key": "src_trade_no", "label": "原始订单号", "level": "order"},
    {"group": "订单基础", "key": "src_tids", "label": "平台单号", "level": "order"},
    {"group": "订单基础", "key": "shop_name", "label": "店铺", "level": "order", "sum_dim": True},
    {"group": "订单基础", "key": "platform_name", "label": "平台", "level": "order", "sum_dim": True},
    {"group": "订单基础", "key": "consign_time", "label": "发货时间", "level": "order"},
    {"group": "订单基础", "key": "month", "label": "核算月份", "level": "order", "sum_dim": True},
    {"group": "订单基础", "key": "status", "label": "出库单状态", "level": "order", "sum_dim": True},
    {"group": "订单基础", "key": "refund_status", "label": "退款状态", "level": "order"},
    {"group": "订单基础", "key": "goods_count", "label": "商品件数", "level": "order"},
    {"group": "仓库物流", "key": "warehouse_name", "label": "仓库", "level": "order", "sum_dim": True},
    {"group": "仓库物流", "key": "warehouse_no", "label": "仓库编号", "level": "order"},
    {"group": "仓库物流", "key": "logistics_name", "label": "物流公司", "level": "order", "sum_dim": True},
    {"group": "仓库物流", "key": "logistics_no", "label": "物流单号", "level": "order"},
    {"group": "收货地址", "key": "receiver_province", "label": "收货省份", "level": "order", "sum_dim": True},
    {"group": "收货地址", "key": "receiver_city", "label": "收货城市", "level": "order", "sum_dim": True},
    {"group": "收货地址", "key": "receiver_district", "label": "收货区县", "level": "order"},
    {"group": "收货地址", "key": "receiver_address", "label": "详细地址", "level": "order"},
    {"group": "销售金额", "key": "goods_total_amount", "label": "销售金额", "level": "order"},
    {"group": "销售金额", "key": "goods_total_cost", "label": "货品成本", "level": "order"},
    {"group": "销售金额", "key": "post_cost", "label": "物流成本", "level": "order"},
    {"group": "销售金额", "key": "package_cost", "label": "包装成本", "level": "order"},
    {"group": "销售金额", "key": "receivable", "label": "应收金额", "level": "order"},
    {"group": "销售金额", "key": "actual_receivable", "label": "实际应收", "level": "order"},
    {"group": "销售金额", "key": "paid", "label": "已付金额", "level": "order"},
    {"group": "销售金额", "key": "gross_profit", "label": "毛利(计算)", "level": "order"},
    {"group": "销售金额", "key": "gross_margin", "label": "毛利率%(计算)", "level": "order"},
]

ITEM_FIELDS: list[dict[str, Any]] = [
    {"group": "商品明细", "key": "goods_name", "label": "商品名称", "level": "item", "sum_dim": True},
    {"group": "商品明细", "key": "goods_no", "label": "商品编号", "level": "item"},
    {"group": "商品明细", "key": "spec_no", "label": "规格编号", "level": "item", "sum_dim": True},
    {"group": "商品明细", "key": "barcode", "label": "条码", "level": "item"},
    {"group": "商品明细", "key": "spec_name", "label": "规格名称", "level": "item"},
    {"group": "商品明细", "key": "brand_name", "label": "品牌", "level": "item", "sum_dim": True},
    {"group": "商品明细", "key": "class_name", "label": "品类", "level": "item", "sum_dim": True},
    {"group": "商品明细", "key": "item_count", "label": "数量", "level": "item"},
    {"group": "商品明细", "key": "sell_price", "label": "销售单价", "level": "item"},
    {"group": "商品明细", "key": "total_amount", "label": "明细金额", "level": "item"},
    {"group": "商品明细", "key": "share_amount", "label": "分摊金额", "level": "item"},
    {"group": "商品明细", "key": "cost_price", "label": "成本单价", "level": "item"},
    {"group": "商品明细", "key": "share_post", "label": "分摊邮费", "level": "item"},
    {"group": "商品明细", "key": "item_refund_status", "label": "明细退款状态", "level": "item"},
    {"group": "商品明细", "key": "item_profit", "label": "明细毛利(计算)", "level": "item"},
]

ALL_FIELDS = ORDER_FIELDS + ITEM_FIELDS

SUMMARY_DIMENSIONS = [
    {"key": "month", "label": "核算月份"},
    {"key": "shop_name", "label": "店铺"},
    {"key": "platform_name", "label": "平台"},
    {"key": "warehouse_name", "label": "仓库"},
    {"key": "logistics_name", "label": "物流公司"},
    {"key": "receiver_province", "label": "收货省份"},
    {"key": "status", "label": "出库单状态"},
    {"key": "brand_name", "label": "品牌", "item_level": True},
    {"key": "class_name", "label": "品类", "item_level": True},
    {"key": "goods_name", "label": "商品名称", "item_level": True},
    {"key": "goods_no", "label": "商品编号", "item_level": True},
    {"key": "spec_no", "label": "规格编号", "item_level": True},
    {"key": "spec_name", "label": "规格名称", "item_level": True},
]

SUMMARY_MEASURES = [
    {"key": "orders", "label": "订单数"},
    {"key": "qty", "label": "件数"},
    {"key": "sales", "label": "销售金额"},
    {"key": "avg_price", "label": "销售均价", "computed": True},
    {"key": "cost", "label": "成本金额"},
    {"key": "avg_cost", "label": "单件成本", "computed": True},
    {"key": "post", "label": "物流成本"},
    {"key": "package", "label": "包装成本", "order_only": True},
    {"key": "gross", "label": "毛利"},
    {"key": "margin", "label": "毛利率%"},
]


def export_fields_catalog() -> dict[str, Any]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for f in ALL_FIELDS:
        groups.setdefault(f["group"], []).append({"key": f["key"], "label": f["label"], "level": f["level"]})
    return {
        "groups": [{"name": k, "fields": v} for k, v in groups.items()],
        "summary_dimensions": SUMMARY_DIMENSIONS,
        "summary_measures": SUMMARY_MEASURES,
        "note": "带「(计算)」的字段由系统按固定公式计算；品类来自旺店通商品资料缓存，未同步前留空。",
    }


# ---------------------------------------------------------------- 品类缓存
def load_goods_class_cache() -> dict[str, dict[str, str]]:
    try:
        return json.loads(_cache_file().read_text("utf-8"))
    except Exception:
        return {}


def save_goods_class_cache(cache: dict[str, dict[str, str]]) -> None:
    cache_file = _cache_file()
    tmp = cache_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, sort_keys=True), "utf-8")
    tmp.replace(cache_file)


def sync_goods_class_cache(start: str = "2026-01-01") -> dict[str, Any]:
    """分窗口拉取旺店通商品资料，缓存 spec_no -> {class_name, brand_name, goods_name}。"""
    client = WangdianClient()
    if not client.configured:
        raise RuntimeError("旺店通凭证未配置")
    cache = load_goods_class_cache()
    begin = datetime.strptime(start, "%Y-%m-%d")
    end = datetime.now() - timedelta(minutes=3)
    if end <= begin:
        end = datetime.now()
    chunk_len = timedelta(days=25)
    chunks = pages = 0
    chunk_start = begin
    while chunk_start < end:
        chunk_end = min(chunk_start + chunk_len, end)
        page_no = 0
        page_size = 100
        while True:
            payload = client._request("goods_query.php", {
                "start_time": chunk_start.strftime("%Y-%m-%d %H:%M:%S"),
                "end_time": chunk_end.strftime("%Y-%m-%d %H:%M:%S"),
                "page_no": page_no,
                "page_size": page_size,
            })
            goods_list = payload.get("goods_list") or []
            pages += 1
            for g in goods_list:
                for spec in (g.get("spec_list") or []):
                    spec_no = _s(spec.get("spec_no"))
                    if not spec_no:
                        continue
                    cache[spec_no] = {
                        "class_name": _s(g.get("class_name")),
                        "brand_name": _s(g.get("brand_name")),
                        "goods_name": _s(g.get("goods_name")),
                    }
            total = int(payload.get("total_count") or 0)
            if not goods_list or len(goods_list) < page_size or page_no * page_size >= total:
                break
            page_no += 1
            if page_no > 200:
                break
        chunks += 1
        chunk_start = chunk_end
    save_goods_class_cache(cache)
    return {"ok": True, "chunks": chunks, "pages": pages, "spec_count": len(cache), "cache_file": _cache_file().name}


# ---------------------------------------------------------------- 数据组装
def _iter_filtered_orders(db: Session, start: str, end: str, filters: dict[str, str]):
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
    q = select(StockoutOrder).where(StockoutOrder.consign_time >= s, StockoutOrder.consign_time < e)
    kw = _s(filters.get("keyword"))
    if kw:
        p = f"%{kw}%"
        q = q.where(or_(
            StockoutOrder.stockout_no.ilike(p), StockoutOrder.trade_no.ilike(p),
            StockoutOrder.src_trade_no.ilike(p), StockoutOrder.src_tids.ilike(p),
            StockoutOrder.shop_name.ilike(p),
        ))
    if _s(filters.get("shop")):
        q = q.where(StockoutOrder.shop_name == filters["shop"].strip())
    if _s(filters.get("warehouse")):
        # 仓库名在 raw_json 里（主表无此列），JSON 提取过滤（方言自适应）
        # 入参可能是台账口径名（下拉里展示的名字），先还原成旺店通原始名
        q = q.where(_json_text_get(StockoutOrder.raw_json, "warehouse_name")
                    == wh_in(filters["warehouse"].strip()))
    if _s(filters.get("platform")):
        q = q.where(StockoutOrder.platform_id == filters["platform"].strip())
    if _s(filters.get("status")):
        q = q.where(StockoutOrder.status == filters["status"].strip())
    if _s(filters.get("refund_status")):
        q = q.where(StockoutOrder.refund_status == filters["refund_status"].strip())
    yield from db.scalars(q.order_by(StockoutOrder.consign_time, StockoutOrder.id)).all()


# 各汇总维度的取值来源（按 key 分组，命中后由 distinct_values 拉 distinct）
_DIM_DISTINCT_SOURCES = {
    "shop_name": lambda: StockoutOrder.shop_name,
    "platform_name": lambda: StockoutOrder.platform_id,
    "warehouse_name": lambda: _json_text_get(StockoutOrder.raw_json, "warehouse_name"),
    "logistics_name": lambda: _json_text_get(StockoutOrder.raw_json, "logistics_name"),
    "receiver_province": lambda: _json_text_get(StockoutOrder.raw_json, "receiver_province"),
    "status": lambda: StockoutOrder.status,
}
# 明细级维度（来自 StockoutItem 结构化列）
_DIM_DISTINCT_ITEM_SOURCES = {
    "goods_name": lambda: StockoutItem.goods_name,
    "spec_no": lambda: StockoutItem.spec_no,
}

# 维度值过滤缓存（按 (dim, start, end) 缓存 10 分钟）
_distinct_cache: dict[tuple[str, str, str], tuple[float, list[str]]] = {}


def distinct_values(db: Session, dim: str, start: str, end: str) -> dict[str, Any]:
    """返回指定汇总维度在日期范围内的 distinct 可选值列表。"""
    import time

    if dim == "month":
        # 月份从 consign_time 推导，扫描范围起点~终点
        s = datetime.strptime(start, "%Y-%m-%d")
        e = datetime.strptime(end, "%Y-%m-%d")
        months: set[str] = set()
        cur = datetime(s.year, s.month, 1)
        end_m = datetime(e.year, e.month, 1)
        while cur <= end_m:
            months.add(cur.strftime("%Y-%m"))
            cur = datetime(cur.year + (1 if cur.month == 12 else 0), 1 if cur.month == 12 else cur.month + 1, 1)
        return {"dim": dim, "values": sorted(months)}

    key = (dim, start, end)
    hit = _distinct_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return {"dim": dim, "values": hit[1]}

    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
    span = StockoutOrder.consign_time >= s, StockoutOrder.consign_time < e
    values: list[str] = []
    if dim in _DIM_DISTINCT_SOURCES:
        expr = _DIM_DISTINCT_SOURCES[dim]()
        values = sorted({r for r in db.scalars(select(expr).distinct().where(*span)).all() if r is not None and r != ""})
        if dim == "warehouse_name":
            values = sorted({wh_out(v) for v in values})
        if dim == "platform_name":
            values = [platform_name(v) for v in values]
            # 仅暴露已知平台，未知 ID（0/3 等）不在下拉中（用其它维度或不用筛选可见）
            known = set(PLATFORM_NAMES.values())
            values = [v for v in values if v in known]
    elif dim in _DIM_DISTINCT_ITEM_SOURCES:
        expr = _DIM_DISTINCT_ITEM_SOURCES[dim]()
        values = sorted({r for r in db.scalars(
            select(expr).distinct().join(StockoutOrder, StockoutItem.order_id == StockoutOrder.id).where(*span)
        ).all() if r})
    else:
        # 明细级字段（brand/class/spec_name）暂不开放值过滤（来源在 raw_json，全扫成本高）；返回空表示不支持
        return {"dim": dim, "values": [], "unsupported": True}

    _distinct_cache[key] = (time.time(), values)
    return {"dim": dim, "values": values}


def filter_options(db: Session, start: str, end: str) -> dict[str, list[str]]:
    """日期范围内可用的筛选值：店铺（结构化列）与仓库（raw_json 提取）。

    大范围 + JSONB 提取全扫很慢（实测 30s+），加 10 分钟内存缓存；同步期间仓库名基本稳定，可接受。
    """
    import time

    key = (start, end)
    hit = _options_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)
    span = StockoutOrder.consign_time >= s, StockoutOrder.consign_time < e
    shops = sorted({r for r in db.scalars(select(StockoutOrder.shop_name).distinct().where(*span)).all() if r})
    wh_expr = _json_text_get(StockoutOrder.raw_json, "warehouse_name")
    warehouses = sorted({wh_out(r) for r in db.scalars(select(wh_expr).distinct().where(*span)).all() if r})
    result = {"shops": shops, "warehouses": warehouses}
    _options_cache[key] = (time.time(), result)
    return result


def _order_record(o: StockoutOrder, raw: dict[str, Any], class_cache: dict[str, dict[str, str]]) -> dict[str, Any]:
    # 金额一律取落库列（同步时已做 share_amount 兜底修正），raw_json 只取仓库/物流/地址等扩展字段
    amount = _money(o.goods_total_amount)
    cost = _money(o.goods_total_cost)
    post = _money(o.post_cost)
    package = _money(o.package_cost)
    gross = round(amount - cost - post - package, 2)
    return {
        "stockout_no": _s(o.stockout_no),
        "trade_no": _s(o.trade_no),
        "src_trade_no": _s(o.src_trade_no),
        "src_tids": _s(o.src_tids),
        "shop_name": _s(o.shop_name),
        "platform_name": platform_name(o.platform_id),
        "consign_time": _fmt_dt(o.consign_time),
        "month": o.consign_time.strftime("%Y-%m") if o.consign_time else "",
        "status": _s(o.status),
        "refund_status": _s(o.refund_status),
        "goods_count": _money(o.goods_count),
        "warehouse_name": wh_out(_s(raw.get("warehouse_name"))),
        "warehouse_no": _s(raw.get("warehouse_no")),
        "logistics_name": _s(raw.get("logistics_name")),
        "logistics_no": _s(raw.get("logistics_no")),
        "receiver_province": _s(raw.get("receiver_province")),
        "receiver_city": _s(raw.get("receiver_city")),
        "receiver_district": _s(raw.get("receiver_district")),
        "receiver_address": _s(raw.get("receiver_address")),
        "goods_total_amount": amount,
        "goods_total_cost": cost,
        "post_cost": post,
        "package_cost": package,
        "receivable": _money(o.receivable),
        "actual_receivable": _money(o.actual_receivable),
        "paid": _money(o.paid),
        "gross_profit": gross,
        "gross_margin": round(gross / amount * 100, 2) if amount else 0.0,
        "_class_cache": class_cache,
    }


def _item_record(item: StockoutItem, order_rec: dict[str, Any], brand_map: dict[str, str]) -> dict[str, Any]:
    cache = order_rec["_class_cache"]
    spec_no = _s(item.spec_no)
    meta = cache.get(spec_no) or {}
    num = _money(item.goods_count)
    share = _money(item.share_amount)
    cost_total = round(_money(item.cost_price) * num, 2)
    post_share = _money(item.share_post)
    profit = round(share - cost_total - post_share, 2)
    rec = {k: v for k, v in order_rec.items() if not k.startswith("_")}
    rec.update({
        "goods_name": _s(item.goods_name),
        "goods_no": _s(item.goods_no),
        "spec_no": spec_no,
        "barcode": _s(item.barcode),
        "spec_name": _s(item.spec_name),
        "brand_name": _s(meta.get("brand_name")) or _s(brand_map.get(spec_no)),
        "class_name": _s(meta.get("class_name")),
        "item_count": num,
        "sell_price": _money(item.sell_price),
        "total_amount": _money(item.total_amount),
        "share_amount": share,
        "cost_price": _money(item.cost_price),
        "share_post": post_share,
        "item_refund_status": _s(item.refund_status),
        "item_profit": profit,
    })
    return rec


def _collect(db: Session, start: str, end: str, filters: dict[str, str], with_items: bool):
    class_cache = load_goods_class_cache()
    orders = list(_iter_filtered_orders(db, start, end, filters))
    # 批量取明细：一次 IN 查询代替逐单查询（订单多时 N+1 是导出/汇总慢的主因）
    items_by_order: dict[int, list[StockoutItem]] = {}
    if with_items and orders:
        for i in range(0, len(orders), 2000):
            chunk = orders[i:i + 2000]
            rows = db.scalars(
                select(StockoutItem)
                .where(StockoutItem.order_id.in_([o.id for o in chunk]))
                .order_by(StockoutItem.id)
            ).all()
            for it in rows:
                items_by_order.setdefault(it.order_id, []).append(it)
    for o in orders:
        try:
            raw = json.loads(o.raw_json or "{}")
        except Exception:
            raw = {}
        order_rec = _order_record(o, raw, class_cache)
        brand_map: dict[str, str] = {}
        for d in (raw.get("details_list") or []):
            sn = _s(d.get("spec_no"))
            if sn and _s(d.get("brand_name")):
                brand_map.setdefault(sn, _s(d.get("brand_name")))
        items = items_by_order.get(o.id, []) if with_items else []
        yield order_rec, items, brand_map


# ---------------------------------------------------------------- 导出
def _csv_response(
    headers: list[str],
    rows: list[list[Any]],
    filename: str,
    extra_headers: dict[str, str] | None = None,
) -> StreamingResponse:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    content = "\ufeff" + buf.getvalue()
    hdr = {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"}
    if extra_headers:
        hdr.update(extra_headers)
        exposed = ",".join(extra_headers.keys())
        hdr["Access-Control-Expose-Headers"] = exposed
    return StreamingResponse(
        iter([content.encode("utf-8")]),
        media_type="text/csv; charset=utf-8",
        headers=hdr,
    )


def _resolve_fields(fields_param: str) -> list[dict[str, Any]]:
    """按用户选择排序输出列；参数为空时给默认常用列。"""
    known = {f["key"]: f for f in ALL_FIELDS}
    keys = [k.strip() for k in fields_param.split(",") if k.strip()] if fields_param else []
    if not keys:
        keys = ["consign_time", "stockout_no", "shop_name", "platform_name", "warehouse_name",
                "logistics_name", "logistics_no", "goods_name", "spec_no", "class_name", "brand_name",
                "item_count", "share_amount", "cost_price", "item_profit"]
    out = []
    for k in keys:
        f = known.get(k)
        if f:
            out.append(f)
    return out or [known["stockout_no"]]


def export_detail(db: Session, start: str, end: str, fields_param: str, filters: dict[str, str]) -> StreamingResponse:
    fields = _resolve_fields(fields_param)
    headers = [f["label"] for f in fields]
    keys = [f["key"] for f in fields]
    rows: list[list[Any]] = []
    for order_rec, items, brand_map in _collect(db, start, end, filters, with_items=True):
        for item in items:
            rec = _item_record(item, order_rec, brand_map)
            rows.append([rec.get(k, "") for k in keys])
    name = f"DIY导出_明细_{start}_{end}.csv"
    return _csv_response(headers, rows, name)


def export_order_level(db: Session, start: str, end: str, fields_param: str, filters: dict[str, str]) -> StreamingResponse:
    fields = _resolve_fields(fields_param)
    fields = [f for f in fields if f["level"] == "order"]
    if not fields:
        fields = _resolve_fields("")
    headers = [f["label"] for f in fields]
    keys = [f["key"] for f in fields]
    rows: list[list[Any]] = []
    for order_rec, _, _ in _collect(db, start, end, filters, with_items=False):
        rows.append([order_rec.get(k, "") for k in keys])
    name = f"DIY导出_订单_{start}_{end}.csv"
    return _csv_response(headers, rows, name)


def _summary_table(
    db: Session,
    start: str,
    end: str,
    group_param: str,
    measure_param: str,
    filters: dict[str, str],
    sort_by: str = "",
    sort_dir: str = "desc",
    top: int = 0,
    dim_filters: dict[str, str] | None = None,
) -> tuple[list[str], list[list[Any]], int]:
    dim_keys = [k.strip() for k in group_param.split(",") if k.strip()]
    dim_keys = [k for k in dim_keys if k in {d["key"] for d in SUMMARY_DIMENSIONS}]
    if not dim_keys:
        dim_keys = ["month", "shop_name"]
    measure_keys = [k.strip() for k in measure_param.split(",") if k.strip()] or ["orders", "sales", "cost", "gross", "margin"]
    dims_meta = {d["key"]: d for d in SUMMARY_DIMENSIONS}
    item_dims = [k for k in dim_keys if dims_meta[k].get("item_level")]
    order_only_measures = {m["key"] for m in SUMMARY_MEASURES if m.get("order_only")}
    use_items = bool(item_dims)
    measure_keys = [k for k in measure_keys if not (use_items and k in order_only_measures)]
    # 清理维度值过滤：只保留已勾选维度中用户实际选了具体值的
    if dim_filters:
        dim_filters = {k: v for k, v in dim_filters.items() if k in dim_keys and v}
    else:
        dim_filters = {}

    groups: dict[tuple, dict[str, Any]] = {}

    def bucket(key_tuple: tuple) -> dict[str, Any]:
        return groups.setdefault(key_tuple, {"orders": 0, "qty": 0.0, "sales": 0.0, "cost": 0.0,
                                             "post": 0.0, "package": 0.0, "gross": 0.0, "_order_nos": set()})

    for order_rec, items, brand_map in _collect(db, start, end, filters, with_items=use_items):
        # 订单级维度的值过滤（不在 use_items 分支）
        if dim_filters and not use_items:
            if any(order_rec.get(k, "") != v for k, v in dim_filters.items()):
                continue
        if use_items:
            for item in items:
                rec = _item_record(item, order_rec, brand_map)
                # 明细级维度的值过滤：item 维度按明细行筛
                if dim_filters and any(rec.get(k, "") != v for k, v in dim_filters.items()):
                    continue
                key = tuple(rec.get(k, "") for k in dim_keys)
                b = bucket(key)
                if rec["stockout_no"] not in b["_order_nos"]:
                    b["_order_nos"].add(rec["stockout_no"])
                    b["orders"] += 1
                b["qty"] += rec["item_count"]
                b["sales"] += rec["share_amount"]
                b["cost"] += round(rec["cost_price"] * rec["item_count"], 2)
                b["post"] += rec["share_post"]
                b["gross"] += rec["item_profit"]
        else:
            key = tuple(order_rec.get(k, "") for k in dim_keys)
            b = bucket(key)
            b["orders"] += 1
            b["qty"] += order_rec["goods_count"]
            b["sales"] += order_rec["goods_total_amount"]
            b["cost"] += order_rec["goods_total_cost"]
            b["post"] += order_rec["post_cost"]
            b["package"] += order_rec["package_cost"]
            b["gross"] += order_rec["gross_profit"]

    label_of = {d["key"]: d["label"] for d in SUMMARY_DIMENSIONS}
    headers = [label_of[k] for k in dim_keys] + [
        next(m["label"] for m in SUMMARY_MEASURES if m["key"] == k) for k in measure_keys
    ]
    # 行值计算：先算出每行完整指标，便于按任意指标排序
    rows: list[list[Any]] = []
    for key, b in groups.items():
        out = list(key)
        for mk in measure_keys:
            v: Any = b.get(mk, 0)
            if mk == "orders":
                v = b["orders"]
            elif mk == "avg_price":
                v = round(b["sales"] / b["qty"], 2) if b["qty"] else 0.0
            elif mk == "avg_cost":
                v = round(b["cost"] / b["qty"], 2) if b["qty"] else 0.0
            elif mk == "margin":
                v = round(b["gross"] / b["sales"] * 100, 2) if b["sales"] else 0.0
            else:
                v = round(b[mk], 2)
            out.append(v)
        rows.append(out)

    # 排序：默认按所选的第一个数值指标倒序（未指定时按销售额倒序）
    sort_key = sort_by if sort_by in measure_keys else None
    if sort_key is None:
        for cand in ("sales", "orders", "qty", "gross"):
            if cand in measure_keys:
                sort_key = cand
                break
    if sort_key:
        idx = len(dim_keys) + measure_keys.index(sort_key)
        rev = sort_dir.lower() != "asc"
        rows.sort(key=lambda r: (r[idx] is None, r[idx]), reverse=rev)
    else:
        rows.sort(key=lambda r: str(r[0]))

    total_groups = len(rows)
    if top and top > 0:
        rows = rows[:top]
    return headers, rows, total_groups


def _summary_filename(filters: dict[str, str], start: str, end: str) -> str:
    tag = "_".join(
        str(filters.get(k, "")).strip()
        for k in ("warehouse", "shop")
        if str(filters.get(k, "")).strip()
    )
    return f"DIY导出_汇总{'_' + tag if tag else ''}_{start}_{end}.csv"


def export_summary(
    db: Session,
    start: str,
    end: str,
    group_param: str,
    measure_param: str,
    filters: dict[str, str],
    sort_by: str = "",
    sort_dir: str = "desc",
    top: int = 0,
    dim_filters: dict[str, str] | None = None,
) -> StreamingResponse:
    headers, rows, total_groups = _summary_table(
        db, start, end, group_param, measure_param, filters, sort_by, sort_dir, top, dim_filters
    )
    return _csv_response(
        headers,
        rows,
        _summary_filename(filters, start, end),
        extra_headers={"X-Total-Groups": str(total_groups), "X-Exported-Rows": str(len(rows))},
    )


def summary_result(
    db: Session,
    start: str,
    end: str,
    group_param: str,
    measure_param: str,
    filters: dict[str, str],
    sort_by: str = "",
    sort_dir: str = "desc",
    top: int = 0,
    dim_filters: dict[str, str] | None = None,
) -> dict[str, Any]:
    """汇总结果以 JSON 返回（报表中心页内表格用）。"""
    headers, rows, total_groups = _summary_table(
        db, start, end, group_param, measure_param, filters, sort_by, sort_dir, top, dim_filters
    )
    return {"headers": headers, "rows": rows, "total_groups": total_groups, "exported_rows": len(rows)}
