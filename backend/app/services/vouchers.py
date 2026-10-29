"""客户打款凭证：存图 → OCR → 解析金额/日期/付款方 → 确认入账 → 应收账款。

用户 2026-10-28 要求
--------------------
「客户打款，我需要有个凭证粘到这里，客户付款了，你自动根据图片来识别，
  放在应收账款里面，每个订单都可以放」

三条硬规矩
----------
1. **识别只是辅助，不自动改钱**。OCR 出金额后要人看一眼点「确认收款」，
   才写订单的 paid_amount。财务数字宁可多点一下，也不能被识别错带歪。
2. **一张凭证只入账一次**。`applied` 标记 + 入账前校验，防止同一张截图点两次翻倍。
3. **原图必须留着**。财务追溯要看原始凭证，图片存磁盘不进 SQLite。

OCR 引擎
--------
本地离线部署（8050），所以用 rapidocr-onnxruntime：pip 装完就能跑，不联网、不要 key。
没装时降级为 `ocr_status="unavailable"`，凭证照样能存、金额手填，功能不残。
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import FxDistributor, FxOrder, FxPaymentVoucher

# backend/app/services/vouchers.py -> parents[3] = 项目根
_ROOT = Path(__file__).resolve().parents[3]
VOUCHER_ROOT = Path(os.environ.get("VOUCHER_DIR") or (_ROOT / "data" / "local" / "vouchers"))

ALLOWED_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/gif": ".gif",
}
MAX_BYTES = 12 * 1024 * 1024  # 12MB，手机截图一般 200KB~3MB

PAY_METHOD_KEYWORDS = [
    ("微信", "微信"),
    ("支付宝", "支付宝"),
    ("银联", "银联"),
    ("中国银行", "中国银行"),
    ("工商银行", "工商银行"),
    ("建设银行", "建设银行"),
    ("农业银行", "农业银行"),
    ("招商银行", "招商银行"),
    ("交通银行", "交通银行"),
    ("浦发银行", "浦发银行"),
    ("民生银行", "民生银行"),
    ("兴业银行", "兴业银行"),
    ("中信银行", "中信银行"),
    ("平安银行", "平安银行"),
    ("邮政储蓄", "邮储银行"),
    ("农商银行", "农商银行"),
]

_AMOUNT_LABELS = ("转账金额", "交易金额", "付款金额", "实付金额", "收款金额", "打款金额", "金额合计", "合计", "金额")
_PAYER_LABELS = ("付款方", "付款人", "付款账户", "付款单位", "汇款人", "转出方", "来自")
_PAYEE_LABELS = ("收款方", "收款人", "收款账户", "收款单位", "转入方", "转账给", "收款")
_TIME_LABELS = ("转账时间", "交易时间", "付款时间", "创建时间", "到账时间", "交易日期", "日期", "时间")
_NOTE_LABELS = ("备注", "附言", "用途", "摘要", "转账说明", "留言")

_NUM = r"[-+]?[0-9][0-9,，]*(?:\.[0-9]{1,2})?"
_DATE_RE = re.compile(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})(?::(\d{2}))?")


# ---------------------------------------------------------------- 存储

def _rel_dir(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m")


def save_file(content: bytes, filename: str, mime: str) -> tuple[str, str, int]:
    """把图片写到磁盘，返回 (相对路径, 实际 mime, 字节数)。"""
    if not content:
        raise ValueError("文件是空的")
    if len(content) > MAX_BYTES:
        raise ValueError(f"图片太大（{len(content) // 1024 // 1024}MB），请压到 12MB 以内")
    mime = (mime or "").split(";")[0].strip().lower()
    ext = ALLOWED_MIME.get(mime)
    if not ext:
        # 浏览器有时给 application/octet-stream，退回看扩展名
        guess = Path(filename or "").suffix.lower()
        if guess in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
            ext = ".jpg" if guess == ".jpeg" else guess
            mime = mime or "image/jpeg"
        else:
            raise ValueError("只支持图片凭证（png / jpg / webp / bmp / gif）")
    rel_dir = _rel_dir()
    target_dir = VOUCHER_ROOT / rel_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}{ext}"
    (target_dir / name).write_bytes(content)
    return f"{rel_dir}/{name}", mime, len(content)


def file_path(stored_path: str) -> Path:
    """相对路径 → 绝对路径。禁止路径穿越。"""
    if not stored_path or ".." in stored_path:
        raise ValueError("凭证路径非法")
    target = (VOUCHER_ROOT / stored_path).resolve()
    root = VOUCHER_ROOT.resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise ValueError("凭证文件不存在")
    return target


# ---------------------------------------------------------------- OCR

_ENGINE_CACHE: dict[str, object] = {}


def _engine():
    """懒加载 RapidOCR。第一次调用会加载模型（几百毫秒），之后复用。"""
    if "engine" in _ENGINE_CACHE:
        return _ENGINE_CACHE["engine"]
    eng = None
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore

        eng = RapidOCR()
    except Exception:
        try:
            from rapidocr import RapidOCR  # type: ignore  # 新版包名

            eng = RapidOCR()
        except Exception:
            eng = None
    _ENGINE_CACHE["engine"] = eng
    return eng


def ocr_available() -> bool:
    return _engine() is not None


def run_ocr(path: Path) -> tuple[str, str]:
    """返回 (识别文本, 引擎名)。引擎不可用时返回 ("", "unavailable")。"""
    eng = _engine()
    if eng is None:
        return "", "unavailable"
    try:
        result = eng(str(path))
    except Exception as exc:  # 引擎内部异常不能把上传搞崩
        return f"（OCR 报错：{exc}）", "failed"
    # RapidOCR 两种返回：老版 (list[[box, text, score]], elapse) / 新版 Result 对象
    lines: list[str] = []
    raw = result
    if isinstance(raw, tuple):
        raw = raw[0]
    items = getattr(raw, "txts", None)
    if items is None and isinstance(raw, list):
        for row in raw:
            if isinstance(row, (list, tuple)) and len(row) >= 2 and isinstance(row[1], str):
                lines.append(row[1])
            elif isinstance(row, str):
                lines.append(row)
    elif items is not None:
        lines = [str(x) for x in items]
    text = "\n".join(x.strip() for x in lines if x and x.strip())
    return text, "rapidocr"


# ---------------------------------------------------------------- 解析

def _to_decimal(raw: str) -> Decimal | None:
    try:
        value = Decimal(raw.replace(",", "").replace("，", "").replace(" ", ""))
    except (InvalidOperation, AttributeError):
        return None
    if value <= 0 or value > Decimal("100000000"):
        return None
    return value.quantize(Decimal("0.01"))


def _line_after(lines: list[str], idx: int, labels: tuple[str, ...]) -> str:
    """标签行里冒号后面有内容就用它，否则看下一行（很多截图是「标签\n值」两行）。"""
    line = lines[idx]
    for label in labels:
        if label in line:
            rest = line.split(label, 1)[1].lstrip("：: 　")
            if rest and not re.fullmatch(r"[\s:：]*", rest):
                return rest.strip()
    if idx + 1 < len(lines):
        nxt = lines[idx + 1].strip()
        if nxt and not any(lbl in nxt for lbl in labels):
            return nxt
    return ""


def _fallback_amount(lines: list[str]) -> Decimal | None:
    """没有「金额」标签时的兜底：先挑带货币符号/元/人民币的，再挑带小数的。

    必须先剔掉日期和长串单号，否则「2026年10月26日」里的 2026 会被当成金额
    （真实踩过：支付宝截图把 2026 识别成了金额）。
    """
    best: tuple[int, Decimal] | None = None
    for line in lines:
        cleaned = _DATE_RE.sub(" ", line)
        cleaned = _TIME_RE.sub(" ", cleaned)
        cleaned = re.sub(r"\d{7,}", " ", cleaned)   # 转账单号 / 账号
        has_currency = bool(re.search(r"[¥￥]|元|人民币", cleaned))
        for m in re.finditer(_NUM, cleaned):
            token = m.group(0)
            val = _to_decimal(token)
            if not val:
                continue
            if not has_currency and "." not in token:
                continue  # 没有货币标记又没小数 → 多半是数量/期数，不当金额
            score = 2 if has_currency else 1
            if best is None or (score, val) > best:
                best = (score, val)
    return best[1] if best else None


def parse_payment_text(text: str) -> dict:
    """从 OCR 文本里抠出金额 / 时间 / 付款方 / 收款方 / 方式 / 备注。

    策略是「先找标签，再兜底找最像的」：
    - 金额：标签（转账金额/交易金额/金额…）后的数字优先，否则取全图最大的 ¥ 数字
      （手续费、余额这些一般比本金小，取最大比取第一个稳）；
    - 时间：标签附近优先，否则取第一个 20xx-xx-xx；
    - 付款方：标签后一段文字，否则空着让人填 —— 宁可留空也不要瞎猜一个名字。
    """
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    out = {"amount": None, "paid_at": "", "payer": "", "payee": "", "pay_method": "", "note": ""}
    if not lines:
        return out

    # 金额
    amount = None
    for i, line in enumerate(lines):
        if any(lbl in line for lbl in _AMOUNT_LABELS):
            candidates = re.findall(_NUM, _line_after(lines, i, _AMOUNT_LABELS)) or re.findall(_NUM, line)
            for cand in candidates:
                val = _to_decimal(cand)
                if val:
                    amount = val
                    break
            if amount:
                break
    if amount is None:
        amount = _fallback_amount(lines)
    out["amount"] = amount

    # 时间
    for i, line in enumerate(lines):
        if any(lbl in line for lbl in _TIME_LABELS):
            window = " ".join(lines[i:i + 2])
            m = _DATE_RE.search(window)
            if m:
                y, mo, d = (int(x) for x in m.groups())
                t = _TIME_RE.search(window[m.end():])
                stamp = f"{y:04d}-{mo:02d}-{d:02d}"
                if t:
                    hh, mm = int(t.group(1)), int(t.group(2))
                    if hh < 24 and mm < 60:
                        stamp += f" {hh:02d}:{mm:02d}"
                out["paid_at"] = stamp
                break
    if not out["paid_at"]:
        m = _DATE_RE.search(text or "")
        if m:
            y, mo, d = (int(x) for x in m.groups())
            out["paid_at"] = f"{y:04d}-{mo:02d}-{d:02d}"

    # 付款方 / 收款方
    for i, line in enumerate(lines):
        if not out["payer"] and any(lbl in line for lbl in _PAYER_LABELS):
            val = _line_after(lines, i, _PAYER_LABELS)
            if val and not _DATE_RE.search(val):
                out["payer"] = val[:80]
        if not out["payee"] and any(lbl in line for lbl in _PAYEE_LABELS):
            val = _line_after(lines, i, _PAYEE_LABELS)
            if val and not _DATE_RE.search(val):
                out["payee"] = val[:80]

    # 方式
    for kw, label in PAY_METHOD_KEYWORDS:
        if kw in (text or ""):
            out["pay_method"] = label if label.endswith("银行") else f"{label}支付"
            break
    if not out["pay_method"]:
        if "转账" in (text or "") or "汇款" in (text or ""):
            out["pay_method"] = "银行转账"

    # 备注
    for i, line in enumerate(lines):
        if any(lbl in line for lbl in _NOTE_LABELS):
            val = _line_after(lines, i, _NOTE_LABELS)
            if val:
                out["note"] = val[:120]
                break

    return out


# ---------------------------------------------------------------- CRUD

def _view(row: FxPaymentVoucher, order_no: str = "") -> dict:
    return {
        "id": row.id,
        "order_id": row.order_id,
        "order_no": order_no,
        "distributor_id": row.distributor_id,
        "filename": row.filename,
        "url": f"/api/fx/vouchers/{row.id}/file",
        "mime": row.mime,
        "size": row.size,
        "ocr_status": row.ocr_status,
        "ocr_engine": row.ocr_engine,
        "ocr_text": row.ocr_text or "",
        "amount": None if row.amount is None else f"{Decimal(row.amount):.2f}",
        "paid_at": row.paid_at or "",
        "payer": row.payer or "",
        "pay_method": row.pay_method or "",
        "note": row.note or "",
        "applied": int(row.applied or 0),
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
    }


def add_voucher(
    db: Session,
    *,
    content: bytes,
    filename: str,
    mime: str,
    order_id: int | None = None,
    distributor_id: int | None = None,
    run_ocr_now: bool = True,
) -> dict:
    """保存凭证并识别。order_id 为空时先挂分销商（钱到了但还没认领订单）。"""
    order = db.get(FxOrder, int(order_id)) if order_id else None
    if order_id and not order:
        raise ValueError("订单不存在")
    dist_id = int(distributor_id or (order.distributor_id if order else 0) or 0)
    if order and dist_id != order.distributor_id:
        raise ValueError("收款凭证的客户与所选订单客户不一致")
    if not dist_id:
        raise ValueError("要么给订单，要么给分销商")
    if not db.get(FxDistributor, dist_id):
        raise ValueError("分销商不存在")

    stored_path, real_mime, size = save_file(content, filename, mime)
    digest = hashlib.sha256(content).hexdigest()

    row = FxPaymentVoucher(
        order_id=order.id if order else None,
        distributor_id=dist_id,
        filename=(filename or "")[:255],
        stored_path=stored_path,
        mime=real_mime,
        size=size,
        sha256=digest,
        ocr_status="pending",
    )
    db.add(row)
    db.flush()

    if run_ocr_now:
        _ocr_into(row)

    db.commit()
    db.refresh(row)
    return _view(row, order.order_no if order else "")


def _ocr_into(row: FxPaymentVoucher) -> None:
    """跑 OCR 并把识别结果写进 row（不 commit，调用方负责）。"""
    try:
        path = file_path(row.stored_path)
    except ValueError:
        row.ocr_status = "failed"
        row.ocr_engine = "missing-file"
        return
    text, engine = run_ocr(path)
    row.ocr_engine = engine
    row.ocr_text = text or ""
    if engine == "unavailable":
        row.ocr_status = "unavailable"
        return
    if engine == "failed":
        row.ocr_status = "failed"
        return
    row.ocr_status = "done"
    parsed = parse_payment_text(text)
    if parsed["amount"] is not None:
        row.amount = parsed["amount"]
    row.paid_at = parsed["paid_at"] or ""
    row.payer = parsed["payer"] or ""
    row.pay_method = parsed["pay_method"] or ""
    row.note = parsed["note"] or ""


def reparse(db: Session, voucher_id: int) -> dict:
    """重跑 OCR（引擎刚装好、或图片换了的情况）。已入账的不许改金额。"""
    row = db.get(FxPaymentVoucher, int(voucher_id))
    if not row:
        raise ValueError("凭证不存在")
    if row.applied:
        raise ValueError("这张凭证已经入账，不能再重新识别（先撤销入账）")
    _ocr_into(row)
    db.commit()
    db.refresh(row)
    order = db.get(FxOrder, row.order_id) if row.order_id else None
    return _view(row, order.order_no if order else "")


def list_vouchers(db: Session, order_id: int) -> list[dict]:
    order = db.get(FxOrder, int(order_id))
    rows = db.scalars(
        select(FxPaymentVoucher)
        .where(FxPaymentVoucher.order_id == int(order_id))
        .order_by(FxPaymentVoucher.id.desc())
    ).all()
    return [_view(r, order.order_no if order else "") for r in rows]


def update_voucher(db: Session, voucher_id: int, data: dict) -> dict:
    """人工修正识别结果（金额/日期/付款方/备注）或改挂订单。"""
    row = db.get(FxPaymentVoucher, int(voucher_id))
    if not row:
        raise ValueError("凭证不存在")
    # 已确认入账的金额/归属是财务流水，必须先撤销入账再修改。
    # 否则后续撤销会扣错金额，或把 A 单的钱直接挂到 B 单。
    if row.applied and ("amount" in data or "order_id" in data):
        raise ValueError("凭证已经入账，请先撤销入账，再修改金额或关联订单")
    if "amount" in data:
        raw = data["amount"]
        if raw in (None, ""):
            row.amount = None
        else:
            val = _to_decimal(str(raw))
            if val is None:
                raise ValueError(f"金额格式不对: {raw}")
            row.amount = val
    for field, limit in (("paid_at", 20), ("payer", 160), ("pay_method", 32), ("note", 255)):
        if field in data and data[field] is not None:
            setattr(row, field, str(data[field]).strip()[:limit])
    if "order_id" in data and data["order_id"] is not None:
        new_id = int(data["order_id"] or 0)
        if new_id:
            order = db.get(FxOrder, new_id)
            if not order:
                raise ValueError("订单不存在")
            row.order_id = order.id
            row.distributor_id = order.distributor_id
        else:
            row.order_id = None
    db.commit()
    db.refresh(row)
    order = db.get(FxOrder, row.order_id) if row.order_id else None
    return _view(row, order.order_no if order else "")


def delete_voucher(db: Session, voucher_id: int) -> dict:
    row = db.get(FxPaymentVoucher, int(voucher_id))
    if not row:
        raise ValueError("凭证不存在")
    if row.applied:
        raise ValueError("这张凭证已经入账，先撤销入账再删")
    stored = row.stored_path
    db.delete(row)
    db.commit()
    try:
        path = file_path(stored)
        path.unlink(missing_ok=True)
    except ValueError:
        pass
    return {"ok": True, "id": int(voucher_id)}


def _payment_status(total: Decimal, paid: Decimal) -> str:
    if paid <= 0:
        return "unpaid"
    if total > 0 and paid + Decimal("0.005") >= total:
        return "received"
    if total <= 0:
        return "received"
    return "partial"


def apply_voucher(db: Session, voucher_id: int, *, amount: str | None = None,
                  mode: str = "add") -> dict:
    """把凭证金额计入订单收款。

    mode="add"：累加到已收（一笔订单分几次打款的常规情况）；
    mode="set"：仅允许订单尚无任何已收款时使用，不覆盖已有入账凭证。
    """
    row = db.get(FxPaymentVoucher, int(voucher_id))
    if not row:
        raise ValueError("凭证不存在")
    if not row.order_id:
        raise ValueError("这张凭证还没挂到订单上，先选一张订单")
    if row.applied:
        raise ValueError("这张凭证已经入账过了，不能重复入账")
    order = db.get(FxOrder, row.order_id)
    if not order:
        raise ValueError("订单不存在")

    raw = amount if amount not in (None, "") else (None if row.amount is None else str(row.amount))
    if raw in (None, ""):
        raise ValueError("这张凭证还没识别出金额，请先手填金额")
    value = _to_decimal(str(raw))
    if value is None:
        raise ValueError(f"金额格式不对: {raw}")

    if mode not in {"add", "set"}:
        raise ValueError(f"未知入账模式: {mode}")
    current_paid = Decimal(order.paid_amount or 0).quantize(Decimal("0.01"))
    if mode == "set":
        # 覆盖已有已收金额会让此前已入账的凭证与订单余额不一致；
        # 只有全新、尚未收款的订单允许等价的 set/add。
        any_applied = db.scalar(
            select(FxPaymentVoucher.id)
            .where(FxPaymentVoucher.order_id == order.id, FxPaymentVoucher.applied == 1)
            .limit(1)
        )
        if current_paid != 0 or any_applied:
            raise ValueError("订单已有收款记录，不能覆盖已收金额；请用累加入账或先撤销对应凭证")
    paid = (current_paid + value).quantize(Decimal("0.01"))
    total = Decimal(order.total_amount or 0).quantize(Decimal("0.01"))
    if paid > total:
        raise ValueError(f"入账后金额 {paid:.2f} 超过订单金额 {total:.2f}，请核对凭证及订单")
    order.paid_amount = paid
    order.payment_status = _payment_status(Decimal(order.total_amount or 0), paid)
    if row.paid_at:
        try:
            order.payment_date = datetime.strptime(row.paid_at[:16], "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                order.payment_date = datetime.strptime(row.paid_at[:10], "%Y-%m-%d")
            except ValueError:
                order.payment_date = order.payment_date or datetime.utcnow()
    else:
        order.payment_date = order.payment_date or datetime.utcnow()
    if row.payer and not (order.pay_company or "").strip():
        order.pay_company = row.payer[:160]
    row.applied = 1
    row.amount = value
    order.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    db.refresh(order)
    return {
        "ok": True,
        "voucher": _view(row, order.order_no),
        "order": {
            "id": order.id,
            "order_no": order.order_no,
            "paid_amount": f"{Decimal(order.paid_amount or 0):.2f}",
            "total_amount": f"{Decimal(order.total_amount or 0):.2f}",
            "payment_status": order.payment_status,
        },
    }


def unapply_voucher(db: Session, voucher_id: int) -> dict:
    """撤销入账：把金额从订单已收里扣回去（凭证保留，可改可删）。"""
    row = db.get(FxPaymentVoucher, int(voucher_id))
    if not row:
        raise ValueError("凭证不存在")
    if not row.applied:
        raise ValueError("这张凭证还没入账")
    order = db.get(FxOrder, row.order_id) if row.order_id else None
    if order:
        paid = (Decimal(order.paid_amount or 0) - Decimal(row.amount or 0)).quantize(Decimal("0.01"))
        if paid < 0:
            raise ValueError("订单已收金额小于这张凭证的入账金额，账面数据不一致；已停止撤销，请先核对")
        order.paid_amount = paid
        order.payment_status = _payment_status(Decimal(order.total_amount or 0), paid)
        order.updated_at = datetime.utcnow()
    row.applied = 0
    db.commit()
    return {"ok": True, "id": row.id}


# ---------------------------------------------------------------- 应收账款

def receivables(
    db: Session,
    *,
    distributor_id: int | None = None,
    status: str = "",
    keyword: str = "",
    limit: int = 500,
) -> dict:
    """应收账款：订单维度列出 应收 / 已收 / 未收 + 凭证。

    「应收」用订单总额（total_amount）；已收是 paid_amount（人工确认或凭证入账累积）。
    这样应收账款的数和订单页看到的完全一致，不另起一套口径。
    """
    stmt = select(FxOrder).order_by(FxOrder.order_date.desc(), FxOrder.id.desc())
    if distributor_id:
        stmt = stmt.where(FxOrder.distributor_id == int(distributor_id))
    if status:
        stmt = stmt.where(FxOrder.payment_status == status)
    if keyword:
        kw = f"%{keyword.strip()}%"
        stmt = stmt.where(FxOrder.order_no.like(kw) | FxOrder.original_order_no.like(kw))
    orders = db.scalars(stmt.limit(max(1, min(int(limit or 500), 2000)))).all()

    ids = [o.id for o in orders]
    vmap: dict[int, list[FxPaymentVoucher]] = {}
    if ids:
        rows = db.scalars(
            select(FxPaymentVoucher)
            .where(FxPaymentVoucher.order_id.in_(ids))
            .order_by(FxPaymentVoucher.id.desc())
        ).all()
        for r in rows:
            vmap.setdefault(r.order_id, []).append(r)

    dist_names = {
        d.id: d.name
        for d in db.scalars(select(FxDistributor).where(FxDistributor.id.in_({o.distributor_id for o in orders}))).all()
    } if orders else {}

    items, totals = [], {
        "order_count": 0, "receivable": Decimal("0"), "received": Decimal("0"), "outstanding": Decimal("0"),
        "with_voucher": 0, "without_voucher": 0,
    }
    for o in orders:
        total = Decimal(o.total_amount or 0)
        paid = Decimal(o.paid_amount or 0)
        open_amt = (total - paid).quantize(Decimal("0.01"))
        if open_amt < 0:
            open_amt = Decimal("0.00")
        vouchers = vmap.get(o.id, [])
        items.append({
            "id": o.id,
            "order_no": o.order_no,
            "order_date": o.order_date.strftime("%Y-%m-%d") if o.order_date else "",
            "distributor_id": o.distributor_id,
            "distributor_name": dist_names.get(o.distributor_id, ""),
            "total_amount": f"{total:.2f}",
            "paid_amount": f"{paid:.2f}",
            "open_amount": f"{open_amt:.2f}",
            "payment_status": o.payment_status or "unpaid",
            "payment_date": o.payment_date.strftime("%Y-%m-%d") if o.payment_date else "",
            "pay_company": o.pay_company or "",
            "voucher_count": len(vouchers),
            "vouchers": [_view(v, o.order_no) for v in vouchers[:6]],
            "has_voucher": bool(vouchers),
            "applied_amount": f"{sum(Decimal(v.amount or 0) for v in vouchers if v.applied):.2f}",
        })
        totals["order_count"] += 1
        totals["receivable"] += total
        totals["received"] += paid
        totals["outstanding"] += open_amt
        if vouchers:
            totals["with_voucher"] += 1
        else:
            totals["without_voucher"] += 1

    return {
        "items": items,
        "totals": {k: (f"{v:.2f}" if isinstance(v, Decimal) else v) for k, v in totals.items()},
        "ocr_available": ocr_available(),
    }


def unclaimed_vouchers(db: Session, limit: int = 100) -> list[dict]:
    """还没挂订单的凭证（钱到了但没认领）——应收账款页顶部提醒用。"""
    rows = db.scalars(
        select(FxPaymentVoucher)
        .where(FxPaymentVoucher.order_id.is_(None))
        .order_by(FxPaymentVoucher.id.desc())
        .limit(limit)
    ).all()
    dist = {d.id: d.name for d in db.scalars(select(FxDistributor)).all()}
    out = []
    for r in rows:
        v = _view(r)
        v["distributor_name"] = dist.get(r.distributor_id, "")
        out.append(v)
    return out


def voucher_stats(db: Session) -> dict:
    total = db.scalar(select(func.count(FxPaymentVoucher.id))) or 0
    unclaimed = db.scalar(
        select(func.count(FxPaymentVoucher.id)).where(FxPaymentVoucher.order_id.is_(None))
    ) or 0
    return {"total": int(total), "unclaimed": int(unclaimed), "ocr_available": ocr_available()}
