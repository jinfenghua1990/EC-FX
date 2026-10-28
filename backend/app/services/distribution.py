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
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..models import FxDistributor, FxOrder, FxOrderLine, FxPrice, FxProduct
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
    stmt = select(FxDistributor).order_by(FxDistributor.id.desc())
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
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "contact": row.contact,
        "phone": row.phone,
        "email": row.email,
        "tax_no": row.tax_no or "",
        "brands": brand_list_of(row),
        "region": row.region,
        "note": row.note,
        "active": bool(row.active),
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
    }


def _brands_to_text(value) -> str:
    """录入的品牌可能是数组（前端勾选）也可能是逗号串（批量导入），统一成去重后的逗号串。"""
    items = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    out: list[str] = []
    for b in items:
        b = str(b).strip()
        if b and b not in out:
            out.append(b)
    return ",".join(out)


def _next_distributor_code(db: Session) -> str:
    """编码留空时自动生成：从 D001 起找第一个没人用的号（编号保持连续，不跳号）。"""
    used = set(db.scalars(select(FxDistributor.code)).all())
    n = 1
    while f"D{n:03d}" in used:
        n += 1
    return f"D{n:03d}"


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
    row = FxDistributor(
        code=code,
        name=name,
        contact=(data.get("contact") or "").strip(),
        phone=(data.get("phone") or "").strip(),
        email=(data.get("email") or "").strip(),
        tax_no=(data.get("tax_no") or "").strip(),
        brands=_brands_to_text(data.get("brands")),
        note=(data.get("note") or "").strip(),
        active=1,
    )
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
    for field in ("name", "contact", "phone", "email", "tax_no", "note"):
        if field in data and data[field] is not None:
            setattr(row, field, str(data[field]).strip())
    if "brands" in data and data["brands"] is not None:
        row.brands = _brands_to_text(data["brands"])
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


# ---------------------------------------------------------------- 商品
def list_products(db: Session, include_inactive: bool = True, brand: str = "") -> list[dict]:
    stmt = select(FxProduct).order_by(FxProduct.id.desc())
    if not include_inactive:
        stmt = stmt.where(FxProduct.active == 1)
    if brand:
        stmt = stmt.where(FxProduct.brand == brand)
    return [_product_view(r) for r in db.scalars(stmt).all()]


def list_brands(db: Session) -> list[dict]:
    """按商品档案的品牌聚合，供顶部「品牌维度」切换器使用。

    - 只统计在售商品：停用的历史商品不该在切换器里占一个入口。
    - 空品牌不进列表：旺店通档案里没填品牌的商品只在「全部品牌」下出现。
    - 按商品数倒序，同数量时按品牌名排，保证每次刷新顺序稳定。
    """
    rows = db.execute(
        select(FxProduct.brand, func.count(FxProduct.id).label("cnt"))
        .where(FxProduct.active == 1)
        .where(FxProduct.brand.is_not(None))
        .where(FxProduct.brand != "")
        .group_by(FxProduct.brand)
        .order_by(func.count(FxProduct.id).desc(), FxProduct.brand)
    ).all()
    return [{"brand": brand, "count": cnt} for brand, cnt in rows]


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
    重推同单走 sales_trade_push 的 tid 更新语义，不会重复建单。
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
                "goods_no": product.sku,
                "spec_no": product.spec,
                "goods_name": product.name,
                "spec_name": product.spec,
                "quantity": quantity,
                "unit_price": unit_price,
                "subtotal": subtotal,
            }
        )
    return normalized, _money(total)


def _order_view(row: FxOrder, with_lines: bool = True, pmap: dict | None = None) -> dict:
    data = {
        "id": row.id,
        "order_no": row.order_no,
        "distributor_id": row.distributor_id,
        "distributor_name": row.distributor.name if row.distributor else "",
        "distributor_code": row.distributor.code if row.distributor else "",
        "status": row.status,
        "status_label": STATUS_LABELS.get(row.status, row.status),
        "order_date": row.order_date.strftime("%Y-%m-%d %H:%M") if row.order_date else "",
        "total_amount": str(_money(row.total_amount)),
        "remark": row.remark or "",
        "wdt_tid": row.wdt_tid or "",
        "wdt_shop_no": row.wdt_shop_no or "",
        "push_status": row.push_status or "",
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
                "unit_price": str(_money(line.unit_price)),
                "subtotal": str(_money(line.subtotal)),
                "category": ((pmap or {}).get(line.product_id) or ("", ""))[0],
                "top_category": ((pmap or {}).get(line.product_id) or ("", ""))[1],
            }
            for line in (row.lines or [])
        ]
    return data


def create_order(db: Session, distributor_id: int, lines: list[dict], remark: str = "", wdt_shop_no: str = "") -> dict:
    distributor = db.get(FxDistributor, int(distributor_id or 0))
    if not distributor:
        raise ValueError("分销商不存在")
    if not distributor.active:
        raise ValueError(f"分销商已停用: {distributor.name}")
    snapshot, total = _validate_lines(db, lines, distributor.id)
    row = FxOrder(
        order_no=_next_order_no(db),
        distributor_id=distributor.id,
        status="draft",
        total_amount=total,
        remark=(remark or "").strip(),
        wdt_shop_no=(wdt_shop_no or "").strip(),
    )
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


def update_order(db: Session, order_id: int, distributor_id: int, lines: list[dict], remark: str = "") -> dict:
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
def _build_sales_trade(order: FxOrder, distributor: FxDistributor, shop_no: str) -> dict:
    """把分销订单组装成旺店通销售单报文。

    字段口径按旺店通 openapi2 sales_trade_push.php；未配置的写空字符串，
    实际字段以联调返回为准（推送框架先行，密钥配置后一次实测即可校准）。
    """
    trade: dict = {
        "shop_no": shop_no,
        "tid": order.order_no,  # 原始单号用分销订单号，便于两边互查
        "trade_time": order.order_date.strftime("%Y-%m-%d %H:%M:%S") if order.order_date else "",
        "pay_status": 2,  # 已付款
        "delivery_term": 1,  # 现款现货
        "buyer_message": order.remark or "",
        "receiver_name": distributor.name,
        "receiver_mobile": distributor.phone or "",
        # 收货地址历史上取分销商「区域」。2026-10-28 起区域不再录入（旧值仍留在库里），
        # 新分销商这里会是空串，推送后由旺店通侧补齐；不为删字段而丢掉老订单的地址。
        "receiver_address": distributor.region or "",
        "goods_list": [
            {
                "spec_no": line.goods_no,  # 明细快照里的编号即商品/规格编码
                "num": _plain(Decimal(line.quantity or 0)),
                "price": str(_money(line.unit_price)),
            }
            for line in (order.lines or [])
        ],
    }
    if settings.wdt_warehouse_no:
        trade["warehouse_no"] = settings.wdt_warehouse_no
    return trade


def push_order_to_wangdian(db: Session, order_id: int) -> dict:
    """把已审核的分销订单推送到旺店通（销售单）。

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
    order.push_status = "sent"
    order.push_message = message[:480]
    order.pushed_at = datetime.now()
    db.commit()
    return {"ok": True, "order": _order_view(order)}


def create_and_push_order(
    db: Session, distributor_id: int, lines: list[dict], remark: str = "", wdt_shop_no: str = ""
) -> dict:
    """下单页一键流程：创建订单 → 自动审核通过 → 推送旺店通。

    推送失败不回滚订单（单据已落库、状态为已审核），把报错带给前端，
    用户在「分销订单」页修正后可重推。
    """
    view = create_order(db, distributor_id, lines, remark, wdt_shop_no=wdt_shop_no)
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
