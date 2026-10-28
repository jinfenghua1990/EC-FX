"""分销商「分销品牌」brands + 全站品牌作用域。

用户 2026-10-28 要求：
1. 顶部切到某个品牌后，所有页面（分销商 / 下单 / 订单 / 对账）都只处理该品牌的数据；
2. 一个客户可以同时分销多个品牌 —— 在分销商档案里勾多个品牌，下单时才能看到这些品牌的货；
3. 没勾任何品牌 = 还没录授权：选了具体品牌时它不出现（绝不默认成「全品牌通吃」）。

这里锁死四条底线：
1. brands 既能收数组也能收逗号串，去重去空；
2. 选了品牌时，只有勾了该品牌的分销商出现；没勾的不出现；全部品牌时都能看到；
3. 订单 / 对账单按「下单分销商被授权的品牌」过滤（订单本身不带品牌字段）；
4. 写 brands 不会动到税号/编码等其它字段。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxDistributor, FxOrder, FxOrderLine, FxProduct
from app.services import distribution as dist


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture()
def seeded(db):
    """格力高单品牌商 / 格力高+桂格双品牌商 / 没勾品牌的商，各带一张已审核订单。"""
    a = dist.create_distributor(db, {"code": "D001", "name": "甲（只格力高）", "brands": ["格力高"]})
    b = dist.create_distributor(db, {"code": "D002", "name": "乙（格力高+桂格）", "brands": "格力高,桂格"})
    c = dist.create_distributor(db, {"code": "D003", "name": "丙（没勾品牌）"})
    prod = FxProduct(sku="G-1", name="百奇", brand="格力高", base_price=Decimal("10"))
    db.add(prod)
    db.commit()
    orders = []
    for d in (a, b, c):
        o = FxOrder(order_no="SO-" + d["code"], distributor_id=d["id"], status="approved",
                    order_date=datetime(2026, 10, 20), total_amount=Decimal("100"))
        db.add(o)
        db.commit()
        db.add(FxOrderLine(order_id=o.id, product_id=prod.id, goods_no="G-1", goods_name="百奇",
                           quantity=Decimal("1"), unit_price=Decimal("10"), subtotal=Decimal("10")))
        db.commit()
        orders.append(o)
    return {"a": a, "b": b, "c": c, "orders": orders}


def test_brands_accept_list_or_csv_and_dedupe(db):
    one = dist.create_distributor(db, {"name": "甲", "brands": ["格力高"]})
    two = dist.create_distributor(db, {"name": "乙", "brands": "格力高, 桂格 ,格力高"})
    assert one["brands"] == ["格力高"]
    assert two["brands"] == ["格力高", "桂格"]
    assert dist.brand_list_of(db.get(FxDistributor, two["id"])) == ["格力高", "桂格"]


def test_list_distributors_scoped_by_brand(seeded, db):
    got = dist.list_distributors(db, brand="格力高")
    assert [d["name"] for d in got] == ["乙（格力高+桂格）", "甲（只格力高）"]  # 按 id 倒序
    got2 = dist.list_distributors(db, brand="桂格")
    assert [d["name"] for d in got2] == ["乙（格力高+桂格）"]
    # 没勾品牌的分销商：只在「全部品牌」下可见
    assert "丙（没勾品牌）" not in [d["name"] for d in got]
    assert "丙（没勾品牌）" in [d["name"] for d in dist.list_distributors(db)]
    # 一个都没勾该品牌时不报错，返回空
    assert dist.list_distributors(db, brand="瑞士莲") == []


def test_orders_scoped_by_distributor_brands(seeded, db):
    got = dist.list_orders(db, brand="格力高")
    assert {o["order_no"] for o in got} == {"SO-D001", "SO-D002"}
    assert {o["order_no"] for o in dist.list_orders(db, brand="桂格")} == {"SO-D002"}
    assert len(dist.list_orders(db)) == 3                      # 全部品牌：三张都在
    assert dist.list_orders(db, brand="瑞士莲") == []


def test_statements_scoped_by_distributor_brands(seeded, db):
    data = dist.list_statements(db, brand="格力高")
    assert len(data["orders"]) == 2
    assert len(dist.list_statements(db, brand="桂格")["orders"]) == 1
    assert len(dist.list_statements(db)["orders"]) == 3


def test_update_brands_touches_nothing_else(seeded, db):
    before = dist.list_distributors(db, brand="格力高")
    a = next(d for d in before if d["code"] == "D001")
    got = dist.update_distributor(db, a["id"], {"brands": ["格力高", "桂格"]})
    assert got["brands"] == ["格力高", "桂格"]
    assert got["code"] == "D001"
    assert got["name"] == "甲（只格力高）"
    # 甲现在也出现在桂格下
    assert "甲（只格力高）" in [d["name"] for d in dist.list_distributors(db, brand="桂格")]
