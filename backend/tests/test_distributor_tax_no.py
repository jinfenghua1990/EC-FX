"""分销商：新增「税号」tax_no、编码留空自动生成、「区域」字段退出录入。

用户 2026-10-28 要求：
1. 新建分销商想把税号当识别码用 —— 加「税号」字段，选填；
2. 编码不用手填了 —— 留空自动生成 H-01、H-02…，且不跳号、不撞已有编码；
3. 「区域」页面上删掉（数据是历史值，保留在库里，只是不再录入、不再更新）。

这里锁死三条底线：
1. 不填编码也能建成功，编号连续不重复；
2. 税号能存能改，且改税号不会动到别的字段；
3. 接口/导入都不再接受 region —— 传了也不写库，旧的区域值原样留着。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxDistributor
from app.services import distribution as dist


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def test_code_auto_generated_when_blank(db):
    a = dist.create_distributor(db, {"code": "", "name": "上海甲商贸", "tax_no": "91310115AAAA"})
    b = dist.create_distributor(db, {"code": "   ", "name": "上海乙商贸"})
    assert a["code"] == "H-01"
    assert b["code"] == "H-02"
    assert a["tax_no"] == "91310115AAAA"
    assert b["tax_no"] == ""


def test_explicit_code_still_respected_and_no_skip(db):
    """手工编码照旧；自动编号会跳过已占用的号，不会撞车也不会跳空号。"""
    dist.create_distributor(db, {"code": "FX001", "name": "老分销商"})
    auto = dist.create_distributor(db, {"name": "新分销商"})
    assert auto["code"] == "H-01"          # 手工编码不占 H 号段，编号从 H-01 起

    dist.create_distributor(db, {"code": "H-02", "name": "手工占了 H-02"})
    auto2 = dist.create_distributor(db, {"name": "再来一个"})
    assert auto2["code"] == "H-03"         # H-01/H-02 都占用，顺延到下一个空号


def test_duplicate_code_still_rejected(db):
    dist.create_distributor(db, {"code": "FX001", "name": "甲"})
    with pytest.raises(ValueError, match="已存在"):
        dist.create_distributor(db, {"code": "FX001", "name": "甲重复"})


def test_name_required_code_not(db):
    with pytest.raises(ValueError, match="名称必填"):
        dist.create_distributor(db, {"code": "", "name": "  "})


def test_tax_no_updatable_and_touches_nothing_else(db):
    row = dist.create_distributor(db, {"name": "甲", "phone": "13800000000", "contact": "王经理"})
    got = dist.update_distributor(db, row["id"], {"tax_no": "91310115BBBB"})
    assert got["tax_no"] == "91310115BBBB"
    assert got["phone"] == "13800000000"
    assert got["contact"] == "王经理"


def test_region_not_writable_anymore(db):
    """区域：接口层不再收（payload 里没有这个字段），旧值读到但改不动。"""
    old = FxDistributor(code="FX9", name="老区域商", region="华东")
    db.add(old)
    db.commit()

    # 就算有老代码/导入模板还在传 region，也不写库
    got = dist.update_distributor(db, old.id, {"name": "老区域商改名", "region": "华南"})
    assert got["region"] == "华东"           # 旧数据原样留着
    assert got["name"] == "老区域商改名"
    fresh = db.get(FxDistributor, old.id)
    assert fresh.region == "华东"


def test_import_uses_tax_no_and_auto_code(db):
    res = dist.import_distributors(db, [
        {"code": "", "name": "甲", "tax_no": "T1"},
        {"code": "", "name": "乙", "tax_no": "T2"},
        {"code": "", "name": "甲", "tax_no": "T1"},   # 同名不同编码，可建（重名不拦）
    ])
    assert res["created"] == 3
    codes = sorted(db.scalars(select(FxDistributor.code)).all())
    assert codes == ["H-01", "H-02", "H-03"]
    assert sorted(db.scalars(select(FxDistributor.tax_no)).all()) == ["T1", "T1", "T2"]
