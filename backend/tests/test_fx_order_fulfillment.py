"""分销订单标准字段：原始单号 / 收款 / 发货。

回归用户 2026-10-28 的反馈：订单页不能只有内部单号和审核状态，
必须能看到旺店通/Excel 的原始订单号、款是否收到、订单是否发货，
并且这些状态能在详情里更新。
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxDistributor, FxOrder, FxOrderLine
from app.services.distribution import order_detail, update_order_fulfillment


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture()
def seeded(db):
    dist = FxDistributor(code="T-ORDER-001", name="测试客户", contact="张三",
                         phone="13800000000", delivery_address="上海市测试路1号")
    db.add(dist)
    db.flush()
    order = FxOrder(
        order_no="FX20261028001",
        distributor_id=dist.id,
        status="approved",
        total_amount=Decimal("100.00"),
        wdt_tid="202610201430,202610201431",
        pay_company="测试付款公司",
        original_order_no="202610201430,202610201431",
        shipping_status="shipped",
    )
    order.lines = [
        FxOrderLine(
            goods_no="G001", spec_no="SPEC-1", goods_name="测试商品", spec_name="规格",
            quantity=Decimal("10"), unit_price=Decimal("10.00"), subtotal=Decimal("100.00"),
        )
    ]
    db.add(order)
    db.commit()
    return db, order


def test_order_detail_exposes_original_numbers_and_fulfillment(seeded):
    db, order = seeded
    detail = order_detail(db, order.id)
    assert detail["order_no"] == "FX20261028001"
    assert detail["original_order_no"] == "202610201430,202610201431"
    assert detail["original_order_nos"] == ["202610201430", "202610201431"]
    assert detail["payment_status"] == "unpaid"
    assert detail["payment_status_label"] == "未收到"
    assert detail["shipping_status"] == "shipped"
    assert detail["shipping_status_label"] == "已发货"
    assert detail["pay_company"] == "测试付款公司"
    assert detail["receiver_name"] == "张三"
    assert detail["receiver_address"] == "上海市测试路1号"
    assert len(detail["lines"]) == 1


def test_mark_received_fills_total_and_date(seeded):
    db, order = seeded
    detail = update_order_fulfillment(db, order.id, {"payment_status": "received"})
    assert detail["payment_status"] == "received"
    assert detail["payment_status_label"] == "已收到"
    assert Decimal(detail["paid_amount"]) == Decimal("100.00")
    assert detail["payment_date"]


def test_partial_payment_requires_positive_amount(seeded):
    db, order = seeded
    with pytest.raises(ValueError):
        update_order_fulfillment(db, order.id, {"payment_status": "partial", "paid_amount": 0})


def test_mark_shipped_and_logistics(seeded):
    db, order = seeded
    detail = update_order_fulfillment(db, order.id, {
        "shipping_status": "shipped",
        "logistics_company": "顺丰",
        "logistics_no": "SF123456",
    })
    assert detail["shipping_status"] == "shipped"
    assert detail["logistics_company"] == "顺丰"
    assert detail["logistics_no"] == "SF123456"
    assert detail["shipped_at"]


def test_mark_unshipped_clears_shipped_time(seeded):
    db, order = seeded
    update_order_fulfillment(db, order.id, {"shipping_status": "shipped"})
    detail = update_order_fulfillment(db, order.id, {"shipping_status": "unshipped"})
    assert detail["shipping_status_label"] == "未发货"
    assert detail["shipped_at"] == ""


def test_unknown_fulfillment_status_rejected(seeded):
    db, order = seeded
    with pytest.raises(ValueError):
        update_order_fulfillment(db, order.id, {"payment_status": "maybe"})
    with pytest.raises(ValueError):
        update_order_fulfillment(db, order.id, {"shipping_status": "maybe"})


def test_unpaid_cannot_keep_old_paid_amount(seeded):
    db, order = seeded
    update_order_fulfillment(db, order.id, {"payment_status": "received"})
    with pytest.raises(ValueError):
        update_order_fulfillment(db, order.id, {"payment_status": "unpaid", "paid_amount": "100"})
