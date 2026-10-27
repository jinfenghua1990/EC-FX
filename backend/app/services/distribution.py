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

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import settings
from ..models import FxDistributor, FxOrder, FxOrderLine, FxPrice, FxProduct
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


# ---------------------------------------------------------------- 分销商
def list_distributors(db: Session, include_inactive: bool = True) -> list[dict]:
    stmt = select(FxDistributor).order_by(FxDistributor.id.desc())
    if not include_inactive:
        stmt = stmt.where(FxDistributor.active == 1)
    rows = db.scalars(stmt).all()
    return [_distributor_view(r) for r in rows]


def _distributor_view(row: FxDistributor) -> dict:
    return {
        "id": row.id,
        "code": row.code,
        "name": row.name,
        "contact": row.contact,
        "phone": row.phone,
        "email": row.email,
        "region": row.region,
        "note": row.note,
        "active": bool(row.active),
        "created_at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
    }


def create_distributor(db: Session, data: dict) -> dict:
    code = (data.get("code") or "").strip()
    name = (data.get("name") or "").strip()
    if not code or not name:
        raise ValueError("分销商编码和名称必填")
    exists = db.scalar(select(FxDistributor).where(FxDistributor.code == code))
    if exists:
        raise ValueError(f"分销商编码已存在: {code}")
    row = FxDistributor(
        code=code,
        name=name,
        contact=(data.get("contact") or "").strip(),
        phone=(data.get("phone") or "").strip(),
        email=(data.get("email") or "").strip(),
        region=(data.get("region") or "").strip(),
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
    for field in ("name", "contact", "phone", "email", "region", "note"):
        if field in data and data[field] is not None:
            setattr(row, field, str(data[field]).strip())
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
    return {
        "id": row.id,
        "sku": row.sku,
        "name": row.name,
        "spec": row.spec,
        "unit": row.unit,
        "base_price": str(_money(row.base_price)),
        "note": row.note,
        "active": bool(row.active),
        # 旺店通商品档案字段
        "spec_no": row.spec_no or "",
        "goods_no": row.goods_no or "",
        "barcode": row.barcode or "",
        "category": row.category or "",
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
        base_price=base_price,
        note=(data.get("note") or "").strip(),
        # 手工建品也能指定品牌/分类：否则手工商品品牌为空，永远进不了顶部品牌维度
        brand=(data.get("brand") or "").strip(),
        category=(data.get("category") or "").strip(),
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
    for field in ("name", "spec", "unit", "note", "brand", "category"):
        if field in data and data[field] is not None:
            setattr(row, field, str(data[field]).strip())
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


# ---------------------------------------------------------------- 分销订单
def _next_order_no(db: Session) -> str:
    prefix = datetime.now().strftime("FX%Y%m%d")
    count = db.scalar(select(func.count()).select_from(FxOrder).where(FxOrder.order_no.like(f"{prefix}-%"))) or 0
    return f"{prefix}-{count + 1:04d}"


def price_map(db: Session, distributor_id: int) -> dict[int, Decimal]:
    """某分销商的商品专属价：{product_id: price}。下单缺省价、价格页都靠它。"""
    if not distributor_id:
        return {}
    rows = db.scalars(select(FxPrice).where(FxPrice.distributor_id == int(distributor_id))).all()
    return {row.product_id: _money(row.price) for row in rows}


def _validate_lines(db: Session, lines: list[dict], distributor_id: int | None = None) -> tuple[list[dict], Decimal]:
    """校验订单明细并返回快照行；金额在服务端统一计算。

    单价留空时取值顺序：经销商专属价 > 商品基础价。
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
            unit_price = overrides.get(product.id, product.base_price)
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


def _order_view(row: FxOrder, with_lines: bool = True) -> dict:
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
        "push_status": row.push_status or "",
        "push_message": row.push_message or "",
        "pushed_at": row.pushed_at.strftime("%Y-%m-%d %H:%M:%S") if row.pushed_at else "",
        "line_count": len(row.lines) if row.lines else 0,
    }
    if with_lines:
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
            }
            for line in (row.lines or [])
        ]
    return data


def create_order(db: Session, distributor_id: int, lines: list[dict], remark: str = "") -> dict:
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


def list_orders(db: Session, status: str = "", distributor_id: int | None = None, limit: int = 200) -> list[dict]:
    stmt = (
        db.query(FxOrder)
        .options(selectinload(FxOrder.lines))
        .order_by(FxOrder.id.desc())
    )
    if status:
        if status not in FX_ORDER_STATUSES:
            raise ValueError(f"未知订单状态: {status}")
        stmt = stmt.filter(FxOrder.status == status)
    if distributor_id:
        stmt = stmt.filter(FxOrder.distributor_id == int(distributor_id))
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
def _build_sales_trade(order: FxOrder, distributor: FxDistributor) -> dict:
    """把分销订单组装成旺店通销售单报文。

    字段口径按旺店通 openapi2 sales_trade_push.php；未配置的写空字符串，
    实际字段以联调返回为准（推送框架先行，密钥配置后一次实测即可校准）。
    """
    trade: dict = {
        "shop_no": settings.wdt_shop_no,
        "tid": order.order_no,  # 原始单号用分销订单号，便于两边互查
        "trade_time": order.order_date.strftime("%Y-%m-%d %H:%M:%S") if order.order_date else "",
        "pay_status": 2,  # 已付款
        "delivery_term": 1,  # 现款现货
        "buyer_message": order.remark or "",
        "receiver_name": distributor.name,
        "receiver_mobile": distributor.phone or "",
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
    if not settings.wdt_shop_no:
        raise ValueError("旺店通店铺号未配置，请在 .env 增加 WDT_SHOP_NO=店铺编号 后重启")

    trade = _build_sales_trade(order, distributor)
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


# ---------------------------------------------------------------- 分销商对账单
def list_statements(db: Session, distributor_id: int | None = None, month: str = "") -> dict:
    """按月汇总已审核订单；指定月份时返回该月订单明细（供页面表格 / Excel 用）。"""
    stmt = (
        db.query(FxOrder)
        .options(selectinload(FxOrder.lines))
        .filter(FxOrder.status == "approved")
        .order_by(FxOrder.order_date.desc(), FxOrder.id.desc())
    )
    if distributor_id:
        stmt = stmt.filter(FxOrder.distributor_id == int(distributor_id))
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
        "orders": [_order_view(order) for order in selected],
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
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=8)
    ws.cell(row=1, column=1).font = Font(bold=True, size=13)
    ws.cell(row=1, column=1).alignment = Alignment(horizontal="center")
    ws.append([])
    ws.append(["订单号", "下单时间", "商品编号", "商品名称", "规格", "数量", "单价（元）", "小计（元）"])
    for cell in ws[3]:
        cell.font = Font(bold=True)

    for order in data["orders"]:
        for line in order.get("lines") or []:
            ws.append([
                order["order_no"],
                order["order_date"],
                line["goods_no"],
                line["goods_name"],
                line["spec_name"],
                float(Decimal(line["quantity"])),
                float(Decimal(line["unit_price"])),
                float(Decimal(line["subtotal"])),
            ])
    ws.append([])
    ws.append(["合计", "", "", "", "", "", "", float(Decimal(data["total_amount"]))])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)

    # 简单列宽，肉眼可读即可
    widths = (16, 18, 14, 24, 14, 8, 12, 12)
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + index)].width = width

    buffer = io.BytesIO()
    wb.save(buffer)
    filename = f"对账单_{dist.code}_{month}.xlsx"
    return filename, buffer.getvalue()
