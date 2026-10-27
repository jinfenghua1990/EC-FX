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

    def sales_trade_push(self, trade: dict[str, Any]) -> dict[str, Any]:
        """推送/更新销售单（分销订单 → 旺店通）。

        旺店通 openapi2 sales_trade_push.php：shop_no + tid + trade（JSON 串）。
        tid 相同则更新原单，因此重推分销订单不会造成重复。
        """
        if not trade.get("shop_no"):
            raise RuntimeError("旺店通销售单推送缺少 shop_no，请配置 WDT_SHOP_NO")
        if not trade.get("tid"):
            raise RuntimeError("销售单推送缺少 tid（原始单号）")
        return self._request(
            "sales_trade_push.php",
            {
                "shop_no": _s(trade.get("shop_no")),
                "tid": _s(trade.get("tid")),
                "trade": json.dumps(trade, ensure_ascii=False, default=str),
            },
        )


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
