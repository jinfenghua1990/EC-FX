from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import asyncio
import contextlib
import json
import httpx

from urllib.parse import quote

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from decimal import Decimal
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import ManualAdjustment, StockoutItem, StockoutOrder
from .services.importers import read_tabular
from .services.wangdian import WangdianClient, upsert_stockouts
from .services.sales_outbound import sales_outbound_detail, sales_outbound_facets, sales_outbound_orders, sales_outbound_overview
from .services.diy_export import distinct_values, export_detail, export_fields_catalog, export_order_level, export_summary, filter_options, summary_result, sync_goods_class_cache
from .services.system_update import (get_update_status, queue_action, stage_update_zip, github_check, github_download_update, set_update_status, read_auto_config, write_auto_config)
from .services.adjustments import (
    create_adjustment, effective_stockout_rows, list_adjustments, stockout_adjustment_summary,
)
from .services.distribution import (
    FX_ORDER_STATUSES, STATUS_LABELS, create_distributor, create_order, create_product,
    delete_price, fx_overview, import_distributors, import_products,
    list_distributors, list_orders, list_prices, list_products, list_statements,
    list_brands,
    order_detail, push_order_to_wangdian, set_price, statement_excel_bytes,
    transition_order, toggle_distributor, toggle_product, update_distributor,
    update_order, update_product,
)
from .services.wdt_products import sync_products_from_wdt

app = FastAPI(title=settings.app_name, version=settings.app_version)
STATIC_DIR = Path(__file__).parent / "static"


class AdjustmentPayload(BaseModel):
    target_type: str = "order"
    target_key: str
    period: str = ""
    field_name: str = ""
    operation: str = "add"
    decimal_value: Decimal | None = None
    text_value: str = ""
    reason: str
    active: bool = True


class FxDistributorPayload(BaseModel):
    code: str
    name: str
    contact: str = ""
    phone: str = ""
    email: str = ""
    region: str = ""
    note: str = ""


class FxProductPayload(BaseModel):
    sku: str
    name: str
    spec: str = ""
    unit: str = "箱"
    base_price: Decimal = Decimal("0")
    note: str = ""
    brand: str = ""
    category: str = ""


class FxOrderLinePayload(BaseModel):
    product_id: int
    quantity: Decimal
    unit_price: Decimal | None = None


class FxOrderPayload(BaseModel):
    distributor_id: int
    remark: str = ""
    lines: list[FxOrderLinePayload] = Field(default_factory=list)


class FxPricePayload(BaseModel):
    distributor_id: int
    product_id: int
    price: Decimal | None = None
    note: str = ""


class FxImportPayload(BaseModel):
    items: list[dict] = Field(default_factory=list)


@app.get("/healthz")
def healthz():
    return {"ok": True, "app": settings.app_name, "version": settings.app_version}



@app.get("/api/system-update/status")
def system_update_status():
    data = get_update_status(current_version=settings.app_version, data_root=settings.system_update_data_root)
    # 本地离线副本没有配置 GitHub 时，前端不要自动发起 check（否则每次打开更新中心都有 400 噪音）
    data["github_configured"] = bool(settings.github_repo and settings.github_token)
    return data


@app.post("/api/system-update/check")
def system_update_check():
    if not settings.github_repo:
        raise HTTPException(400, "尚未配置 GITHUB_REPO")
    if not settings.github_token:
        raise HTTPException(400, "尚未配置 GITHUB_TOKEN")
    try:
        info = github_check(
            repo=settings.github_repo,
            branch=settings.github_branch,
            token=settings.github_token,
            current_version=settings.app_version,
            api_url=settings.github_api_url,
        )
        message = (
            f"发现新版本 v{info['remote_version']}，可点击“下载并安装”。"
            if info["has_update"]
            else f"当前已是最新版本 v{settings.app_version}。"
        )
        set_update_status(
            data_root=settings.system_update_data_root,
            STATE="success",
            MESSAGE=message,
            CURRENT_VERSION=settings.app_version,
            REMOTE_VERSION=info["remote_version"],
            REMOTE_COMMIT=info["remote_commit"],
            HAS_UPDATE="1" if info["has_update"] else "0",
            MODE="github-api",
            GITHUB_REPO=info["repo"],
        )
        return {"ok": True, **info}
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        set_update_status(
            data_root=settings.system_update_data_root,
            STATE="error",
            MESSAGE=str(exc),
            CURRENT_VERSION=settings.app_version,
            MODE="github-api",
            GITHUB_REPO=settings.github_repo,
        )
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/system-update/apply")
def system_update_apply():
    if not settings.github_repo:
        raise HTTPException(400, "尚未配置 GITHUB_REPO")
    if not settings.github_token:
        raise HTTPException(400, "尚未配置 GITHUB_TOKEN")
    try:
        info = github_check(
            repo=settings.github_repo,
            branch=settings.github_branch,
            token=settings.github_token,
            current_version=settings.app_version,
            api_url=settings.github_api_url,
        )
        if not info["has_update"]:
            set_update_status(
                data_root=settings.system_update_data_root,
                STATE="success",
                MESSAGE=f"当前已是最新版本 v{settings.app_version}。",
                CURRENT_VERSION=settings.app_version,
                REMOTE_VERSION=info["remote_version"],
                REMOTE_COMMIT=info["remote_commit"],
                HAS_UPDATE="0",
                MODE="github-api",
                GITHUB_REPO=info["repo"],
            )
            return {"ok": True, "message": "already_latest", **info}
        package = github_download_update(
            repo=settings.github_repo,
            branch=settings.github_branch,
            token=settings.github_token,
            api_url=settings.github_api_url,
            data_root=settings.system_update_data_root,
        )
        req = queue_action(
            "install_package",
            data_root=settings.system_update_data_root,
            package_dir=package["package_dir"],
            package_version=package["package_version"],
        )
        return {"ok": True, **info, **package, **req}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/system-update/rollback")
def system_update_rollback():
    try:
        req = queue_action("rollback", data_root=settings.system_update_data_root)
        return {"ok": True, **req}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/system-update/upload-package")
async def system_update_upload_package(file: UploadFile = File(...)):
    content = await file.read()
    try:
        package = stage_update_zip(
            content,
            file.filename or "update.zip",
            data_root=settings.system_update_data_root,
        )
        req = queue_action(
            "install_package",
            data_root=settings.system_update_data_root,
            package_dir=package["package_dir"],
            package_version=package["package_version"],
        )
        return {"ok": True, **package, **req}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400 if isinstance(exc, ValueError) else 409, str(exc)) from exc


class AutoUpdatePayload(BaseModel):
    enabled: bool
    interval_minutes: int = 30


@app.get("/api/system-update/auto")
def system_update_auto_get():
    """自动升级开关状态。"""
    return read_auto_config(data_root=settings.system_update_data_root)


def _wake_auto_update() -> None:
    """立刻唤醒自动升级循环，让开关/间隔改动马上生效，不必等满一个周期。"""
    event = getattr(app.state, "auto_update_wakeup", None)
    if event is not None:
        event.set()


@app.post("/api/system-update/auto")
def system_update_auto_set(payload: AutoUpdatePayload):
    """打开/关闭自动升级，并设置检查间隔（5~1440 分钟）。"""
    result = write_auto_config(
        data_root=settings.system_update_data_root,
        enabled=payload.enabled,
        interval_minutes=payload.interval_minutes,
    )
    _wake_auto_update()
    return result


def _auto_update_tick() -> None:
    """自动升级的一次完整检查：发现新版本就下载并排队安装。"""
    root = settings.system_update_data_root
    stamp = datetime.utcnow().isoformat()
    if not settings.github_repo or not settings.github_token:
        write_auto_config(data_root=root, LAST_CHECK_AT=stamp, LAST_RESULT="未配置 GITHUB_REPO / GITHUB_TOKEN，自动升级跳过")
        return
    try:
        info = github_check(
            repo=settings.github_repo,
            branch=settings.github_branch,
            token=settings.github_token,
            current_version=settings.app_version,
            api_url=settings.github_api_url,
        )
    except Exception as exc:  # noqa: BLE001 - 自动任务不能因单次失败中断
        write_auto_config(data_root=root, LAST_CHECK_AT=stamp, LAST_RESULT=f"检查失败：{exc}")
        return
    remote = str(info.get("remote_version") or "")
    if not info.get("has_update"):
        write_auto_config(data_root=root, LAST_CHECK_AT=stamp, LAST_CHECK_VERSION=remote, LAST_RESULT=f"已是最新（{remote}）")
        return
    try:
        package = github_download_update(
            repo=settings.github_repo,
            branch=settings.github_branch,
            token=settings.github_token,
            api_url=settings.github_api_url,
            data_root=root,
        )
        queue_action(
            "install_package",
            data_root=root,
            package_dir=package["package_dir"],
            package_version=package["package_version"],
        )
        set_update_status(
            data_root=root,
            STATE="queued",
            MESSAGE=f"自动升级：已下载 {package['package_version']}，等待群晖后台任务安装",
            CURRENT_VERSION=settings.app_version,
            REMOTE_VERSION=remote,
            HAS_UPDATE="1",
            MODE="github-api-auto",
            GITHUB_REPO=info["repo"],
        )
        write_auto_config(data_root=root, LAST_CHECK_AT=stamp, LAST_CHECK_VERSION=remote, LAST_RESULT=f"已下载 {package['package_version']} 并排队安装")
    except Exception as exc:  # noqa: BLE001
        write_auto_config(data_root=root, LAST_CHECK_AT=stamp, LAST_CHECK_VERSION=remote, LAST_RESULT=f"下载或排队失败：{exc}")


async def _auto_update_loop() -> None:
    """后台常驻：按配置间隔执行自动升级检查。"""
    await asyncio.sleep(15)
    wakeup: asyncio.Event = app.state.auto_update_wakeup
    while True:
        interval_minutes = 30
        try:
            cfg = read_auto_config(data_root=settings.system_update_data_root)
            interval_minutes = max(5, int(cfg.get("interval_minutes") or 30))
            if cfg.get("enabled"):
                await asyncio.to_thread(_auto_update_tick)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 任何异常都不应终止循环
            pass
        try:
            await asyncio.wait_for(wakeup.wait(), timeout=interval_minutes * 60)
        except asyncio.TimeoutError:
            pass
        wakeup.clear()


@app.on_event("startup")
async def _start_auto_update_loop() -> None:
    app.state.auto_update_wakeup = asyncio.Event()
    app.state.auto_update_task = asyncio.create_task(_auto_update_loop())


@app.on_event("shutdown")
async def _stop_auto_update_loop() -> None:
    task = getattr(app.state, "auto_update_task", None)
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@app.get("/api/status")
def status(db: Session = Depends(get_db)):
    return {
        "wdt_configured": WangdianClient().configured,
        "stockout_orders": db.scalar(select(func.count()).select_from(StockoutOrder)) or 0,
        "stockout_items": db.scalar(select(func.count()).select_from(StockoutItem)) or 0,
    }


@app.get("/api/wdt/test")
def test_wdt_connection():
    client = WangdianClient()
    if not client.configured:
        raise HTTPException(400, "旺店通凭证未配置")
    try:
        payload = client.test_connection()
        shops = payload.get("shop_list") or payload.get("shops") or []
        return {"ok": True, "message": "旺店通正式环境连接成功", "shop_count": len(shops)}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/wdt/sync")
def sync_wdt(start: datetime, end: datetime, db: Session = Depends(get_db)):
    client = WangdianClient()
    if not client.configured:
        raise HTTPException(400, "旺店通凭证未配置，请在 .env 中填写 WDT_SID / WDT_APPKEY / WDT_APPSECRET")
    try:
        rows = client.stockouts(start, end)
        orders, items = upsert_stockouts(db, rows, "wdt_api")
        return {"ok": True, "orders": orders, "items": items}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/import/wdt-stockout")
async def upload_wdt_stockout(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """受限平台兜底。后续拿到真实旺店通 API 后，由 adapter 替换数据源，不改0610算法层。"""
    content = await file.read()
    try:
        rows = read_tabular(file.filename or "wdt.xlsx", content)
        normalized = []
        for row in rows:
            if "raw_json" in row and row["raw_json"]:
                normalized.append(json.loads(str(row["raw_json"])))
                continue
            normalized.append({
                "stockout_no": row.get("出库单号") or row.get("销售出库单号") or row.get("单据编号"),
                "trade_no": row.get("系统订单编号") or row.get("订单编号"),
                "src_trade_no": row.get("原始单号") or row.get("平台订单号"),
                "src_tids": row.get("原始单号") or row.get("平台订单号"),
                "shop_name": row.get("店铺名称") or row.get("店铺"),
                "shop_no": row.get("店铺编号"),
                "platform_id": row.get("平台id") or row.get("平台ID") or row.get("平台"),
                "consign_time": row.get("发货时间") or row.get("出库时间"),
                "pay_time": row.get("付款时间"),
                "status": row.get("状态") or row.get("出库单状态"),
                "goods_count": row.get("货品数量") or row.get("数量"),
                "goods_total_amount": row.get("货品总售价") or row.get("总货款") or row.get("销售金额"),
                "goods_total_cost": row.get("货品总成本") or row.get("总成本") or row.get("成本金额"),
                "receivable": row.get("应收金额"),
                "actual_receivable": row.get("实际应收") or row.get("主单实际应收"),
                "paid": row.get("已付金额") or row.get("实付金额"),
                "post_cost": row.get("邮费成本") or row.get("物流成本"),
                "package_cost": row.get("包装成本") or row.get("包装费"),
                "details_list": [],
            })
        orders, items = upsert_stockouts(db, normalized, "wdt_export")
        return {"ok": True, "orders": orders, "items": items}
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/data/sales-outbound/sync")
def data_sales_outbound_sync(start: datetime, end: datetime, db: Session = Depends(get_db)):
    client = WangdianClient()
    if not client.configured:
        raise HTTPException(400, "旺店通凭证未配置")
    # 旺店通要求 end_time 早于当前时间 2-3 分钟
    clamped_end = datetime.now() - timedelta(minutes=3)
    if end > clamped_end:
        end = clamped_end
    if end <= start:
        raise HTTPException(400, "end 必须晚于 start")
    # 单次跨度不能超过 30 天，自动分块（每块 25 天留余量）
    total_orders = total_items = 0
    chunk_start, chunk_len = start, timedelta(days=25)
    try:
        while chunk_start < end:
            chunk_end = min(chunk_start + chunk_len, end)
            rows = client.stockouts(chunk_start, chunk_end)
            orders, items = upsert_stockouts(db, rows, "wdt_api")
            total_orders += orders
            total_items += items
            chunk_start = chunk_end
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ok": True, "source": "wdt_api", "orders": total_orders, "items": total_items, "start": start.isoformat(), "end": end.isoformat()}


@app.get("/api/data/sales-outbound/adjustments")
def data_sales_adjustments(period: str = "", db: Session = Depends(get_db)):
    rows = list_adjustments(db, period)
    return [{
        "id": x.id, "target_type": x.target_type, "target_key": x.target_key, "period": x.period,
        "field_name": x.field_name, "operation": x.operation,
        "decimal_value": float(x.decimal_value) if x.decimal_value is not None else None,
        "text_value": x.text_value, "reason": x.reason, "active": bool(x.active),
        "created_at": x.created_at.isoformat() if x.created_at else None,
    } for x in rows]


@app.post("/api/data/sales-outbound/adjustments")
def data_sales_adjustment_create(payload: AdjustmentPayload, db: Session = Depends(get_db)):
    try:
        x = create_adjustment(db, payload.model_dump())
        return {"ok": True, "id": x.id}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/data/sales-outbound/adjustments/{adjustment_id}/toggle")
def data_sales_adjustment_toggle(adjustment_id: int, active: bool = True, db: Session = Depends(get_db)):
    row = db.get(ManualAdjustment, adjustment_id)
    if not row:
        raise HTTPException(404, "调整记录不存在")
    row.active = 1 if active else 0
    db.commit()
    return {"ok": True, "id": row.id, "active": bool(row.active)}


@app.get("/api/data/sales-outbound/effective")
def data_sales_effective(month: str = Query(pattern=r"^\d{4}-\d{2}$"), db: Session = Depends(get_db)):
    rows = effective_stockout_rows(db, month)
    return {"month": month, "rows": rows, "count": len(rows), "note": "raw 为旺店通原值，effective 为叠加手工调整后的最终值；原始旺店通数据不被覆盖。"}


@app.get("/api/data/sales-outbound/summary")
def data_sales_summary(month: str = Query(pattern=r"^\d{4}-\d{2}$"), db: Session = Depends(get_db)):
    return stockout_adjustment_summary(db, month)




@app.get("/api/data/sales-outbound/overview")
def data_sales_overview(month: str = Query(pattern=r"^\d{4}-\d{2}$"), db: Session = Depends(get_db)):
    return sales_outbound_overview(db, month)


@app.get("/api/data/sales-outbound/filters")
def data_sales_filters(month: str = Query(pattern=r"^\d{4}-\d{2}$"), db: Session = Depends(get_db)):
    return sales_outbound_facets(db, month)


@app.get("/api/data/sales-outbound/orders")
def data_sales_orders(
    month: str = Query(pattern=r"^\d{4}-\d{2}$"), keyword: str = "", shop: str = "", platform: str = "",
    status: str = "", refund_status: str = "", adjustment: str = "all", page: int = 1, page_size: int = 50,
    db: Session = Depends(get_db),
):
    return sales_outbound_orders(db, month, keyword, shop, platform, status, refund_status, adjustment, page, page_size)


@app.get("/api/data/sales-outbound/orders/{stockout_no}")
def data_sales_order_detail(stockout_no: str, db: Session = Depends(get_db)):
    try:
        return sales_outbound_detail(db, stockout_no)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/data/sales-outbound/export/fields")
def data_sales_export_fields():
    return export_fields_catalog()


@app.get("/api/data/sales-outbound/export/distinct-values")
def data_sales_export_distinct_values(
    dim: str,
    start: str = Query(default="2026-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end: str = Query(default="2026-12-31", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db),
):
    try:
        return distinct_values(db, dim, start, end)
    except Exception as exc:
        raise HTTPException(400, f"维度取值失败：{exc}") from exc


@app.get("/api/data/sales-outbound/export/filter-options")
def data_sales_export_filter_options(
    start: str = Query(default="2026-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end: str = Query(default="2026-12-31", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    db: Session = Depends(get_db),
):
    return filter_options(db, start, end)


@app.get("/api/data/sales-outbound/export")
def data_sales_export(
    mode: str = "detail",
    start: str = Query(default="2026-01-01", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end: str = Query(default="2026-12-31", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    fields: str = "",
    group_by: str = "",
    measures: str = "",
    keyword: str = "",
    shop: str = "",
    warehouse: str = "",
    platform: str = "",
    status: str = "",
    refund_status: str = "",
    sort_by: str = "",
    sort_dir: str = "desc",
    top: int = 0,
    dim_filters: str = "",
    format: str = "csv",
    db: Session = Depends(get_db),
):
    filters = {"keyword": keyword, "shop": shop, "warehouse": warehouse, "platform": platform, "status": status, "refund_status": refund_status}
    # 维度值过滤：格式 dim1:value1|dim2:value2（值含冒号或竖线需 escape，但字段值都是中文店名仓名不含，避免引入）
    parsed_dim_filters: dict[str, str] = {}
    if dim_filters:
        for kv in dim_filters.split("|"):
            if ":" in kv:
                k, v = kv.split(":", 1)
                if k and v:
                    parsed_dim_filters[k] = v
    try:
        if mode == "summary":
            if format == "json":
                return summary_result(db, start, end, group_by, measures, filters, sort_by, sort_dir, top, parsed_dim_filters)
            return export_summary(db, start, end, group_by, measures, filters, sort_by, sort_dir, top, parsed_dim_filters)
        if mode == "order":
            return export_order_level(db, start, end, fields, filters)
        return export_detail(db, start, end, fields, filters)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/wdt/sync-goods-class")
def wdt_sync_goods_class(start: str = "2026-01-01"):
    try:
        return sync_goods_class_cache(start=start)
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


# ============ 红盒子供销平台（原分销系统） ============
@app.get("/api/fx/overview")
def fx_system_overview(db: Session = Depends(get_db)):
    return fx_overview(db)


@app.get("/api/fx/distributors")
def fx_distributors(include_inactive: int = 1, db: Session = Depends(get_db)):
    return {"distributors": list_distributors(db, include_inactive=bool(include_inactive))}


@app.post("/api/fx/distributors")
def fx_distributor_create(payload: FxDistributorPayload, db: Session = Depends(get_db)):
    try:
        return {"ok": True, "distributor": create_distributor(db, payload.model_dump())}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/fx/distributors/{distributor_id}")
def fx_distributor_update(distributor_id: int, payload: FxDistributorPayload, db: Session = Depends(get_db)):
    try:
        return {"ok": True, "distributor": update_distributor(db, distributor_id, payload.model_dump())}
    except ValueError as exc:
        raise HTTPException(404 if "不存在" in str(exc) else 400, str(exc)) from exc


@app.post("/api/fx/distributors/{distributor_id}/toggle")
def fx_distributor_toggle(distributor_id: int, db: Session = Depends(get_db)):
    try:
        row = toggle_distributor(db, distributor_id)
        return {"ok": True, "distributor": row}
    except ValueError as exc:
        raise HTTPException(404 if "不存在" in str(exc) else 400, str(exc)) from exc


@app.get("/api/fx/products")
def fx_products(include_inactive: int = 1, brand: str = "", db: Session = Depends(get_db)):
    return {"products": list_products(db, include_inactive=bool(include_inactive), brand=brand)}


@app.get("/api/fx/brands")
def fx_brands(db: Session = Depends(get_db)):
    """顶部「品牌维度」切换器的数据源：品牌名 + 在售商品数。"""
    return {"brands": list_brands(db)}


@app.post("/api/fx/products")
def fx_product_create(payload: FxProductPayload, db: Session = Depends(get_db)):
    try:
        return {"ok": True, "product": create_product(db, payload.model_dump())}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/fx/products/{product_id}")
def fx_product_update(product_id: int, payload: FxProductPayload, db: Session = Depends(get_db)):
    try:
        return {"ok": True, "product": update_product(db, product_id, payload.model_dump())}
    except ValueError as exc:
        raise HTTPException(404 if "不存在" in str(exc) else 400, str(exc)) from exc


@app.post("/api/fx/products/{product_id}/toggle")
def fx_product_toggle(product_id: int, db: Session = Depends(get_db)):
    try:
        row = toggle_product(db, product_id)
        return {"ok": True, "product": row}
    except ValueError as exc:
        raise HTTPException(404 if "不存在" in str(exc) else 400, str(exc)) from exc


@app.get("/api/fx/products/wdt-status")
def fx_products_wdt_status():
    """旺店通商品同步的可用性自检：前端据此决定按钮是否可点。"""
    client = WangdianClient()
    return {
        "configured": client.configured,
        "sid": client.sid,
        "appkey": client.appkey,
        "base_url": client.base_url,
        "missing": [
            name
            for name, value in (
                ("WDT_SID", client.sid),
                ("WDT_APPKEY", client.appkey),
                ("WDT_APPSECRET", client.appsecret),
            )
            if not value
        ],
    }


@app.post("/api/fx/products/sync-wdt")
def fx_products_sync_wdt(
    start: str = "",
    end: str = "",
    overwrite_price: int = 0,
    dry_run: int = 0,
    page_size: int = 100,
    lookback_days: int = 1825,
    db: Session = Depends(get_db),
):
    """从旺店通商品档案同步商品到 fx_products。

    - 不传 start/end：按 30 天时间窗回扫最近 lookback_days 天（默认 1825 天 ≈ 5 年）
      （旺店通 goods_query.php 强制要求时间区间且单次跨度 ≤30 天，无法一次拉全量）
    - dry_run=1：只预览统计，不落库
    - overwrite_price=1：用旺店通价格覆盖已维护的基准价（默认不覆盖）
    """
    try:
        return sync_products_from_wdt(
            db,
            start=start,
            end=end,
            overwrite_price=bool(overwrite_price),
            dry_run=bool(dry_run),
            page_size=page_size,
            lookback_days=lookback_days,
        )
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"旺店通商品同步失败：{exc}") from exc


@app.get("/api/fx/orders")
def fx_orders(status: str = "", distributor_id: int | None = None, limit: int = 200, db: Session = Depends(get_db)):
    try:
        return {"orders": list_orders(db, status=status, distributor_id=distributor_id, limit=limit)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/fx/orders/{order_id}")
def fx_order_detail(order_id: int, db: Session = Depends(get_db)):
    try:
        return order_detail(db, order_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/fx/orders")
def fx_order_create(payload: FxOrderPayload, db: Session = Depends(get_db)):
    try:
        row = create_order(
            db,
            distributor_id=payload.distributor_id,
            lines=[line.model_dump() for line in payload.lines],
            remark=payload.remark,
        )
        return {"ok": True, "order": row}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/fx/orders/{order_id}")
def fx_order_update(order_id: int, payload: FxOrderPayload, db: Session = Depends(get_db)):
    try:
        row = update_order(
            db,
            order_id,
            distributor_id=payload.distributor_id,
            lines=[line.model_dump() for line in payload.lines],
            remark=payload.remark,
        )
        return {"ok": True, "order": row}
    except ValueError as exc:
        raise HTTPException(404 if "不存在" in str(exc) else 400, str(exc)) from exc


# 注意：本路由必须定义在下方 {action} 通配路由之前，否则 push-wdt 会被它先匹配走
@app.post("/api/fx/orders/{order_id}/push-wdt")
def fx_order_push_wdt(order_id: int, db: Session = Depends(get_db)):
    try:
        return push_order_to_wangdian(db, order_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/fx/orders/{order_id}/{action}")
def fx_order_transition(order_id: int, action: str, db: Session = Depends(get_db)):
    if action not in {"submit", "approve", "reject", "cancel"}:
        raise HTTPException(404, "未知订单动作")
    try:
        return {"ok": True, "order": transition_order(db, order_id, action)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/fx/prices")
def fx_prices(distributor_id: int | None = None, db: Session = Depends(get_db)):
    return {"prices": list_prices(db, distributor_id=distributor_id)}


@app.post("/api/fx/prices")
def fx_price_set(payload: FxPricePayload, db: Session = Depends(get_db)):
    try:
        return {"ok": True, "price": set_price(db, payload.distributor_id, payload.product_id, payload.price, payload.note)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/fx/prices/{distributor_id}/{product_id}")
def fx_price_delete(distributor_id: int, product_id: int, db: Session = Depends(get_db)):
    try:
        return delete_price(db, distributor_id, product_id)
    except ValueError as exc:
        raise HTTPException(404 if "未设置" in str(exc) else 400, str(exc)) from exc


@app.post("/api/fx/distributors/import")
def fx_distributors_import(payload: FxImportPayload, db: Session = Depends(get_db)):
    if not payload.items:
        raise HTTPException(400, "没有可导入的数据行")
    result = import_distributors(db, payload.items)
    return {"ok": True, **result}


@app.post("/api/fx/products/import")
def fx_products_import(payload: FxImportPayload, db: Session = Depends(get_db)):
    if not payload.items:
        raise HTTPException(400, "没有可导入的数据行")
    result = import_products(db, payload.items)
    return {"ok": True, **result}


@app.get("/api/fx/statements")
def fx_statements(distributor_id: int | None = None, month: str = "", db: Session = Depends(get_db)):
    if month and not (len(month) == 7 and month[:4].isdigit() and month[5:7].isdigit() and month[4] == "-"):
        raise HTTPException(400, "month 格式应为 YYYY-MM")
    return list_statements(db, distributor_id=distributor_id, month=month)


@app.get("/api/fx/statements/export")
def fx_statements_export(distributor_id: int, month: str, db: Session = Depends(get_db)):
    try:
        filename, content = statement_excel_bytes(db, distributor_id, month)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}; filename=\"statement.xlsx\""},
    )


@app.get("/workbench")
def workbench_page():
    return FileResponse(STATIC_DIR / "workbench.html")


@app.get("/system-update")
def system_update_page():
    # 系统更新独立页（原先寄生在核销工作台的弹窗里）
    return FileResponse(STATIC_DIR / "system-update.html")


@app.get("/coming-soon")
def coming_soon_page():
    # 统一占位页：菜单里已规划、但数据模型/接口尚未建立的功能
    return FileResponse(STATIC_DIR / "coming-soon.html")


@app.get("/shipments")
def shipments_page():
    return FileResponse(STATIC_DIR / "shipments.html")


@app.get("/reports")
def reports_page():
    return FileResponse(STATIC_DIR / "reports.html")


@app.get("/sales-outbound")
def sales_outbound_page():
    return FileResponse(STATIC_DIR / "sales-outbound.html")


@app.get("/distribution")
def distribution_page():
    # 红盒子供销平台：分销管理主模块
    return FileResponse(STATIC_DIR / "distribution.html")


@app.get("/")
def index():
    # 首页 = 外壳框架（顶部一级菜单 + 左侧二级菜单 + 多标签 iframe）
    # /workbench 为「经营总览」页，也可直接访问
    return FileResponse(STATIC_DIR / "shell.html")


@app.get("/static/{filename}")
def static_asset(filename: str):
    """静态资源（embed.js 等）。只放行静态目录下真实存在的文件，禁止路径穿越。"""
    if not filename or "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(404, "not found")
    target = STATIC_DIR / filename
    if not target.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(target)
