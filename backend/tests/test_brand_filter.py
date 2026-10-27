"""商品品牌维度：品牌聚合、按品牌过滤、手工建品带品牌。

背景：顶部品牌切换器依赖 list_brands()，商品档案的品牌筛选依赖 list_products(brand=)。
两者都只应统计「在售」商品——停用商品不该在切换器里占入口。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxProduct
from app.services.distribution import create_product, list_brands, list_products


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _add(db, sku, name, brand, active=1):
    row = FxProduct(sku=sku, name=name, brand=brand, active=active)
    db.add(row)
    db.commit()
    return row


def test_list_brands_aggregates_and_orders_by_count(db):
    """商品数多的品牌排前面，切换器才不会把主力品牌埋在后面。"""
    _add(db, "S1", "商品1", "格力高/Glico")
    _add(db, "S2", "商品2", "格力高/Glico")
    _add(db, "S3", "商品3", "雀巢/Nestle")

    brands = list_brands(db)
    assert [(b["brand"], b["count"]) for b in brands] == [
        ("格力高/Glico", 2),
        ("雀巢/Nestle", 1),
    ]


def test_list_brands_skips_empty_brand_and_inactive_products(db):
    """空品牌和停用商品都不进切换器，否则会出现空壳入口和虚高数量。"""
    _add(db, "S1", "有品牌", "雀巢/Nestle")
    _add(db, "S2", "无品牌", "")
    _add(db, "S3", "停用商品", "雀巢/Nestle", active=0)

    assert list_brands(db) == [{"brand": "雀巢/Nestle", "count": 1}]


def test_list_products_filters_by_brand(db):
    _add(db, "S1", "格力高饼干", "格力高/Glico")
    _add(db, "S2", "雀巢咖啡", "雀巢/Nestle")

    assert len(list_products(db)) == 2
    only = list_products(db, brand="格力高/Glico")
    assert [p["sku"] for p in only] == ["S1"]
    assert list_products(db, brand="不存在的品牌") == []


def test_list_products_without_brand_returns_all(db):
    """前端不传 brand 时必须返回全部，否则「全部品牌」会变空。"""
    _add(db, "S1", "A", "格力高/Glico")
    _add(db, "S2", "B", "雀巢/Nestle")
    assert len(list_products(db, brand="")) == 2


def test_create_product_keeps_brand_and_category(db):
    """手工建品要能带品牌，否则手工商品永远进不了品牌维度。"""
    view = create_product(
        db, {"sku": "NEW1", "name": "手工新品", "brand": "自建品牌", "category": "零食"}
    )
    assert view["brand"] == "自建品牌"
    assert view["category"] == "零食"
    assert list_brands(db) == [{"brand": "自建品牌", "count": 1}]


def test_create_product_without_brand_is_allowed(db):
    """不填品牌不应报错，只是不进品牌切换器。"""
    view = create_product(db, {"sku": "NEW2", "name": "无品牌商品"})
    assert view["brand"] == ""
    assert list_brands(db) == []
