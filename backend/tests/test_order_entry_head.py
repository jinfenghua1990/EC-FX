"""下单页订单头字段（渠道 / 仓库 / 配送方式 / 刮码 / 发货要求 / 下单时间）。

业务口径（2026-10-28 用户明确）
------------------------------
用户要的是「像旺店通那样」的下单界面：下单时间、下单经销商、下单渠道、发货仓库、
发货要求、自提还是送货、特殊情况（刮码）都要在**订单维度**留档，不能只写在备注里。
"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxDistributor, FxProduct
from app.services.distribution import (
    create_order,
    update_order_fulfillment,
)


@pytest.fixture()
def seeded():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    d = FxDistributor(code="D001", name="分销商一", active=True)
    db.add(d)
    db.flush()
    p = FxProduct(sku="SKU-1", name="商品一", spec="盒", base_price=Decimal("10"), pack_qty=6)
    db.add(p)
    db.commit()
    yield db, d, p
    db.close()


def test_create_order_stores_order_head(seeded):
    """订单头 5 个字段 + 自定义下单时间都要原样落库。"""
    db, d, p = seeded
    view = create_order(
        db, d.id, [{"product_id": p.id, "quantity": 12}],
        remark="急单",
        order_channel="团购",
        ship_warehouse="磐石仓",
        ship_note="两箱一捆 随货送授权书",
        delivery_type="pickup",
        scratch_code=1,
        order_date=datetime(2026, 10, 27, 9, 30),
    )
    assert view["order_channel"] == "团购"
    assert view["ship_warehouse"] == "磐石仓"
    assert view["ship_note"] == "两箱一捆 随货送授权书"
    assert view["delivery_type"] == "pickup"
    assert view["delivery_type_label"] == "自提"
    assert view["scratch_code"] == 1
    assert view["order_date"] == "2026-10-27 09:30"
    assert view["total_amount"] == "120.00"


def test_create_order_defaults_are_empty(seeded):
    """不传订单头时留空，不编造默认渠道/仓库。"""
    db, d, p = seeded
    view = create_order(db, d.id, [{"product_id": p.id, "quantity": 6}])
    assert view["order_channel"] == ""
    assert view["ship_warehouse"] == ""
    assert view["ship_note"] == ""
    assert view["delivery_type"] == ""
    assert view["delivery_type_label"] == "未指定"
    assert view["scratch_code"] == 0


def test_create_order_rejects_unknown_delivery_type(seeded):
    """配送方式只允许 自提 / 送货，别的值直接拒，避免脏数据混进订单头。"""
    db, d, p = seeded
    with pytest.raises(ValueError, match="配送方式"):
        create_order(db, d.id, [{"product_id": p.id, "quantity": 1}], delivery_type="airdrop")


def test_update_fulfillment_patches_order_head(seeded):
    """录漏了能补：fulfillment 接口只改传进来的字段。"""
    db, d, p = seeded
    view = create_order(db, d.id, [{"product_id": p.id, "quantity": 6}], order_channel="线下")
    order_id = view["id"]

    out = update_order_fulfillment(db, order_id, {
        "ship_warehouse": "磐石仓B",
        "delivery_type": "delivery",
        "scratch_code": 1,
    })
    assert out["order_channel"] == "线下"          # 没传的不动
    assert out["ship_warehouse"] == "磐石仓B"
    assert out["delivery_type"] == "delivery"
    assert out["delivery_type_label"] == "送货"
    assert out["scratch_code"] == 1

    # 空串表示「清空」，和「没传」要区分开
    out2 = update_order_fulfillment(db, order_id, {"ship_warehouse": "", "scratch_code": 0})
    assert out2["ship_warehouse"] == ""
    assert out2["scratch_code"] == 0
    assert out2["order_channel"] == "线下"
