from __future__ import annotations
import hashlib
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..config import settings
from ..models import StockoutItem, StockoutOrder


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _d(value: Any) -> Decimal:
    try:
        return Decimal(_s(value) or "0")
    except InvalidOperation:
        return Decimal("0")


def _dt(value: Any) -> datetime | None:
    text = _s(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def sign_params(params: dict[str, Any], appsecret: str) -> str:
    """旺店通标准 API Sign 算法。appsecret 不参与参数排序，只在末尾拼接。"""
    chunks: list[str] = []
    for key in sorted(params):
        value = _s(params[key])
        key_len = len(key.encode("utf-8"))
        val_len = len(value.encode("utf-8"))
        chunks.append(f"{key_len:02d}-{key}:{val_len:04d}-{value}")
    raw = ";".join(chunks) + appsecret
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


class WangdianClient:
    def __init__(self) -> None:
        self.sid = settings.wdt_sid
        self.appkey = settings.wdt_appkey
        self.appsecret = settings.wdt_appsecret
        self.base_url = settings.wdt_base_url.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.sid and self.appkey and self.appsecret)

    def _request(self, endpoint: str, business_params: dict[str, Any]) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("旺店通未配置：请填写 WDT_SID / WDT_APPKEY / WDT_APPSECRET")
        params: dict[str, Any] = {
            "sid": self.sid,
            "appkey": self.appkey,
            "timestamp": int(time.time()),
            **business_params,
        }
        params["sign"] = sign_params(params, self.appsecret)
        url = f"{self.base_url}/{endpoint}"
        with httpx.Client(timeout=settings.wdt_timeout_seconds) as client:
            response = client.post(url, data={k: _s(v) for k, v in params.items()})
            response.raise_for_status()
            payload = response.json()
        if int(payload.get("code", -1)) != 0:
            raise RuntimeError(f"旺店通接口失败：{payload.get('code')} {payload.get('message', '')}")
        return payload

    def test_connection(self) -> dict[str, Any]:
        """用查询店铺接口做最小只读连通性验证。"""
        return self._request("shop.php", {})

    def stockouts(self, start: datetime, end: datetime, page_size: int = 100) -> list[dict[str, Any]]:
        if end <= start:
            raise ValueError("end 必须晚于 start")
        if end - start > timedelta(days=30):
            raise ValueError("旺店通单次时间跨度不能超过30天")
        rows: list[dict[str, Any]] = []
        page_no = 0
        while True:
            payload = self._request(
                "stockout_order_query_trade.php",
                {
                    "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                    "page_no": page_no,
                    "page_size": page_size,
                    "is_by_modified": 1,
                },
            )
            page_rows = payload.get("stockout_list") or []
            rows.extend(page_rows)
            # total_count 不可信：大结果集时旺店通实测返回 0，若用它判停会在第 2 页提前终止（曾导致每月只同步到 ~200 单）
            if not page_rows or len(page_rows) < page_size:
                break
            page_no += 1
        return rows

    def warehouses(self, page_size: int = 100) -> list[dict[str, Any]]:
        """拉取旺店通仓库档案（warehouse_query.php，只读）。

        下单页只允许选红盒子-格力高磐石仓，但仓库编号必须以旺店通档案为准，
        不能在代码里写死显示名；拉取失败时页面保留一个安全的 Glico 兜底。
        """
        rows: list[dict[str, Any]] = []
        page_no = 0
        while True:
            payload = self._request(
                "warehouse_query.php",
                {"page_no": page_no, "page_size": page_size},
            )
            page_rows = payload.get("warehouses") or []
            rows.extend(page_rows)
            if not page_rows or len(page_rows) < page_size:
                break
            page_no += 1
        return rows

    def sales_trade_push(self, trade: dict[str, Any]) -> dict[str, Any]:
        """推送/更新销售单（分销订单 → 旺店通）。

        旺店通 openapi2 `trade_push.php` 的正式报文：
        `shop_no` + `trade_list`（JSON 数组，单条包含 `order_list`）。
        同一 `tid` 会更新原单，因此重推分销订单不会造成重复。

        方法名保留 `sales_trade_push` 是为了兼容既有调用和测试；实际接口已按
        官方文档更正，不再使用不存在的 `sales_trade_push.php` / `trade` 单对象。
        """
        if not trade.get("shop_no"):
            raise RuntimeError("旺店通销售单推送缺少 shop_no，请配置 WDT_SHOP_NO")
        trade_list = trade.get("trade_list")
        if not trade_list:
            # 兼容旧调用方：如果直接传一条 trade（含 tid），包成 trade_list
            if trade.get("tid"):
                trade_list = [{k: v for k, v in trade.items() if k != "shop_no"}]
            else:
                raise RuntimeError("销售单推送缺少 trade_list / tid")
        return self._request(
            "trade_push.php",
            {
                "shop_no": _s(trade.get("shop_no")),
                "switch": "0",  # 非严格模式：先保证能建单，必填字段仍全部显式传
                "trade_list": json.dumps(trade_list, ensure_ascii=False, default=str),
            },
        )

    def purchases(
        self,
        start: datetime,
        end: datetime,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """拉取采购单（purchase_order_query.php），含明细价格。

        三条硬约束（实测，别再改成一次性全量）
        -------------------------------------
        1. start_time / end_time **必填且跨度不得超过 30 天**，超了报 2180
           （与 goods_query.php 同一套限制）。
        2. end_time **不能晚于当前时间**，否则报 2150。调用方需自带安全余量
           （本项目统一用 now - 10min）。
        3. total_count 不可信 —— 与出库单一致，只用「本页条数 < page_size」判停。

        明细里的价格字段（实测口径）
        ---------------------------
        - price / tax_price：含税单价，按基本单位（盒/袋），**已是折后实付价**。
          例：格力高百奇蜜桃 55g 牌价含税 5.83/盒，8 折拿货 → 采购单 price=4.6641。
          与本地 FxProduct.base_price 同口径，40 个有价 SKU 中 35 个精确命中。
        - purchase_price：历史不含税价，口径不稳定（瑞士莲 600g：price=103、
          purchase_price=92.42，比值 1.1145 非 13%），**不要当成本用**。
        - last_price：上一次采购价；lowest_price：历史最低价（仅供比价参考）。
        """
        if end <= start:
            raise ValueError("end 必须晚于 start")
        if end - start > timedelta(days=30):
            raise ValueError("旺店通单次时间跨度不能超过30天")
        rows: list[dict[str, Any]] = []
        page_no = 0
        while True:
            payload = self._request(
                "purchase_order_query.php",
                {
                    "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                    "page_no": page_no,
                    "page_size": page_size,
                },
            )
            page_rows = payload.get("purchase_list") or []
            rows.extend(page_rows)
            if not page_rows or len(page_rows) < page_size:
                break
            page_no += 1
            if page_no > 500:  # 兜底，防止接口异常时无限翻页
                break
        return rows

    def stock_query(
        self,
        start: datetime,
        end: datetime,
        page_size: int = 100,
    ) -> list[dict[str, Any]]:
        """库存查询（stock_query.php）：返回各 SKU 的当前实际/可用库存。

        ⚠️ 返回的是「当前」库存，不是历史。要算「昨天库存 + 今天入库 = 实际可用」，
        必须由调用方按日期自己拍快照（见 services/inventory.py 的 sync_stock_snapshot）。

        单次时间跨度限制同其他接口（≤30 天），用 modified 时间窗切页。
        防御式解析：不同账号字段名可能略有差异，优先取 stock_num（实际库存），
        否则 available_num（可用），再否则 num。
        """
        if end <= start:
            raise ValueError("end 必须晚于 start")
        if end - start > timedelta(days=30):
            raise ValueError("旺店通单次时间跨度不能超过30天")
        rows: list[dict[str, Any]] = []
        page_no = 0
        while True:
            payload = self._request(
                "stock_query.php",
                {
                    "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                    "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                    "page_no": page_no,
                    "page_size": page_size,
                },
            )
            page_rows = payload.get("stock_list") or []
            rows.extend(page_rows)
            if not page_rows or len(page_rows) < page_size:
                break
            page_no += 1
            if page_no > 500:
                break
        return rows

def upsert_stockouts(db: Session, rows: list[dict[str, Any]], source: str = "wdt_api") -> tuple[int, int]:
    order_count = item_count = 0
    for row in rows:
        stockout_no = _s(row.get("stockout_no") or row.get("order_no"))
        if not stockout_no:
            continue
        order = db.scalar(select(StockoutOrder).where(StockoutOrder.stockout_no == stockout_no))
        if order is None:
            order = StockoutOrder(stockout_no=stockout_no)
            db.add(order)
            db.flush()
        order.source = source
        order.stockout_id = _s(row.get("stockout_id"))
        order.trade_no = _s(row.get("trade_no") or row.get("src_order_no"))
        order.src_trade_no = _s(row.get("src_trade_no"))
        order.src_tids = _s(row.get("src_tids") or row.get("src_trade_no"))
        order.shop_no = _s(row.get("shop_no"))
        order.shop_name = _s(row.get("shop_name"))
        order.platform_id = _s(row.get("platform_id"))
        order.consign_time = _dt(row.get("consign_time"))
        order.pay_time = _dt(row.get("pay_time"))
        order.modified = _dt(row.get("modified"))
        order.status = _s(row.get("status"))
        order.refund_status = _s(row.get("refund_status"))
        order.goods_count = _d(row.get("goods_count"))
        details_list = row.get("details_list") or []
        # WDT 主单 goods_total_amount 不可信：实测既有恒为 0 的，也有 6.09 亿的垃圾值
        # （CK202604081089 主单 6.096 亿，明细 share_amount 合计 50800 与应收分毫不差）。
        # 一律以明细 share_amount 合计为准，主单金额只在无明细时兜底。
        goods_amount = sum(
            (_d(d.get("share_amount") or d.get("total_amount")) for d in details_list),
            Decimal("0"),
        )
        if not goods_amount:
            goods_amount = _d(row.get("goods_total_amount"))
        order.goods_total_amount = goods_amount
        order.goods_total_cost = _d(row.get("goods_total_cost"))
        order.receivable = _d(row.get("receivable"))
        order.actual_receivable = _d(row.get("actual_receivable"))
        order.paid = _d(row.get("paid"))
        order.post_cost = _d(row.get("post_cost") or row.get("post_fee"))
        order.package_cost = _d(row.get("package_cost") or row.get("package_fee"))
        order.raw_json = json.dumps(row, ensure_ascii=False, default=str)
        order.items.clear()
        for detail in details_list:
            item = StockoutItem(
                rec_id=_s(detail.get("rec_id")),
                spec_no=_s(detail.get("spec_no")),
                barcode=_s(detail.get("barcode")),
                goods_no=_s(detail.get("goods_no")),
                goods_name=_s(detail.get("goods_name")),
                spec_name=_s(detail.get("spec_name")),
                src_tid=_s(detail.get("src_tid")),
                src_oid=_s(detail.get("src_oid")),
                goods_count=_d(detail.get("goods_count") or detail.get("num")),
                sell_price=_d(detail.get("sell_price") or detail.get("price")),
                total_amount=_d(detail.get("total_amount")),
                share_amount=_d(detail.get("share_amount")),
                cost_price=_d(detail.get("cost_price")),
                share_post=_d(detail.get("share_post")),
                refund_status=_s(detail.get("refund_status")),
            )
            order.items.append(item)
            item_count += 1
        order_count += 1
    db.commit()
    return order_count, item_count
