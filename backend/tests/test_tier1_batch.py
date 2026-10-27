"""批量折扣率设一级分销价 + 牌价回填（按品牌反推）。

业务口径（2026-10-27 用户明确）
------------------------------
1. 拿货折扣**按品牌**：
   - 格力高：采购价 = 牌价 × 0.8（8 折拿货，88 个已维护牌价商品实测全部命中）
   - 瑞士莲/桂格等其他品牌：**采购价本身就是品牌标价**，牌价 = 采购价
2. 一级分销价要能「勾选商品 × 折扣率」批量设：一级价 = 牌价 × 折扣（8.5 折 = 0.85）。
3. 箱价 = 一级价 × 箱规，用户按箱拿货时看的是整箱价。
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxProduct
from app.services.distribution import _product_view
from app.services.product_maintenance import backfill_list_prices, tier1_by_discount


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


# ---------------------------------------------------------------- 牌价回填
def test_backfill_discount_differs_by_brand(db):
    """折扣按品牌：格力高 4.6641÷0.8=5.83；瑞士莲采购价即牌价，103 原样回填。"""
    db.add_all([
        FxProduct(sku="G1", name="格力高百奇", brand="格力高/Glico", purchase_price=Decimal("4.6641")),
        FxProduct(sku="S1", name="瑞士莲600g", brand="瑞士莲/lindt", purchase_price=Decimal("103")),
        FxProduct(sku="Q1", name="桂格燕麦", brand="桂格/QUAKER", purchase_price=Decimal("17.75")),
    ])
    db.commit()

    result = backfill_list_prices(db, dry_run=True)
    rows = {r["sku"]: r for r in result["rows"]}
    assert rows["G1"]["discount"] == "0.8"
    assert rows["G1"]["list_price"] == "5.83"
    assert rows["S1"]["discount"] == "1"
    assert rows["S1"]["list_price"] == "103.00"
    assert rows["Q1"]["list_price"] == "17.75"
    # 预览不写库
    assert db.query(FxProduct).filter_by(sku="G1").one().list_price == Decimal("0")

    result = backfill_list_prices(db, dry_run=False)
    assert result["updated"] == 3
    assert db.query(FxProduct).filter_by(sku="G1").one().list_price == Decimal("5.83")
    assert db.query(FxProduct).filter_by(sku="S1").one().list_price == Decimal("103.0000")


def test_backfill_no_brand_uses_default_discount(db):
    """没填品牌的商品按默认 1.0：采购价即牌价，不瞎猜 8 折。"""
    db.add(FxProduct(sku="P1", name="没品牌", purchase_price=Decimal("4.6641")))
    db.commit()
    result = backfill_list_prices(db, dry_run=False)
    assert result["rows"][0]["list_price"] == "4.66"


def test_backfill_never_overwrites_existing_list_price(db):
    """只补空值：已有牌价的商品绝不能被反推值覆盖。"""
    db.add(FxProduct(sku="P1", name="已有牌价", purchase_price=Decimal("4.6641"),
                     list_price=Decimal("6.00")))
    db.commit()
    result = backfill_list_prices(db, dry_run=False)
    assert result["eligible"] == 0
    assert db.query(FxProduct).filter_by(sku="P1").one().list_price == Decimal("6.00")


def test_backfill_excludes_non_merchandise(db):
    """纸箱耗材不需要牌价，反推出来只会污染页面。"""
    db.add(FxProduct(sku="BOX1", name="纸箱耗材-126g-2盒", purchase_price=Decimal("0.4534")))
    db.commit()
    result = backfill_list_prices(db, dry_run=False)
    assert result["eligible"] == 0


def test_backfill_no_purchase_price_is_skipped(db):
    """没有采购价就没有基准，反推不了。"""
    db.add(FxProduct(sku="P1", name="没采购过", purchase_price=Decimal("0")))
    db.commit()
    assert backfill_list_prices(db, dry_run=False)["eligible"] == 0


def test_backfill_brand_filter(db):
    db.add_all([
        FxProduct(sku="G1", name="格力高甲", brand="格力高/Glico", purchase_price=Decimal("4.6641")),
        FxProduct(sku="S1", name="瑞士莲乙", brand="瑞士莲/lindt", purchase_price=Decimal("103")),
    ])
    db.commit()
    result = backfill_list_prices(db, brand="格力高", dry_run=False)
    assert result["eligible"] == 1
    assert result["rows"][0]["sku"] == "G1"


def test_backfill_invalid_discount_raises(db):
    db.add(FxProduct(sku="P1", name="甲", purchase_price=Decimal("4.6641")))
    db.commit()
    with pytest.raises(ValueError, match="0~1"):
        backfill_list_prices(db, discount="1.25", dry_run=False)


# ---------------------------------------------------------------- 批量折扣设一级价
def test_tier1_by_discount_zhe_and_decimal_equivalent(db):
    """8.5（几折）和 0.85（小数）是同一个折扣：5.83 × 0.85 = 4.9555 → 4.96。"""
    db.add(FxProduct(sku="P1", name="甲", list_price=Decimal("5.83")))
    db.commit()

    by_zhe = tier1_by_discount(db, [1], "8.5", dry_run=True)
    by_dec = tier1_by_discount(db, [1], "0.85", dry_run=True)
    assert by_zhe["rows"][0]["tier1_price"] == "4.96"
    assert by_dec["rows"][0]["tier1_price"] == "4.96"
    assert by_zhe["rate"] == "0.8500"
    # dry_run 不写库
    assert db.query(FxProduct).filter_by(sku="P1").one().tier1_price == Decimal("0")


def test_tier1_round_half_up_not_bankers(db):
    """5.83 × 0.9 = 5.247 → 5.25（四舍五入），不是银行家舍入的 5.25 也得对上。"""
    db.add(FxProduct(sku="P1", name="甲", list_price=Decimal("5.83")))
    db.commit()
    result = tier1_by_discount(db, [1], "9", dry_run=False)
    assert result["rows"][0]["tier1_price"] == "5.25"
    assert db.query(FxProduct).filter_by(sku="P1").one().tier1_price == Decimal("5.25")


def test_tier1_box_price_times_pack_qty(db):
    """箱价 = 一级价 × 箱规：4.96 × 36 = 178.56。"""
    db.add(FxProduct(sku="P1", name="甲", list_price=Decimal("5.83"), pack_qty=36))
    db.commit()
    result = tier1_by_discount(db, [1], "8.5", dry_run=False)
    assert result["rows"][0]["tier1_price"] == "4.96"
    assert result["rows"][0]["box_price"] == "178.56"


def test_tier1_skips_products_without_list_price(db):
    """牌价是折扣的基准，没牌价的跳过并给原因，不影响其他商品。"""
    db.add_all([
        FxProduct(sku="P1", name="有牌价", list_price=Decimal("5.83")),
        FxProduct(sku="P2", name="没牌价", list_price=Decimal("0")),
    ])
    db.commit()
    result = tier1_by_discount(db, [1, 2], "8.5", dry_run=False)
    assert result["updated"] == 1
    assert result["skipped"] == [{"id": 2, "sku": "P2", "reason": result["skipped"][0]["reason"]}]
    assert "牌价为空" in result["skipped"][0]["reason"]
    assert db.query(FxProduct).filter_by(sku="P1").one().tier1_price == Decimal("4.96")
    assert db.query(FxProduct).filter_by(sku="P2").one().tier1_price == Decimal("0")


def test_tier1_requires_selection(db):
    with pytest.raises(ValueError, match="勾选"):
        tier1_by_discount(db, [], "8.5")


def test_tier1_invalid_zhe_raises(db):
    db.add(FxProduct(sku="P1", name="甲", list_price=Decimal("5.83")))
    db.commit()
    with pytest.raises(ValueError, match="0~10"):
        tier1_by_discount(db, [1], "12")
    with pytest.raises(ValueError, match="格式"):
        tier1_by_discount(db, [1], "abc")


# ---------------------------------------------------------------- 商品视图带箱价
def test_product_view_includes_tier1_box_price(db):
    """列表接口要能直接显示箱价：一级价 4.96 × 箱规 36 = 178.56；没箱规/没定价为空。"""
    db.add_all([
        FxProduct(sku="P1", name="有箱规", list_price=Decimal("5.83"), tier1_price=Decimal("4.96"), pack_qty=36),
        FxProduct(sku="P2", name="没箱规", tier1_price=Decimal("4.96")),
        FxProduct(sku="P3", name="没定价", pack_qty=24),
    ])
    db.commit()
    views = {v["sku"]: v for v in [_product_view(p) for p in db.query(FxProduct).all()]}
    assert views["P1"]["tier1_box_price"] == "178.56"
    assert views["P2"]["tier1_box_price"] == ""
    assert views["P3"]["tier1_box_price"] == ""
