"""一级分销价：设定、毛利计算、批量改价、含税口径。

背景：商品原来只有一个价格（品牌原价，含税）。现在要区分两个价：
- base_price    品牌给我们的单价（含税），成本基准；
- tier1_price   我们卖给一级分销商的单价（含税），人工设定。

固定口径：两个价都是含税，毛利 = tier1_price - base_price，
**不做 ÷1.06 / ×1.06 换算**（税率不落库，避免历史数据被后来的税率带偏）。
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.services.distribution import (
    _tier1_margin,
    create_product,
    list_products,
    set_tier1_prices,
    update_product,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _make(db, sku="T1", base="100.00", tier1=None):
    data = {"sku": sku, "name": "测试商品 " + sku, "base_price": base}
    if tier1 is not None:
        data["tier1_price"] = tier1
    return create_product(db, data)


# ---------------------------------------------------------------- 毛利计算
@pytest.mark.parametrize(
    "base,tier1,expect_margin,expect_rate,expect_below",
    [
        ("100.00", "120.00", "20.00", "16.7", False),   # 常规加价
        ("100.00", "100.00", "0.00", "0.0", False),     # 平进平出，不算亏
        ("100.00", "88.00", "-12.00", "-13.6", True),   # 卖价低于品牌价，标红
        ("0", "88.00", "", "", False),                  # 品牌价没维护，不给毛利
        ("100.00", "0", "", "", False),                  # 还没定价，不给毛利
    ],
)
def test_margin(base, tier1, expect_margin, expect_rate, expect_below):
    result = _tier1_margin(Decimal(base), Decimal(tier1))
    assert result["tier1_margin"] == expect_margin
    assert result["tier1_rate"] == expect_rate
    assert result["tier1_below_cost"] is expect_below


def test_margin_is_tax_inclusive_subtraction():
    """两个价都是含税价，直接相减；不允许出现 ÷1.06 之类的换算结果。"""
    result = _tier1_margin(Decimal("106.00"), Decimal("116.60"))
    assert result["tier1_margin"] == "10.60"
    # 若误做未税换算，毛利会变成 10.00 左右，这里就是防回归
    assert result["tier1_margin"] != "10.00"


# ---------------------------------------------------------------- 落库与视图
def test_create_with_tier1_price(db):
    product = _make(db, sku="A1", base="100", tier1="125.5")
    assert product["base_price"] == "100.00"
    assert product["tier1_price"] == "125.50"
    assert product["tier1_margin"] == "25.50"
    assert product["tier1_rate"] == "20.3"


def test_create_without_tier1_defaults_to_zero(db):
    product = _make(db, sku="A2", base="100")
    assert product["tier1_price"] == "0.00"
    assert product["tier1_margin"] == ""


def test_create_rejects_negative_tier1(db):
    with pytest.raises(ValueError):
        _make(db, sku="A3", base="100", tier1="-1")


def test_update_can_change_tier1(db):
    product = _make(db, sku="B1", base="100", tier1="120")
    updated = update_product(db, product["id"], {"tier1_price": "138.00"})
    assert updated["tier1_price"] == "138.00"
    assert updated["tier1_margin"] == "38.00"


def test_update_without_tier1_keeps_existing(db):
    """其它字段编辑（改名字）不能把已定好的一级分销价清掉。"""
    product = _make(db, sku="B2", base="100", tier1="120")
    updated = update_product(db, product["id"], {"name": "改了名字"})
    assert updated["tier1_price"] == "120.00"


# ---------------------------------------------------------------- 批量改价
def test_set_tier1_prices_bulk(db):
    a = _make(db, sku="C1", base="100")
    b = _make(db, sku="C2", base="50")
    result = set_tier1_prices(db, [
        {"product_id": a["id"], "tier1_price": "110"},
        {"product_id": b["id"], "tier1_price": "58.80"},
    ])
    assert result["updated"] == 2 and not result["errors"]
    rows = {p["sku"]: p for p in list_products(db)}
    assert rows["C1"]["tier1_price"] == "110.00"
    assert rows["C2"]["tier1_price"] == "58.80"


def test_set_tier1_prices_bad_rows_do_not_block_others(db):
    a = _make(db, sku="D1", base="100")
    result = set_tier1_prices(db, [
        {"product_id": a["id"], "tier1_price": "99"},
        {"product_id": a["id"], "tier1_price": "-5"},
        {"product_id": 999999, "tier1_price": "10"},
    ])
    assert result["updated"] == 1
    assert len(result["errors"]) == 2
    assert "不能为负数" in result["errors"][0]["message"]
    assert "不存在" in result["errors"][1]["message"]


def test_set_tier1_prices_empty_clears_price(db):
    a = _make(db, sku="E1", base="100", tier1="120")
    set_tier1_prices(db, [{"product_id": a["id"], "tier1_price": ""}])
    rows = {p["sku"]: p for p in list_products(db)}
    assert rows["E1"]["tier1_price"] == "0.00"
