"""商品「箱规」：解析、落库、手工维护。

背景：分销报价和整箱订货要知道「1 箱 = 多少件」。旺店通把它放在货品级
aux_unit（数量）与 aux_unit_name（形如「36/箱」），同步时要提出来；
纸箱耗材这类 aux_unit=0 的商品必须记成 0（未维护），不能当成 1 箱。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxProduct
from app.services.distribution import create_product, update_product
from app.services.wdt_products import _parse_pack, normalize_products, upsert_products


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.mark.parametrize(
    "goods,expect",
    [
        # 旺店通最常见形态：aux_unit=36 + aux_unit_name="36/箱"
        ({"aux_unit": "36", "aux_unit_name": "36/箱"}, (36, "箱")),
        # 只给换算数量、没给名称
        ({"aux_unit": "12"}, (12, "箱")),
        # 只给名称，数量从「24/箱」里拆
        ({"aux_unit_name": "24/箱"}, (24, "箱")),
        # 非「数字/单位」写法：整串当单位名，数量取 aux_unit
        ({"aux_unit": "10", "aux_unit_name": "10/提"}, (10, "提")),
        # 纸箱耗材：aux_unit=0，表示没维护箱规
        ({"aux_unit": "0", "aux_unit_name": ""}, (0, "箱")),
        # 1/箱 没有信息量，按未维护处理
        ({"aux_unit": "1", "aux_unit_name": "1/箱"}, (0, "箱")),
        ({}, (0, "箱")),
    ],
)
def test_parse_pack(goods, expect):
    assert _parse_pack(goods) == expect


def test_normalize_products_carries_pack_to_every_spec():
    """箱规挂在货品上，同一货品的多个规格都要带上，否则按规格看就漏了。"""
    goods = [
        {
            "goods_no": "G001",
            "goods_name": "百奇 55G*36",
            "unit_name": "件",
            "aux_unit": "36",
            "aux_unit_name": "36/箱",
            "spec_list": [
                {"spec_no": "S1", "spec_name": "默认规格"},
                {"spec_no": "S2", "spec_name": "礼盒装"},
            ],
        }
    ]
    rows = normalize_products(goods)
    assert [(r["pack_qty"], r["pack_unit"]) for r in rows] == [(36, "箱"), (36, "箱")]


def test_upsert_writes_pack_and_keeps_manual_one_when_wdt_has_none(db):
    """旺店通有箱规时以它为准；旺店通没有时不能把手工维护的清掉。"""
    items = [
        {"sku": "S1", "spec_no": "S1", "goods_no": "G1", "name": "有箱规", "spec": "", "barcode": "",
         "category": "", "brand": "", "unit": "件", "pack_qty": 24, "pack_unit": "箱", "base_price": None},
        {"sku": "S2", "spec_no": "S2", "goods_no": "G2", "name": "无箱规", "spec": "", "barcode": "",
         "category": "", "brand": "", "unit": "件", "pack_qty": 0, "pack_unit": "箱", "base_price": None},
    ]
    upsert_products(db, items)
    assert db.get(FxProduct, 1).pack_qty == 24

    # 先手工给 S2 填箱规，再用「旺店通没有箱规」的数据同步一次
    db.get(FxProduct, 2).pack_qty = 8
    db.get(FxProduct, 2).pack_unit = "提"
    db.commit()
    upsert_products(db, items)
    row = db.get(FxProduct, 2)
    assert (row.pack_qty, row.pack_unit) == (8, "提")


def test_create_and_update_product_pack_spec(db):
    """手工建品/改品都能维护箱规，页面显示用的 pack_spec 由 qty+unit 拼出。"""
    created = create_product(db, {"sku": "M1", "name": "手工商品", "unit": "件",
                                  "pack_qty": 12, "pack_unit": "箱"})
    assert created["pack_spec"] == "12/箱"

    updated = update_product(db, created["id"], {"pack_qty": 0})
    assert updated["pack_qty"] == 0
    assert updated["pack_spec"] == ""

    # 负数/非法值统一归零，不写进库
    bad = update_product(db, created["id"], {"pack_qty": -5})
    assert bad["pack_qty"] == 0
