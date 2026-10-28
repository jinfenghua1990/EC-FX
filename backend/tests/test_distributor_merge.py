"""分销商「付款账户」pay_accounts + 合并分销商 merge_distributors。

用户 2026-10-28 场景：同一个客户，钱可能从不同公司/账户打过来
（自己付、关联公司代付、个人卡付）。两条能力：
1. 客户档案可录多个付款账户（逗号/顿号/中文逗号分隔，去重去空）；
2. 把拆开的几行客户并成一行：订单、经销商专属价移到保留客户名下，
   被并行的名称+付款账户汇进保留客户的付款账户列表，然后删除被并行。

这里锁死五条底线：
1. pay_accounts 收数组或逗号串都行，去重去空；
2. 合并后订单全部改挂目标客户（数量不变、金额不变）；
3. 专属价：目标没有同商品的价就搬，有就丢弃（保留目标价）；
4. 被并行的名称和付款账户进入目标的付款账户，不丢对账线索；
5. 合并不能把目标并进自己；不存在的 id 报错。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxDistributor, FxOrder, FxOrderLine, FxPrice, FxProduct
from app.services import distribution as dist


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _mk_product(db, sku, price="10.00"):
    return dist.create_product(db, {
        "sku": sku, "name": f"商品{sku}", "brand": "格力高", "category": "零食",
        "unit": "盒", "pack_size": 1, "list_price": price,
    })["id"]


def test_pay_accounts_roundtrip(db):
    """付款账户：数组、逗号串、顿号/中文逗号都能收，去重去空。"""
    d = dist.create_distributor(db, {
        "name": "优华", "pay_accounts": ["上海优华贸易有限公司", "钟军", "钟军"],
    })
    assert d["pay_accounts"] == ["上海优华贸易有限公司", "钟军"]

    d2 = dist.update_distributor(db, d["id"], {"pay_accounts": "甲公司、乙公司，丙公司"})
    assert d2["pay_accounts"] == ["甲公司", "乙公司", "丙公司"]

    d3 = dist.update_distributor(db, d["id"], {"pay_accounts": []})
    assert d3["pay_accounts"] == []


def test_merge_moves_orders_and_prices(db):
    """合并：订单/专属价移到目标；付款账户汇入；被并行删除。"""
    target_id = dist.create_distributor(db, {"name": "上海优华贸易有限公司", "pay_accounts": ["上海优华贸易有限公司"]})["id"]
    src_id = dist.create_distributor(db, {"name": "钟军", "pay_accounts": ["钟军个人卡"]})["id"]
    other_id = dist.create_distributor(db, {"name": "无关客户"})["id"]

    p1 = _mk_product(db, "SKU-A", "100.00")
    p2 = _mk_product(db, "SKU-B", "50.00")

    # 目标和被并行各有一笔订单
    o1 = dist.create_order(db, target_id, [{"product_id": p1, "quantity": 10}])
    o2 = dist.create_order(db, src_id, [{"product_id": p2, "quantity": 4}])
    o3 = dist.create_order(db, other_id, [{"product_id": p1, "quantity": 1}])

    # 专属价：SKU-A 双方都有（应保留目标的价）；SKU-B 只有被并行有（应搬过去）
    dist.set_price(db, target_id, p1, Decimal("88.00"))
    dist.set_price(db, src_id, p1, Decimal("66.00"))
    dist.set_price(db, src_id, p2, Decimal("40.00"))

    result = dist.merge_distributors(db, target_id, [src_id])

    assert result["moved_orders"] == 1
    assert result["moved_prices"] == 1
    assert result["dropped_prices"] == 1

    orders = {o.id: o for o in db.scalars(select(FxOrder)).all()}
    assert len(orders) == 3  # 订单总数不变
    assert orders[o2["id"]].distributor_id == target_id
    assert orders[o1["id"]].distributor_id == target_id
    assert orders[o3["id"]].distributor_id == other_id  # 无关客户不受影响

    # 专属价：目标 SKU-A 还是 88，SKU-B 搬来 40；被并行的 66 消失
    prices = {(p.distributor_id, p.product_id): p.price for p in db.scalars(select(FxPrice)).all()}
    assert prices[(target_id, p1)] == Decimal("88.00")
    assert prices[(target_id, p2)] == Decimal("40.00")

    # 付款账户汇入：目标名 + 被并行名 + 被并行账户
    view = dist._distributor_view(db.get(FxDistributor, target_id))
    assert "钟军" in view["pay_accounts"]
    assert "钟军个人卡" in view["pay_accounts"]
    assert "上海优华贸易有限公司" in view["pay_accounts"]

    # 被并行已删除
    assert db.get(FxDistributor, src_id) is None
    # 目标的备注留了合并痕迹
    assert "钟军" in (view["note"] or "")


def test_merge_rejects_bad_input(db):
    a = dist.create_distributor(db, {"name": "甲"})["id"]
    b = dist.create_distributor(db, {"name": "乙"})["id"]
    with pytest.raises(ValueError):
        dist.merge_distributors(db, a, [a])  # 并进自己
    with pytest.raises(ValueError):
        dist.merge_distributors(db, a, [999])  # 不存在
    with pytest.raises(ValueError):
        dist.merge_distributors(db, a, [])  # 空列表
    # 正常路径不受影响
    dist.merge_distributors(db, a, [b])
    assert db.get(FxDistributor, b) is None


def test_merge_then_order_lines_intact(db):
    """合并后订单明细行跟着走，商品匹配不受影响。"""
    target_id = dist.create_distributor(db, {"name": "威太"})["id"]
    src_id = dist.create_distributor(db, {"name": "威太-周老板"})["id"]
    p = _mk_product(db, "SKU-C", "134.00")
    o = dist.create_order(db, src_id, [{"product_id": p, "quantity": 400}])
    dist.merge_distributors(db, target_id, [src_id])
    lines = db.scalars(select(FxOrderLine).where(FxOrderLine.order_id == o["id"])).all()
    assert len(lines) == 1
    assert lines[0].quantity == 400
    assert lines[0].product_id == p
