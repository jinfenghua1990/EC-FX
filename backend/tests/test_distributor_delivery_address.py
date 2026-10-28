"""分销商：独立「收货地址」delivery_address 字段。

用户 2026-10-28 要求：客户收货地址要单独一栏，不要只能塞在备注里
（原来备注是「B2B团单导入｜收货：上海市松江区盛龙路755号7栋1楼、13817654550、林」）。

这里锁死三条底线：
1. 新建/修改/批量导入都能带收货地址，且互不干扰备注等其它字段；
2. 合并客户时源客户的收货地址不会被丢；
3. 旧备注里的收货信息能按固定口径拆出地址（迁移 0023 的回填口径）。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.services import distribution as dist

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "migrations" / "versions" / "0023_fx_distributor_delivery_address.py"
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig0023", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_create_with_delivery_address(db):
    got = dist.create_distributor(db, {
        "name": "林（正祥）", "contact": "林", "phone": "13817654550",
        "delivery_address": "上海市松江区盛龙路755号7栋1楼", "note": "月结30天",
    })
    assert got["delivery_address"] == "上海市松江区盛龙路755号7栋1楼"
    assert got["note"] == "月结30天"


def test_update_delivery_address_touches_nothing_else(db):
    row = dist.create_distributor(db, {"name": "甲", "phone": "13800000000", "note": "开票要求"})
    got = dist.update_distributor(db, row["id"], {"delivery_address": "上海市松江区XX路1号"})
    assert got["delivery_address"] == "上海市松江区XX路1号"
    assert got["phone"] == "13800000000"
    assert got["note"] == "开票要求"

    cleared = dist.update_distributor(db, row["id"], {"delivery_address": ""})
    assert cleared["delivery_address"] == ""


def test_import_carries_delivery_address(db):
    res = dist.import_distributors(db, [
        {"code": "", "name": "甲", "delivery_address": "上海市青浦区XX路2号"},
        {"code": "", "name": "乙"},
    ])
    assert res["created"] == 2
    rows = {d["name"]: d for d in dist.list_distributors(db)}
    assert rows["甲"]["delivery_address"] == "上海市青浦区XX路2号"
    assert rows["乙"]["delivery_address"] == ""


def test_merge_keeps_source_delivery_address(db):
    target = dist.create_distributor(db, {"name": "甲", "delivery_address": "上海市松江区A号"})
    source = dist.create_distributor(db, {"name": "乙", "delivery_address": "上海市松江区B号"})
    merged = dist.merge_distributors(db, target["id"], [source["id"]])
    assert "上海市松江区A号" in merged["distributor"]["delivery_address"]
    assert "上海市松江区B号" in merged["distributor"]["delivery_address"]


def test_migration_splits_address_out_of_old_note():
    """迁移 0023 的回填口径：备注「收货：地址、电话、收货人」→ 地址单独一栏。"""
    mod = _load_migration()
    addr, shipper = mod._split_receipt(
        "上海威太贸易有限公司-送货地址：上海市松江区潮富路78号、186-1620-4493、王师傅"
    )
    assert addr == "上海市松江区潮富路78号"
    assert shipper == "上海威太贸易有限公司"

    addr2, shipper2 = mod._split_receipt("上海市松江区盛龙路755号7栋1楼、13817654550、林")
    assert addr2 == "上海市松江区盛龙路755号7栋1楼"
    assert shipper2 == ""
