"""红盒子供销平台：分销业务服务。

第一版范围：
- 分销商档案（增删改查 / 启停）
- 商品与基础价格
- 分销订单（草稿 → 提交 → 审核，可退回 / 取消）
- 订单状态总览

设计原则与核销模块保持一致：
- 订单金额全部在服务端用 Decimal 计算，前端只负责展示。
- 创建订单时快照商品名称 / 编号，后续商品改名不影响历史订单。
- 状态流转集中在此处，避免各接口各自判断。
"""
from __future__ import annotations

import io
import re
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..models import (
    FxBrand,
    FxDistributor,
    FxDistributorPayLink,
    FxOrder,
    FxOrderLine,
    FxPayEntity,
    FxPrice,
    FxProduct,
    StockoutItem,
    StockoutOrder,
)
from .product_maintenance import log_price_change, maintenance_of
from .wangdian import WangdianClient

MONEY_Q = Decimal("0.01")
# 订单状态：与本系统现有风格一致，状态字符串直接存库，展示层负责翻译。
FX_ORDER_STATUSES = ("draft", "submitted", "approved", "cancelled")

FX_TRANSITIONS: dict[str, dict[str, str]] = {
    # 当前状态 -> 动作 -> 目标状态；不允许的动作直接报错
    "draft": {"submit": "submitted", "cancel": "cancelled"},
    "submitted": {"approve": "approved", "reject": "draft", "cancel": "cancelled"},
    "approved": {},
    "cancelled": {},
}

STATUS_LABELS = {
    "draft": "草稿",
    "submitted": "已提交待审核",
    "approved": "已审核",
    "cancelled": "已取消",
}

# 旺店通推送状态。"" = 真的还没推（新建单）；
# "sent" = 已经在旺店通（系统推的，或原表本来就带出库单号）；
# "history" = 历史导入单，原表没有旺店通单号——既不冒充已推送，也不再混进待推送。
PUSH_STATUS_LABELS = {
    "": "未推送",
    "sent": "已推送旺店通",
    "failed": "推送失败",
    "history": "历史单（原表无单号）",
}

# 订单除了审核状态，还必须能看清钱和货：收款、发货各自独立。
PAYMENT_STATUS_LABELS = {
    "": "未记录",
    "unpaid": "未收到",
    "partial": "部分收款",
    "received": "已收到",
    "refunded": "已退款",
}
SHIPPING_STATUS_LABELS = {
    "": "未记录",
    "unshipped": "未发货",
    "partial": "部分发货",
    "shipped": "已发货",
    "returned": "已退回",
}
PAYMENT_STATUSES = tuple(PAYMENT_STATUS_LABELS)
SHIPPING_STATUSES = tuple(SHIPPING_STATUS_LABELS)
# 配送方式：自提没有物流单号，送货必须有。下单页必选，避免发货环节来回问。
DELIVERY_TYPE_LABELS = {
    "": "未指定",
    "pickup": "自提",
    "delivery": "送货",
}
DELIVERY_TYPES = tuple(k for k in DELIVERY_TYPE_LABELS if k)
# 当前分销业务只从红盒子-格力高磐石仓发货（次品仓 HHZ-Glico-C 不参与）。
# 下单页会从旺店通仓库档案实时选，但推送必须有编号；这里作为老订单/接口漏传时的兜底。
DEFAULT_WDT_WAREHOUSE_NO = "HHZ-Glico"


def _dt_text(value: datetime | None, date_only: bool = False) -> str:
    if not value:
        return ""
    return value.strftime("%Y-%m-%d" if date_only else "%Y-%m-%d %H:%M")


def _parse_dt(value, field: str) -> datetime | None:
    """页面/源表传来的日期：支持空值、ISO、YYYY-MM-DD、YYYY/MM/DD。"""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip().replace("T", " ").replace("/", "-")
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(f"{field}格式不正确，应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM")


def _money(value: Decimal) -> Decimal:
    return (value or Decimal("0")).quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def _plain(value: Decimal) -> str:
    """Decimal 转普通文本：避免 normalize() 把 100 变成 1E+2。"""
    return format((value or Decimal("0")).normalize(), "f")


def _price_gap(base_price: Decimal, purchase_price: Decimal) -> str:
    """品牌原价 − 采购价（同为含税、同基本单位），任一侧为 0 时留空。

    正值说明牌价比实付高（牌价口径，折扣未反映）；接近 0 说明 base_price
    本身就是实付价；负值说明采购价已涨过牌价，需要复核。
    """
    base = _money(base_price)
    cost = _money(purchase_price)
    if base <= 0 or cost <= 0:
        return ""
    return str(_money(base - cost))


def _tier1_margin(
    cost_price: Decimal,
    tier1_price: Decimal,
    cost_source: str = "purchase",
    cost_status: str = "",
) -> dict:
    """一级分销价相对**采购入库价**（含税）的毛利。

    成本口径（2026-10-27 用户明确）：
    「一切都是以采购入库价格为主算利润」—— 成本必须是真实拿货价，
    不是品牌牌价。牌价只用于算分销折扣率，不参与利润计算。

    所以成本取值优先级：采购入库价 > 牌价（采购单没覆盖到时退回）。
    cost_source 回传是哪个价算的（"purchase" / "base"），页面据此提示
    「这个毛利是按采购价算的」还是「按牌价估的」，避免口径含糊。

    cost_status="pending"（成本待核对）时不出毛利 —— 拿错价算利润比不算更糟。
    典型场景：旺店通源单把两个 SKU 单价录反了，那批商品的成本根本不能用。

    两个价都是含税口径，直接相减，不做 ÷1.06 之类的税率换算
    （税率是报价时的约定，系统不存，避免历史数据被后来的税率改动带偏）。
    任一侧为 0 —— 成本没拿到、或一级分销价还没定 —— 就不给毛利，
    免得把「0 元」误读成「零毛利」。
    """
    cost = _money(cost_price)
    tier1 = _money(tier1_price)
    base_out = {
        "tier1_margin": "",
        "tier1_rate": "",
        "tier1_below_cost": False,
        "tier1_cost_source": "",
        "tier1_cost_pending": False,
    }
    if cost_status == "pending":
        return {**base_out, "tier1_cost_source": cost_source, "tier1_cost_pending": True}
    if cost <= 0 or tier1 <= 0:
        return base_out
    margin = _money(tier1 - cost)
    rate = (margin / tier1 * Decimal("100")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return {
        "tier1_margin": str(margin),
        "tier1_rate": str(rate),
        "tier1_below_cost": margin < 0,
        "tier1_cost_source": cost_source,
        "tier1_cost_pending": False,
    }


def _tier1_discount(tier1_price: Decimal, list_price: Decimal) -> str:
    """分销折扣率 = 一级分销价 ÷ 牌价，按「几折」表达（0.852 → "8.52"）。

    牌价只有格力高维护（用户明确：「其他品牌不维护，不需要啊，就是格力高想要」），
    没维护就留空。这个数只做报价参考，不参与利润计算 —— 利润按采购入库价算。
    """
    lp = _money(list_price)
    t1 = _money(tier1_price)
    if lp <= 0 or t1 <= 0:
        return ""
    return str((t1 / lp * Decimal("10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _parse_money(value, label: str) -> Decimal:
    """金额入参解析：空值当 0，非法/负数报错（label 用于提示哪个字段）。"""
    if value is None or value == "":
        return Decimal("0")
    try:
        amount = _money(Decimal(str(value)))
    except Exception as exc:
        raise ValueError(f"{label}格式不正确") from exc
    if amount < 0:
        raise ValueError(f"{label}不能为负数")
    return amount


def _pack_qty(value) -> int:
    """箱规数量归一：空/非法/负数一律当 0 = 未维护。"""
    if value is None or value == "":
        return 0
    try:
        qty = int(Decimal(str(value).strip()))
    except Exception:
        return 0
    return qty if qty > 0 else 0


# ---------------------------------------------------------------- 分销商
def brand_list_of(row: FxDistributor) -> list[str]:
    """把 brands 字段（逗号分隔）解析成品牌清单，去掉空值与重复，保持录入顺序。"""
    out: list[str] = []
    for b in (row.brands or "").split(","):
        b = b.strip()
        if b and b not in out:
            out.append(b)
    return out


def distributor_in_brand(row: FxDistributor, brand: str) -> bool:
    """该分销商在「当前品牌」下是否可见。

    brand 为空 = 全部品牌，一律可见；brand 非空时，只有勾了这个品牌的才出现。
    没勾任何品牌的分销商 = 还没录授权，选了具体品牌时看不到（不算全品牌通吃）。
    """
    if not brand:
        return True
    return brand in brand_list_of(row)


def list_distributors(db: Session, include_inactive: bool = True, brand: str = "") -> list[dict]:
    stmt = (
        select(FxDistributor)
        .options(selectinload(FxDistributor.pay_links))
        .order_by(FxDistributor.id.desc())
    )
    if not include_inactive:
        stmt = stmt.where(FxDistributor.active == 1)
    brand = (brand or "").strip()
    if brand:
        # 先在 SQL 里按逗号分隔项粗筛（brands 存成 "格力高,桂格"），
        # 再用 distributor_in_brand 精确兜底，避免 "格力高" 误命中 "格力高食品"
        stmt = stmt.where(or_(
            FxDistributor.brands == brand,
            FxDistributor.brands.like(brand + ",%"),
            FxDistributor.brands.like("%," + brand + ",%"),
            FxDistributor.brands.like("%," + brand),
        ))
    rows = db.scalars(stmt).all()
    if brand:
        rows = [r for r in rows if distributor_in_brand(r, brand)]
    return [_distributor_view(r) for r in rows]


def _distributor_view(row: FxDistributor) -> dict:
    accounts = _payment_accounts_view(row)
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "contact": row.contact,
        "phone": row.phone,
        "email": row.email,
        "tax_no": row.tax_no or "",
        "brands": brand_list_of(row),
        # pay_accounts 保留「账户名数组」旧口径，旧页面/旧导入继续可用；
        # payment_accounts 是完整明细，新编辑弹窗和品牌/对账页使用这一份。
        "pay_accounts": [a["account_name"] for a in accounts],
        "payment_accounts": accounts,
        "region": row.region,
        "note": row.note,
        "delivery_address": row.delivery_address or "",
        "active": bool(row.active),
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
    }


def _text_to_list(value: str) -> list[str]:
    """逗号/顿号/中文逗号分隔的串 -> 去重列表。"""
    import re as _re
    out: list[str] = []
    for item in _re.split(r"[,，、]", str(value or "")):
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


def _brands_to_text(value) -> str:
    """录入的品牌可能是数组（前端勾选）也可能是逗号串（批量导入），统一成去重后的逗号串。"""
    items = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    out: list[str] = []
    for b in items:
        b = str(b).strip()
        if b and b not in out:
            out.append(b)
    return ",".join(out)


def _clip_text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


# 抬头主档字段：保存时「非空新值」会写回主档（同名抬头一份资料，改一处全同步）
PAY_ENTITY_FIELDS = ("tax_no", "bank_name", "bank_branch", "bank_account", "account_holder", "account_type", "note")


def _pay_account_view(link: FxDistributorPayLink) -> dict:
    """关联行 → 页面用的账户明细（抬头主档资料 + 本客户自己的默认/收款备注）。"""
    ent = link.entity
    return {
        "id": link.id,
        "entity_id": ent.id if ent else None,
        "account_name": (ent.name if ent else "") or "",
        "tax_no": ent.tax_no or "" if ent else "",
        "bank_name": ent.bank_name or "" if ent else "",
        "bank_branch": ent.bank_branch or "" if ent else "",
        "bank_account": ent.bank_account or "" if ent else "",
        "account_holder": ent.account_holder or "" if ent else "",
        "account_type": (ent.account_type or "company") if ent else "company",
        "note": ent.note or "" if ent else "",
        "collection_note": link.collection_note or "",
        "is_default": bool(link.is_default),
    }


def _pay_account_snapshot(link_or_dict) -> dict:
    """把明细行转成可重新落库的普通 dict（合并时先抓快照，再删源客户）。"""
    data = dict(link_or_dict if isinstance(link_or_dict, dict) else _pay_account_view(link_or_dict))
    data.pop("id", None)
    data.pop("entity_id", None)
    return data


def _empty_pay_account(name: str, tax_no: str = "") -> dict:
    return {
        "id": None,
        "entity_id": None,
        "account_name": _clip_text(name, 160),
        "tax_no": _clip_text(tax_no, 80),
        "bank_name": "",
        "bank_branch": "",
        "bank_account": "",
        "account_holder": "",
        "account_type": "company",
        "collection_note": "",
        "is_default": False,
        "note": "",
    }


def _normalize_payment_accounts(items) -> list[dict]:
    """统一付款账户录入：支持明细数组、旧字符串数组、逗号串。

    - 每条必须有付款方名称；只有银行信息没有名称的行会被忽略；
    - 同一「付款方 + 银行账号」去重，但允许同一公司开多个银行账号；
    - 最多一个默认账户；没有指定时默认第一条，和弹窗的默认选中保持一致。
    """
    if isinstance(items, str):
        items = _text_to_list(items)
    elif isinstance(items, dict):
        items = [items]
    if not items:
        return []
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if isinstance(item, str):
            item = {"account_name": item}
        if not isinstance(item, dict):
            continue
        name = _clip_text(item.get("account_name") or item.get("name"), 160)
        if not name:
            continue
        bank_account = _clip_text(item.get("bank_account"), 80)
        key = (name, bank_account)
        if key in seen:
            continue
        seen.add(key)
        default_raw = item.get("is_default", False)
        is_default = default_raw in (True, 1, "1", "true", "True", "on")
        account_type = str(item.get("account_type") or "company").strip().lower()
        if account_type in ("personal", "private", "对私", "个人"):
            account_type = "personal"
        else:
            account_type = "company"
        out.append({
            "account_name": name,
            "tax_no": _clip_text(item.get("tax_no"), 80),
            "bank_name": _clip_text(item.get("bank_name"), 160),
            "bank_branch": _clip_text(item.get("bank_branch"), 160),
            "bank_account": bank_account,
            "account_holder": _clip_text(item.get("account_holder"), 160),
            "account_type": account_type,
            "collection_note": _clip_text(item.get("collection_note"), 500),
            "is_default": is_default,
            "note": _clip_text(item.get("note"), 500),
        })
    if out:
        defaults = [a for a in out if a["is_default"]]
        for account in defaults[1:]:
            account["is_default"] = False
        if not defaults:
            out[0]["is_default"] = True
    return out


def _payment_details_from_payload(data: dict) -> list[dict] | None:
    """从请求里取付款账户明细；返回 None 表示本次不修改账户。"""
    if "payment_accounts" in data:
        detailed = data.get("payment_accounts")
        if detailed:
            return _normalize_payment_accounts(detailed)
        # 新字段显式为空时，兼容旧前端仍传 pay_accounts 的情况
        if data.get("pay_accounts"):
            return _normalize_payment_accounts(data.get("pay_accounts"))
        return []
    if "pay_accounts" in data:
        return _normalize_payment_accounts(data.get("pay_accounts"))
    return None


def _payment_accounts_view(row: FxDistributor) -> list[dict]:
    accounts = [_pay_account_view(l) for l in (row.pay_links or [])]
    if accounts:
        return accounts
    # 兼容迁移前/旧导入只写逗号串的数据：先展示成「只有名称」的明细
    return [_empty_pay_account(name) for name in _text_to_list(row.pay_accounts or "")]


def _get_or_create_entity(db: Session, detail: dict) -> FxPayEntity:
    """按名称取/建抬头主档；已有抬头用非空新值补全（一份资料，改一处全同步）。"""
    name = _clip_text(detail.get("account_name"), 160)
    ent = db.scalar(select(FxPayEntity).where(FxPayEntity.name == name))
    if ent is None:
        ent = FxPayEntity(name=name)
        db.add(ent)
        db.flush()
    for f in PAY_ENTITY_FIELDS:
        val = _clip_text(detail.get(f), 500 if f in ("note",) else 160)
        if f == "tax_no":
            val = _clip_text(detail.get(f), 80)
        elif f == "bank_account":
            val = _clip_text(detail.get(f), 80)
        elif f == "account_type":
            val = detail.get(f) or "company"
        if val and val != "company":
            setattr(ent, f, val)
        elif f == "account_type":
            ent.account_type = ent.account_type or "company"
    db.flush()
    return ent


def _replace_payment_accounts(db: Session, row: FxDistributor, details: list[dict]) -> None:
    """整组替换客户的付款抬头关联：同名抬头共用主档，只重建关联行。"""
    row.pay_links.clear()
    db.flush()
    seen: set[str] = set()
    for i, detail in enumerate(details):
        name = _clip_text(detail.get("account_name"), 160)
        if not name or name in seen:
            continue
        seen.add(name)
        ent = _get_or_create_entity(db, detail)
        row.pay_links.append(FxDistributorPayLink(
            distributor_id=row.id,
            entity_id=ent.id,
            is_default=1 if detail.get("is_default") else 0,
            collection_note=_clip_text(detail.get("collection_note"), 500),
            sort_order=i,
        ))
    row.pay_accounts = ",".join(_clip_text(d.get("account_name"), 160) for d in details if d.get("account_name"))[:800]


def _next_distributor_code(db: Session) -> str:
    """编码留空时自动生成：从 H-01 起找第一个没人用的号（编号保持连续，不跳号）。"""
    used = set(db.scalars(select(FxDistributor.code)).all())
    n = 1
    while f"H-{n:02d}" in used:
        n += 1
    return f"H-{n:02d}"


def create_distributor(db: Session, data: dict) -> dict:
    code = (data.get("code") or "").strip()
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("分销商名称必填")
    if not code:
        code = _next_distributor_code(db)
    exists = db.scalar(select(FxDistributor).where(FxDistributor.code == code))
    if exists:
        raise ValueError(f"分销商编码已存在: {code}")
    accounts = _payment_details_from_payload(data) or []
    row = FxDistributor(
        code=code,
        name=name,
        contact=(data.get("contact") or "").strip(),
        phone=(data.get("phone") or "").strip(),
        email=(data.get("email") or "").strip(),
        delivery_address=(data.get("delivery_address") or "").strip(),
        tax_no=(data.get("tax_no") or "").strip(),
        brands=_brands_to_text(data.get("brands")),
        note=(data.get("note") or "").strip(),
        active=1,
    )
    _replace_payment_accounts(db, row, accounts)
    db.add(row)
    db.commit()
    return _distributor_view(row)


def update_distributor(db: Session, distributor_id: int, data: dict) -> dict:
    row = db.get(FxDistributor, distributor_id)
    if not row:
        raise ValueError("分销商不存在")
    new_code = (data.get("code") or row.code).strip()
    if new_code and new_code != row.code:
        dup = db.scalar(select(FxDistributor).where(FxDistributor.code == new_code, FxDistributor.id != row.id))
        if dup:
            raise ValueError(f"分销商编码已存在: {new_code}")
        row.code = new_code
    for field in ("name", "contact", "phone", "email", "tax_no", "note", "delivery_address"):
        if field in data and data[field] is not None:
            setattr(row, field, str(data[field]).strip())
    if "brands" in data and data["brands"] is not None:
        row.brands = _brands_to_text(data["brands"])
    details = _payment_details_from_payload(data)
    if details is not None:
        _replace_payment_accounts(db, row, details)
    if "name" in data and not (data.get("name") or "").strip():
        raise ValueError("分销商名称不能为空")
    db.commit()
    return _distributor_view(row)


def toggle_distributor(db: Session, distributor_id: int) -> dict:
    row = db.get(FxDistributor, distributor_id)
    if not row:
        raise ValueError("分销商不存在")
    row.active = 0 if row.active else 1
    db.commit()
    return _distributor_view(row)


def merge_distributors(db: Session, target_id: int, source_ids: list[int]) -> dict:
    """把几个分销商行并成一个客户（同一客户多个付款公司的场景）。

    - 订单、经销商专属价全部移到目标客户名下（专属价冲突时保留目标客户的价）；
    - 被并行的「名称」和「付款账户」都汇进目标客户的付款账户列表，对账时不丢线索；
    - 被并行删除（引用已全部转移，不留死数据）。
    """
    target = db.get(FxDistributor, target_id)
    if not target:
        raise ValueError("目标客户不存在")
    ids = [i for i in dict.fromkeys(int(s) for s in (source_ids or [])) if i != target_id]
    if not ids:
        raise ValueError("请勾选至少一个要并入的客户（不能和目标相同）")
    sources = [db.get(FxDistributor, i) for i in ids]
    if any(s is None for s in sources):
        raise ValueError("有要并入的客户不存在")

    moved_orders = moved_prices = dropped_prices = 0
    # 先按「账户名 + 银行账号」把明细合并去重；同公司多账号保留多条。
    accounts: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add_account(item: dict) -> None:
        name = _clip_text(item.get("account_name"), 160)
        if not name:
            return
        key = (name, _clip_text(item.get("bank_account"), 80))
        if key in seen:
            return
        seen.add(key)
        item = dict(item)
        item.pop("id", None)
        if not any(a.get("is_default") for a in accounts):
            item["is_default"] = True
        else:
            item["is_default"] = False
        accounts.append(item)

    for existing in _payment_accounts_view(target):
        add_account(existing)
    for src in sources:
        # 1) 订单全部改挂到目标
        r = db.execute(
            update(FxOrder).where(FxOrder.distributor_id == src.id).values(distributor_id=target.id)
        )
        moved_orders += r.rowcount or 0
        # 2) 经销商专属价：目标没有同商品的覆盖价就搬过去，有就丢弃（保留目标价）
        src_prices = db.scalars(select(FxPrice).where(FxPrice.distributor_id == src.id)).all()
        for sp in src_prices:
            dup = db.scalar(
                select(FxPrice).where(FxPrice.distributor_id == target.id, FxPrice.product_id == sp.product_id)
            )
            if dup:
                db.delete(sp)
                dropped_prices += 1
            else:
                sp.distributor_id = target.id
                moved_prices += 1
        # 3) 名称 + 付款账户明细并入目标；源客户名也留一条只含名称的账户，绝不丢对账线索。
        add_account(_empty_pay_account(src.name, src.tax_no))
        for acc in _payment_accounts_view(src):
            add_account(acc)
    _replace_payment_accounts(db, target, accounts)
    # 收货地址：源客户有、目标没有的收货点也并进来（同一客户多个收货点不能丢）
    addrs = [a for a in dict.fromkeys([target.delivery_address] + [s.delivery_address for s in sources]) if a]
    target.delivery_address = "；".join(addrs)
    merged_names = "、".join(s.name for s in sources)
    target.note = (f"{target.note or ''}｜合并入档：{merged_names}（{datetime.utcnow():%Y-%m-%d}）").strip("｜")
    # 4) 删除被并行（订单/专属价已转移；付款账户已先快照并入目标）
    for src in sources:
        db.delete(src)
    db.commit()
    return {
        "distributor": _distributor_view(target),
        "moved_orders": moved_orders,
        "moved_prices": moved_prices,
        "dropped_prices": dropped_prices,
        "merged_names": merged_names,
    }


# ---------------------------------------------------------------- 付款抬头（交叉维度）
def list_pay_entities(db: Session, keyword: str = "") -> list[dict]:
    """付款抬头主档 + 每个抬头被哪些分销商使用（多维度交叉视图）。

    同一抬头挂在多个分销商名下时（如关联公司代付），这里能一眼看出
    「这笔钱到底是谁付的、替谁付的」，列表按关联客户数倒序。
    """
    kw = (keyword or "").strip().lower()
    links = db.scalars(
        select(FxDistributorPayLink).options(selectinload(FxDistributorPayLink.entity))
    ).all()
    dists = {d.id: d for d in db.scalars(select(FxDistributor)).all()}
    grouped: dict[int, dict] = {}
    for link in links:
        ent = link.entity
        if ent is None:
            continue
        bucket = grouped.setdefault(ent.id, {
            "id": ent.id,
            "name": ent.name,
            "tax_no": ent.tax_no or "",
            "account_type": ent.account_type or "company",
            "bank_name": ent.bank_name or "",
            "bank_account": ent.bank_account or "",
            "distributors": [],
        })
        d = dists.get(link.distributor_id)
        if d is not None:
            bucket["distributors"].append({
                "id": d.id,
                "code": d.code,
                "name": d.name,
                "active": bool(d.active),
                "is_default": bool(link.is_default),
                "collection_note": link.collection_note or "",
            })
    out = list(grouped.values())
    for e in out:
        e["distributor_count"] = len(e["distributors"])
        # 客户侧顺序：编码倒序排（和列表页一致），默认抬头排前面
        e["distributors"].sort(key=lambda x: (not x["is_default"], x["code"] or ""), reverse=False)
        e["distributors"].sort(key=lambda x: x["code"] or "")
    if kw:
        out = [e for e in out if kw in e["name"].lower()
               or any(kw in d["name"].lower() or kw in (d["code"] or "").lower() for d in e["distributors"])]
    out.sort(key=lambda e: (-e["distributor_count"], e["name"]))
    return out


# ---------------------------------------------------------------- 商品
def list_products(
    db: Session, include_inactive: bool = True, brand: str = "", hot_only: bool = False
) -> list[dict]:
    # 默认排序：热销商品优先，其余仍按 id 倒序（新商品在前）。
    # hot_only 给下单页/外部调用使用，商品档案页拿全量后由前端筛选。
    stmt = select(FxProduct).order_by(FxProduct.is_hot.desc(), FxProduct.id.desc())
    if not include_inactive:
        stmt = stmt.where(FxProduct.active == 1)
    if brand:
        stmt = stmt.where(FxProduct.brand == brand)
    if hot_only:
        stmt = stmt.where(FxProduct.is_hot == 1)
    return [_product_view(r) for r in db.scalars(stmt).all()]


def _short_brand_name(name: str) -> str:
    text = (name or "").strip()
    return text.split("/", 1)[0].strip() or text


def _brand_counts(db: Session) -> tuple[dict[str, int], dict[str, int]]:
    """返回 (在售商品数, 授权分销商数)，按品牌全名索引。"""
    product_rows = db.execute(
        select(FxProduct.brand, func.count(FxProduct.id).label("cnt"))
        .where(FxProduct.active == 1)
        .where(FxProduct.brand.is_not(None))
        .where(FxProduct.brand != "")
        .group_by(FxProduct.brand)
    ).all()
    product_counts = {name: int(cnt or 0) for name, cnt in product_rows}
    distributor_counts: dict[str, int] = {}
    for row in db.scalars(select(FxDistributor)).all():
        for name in brand_list_of(row):
            distributor_counts[name] = distributor_counts.get(name, 0) + 1
    return product_counts, distributor_counts


def _brand_view(row: FxBrand, product_counts: dict[str, int], distributor_counts: dict[str, int]) -> dict:
    name = row.name or ""
    return {
        "id": row.id,
        "brand": name,
        "name": name,
        "short_name": (row.short_name or "").strip() or _short_brand_name(name),
        "code": row.code or "",
        "logo": row.logo or "",
        "sort_order": int(row.sort_order or 0),
        "active": bool(row.active),
        "managed": True,
        "count": product_counts.get(name, 0),
        "product_count": product_counts.get(name, 0),
        "distributor_count": distributor_counts.get(name, 0),
        "note": row.note or "",
    }


def list_brands(db: Session, include_inactive: bool = False) -> list[dict]:
    """品牌主数据 + 商品聚合，供顶栏切换、分销商授权和品牌管理共用。

    已建档品牌按 sort_order / 商品数排序；商品里出现但还没建档的品牌以
    managed=False 的虚拟行返回，页面可一键「纳入管理」，不会因为漏建档就消失。
    """
    product_counts, distributor_counts = _brand_counts(db)
    masters = db.scalars(select(FxBrand).order_by(FxBrand.sort_order, FxBrand.name)).all()
    seen = {row.name for row in masters}
    out: list[dict] = []
    for row in masters:
        if not include_inactive and not row.active:
            continue
        out.append(_brand_view(row, product_counts, distributor_counts))

    for name, count in product_counts.items():
        if name in seen:
            continue
        short = _short_brand_name(name)
        out.append({
            "id": None,
            "brand": name,
            "name": name,
            "short_name": short,
            "code": "",
            "logo": "",
            "sort_order": 9999,
            "active": True,
            "managed": False,
            "count": count,
            "product_count": count,
            "distributor_count": distributor_counts.get(name, 0),
            "note": "",
        })
    for name in distributor_counts:
        if name in seen or any(item["name"] == name for item in out):
            continue
        out.append({
            "id": None,
            "brand": name,
            "name": name,
            "short_name": _short_brand_name(name),
            "code": "",
            "logo": "",
            "sort_order": 9998,
            "active": True,
            "managed": False,
            "count": 0,
            "product_count": 0,
            "distributor_count": distributor_counts[name],
            "note": "",
        })

    out.sort(key=lambda item: (
        0 if item["active"] else 1,
        int(item["sort_order"] or 0),
        -int(item["count"] or 0),
        item["name"],
    ))
    return out


def _brand_duplicate(db: Session, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(FxBrand).where(FxBrand.name == name)
    if exclude_id is not None:
        stmt = stmt.where(FxBrand.id != exclude_id)
    return db.scalar(stmt) is not None


def _brand_payload_fields(data: dict) -> dict:
    name = _clip_text(data.get("name") or data.get("brand"), 100)
    if not name:
        raise ValueError("品牌名称必填")
    short = _clip_text(data.get("short_name"), 60) or _short_brand_name(name)
    try:
        sort_order = int(data.get("sort_order") or 0)
    except (TypeError, ValueError):
        sort_order = 0
    return {
        "name": name,
        "short_name": short,
        "code": _clip_text(data.get("code"), 60),
        "logo": _clip_text(data.get("logo"), 255),
        "sort_order": sort_order,
        "active": 1 if data.get("active", True) in (True, 1, "1", "true", "True", "on") else 0,
        "note": _clip_text(data.get("note"), 500),
    }


def create_brand(db: Session, data: dict) -> dict:
    fields = _brand_payload_fields(data)
    if _brand_duplicate(db, fields["name"]):
        raise ValueError(f"品牌已存在: {fields['name']}")
    row = FxBrand(**fields)
    db.add(row)
    db.commit()
    product_counts, distributor_counts = _brand_counts(db)
    return _brand_view(row, product_counts, distributor_counts)


def update_brand(db: Session, brand_id: int, data: dict) -> dict:
    row = db.get(FxBrand, brand_id)
    if not row:
        raise ValueError("品牌不存在")
    fields = _brand_payload_fields(data)
    old_name = row.name
    new_name = fields["name"]
    if new_name != old_name and _brand_duplicate(db, new_name, exclude_id=row.id):
        raise ValueError(f"品牌已存在: {new_name}")

    # 改名必须级联：商品档案和分销商授权都按品牌全名存储，不能留下两个旧名字。
    if new_name != old_name:
        db.execute(update(FxProduct).where(FxProduct.brand == old_name).values(brand=new_name))
        for dist in db.scalars(select(FxDistributor)).all():
            names = brand_list_of(dist)
            if old_name in names:
                replaced: list[str] = []
                for name in names:
                    value = new_name if name == old_name else name
                    if value not in replaced:
                        replaced.append(value)
                dist.brands = ",".join(replaced)

    row.name = fields["name"]
    row.short_name = fields["short_name"]
    row.code = fields["code"]
    row.logo = fields["logo"]
    row.sort_order = fields["sort_order"]
    row.active = fields["active"]
    row.note = fields["note"]
    db.commit()
    product_counts, distributor_counts = _brand_counts(db)
    return _brand_view(row, product_counts, distributor_counts)


def toggle_brand(db: Session, brand_id: int) -> dict:
    row = db.get(FxBrand, brand_id)
    if not row:
        raise ValueError("品牌不存在")
    row.active = 0 if row.active else 1
    db.commit()
    product_counts, distributor_counts = _brand_counts(db)
    return _brand_view(row, product_counts, distributor_counts)


def delete_brand(db: Session, brand_id: int) -> dict:
    """删除没有关联数据的品牌。

    商品品牌和分销商授权都是按名称存储的，没有数据库外键；如果直接删主数据，
    会留下指向不存在品牌的孤儿字符串。所以有引用时拒绝删除，引导用户先改关联
    数据，或者只停用（历史数据保留）。
    """
    row = db.get(FxBrand, brand_id)
    if not row:
        raise ValueError("品牌不存在")
    name = row.name
    product_count = db.scalar(
        select(func.count()).select_from(FxProduct).where(FxProduct.brand == name)
    ) or 0
    distributor_count = 0
    for dist in db.scalars(select(FxDistributor)).all():
        if name in brand_list_of(dist):
            distributor_count += 1
    if product_count or distributor_count:
        raise ValueError(
            f"品牌「{name}」仍被 {product_count} 个商品（含停用）、{distributor_count} 个分销商使用，"
            "不能直接删除；请先修改商品品牌或解除分销商授权，或者改为停用。"
        )
    db.delete(row)
    db.commit()
    return {"deleted": True, "id": brand_id, "name": name}


def sync_brands_from_products(db: Session) -> dict:
    """把商品里出现、但品牌主数据还没有的品牌补建出来（不覆盖已有品牌配置）。"""
    product_rows = db.execute(
        select(FxProduct.brand)
        .where(FxProduct.brand.is_not(None))
        .where(FxProduct.brand != "")
        .group_by(FxProduct.brand)
    ).scalars().all()
    existing = {row.name for row in db.scalars(select(FxBrand)).all()}
    created: list[str] = []
    for name in product_rows:
        name = (name or "").strip()
        if not name or name in existing:
            continue
        db.add(FxBrand(name=name, short_name=_short_brand_name(name)))
        existing.add(name)
        created.append(name)
    db.commit()
    return {"created": len(created), "names": created}


def _product_view(row: FxProduct) -> dict:
    pack_qty = int(row.pack_qty or 0)
    pack_unit = (row.pack_unit or "箱").strip() or "箱"
    tier1 = _money(row.tier1_price)
    # 箱价 = 一级分销价(按盒/袋) × 箱规。一级价没设或没箱规时留空，页面不显示
    tier1_box = tier1 * pack_qty if pack_qty > 0 and tier1 > 0 else None
    return {
        "id": row.id,
        "sku": row.sku,
        "name": row.name,
        "spec": row.spec,
        "unit": row.unit,
        # 箱规：pack_spec 是给页面直接显示的整串（36/箱），qty 为 0 时留空
        "pack_qty": pack_qty,
        "pack_unit": pack_unit,
        "pack_spec": f"{pack_qty}/{pack_unit}" if pack_qty > 0 else "",
        "base_price": str(_money(row.base_price)),
        # 一级分销价（含税）：0 = 还没定价
        "tier1_price": str(tier1),
        # 一级分销箱价 = 一级价 × 箱规（按箱/件拿货时的整箱单价）；没箱规或没定价时为空
        "tier1_box_price": str(tier1_box) if tier1_box is not None else "",
        # 采购价（含税/基本单位）：来自旺店通采购单，真实拿货成本；0 = 从未采购
        # 按「仓库」分组取价：这里存的是主成本仓的价，其余仓的价在 /api/fx/purchase-prices
        "purchase_price": str(_money(row.purchase_price)),
        "purchase_date": row.purchase_date.strftime("%Y-%m-%d") if row.purchase_date else "",
        "purchase_no": row.purchase_no or "",
        "purchase_warehouse": row.purchase_warehouse or "",
        "purchase_provider": row.purchase_provider or "",
        "cost_warehouse": row.cost_warehouse or "",
        # 采购价与品牌原价的差额（>0 表示牌价比实付高，说明 base_price 是牌价口径）
        "price_gap": str(_price_gap(row.base_price, row.purchase_price)),
        # 成本口径：采购入库价优先（用户明确「一切都是以采购入库价格为主算利润」），
        # 采购单没覆盖到才退回牌价，并回传用了哪个（页面要标出来）
        **_tier1_margin(
            row.purchase_price if _money(row.purchase_price) > 0 else row.base_price,
            row.tier1_price,
            "purchase" if _money(row.purchase_price) > 0 else "base",
            row.cost_status or "",
        ),
        # 牌价（含税/基本单位）：只用于算分销折扣率，不参与利润。0 = 没维护（非格力高品牌不维护）
        "list_price": str(_money(row.list_price)),
        # 分销折扣率 = 一级分销价 ÷ 牌价（如 0.85 = 8.5 折）；牌价没维护或未定价时留空
        "tier1_discount": _tier1_discount(row.tier1_price, row.list_price),
        # 成本核对状态：pending = 采购价存疑，页面标「待核对」且不出毛利
        "cost_status": row.cost_status or "",
        "cost_note": row.cost_note or "",
        # 待维护标记：缺什么、缺几项，页面据此标红并支持筛选
        # 规则在 services/product_maintenance.py —— 牌价只对格力高要求、
        # 耗材/帆布袋/好评卡不算缺项、分销价只对在售商品要求。
        **maintenance_of(row),
        "note": row.note,
        "active": bool(row.active),
        # 热销：1 = 分销下单页展示；与 active 独立，停用后仍可保留标记但不会下单
        "is_hot": bool(row.is_hot),
        # 旺店通商品档案字段
        "spec_no": row.spec_no or "",
        "goods_no": row.goods_no or "",
        "barcode": row.barcode or "",
        # 分类 = 细分类（百奇/百醇/麦片…）；大类 = 更粗的一层，用户自己维护，报表按它汇总
        "category": row.category or "",
        "top_category": row.top_category or "",
        "brand": row.brand or "",
        "source": row.source or "manual",
        "wdt_synced_at": row.wdt_synced_at.strftime("%Y-%m-%d %H:%M") if row.wdt_synced_at else "",
    }


def create_product(db: Session, data: dict) -> dict:
    sku = (data.get("sku") or "").strip()
    name = (data.get("name") or "").strip()
    if not sku or not name:
        raise ValueError("商品 SKU 和名称必填")
    exists = db.scalar(select(FxProduct).where(FxProduct.sku == sku))
    if exists:
        raise ValueError(f"商品 SKU 已存在: {sku}")
    try:
        base_price = _money(Decimal(str(data.get("base_price") or "0")))
    except Exception as exc:
        raise ValueError("基础价格格式不正确") from exc
    if base_price < 0:
        raise ValueError("基础价格不能为负数")
    row = FxProduct(
        sku=sku,
        name=name,
        spec=(data.get("spec") or "").strip(),
        unit=(data.get("unit") or "箱").strip() or "箱",
        pack_qty=_pack_qty(data.get("pack_qty")),
        pack_unit=(str(data.get("pack_unit") or "").strip() or "箱"),
        base_price=base_price,
        tier1_price=_parse_money(data.get("tier1_price"), "一级分销价"),
        note=(data.get("note") or "").strip(),
        # 手工建品也能指定品牌/分类/大类：否则手工商品品牌为空，永远进不了顶部品牌维度
        brand=(data.get("brand") or "").strip(),
        category=(data.get("category") or "").strip(),
        top_category=(data.get("top_category") or "").strip(),
        active=1,
        is_hot=1 if data.get("is_hot") else 0,
    )
    db.add(row)
    db.commit()
    return _product_view(row)


def update_product(db: Session, product_id: int, data: dict) -> dict:
    row = db.get(FxProduct, product_id)
    if not row:
        raise ValueError("商品不存在")
    new_sku = (data.get("sku") or row.sku).strip()
    if new_sku and new_sku != row.sku:
        dup = db.scalar(select(FxProduct).where(FxProduct.sku == new_sku, FxProduct.id != row.id))
        if dup:
            raise ValueError(f"商品 SKU 已存在: {new_sku}")
        row.sku = new_sku
    if "name" in data and not (data.get("name") or "").strip():
        raise ValueError("商品名称不能为空")
    if "base_price" in data and data["base_price"] is not None:
        try:
            base_price = _money(Decimal(str(data["base_price"])))
        except Exception as exc:
            raise ValueError("基础价格格式不正确") from exc
        if base_price < 0:
            raise ValueError("基础价格不能为负数")
        row.base_price = base_price
    if data.get("tier1_price") is not None:
        new_val = _parse_money(data["tier1_price"], "一级分销价")
        old_val = row.tier1_price
        row.tier1_price = new_val
        log_price_change(db, row, old_val, new_val, source="manual", note="编辑商品弹窗")
    for field in ("name", "spec", "unit", "note", "brand", "category", "top_category"):
        if field in data and data[field] is not None:
            setattr(row, field, str(data[field]).strip())
    if data.get("is_hot") is not None:
        row.is_hot = 1 if data["is_hot"] else 0
    if "pack_qty" in data and data["pack_qty"] is not None:
        row.pack_qty = _pack_qty(data["pack_qty"])
    if "pack_unit" in data and data["pack_unit"] is not None:
        row.pack_unit = str(data["pack_unit"]).strip() or row.pack_unit or "箱"
    row.unit = row.unit or "箱"
    db.commit()
    return _product_view(row)


def toggle_product(db: Session, product_id: int) -> dict:
    row = db.get(FxProduct, product_id)
    if not row:
        raise ValueError("商品不存在")
    row.active = 0 if row.active else 1
    db.commit()
    return _product_view(row)


def set_hot_products(db: Session, product_ids: list[int], is_hot: bool = True) -> dict:
    """批量标记 / 取消热销。

    只写 is_hot，不动在售状态、价格和分类。勾选里混有已删除的 id 时，
    能更新的照常更新，返回 not_found 让页面提示，而不是整批失败。
    """
    ids: list[int] = []
    for pid in product_ids:
        try:
            value = int(pid)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        raise ValueError("没有勾选商品")
    rows = db.scalars(select(FxProduct).where(FxProduct.id.in_(ids))).all()
    if not rows:
        raise ValueError("勾选的商品不存在")
    flag = 1 if is_hot else 0
    for row in rows:
        row.is_hot = flag
    db.commit()
    found = {row.id for row in rows}
    return {
        "updated": len(rows),
        "is_hot": bool(flag),
        "not_found": [pid for pid in ids if pid not in found],
    }


def set_tier1_prices(db: Session, items: list[dict]) -> dict:
    """批量设「一级分销价」（商品表里直接改价，一次提交）。

    - 传数字 = 设价；传空字符串 = 清空回「待定」；负数等非法值只报这一行，不影响其余行。
    - 不校验「必须高于品牌原价」：现实中会有清仓、临期折价，
      但前端会把低于成本的标红并提示，避免默默亏卖。
    """
    updated = 0
    errors: list[dict] = []
    for index, item in enumerate(items, start=1):
        product_id = item.get("product_id")
        row = db.get(FxProduct, int(product_id or 0)) if str(product_id or "").isdigit() else None
        if not row:
            errors.append({"row": index, "message": f"商品不存在: {product_id}"})
            continue
        try:
            new_val = _parse_money(item.get("tier1_price"), "一级分销价")
        except ValueError as exc:
            errors.append({"row": index, "message": str(exc)})
            continue
        old_val = row.tier1_price
        row.tier1_price = new_val
        log_price_change(db, row, old_val, new_val, source="manual")
        updated += 1
    db.commit()
    return {"updated": updated, "errors": errors}


def set_top_categories(db: Session, items: list[dict]) -> dict:
    """批量设「大类」（商品表里直接填，一次提交）。

    与「分类」category 完全独立：只写 top_category，绝不动 category，
    也不反过来用大类覆盖分类 —— 用户明确「分类要保留，后续有用」。
    - 传字符串 = 设大类；传空字符串 = 清空。
    """
    updated = 0
    errors: list[dict] = []
    for index, item in enumerate(items, start=1):
        product_id = item.get("product_id")
        row = db.get(FxProduct, int(product_id or 0)) if str(product_id or "").isdigit() else None
        if not row:
            errors.append({"row": index, "message": f"商品不存在: {product_id}"})
            continue
        value = str(item.get("top_category") or "").strip()
        if value == (row.top_category or ""):
            continue  # 没变化就不算一次更新
        row.top_category = value
        updated += 1
    db.commit()
    return {"updated": updated, "errors": errors}


def list_top_categories(db: Session) -> list[str]:
    """已经用过的大类值（去重、排序）—— 页面下拉/自动补全用，也方便看出填过哪些。"""
    rows = db.execute(
        select(FxProduct.top_category)
        .where(FxProduct.top_category != "")
        .distinct()
        .order_by(FxProduct.top_category)
    ).all()
    return [r[0] for r in rows if r[0]]


# ---------------------------------------------------------------- 分销订单
def _next_order_no(db: Session) -> str:
    """订单号自动生成：FX + 日期 + 当天最大序号+1（如 FX20261027-0001）。

    同时作为推送旺店通的 tid（原始单号），两边单号一致便于互查；
    重推同单走 trade_push 的 tid 更新语义，不会重复建单。
    取最大序号而非条数，避免删单后序号回绕撞唯一约束。
    """
    prefix = datetime.now().strftime("FX%Y%m%d")
    max_seq = 0
    for no in db.scalars(select(FxOrder.order_no).where(FxOrder.order_no.like(f"{prefix}-%"))):
        try:
            max_seq = max(max_seq, int(str(no).rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"{prefix}-{max_seq + 1:04d}"


def price_map(db: Session, distributor_id: int) -> dict[int, Decimal]:
    """某分销商的商品专属价：{product_id: price}。下单缺省价、价格页都靠它。"""
    if not distributor_id:
        return {}
    rows = db.scalars(select(FxPrice).where(FxPrice.distributor_id == int(distributor_id))).all()
    return {row.product_id: _money(row.price) for row in rows}


def _validate_lines(db: Session, lines: list[dict], distributor_id: int | None = None) -> tuple[list[dict], Decimal]:
    """校验订单明细并返回快照行；金额在服务端统一计算。

    单价留空时取值顺序：经销商专属价 > 一级分销价(>0) > 商品基础价。
    """
    if not lines:
        raise ValueError("订单至少需要一条明细")
    normalized: list[dict] = []
    total = Decimal("0")
    overrides = price_map(db, distributor_id or 0)
    for index, line in enumerate(lines, start=1):
        product_id = line.get("product_id")
        product = db.get(FxProduct, int(product_id or 0))
        if not product:
            raise ValueError(f"第 {index} 行商品不存在")
        if not product.active:
            raise ValueError(f"第 {index} 行商品已停用: {product.sku}")
        try:
            quantity = Decimal(str(line.get("quantity") or "0"))
        except Exception as exc:
            raise ValueError(f"第 {index} 行数量格式不正确") from exc
        if quantity <= 0:
            raise ValueError(f"第 {index} 行数量必须大于 0")
        raw_price = line.get("unit_price")
        if raw_price in (None, ""):
            # 下单默认价：经销商专属价 > 一级分销价(>0) > 商品基础价
            tier1 = Decimal(str(product.tier1_price or 0))
            default_price = overrides.get(product.id) or (tier1 if tier1 > 0 else product.base_price)
            unit_price = default_price
        else:
            try:
                unit_price = Decimal(str(raw_price))
            except Exception as exc:
                raise ValueError(f"第 {index} 行单价格式不正确") from exc
        unit_price = _money(unit_price)
        if unit_price < 0:
            raise ValueError(f"第 {index} 行单价不能为负数")
        subtotal = _money(quantity * unit_price)
        total += subtotal
        normalized.append(
            {
                "product_id": product.id,
                "goods_no": (product.goods_no or product.sku or "").strip(),
                "spec_no": (product.spec_no or product.sku or "").strip(),
                "goods_name": product.name,
                "spec_name": product.spec,
                "quantity": quantity,
                "unit_price": unit_price,
                "subtotal": subtotal,
            }
        )
    return normalized, _money(total)


def _order_view(row: FxOrder, with_lines: bool = True, pmap: dict | None = None) -> dict:
    total = _money(row.total_amount)
    paid = _money(row.paid_amount or Decimal("0"))
    original_no = (row.original_order_no or "").strip()
    # 兼容 0024 迁移前/尚未回填的历史单：wdt_tid 不是内部单号时就是原始单号。
    if not original_no and row.wdt_tid and row.wdt_tid != row.order_no:
        original_no = row.wdt_tid.strip()
    original_nos = [x.strip() for x in original_no.replace("，", ",").replace("；", ",").replace(";", ",").split(",") if x.strip()]
    distributor = row.distributor
    data = {
        "id": row.id,
        "order_no": row.order_no,
        "original_order_no": original_no,
        "original_order_nos": original_nos,
        "source_channel": row.source_channel or "",
        "distributor_id": row.distributor_id,
        "distributor_name": distributor.name if distributor else "",
        "distributor_code": distributor.code if distributor else "",
        "receiver_name": (distributor.contact or distributor.name) if distributor else "",
        "receiver_phone": distributor.phone if distributor else "",
        "receiver_address": distributor.delivery_address if distributor else "",
        "status": row.status,
        "status_label": STATUS_LABELS.get(row.status, row.status),
        "order_date": _dt_text(row.order_date),
        "total_amount": str(total),
        "remark": row.remark or "",
        "wdt_tid": row.wdt_tid or "",
        "wdt_shop_no": row.wdt_shop_no or "",
        "wdt_warehouse_no": row.wdt_warehouse_no or "",
        "payment_status": row.payment_status or "",
        "payment_status_label": PAYMENT_STATUS_LABELS.get(row.payment_status or "", row.payment_status or ""),
        "paid_amount": str(paid),
        "outstanding_amount": str(_money(max(total - paid, Decimal("0")))),
        "payment_date": _dt_text(row.payment_date, date_only=True),
        "pay_company": row.pay_company or "",
        "order_channel": row.order_channel or "",
        "ship_warehouse": row.ship_warehouse or "",
        "ship_note": row.ship_note or "",
        "delivery_type": row.delivery_type or "",
        "delivery_type_label": DELIVERY_TYPE_LABELS.get(row.delivery_type or "", ""),
        "scratch_code": int(row.scratch_code or 0),
        "shipping_status": row.shipping_status or "",
        "shipping_status_label": SHIPPING_STATUS_LABELS.get(row.shipping_status or "", row.shipping_status or ""),
        "shipped_at": _dt_text(row.shipped_at),
        "logistics_company": row.logistics_company or "",
        "logistics_no": row.logistics_no or "",
        "signed_at": _dt_text(row.signed_at),
        "push_status": row.push_status or "",
        "push_status_label": PUSH_STATUS_LABELS.get(row.push_status or "", row.push_status or ""),
        "push_message": row.push_message or "",
        "pushed_at": row.pushed_at.strftime("%Y-%m-%d %H:%M:%S") if row.pushed_at else "",
        "line_count": len(row.lines) if row.lines else 0,
    }
    if with_lines:
        # 大类/分类按当前商品资料取（不快照）：报表要按最新归类出数，
        # 商品后来改了归类，报表跟着变才对得上账。
        data["lines"] = [
            {
                "id": line.id,
                "product_id": line.product_id,
                "goods_no": line.goods_no,
                "spec_no": line.spec_no,
                "goods_name": line.goods_name,
                "spec_name": line.spec_name,
                "quantity": str(line.quantity),
                # 单价不能截成 2 位：件价常是箱价 ÷ 箱规（如 152/36=4.2222），
                # 截断后反算箱价会差钱（4.22×36=151.92）。按库里的 4 位精度原样输出。
                "unit_price": str(Decimal(line.unit_price or 0)),
                "subtotal": str(_money(line.subtotal)),
                "category": ((pmap or {}).get(line.product_id) or ("", ""))[0],
                "top_category": ((pmap or {}).get(line.product_id) or ("", ""))[1],
            }
            for line in (row.lines or [])
        ]
    return data


def create_order(
    db: Session,
    distributor_id: int,
    lines: list[dict],
    remark: str = "",
    wdt_shop_no: str = "",
    wdt_warehouse_no: str = "",
    *,
    order_channel: str = "",
    ship_warehouse: str = "",
    ship_note: str = "",
    delivery_type: str = "",
    scratch_code: int = 0,
    order_date: datetime | None = None,
) -> dict:
    distributor = db.get(FxDistributor, int(distributor_id or 0))
    if not distributor:
        raise ValueError("分销商不存在")
    if not distributor.active:
        raise ValueError(f"分销商已停用: {distributor.name}")
    delivery_type = (delivery_type or "").strip()
    if delivery_type and delivery_type not in DELIVERY_TYPES:
        raise ValueError(f"配送方式只能是 自提/送货，收到: {delivery_type}")
    snapshot, total = _validate_lines(db, lines, distributor.id)
    row = FxOrder(
        order_no=_next_order_no(db),
        distributor_id=distributor.id,
        status="draft",
        total_amount=total,
        remark=(remark or "").strip(),
        source_channel="手工",
        payment_status="unpaid",
        shipping_status="unshipped",
        wdt_shop_no=(wdt_shop_no or "").strip(),
        wdt_warehouse_no=(wdt_warehouse_no or "").strip(),
        # 订单头信息（用户 2026-10-28 要求下单时一次录全）
        order_channel=(order_channel or "").strip()[:32],
        ship_warehouse=(ship_warehouse or "").strip()[:80],
        ship_note=(ship_note or "").strip()[:500],
        delivery_type=delivery_type,
        scratch_code=1 if scratch_code else 0,
    )
    if order_date is not None:
        row.order_date = order_date
    row.lines = [
        FxOrderLine(
            product_id=item["product_id"],
            goods_no=item["goods_no"],
            spec_no=item["spec_no"],
            goods_name=item["goods_name"],
            spec_name=item["spec_name"],
            quantity=item["quantity"],
            unit_price=item["unit_price"],
            subtotal=item["subtotal"],
        )
        for item in snapshot
    ]
    db.add(row)
    db.commit()
    return _order_view(row)


def update_order(
    db: Session,
    order_id: int,
    distributor_id: int,
    lines: list[dict],
    remark: str = "",
    wdt_shop_no: str | None = None,
    wdt_warehouse_no: str | None = None,
    *,
    order_channel: str | None = None,
    ship_warehouse: str | None = None,
    ship_note: str | None = None,
    delivery_type: str | None = None,
    scratch_code: int | None = None,
    order_date: datetime | None = None,
) -> dict:
    row = db.get(FxOrder, int(order_id or 0))
    if not row:
        raise ValueError("订单不存在")
    if row.status != "draft":
        raise ValueError("只有草稿订单可以修改")
    distributor = db.get(FxDistributor, int(distributor_id or 0))
    if not distributor:
        raise ValueError("分销商不存在")
    snapshot, total = _validate_lines(db, lines, distributor.id)
    row.distributor_id = distributor.id
    row.remark = (remark or "").strip()
    row.total_amount = total
    if wdt_shop_no is not None:
        row.wdt_shop_no = (wdt_shop_no or "").strip()[:40]
    if wdt_warehouse_no is not None:
        row.wdt_warehouse_no = (wdt_warehouse_no or "").strip()[:40]
    # 订单头字段：传 None 表示这次不动它，传空串表示清空
    if order_channel is not None:
        row.order_channel = (order_channel or "").strip()[:32]
    if ship_warehouse is not None:
        row.ship_warehouse = (ship_warehouse or "").strip()[:80]
    if ship_note is not None:
        row.ship_note = (ship_note or "").strip()[:500]
    if delivery_type is not None:
        value = (delivery_type or "").strip()
        if value and value not in DELIVERY_TYPES:
            raise ValueError(f"配送方式只能是 自提/送货，收到: {value}")
        row.delivery_type = value
    if scratch_code is not None:
        row.scratch_code = 1 if scratch_code else 0
    if order_date is not None:
        row.order_date = order_date
    row.lines.clear()
    for item in snapshot:
        row.lines.append(
            FxOrderLine(
                product_id=item["product_id"],
                goods_no=item["goods_no"],
                spec_no=item["spec_no"],
                goods_name=item["goods_name"],
                spec_name=item["spec_name"],
                quantity=item["quantity"],
                unit_price=item["unit_price"],
                subtotal=item["subtotal"],
            )
        )
    db.commit()
    return _order_view(row)


def transition_order(db: Session, order_id: int, action: str) -> dict:
    if action not in {"submit", "approve", "reject", "cancel"}:
        raise ValueError(f"未知订单动作: {action}")
    row = db.get(FxOrder, int(order_id or 0))
    if not row:
        raise ValueError("订单不存在")
    target = FX_TRANSITIONS.get(row.status, {}).get(action)
    if not target:
        raise ValueError(f"当前状态[{STATUS_LABELS.get(row.status, row.status)}]不允许执行: {action}")
    row.status = target
    db.commit()
    return _order_view(row)


def batch_transition_orders(db: Session, order_ids: list[int], action: str) -> dict:
    """批量改订单状态：逐单走同一套流转规则，合法的改、不合法的单独报出来。

    不做「一单失败整批回滚」—— 已改的单留着，失败的列出来让你单独处理，
    避免因为一张脏单把整批合法操作全废掉。
    """
    if action not in {"submit", "approve", "reject", "cancel"}:
        raise ValueError(f"未知订单动作: {action}")
    done, failed = 0, []
    for oid in order_ids or []:
        try:
            row = db.get(FxOrder, int(oid or 0))
            if not row:
                raise ValueError("订单不存在")
            target = FX_TRANSITIONS.get(row.status, {}).get(action)
            if not target:
                raise ValueError(f"当前状态[{STATUS_LABELS.get(row.status, row.status)}]不允许执行: {action}")
            row.status = target
            done += 1
        except ValueError as exc:
            failed.append({"id": int(oid), "error": str(exc)})
    db.commit()
    return {"updated": done, "failed": failed}


def batch_push_orders(db: Session, order_ids: list[int]) -> dict:
    """批量重推旺店通：只推已审核且没推成功的；已推送过的跳过不重复推。"""
    done, skipped, failed = 0, 0, []
    for oid in order_ids or []:
        try:
            order = db.get(FxOrder, int(oid or 0))
            if not order:
                raise ValueError("订单不存在")
            if order.status != "approved":
                skipped += 1
                continue
            if order.push_status == "sent":
                skipped += 1
                continue
            push_order_to_wangdian(db, order.id)
            done += 1
        except ValueError as exc:
            failed.append({"id": int(oid), "error": str(exc)})
    return {"pushed": done, "skipped": skipped, "failed": failed}


def update_order_fulfillment(db: Session, order_id: int, data: dict) -> dict:
    """更新订单的收款 / 发货 / 原始单号。

    这些是业务执行状态，和审核状态分开：
    - 审核通过只代表可以推送 / 发货，不代表钱已到账或货已发出；
    - 收款、发货可分别维护，互不覆盖；
    - 标记已收到但没填金额时，用订单总额补齐，避免页面显示“已收到 0 元”。
    """
    row = (
        db.query(FxOrder)
        .options(selectinload(FxOrder.lines))
        .filter(FxOrder.id == int(order_id or 0))
        .first()
    )
    if not row:
        raise ValueError("订单不存在")

    if "original_order_no" in data:
        row.original_order_no = str(data.get("original_order_no") or "").strip()[:255]
    if "source_channel" in data:
        row.source_channel = str(data.get("source_channel") or "").strip()[:32]
    # 订单头信息：下单页录漏了可以在这里补，不用回下单页重来
    if "wdt_shop_no" in data:
        row.wdt_shop_no = str(data.get("wdt_shop_no") or "").strip()[:40]
    if "wdt_warehouse_no" in data:
        row.wdt_warehouse_no = str(data.get("wdt_warehouse_no") or "").strip()[:40]
    if "order_channel" in data:
        row.order_channel = str(data.get("order_channel") or "").strip()[:32]
    if "ship_warehouse" in data:
        row.ship_warehouse = str(data.get("ship_warehouse") or "").strip()[:80]
    if "ship_note" in data:
        row.ship_note = str(data.get("ship_note") or "").strip()[:500]
    if "delivery_type" in data:
        value = str(data.get("delivery_type") or "").strip()
        if value and value not in DELIVERY_TYPES:
            raise ValueError(f"配送方式只能是 自提/送货，收到: {value}")
        row.delivery_type = value
    if "scratch_code" in data:
        row.scratch_code = 1 if data.get("scratch_code") else 0

    if "payment_status" in data:
        status = str(data.get("payment_status") or "").strip()
        if status not in PAYMENT_STATUSES:
            raise ValueError(f"未知收款状态: {status}")
        row.payment_status = status
        if status == "received" and "paid_amount" not in data:
            row.paid_amount = row.total_amount or Decimal("0")
        if status == "received" and "payment_date" not in data and not row.payment_date:
            row.payment_date = datetime.now()
        if status == "unpaid":
            if "paid_amount" not in data:
                row.paid_amount = Decimal("0")
            if "payment_date" not in data:
                row.payment_date = None
    if "paid_amount" in data:
        try:
            paid = Decimal(str(data.get("paid_amount") or "0"))
        except Exception as exc:
            raise ValueError("已收金额格式不正确") from exc
        paid = _money(paid)
        if paid < 0:
            raise ValueError("已收金额不能为负数")
        row.paid_amount = paid
    if "payment_date" in data:
        row.payment_date = _parse_dt(data.get("payment_date"), "收款日期")
    if "pay_company" in data:
        row.pay_company = str(data.get("pay_company") or "").strip()[:160]

    paid_now = _money(row.paid_amount or Decimal("0"))
    total_now = _money(row.total_amount or Decimal("0"))
    if "payment_status" in data and row.payment_status == "partial" and paid_now <= 0:
        raise ValueError("部分收款时，已收金额必须大于 0")
    if "payment_status" in data and row.payment_status == "unpaid" and paid_now > 0:
        raise ValueError("未收到时不能填写已收金额；请改为部分收款或已收到")
    if "payment_status" in data and row.payment_status == "received" and total_now > 0 and paid_now <= 0:
        raise ValueError("已收到时，已收金额必须大于 0")

    if "payment_status" in data or "paid_amount" in data:
        # 订单详情允许手动记录无凭证收款，但不能把已确认凭证的金额冲掉。
        from ..models import FxPaymentVoucher
        applied_total = db.scalar(
            select(func.coalesce(func.sum(FxPaymentVoucher.amount), 0))
            .where(FxPaymentVoucher.order_id == row.id, FxPaymentVoucher.applied == 1)
        )
        if paid_now < _money(Decimal(applied_total or 0)):
            raise ValueError("已收金额不能低于已入账凭证合计，请先撤销相应凭证")

    if "shipping_status" in data:
        status = str(data.get("shipping_status") or "").strip()
        if status not in SHIPPING_STATUSES:
            raise ValueError(f"未知发货状态: {status}")
        row.shipping_status = status
        if status in {"shipped", "partial"} and "shipped_at" not in data and not row.shipped_at:
            row.shipped_at = datetime.now()
        if status == "unshipped" and "shipped_at" not in data:
            row.shipped_at = None
    if "shipped_at" in data:
        row.shipped_at = _parse_dt(data.get("shipped_at"), "发货时间")
    if "logistics_company" in data:
        row.logistics_company = str(data.get("logistics_company") or "").strip()[:100]
    if "logistics_no" in data:
        row.logistics_no = str(data.get("logistics_no") or "").strip()[:120]
    if "signed_at" in data:
        row.signed_at = _parse_dt(data.get("signed_at"), "签收时间")

    db.commit()
    return _order_view(row)


def order_detail(db: Session, order_id: int) -> dict:
    row = (
        db.query(FxOrder)
        .options(selectinload(FxOrder.lines))
        .filter(FxOrder.id == int(order_id or 0))
        .first()
    )
    if not row:
        raise ValueError("订单不存在")
    return _order_view(row)


def _brand_dist_ids(db: Session, brand: str) -> set[int] | None:
    """当前品牌下可见的分销商 id 集合；brand 为空返回 None（= 不过滤）。"""
    brand = (brand or "").strip()
    if not brand:
        return None
    rows = db.scalars(select(FxDistributor)).all()
    return {r.id for r in rows if distributor_in_brand(r, brand)}


def list_orders(db: Session, status: str = "", distributor_id: int | None = None, limit: int = 200,
                brand: str = "", push_status: str = "") -> list[dict]:
    stmt = (
        db.query(FxOrder)
        .options(selectinload(FxOrder.lines))
        .order_by(FxOrder.id.desc())
    )
    if status:
        if status not in FX_ORDER_STATUSES:
            raise ValueError(f"未知订单状态: {status}")
        stmt = stmt.filter(FxOrder.status == status)
    # push_status：旺店通推送结果过滤。"failed" = 异常单（推送失败待重推）
    if push_status:
        stmt = stmt.filter(FxOrder.push_status == push_status)
    if distributor_id:
        stmt = stmt.filter(FxOrder.distributor_id == int(distributor_id))
    # 品牌作用域：订单本身不带品牌，按「下单的分销商被授权的品牌」判断
    ids = _brand_dist_ids(db, brand)
    if ids is not None:
        stmt = stmt.filter(FxOrder.distributor_id.in_(ids) if ids else FxOrder.distributor_id == 0)
    # limit<=0 = 不限量（分销订单页要全量展示，查询/筛选在页面本地做）
    if limit and limit > 0:
        stmt = stmt.limit(max(1, min(limit, 500)))
    return [_order_view(row, with_lines=False) for row in stmt.all()]


def fx_overview(db: Session) -> dict:
    counts: dict[str, int] = {}
    amounts: dict[str, str] = {}
    for status in FX_ORDER_STATUSES:
        row = db.query(
            func.count(FxOrder.id), func.coalesce(func.sum(FxOrder.total_amount), 0)
        ).filter(FxOrder.status == status).one()
        counts[status] = int(row[0] or 0)
        amounts[status] = str(_money(Decimal(row[1] or 0)))
    distributor_count = db.scalar(select(func.count()).select_from(FxDistributor).where(FxDistributor.active == 1)) or 0
    product_count = db.scalar(select(func.count()).select_from(FxProduct).where(FxProduct.active == 1)) or 0
    price_override_count = db.scalar(select(func.count()).select_from(FxPrice)) or 0
    return {
        "statuses": [{"key": s, "label": STATUS_LABELS[s], "count": counts[s], "amount": amounts[s]} for s in FX_ORDER_STATUSES],
        "active_distributors": int(distributor_count),
        "active_products": int(product_count),
        "price_overrides": int(price_override_count),
        "status_labels": STATUS_LABELS,
    }


# ---------------------------------------------------------------- 经销商价格体系
def _price_view(row: FxPrice, dmap: dict[int, FxDistributor], pmap: dict[int, FxProduct]) -> dict:
    dist = dmap.get(row.distributor_id)
    prod = pmap.get(row.product_id)
    return {
        "id": row.id,
        "distributor_id": row.distributor_id,
        "distributor_code": dist.code if dist else "",
        "distributor_name": dist.name if dist else "",
        "product_id": row.product_id,
        "sku": prod.sku if prod else "",
        "product_name": prod.name if prod else "",
        "spec": prod.spec if prod else "",
        "unit": prod.unit if prod else "",
        "base_price": str(_money(prod.base_price)) if prod else "0.00",
        "price": str(_money(row.price)),
        "note": row.note or "",
        "updated_at": row.updated_at.strftime("%Y-%m-%d %H:%M") if row.updated_at else "",
    }


def list_prices(db: Session, distributor_id: int | None = None) -> list[dict]:
    stmt = select(FxPrice).order_by(FxPrice.distributor_id, FxPrice.product_id)
    if distributor_id:
        stmt = stmt.where(FxPrice.distributor_id == int(distributor_id))
    rows = db.scalars(stmt).all()
    dmap = {row.id: row for row in db.scalars(select(FxDistributor)).all()}
    pmap = {row.id: row for row in db.scalars(select(FxProduct)).all()}
    return [_price_view(row, dmap, pmap) for row in rows]


def set_price(db: Session, distributor_id: int, product_id: int, price: Decimal | str, note: str = "") -> dict:
    dist = db.get(FxDistributor, int(distributor_id or 0))
    if not dist:
        raise ValueError("分销商不存在")
    prod = db.get(FxProduct, int(product_id or 0))
    if not prod:
        raise ValueError("商品不存在")
    try:
        value = _money(Decimal(str(price)))
    except Exception as exc:
        raise ValueError("价格格式不正确") from exc
    if value < 0:
        raise ValueError("价格不能为负数")
    row = db.scalar(select(FxPrice).where(FxPrice.distributor_id == dist.id, FxPrice.product_id == prod.id))
    if row:
        row.price = value
        row.note = (note or "").strip()
    else:
        row = FxPrice(distributor_id=dist.id, product_id=prod.id, price=value, note=(note or "").strip())
        db.add(row)
    db.commit()
    dmap = {r.id: r for r in [dist]}
    pmap = {r.id: r for r in [prod]}
    return _price_view(row, dmap, pmap)


def delete_price(db: Session, distributor_id: int, product_id: int) -> dict:
    row = db.scalar(select(FxPrice).where(FxPrice.distributor_id == int(distributor_id), FxPrice.product_id == int(product_id)))
    if not row:
        raise ValueError("该商品未设置经销商专属价")
    db.delete(row)
    db.commit()
    return {"ok": True, "distributor_id": int(distributor_id), "product_id": int(product_id)}


# ---------------------------------------------------------------- 批量导入（粘贴文本 → 结构化列表在服务端落库）
def import_distributors(db: Session, items: list[dict]) -> dict:
    """批量建分销商。重复编码跳过，单行出错不影响其余行。"""
    created = skipped = 0
    skipped_codes: list[str] = []
    errors: list[dict] = []
    for index, item in enumerate(items, start=1):
        try:
            create_distributor(db, item)
            created += 1
        except ValueError as exc:
            message = str(exc)
            if "已存在" in message:
                skipped += 1
                skipped_codes.append(str(item.get("code") or ""))
            else:
                errors.append({"row": index, "message": message})
        except Exception as exc:  # 防御：单行数据脏不能中断整批
            errors.append({"row": index, "message": str(exc)})
    return {"created": created, "skipped": skipped, "skipped_codes": skipped_codes, "errors": errors}


def import_products(db: Session, items: list[dict]) -> dict:
    """批量建商品。重复 SKU 跳过，单行出错不影响其余行。"""
    created = skipped = 0
    skipped_skus: list[str] = []
    errors: list[dict] = []
    for index, item in enumerate(items, start=1):
        try:
            create_product(db, item)
            created += 1
        except ValueError as exc:
            message = str(exc)
            if "已存在" in message:
                skipped += 1
                skipped_skus.append(str(item.get("sku") or ""))
            else:
                errors.append({"row": index, "message": message})
        except Exception as exc:
            errors.append({"row": index, "message": str(exc)})
    return {"created": created, "skipped": skipped, "skipped_skus": skipped_skus, "errors": errors}


# ---------------------------------------------------------------- 推送旺店通
_MUNICIPALITIES = ("北京市", "上海市", "天津市", "重庆市")


def _split_cn_address(value: str) -> tuple[str, str, str, str]:
    """把中文详细地址拆成省 / 市 / 区县 / 详细地址。

    旺店通 trade_push.php 要求 receiver_province / city / district 与地址库
    匹配；如果只传详细地址，官方要求省市区之间用空格分隔才会尝试解析。
    分销商档案里的地址是「上海市松江区潮富路78号」这种连续写法，所以这里
    先拆出省市区，再把剩余部分作为 receiver_address 推送。
    """
    text = (value or "").strip()
    if not text:
        return "", "", "", ""
    for municipality in _MUNICIPALITIES:
        if text.startswith(municipality):
            rest = text[len(municipality):].strip()
            match = re.match(r"^(.+?(?:区|县|市))", rest)
            district = match.group(1) if match else ""
            detail = rest[len(district):].strip() if district else rest
            return municipality, municipality, district, detail or rest
    province = ""
    rest = text
    match = re.match(r"^(.+?(?:省|自治区|特别行政区))", rest)
    if match:
        province = match.group(1)
        rest = rest[len(province):].strip()
    city = ""
    match = re.match(r"^(.+?(?:市|自治州|地区|盟))", rest)
    if match:
        city = match.group(1)
        rest = rest[len(city):].strip()
    district = ""
    match = re.match(r"^(.+?(?:区|县|旗|市))", rest)
    if match:
        district = match.group(1)
        rest = rest[len(district):].strip()
    return province, city, district, rest or text


def _build_sales_trade(order: FxOrder, distributor: FxDistributor, shop_no: str) -> dict:
    """把分销订单组装成旺店通 `trade_push.php` 报文（不在此处发请求）。

    用户 2026-10-28 要求下单页把旺店通需要的字段都体现出来。官方必填项：
    shop_no、tid、trade_status、delivery_term、buyer_nick、receiver_name、
    receiver_province/city/district/address/mobile、post_amount、cod_amount、
    ext_cod_fee、other_amount、paid、warehouse_no、order_list。

    其中：
    - 店铺/仓库由下单页选择，仓库只允许 HHZ-Glico（红盒子-格力高磐石仓）；
    - 收货人/手机/地址取自分销商档案（下单页会显示，缺项会提示）；
    - 实收金额与支付状态取本系统已确认收款，不得默认全额已付；发货条件仍是款到发货；
    - 货品明细按官方 order_list 结构生成，而不是旧的 goods_list。
    """
    address = (distributor.delivery_address or "").strip()
    province, city, district, detail = _split_cn_address(address)
    receiver_name = (distributor.contact or distributor.name or "").strip()[:40]
    receiver_mobile = (distributor.phone or "").strip()[:40]
    buyer_nick = (distributor.name or f"FX{order.distributor_id}").strip()[:100]
    warehouse_no = (
        (getattr(order, "wdt_warehouse_no", "") or "").strip()
        or (settings.wdt_warehouse_no or "").strip()
        or DEFAULT_WDT_WAREHOUSE_NO
    )

    lines: list[dict] = []
    for index, line in enumerate(order.lines or [], start=1):
        goods_no = (line.goods_no or "").strip()
        spec_no = (line.spec_no or goods_no or "").strip()
        goods_name = (line.goods_name or spec_no or goods_no or "").strip()[:255]
        lines.append({
            "oid": f"{order.order_no}-{index:03d}",
            "status": 30,
            "refund_status": 0,
            # 本系统 goods_no 就是旺店通商家编码（SKU/条码），goods_id 用它兜底；
            # 没有平台 SPU 编码时不能留空，否则旺店通拒单。
            "goods_id": goods_no or spec_no or str(line.product_id or ""),
            "spec_id": (line.spec_no or goods_no or "").strip()[:40],
            "goods_no": goods_no,
            "spec_no": spec_no,
            "goods_name": goods_name,
            "spec_name": (line.spec_name or "").strip()[:100],
            "num": _plain(Decimal(line.quantity or 0)),
            "price": _plain(_money(Decimal(line.unit_price or 0))),
            "adjust_amount": "0",
            "discount": "0",
            "share_discount": "0",
        })

    seller_memo = "；".join(x for x in (
        (order.ship_note or "").strip(),
        "配送：自提" if order.delivery_type == "pickup" else "",
        "客户要求刮码" if int(order.scratch_code or 0) else "",
    ) if x)[:1024]

    total_amount = _money(Decimal(order.total_amount or 0))
    paid_amount = _money(Decimal(order.paid_amount or 0))
    if paid_amount < 0 or paid_amount > total_amount:
        raise ValueError("订单已收金额与订单总额不一致，请核对后再推送旺店通")
    if order.payment_status == "refunded":
        raise ValueError("退款订单须核对退款明细，不能按普通订单推送旺店通")
    # 旺店通 trade_push.php：10待支付 / 20待尾款 / 30已支付；
    # pay_status 0未付款 / 1部分付款 / 2已付款。不得把未收款订单谎报成全额已付。
    pay_status = 0 if paid_amount == 0 else 2 if paid_amount >= total_amount else 1
    trade_status = {0: 10, 1: 20, 2: 30}[pay_status]
    pay_time = (
        order.payment_date.strftime("%Y-%m-%d %H:%M:%S")
        if paid_amount > 0 and order.payment_date else "0000-00-00 00:00:00"
    )

    trade = {
        "tid": order.order_no,  # 原始单号用分销订单号，便于两边互查
        "trade_status": trade_status,
        "pay_status": pay_status,
        "delivery_term": 1,     # 款到发货；未付款单先入 ERP，等待收款后放行
        "fenxiao_type": 2,      # 经销
        "trade_time": order.order_date.strftime("%Y-%m-%d %H:%M:%S") if order.order_date else "",
        "pay_time": pay_time,
        "buyer_nick": buyer_nick,
        "buyer_message": (order.remark or "")[:1024],
        "seller_memo": seller_memo,
        "receiver_name": receiver_name,
        "receiver_mobile": receiver_mobile,
        "receiver_province": province,
        "receiver_city": city,
        "receiver_district": district,
        "receiver_address": (detail or address)[:256],
        # 当前业务不收运费/到付款/其他费用，显式传 0，避免旺店通按缺省值算错。
        "post_amount": "0",
        "cod_amount": "0",
        "ext_cod_fee": "0",
        "other_amount": "0",
        "paid": _plain(paid_amount),
        "warehouse_no": warehouse_no,
        "order_list": lines,
    }
    return {"shop_no": shop_no, "trade_list": [trade]}


def detect_push_status(db: Session, order_ids: list[int] | None = None) -> dict:
    """自动核对订单在旺店通里的真实状态，不再靠人点「推送旺店通」。

    为什么需要这个：历史团单（格力高 B2B）本来就是在旺店通里开的单、出的库，
    原表「出库-旺店通销售」那一列记着出库单号。这类单再让用户点一次推送，
    既多余又危险（会在旺店通里造重复单）。所以按证据自动判定，判据从强到弱：

    1. 销售出库单里能按 trade_no / src_trade_no 找到对应单号
       —— 最硬的证据，顺便把真实出库时间写进 pushed_at；
    2. 单据自己带旺店通单号（导入的历史团单，或以前推成功过的）
       —— 说明它已经在旺店通里存在；
    3. 单号什么都没有、但来源是历史团单导入
       —— 标 history：不冒充已推送，也不再出现在待推送里。

    只改「还没判定过」的单（push_status 为 "" / "history"），
    已经 sent / failed 的一律不动，避免把失败记录洗白。
    """
    stmt = select(FxOrder)
    if order_ids:
        stmt = stmt.where(FxOrder.id.in_([int(i) for i in order_ids]))
    orders = list(db.scalars(stmt).all())

    # 出库单索引：一个旺店通单号可能既是 trade_no 又是 src_trade_no
    shipped: dict[str, datetime] = {}
    for so in db.scalars(select(StockoutOrder)).all():
        for key in (so.trade_no, so.src_trade_no):
            k = (key or "").strip()
            if k and so.consign_time:
                shipped.setdefault(k, so.consign_time)

    stats = {"checked": 0, "shipped": 0, "pushed": 0, "history": 0, "kept": 0}
    for o in orders:
        stats["checked"] += 1
        if o.push_status in ("sent", "failed"):
            stats["kept"] += 1
            continue
        tids = [t.strip() for t in (o.wdt_tid or "").split(",") if t.strip()]
        hit = next((shipped[t] for t in tids if t in shipped), None)
        if hit:
            o.push_status = "sent"
            o.push_message = f"自动核对：旺店通已出库（{hit:%Y-%m-%d %H:%M}）"[:480]
            o.pushed_at = o.pushed_at or hit
            stats["shipped"] += 1
        elif tids:
            o.push_status = "sent"
            o.push_message = (
                f"自动核对：单据已含旺店通单号 {tids[0]}" + (f" 等 {len(tids)} 个" if len(tids) > 1 else "")
            )[:480]
            o.pushed_at = o.pushed_at or o.order_date
            stats["pushed"] += 1
        elif (o.remark or "").startswith("团单导入"):
            if o.push_status == "history":
                stats["kept"] += 1
                continue
            o.push_status = "history"
            o.push_message = "历史团单：原表未记录旺店通单号，未在系统内推送"[:480]
            stats["history"] += 1
        else:
            stats["kept"] += 1
    db.commit()
    return stats


def reconcile_orders_stockout(db: Session) -> dict:
    """订单 ⇄ 出库件数核对（单位口径按旺店通销售出库单反推确认）。

    旺店通销售出库单里的 `goods_count` / `sell_price` 是**基本单位（件）**口径，
    而分销订单按业务习惯录入、库里的 quantity 也是基本单位（件），两边应当 1:1。
    历史上的错单是「按箱录但按件存」，相差一个箱规（如 36 倍）；2026-10-28 已用
    scripts/fix_order_units_20261028.py 统一成件口径，所以这里按件直比，并把
    差额折算成箱数（diff ÷ 箱规）方便业务直观理解。

    另外补两类靠单号比不出来的问题：
    - 重复单：多张分销订单共用同一个旺店通单号（同一批出库被重复认领）；
    - 金额不符：整单金额与出库单金额不一致。
    """
    # 出库单号索引：一个出库单可能同时记着 trade_no / src_trade_no / src_tids 多个号
    so_no_by_oid: dict[int, str] = {}
    so_meta: dict[str, dict] = {}
    so_by_tid: dict[str, set[str]] = {}
    for so in db.scalars(select(StockoutOrder)).all():
        sno = so.stockout_no
        so_no_by_oid[so.id] = sno
        so_meta[sno] = {
            "consign_time": so.consign_time.strftime("%Y-%m-%d %H:%M") if so.consign_time else "",
            "amount": Decimal(so.goods_total_amount or 0),
        }
        tids: set[str] = set()
        for key in (so.trade_no, so.src_trade_no, so.src_tids):
            for t in (key or "").split(","):
                t = t.strip()
                if t:
                    tids.add(t)
        for t in tids:
            so_by_tid.setdefault(t, set()).add(sno)

    # 出库明细按 (单号, 规格) 聚合件数（同一规格可能拆多张出库单）
    so_qty: dict[tuple[str, str], Decimal] = {}
    so_name: dict[str, str] = {}
    for it in db.scalars(select(StockoutItem)).all():
        sno = so_no_by_oid.get(it.order_id)
        spec = (it.spec_no or "").strip()
        if not sno or not spec:
            continue
        k = (sno, spec)
        so_qty[k] = so_qty.get(k, Decimal("0")) + (it.goods_count or 0)
        so_name.setdefault(spec, it.goods_name or "")

    products = {p.spec_no: p for p in db.scalars(select(FxProduct)).all() if p.spec_no}
    known_specs = set(products) | {spec for (_, spec) in so_qty}
    orders = db.scalars(select(FxOrder).order_by(FxOrder.order_no)).all()

    # 旺店通单号被哪些分销订单占用：>1 就是重复单（同一批出库被认领两次）
    tid_owners: dict[str, list[str]] = {}
    for o in orders:
        for t in (o.wdt_tid or "").split(","):
            t = t.strip()
            if t:
                tid_owners.setdefault(t, []).append(o.order_no)

    rows: list[dict] = []
    stats = {"checked": 0, "match": 0, "mismatch": 0, "no_ship": 0, "duplicate": 0}
    for o in orders:
        if not o.lines:
            continue
        stats["checked"] += 1
        wdt_tids = [t.strip() for t in (o.wdt_tid or "").split(",") if t.strip()]
        snos: set[str] = set()
        for t in [o.order_no] + wdt_tids:
            snos |= so_by_tid.get(t, set())
        dup_with = sorted({n for t in wdt_tids for n in tid_owners.get(t, []) if n != o.order_no})

        o_qty: dict[str, Decimal] = {}
        o_name: dict[str, str] = {}
        for line in o.lines:
            snapshot_spec = (line.spec_no or "").strip()
            snapshot_goods = (line.goods_no or "").strip()
            # 新单 spec_no 是旺店通规格编码；历史旧单曾误把规格名称放在 spec_no，
            # 将实际 SKU 放进 goods_no。仅在能匹配到已知规格时回退，绝不拿货品编码
            # 去覆盖新单的真实规格编码。
            spec = (
                snapshot_spec if snapshot_spec in known_specs
                else snapshot_goods if snapshot_goods in known_specs
                else snapshot_spec or snapshot_goods
            )
            if not spec:
                continue
            o_qty[spec] = o_qty.get(spec, Decimal("0")) + (line.quantity or 0)
            o_name.setdefault(spec, line.goods_name or "")

        base = {
            "order_id": o.id,
            "order_no": o.order_no,
            "order_date": o.order_date.strftime("%Y-%m-%d") if o.order_date else "",
            "push_status": o.push_status,
            "order_amount": str(Decimal(o.total_amount or 0)),
        }
        if not snos:
            stats["no_ship"] += 1
            rows.append({**base, "status": "no_ship",
                         "total_order": str(sum(o_qty.values(), Decimal("0"))),
                         "total_stockout": "0", "stockouts": [], "duplicate_with": dup_with,
                         "amount_diff": "", "lines": [
                             {"spec_no": s, "goods_name": o_name.get(s, ""), "order_qty": str(q),
                              "stockout_qty": "0", "diff": str(-q), "pack_qty": 0, "diff_boxes": ""}
                             for s, q in o_qty.items()]})
            continue

        specs = sorted(set(o_qty) | {s for (sn, s) in so_qty if sn in snos})
        lines_out = []
        qty_ok = True
        for spec in specs:
            oq = o_qty.get(spec, Decimal("0"))
            sq = sum((so_qty.get((sn, spec), Decimal("0")) for sn in snos), Decimal("0"))
            diff = sq - oq
            if diff != 0:
                qty_ok = False
            prod = products.get(spec)
            pack = int(prod.pack_qty or 0) if prod else 0
            lines_out.append({
                "spec_no": spec,
                "goods_name": o_name.get(spec) or so_name.get(spec) or "",
                "order_qty": str(oq),
                "stockout_qty": str(sq),
                "diff": str(diff),
                "pack_qty": pack,
                "diff_boxes": (str((diff / pack).quantize(Decimal("0.01"))) if pack and diff else ""),
            })

        so_amount = sum((so_meta[s]["amount"] for s in snos), Decimal("0"))
        amount_diff = (so_amount - Decimal(o.total_amount or 0))
        if dup_with:
            worst = "duplicate"
        elif not qty_ok or amount_diff != 0:
            worst = "mismatch"
        else:
            worst = "match"
        stats[worst] += 1
        consign = max((so_meta[s]["consign_time"] for s in snos if so_meta[s]["consign_time"]), default="")
        rows.append({**base, "status": worst,
                     "total_order": str(sum(o_qty.values(), Decimal("0"))),
                     "total_stockout": str(sum((q for (sn, _), q in so_qty.items() if sn in snos), Decimal("0"))),
                     "stockout_amount": str(so_amount),
                     "amount_diff": (str(amount_diff) if amount_diff else ""),
                     "stockouts": sorted(snos), "consign_time": consign,
                     "duplicate_with": dup_with, "lines": lines_out})

    # 问题单排最前：数量/金额不符 → 重复单 → 未找到出库单 → 一致
    rank = {"mismatch": 0, "duplicate": 1, "no_ship": 2, "match": 3}
    rows.sort(key=lambda r: (rank[r["status"]], r["order_date"] or "", r["order_no"]))
    return {"stats": stats, "rows": rows}


def push_order_to_wangdian(db: Session, order_id: int) -> dict:
    """把已审核的分销订单推送到旺店通（销售单）。

    报文按旺店通 `trade_push.php`（trade_list + order_list）组装；
    未配置密钥 / 店铺号时报清晰的 ValueError，不落失败记录；
    推送失败落 push_status=failed 并把报错存 push_message，便于重试前排查。
    """
    order = db.get(FxOrder, int(order_id or 0))
    if not order:
        raise ValueError("订单不存在")
    if order.status != "approved":
        raise ValueError("只有已审核的订单可以推送旺店通")
    if order.push_status == "sent":
        raise ValueError(f"订单已推送过（单号 {order.wdt_tid or order.order_no}），不要重复推送")
    # 重复单拦截：旺店通单号是销售/出库单的唯一凭据，一张单只能被一个分销订单认领。
    # 团单 Excel 里出现过同一单号（如 202610201430）落在两个送货块，导入后生成两张
    # 明细重叠的订单；trade_push 是 update 语义，推过去只会互相覆盖明细、金额对不上。
    tids = [t.strip() for t in (order.wdt_tid or "").split(",") if t.strip()]
    if tids:
        conflicts = []
        for other in db.scalars(
            select(FxOrder)
            .where(
                FxOrder.id != order.id,
                FxOrder.wdt_tid.isnot(None),
                FxOrder.wdt_tid != "",
            )
            .order_by(FxOrder.order_no)
        ).all():
            hit = {t.strip() for t in (other.wdt_tid or "").split(",") if t.strip()} & set(tids)
            if hit:
                conflicts.append(f"{other.order_no}（{'、'.join(sorted(hit))}）")
        if conflicts:
            raise ValueError(
                f"旺店通单号 {'、'.join(tids)} 已被 {'、'.join(conflicts)} 占用，"
                "同一张旺店通单不能由多张分销订单重复认领；"
                "请先在「订单 ⇄ 件数核对」里核对重复单、确认单号归属并清理后再推送"
            )
    distributor = order.distributor
    if not distributor:
        raise ValueError("订单缺少分销商信息，无法推送")

    client = WangdianClient()
    if not client.configured:
        missing = client.missing
        raise ValueError(f"旺店通未配置，请在 .env 补齐后重启：{'、'.join(missing)}")
    # 店铺：订单上记的优先（下单时选定），否则回退系统默认配置
    shop_no = order.wdt_shop_no or settings.wdt_shop_no
    if not shop_no:
        raise ValueError("未选择推送店铺：下单时请选择店铺，或在 .env 配置 WDT_SHOP_NO")

    trade = _build_sales_trade(order, distributor, shop_no)
    try:
        result = client.sales_trade_push(trade)
    except Exception as exc:
        order.push_status = "failed"
        order.push_message = str(exc)[:480]
        order.pushed_at = datetime.now()
        db.commit()
        raise ValueError(f"推送旺店通失败：{exc}") from exc

    message = ""
    if isinstance(result, dict):
        message = str(result.get("message") or result.get("msg") or "")
        new_tid = str(result.get("tid") or result.get("trade_id") or "")
        if new_tid:
            order.wdt_tid = new_tid
        elif not order.wdt_tid:
            # trade_push 的响应只返回 new_count/chg_count，不回 tid；
            # 本系统推送时 tid 就是 order_no，记下来便于订单详情追溯。
            order.wdt_tid = order.order_no
        new_count = int(result.get("new_count") or 0)
        chg_count = int(result.get("chg_count") or 0)
        if not message and (new_count or chg_count):
            message = f"新增 {new_count} 单，更新 {chg_count} 单"
    order.push_status = "sent"
    order.push_message = message[:480]
    order.pushed_at = datetime.now()
    db.commit()
    return {"ok": True, "order": _order_view(order)}


def create_and_push_order(
    db: Session,
    distributor_id: int,
    lines: list[dict],
    remark: str = "",
    wdt_shop_no: str = "",
    wdt_warehouse_no: str = "",
    *,
    order_channel: str = "",
    ship_warehouse: str = "",
    ship_note: str = "",
    delivery_type: str = "",
    scratch_code: int = 0,
    order_date: datetime | None = None,
) -> dict:
    """下单页一键流程：创建订单 → 自动审核通过 → 推送旺店通。

    推送失败不回滚订单（单据已落库、状态为已审核），把报错带给前端，
    用户在「分销订单」页修正后可重推。
    """
    view = create_order(
        db,
        distributor_id,
        lines,
        remark,
        wdt_shop_no=wdt_shop_no,
        wdt_warehouse_no=wdt_warehouse_no,
        order_channel=order_channel,
        ship_warehouse=ship_warehouse,
        ship_note=ship_note,
        delivery_type=delivery_type,
        scratch_code=scratch_code,
        order_date=order_date,
    )
    order = db.get(FxOrder, int(view["id"]))
    transition_order(db, order.id, "submit")
    transition_order(db, order.id, "approve")
    pushed = False
    push_error = ""
    try:
        push_order_to_wangdian(db, order.id)
        pushed = True
    except ValueError as exc:
        push_error = str(exc)
    db.refresh(order)
    return {"order": _order_view(order), "pushed": pushed, "push_error": push_error}


# ---------------------------------------------------------------- 分销商对账单
def list_statements(db: Session, distributor_id: int | None = None, month: str = "", brand: str = "") -> dict:
    """按月汇总已审核订单；指定月份时返回该月订单明细（供页面表格 / Excel 用）。"""
    stmt = (
        db.query(FxOrder)
        .options(selectinload(FxOrder.lines))
        .filter(FxOrder.status == "approved")
        .order_by(FxOrder.order_date.desc(), FxOrder.id.desc())
    )
    if distributor_id:
        stmt = stmt.filter(FxOrder.distributor_id == int(distributor_id))
    # 品牌作用域：与订单页同一口径 —— 按下单分销商被授权的品牌过滤
    ids = _brand_dist_ids(db, brand)
    if ids is not None:
        stmt = stmt.filter(FxOrder.distributor_id.in_(ids) if ids else FxOrder.distributor_id == 0)
    orders = stmt.all()

    months_map: dict[str, dict] = {}
    for order in orders:
        key = order.order_date.strftime("%Y-%m") if order.order_date else "未知"
        bucket = months_map.setdefault(key, {"month": key, "order_count": 0, "total_amount": 0})
        bucket["order_count"] += 1
        bucket["total_amount"] += Decimal(order.total_amount or 0)

    distributor = db.get(FxDistributor, int(distributor_id)) if distributor_id else None
    month_value = ""
    selected = orders
    if month:
        month_value = month
        selected = [o for o in orders if (o.order_date.strftime("%Y-%m") if o.order_date else "未知") == month]

    # 一次性把明细里出现过的商品归类查出来：订单行只存商品快照，
    # 大类/分类要从商品资料带出来（报表按最新归类出数，不是快照）
    pids = {line.product_id for o in selected for line in (o.lines or []) if line.product_id}
    pmap: dict[int, tuple[str, str]] = {}
    if pids:
        pmap = {
            p.id: (p.category or "", p.top_category or "")
            for p in db.query(FxProduct).filter(FxProduct.id.in_(pids)).all()
        }

    return {
        "distributor": _distributor_view(distributor) if distributor else None,
        "months": sorted(
            (
                {
                    "month": value["month"],
                    "order_count": value["order_count"],
                    "total_amount": str(_money(value["total_amount"])),
                }
                for value in months_map.values()
            ),
            key=lambda item: item["month"],
            reverse=True,
        ),
        "month": month_value,
        "orders": [_order_view(order, pmap=pmap) for order in selected],
        "total_amount": str(_money(sum(Decimal(o.total_amount or 0) for o in selected))),
    }


def statement_excel_bytes(db: Session, distributor_id: int, month: str) -> tuple[str, bytes]:
    """导出某分销商某月的对账单 xlsx（一级：按订单明细行展开）。"""
    if not month:
        raise ValueError("请选择月份后再导出")
    dist = db.get(FxDistributor, int(distributor_id or 0))
    if not dist:
        raise ValueError("请选择分销商后再导出")
    data = list_statements(db, distributor_id=dist.id, month=month)
    if not data["orders"]:
        raise ValueError(f"{month} 没有已审核订单，无可导出内容")

    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    wb = Workbook()
    ws = wb.active
    ws.title = "对账单"
    ws.append([f"{dist.name}（{dist.code}） {month} 分销对账单"])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=10)
    ws.cell(row=1, column=1).font = Font(bold=True, size=13)
    ws.cell(row=1, column=1).alignment = Alignment(horizontal="center")
    ws.append([])
    ws.append(["订单号", "下单时间", "大类", "分类", "商品编号", "商品名称", "规格", "数量", "单价（元）", "小计（元）"])
    for cell in ws[3]:
        cell.font = Font(bold=True)

    for order in data["orders"]:
        for line in order.get("lines") or []:
            ws.append([
                order["order_no"],
                order["order_date"],
                line.get("top_category") or "",
                line.get("category") or "",
                line["goods_no"],
                line["goods_name"],
                line["spec_name"],
                float(Decimal(line["quantity"])),
                float(Decimal(line["unit_price"])),
                float(Decimal(line["subtotal"])),
            ])
    ws.append([])
    ws.append(["合计", "", "", "", "", "", "", "", "", float(Decimal(data["total_amount"]))])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)

    # 简单列宽，肉眼可读即可
    widths = (16, 18, 12, 12, 14, 24, 14, 8, 12, 12)
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + index)].width = width

    buffer = io.BytesIO()
    wb.save(buffer)
    filename = f"对账单_{dist.code}_{month}.xlsx"
    return filename, buffer.getvalue()
