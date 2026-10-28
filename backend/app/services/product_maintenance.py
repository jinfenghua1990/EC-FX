"""商品价格「待维护」清单 —— 页面强制提醒的唯一规则来源。

为什么单独一个模块：
价格缺项不是「有没有值」这么简单，不同字段的维护要求不一样，
散在前端判断必然前后不一致（一会说缺、一会说不用管）。集中在这里，
页面只负责渲染，规则只在这里改。

三条口径（来自用户 2026-10-27 的明确说明）：
1. **牌价只有格力高需要维护**，其他品牌不维护 —— 所以不能把 259 个空值全当缺项，
   否则 215 个「本来就不要求」的商品会把真正要处理的 29 个淹掉。
2. 牌价只用于算分销折扣率，不参与利润；没有它不影响成本，只是算不出折扣率。
3. 一级分销价是卖价，**每个在售商品都必须有**，没有就算不出毛利。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import FxPriceHistory, FxProduct

# 价格统一保留两位小数（牌价/一级价都是元，Excel 里也只给两位）
_CENT = Decimal("0.01")

# 需要维护牌价的品牌（按品牌名包含匹配，兼容「格力高/Glico」这类写法）。
# 用户口径：其他品牌不维护牌价。以后某品牌也要牌价，往这里加即可。
LIST_PRICE_BRANDS: tuple[str, ...] = ("格力高",)

# 牌价维护品牌的拿货折扣（采购价 ÷ 牌价）。实测格力高 78 个商品 78 个都精确等于 0.80。
# 用途只有一个：缺牌价但有采购价时，页面给个「约等于多少」的建议值，供用户核对后填。
# 建议值不是数据，绝对不自动写库 —— 折扣会变，写进去就变成假数据了。
LIST_PRICE_DISCOUNT = Decimal("0.8")

# 拿货折扣按品牌区分（2026-10-27 用户明确口径）：
# - 格力高：采购价 = 牌价 × 0.8（8 折拿货，88 个商品实测全部命中）
# - 瑞士莲/桂格等其他品牌：**采购价本身就是品牌标价**，即采购价 = 牌价 × 1.0
# 品牌名按「包含」匹配（兼容「格力高/Glico」写法）；没列到的品牌一律 1.0。
BRAND_PURCHASE_DISCOUNT: tuple[tuple[str, Decimal], ...] = (("格力高", Decimal("0.8")),)
DEFAULT_PURCHASE_DISCOUNT = Decimal("1")


def purchase_discount_of(brand: str) -> Decimal:
    """这个品牌的拿货折扣：采购价 = 牌价 × 折扣。回填牌价与页面 8 折校验共用这一处。"""
    b = (brand or "").strip()
    for key, discount in BRAND_PURCHASE_DISCOUNT:
        if key in b:
            return discount
    return DEFAULT_PURCHASE_DISCOUNT

# 非售卖品：没有牌价天经地义，不能算「待维护」，否则页面永远报缺。
#
# 踩过的坑：一开始把分类「无」「其他」也当非售卖品，结果误杀了一大片正常商品
# —— 三养火鸡面 20 个、格力高粒粒百奇/星奇饼干、瑞士莲 688g、桂格麦片、乐多飞益生菌
# 全被归进「不用定价」，分销价提醒直接少报 76 个。分类「无」只是旺店通没填，
# 不代表不是商品。所以只认**确定的**非售卖分类，其余一律当正常商品（宁可多提醒）。
NON_MERCH_CATEGORIES: frozenset[str] = frozenset({"耗材", "仓库耗材", "包材", "赠品"})
NON_MERCH_KEYWORDS: tuple[str, ...] = (
    "耗材", "纸箱", "帆布", "好评卡", "赠品", "手提袋", "礼品袋", "礼袋", "样品",
    # 买赠周边：这些在旺店通里常挂在「其他」分类下，只能靠品名认
    "水杯", "保温杯", "毛毯", "纸巾", "抽盒", "刨冰机", "冰块",
)


def is_merchandise(product: FxProduct) -> bool:
    """是否是需要定价的正常商品（排除纸箱、帆布袋、好评卡这类非售卖品）。"""
    category = (product.category or "").strip()
    if category in NON_MERCH_CATEGORIES:
        return False
    name = product.name or ""
    return not any(kw in name for kw in NON_MERCH_KEYWORDS)


def list_price_required(product: FxProduct) -> bool:
    """这个商品是否要求维护牌价：属于牌价维护品牌 + 是正常商品。"""
    brand = product.brand or ""
    if not any(b in brand for b in LIST_PRICE_BRANDS):
        return False
    return is_merchandise(product)


def maintenance_of(product: FxProduct) -> dict:
    """算一个商品的缺项。

    返回：
      need_list_price / need_tier1 / need_cost_check  三个布尔
      missing       缺项代码列表，如 ["list_price", "tier1_price"]
      missing_count 缺项个数（页面排序、角标用）
      missing_text  给页面直接显示的短语，如「缺牌价、缺分销价」

    注意 tier1_price 只对**在售**商品要求 —— 停用商品不卖，不必逼着定价。
    """
    list_price = Decimal(str(product.list_price or 0))
    tier1_price = Decimal(str(product.tier1_price or 0))

    need_list = list_price_required(product) and list_price <= 0
    need_tier1 = bool(product.active) and is_merchandise(product) and tier1_price <= 0
    need_cost = (product.cost_status or "") == "pending"

    missing: list[str] = []
    labels: list[str] = []
    if need_list:
        missing.append("list_price")
        labels.append("缺牌价")
    if need_tier1:
        missing.append("tier1_price")
        labels.append("缺分销价")
    if need_cost:
        missing.append("cost_check")
        labels.append("成本待核对")

    return {
        "need_list_price": need_list,
        "need_tier1_price": need_tier1,
        "need_cost_check": need_cost,
        "missing": missing,
        "missing_count": len(missing),
        "missing_text": "、".join(labels),
    }


def maintenance_summary(products: list[FxProduct]) -> dict:
    """汇总在售商品的缺项，给页面顶部横幅用。

    停用商品不参与维护提醒：它们不卖，页面也不展示；牌价仍留在明细里备查，
    但不应该把顶部横幅一直卡成“缺标价”。

    分「必须处理」和「无法自动算」两层：
    - 必须处理：缺牌价（仅格力高正常商品）/ 缺分销价 / 成本待核对
    - 无法自动算：有牌价但没采购价 —— 成本估不准，页面提示但不算缺项
    """
    no_list = no_tier1 = pending = 0
    list_with_purchase = 0  # 缺牌价但已有采购价的，可按 8 折反推
    active_products = sum(1 for product in products if product.active)
    for product in products:
        if not product.active:
            continue
        info = maintenance_of(product)
        if info["need_list_price"]:
            no_list += 1
            if Decimal(str(product.purchase_price or 0)) > 0:
                list_with_purchase += 1
        if info["need_tier1_price"]:
            no_tier1 += 1
        if info["need_cost_check"]:
            pending += 1

    total = len(products)
    blocking = no_list + no_tier1 + pending
    return {
        "total_products": total,
        "active_products": active_products,
        "no_list_price": no_list,
        "no_list_price_with_cost": list_with_purchase,
        "no_tier1_price": no_tier1,
        "cost_pending": pending,
        "blocking": blocking,
        # 一层提示：全部处理完 = 页面不再显示横幅
        "clean": blocking == 0,
        "list_price_brands": list(LIST_PRICE_BRANDS),
        "list_price_discount": str(LIST_PRICE_DISCOUNT),
    }


def summary_from_db(db: Session, brand: str = "", include_inactive: bool = True) -> dict:
    """按当前筛选范围算汇总，并附上「全部品牌」的总数。

    为什么要多算一次全局：顶栏切到某个品牌后，横幅如果只报这个品牌的缺项，
    用户会以为其他品牌都维护好了。所以范围数字用来看当前页，
    all_blocking 用来提示全局还剩多少。
    """
    def _query(apply_brand: str):
        stmt = select(FxProduct)
        if not include_inactive:
            stmt = stmt.where(FxProduct.active == 1)
        if apply_brand:
            stmt = stmt.where(FxProduct.brand == apply_brand)
        return list(db.scalars(stmt).all())

    scoped = maintenance_summary(_query(brand))
    info = dict(scoped)
    info["brand"] = brand
    if brand:
        info["all_blocking"] = maintenance_summary(_query(""))["blocking"]
    else:
        info["all_blocking"] = info["blocking"]
    return info


# ---------------------------------------------------------------- 牌价回填（采购价 ÷ 拿货折扣）
def _norm_purchase_discount(value) -> Decimal:
    """拿货折扣校验：0~1 的小数（0.8 = 8 折拿货）。"""
    try:
        d = Decimal(str(value).strip())
    except Exception as exc:
        raise ValueError(f"拿货折扣格式不正确: {value}") from exc
    if d <= 0 or d > 1:
        raise ValueError("拿货折扣应为 0~1 的小数，如 0.8 表示 8 折拿货")
    return d


def backfill_list_prices(
    db: Session,
    brand: str = "",
    discount: str = "",
    dry_run: bool = True,
) -> dict:
    """把「牌价为空但有采购价」的商品反推牌价，折扣**按品牌**取：

    - 格力高：牌价 = 采购价 ÷ 0.8（8 折拿货，2026-10-27 实测 88/88 命中）；
    - 瑞士莲/桂格等其他品牌：**采购价就是品牌标价**，牌价 = 采购价（折扣 1.0）；
    - discount 传非空值可强制覆盖所有品牌（校验 0~1），留空 = 按品牌自动。

    安全边界：
    - 只补空值（list_price<=0），绝不覆盖已维护的牌价；
    - 排除耗材/赠品等非售卖品（它们不需要牌价，填了只会污染页面）；
    - 默认 dry_run 只预览不写库，前端确认后才真正写入。
    """
    override = _norm_purchase_discount(discount) if str(discount or "").strip() else None

    stmt = (
        select(FxProduct)
        .where(FxProduct.list_price <= 0)
        .where(FxProduct.purchase_price > 0)
        .order_by(FxProduct.brand, FxProduct.id)
    )
    if brand:
        # 模糊匹配：页面传「格力高」要能命中「格力高/Glico」（与 set_cost_status 同口径）
        stmt = stmt.where(FxProduct.brand.like(f"%{brand.strip()}%"))
    products = [p for p in db.scalars(stmt).all() if is_merchandise(p)]

    rows: list[dict] = []

    def _calc(p: FxProduct) -> tuple[Decimal, Decimal]:
        disc = override if override is not None else purchase_discount_of(p.brand)
        purchase = Decimal(str(p.purchase_price))
        return disc, (purchase / disc).quantize(_CENT, rounding=ROUND_HALF_UP)

    for p in products:
        disc, list_price = _calc(p)
        rows.append(
            {
                "id": p.id,
                "sku": p.sku,
                "name": p.name,
                "brand": p.brand or "",
                "purchase_price": str(Decimal(str(p.purchase_price)).quantize(_CENT, rounding=ROUND_HALF_UP)),
                "purchase_date": p.purchase_date.strftime("%Y-%m-%d") if p.purchase_date else "",
                "purchase_no": p.purchase_no or "",
                "discount": str(disc),
                "list_price": str(list_price),
            }
        )

    updated = 0
    if not dry_run:
        for p in products:
            _, list_price = _calc(p)
            p.list_price = list_price
            updated += 1
        db.commit()

    return {
        "eligible": len(rows),
        "rows": rows,
        "updated": updated,
        "dry_run": dry_run,
        "discount": "auto" if override is None else str(override),
    }


# ---------------------------------------------------------------- 改价流水（底层账本）
def log_price_change(
    db: Session, product: FxProduct, old_value, new_value,
    rate="", source: str = "manual", note: str = "",
) -> None:
    """一级分销价变更留底：只在值真变化时写一行 fx_price_history。

    - old/new 传原始值即可；0 或空统一存空串（页面上就是「待定」），口径一致；
    - rate = 本次用的折扣率（归一化小数，如 0.85 = 8.5 折）；手工改价留空；
    - list_price 存改价那一刻的标价快照——事后标价再变也解释得清当时为什么是这个价；
    - 只增不改不删：对账追溯的底线，任何人都别去动这张表的历史行。
    """
    old_s = "" if not old_value or Decimal(str(old_value)) == 0 else str(old_value)
    new_s = "" if not new_value or Decimal(str(new_value)) == 0 else str(new_value)
    if old_s == new_s:
        return
    db.add(
        FxPriceHistory(
            product_id=product.id,
            sku=product.sku or "",
            old_value=old_s,
            new_value=new_s,
            rate=str(rate or ""),
            list_price=Decimal(str(product.list_price or 0)),
            source=source,
            note=(note or "").strip(),
        )
    )


# ---------------------------------------------------------------- 批量折扣率设一级分销价
def _norm_zhe(value) -> Decimal:
    """折扣率归一化：8.5（几折）或 0.85（小数）都认，统一换算成 0.85。"""
    raw = str(value if value is not None else "").strip()
    if not raw:
        raise ValueError("折扣率必填：填 8.5 表示 8.5 折")
    try:
        d = Decimal(raw)
    except Exception as exc:
        raise ValueError(f"折扣率格式不正确: {raw}") from exc
    if d <= 0 or d > 10:
        raise ValueError("折扣率应在 0~10 之间：8.5 表示 8.5 折，0.85 也表示 8.5 折")
    if d > 1:
        d = d / 10
    return d.quantize(Decimal("0.0001"))


def tier1_by_discount(db: Session, product_ids: list[int], zhe, dry_run: bool = False) -> dict:
    """勾选的商品批量按折扣率设一级分销价：一级价 = 牌价 × 折扣。

    - 没维护牌价的商品跳过（折扣率必须有牌价作基准，跳过原因回传给页面提示）；
    - 箱价顺手算好回传：箱价 = 一级价 × 箱规（一级价按盒，页面要的是每箱多少钱）；
    - dry_run=True 只返回将要写入的价格，不落库（预览用）。
    """
    rate = _norm_zhe(zhe)
    ids = [int(pid) for pid in (product_ids or []) if str(pid).strip().isdigit()]
    if not ids:
        raise ValueError("请先勾选要设置的商品")

    rows: list[dict] = []
    skipped: list[dict] = []
    for pid in ids:
        p = db.get(FxProduct, pid)
        if not p:
            skipped.append({"id": pid, "reason": "商品不存在"})
            continue
        list_price = Decimal(str(p.list_price or 0))
        if list_price <= 0:
            skipped.append({"id": pid, "sku": p.sku, "reason": "牌价为空，先「回填牌价」才能按折扣算"})
            continue
        tier1 = (list_price * rate).quantize(_CENT, rounding=ROUND_HALF_UP)
        pack_qty = int(p.pack_qty or 0)
        rows.append(
            {
                "id": pid,
                "sku": p.sku,
                "name": p.name,
                "brand": p.brand or "",
                "list_price": str(list_price),
                "zhe": str(zhe),
                "rate": str(rate),
                "tier1_price": str(tier1),
                "pack_qty": pack_qty,
                "box_price": str((tier1 * pack_qty).quantize(_CENT, rounding=ROUND_HALF_UP)) if pack_qty > 0 else "",
            }
        )

    updated = 0
    if not dry_run:
        for r in rows:
            p = db.get(FxProduct, r["id"])
            if p is None:
                continue
            old_val = p.tier1_price
            p.tier1_price = Decimal(r["tier1_price"])
            log_price_change(db, p, old_val, p.tier1_price, rate=rate, source="batch")
            updated += 1
        db.commit()

    return {
        "rate": str(rate),
        "matched": len(rows),
        "updated": updated,
        "rows": rows,
        "skipped": skipped,
        "dry_run": dry_run,
    }
