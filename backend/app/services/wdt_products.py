"""从旺店通商品档案同步到 FX 商品库（fx_products）。

背景
----
红盒子供销平台的商品主数据以旺店通为准：旺店通里已有的货品/规格，不应该在本地手工重录。
本模块把 `goods_query.php` 返回的 goods_list[].spec_list[] 落库到 fx_products。

三个设计约束（都来自项目已有踩坑记录）
------------------------------------
1. **不覆盖人工维护的价格**：同步默认只补空价，不拿旺店通的零售价盖掉已维护的基准价。
   需要强制覆盖时显式传 overwrite_price=True。
2. **total_count 不可信**：大结果集时旺店通实测返回 0，用它判停会提前终止。
   一律以「本页条数 < page_size」作为翻页终止条件（与 diy_export.sync_goods_class_cache 一致）。
3. **字段名防御式读取**：旺店通不同接口版本的字段命名有差异（如 sale_price / retail_price），
   统一走 _pick() 多候选键取值，取不到留空且不报错，避免整批同步因个别字段失败而中断。

同步是**幂等**的：以 sku(=旺店通 spec_no) 为唯一键 upsert，重复执行不会产生重复商品。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FxProduct
from .wangdian import WangdianClient

# 价格字段优先级：分销场景下「批发价」才是给分销商的供货价，零售价次之。
# 踩坑记录：旺店通里大量商品 retail_price="0.0000"（根本没维护），
# 若把 0 当有效值，就会挡住真正有值的 wholesale_price ——
# 实测某窗口 59 个商品中 44 个只有批发价有值、11 个只有零售价有值。
_SPEC_PRICE_KEYS = (
    "wholesale_price", "retail_price", "market_price", "member_price",
    "custom_price1", "custom_price2", "lowest_price", "sale_price", "std_price", "price",
)
# 货品级价格字段候选（部分版本价格挂在货品上）
_GOODS_PRICE_KEYS = ("wholesale_price", "retail_price", "market_price", "lowest_price")


def _s(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _pick(row: dict[str, Any], keys: Iterable[str]) -> str:
    """按候选键顺序取第一个非空值。"""
    for key in keys:
        if key in row:
            text = _s(row.get(key))
            if text:
                return text
    return ""


def _money_or_none(text: str) -> Decimal | None:
    """0 或负数一律视为「没有价格」。

    旺店通对未维护的字段返回 "0.0000" 而不是空串；如果把它当有效值，
    会挡住后面真正有值的价格字段（这正是第一次同步全部商品基础价为 0.00 的原因）。
    """
    if not text:
        return None
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    return value.quantize(Decimal("0.01"))


def _spec_unit_price(spec: dict[str, Any], goods: dict[str, Any]) -> tuple[Decimal | None, str]:
    """返回 (价格, 命中的字段名)。优先级：规格级 > 货品级。"""
    for source, keys in ((spec, _SPEC_PRICE_KEYS), (goods, _GOODS_PRICE_KEYS)):
        for key in keys:
            price = _money_or_none(_s(source.get(key)))
            if price is not None:
                return price, key
    return None, ""


# 旺店通 goods_query.php 的硬限制（官方文档 + 实测双重确认）
MAX_WINDOW_DAYS = 30   # 单次 start_time/end_time 跨度不得超过 30 天，超了报 2180
DEFAULT_LOOKBACK_DAYS = 1825  # 默认回扫 5 年（商品档案是"最后修改时间"增量，越久越少变动）


def _time_windows(start_dt: datetime, end_dt: datetime, span_days: int = MAX_WINDOW_DAYS):
    """把 [start_dt, end_dt] 切成首尾相接、互不重叠的时间窗（闭区间，无缝隙）。"""
    cursor = end_dt
    while cursor > start_dt:
        begin = cursor - timedelta(days=span_days - 1)
        if begin < start_dt:
            begin = start_dt
        yield begin, cursor
        cursor = begin - timedelta(seconds=1)


def fetch_goods(
    client: WangdianClient,
    start: str = "",
    end: str = "",
    page_size: int = 100,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> tuple[list[dict], int]:
    """按 30 天时间窗回扫，拉取旺店通商品档案。

    两条硬约束（踩过坑，别再改成"一次性全量"）
    ------------------------------------------
    1. start_time / end_time **必填**：不传直接报 2160「为空或不是有效的时间格式」；
       只有传了 spec_no / goods_no 才允许不带时间，那样只能取单个 SKU。
    2. 单次跨度**不得超过 30 天**：传 31 天立刻报 2180。

    因此"全量同步"只能按 30 天窗口从 end 往回扫。同一 goods_no 在多个窗口重复出现
    （不同规格改动时间不同）时，按 modified / spec_modified 取最新的一份。

    返回 (goods_list, api_calls)。
    """
    now = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"

    if end:
        end_dt = datetime.strptime(end.strip()[:19], fmt) if len(end.strip()) >= 19 else datetime.strptime(end.strip(), "%Y-%m-%d")
    else:
        end_dt = now
    if start:
        start_dt = datetime.strptime(start.strip()[:19], fmt) if len(start.strip()) >= 19 else datetime.strptime(start.strip(), "%Y-%m-%d")
    else:
        start_dt = end_dt - timedelta(days=max(1, int(lookback_days or DEFAULT_LOOKBACK_DAYS)))

    if start_dt >= end_dt:
        raise ValueError("start 必须早于 end")

    size = max(1, min(int(page_size or 100), 100))  # 官方上限 100
    merged: dict[str, dict] = {}
    api_calls = 0

    for win_start, win_end in _time_windows(start_dt, end_dt):
        page_no = 0
        while True:
            payload = client._request(
                "goods_query.php",
                {
                    "page_size": size,
                    "page_no": page_no,
                    "start_time": win_start.strftime(fmt),
                    "end_time": win_end.strftime(fmt),
                },
            )
            api_calls += 1
            page_rows = payload.get("goods_list") or []
            for goods in page_rows:
                key = _s(goods.get("goods_no")) or _s(goods.get("goods_id"))
                if not key:
                    continue
                prev = merged.get(key)
                if prev is None or _s(goods.get("goods_modified") or goods.get("modified")) >= _s(
                    prev.get("goods_modified") or prev.get("modified")
                ):
                    merged[key] = goods
            # 不信任 total_count：只用本页条数判停
            if not page_rows or len(page_rows) < size:
                break
            page_no += 1
            if page_no > 500:  # 兜底，防止接口异常时无限翻页
                break

    return list(merged.values()), api_calls


def normalize_products(goods_rows: list[dict]) -> list[dict]:
    """把旺店通 goods_list 展平成「一规格一行」的商品字典。"""
    products: list[dict] = []
    for goods in goods_rows or []:
        goods_no = _s(goods.get("goods_no"))
        goods_name = _s(goods.get("goods_name"))
        class_name = _s(goods.get("class_name"))
        brand_name = _s(goods.get("brand_name"))
        unit_name = _s(goods.get("unit_name") or goods.get("unit") or goods.get("base_unit"))
        specs = goods.get("spec_list") or []
        for spec in specs:
            # sku 取规格编码 spec_no：旺店通出库/下单都以规格为最小单位，这是最可靠的唯一键
            spec_no = _s(spec.get("spec_no") or spec.get("spec_code"))
            if not spec_no:
                continue
            spec_name = _s(spec.get("spec_name") or spec.get("spec_code"))
            unit_price, price_key = _spec_unit_price(spec, goods)
            products.append(
                {
                    "sku": spec_no,
                    "spec_no": spec_no,
                    "goods_no": goods_no,
                    "name": goods_name or spec_name or spec_no,
                    "spec": spec_name,
                    "barcode": _s(spec.get("barcode")),
                    "category": class_name,
                    "brand": brand_name,
                    "unit": unit_name or "箱",
                    "base_price": unit_price,
                    "price_source": price_key,
                }
            )
    return products


def _raw_field_hint(goods_rows: list[dict]) -> dict[str, list[str]]:
    """返回旺店通原始字段名，便于首次真实同步后核对字段映射是否命中。"""
    if not goods_rows:
        return {"goods_keys": [], "spec_keys": []}
    first = goods_rows[0] or {}
    spec_list = first.get("spec_list") or []
    first_spec = spec_list[0] if spec_list else {}
    return {
        "goods_keys": sorted(str(k) for k in first.keys()),
        "spec_keys": sorted(str(k) for k in first_spec.keys()) if isinstance(first_spec, dict) else [],
    }


def upsert_products(
    db: Session,
    products: list[dict],
    overwrite_price: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """按 sku 幂等写入。dry_run=True 时只统计不落库。"""
    created = updated = price_filled = price_kept = skipped = 0
    samples: list[dict] = []
    now = datetime.utcnow()

    for item in products:
        sku = item["sku"]
        row = db.scalar(select(FxProduct).where(FxProduct.sku == sku))
        incoming_price: Decimal | None = item.get("base_price")

        if row is None:
            created += 1
            if not dry_run:
                row = FxProduct(
                    sku=sku,
                    name=item["name"],
                    spec=item["spec"],
                    unit=item["unit"],
                    base_price=incoming_price if incoming_price is not None else Decimal("0"),
                    spec_no=item["spec_no"],
                    goods_no=item["goods_no"],
                    barcode=item["barcode"],
                    category=item["category"],
                    brand=item["brand"],
                    source="wdt",
                    active=1,
                    wdt_synced_at=now,
                )
                db.add(row)
            if incoming_price is not None:
                price_filled += 1
        else:
            updated += 1
            if not dry_run:
                # 名称/规格/条码/分类以旺店通为准
                row.name = item["name"] or row.name
                row.spec = item["spec"] or row.spec
                row.unit = item["unit"] or row.unit
                row.spec_no = item["spec_no"]
                row.goods_no = item["goods_no"]
                row.barcode = item["barcode"] or row.barcode
                row.category = item["category"] or row.category
                row.brand = item["brand"] or row.brand
                row.source = "wdt"
                row.wdt_synced_at = now
                # 价格：默认只补空，避免覆盖人工维护的基准价
                if incoming_price is not None:
                    current = row.base_price or Decimal("0")
                    if overwrite_price or current <= Decimal("0"):
                        row.base_price = incoming_price
                        price_filled += 1
                    else:
                        price_kept += 1
        if len(samples) < 8:
            samples.append(
                {
                    "sku": sku,
                    "name": item["name"],
                    "spec": item["spec"],
                    "category": item["category"],
                    "price": str(incoming_price) if incoming_price is not None else "",
                }
            )

    if dry_run:
        db.rollback()
    else:
        db.commit()

    return {
        "ok": True,
        "dry_run": dry_run,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "price_filled": price_filled,
        "price_kept": price_kept,
        "total_specs": created + updated + skipped,
        "samples": samples,
    }


def sync_products_from_wdt(
    db: Session,
    start: str = "",
    end: str = "",
    overwrite_price: bool = False,
    dry_run: bool = False,
    page_size: int = 100,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """从旺店通拉取商品档案并落库。未配置凭证时抛 RuntimeError（由路由层转 400）。"""
    client = WangdianClient()
    if not client.configured:
        raise RuntimeError("旺店通凭证未配置：请在 .env 填写 WDT_SID / WDT_APPKEY / WDT_APPSECRET")

    goods_rows, api_calls = fetch_goods(
        client, start=start, end=end, page_size=page_size, lookback_days=lookback_days
    )
    products = normalize_products(goods_rows)
    result = upsert_products(db, products, overwrite_price=overwrite_price, dry_run=dry_run)
    source_counter: dict[str, int] = {}
    for item in products:
        key = item.get("price_source") or "(无价格)"
        source_counter[key] = source_counter.get(key, 0) + 1
    result.update(
        {
            "goods_count": len(goods_rows),
            "pages": api_calls,
            "price_sources": source_counter,
            "raw_fields": _raw_field_hint(goods_rows),
        }
    )
    return result
