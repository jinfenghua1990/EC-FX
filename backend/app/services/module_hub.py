from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..models import (
    ImportBatch,
    LegacyPurchaseRow,
    ManualAdjustment,
    ModuleMappingProfile,
    ModuleRawRecord,
    ModuleStandardRecord,
    RebateRule,
    StockoutOrder,
)
from .importers import read_tabular
from .legacy_logic import latest_legacy_batch

D = Decimal


def normalize_month(value: str) -> str:
    """把各种月份写法归一成 'YYYY-MM'（月份补零）。

    支持 '2026-9' / '2026-09' / '2026/9' / '2026.9' / '2026年9月'。
    无法识别时原样返回，避免破坏未知格式。
    """
    s = (value or "").strip()
    if not s:
        return s
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})", s)
    if not m:
        m = re.fullmatch(r"(\d{4})\s*年\s*(\d{1,2})\s*月", s)
    if not m:
        return s
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"


def month_start(value: str) -> datetime:
    """返回某月 1 号 0 点；兼容 '2026-9' 这类非补零写法，避免 strptime 崩溃。"""
    return datetime.strptime(normalize_month(value) + "-01", "%Y-%m-%d")


MODULES: dict[str, dict[str, Any]] = {
    "sales-outbound": {
        "name": "销售出库",
        "source": "旺店通 API",
        "mode": "api",
        "upload": False,
        "note": "主数据只从旺店通 stockout_order_query_trade.php 同步；手工调整独立留痕，不改原始数据。",
    },
    "platform-settlement": {"name": "平台结算收入", "source": "平台原始账单", "mode": "upload", "upload": True},
    "logistics": {"name": "物流费用", "source": "物流/快递原始账单", "mode": "upload", "upload": True},
    "purchase": {"name": "采购单", "source": "采购原始资料", "mode": "upload", "upload": True, "note": "渠道返利的计算基数从标准化采购单产生；采购渠道必须明确。"},
    "inventory": {"name": "库存", "source": "库存/盘点原始资料", "mode": "upload", "upload": True},
    "expenses": {"name": "费用", "source": "费用原始资料", "mode": "upload", "upload": True},
    "prepaid": {"name": "垫付", "source": "垫付原始资料", "mode": "upload", "upload": True},
    "bank": {"name": "银行流水", "source": "银行原始流水", "mode": "upload", "upload": True},
    "rebate": {
        "name": "品牌返利（按渠道）",
        "source": "采购单按渠道计算",
        "mode": "calculated",
        "upload": False,
        "note": "不单独上传返利结果；以渠道为第一维度，按标准化采购单 + 渠道返利规则计算，品牌可作为二级筛选。",
    },
}

# 标准字段并不是“写死平台格式”，而是系统内部统一口径；每个来源文件通过字段映射进入该口径。
MODULE_SCHEMAS: dict[str, dict[str, Any]] = {
    "platform-settlement": {
        "key_field": "order_no",
        "fields": {
            "period": {"label": "核销月份", "type": "period", "aliases": ["月份", "账期", "结算月份", "期间"]},
            "platform": {"label": "平台", "type": "text", "aliases": ["平台", "渠道", "平台名称"]},
            "shop_name": {"label": "店铺", "type": "text", "aliases": ["店铺", "店铺名称", "网店名称"]},
            "order_no": {"label": "订单号", "type": "text", "required": True, "aliases": ["订单号", "平台订单号", "原始订单号", "商户订单号"]},
            "settle_date": {"label": "结算日期", "type": "date", "aliases": ["结算日期", "账务日期", "交易日期", "日期"]},
            "gross_amount": {"label": "收入/应收", "type": "decimal", "aliases": ["收入", "应收", "订单金额", "商品金额", "交易金额"]},
            "platform_fee": {"label": "平台费用", "type": "decimal", "aliases": ["平台费用", "服务费", "佣金", "技术服务费"]},
            "refund_amount": {"label": "退款金额", "type": "decimal", "aliases": ["退款金额", "退款", "退货退款"]},
            "other_fee": {"label": "其他费用", "type": "decimal", "aliases": ["其他费用", "其他扣款", "调整金额"]},
            "net_amount": {"label": "结算净额", "type": "decimal", "required": True, "aliases": ["结算金额", "净额", "实际结算", "实收", "到账金额"]},
        },
    },
    "logistics": {
        "key_field": "logistics_no",
        "fields": {
            "period": {"label": "核销月份", "type": "period", "aliases": ["月份", "账期", "期间"]},
            "bill_date": {"label": "账单日期", "type": "date", "aliases": ["账单日期", "日期", "计费日期"]},
            "order_no": {"label": "订单号", "type": "text", "aliases": ["订单号", "平台订单号", "原始订单号"]},
            "logistics_no": {"label": "物流单号", "type": "text", "required": True, "aliases": ["物流单号", "运单号", "快递单号"]},
            "carrier": {"label": "物流公司", "type": "text", "aliases": ["物流公司", "快递公司", "承运商"]},
            "amount": {"label": "物流费用", "type": "decimal", "required": True, "aliases": ["物流费用", "运费", "快递费", "应付金额", "费用"]},
            "note": {"label": "备注", "type": "text", "aliases": ["备注", "说明"]},
        },
    },
    "purchase": {
        "key_field": "purchase_no",
        "fields": {
            "purchase_no": {"label": "采购单号", "type": "text", "required": True, "aliases": ["采购单号", "采购订单号", "单据编号", "订单号"]},
            "purchase_date": {"label": "采购日期", "type": "date", "aliases": ["采购日期", "下单日期", "订单日期", "日期"]},
            "purchase_month": {"label": "采购月份", "type": "period", "aliases": ["采购月份", "采购月", "月份"]},
            "recon_month": {"label": "核销月份", "type": "period", "aliases": ["核销月份", "核销月", "结算月份"]},
            "channel": {"label": "采购渠道", "type": "text", "required": True, "aliases": ["渠道", "平台", "采购渠道", "销售渠道", "返利渠道"]},
            "supplier": {"label": "供应商", "type": "text", "aliases": ["供应商", "供应商名称", "卖家", "卖家公司"]},
            "brand": {"label": "品牌", "type": "text", "required": True, "aliases": ["品牌", "子品牌", "品牌名称"]},
            "product_code": {"label": "货品编码", "type": "text", "aliases": ["货品编码", "商品编码", "SKU", "商家编码"]},
            "barcode": {"label": "条形码", "type": "text", "aliases": ["条形码", "条码", "Barcode"]},
            "goods_name": {"label": "货品名称", "type": "text", "aliases": ["货品名称", "商品名称", "品名"]},
            "qty_units": {"label": "采购数量", "type": "decimal", "aliases": ["采购数量", "数量", "采购量"]},
            "list_amount_untaxed": {"label": "牌价未税金额", "type": "decimal", "aliases": ["牌价未税金额", "标准未税金额", "未税牌价"]},
            "invoice_amount_untaxed": {"label": "发票未税金额", "type": "decimal", "required": True, "aliases": ["发票未税金额", "采购未税金额", "未税金额", "不含税金额"]},
            "invoice_amount_tax": {"label": "发票含税金额", "type": "decimal", "aliases": ["发票含税金额", "采购含税金额", "价税合计", "含税金额"]},
            "paid_amount": {"label": "采购付款", "type": "decimal", "aliases": ["采购付款", "已付金额", "实付金额", "付款金额"]},
            "status": {"label": "采购状态", "type": "text", "aliases": ["采购状态", "订单状态", "状态"]},
            "note": {"label": "备注", "type": "text", "aliases": ["备注", "说明"]},
        },
    },
    "inventory": {
        "key_field": "barcode",
        "fields": {
            "period": {"label": "库存月份", "type": "period", "required": True, "aliases": ["月份", "库存月份", "盘点月份", "期间"]},
            "warehouse": {"label": "仓库", "type": "text", "aliases": ["仓库", "仓库名称"]},
            "sku": {"label": "SKU", "type": "text", "aliases": ["SKU", "商家编码", "货品编码"]},
            "barcode": {"label": "条形码", "type": "text", "aliases": ["条形码", "条码", "Barcode"]},
            "goods_name": {"label": "货品名称", "type": "text", "aliases": ["货品名称", "商品名称", "品名"]},
            "qty": {"label": "库存数量", "type": "decimal", "aliases": ["库存数量", "数量", "可用库存"]},
            "amount": {"label": "库存金额", "type": "decimal", "required": True, "aliases": ["库存金额", "成本金额", "金额"]},
        },
    },
    "expenses": {
        "key_field": "category",
        "fields": {
            "period": {"label": "费用月份", "type": "period", "required": True, "aliases": ["月份", "费用月份", "期间"]},
            "category": {"label": "费用类别", "type": "text", "required": True, "aliases": ["费用类别", "费用项目", "类别", "项目"]},
            "channel": {"label": "渠道/平台", "type": "text", "aliases": ["渠道", "平台", "店铺"]},
            "amount": {"label": "费用金额", "type": "decimal", "required": True, "aliases": ["费用金额", "金额", "支出金额"]},
            "note": {"label": "备注", "type": "text", "aliases": ["备注", "说明"]},
        },
    },
    "prepaid": {
        "key_field": "counterparty",
        "fields": {
            "period": {"label": "核销月份", "type": "period", "required": True, "aliases": ["月份", "核销月份", "期间"]},
            "txn_date": {"label": "垫付日期", "type": "date", "aliases": ["垫付日期", "日期", "付款日期"]},
            "counterparty": {"label": "对象", "type": "text", "aliases": ["对象", "对方", "收款方", "供应商"]},
            "amount": {"label": "垫付金额", "type": "decimal", "required": True, "aliases": ["垫付金额", "金额", "支出金额"]},
            "note": {"label": "备注", "type": "text", "aliases": ["备注", "说明"]},
        },
    },
    "bank": {
        "key_field": "reference_no",
        "fields": {
            "period": {"label": "月份", "type": "period", "aliases": ["月份", "期间"]},
            "txn_date": {"label": "交易日期", "type": "date", "required": True, "aliases": ["交易日期", "记账日期", "日期", "交易时间"]},
            "reference_no": {"label": "流水号", "type": "text", "aliases": ["流水号", "交易流水号", "凭证号", "参考号"]},
            "counterparty": {"label": "交易对手", "type": "text", "aliases": ["交易对手", "对方户名", "对方名称", "收付款人"]},
            "amount": {"label": "金额", "type": "decimal", "required": True, "aliases": ["金额", "交易金额", "发生额"]},
            "memo": {"label": "摘要", "type": "text", "aliases": ["摘要", "备注", "用途", "附言"]},
        },
    },
}

NUMERIC_STOCKOUT_FIELDS = {
    "goods_count", "goods_total_amount", "goods_total_cost", "receivable",
    "actual_receivable", "paid", "post_cost", "package_cost",
}
TEXT_STOCKOUT_FIELDS = {"shop_name", "shop_no", "platform_id", "status", "refund_status"}
ALLOWED_ADJUST_OPS = {"add", "replace", "multiply", "exclude", "include", "text_replace"}
ALLOWED_TARGET_TYPES = {"order", "shop", "platform", "month"}
ALLOWED_REBATE_BASIS = {"list_amount_untaxed", "invoice_amount_untaxed", "invoice_amount_tax"}


def module_catalog() -> list[dict[str, Any]]:
    out = []
    for key, cfg in MODULES.items():
        x = {"key": key, **cfg}
        if cfg.get("upload"):
            x["upload_endpoint"] = f"/api/data/{key}/upload"
            x["schema_endpoint"] = f"/api/data/{key}/schema"
            x["batches_endpoint"] = f"/api/data/{key}/batches"
            x["standard_endpoint"] = f"/api/data/{key}/standard"
        if key == "sales-outbound":
            x["sync_endpoint"] = "/api/data/sales-outbound/sync"
            x["adjustment_endpoint"] = "/api/data/sales-outbound/adjustments"
        if key == "rebate":
            x["calculate_endpoint"] = "/api/data/rebate/calculate"
            x["rules_endpoint"] = "/api/data/rebate/rules"
        out.append(x)
    return out


def module_schema(module: str) -> dict[str, Any]:
    if module not in MODULE_SCHEMAS:
        raise ValueError("该模块暂无字段标准；销售出库/返利不需要文件字段映射")
    schema = MODULE_SCHEMAS[module]
    return {
        "module": module,
        "name": MODULES[module]["name"],
        "key_field": schema.get("key_field", ""),
        "fields": [{"name": name, **cfg} for name, cfg in schema["fields"].items()],
    }


def _safe_filename(name: str) -> str:
    name = Path(name or "upload.xlsx").name
    return re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", name)[:180] or "upload.xlsx"


def archive_upload(module: str, filename: str, content: bytes, data_root: str = "/app/data") -> str:
    now = datetime.now()
    digest = hashlib.sha256(content).hexdigest()
    folder = Path(data_root) / "module-uploads" / module / f"{now:%Y}" / f"{now:%m}"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{now:%Y%m%d_%H%M%S}_{digest[:10]}_{_safe_filename(filename)}"
    target.write_bytes(content)
    return str(target.relative_to(Path(data_root)))


def _period_from_row(row: dict[str, Any]) -> str:
    candidates = ["月份", "月", "期间", "账期", "结算月份", "采购月份", "核销月份", "日期", "交易日期", "采购日期", "入库日期"]
    for key in candidates:
        v = row.get(key)
        p = _to_period(v)
        if p:
            return p
    return ""


def _external_key(row: dict[str, Any]) -> str:
    for key in ("订单号", "采购单号", "单据编号", "流水号", "平台订单号", "出库单号", "入库单号", "SKU", "商家编码", "条形码"):
        if row.get(key) not in (None, ""):
            return str(row.get(key)).strip()[:255]
    return ""


def import_module_raw(db: Session, module: str, filename: str, content: bytes, data_root: str = "/app/data") -> dict[str, Any]:
    if module not in MODULES or not MODULES[module].get("upload"):
        raise ValueError("该模块不接受原始文件上传")
    rows = read_tabular(filename, content)
    if not rows:
        raise ValueError("文件没有可读取的数据行")
    digest = hashlib.sha256(module.encode("utf-8") + b"\0" + content).hexdigest()
    existing = db.scalar(select(ImportBatch).where(ImportBatch.sha256 == digest))
    if existing:
        std_count = db.scalar(select(func.count()).select_from(ModuleStandardRecord).where(ModuleStandardRecord.batch_id == existing.id)) or 0
        return {"batch_id": existing.id, "rows": existing.row_count, "standard_rows": std_count, "duplicated": True, "archive": "", "status": existing.status}
    archive = archive_upload(module, filename, content, data_root=data_root)
    batch = ImportBatch(kind=f"module:{module}", filename=filename, sha256=digest, status="raw_staged")
    db.add(batch)
    db.flush()
    count = 0
    for idx, raw in enumerate(rows, start=2):
        db.add(ModuleRawRecord(
            batch_id=batch.id,
            module=module,
            row_no=idx,
            period=_period_from_row(raw),
            external_key=_external_key(raw),
            raw_json=json.dumps(raw, ensure_ascii=False, default=str),
        ))
        count += 1
    batch.row_count = count
    db.commit()
    preview = batch_preview(db, batch.id, limit=3)
    if preview.get("missing_required"):
        batch.status = "mapping_required"
        db.commit()
    return {
        "batch_id": batch.id,
        "rows": count,
        "standard_rows": 0,
        "duplicated": False,
        "archive": archive,
        "status": "raw_staged",
        "headers": preview["headers"],
        "auto_mapping": preview.get("auto_mapping", {}),
        "missing_required": preview.get("missing_required", []),
    }


def _norm_header(value: Any) -> str:
    text = str(value or "").strip().lower()
    return re.sub(r"[\s_\-—–:：/\\()（）\[\]【】]+", "", text)


def _source_headers(db: Session, batch_id: int) -> list[str]:
    raw = db.scalar(select(ModuleRawRecord).where(ModuleRawRecord.batch_id == batch_id).order_by(ModuleRawRecord.row_no).limit(1))
    if not raw:
        return []
    obj = json.loads(raw.raw_json or "{}")
    return [str(x) for x in obj.keys()]


def auto_mapping(module: str, headers: list[str]) -> dict[str, str]:
    schema = MODULE_SCHEMAS.get(module)
    if not schema:
        return {}
    normalized = {_norm_header(h): h for h in headers}
    out: dict[str, str] = {}
    for field, cfg in schema["fields"].items():
        candidates = [field, cfg.get("label", ""), *(cfg.get("aliases") or [])]
        for candidate in candidates:
            hit = normalized.get(_norm_header(candidate))
            if hit:
                out[field] = hit
                break
    return out


def _missing_required(module: str, mapping: dict[str, str]) -> list[str]:
    schema = MODULE_SCHEMAS.get(module, {}).get("fields", {})
    return [name for name, cfg in schema.items() if cfg.get("required") and not mapping.get(name)]


def batch_preview(db: Session, batch_id: int, limit: int = 20) -> dict[str, Any]:
    batch = db.get(ImportBatch, batch_id)
    if not batch or not batch.kind.startswith("module:"):
        raise ValueError("原始资料批次不存在")
    module = batch.kind.split(":", 1)[1]
    rows = list(db.scalars(select(ModuleRawRecord).where(ModuleRawRecord.batch_id == batch_id).order_by(ModuleRawRecord.row_no).limit(max(1, min(limit, 100)))).all())
    parsed = [json.loads(x.raw_json or "{}") for x in rows]
    headers = list(parsed[0].keys()) if parsed else []
    mapping = auto_mapping(module, headers)
    missing = _missing_required(module, mapping)
    return {
        "batch_id": batch.id,
        "module": module,
        "filename": batch.filename,
        "status": batch.status,
        "row_count": batch.row_count,
        "headers": headers,
        "rows": parsed,
        "auto_mapping": mapping,
        "missing_required": missing,
        "can_auto_normalize": not missing,
    }


def list_module_batches(db: Session, module: str, limit: int = 30) -> list[dict[str, Any]]:
    if module not in MODULES:
        raise ValueError("模块不存在")
    batches = list(db.scalars(select(ImportBatch).where(ImportBatch.kind == f"module:{module}").order_by(ImportBatch.id.desc()).limit(max(1, min(limit, 100)))).all())
    result = []
    for b in batches:
        std_count = db.scalar(select(func.count()).select_from(ModuleStandardRecord).where(ModuleStandardRecord.batch_id == b.id)) or 0
        invalid = db.scalar(select(func.count()).select_from(ModuleStandardRecord).where(ModuleStandardRecord.batch_id == b.id, ModuleStandardRecord.status != "valid")) or 0
        result.append({
            "id": b.id, "filename": b.filename, "rows": b.row_count, "standard_rows": std_count,
            "invalid_rows": invalid, "status": b.status, "created_at": b.created_at.isoformat() if b.created_at else None,
        })
    return result


def _to_decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return D("0")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return D(str(value))
    text = str(value).strip().replace(",", "").replace("￥", "").replace("¥", "").replace("元", "")
    if not text:
        return D("0")
    if text.startswith("(") and text.endswith(")"):
        text = "-" + text[1:-1]
    try:
        return D(text)
    except InvalidOperation as exc:
        raise ValueError(f"无法识别金额/数量：{value}") from exc


def _to_date(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).isoformat()
    text = str(value).strip()
    # 保留原值也比猜错更安全；可识别时转 ISO。
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).isoformat()
        except ValueError:
            pass
    return text


def _to_period(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m")
    text = str(value).strip()
    m = re.search(r"(20\d{2})[-/年.]?\s*(\d{1,2})", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    return ""


def _convert_value(value: Any, typ: str) -> Any:
    if typ == "decimal":
        return str(_to_decimal(value))
    if typ == "date":
        return _to_date(value)
    if typ == "period":
        return _to_period(value)
    return "" if value is None else str(value).strip()


def save_mapping_profile(db: Session, module: str, mapping: dict[str, str], name: str = "默认映射") -> ModuleMappingProfile:
    row = db.scalar(select(ModuleMappingProfile).where(ModuleMappingProfile.module == module, ModuleMappingProfile.name == name))
    if not row:
        row = ModuleMappingProfile(module=module, name=name)
        db.add(row)
    row.mapping_json = json.dumps(mapping, ensure_ascii=False)
    row.active = 1
    db.commit()
    db.refresh(row)
    return row


def list_mapping_profiles(db: Session, module: str) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(ModuleMappingProfile).where(ModuleMappingProfile.module == module, ModuleMappingProfile.active == 1).order_by(ModuleMappingProfile.id.desc())).all())
    return [{"id": x.id, "name": x.name, "mapping": json.loads(x.mapping_json or "{}"), "updated_at": x.updated_at.isoformat() if x.updated_at else None} for x in rows]


def normalize_module_batch(
    db: Session,
    batch_id: int,
    mapping: dict[str, str] | None = None,
    profile_name: str = "默认映射",
    save_profile: bool = True,
) -> dict[str, Any]:
    batch = db.get(ImportBatch, batch_id)
    if not batch or not batch.kind.startswith("module:"):
        raise ValueError("原始资料批次不存在")
    module = batch.kind.split(":", 1)[1]
    if module not in MODULE_SCHEMAS:
        raise ValueError("该模块不需要或暂不支持字段标准化")
    headers = _source_headers(db, batch_id)
    effective_mapping = dict(mapping or {}) or auto_mapping(module, headers)
    # 允许前端只补几个字段，其余仍使用自动识别。
    auto = auto_mapping(module, headers)
    for k, v in auto.items():
        effective_mapping.setdefault(k, v)
    missing = _missing_required(module, effective_mapping)
    if missing:
        batch.status = "mapping_required"
        db.commit()
        labels = {k: MODULE_SCHEMAS[module]["fields"][k].get("label", k) for k in missing}
        return {
            "ok": False,
            "batch_id": batch_id,
            "status": "mapping_required",
            "mapping": effective_mapping,
            "missing_required": [{"field": k, "label": labels[k]} for k in missing],
            "message": "缺少必需字段映射，请确认原始列名后再标准化。",
        }
    if save_profile:
        save_mapping_profile(db, module, effective_mapping, profile_name or "默认映射")
    db.execute(delete(ModuleStandardRecord).where(ModuleStandardRecord.batch_id == batch_id))
    schema = MODULE_SCHEMAS[module]
    fields = schema["fields"]
    raw_rows = list(db.scalars(select(ModuleRawRecord).where(ModuleRawRecord.batch_id == batch_id).order_by(ModuleRawRecord.row_no)).all())
    valid = 0
    invalid = 0
    errors: list[dict[str, Any]] = []
    for raw_rec in raw_rows:
        raw = json.loads(raw_rec.raw_json or "{}")
        standard: dict[str, Any] = {}
        row_errors: list[str] = []
        for field, cfg in fields.items():
            source = effective_mapping.get(field, "")
            value = raw.get(source) if source else None
            try:
                standard[field] = _convert_value(value, cfg.get("type", "text"))
            except ValueError as exc:
                standard[field] = ""
                row_errors.append(f"{cfg.get('label', field)}: {exc}")
        period = standard.get("period") or standard.get("recon_month") or standard.get("purchase_month")
        if not period:
            for date_field in ("settle_date", "bill_date", "purchase_date", "txn_date"):
                if standard.get(date_field):
                    period = _to_period(standard[date_field])
                    if period:
                        break
        period = str(period or raw_rec.period or "")[:7]
        key_field = schema.get("key_field") or ""
        external_key = str(standard.get(key_field) or raw_rec.external_key or "")[:255]
        for field, cfg in fields.items():
            if cfg.get("required") and standard.get(field) in (None, "", "0"):
                # 金额 0 允许存在；仅文本/日期必需字段判断空，decimal 的 0 是合法值。
                if cfg.get("type") != "decimal" or standard.get(field) in (None, ""):
                    row_errors.append(f"{cfg.get('label', field)}为空")
        status = "invalid" if row_errors else "valid"
        if row_errors:
            invalid += 1
            if len(errors) < 20:
                errors.append({"row_no": raw_rec.row_no, "errors": row_errors})
        else:
            valid += 1
        db.add(ModuleStandardRecord(
            batch_id=batch_id,
            module=module,
            row_no=raw_rec.row_no,
            period=period,
            external_key=external_key,
            standard_json=json.dumps(standard, ensure_ascii=False),
            status=status,
            error_text="；".join(row_errors)[:1000],
        ))
    batch.status = "standardized" if invalid == 0 else "standardized_with_errors"
    db.commit()
    return {
        "ok": True,
        "batch_id": batch_id,
        "module": module,
        "status": batch.status,
        "raw_rows": len(raw_rows),
        "valid_rows": valid,
        "invalid_rows": invalid,
        "mapping": effective_mapping,
        "errors": errors,
    }


def list_standard_records(db: Session, module: str, period: str = "", limit: int = 200) -> list[dict[str, Any]]:
    q = select(ModuleStandardRecord).where(ModuleStandardRecord.module == module)
    if period:
        q = q.where(ModuleStandardRecord.period == period)
    rows = list(db.scalars(q.order_by(ModuleStandardRecord.id.desc()).limit(max(1, min(limit, 1000)))).all())
    return [{
        "id": x.id, "batch_id": x.batch_id, "row_no": x.row_no, "period": x.period,
        "external_key": x.external_key, "status": x.status, "error": x.error_text,
        "data": json.loads(x.standard_json or "{}"),
    } for x in rows]


def standard_record_lineage(db: Session, record_id: int) -> dict[str, Any]:
    rec = db.get(ModuleStandardRecord, record_id)
    if not rec:
        raise ValueError("标准记录不存在")
    raw = db.scalar(select(ModuleRawRecord).where(ModuleRawRecord.batch_id == rec.batch_id, ModuleRawRecord.row_no == rec.row_no))
    batch = db.get(ImportBatch, rec.batch_id)
    return {
        "standard": {"id": rec.id, "module": rec.module, "period": rec.period, "external_key": rec.external_key, "status": rec.status, "data": json.loads(rec.standard_json or "{}")},
        "raw": json.loads(raw.raw_json or "{}") if raw else {},
        "batch": {"id": batch.id, "filename": batch.filename, "sha256": batch.sha256, "status": batch.status, "created_at": batch.created_at.isoformat() if batch and batch.created_at else None} if batch else None,
    }


def module_readiness(db: Session) -> list[dict[str, Any]]:
    out = []
    for key, cfg in MODULES.items():
        if key == "sales-outbound":
            raw = db.scalar(select(func.count()).select_from(StockoutOrder)) or 0
            adjustments = db.scalar(select(func.count()).select_from(ManualAdjustment).where(ManualAdjustment.module == "sales-outbound", ManualAdjustment.active == 1)) or 0
            out.append({"module": key, "name": cfg["name"], "state": "ready" if raw else "waiting", "raw_rows": raw, "standard_rows": raw, "active_adjustments": adjustments, "message": "旺店通销售出库已同步" if raw else "等待首次旺店通同步"})
            continue
        if key == "rebate":
            purchase_count = db.scalar(select(func.count()).select_from(ModuleStandardRecord).where(ModuleStandardRecord.module == "purchase", ModuleStandardRecord.status == "valid")) or 0
            rules = db.scalar(select(func.count()).select_from(RebateRule).where(RebateRule.active == 1)) or 0
            state = "ready" if purchase_count and rules else "waiting"
            out.append({"module": key, "name": cfg["name"], "state": state, "raw_rows": 0, "standard_rows": purchase_count, "active_rules": rules, "message": "采购标准数据和返利规则已就绪" if state == "ready" else "需要采购标准数据 + 返利规则"})
            continue
        raw = db.scalar(select(func.count()).select_from(ModuleRawRecord).where(ModuleRawRecord.module == key)) or 0
        std = db.scalar(select(func.count()).select_from(ModuleStandardRecord).where(ModuleStandardRecord.module == key, ModuleStandardRecord.status == "valid")) or 0
        invalid = db.scalar(select(func.count()).select_from(ModuleStandardRecord).where(ModuleStandardRecord.module == key, ModuleStandardRecord.status != "valid")) or 0
        pending_batches = db.scalar(select(func.count()).select_from(ImportBatch).where(ImportBatch.kind == f"module:{key}", ImportBatch.status.in_(["raw_staged", "mapping_required"]))) or 0
        state = "ready" if std else ("mapping" if raw else "waiting")
        out.append({"module": key, "name": cfg["name"], "state": state, "raw_rows": raw, "standard_rows": std, "invalid_rows": invalid, "pending_batches": pending_batches, "message": "标准数据已可供报表使用" if std else ("原始资料已上传，等待字段标准化" if raw else "等待上传原始资料")})
    return out


def create_adjustment(db: Session, payload: dict[str, Any]) -> ManualAdjustment:
    target_type = str(payload.get("target_type") or "order")
    operation = str(payload.get("operation") or "add")
    field_name = str(payload.get("field_name") or "")
    if target_type not in ALLOWED_TARGET_TYPES:
        raise ValueError(f"target_type 仅支持: {', '.join(sorted(ALLOWED_TARGET_TYPES))}")
    if operation not in ALLOWED_ADJUST_OPS:
        raise ValueError(f"operation 仅支持: {', '.join(sorted(ALLOWED_ADJUST_OPS))}")
    if operation not in {"exclude", "include"} and field_name not in NUMERIC_STOCKOUT_FIELDS | TEXT_STOCKOUT_FIELDS:
        raise ValueError("不支持调整该字段")
    if operation in {"add", "replace", "multiply"} and field_name not in NUMERIC_STOCKOUT_FIELDS:
        raise ValueError("add/replace/multiply 只能用于金额/数量字段")
    if operation == "text_replace" and field_name not in TEXT_STOCKOUT_FIELDS:
        raise ValueError("text_replace 只能用于文本字段")
    row = ManualAdjustment(
        module="sales-outbound",
        target_type=target_type,
        target_key=str(payload.get("target_key") or "").strip(),
        period=str(payload.get("period") or "").strip(),
        field_name=field_name,
        operation=operation,
        decimal_value=(D(str(payload["decimal_value"])) if payload.get("decimal_value") not in (None, "") else None),
        text_value=str(payload.get("text_value") or ""),
        reason=str(payload.get("reason") or ""),
        active=1 if payload.get("active", True) else 0,
    )
    if not row.target_key:
        raise ValueError("target_key 不能为空")
    if not row.reason:
        raise ValueError("手工调整必须填写原因")
    if operation in {"add", "replace", "multiply"} and row.decimal_value is None:
        raise ValueError("数值调整必须填写 decimal_value")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def list_adjustments(db: Session, period: str = "") -> list[ManualAdjustment]:
    q = select(ManualAdjustment).where(ManualAdjustment.module == "sales-outbound")
    if period:
        q = q.where((ManualAdjustment.period == period) | (ManualAdjustment.period == ""))
    return list(db.scalars(q.order_by(ManualAdjustment.id.desc())).all())


def _matches_adjustment(adj: ManualAdjustment, order: StockoutOrder, month: str) -> bool:
    if not adj.active:
        return False
    if adj.period and adj.period != month:
        return False
    if adj.target_type == "order":
        return adj.target_key == order.stockout_no
    if adj.target_type == "shop":
        return adj.target_key == order.shop_name
    if adj.target_type == "platform":
        return adj.target_key == order.platform_id
    if adj.target_type == "month":
        return adj.target_key == month
    return False


def effective_stockout_rows(db: Session, month: str) -> list[dict[str, Any]]:
    start = month_start(month)
    if start.month == 12:
        end = datetime(start.year + 1, 1, 1)
    else:
        end = datetime(start.year, start.month + 1, 1)
    orders = list(db.scalars(select(StockoutOrder).where(StockoutOrder.consign_time >= start, StockoutOrder.consign_time < end).order_by(StockoutOrder.id)).all())
    adjustments = list_adjustments(db, month)
    out = []
    for order in orders:
        raw = {
            "goods_count": D(str(order.goods_count or 0)),
            "goods_total_amount": D(str(order.goods_total_amount or 0)),
            "goods_total_cost": D(str(order.goods_total_cost or 0)),
            "receivable": D(str(order.receivable or 0)),
            "actual_receivable": D(str(order.actual_receivable or 0)),
            "paid": D(str(order.paid or 0)),
            "post_cost": D(str(order.post_cost or 0)),
            "package_cost": D(str(order.package_cost or 0)),
            "shop_name": order.shop_name,
            "shop_no": order.shop_no,
            "platform_id": order.platform_id,
            "status": order.status,
            "refund_status": order.refund_status,
        }
        eff = dict(raw)
        excluded = False
        applied: list[int] = []
        for adj in sorted((a for a in adjustments if _matches_adjustment(a, order, month)), key=lambda a: a.id):
            if adj.operation == "exclude":
                excluded = True
            elif adj.operation == "include":
                excluded = False
            elif adj.operation == "add" and adj.field_name in NUMERIC_STOCKOUT_FIELDS:
                eff[adj.field_name] = D(str(eff.get(adj.field_name) or 0)) + D(str(adj.decimal_value or 0))
            elif adj.operation == "replace" and adj.field_name in NUMERIC_STOCKOUT_FIELDS:
                eff[adj.field_name] = D(str(adj.decimal_value or 0))
            elif adj.operation == "multiply" and adj.field_name in NUMERIC_STOCKOUT_FIELDS:
                eff[adj.field_name] = D(str(eff.get(adj.field_name) or 0)) * D(str(adj.decimal_value or 1))
            elif adj.operation == "text_replace" and adj.field_name in TEXT_STOCKOUT_FIELDS:
                eff[adj.field_name] = adj.text_value
            applied.append(adj.id)
        out.append({
            "stockout_no": order.stockout_no,
            "consign_time": order.consign_time.isoformat() if order.consign_time else None,
            "raw": {k: float(v) if isinstance(v, Decimal) else v for k, v in raw.items()},
            "effective": {k: float(v) if isinstance(v, Decimal) else v for k, v in eff.items()},
            "excluded": excluded,
            "adjustment_ids": applied,
        })
    return out


def stockout_adjustment_summary(db: Session, month: str) -> dict[str, Any]:
    rows = effective_stockout_rows(db, month)
    numeric = sorted(NUMERIC_STOCKOUT_FIELDS)
    raw_totals = {k: D("0") for k in numeric}
    eff_totals = {k: D("0") for k in numeric}
    excluded = 0
    changed = 0
    for row in rows:
        if row["excluded"]:
            excluded += 1
        if row["adjustment_ids"]:
            changed += 1
        for k in numeric:
            raw_totals[k] += D(str(row["raw"].get(k) or 0))
            if not row["excluded"]:
                eff_totals[k] += D(str(row["effective"].get(k) or 0))
    return {
        "month": month, "orders": len(rows), "adjusted_orders": changed, "excluded_orders": excluded,
        "raw_totals": {k: float(v) for k, v in raw_totals.items()},
        "effective_totals": {k: float(v) for k, v in eff_totals.items()},
        "delta": {k: float(eff_totals[k] - raw_totals[k]) for k in numeric},
    }


def create_rebate_rule(db: Session, payload: dict[str, Any]) -> RebateRule:
    basis = str(payload.get("basis_field") or "invoice_amount_untaxed")
    if basis not in ALLOWED_REBATE_BASIS:
        raise ValueError(f"basis_field 仅支持: {', '.join(sorted(ALLOWED_REBATE_BASIS))}")
    row = RebateRule(
        brand=str(payload.get("brand") or "*").strip() or "*",
        channel=str(payload.get("channel") or "*").strip() or "*",
        category=str(payload.get("category") or "品牌返利").strip(),
        basis_field=basis,
        rate=D(str(payload.get("rate") or 0)),
        tax_factor=D(str(payload.get("tax_factor") or 1)),
        effective_from=normalize_month(str(payload.get("effective_from") or "")),
        effective_to=normalize_month(str(payload.get("effective_to") or "")),
        priority=int(payload.get("priority") or 100),
        active=1 if payload.get("active", True) else 0,
        note=str(payload.get("note") or ""),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def update_rebate_rule(db: Session, rule_id: int, payload: dict[str, Any]) -> RebateRule:
    row = db.get(RebateRule, rule_id)
    if not row:
        raise ValueError("返利规则不存在")
    basis = str(payload.get("basis_field") or row.basis_field or "invoice_amount_untaxed")
    if basis not in ALLOWED_REBATE_BASIS:
        raise ValueError(f"basis_field 仅支持: {', '.join(sorted(ALLOWED_REBATE_BASIS))}")
    row.brand = str(payload.get("brand") or "*").strip() or "*"
    row.channel = str(payload.get("channel") or "*").strip() or "*"
    row.category = str(payload.get("category") or "渠道返利").strip() or "渠道返利"
    row.basis_field = basis
    row.rate = D(str(payload.get("rate") if payload.get("rate") is not None else row.rate or 0))
    row.tax_factor = D(str(payload.get("tax_factor") if payload.get("tax_factor") is not None else row.tax_factor or 1))
    row.effective_from = normalize_month(str(payload.get("effective_from") or ""))
    row.effective_to = normalize_month(str(payload.get("effective_to") or ""))
    row.priority = int(payload.get("priority") if payload.get("priority") is not None else row.priority or 100)
    row.active = 1 if payload.get("active", bool(row.active)) else 0
    row.note = str(payload.get("note") or "")
    db.commit()
    db.refresh(row)
    return row


def list_rebate_rules(db: Session) -> list[RebateRule]:
    return list(db.scalars(select(RebateRule).order_by(RebateRule.priority, RebateRule.id)).all())


def _rule_matches_dict(rule: RebateRule, purchase: dict[str, Any], month: str) -> bool:
    if not rule.active:
        return False
    m = normalize_month(month)
    ef = normalize_month(rule.effective_from)
    et = normalize_month(rule.effective_to)
    if ef and m < ef:
        return False
    if et and m > et:
        return False
    if rule.brand not in {"*", ""} and rule.brand != str(purchase.get("brand") or ""):
        return False
    if rule.channel not in {"*", ""} and rule.channel != str(purchase.get("channel") or ""):
        return False
    return True


def _rebate_rule_rank(rule: RebateRule, purchase: dict[str, Any]) -> tuple[int, int, int, int]:
    """Channel is the first dimension, brand is the second, then manual priority.

    Exact channel beats wildcard channel. Within the same channel specificity, exact
    brand beats all-brand. This keeps channel configuration authoritative even when
    users leave every rule at the default priority 100.
    """
    channel_exact = rule.channel not in {"*", ""} and rule.channel == str(purchase.get("channel") or "")
    brand_exact = rule.brand not in {"*", ""} and rule.brand == str(purchase.get("brand") or "")
    return (0 if channel_exact else 1, 0 if brand_exact else 1, int(rule.priority or 100), int(rule.id or 0))


def _standard_purchase_rows(db: Session, month: str) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(ModuleStandardRecord).where(
        ModuleStandardRecord.module == "purchase",
        ModuleStandardRecord.status == "valid",
        ModuleStandardRecord.period == month,
    ).order_by(ModuleStandardRecord.id)).all())
    out = []
    for r in rows:
        d = json.loads(r.standard_json or "{}")
        d["_standard_id"] = r.id
        d["_batch_id"] = r.batch_id
        out.append(d)
    return out


def _legacy_purchase_rows(db: Session, month: str) -> list[dict[str, Any]]:
    batch = latest_legacy_batch(db)
    if not batch:
        return []
    rows = list(db.scalars(select(LegacyPurchaseRow).where(LegacyPurchaseRow.batch_id == batch.id, LegacyPurchaseRow.recon_month == month)).all())
    return [{
        "_legacy_id": p.id,
        "brand": p.subbrand,
        "channel": p.channel,
        "purchase_no": "",
        "product_code": p.product_code,
        "barcode": p.barcode,
        "goods_name": p.goods_name,
        "list_amount_untaxed": str(p.list_amount_untaxed or 0),
        "invoice_amount_untaxed": str(p.invoice_amount_untaxed or 0),
        "invoice_amount_tax": str(p.invoice_amount_tax or 0),
        "qty_units": str(p.qty_units or 0),
    } for p in rows]


def rebate_channel_catalog(db: Session) -> dict[str, Any]:
    """Return channel-first suggestions from standardized purchase data and existing rules."""
    channels: set[str] = set()
    brands_by_channel: dict[str, set[str]] = {}
    rows = list(db.scalars(select(ModuleStandardRecord).where(
        ModuleStandardRecord.module == "purchase",
        ModuleStandardRecord.status == "valid",
    ).order_by(ModuleStandardRecord.id.desc()).limit(5000)).all())
    for row in rows:
        try:
            d = json.loads(row.standard_json or "{}")
        except Exception:
            continue
        channel = str(d.get("channel") or "").strip()
        brand = str(d.get("brand") or "").strip()
        if channel:
            channels.add(channel)
            if brand:
                brands_by_channel.setdefault(channel, set()).add(brand)
    for rule in list_rebate_rules(db):
        channel = str(rule.channel or "").strip()
        brand = str(rule.brand or "").strip()
        if channel and channel != "*":
            channels.add(channel)
            if brand and brand != "*":
                brands_by_channel.setdefault(channel, set()).add(brand)
    return {
        "channels": sorted(channels),
        "brands_by_channel": {k: sorted(v) for k, v in sorted(brands_by_channel.items())},
        "basis_options": [
            {"value": "invoice_amount_untaxed", "label": "采购发票未税金额"},
            {"value": "list_amount_untaxed", "label": "采购牌价未税金额"},
            {"value": "invoice_amount_tax", "label": "采购发票含税金额"},
        ],
        "category_examples": ["运营费", "广告费", "物流费", "渠道奖励", "年度返利", "其他返利"],
        "rule": "渠道为第一匹配维度；品牌为可选二级筛选；同渠道可叠加多个不同返利组成项。",
    }


def calculate_purchase_rebate(db: Session, month: str) -> dict[str, Any]:
    purchases = _standard_purchase_rows(db, month)
    basis_source = "purchase_orders"
    purchase_source = "purchase_standard_records"
    fallback = False
    if not purchases:
        purchases = _legacy_purchase_rows(db, month)
        purchase_source = "legacy_purchase_rows"
        fallback = bool(purchases)
    if not purchases:
        return {"ready": False, "message": "当前还没有可计算的采购标准数据；请先上传采购原始资料并完成字段标准化。", "basis_source": basis_source, "purchase_source": "purchase_standard_records"}
    rules = list_rebate_rules(db)
    if not rules:
        return {
            "ready": False,
            "message": "采购数据已存在，但尚未配置渠道返利规则。返利不会写死；请先选择渠道，再填写该渠道的返利组成项和比例。",
            "purchase_rows": len(purchases),
            "basis_source": basis_source,
            "purchase_source": purchase_source,
            "legacy_fallback": fallback,
        }
    lines = []
    total = D("0")
    category_totals: dict[str, Decimal] = {}
    channel_totals: dict[str, Decimal] = {}
    channel_category_totals: dict[str, dict[str, Decimal]] = {}
    matched_purchase_ids: set[str] = set()
    unmatched = 0
    unmatched_by_channel: dict[str, int] = {}
    unmatched_examples = []
    for p in purchases:
        matched = [r for r in rules if _rule_matches_dict(r, p, month)]
        if not matched:
            unmatched += 1
            channel_name = str(p.get("channel") or "未填写渠道")
            unmatched_by_channel[channel_name] = unmatched_by_channel.get(channel_name, 0) + 1
            if len(unmatched_examples) < 20:
                unmatched_examples.append({"brand": p.get("brand", ""), "channel": p.get("channel", ""), "purchase_no": p.get("purchase_no", "")})
            continue
        matched_purchase_ids.add(str(p.get("_standard_id") or p.get("_legacy_id") or p.get("purchase_no") or id(p)))
        # 同一采购单允许叠加多个返利组成项；同一 category 若同时命中多条规则，
        # 先按“渠道精确 > 渠道通配”，再按“品牌精确 > 全部品牌”，最后才比较 priority。
        chosen: dict[str, RebateRule] = {}
        for rule in matched:
            category = rule.category or "渠道返利"
            current = chosen.get(category)
            if current is None or _rebate_rule_rank(rule, p) < _rebate_rule_rank(current, p):
                chosen[category] = rule
        for category, rule in sorted(chosen.items(), key=lambda kv: (kv[1].priority, kv[1].id)):
            basis = _to_decimal(p.get(rule.basis_field))
            amount = basis * D(str(rule.rate or 0)) * D(str(rule.tax_factor or 1))
            total += amount
            category_totals[category] = category_totals.get(category, D("0")) + amount
            channel_name = str(p.get("channel") or "未填写渠道")
            channel_totals[channel_name] = channel_totals.get(channel_name, D("0")) + amount
            channel_category_totals.setdefault(channel_name, {})[category] = channel_category_totals.setdefault(channel_name, {}).get(category, D("0")) + amount
            lines.append({
                "purchase_record_id": p.get("_standard_id") or p.get("_legacy_id"),
                "purchase_no": p.get("purchase_no", ""),
                "brand": p.get("brand", ""),
                "channel": p.get("channel", ""),
                "category": category,
                "basis_field": rule.basis_field,
                "basis_amount": float(basis),
                "rate": float(rule.rate),
                "tax_factor": float(rule.tax_factor),
                "rebate_amount": float(amount),
                "rule_id": rule.id,
            })
    return {
        "ready": True,
        "month": month,
        "basis_source": basis_source,
        "purchase_source": purchase_source,
        "legacy_fallback": fallback,
        "purchase_rows": len(purchases),
        "matched_rows": len(lines),
        "matched_purchase_rows": len(matched_purchase_ids),
        "rule_lines": len(lines),
        "unmatched_rows": unmatched,
        "unmatched_by_channel": unmatched_by_channel,
        "unmatched_examples": unmatched_examples,
        "rebate_total": float(total),
        "category_totals": {k: float(v) for k, v in category_totals.items()},
        "channel_totals": {k: float(v) for k, v in channel_totals.items()},
        "channel_category_totals": {ch: {k: float(v) for k, v in cats.items()} for ch, cats in channel_category_totals.items()},
        "lines": lines,
    }
