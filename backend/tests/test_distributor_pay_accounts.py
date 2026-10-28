"""分销商付款账户明细：一客户多账户 + 税号/银行/收款信息。

2026-10-28 用户要求付款账户不能再只是一个名称字符串：
每个账户要有税号、开户行/支行、银行账号、户名/收款人、客户收款备注，
一个客户可以有多条；金额仍由订单/对账单派生，这里不存金额。
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxDistributor, FxDistributorPayLink
from app.services import distribution as dist


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def test_detailed_payment_accounts_roundtrip(db):
    view = dist.create_distributor(db, {
        "code": "D100",
        "name": "上海优华贸易有限公司",
        "tax_no": "91310000MAIN",
        "payment_accounts": [
            {
                "account_name": "上海优华贸易有限公司",
                "tax_no": "91310000MAIN",
                "bank_name": "中国工商银行上海分行",
                "bank_branch": "徐汇支行",
                "bank_account": "1001234509876543210",
                "account_holder": "上海优华贸易有限公司",
                "account_type": "company",
                "collection_note": "货款",
                "is_default": True,
            },
            {
                "account_name": "钟军",
                "tax_no": "310101199001010011",
                "bank_name": "招商银行",
                "bank_branch": "静安支行",
                "bank_account": "6225888800001234",
                "account_holder": "钟军",
                "account_type": "personal",
                "collection_note": "关联公司代付",
                "is_default": False,
            },
        ],
    })

    assert view["pay_accounts"] == ["上海优华贸易有限公司", "钟军"]
    assert len(view["payment_accounts"]) == 2
    first, second = view["payment_accounts"]
    assert first["account_name"] == "上海优华贸易有限公司"
    assert first["tax_no"] == "91310000MAIN"
    assert first["bank_branch"] == "徐汇支行"
    assert first["bank_account"] == "1001234509876543210"
    assert first["collection_note"] == "货款"
    assert first["is_default"] is True
    assert second["account_type"] == "personal"
    assert second["bank_name"] == "招商银行"
    assert second["is_default"] is False

    row = db.get(FxDistributor, view["id"])
    assert row.pay_accounts == "上海优华贸易有限公司,钟军"
    assert db.scalar(select(FxDistributorPayLink).where(
        FxDistributorPayLink.distributor_id == view["id"]
    )) is not None


def test_update_only_name_keeps_accounts_and_can_replace_or_clear(db):
    created = dist.create_distributor(db, {
        "name": "甲客户",
        "payment_accounts": [{"account_name": "甲客户", "bank_account": "A-1"}],
    })
    did = created["id"]

    # 只改名字：不能把已有付款账户清空
    renamed = dist.update_distributor(db, did, {"name": "甲客户改名"})
    assert renamed["name"] == "甲客户改名"
    assert [a["account_name"] for a in renamed["payment_accounts"]] == ["甲客户"]
    assert renamed["payment_accounts"][0]["bank_account"] == "A-1"

    replaced = dist.update_distributor(db, did, {
        "payment_accounts": [{"account_name": "关联公司", "tax_no": "T2", "account_type": "company"}],
    })
    assert [a["account_name"] for a in replaced["payment_accounts"]] == ["关联公司"]
    assert replaced["payment_accounts"][0]["tax_no"] == "T2"
    assert replaced["payment_accounts"][0]["is_default"] is True

    cleared = dist.update_distributor(db, did, {"payment_accounts": []})
    assert cleared["payment_accounts"] == []
    assert cleared["pay_accounts"] == []
    assert db.get(FxDistributor, did).pay_accounts == ""


def test_legacy_name_list_still_works_and_account_name_required(db):
    # 旧前端/旧导入只传名称数组，仍要能落成明细
    view = dist.create_distributor(db, {
        "name": "老接口客户",
        "pay_accounts": "甲公司、乙公司，甲公司",
    })
    assert view["pay_accounts"] == ["甲公司", "乙公司"]
    assert [a["account_name"] for a in view["payment_accounts"]] == ["甲公司", "乙公司"]
    assert view["payment_accounts"][0]["is_default"] is True

    # 只有银行信息、没有付款方名称的行不应落库
    view2 = dist.create_distributor(db, {
        "name": "空账户客户",
        "payment_accounts": [
            {"account_name": "", "bank_account": "NO-NAME"},
            {"account_name": "有效账户", "is_default": True},
        ],
    })
    assert [a["account_name"] for a in view2["payment_accounts"]] == ["有效账户"]


def test_merge_moves_payment_account_details(db):
    target = dist.create_distributor(db, {
        "name": "目标客户",
        "payment_accounts": [{"account_name": "目标客户", "tax_no": "T-TARGET", "bank_account": "B-1"}],
    })
    source = dist.create_distributor(db, {
        "name": "源客户",
        "payment_accounts": [{
            "account_name": "源客户关联公司",
            "tax_no": "T-SOURCE",
            "bank_name": "建设银行",
            "bank_account": "B-2",
            "account_holder": "源客户关联公司",
            "collection_note": "代付货款",
        }],
    })

    dist.merge_distributors(db, target["id"], [source["id"]])
    merged = dist._distributor_view(db.get(FxDistributor, target["id"]))
    names = [a["account_name"] for a in merged["payment_accounts"]]
    assert "目标客户" in names
    assert "源客户关联公司" in names
    assert "源客户" in names  # 被合并客户名也保留为对账线索
    source_acc = next(a for a in merged["payment_accounts"] if a["account_name"] == "源客户关联公司")
    assert source_acc["tax_no"] == "T-SOURCE"
    assert source_acc["collection_note"] == "代付货款"
    assert source_acc["is_default"] is False
    assert db.get(FxDistributor, source["id"]) is None
    # 原来源账户明细不能残留
    assert db.scalars(select(FxDistributorPayLink).where(
        FxDistributorPayLink.distributor_id == source["id"]
    )).all() == []
