"""拉取旺店通采购单明细，按 spec_no 汇总采购价，与本地 fx_products.base_price 对比。

用途：确认 purchase_order_query.php 里的哪个价格字段 = 我们口径的「品牌原价/牌价」。
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import settings  # noqa: E402
from app.models import FxProduct  # noqa: E402
from app.services.wangdian import sign_params  # noqa: E402

BASE = settings.wdt_base_url.rstrip("/")
FMT = "%Y-%m-%d %H:%M:%S"


def call(endpoint: str, params: dict) -> dict:
    payload = {
        "sid": settings.wdt_sid,
        "appkey": settings.wdt_appkey,
        "timestamp": int(time.time()),
        **params,
    }
    payload["sign"] = sign_params(payload, settings.wdt_appsecret)
    with httpx.Client(timeout=60) as client:
        resp = client.post(f"{BASE}/{endpoint}", data={k: str(v) for k, v in payload.items()})
    return resp.json()


def fetch_purchases(start_dt: datetime, end_dt: datetime, page_size: int = 100) -> tuple[list[dict], int]:
    """按 30 天窗口回扫采购单。"""
    rows: list[dict] = []
    calls = 0
    cursor = end_dt
    while cursor > start_dt:
        begin = cursor - timedelta(days=29)
        if begin < start_dt:
            begin = start_dt
        page_no = 0
        while True:
            data = call(
                "purchase_order_query.php",
                {
                    "start_time": begin.strftime(FMT),
                    "end_time": cursor.strftime(FMT),
                    "page_no": page_no,
                    "page_size": page_size,
                },
            )
            calls += 1
            code = data.get("code")
            if code is None or int(code) != 0:
                print(f"  !! {begin:%Y-%m-%d}~{cursor:%Y-%m-%d} code={code} {data.get('message')}")
                break
            page = data.get("purchase_list") or []
            rows.extend(page)
            if len(page) < page_size:
                break
            page_no += 1
            if page_no > 300:
                break
        cursor = begin - timedelta(seconds=1)
    return rows, calls


def main() -> None:
    months = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    end_dt = datetime.now() - timedelta(minutes=10)
    start_dt = end_dt - timedelta(days=30 * months)
    print(f"拉取采购单 {start_dt:%Y-%m-%d} ~ {end_dt:%Y-%m-%d} ...")
    orders, calls = fetch_purchases(start_dt, end_dt)
    print(f"采购单 {len(orders)} 张，接口调用 {calls} 次")

    status_counter: dict[str, int] = defaultdict(int)
    by_spec: dict[str, list[dict]] = defaultdict(list)
    for order in orders:
        status_counter[str(order.get("status"))] += 1
        odate = (order.get("check_time") or order.get("created") or "")[:19]
        if odate.startswith("0000"):
            odate = (order.get("created") or "")[:19]
        for d in order.get("details_list") or []:
            spec_no = str(d.get("spec_no") or "").strip()
            if not spec_no:
                continue
            by_spec[spec_no].append(
                {
                    "date": odate,
                    "purchase_no": order.get("purchase_no"),
                    "status": order.get("status"),
                    "provider": order.get("provider_name"),
                    "num": d.get("num"),
                    "num2": d.get("num2"),
                    "purchase_unit": d.get("purchase_unit"),
                    "unit_ratio": d.get("unit_ratio"),
                    "price": d.get("price"),
                    "tax_price": d.get("tax_price"),
                    "purchase_price": d.get("purchase_price"),
                    "discount": d.get("discount"),
                    "amount": d.get("amount"),
                    "tax": d.get("tax"),
                }
            )
    print("单据状态分布:", dict(status_counter))
    print(f"涉及 SKU {len(by_spec)} 个")

    engine = create_engine(settings.database_url)
    with Session(engine) as db:
        products = {p.sku: p for p in db.scalars(select(FxProduct)).all()}

    matched = [s for s in by_spec if s in products]
    print(f"与商品库匹配 {len(matched)} 个；采购单里有、库里没有 {len(by_spec) - len(matched)} 个")

    # 取每个 SKU 最近一次采购明细
    samples = []
    price_eq = {"price": 0, "purchase_price": 0, "tax_price": 0}
    ratio_hits: dict[str, list[float]] = defaultdict(list)
    for spec_no in matched:
        recs = sorted([r for r in by_spec[spec_no] if r["date"]], key=lambda r: r["date"])
        latest = recs[-1]
        product = products[spec_no]
        base = Decimal(str(product.base_price or 0))
        def dv(key: str) -> Decimal:
            try:
                return Decimal(str(latest.get(key) or "0"))
            except Exception:
                return Decimal("0")
        p_price, p_tax, p_pur = dv("price"), dv("tax_price"), dv("purchase_price")
        if base > 0:
            for label, val in (("price", p_price), ("tax_price", p_tax), ("purchase_price", p_pur)):
                if val > 0 and abs(val - base) <= Decimal("0.02"):
                    price_eq[label] += 1
                if val > 0:
                    ratio_hits[label].append(float(base / val))
        if len(samples) < 25:
            samples.append((spec_no, product.name[:26], str(base), str(p_price), str(p_tax), str(p_pur), latest["date"][:10], latest["purchase_unit"]))

    print("\n与 base_price 完全相等的命中数（仅统计 base_price>0 的 SKU）:", price_eq)
    print(f"base_price>0 且匹配的 SKU 数: {sum(1 for s in matched if Decimal(str(products[s].base_price or 0)) > 0)}")
    for label, ratios in ratio_hits.items():
        ratios_sorted = sorted(ratios)
        mid = ratios_sorted[len(ratios_sorted) // 2] if ratios_sorted else 0
        print(f"  base/{label} 中位数={mid:.4f}  样本={len(ratios)}")

    print("\n样例（sku | 名称 | base_price | price | tax_price | purchase_price | 最近采购日 | 采购单位）")
    for row in samples:
        print("  " + " | ".join(row))

    out = Path("/tmp/wdt_purchase_dump.json")
    out.write_text(json.dumps({k: v for k, v in by_spec.items()}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已存 {out}")


if __name__ == "__main__":
    main()
