"""商品价格「待维护」清单。

这组用例锁的是三条业务口径，任何一条被改坏，页面就会要么漏报（用户以为维护好了）、
要么狂报（用户被 200 多个「本来就不用管」的商品淹没）：

1. **牌价只有格力高需要维护** —— 雀巢/桂格/瑞士莲缺牌价不算缺项。
2. **非售卖品不算缺项** —— 纸箱耗材、帆布袋、好评卡、赠品本来就没有牌价，也不该催定价。
3. **一级分销价只有在售商品才要求** —— 停用商品不卖，逼着定价没有意义。

重点回归：分类「无」「其他」不能当非售卖品。旺店通里这两个分类参杂大量正常商品
（三养火鸡面 20 个、瑞士莲 688g、桂格麦片…），一度被误判成「不用定价」，
导致分销价提醒少报 76 个。
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxProduct
from app.services.product_maintenance import (
    LIST_PRICE_BRANDS,
    LIST_PRICE_DISCOUNT,
    is_merchandise,
    list_price_required,
    maintenance_of,
    maintenance_summary,
    summary_from_db,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _product(**kw):
    base = dict(
        sku="6901", name="百奇巧克力味装饰饼干55g", unit="盒",
        brand="格力高/Glico", category="零食",
        base_price=Decimal("5.83"), list_price=Decimal("0"),
        tier1_price=Decimal("0"), purchase_price=Decimal("4.66"),
        active=True, cost_status="", pack_qty=36, pack_unit="箱",
    )
    base.update(kw)
    return FxProduct(**base)


# ---------------------------------------------------------------- 牌价范围
def test_list_price_required_only_for_configured_brand():
    """牌价只对配置的品牌要求：格力高要，雀巢不要。"""
    assert list_price_required(_product()) is True
    assert list_price_required(_product(brand="雀巢/Nestle", name="雀巢咖啡1+2")) is False


def test_list_price_brand_matches_partial_name():
    """品牌名是「格力高/Glico」，按包含匹配，不能要求完全相等。"""
    assert "格力高" in LIST_PRICE_BRANDS
    assert list_price_required(_product(brand="格力高/Glico")) is True
    assert list_price_required(_product(brand="上海格力高")) is True


# ---------------------------------------------------------------- 非售卖品
@pytest.mark.parametrize("category,name", [
    ("耗材", "格力高纸箱耗材-10盒(18*13*17.5)"),
    ("仓库耗材", "格力高 新年礼盒 (耗材)"),
    ("包材", "【包材】桂格-五谷十珍-上内衬"),
    ("赠品", "瑞士莲 签字笔"),
    ("其他", "格力高 蜡笔小新帆布袋"),
    ("无", "格力高 好评卡"),
])
def test_non_merchandise_excluded(category, name):
    """纸箱 / 包材 / 赠品 / 帆布袋 / 好评卡都不是要定价的商品。"""
    assert is_merchandise(_product(category=category, name=name)) is False


@pytest.mark.parametrize("category,name", [
    # 「无」「其他」是旺店通没填分类，不是「非商品」—— 曾经误判，导致少报 76 个
    ("无", "三养火鸡面(原味)5袋装"),
    ("无", "瑞士莲 软心精选巧克力688g"),
    ("无", "桂格 5红混合即食麦片(罐装)860克-保质期1年"),
    ("其他", "雀巢 可可味滋滋营养谷物食品330g"),
    ("零食", "格力高 百奇分享装巧克力味饼干140g"),
])
def test_normal_products_in_unfilled_category_still_counted(category, name):
    """分类没填（「无」「其他」）的正常商品必须仍然算缺项，不能被当成非售卖品。"""
    assert is_merchandise(_product(category=category, name=name)) is True


# ---------------------------------------------------------------- 缺项判定
def test_missing_list_price_and_tier1():
    info = maintenance_of(_product())
    assert info["need_list_price"] is True
    assert info["need_tier1_price"] is True
    assert info["missing"] == ["list_price", "tier1_price"]
    assert info["missing_count"] == 2
    assert info["missing_text"] == "缺牌价、缺分销价"


def test_nothing_missing_when_all_prices_filled():
    info = maintenance_of(_product(list_price=Decimal("5.83"), tier1_price=Decimal("4.95")))
    assert info["missing"] == []
    assert info["missing_count"] == 0
    assert info["missing_text"] == ""


def test_inactive_product_does_not_require_tier1():
    """停用商品不卖，不要求定分销价；但牌价仍然算（要留档）。"""
    info = maintenance_of(_product(active=False))
    assert info["need_tier1_price"] is False
    assert info["need_list_price"] is True
    assert info["missing"] == ["list_price"]


def test_pending_cost_flagged():
    info = maintenance_of(_product(cost_status="pending", list_price=Decimal("165"),
                                   tier1_price=Decimal("180")))
    assert info["need_cost_check"] is True
    assert info["missing"] == ["cost_check"]


def test_other_brand_only_requires_tier1():
    """雀巢不要求牌价，但分销价要：缺项里不能出现 list_price。"""
    info = maintenance_of(_product(brand="雀巢/Nestle", name="雀巢咖啡1+2"))
    assert info["missing"] == ["tier1_price"]


# ---------------------------------------------------------------- 汇总
def test_summary_counts():
    rows = [
        _product(sku="A"),                                                    # 缺牌价 + 缺分销价
        _product(sku="B", list_price=Decimal("5.83")),                        # 缺分销价
        _product(sku="C", list_price=Decimal("5.83"), tier1_price=Decimal("4.95")),  # 齐全
        _product(sku="D", brand="雀巢/Nestle", name="雀巢咖啡", list_price=Decimal("0")),  # 不要求牌价
        _product(sku="E", name="格力高 好评卡", category="无"),                 # 非售卖品
    ]
    s = maintenance_summary(rows)
    assert s["total_products"] == 5
    assert s["no_list_price"] == 1          # 只有 A（D 品牌不要求、E 非售卖品）
    assert s["no_tier1_price"] == 3         # A B D（C 有价、E 非售卖品）
    assert s["cost_pending"] == 0
    assert s["blocking"] == 4
    assert s["clean"] is False


def test_summary_clean_when_all_maintained():
    rows = [_product(sku="A", list_price=Decimal("5.83"), tier1_price=Decimal("4.95"))]
    s = maintenance_summary(rows)
    assert s["clean"] is True
    assert s["blocking"] == 0


def test_summary_counts_list_price_with_cost():
    """缺牌价但已有采购价的要单独计数 —— 页面用它提示「这 11 个可以反推牌价」。"""
    rows = [
        _product(sku="A", purchase_price=Decimal("4.66")),
        _product(sku="B", purchase_price=Decimal("0")),
    ]
    s = maintenance_summary(rows)
    assert s["no_list_price"] == 2
    assert s["no_list_price_with_cost"] == 1


def test_summary_reports_discount_for_suggestion():
    """折扣率只是「建议值」的算法参数，由后端下发，前端不在代码里写死 0.8。"""
    s = maintenance_summary([_product()])
    assert s["list_price_discount"] == str(LIST_PRICE_DISCOUNT) == "0.8"


def test_summary_from_db_scoped_by_brand(db):
    """选了品牌只看该品牌；同时回传全局待处理数，避免切品牌后以为别家都好了。"""
    db.add_all([
        _product(sku="A", brand="格力高/Glico"),
        _product(sku="B", brand="雀巢/Nestle", name="雀巢咖啡"),
    ])
    db.commit()

    all_s = summary_from_db(db)
    assert all_s["total_products"] == 2
    assert all_s["all_blocking"] == all_s["blocking"] == 3  # A 两项 + B 一项

    scoped = summary_from_db(db, brand="格力高/Glico")
    assert scoped["total_products"] == 1
    assert scoped["blocking"] == 2
    assert scoped["all_blocking"] == 3
