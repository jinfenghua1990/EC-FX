"""探测旺店通采购单/入库单接口：找出能返回采购价的接口与字段。

用法：
    .venv/Scripts/python.exe scripts/probe_wdt_purchase.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import httpx  # noqa: E402

from app.config import settings  # noqa: E402
from app.services.wangdian import sign_params  # noqa: E402

BASE = settings.wdt_base_url.rstrip("/")


def call(endpoint: str, params: dict) -> dict:
    payload = {
        "sid": settings.wdt_sid,
        "appkey": settings.wdt_appkey,
        "timestamp": int(time.time()),
        **params,
    }
    payload["sign"] = sign_params(payload, settings.wdt_appsecret)
    with httpx.Client(timeout=30) as client:
        resp = client.post(f"{BASE}/{endpoint}", data={k: str(v) for k, v in payload.items()})
    try:
        return resp.json()
    except Exception:
        return {"_http": resp.status_code, "_text": resp.text[:400]}


END = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 300))
START = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 300 - 25 * 86400))
CANDIDATES = [
    ("purchase_order_query.php", {"start_time": START, "end_time": END, "page_no": 0, "page_size": 2}),
    ("stockin_order_query.php", {"start_time": START, "end_time": END, "page_no": 0, "page_size": 2}),
]


def main() -> None:
    for endpoint, params in CANDIDATES:
        print("=" * 70)
        print(f"POST {endpoint}  params={ {k: v for k, v in params.items() if k not in ('page_no', 'page_size')} }")
        try:
            data = call(endpoint, params)
        except Exception as exc:  # noqa: BLE001
            print("  EXC:", exc)
            continue
        code = data.get("code")
        print("  code=", code, "message=", data.get("message"))
        if code is None or int(code) != 0:
            print("  ", json.dumps(data, ensure_ascii=False)[:300])
            continue
        # 打印顶层键 + 首条记录的字段
        top_keys = [k for k in data.keys() if k not in ("code", "message")]
        print("  顶层键:", top_keys)
        for key in top_keys:
            value = data.get(key)
            if isinstance(value, list) and value:
                print(f"  {key}: {len(value)} 条")
                first = value[0]
                print("  首条字段:", sorted(first.keys()))
                print("  首条内容:", json.dumps(first, ensure_ascii=False, indent=2)[:6000])
                # 明细字段单独打印
                for dk, dv in first.items():
                    if isinstance(dv, list) and dv and isinstance(dv[0], dict):
                        print(f"  >> 明细 {dk} 字段:", sorted(dv[0].keys()))
                        print(f"  >> 明细 {dk} 首条:", json.dumps(dv[0], ensure_ascii=False, indent=2)[:4000])
                break
            elif isinstance(value, int):
                print(f"  {key} = {value}")


if __name__ == "__main__":
    main()
