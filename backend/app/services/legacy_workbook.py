from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Iterable, Iterator

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from ..models import (
    ImportBatch,
    LegacyIncomeRow,
    LegacyInventoryRow,
    LegacyLogisticsRow,
    LegacyPrepaidRow,
    LegacyPurchaseRow,
    LegacyRebateRow,
    LegacyShipmentRow,
)

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
RID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
EXCEL_EPOCH = datetime(1899, 12, 30)


def _text(v) -> str:
    if v is None:
        return ""
    return str(v).strip().replace("\t", "").lstrip("'")


def _decimal(v) -> Decimal:
    s = _text(v).replace(",", "").replace("￥", "").replace("¥", "")
    if not s or s.startswith("#"):
        return Decimal("0")
    try:
        return Decimal(s)
    except InvalidOperation:
        return Decimal("0")


def _month(v) -> str:
    s = _text(v)
    m = re.search(r"(20\d{2})[-/.年](\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    try:
        n = float(s)
        if 30000 <= n <= 80000:
            return (EXCEL_EPOCH + timedelta(days=n)).strftime("%Y-%m")
    except ValueError:
        pass
    return ""


def _datetime(v) -> datetime | None:
    s = _text(v)
    if not s:
        return None
    try:
        n = float(s)
        if 30000 <= n <= 80000:
            return EXCEL_EPOCH + timedelta(days=n)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _header(v) -> str:
    return re.sub(r"\s+", "", _text(v)).replace("（", "(").replace("）", ")")


def _col(cell_ref: str) -> str:
    m = re.match(r"[A-Z]+", cell_ref or "")
    return m.group(0) if m else ""


class XlsxReader:
    """Small streaming OOXML reader.

    We only need cached values from formula cells, so no Excel engine is required.
    This also keeps the 14万+ row shipment sheet import memory-bounded.
    """

    def __init__(self, content: bytes):
        self.buf = io.BytesIO(content)
        self.z = zipfile.ZipFile(self.buf)
        self.shared_strings = self._load_shared_strings()
        self.sheet_map = self._load_sheet_map()

    def close(self):
        self.z.close()

    def _load_shared_strings(self) -> list[str]:
        if "xl/sharedStrings.xml" not in self.z.namelist():
            return []
        out: list[str] = []
        with self.z.open("xl/sharedStrings.xml") as f:
            for event, elem in ET.iterparse(f, events=("end",)):
                if elem.tag.endswith("}si"):
                    out.append("".join(t.text or "" for t in elem.iter() if t.tag.endswith("}t")))
                    elem.clear()
        return out

    def _load_sheet_map(self) -> dict[str, str]:
        wb = ET.fromstring(self.z.read("xl/workbook.xml"))
        rel_root = ET.fromstring(self.z.read("xl/_rels/workbook.xml.rels"))
        rels = {e.attrib["Id"]: e.attrib["Target"] for e in rel_root}
        result: dict[str, str] = {}
        for s in wb.find("m:sheets", NS) or []:
            target = rels[s.attrib[RID]]
            result[s.attrib["name"]] = posixpath.normpath(posixpath.join("xl", target)) if not target.startswith("xl/") else target
        return result

    def _cell_value(self, c: ET.Element):
        typ = c.attrib.get("t", "")
        if typ == "inlineStr":
            return "".join(t.text or "" for t in c.iter() if t.tag.endswith("}t"))
        v = c.find("m:v", NS)
        if v is None:
            return ""
        raw = v.text or ""
        if typ == "s":
            try:
                return self.shared_strings[int(raw)]
            except (ValueError, IndexError):
                return raw
        if typ == "b":
            return raw == "1"
        return raw

    def iter_rows(self, sheet_name: str) -> Iterator[tuple[int, dict[str, object]]]:
        target = self.sheet_map.get(sheet_name)
        if not target:
            return
        with self.z.open(target) as f:
            for event, elem in ET.iterparse(f, events=("end",)):
                if elem.tag.endswith("}row"):
                    rn = int(elem.attrib.get("r", "0") or 0)
                    row: dict[str, object] = {}
                    for c in elem.findall("m:c", NS):
                        row[_col(c.attrib.get("r", ""))] = self._cell_value(c)
                    yield rn, row
                    elem.clear()

    def iter_dicts(self, sheet_name: str, header_row: int = 1) -> Iterator[dict[str, object]]:
        headers: dict[str, str] = {}
        for rn, row in self.iter_rows(sheet_name):
            if rn == header_row:
                headers = {col: _header(v) for col, v in row.items() if _header(v)}
                continue
            if rn <= header_row or not headers:
                continue
            out = {headers[col]: v for col, v in row.items() if col in headers}
            if any(_text(v) for v in out.values()):
                yield out


def _get(row: dict[str, object], *names: str):
    for name in names:
        key = _header(name)
        if key in row:
            return row[key]
    return ""


def _bulk(db: Session, model, mappings: Iterable[dict], chunk_size: int = 2500) -> int:
    buf: list[dict] = []
    total = 0
    for item in mappings:
        buf.append(item)
        if len(buf) >= chunk_size:
            db.execute(insert(model), buf)
            total += len(buf)
            buf.clear()
    if buf:
        db.execute(insert(model), buf)
        total += len(buf)
    return total


INCOME_SPECS = {
    "收入-快手": dict(platform="快手", period="下单日期", amount="结算金额", shop="店铺", order="订单号", brand="", txn="", note=""),
    "收入-抖音": dict(platform="抖音", period="下单时间", amount="结算金额", shop="", shop_const="梦奇思-抖音-梦奇思零食专营店", order="订单号", brand="", txn="结算单类型", note="商品名称"),
    "收入-PDD": dict(platform="拼多多", period="订单月份", amount="结算金额", shop="店铺", order="商户订单号", brand="品牌", txn="账务类型", note="业务描述"),
    "收入-JD": dict(platform="京东", period="时间", amount="金额", shop="", shop_const="聚小翻-京东-慧采", order="订单号", brand="", txn="应付帐单据类型", note="备注"),
    "收入-得物": dict(platform="得物", period="结算月份", amount="应结金额", shop="", shop_const="聚小翻-得物-聚小翻（上海）贸易有限公司", order="账单编号", brand="", txn="结算状态", note=""),
    "收入-支付宝": dict(platform="1688/支付宝", period="时间", amount="金额", shop="店铺", order="订单号", brand="", txn="业务类型", note="备注"),
    "收入-分销渠道": dict(platform="分销", period="下单时间", amount="客户-付款金额", shop="店铺", order="订单号", brand="", txn="类型", note=""),
}


def import_0610_workbook(db: Session, filename: str, content: bytes) -> tuple[ImportBatch, dict[str, int], bool]:
    digest = hashlib.sha256(content).hexdigest()
    existing = db.scalar(select(ImportBatch).where(ImportBatch.sha256 == digest))
    if existing and existing.status == "imported":
        return existing, {"total": existing.row_count}, True

    # 失败过的同一文件允许直接重试。V0.3 会保留 failed 批次，
    # 如果仍按 SHA256 当成“重复导入”，修复字段后也无法再次导入。
    if existing:
        batch = existing
        batch.filename = filename
        batch.row_count = 0
        batch.status = "importing"
    else:
        batch = ImportBatch(kind="legacy_workbook", filename=filename, sha256=digest, status="importing")
        db.add(batch)
    db.flush()
    counts: dict[str, int] = {}
    reader = XlsxReader(content)
    try:
        def shipments():
            # 发货明细账 V 列“品牌”是隐藏辅助列，原文件 V1 没有标题，必须按列位读取。
            for rn, r in reader.iter_rows("发货明细账"):
                if rn <= 1:
                    continue
                shop = _text(r.get("M"))
                goods_cost = _decimal(r.get("Q"))
                order_no = _text(r.get("J"))
                src_order_no = _text(r.get("K"))
                stockout_no = _text(r.get("I"))
                if not (shop or order_no or src_order_no or stockout_no or goods_cost):
                    continue
                consign = _datetime(r.get("L"))
                recon_month = _month(r.get("C")) or (consign.strftime("%Y-%m") if consign else "")
                standard = _decimal(r.get("F"))
                if not standard and goods_cost:
                    standard = goods_cost / Decimal("0.8")
                yield {
                    "batch_id": batch.id,
                    "recon_month": recon_month,
                    "actual_profit": _decimal(r.get("D")),
                    "settlement_income": _decimal(r.get("E")),
                    "standard_cost_untaxed": standard,
                    "fee_ratio": _decimal(r.get("G")),
                    "order_type": _text(r.get("H")),
                    "stockout_no": stockout_no,
                    "order_no": order_no,
                    "src_order_no": src_order_no,
                    "consign_time": consign,
                    "shop_name": shop,
                    "warehouse": _text(r.get("N")),
                    "logistics_company": _text(r.get("O")),
                    "logistics_no": _text(r.get("P")),
                    "goods_cost": goods_cost,
                    "order_amount": _decimal(r.get("R")),
                    "goods_qty": _decimal(r.get("S")),
                    "estimated_profit": _decimal(r.get("T")),
                    "shipment_profit": _decimal(r.get("U")),
                    "brand": _text(r.get("V")),
                }
        counts["shipments"] = _bulk(db, LegacyShipmentRow, shipments())

        def income_rows():
            for sheet, spec in INCOME_SPECS.items():
                if sheet not in reader.sheet_map:
                    continue
                for r in reader.iter_dicts(sheet):
                    period = _month(_get(r, spec["period"]))
                    amount = _decimal(_get(r, spec["amount"]))
                    shop = spec.get("shop_const") or _text(_get(r, spec.get("shop", "")))
                    order_no = _text(_get(r, spec.get("order", "")))
                    if not period or (amount == 0 and not order_no):
                        continue
                    yield {
                        "batch_id": batch.id,
                        "source_sheet": sheet,
                        "platform": spec["platform"],
                        "period": period,
                        "shop_name": shop,
                        "order_no": order_no,
                        "amount": amount,
                        "brand": _text(_get(r, spec.get("brand", ""))),
                        "txn_type": _text(_get(r, spec.get("txn", ""))),
                        "raw_note": _text(_get(r, spec.get("note", "")))[:500],
                    }
        counts["income"] = _bulk(db, LegacyIncomeRow, income_rows())

        def logistics():
            for r in reader.iter_dicts("物流费"):
                period = _month(_get(r, "账单周期"))
                shop = _text(_get(r, "店铺"))
                amount = _decimal(_get(r, "TTL"))
                if period and shop and amount:
                    yield {"batch_id": batch.id, "period": period, "shop_name": shop, "amount": amount}
        counts["logistics"] = _bulk(db, LegacyLogisticsRow, logistics())

        def prepaid():
            for r in reader.iter_dicts("垫付"):
                period = _month(_get(r, "合同月份"))
                amount = _decimal(_get(r, "核销金额"))
                if period and amount:
                    yield {
                        "batch_id": batch.id,
                        "period": period,
                        "channel": _text(_get(r, "渠道")),
                        "category": _text(_get(r, "其他")),
                        "amount": amount,
                        "note": _text(_get(r, "备注"))[:500],
                    }
        counts["prepaid"] = _bulk(db, LegacyPrepaidRow, prepaid())

        def inventory():
            for r in reader.iter_dicts("库存"):
                period = _month(_get(r, "日期"))
                amount = _decimal(_get(r, "库存金额"))
                if period and amount:
                    yield {
                        "batch_id": batch.id,
                        "period": period,
                        "amount": amount,
                        "warehouse": _text(_get(r, "仓库")),
                        "barcode": _text(_get(r, "商家编码")),
                        "goods_name": _text(_get(r, "货品名称"))[:255],
                    }
        counts["inventory"] = _bulk(db, LegacyInventoryRow, inventory())

        def purchases():
            for r in reader.iter_dicts("采购明细"):
                recon_month = _month(_get(r, "核销月份"))
                purchase_month = _month(_get(r, "采购月份"))
                channel = _text(_get(r, "渠道"))
                untaxed = _decimal(_get(r, "到货-折后开票金额(未税)"))
                taxed = _decimal(_get(r, "到货-折后开票金额(含税)"))
                if not (recon_month or purchase_month or untaxed or taxed):
                    continue
                yield {
                    "batch_id": batch.id,
                    "recon_month": recon_month,
                    "purchase_month": purchase_month,
                    "channel": channel,
                    "product_code": _text(_get(r, "产品代码")),
                    "barcode": _text(_get(r, "条码")),
                    "goods_name": _text(_get(r, "产品名称", "产品名称"))[:255],
                    "discount_rate": _decimal(_get(r, "折扣")) or Decimal("0.8"),
                    "qty_units": _decimal(_get(r, "到货-数量(件)")),
                    "list_amount_untaxed": _decimal(_get(r, "到货-标价总额(未税)")),
                    "invoice_amount_untaxed": untaxed,
                    "invoice_amount_tax": taxed,
                    "subbrand": _text(_get(r, "子品牌")),
                }
        counts["purchases"] = _bulk(db, LegacyPurchaseRow, purchases())

        def rebates():
            for rn, row in reader.iter_rows("返利核销情况"):
                if rn < 26:
                    continue
                # 快手 A:P
                period = _month(row.get("B"))
                category = _text(row.get("E"))
                if period and (category or _decimal(row.get("H")) or _decimal(row.get("L"))):
                    yield {
                        "batch_id": batch.id, "channel": _text(row.get("A")) or "快手", "period": period,
                        "basis_amount_untaxed": _decimal(row.get("C")), "category": category,
                        "subcategory": _text(row.get("F")), "rate": _decimal(row.get("G")),
                        "expected_amount_tax": _decimal(row.get("H")), "invoice_category": _text(row.get("K")),
                        "invoice_amount": _decimal(row.get("L")), "invoice_status": _text(row.get("M")),
                        "arrival_marker": _text(row.get("N")), "actual_total": _decimal(row.get("O")),
                        "difference": _decimal(row.get("P")),
                    }
                # 抖音 Q:AF
                period = _month(row.get("R"))
                category = _text(row.get("U"))
                if period and (category or _decimal(row.get("X")) or _decimal(row.get("AB"))):
                    yield {
                        "batch_id": batch.id, "channel": _text(row.get("Q")) or "抖音", "period": period,
                        "basis_amount_untaxed": _decimal(row.get("S")), "category": category,
                        "subcategory": _text(row.get("V")), "rate": _decimal(row.get("W")),
                        "expected_amount_tax": _decimal(row.get("X")), "invoice_category": _text(row.get("AA")),
                        "invoice_amount": _decimal(row.get("AB")), "invoice_status": _text(row.get("AC")),
                        "arrival_marker": _text(row.get("AD")), "actual_total": _decimal(row.get("AE")),
                        "difference": _decimal(row.get("AF")),
                    }
        counts["rebates"] = _bulk(db, LegacyRebateRow, rebates())

        counts["total"] = sum(counts.values())
        batch.row_count = counts["total"]
        batch.status = "imported"
        db.commit()
        return batch, counts, False
    except Exception:
        db.rollback()
        batch.status = "failed"
        db.add(batch)
        db.commit()
        raise
    finally:
        reader.close()
