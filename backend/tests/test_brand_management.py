"""品牌主数据：单独建档、改名级联、停用启用、商品品牌补建。"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxBrand, FxDistributor, FxProduct
from app.services import distribution as dist


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _product(db, sku="S1", brand="格力高/Glico", active=1):
    row = FxProduct(sku=sku, name=f"商品{sku}", brand=brand, active=active)
    db.add(row)
    db.commit()
    return row


def test_unmanaged_product_brand_can_be_promoted(db):
    _product(db, brand="新品牌/NewBrand")
    brands = dist.list_brands(db)
    item = next(b for b in brands if b["name"] == "新品牌/NewBrand")
    assert item["managed"] is False
    assert item["id"] is None
    assert item["short_name"] == "新品牌"
    assert item["count"] == 1

    made = dist.create_brand(db, {"name": "新品牌/NewBrand", "code": "NEW"})
    assert made["managed"] is True
    after = next(b for b in dist.list_brands(db) if b["name"] == "新品牌/NewBrand")
    assert after["id"] == made["id"]
    assert after["code"] == "NEW"
    assert after["product_count"] == 1


def test_create_and_update_brand_with_rename_cascade(db):
    _product(db, sku="G1", brand="格力高/Glico")
    d = dist.create_distributor(db, {"name": "经销商甲", "brands": ["格力高/Glico", "雀巢/Nestle"]})
    brand = dist.create_brand(db, {
        "name": "格力高/Glico", "short_name": "格力高", "code": "GLICO",
        "sort_order": 3, "logo": "glico.png", "note": "主品牌",
    })
    assert brand["name"] == "格力高/Glico"
    assert brand["active"] is True

    updated = dist.update_brand(db, brand["id"], {
        "name": "格力高食品/Glico", "short_name": "格力高", "code": "GLICO",
        "sort_order": 1, "logo": "glico.png", "active": True,
    })
    assert updated["name"] == "格力高食品/Glico"
    # 商品档案改名级联
    assert db.scalar(select(FxProduct.brand).where(FxProduct.sku == "G1")) == "格力高食品/Glico"
    # 分销商授权改名级联，且不误伤其它品牌
    fresh = db.get(FxDistributor, d["id"])
    assert fresh.brands == "格力高食品/Glico,雀巢/Nestle"

    # 改名后老品牌不能还能重复建
    with pytest.raises(ValueError):
        dist.create_brand(db, {"name": "格力高食品/Glico"})


def test_toggle_hides_brand_from_default_list_but_keeps_management_row(db):
    brand = dist.create_brand(db, {"name": "待停用品牌", "sort_order": 2})
    assert any(b["id"] == brand["id"] for b in dist.list_brands(db))
    stopped = dist.toggle_brand(db, brand["id"])
    assert stopped["active"] is False
    assert all(b["id"] != brand["id"] for b in dist.list_brands(db))
    managed = dist.list_brands(db, include_inactive=True)
    row = next(b for b in managed if b["id"] == brand["id"])
    assert row["active"] is False
    # 再启用
    assert dist.toggle_brand(db, brand["id"])["active"] is True


def test_delete_unused_brand_but_reject_brands_in_use(db):
    unused = dist.create_brand(db, {"name": "仅测试空品牌"})
    result = dist.delete_brand(db, unused["id"])
    assert result["deleted"] is True
    assert result["name"] == "仅测试空品牌"
    assert db.get(FxBrand, unused["id"]) is None

    # 商品还在用：不能删主数据，否则商品品牌会变成孤儿字符串
    used = dist.create_brand(db, {"name": "被商品使用品牌"})
    _product(db, sku="USED-1", brand="被商品使用品牌")
    with pytest.raises(ValueError, match="不能直接删除"):
        dist.delete_brand(db, used["id"])
    assert db.get(FxBrand, used["id"]) is not None

    # 分销商授权还在用：同样拒绝
    authorized = dist.create_brand(db, {"name": "被授权品牌"})
    dist.create_distributor(db, {"name": "授权测试客户", "brands": ["被授权品牌"]})
    with pytest.raises(ValueError, match="不能直接删除"):
        dist.delete_brand(db, authorized["id"])
    assert db.get(FxBrand, authorized["id"]) is not None

    with pytest.raises(ValueError, match="不存在"):
        dist.delete_brand(db, 999999)


def test_sync_products_creates_missing_brands_only(db):
    _product(db, sku="S1", brand="品牌A")
    _product(db, sku="S2", brand="品牌B")
    dist.create_brand(db, {"name": "品牌A", "short_name": "A"})
    result = dist.sync_brands_from_products(db)
    assert result["created"] == 1
    assert result["names"] == ["品牌B"]
    names = {b.name for b in db.scalars(select(FxBrand)).all()}
    assert names == {"品牌A", "品牌B"}
    # 再同步不重复建
    assert dist.sync_brands_from_products(db)["created"] == 0
