"""商品「热销」标记：批量开关、默认排序、下单页只取在售热销。

业务口径（2026-10-28 用户明确）
-----------------------------
1. 商品档案状态要能看出「热销」，勾选商品后可批量标记/取消；
2. 商品档案默认排序热销在前，其余仍按 id 倒序；
3. 下单页只显示「在售 + 热销」的商品，停用的热销商品不能下单；
4. 热销与在售是两个维度，取消热销不得动 active，停用也不得清掉热销标记。
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxProduct
from app.services.distribution import list_products, set_hot_products


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _product(sku: str, active: bool = True, is_hot: bool = False) -> FxProduct:
    return FxProduct(
        sku=sku,
        name=f"商品-{sku}",
        spec="默认规格",
        base_price=Decimal("10"),
        active=1 if active else 0,
        is_hot=1 if is_hot else 0,
    )


def test_hot_products_sort_first_then_id_desc(db):
    """默认排序：热销优先；同组内 id 倒序（新商品在前）。"""
    p1 = _product("SKU-1", is_hot=False)
    p2 = _product("SKU-2", is_hot=True)
    p3 = _product("SKU-3", is_hot=False)
    p4 = _product("SKU-4", is_hot=True)
    db.add_all([p1, p2, p3, p4])
    db.commit()

    rows = list_products(db)
    assert [r["id"] for r in rows] == [p4.id, p2.id, p3.id, p1.id]
    assert [r["is_hot"] for r in rows] == [True, True, False, False]


def test_hot_only_returns_hot_rows_even_if_inactive(db):
    """hot_only 是数据过滤，不改写状态；停用的热销商品仍会返回，由页面再按 active 过滤。"""
    active_hot = _product("SKU-AH", is_hot=True)
    inactive_hot = _product("SKU-IH", active=False, is_hot=True)
    cold = _product("SKU-C", is_hot=False)
    db.add_all([active_hot, inactive_hot, cold])
    db.commit()

    rows = list_products(db, hot_only=True)
    assert [r["id"] for r in rows] == [inactive_hot.id, active_hot.id]
    assert all(r["is_hot"] for r in rows)


def test_set_hot_products_batch_toggle_keeps_active(db):
    """批量标记/取消只动 is_hot，active、价格、分类都不动。"""
    p1 = _product("SKU-1", is_hot=False)
    p2 = _product("SKU-2", active=False, is_hot=False)
    p3 = _product("SKU-3", is_hot=False)
    db.add_all([p1, p2, p3])
    db.commit()

    out = set_hot_products(db, [p1.id, p2.id, 999999], True)
    assert out["updated"] == 2
    assert out["not_found"] == [999999]
    db.refresh(p1)
    db.refresh(p2)
    db.refresh(p3)
    assert p1.is_hot == 1
    assert p2.is_hot == 1
    assert p3.is_hot == 0
    assert p1.active == 1
    assert p2.active == 0  # 停用状态不被热销标记覆盖

    out2 = set_hot_products(db, [p1.id], False)
    assert out2["updated"] == 1
    db.refresh(p1)
    assert p1.is_hot == 0
    assert p1.active == 1


def test_set_hot_products_rejects_empty_selection(db):
    with pytest.raises(ValueError, match="没有勾选商品"):
        set_hot_products(db, [], True)
