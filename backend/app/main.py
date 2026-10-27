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
from .models import ImportBatch, ManualAdjustment, ModuleRawRecord, ModuleStandardRecord, RebateRule, ReconResult, SettlementRow, StockoutItem, StockoutOrder
from .services.importers import import_bank, import_settlement, read_tabular
from .services.legacy_logic import latest_legacy_batch, monthly_summary, yearly_series
from .services.legacy_workbook import import_0610_workbook
from .services.reconciliation import profit_rows, run_reconciliation
from .services.wangdian import WangdianClient, upsert_stockouts
from .services.sales_outbound import sales_outbound_detail, sales_outbound_facets, sales_outbound_orders, sales_outbound_overview
from .services.diy_export import distinct_values, export_detail, export_fields_catalog, export_order_level, export_summary, filter_options, summary_result, sync_goods_class_cache
from .services.system_update import (get_update_status, queue_action, stage_update_zip, github_check, github_download_update, set_update_status, read_auto_config, write_auto_config)
from .services.module_hub import (
    MODULES, batch_preview, calculate_purchase_rebate, create_adjustment, create_rebate_rule,
    effective_stockout_rows, import_module_raw, list_adjustments, list_mapping_profiles,
    list_module_batches, list_rebate_rules, list_standard_records, module_catalog, module_readiness, rebate_channel_catalog, update_rebate_rule,
    module_schema, normalize_module_batch, standard_record_lineage, stockout_adjustment_summary,
)
from .services.distribution import (
    FX_ORDER_STATUSES, STATUS_LABELS, create_distributor, create_order, create_product,
    delete_price, fx_overview, import_distributors, import_products,
    list_distributors, list_orders, list_prices, list_products, list_statements,
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


class MappingPayload(BaseModel):
    mapping: dict[str, str] = Field(default_factory=dict)
    profile_name: str = "默认映射"
    save_profile: bool = True


class RebateRulePayload(BaseModel):
    brand: str = "*"
    channel: str = "*"
    category: str = "渠道返利"
    basis_field: str = "invoice_amount_untaxed"
    rate: Decimal = Decimal("0")
    tax_factor: Decimal = Decimal("1")
    effective_from: str = ""
    effective_to: str = ""
    priority: int = 100
    active: bool = True
    note: str = ""


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
    legacy = latest_legacy_batch(db)
    return {
        "wdt_configured": WangdianClient().configured,
        "stockout_orders": db.scalar(select(func.count()).select_from(StockoutOrder)) or 0,
        "stockout_items": db.scalar(select(func.count()).select_from(StockoutItem)) or 0,
        "settlement_rows": db.scalar(select(func.count()).select_from(SettlementRow)) or 0,
        "imports": db.scalar(select(func.count()).select_from(ImportBatch)) or 0,
        "legacy_workbook": ({"batch_id": legacy.id, "filename": legacy.filename, "rows": legacy.row_count} if legacy else None),
    }


@app.post("/api/import/legacy-workbook")
async def upload_legacy_workbook(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not (file.filename or "").lower().endswith(".xlsx"):
        raise HTTPException(400, "核销母表仅支持 XLSX")
    content = await file.read()
    try:
        batch, counts, duplicated = import_0610_workbook(db, file.filename or "核销表0610.xlsx", content)
    except Exception as exc:
        raise HTTPException(400, f"核销表导入失败：{exc}") from exc
    return {"ok": True, "batch_id": batch.id, "counts": counts, "duplicated": duplicated}


@app.get("/api/monthly-summary")
def api_monthly_summary(month: str = Query(pattern=r"^\d{4}-\d{2}$"), db: Session = Depends(get_db)):
    return monthly_summary(db, month)


@app.get("/api/yearly-series")
def api_yearly_series(year: int = Query(ge=2020, le=2100), db: Session = Depends(get_db)):
    return yearly_series(db, year)


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
        raise HTTPException(400, "旺店通凭证未配置；当前可以先完整使用0610核销表逻辑")
    try:
        rows = client.stockouts(start, end)
        orders, items = upsert_stockouts(db, rows, "wdt_api")
        return {"ok": True, "orders": orders, "items": items}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/import/settlement")
async def upload_settlement(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        batch, count, duplicated = import_settlement(db, file.filename or "settlement.xlsx", content)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "batch_id": batch.id, "rows": count, "duplicated": duplicated}


@app.post("/api/import/bank")
async def upload_bank(file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        batch, count, duplicated = import_bank(db, file.filename or "bank.xlsx", content)
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True, "batch_id": batch.id, "rows": count, "duplicated": duplicated}


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


# --- V0.8 模块化数据中心：原始层 → 字段映射 → 标准层 → 算法层 → 报表层。 ---
@app.get("/api/data/modules")
def data_modules(db: Session = Depends(get_db)):
    raw_counts = {}
    standard_counts = {}
    for key in MODULES:
        raw_counts[key] = db.scalar(select(func.count()).select_from(ModuleRawRecord).where(ModuleRawRecord.module == key)) or 0
        standard_counts[key] = db.scalar(select(func.count()).select_from(ModuleStandardRecord).where(ModuleStandardRecord.module == key, ModuleStandardRecord.status == "valid")) or 0
    rows = module_catalog()
    readiness = {x["module"]: x for x in module_readiness(db)}
    for row in rows:
        row["raw_rows"] = raw_counts.get(row["key"], 0)
        row["standard_rows"] = standard_counts.get(row["key"], 0)
        row["readiness"] = readiness.get(row["key"], {})
    return {"port": settings.app_port, "modules": rows}


@app.get("/api/data/readiness")
def data_readiness(db: Session = Depends(get_db)):
    return {"modules": module_readiness(db), "report_rule": "只有标准化且校验通过的数据才进入后续正式报表；原始层永远保留。"}


@app.get("/api/data/{module}/schema")
def data_module_schema(module: str):
    try:
        return module_schema(module)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/data/{module}/batches")
def data_module_batches(module: str, limit: int = 30, db: Session = Depends(get_db)):
    try:
        return {"module": module, "batches": list_module_batches(db, module, limit)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/data/{module}/mapping-profiles")
def data_module_mapping_profiles(module: str, db: Session = Depends(get_db)):
    return {"module": module, "profiles": list_mapping_profiles(db, module)}


@app.get("/api/data/batches/{batch_id}/preview")
def data_batch_preview(batch_id: int, limit: int = 20, db: Session = Depends(get_db)):
    try:
        return batch_preview(db, batch_id, limit)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/data/batches/{batch_id}/normalize")
def data_batch_normalize(batch_id: int, payload: MappingPayload, db: Session = Depends(get_db)):
    try:
        result = normalize_module_batch(
            db, batch_id, mapping=payload.mapping, profile_name=payload.profile_name, save_profile=payload.save_profile,
        )
        if not result.get("ok", True):
            return result
        return {"ok": True, **result}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/data/{module}/standard")
def data_module_standard(module: str, period: str = "", limit: int = 200, db: Session = Depends(get_db)):
    if module not in MODULES:
        raise HTTPException(404, "模块不存在")
    return {"module": module, "period": period, "rows": list_standard_records(db, module, period, limit)}


@app.get("/api/data/standard/{record_id}/lineage")
def data_standard_lineage(record_id: int, db: Session = Depends(get_db)):
    try:
        return standard_record_lineage(db, record_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/data/{module}/upload")
async def module_upload(module: str, file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        result = import_module_raw(db, module, file.filename or "upload.xlsx", content, data_root="/app/data")
        # 能自动识别必需字段时，直接进入标准层；识别不全则停在待映射状态，绝不猜字段。
        if not result.get("duplicated") and not result.get("missing_required"):
            normalized = normalize_module_batch(db, result["batch_id"], mapping=result.get("auto_mapping") or {})
            result["normalization"] = normalized
            result["status"] = normalized.get("status", result.get("status"))
            result["standard_rows"] = normalized.get("valid_rows", 0)
        return {"ok": True, "module": module, **result}
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


@app.get("/api/data/rebate/catalog")
def data_rebate_catalog(db: Session = Depends(get_db)):
    return rebate_channel_catalog(db)


@app.get("/api/data/rebate/rules")
def data_rebate_rules(db: Session = Depends(get_db)):
    return [{
        "id": x.id, "brand": x.brand, "channel": x.channel, "category": x.category,
        "basis_field": x.basis_field, "rate": float(x.rate), "tax_factor": float(x.tax_factor),
        "effective_from": x.effective_from, "effective_to": x.effective_to,
        "priority": x.priority, "active": bool(x.active), "note": x.note,
    } for x in list_rebate_rules(db)]


@app.post("/api/data/rebate/rules")
def data_rebate_rule_create(payload: RebateRulePayload, db: Session = Depends(get_db)):
    try:
        x = create_rebate_rule(db, payload.model_dump())
        return {"ok": True, "id": x.id}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.put("/api/data/rebate/rules/{rule_id}")
def data_rebate_rule_update(rule_id: int, payload: RebateRulePayload, db: Session = Depends(get_db)):
    try:
        x = update_rebate_rule(db, rule_id, payload.model_dump())
        return {"ok": True, "id": x.id}
    except ValueError as exc:
        raise HTTPException(404 if "不存在" in str(exc) else 400, str(exc)) from exc


@app.post("/api/data/rebate/rules/{rule_id}/toggle")
def data_rebate_rule_toggle(rule_id: int, active: bool = True, db: Session = Depends(get_db)):
    row = db.get(RebateRule, rule_id)
    if not row:
        raise HTTPException(404, "返利规则不存在")
    row.active = 1 if active else 0
    db.commit()
    return {"ok": True, "id": row.id, "active": bool(row.active)}


@app.get("/api/data/rebate/calculate")
def data_rebate_calculate(month: str = Query(pattern=r"^\d{4}-\d{2}$"), db: Session = Depends(get_db)):
    return calculate_purchase_rebate(db, month)


# ============ FX 分销系统（由核销中心升级） ============
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
def fx_products(include_inactive: int = 1, db: Session = Depends(get_db)):
    return {"products": list_products(db, include_inactive=bool(include_inactive))}


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
    # FX 分销系统（升级中的主模块）
    return FileResponse(STATIC_DIR / "distribution.html")


# V0.1 的逐单核销接口继续保留，后续 API 接入时直接使用。
@app.post("/api/reconciliation/run")
def reconcile(month: str = Query(pattern=r"^\d{4}-\d{2}$"), db: Session = Depends(get_db)):
    return run_reconciliation(db, month)


@app.get("/api/reconciliation")
def reconciliation_list(month: str = Query(pattern=r"^\d{4}-\d{2}$"), db: Session = Depends(get_db)):
    start = month_start(month)
    end = datetime(start.year + (1 if start.month == 12 else 0), 1 if start.month == 12 else start.month + 1, 1)
    rows = db.execute(
        select(ReconResult, SettlementRow, StockoutOrder)
        .join(SettlementRow, SettlementRow.id == ReconResult.settlement_row_id)
        .outerjoin(StockoutOrder, StockoutOrder.id == ReconResult.stockout_order_id)
        .where(SettlementRow.settle_date >= start, SettlementRow.settle_date < end)
        .order_by(ReconResult.status, SettlementRow.id.desc())
    ).all()
    return [{
        "status": recon.status,
        "order_no": settlement.order_no,
        "settled_amount": float(recon.settled_amount),
        "expected_amount": float(recon.expected_amount),
        "difference": float(recon.difference),
        "stockout_no": stockout.stockout_no if stockout else None,
        "shop_name": stockout.shop_name if stockout else settlement.shop_name,
        "note": recon.note,
    } for recon, settlement, stockout in rows]


@app.get("/api/profit")
def profit(month: str = Query(pattern=r"^\d{4}-\d{2}$"), db: Session = Depends(get_db)):
    """兼容旧接口。若已导入0610母表，优先返回原表口径总览。"""
    legacy = latest_legacy_batch(db)
    if legacy:
        summary = monthly_summary(db, month)
        t = summary["totals"]
        return {
            "month": month,
            "mode": "0610_workbook_logic",
            "totals": {
                "orders": 0,
                "revenue": t["settlement_revenue"],
                "goods_cost": t["goods_cost_8_discount_untaxed"],
                "post_cost": t["logistics"],
                "package_cost": 0,
                "gross_profit": t["sales_gross_profit"],
                "gross_margin_pct": (t["sales_gross_profit"] / t["settlement_revenue"] * 100) if t["settlement_revenue"] else 0,
            },
            "rows": [],
        }
    rows = profit_rows(db, month)
    totals = {
        "orders": len(rows),
        "revenue": round(sum(r["revenue"] for r in rows), 2),
        "goods_cost": round(sum(r["goods_cost"] for r in rows), 2),
        "post_cost": round(sum(r["post_cost"] for r in rows), 2),
        "package_cost": round(sum(r["package_cost"] for r in rows), 2),
        "gross_profit": round(sum(r["gross_profit"] for r in rows), 2),
    }
    totals["gross_margin_pct"] = round((totals["gross_profit"] / totals["revenue"] * 100), 2) if totals["revenue"] else 0
    return {"month": month, "mode": "wdt_v01", "totals": totals, "rows": rows}


@app.get("/")
def index():
    # 首页 = 外壳框架（顶部一级菜单 + 左侧二级菜单 + 多标签 iframe）
    # 原来的核销工作台仍可通过 /workbench 直接访问
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
