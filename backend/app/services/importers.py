from __future__ import annotations
import csv
import hashlib
import io
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..models import BankRow, ImportBatch, SettlementRow

HEADER_ALIASES = {
    "order_no": ["订单号", "平台订单号", "商户订单号", "原始单号", "交易订单号", "订单编号"],
    "platform": ["平台", "渠道", "平台名称"],
    "shop_name": ["店铺", "店铺名称", "网店"],
    "settle_date": ["结算日期", "账务日期", "入账日期", "结算时间", "日期"],
    "gross_amount": ["订单金额", "交易金额", "商品金额", "应收金额", "收入金额"],
    "platform_fee": ["平台服务费", "佣金", "技术服务费", "手续费", "平台费用"],
    "refund_amount": ["退款金额", "退款", "售后退款"],
    "other_fee": ["其他费用", "其他扣款", "推广费", "运费险"],
    "net_amount": ["实收金额", "净额", "结算金额", "到账金额", "收入净额"],
    "txn_date": ["交易日期", "记账日期", "入账日期", "交易时间", "日期"],
    "reference_no": ["流水号", "交易流水号", "参考号", "凭证号"],
    "counterparty": ["对方户名", "对方名称", "交易对手", "付款方", "收款方"],
    "amount": ["金额", "收入", "贷方发生额", "入账金额", "交易金额"],
    "memo": ["摘要", "备注", "用途", "附言"],
}


def _norm(value) -> str:
    return str(value or "").strip().replace("\n", "")


def _decimal(value) -> Decimal:
    text = _norm(value).replace(",", "").replace("¥", "").replace("￥", "")
    if not text:
        return Decimal("0")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def _date(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = _norm(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _rows_from_xlsx(content: bytes) -> list[dict[str, object]]:
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    iterator = ws.iter_rows(values_only=True)
    headers = [_norm(v) for v in next(iterator)]
    return [{headers[i]: values[i] for i in range(min(len(headers), len(values))) if headers[i]} for values in iterator if any(v not in (None, "") for v in values)]


def _rows_from_csv(content: bytes) -> list[dict[str, object]]:
    text = None
    for encoding in ("utf-8-sig", "gb18030", "gbk"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("CSV 编码无法识别")
    return list(csv.DictReader(io.StringIO(text)))


def read_tabular(filename: str, content: bytes) -> list[dict[str, object]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".xlsx":
        return _rows_from_xlsx(content)
    if suffix == ".csv":
        return _rows_from_csv(content)
    raise ValueError("仅支持 XLSX / CSV")


def _find(row: dict[str, object], logical: str):
    normalized = {_norm(k): v for k, v in row.items()}
    for alias in HEADER_ALIASES[logical]:
        if alias in normalized:
            return normalized[alias]
    return None


def create_batch(db: Session, kind: str, filename: str, content: bytes) -> ImportBatch:
    digest = hashlib.sha256(content).hexdigest()
    existing = db.scalar(select(ImportBatch).where(ImportBatch.sha256 == digest))
    if existing:
        return existing
    batch = ImportBatch(kind=kind, filename=filename, sha256=digest)
    db.add(batch)
    db.flush()
    return batch


def import_settlement(db: Session, filename: str, content: bytes) -> tuple[ImportBatch, int, bool]:
    digest = hashlib.sha256(content).hexdigest()
    existing = db.scalar(select(ImportBatch).where(ImportBatch.sha256 == digest))
    if existing:
        return existing, existing.row_count, True
    rows = read_tabular(filename, content)
    batch = create_batch(db, "settlement", filename, content)
    count = 0
    for raw in rows:
        order_no = _norm(_find(raw, "order_no"))
        if not order_no:
            continue
        gross = _decimal(_find(raw, "gross_amount"))
        fee = _decimal(_find(raw, "platform_fee"))
        refund = _decimal(_find(raw, "refund_amount"))
        other = _decimal(_find(raw, "other_fee"))
        net_value = _find(raw, "net_amount")
        net = _decimal(net_value) if _norm(net_value) else gross - fee - refund - other
        db.add(SettlementRow(
            batch_id=batch.id,
            platform=_norm(_find(raw, "platform")),
            shop_name=_norm(_find(raw, "shop_name")),
            order_no=order_no,
            settle_date=_date(_find(raw, "settle_date")),
            gross_amount=gross,
            platform_fee=fee,
            refund_amount=refund,
            other_fee=other,
            net_amount=net,
            raw_json=json.dumps(raw, ensure_ascii=False, default=str),
        ))
        count += 1
    batch.row_count = count
    db.commit()
    return batch, count, False


def import_bank(db: Session, filename: str, content: bytes) -> tuple[ImportBatch, int, bool]:
    digest = hashlib.sha256(content).hexdigest()
    existing = db.scalar(select(ImportBatch).where(ImportBatch.sha256 == digest))
    if existing:
        return existing, existing.row_count, True
    rows = read_tabular(filename, content)
    batch = create_batch(db, "bank", filename, content)
    count = 0
    for raw in rows:
        amount = _decimal(_find(raw, "amount"))
        if amount == 0 and not _norm(_find(raw, "reference_no")):
            continue
        db.add(BankRow(
            batch_id=batch.id,
            txn_date=_date(_find(raw, "txn_date")),
            reference_no=_norm(_find(raw, "reference_no")),
            counterparty=_norm(_find(raw, "counterparty")),
            amount=amount,
            memo=_norm(_find(raw, "memo")),
            raw_json=json.dumps(raw, ensure_ascii=False, default=str),
        ))
        count += 1
    batch.row_count = count
    db.commit()
    return batch, count, False
