"""「订单状态处理」页：待审核 / 已审核 / 推送异常 三合一，支持批量处理。

用户 2026-10-28 要求：把「待审核订单」「已审核订单」「订单异常」合并成一个「订单状态处理」。
这里锁死四条底线：
1. 订单能按 push_status 过滤（异常单 = push_status=failed，未推送的不算异常）；
2. 批量改状态逐单走同一套流转规则，一单不合法不影响其它单，失败的单独列出来；
3. 批量重推只推「已审核且没推成功」的，已推送过的自动跳过，绝不重复推；
4. 空列表 / 未知动作要报清晰的错，不静默吞掉。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxDistributor, FxOrder
from app.services import distribution as dist


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture()
def seeded(db):
    d = dist.create_distributor(db, {"code": "D001", "name": "甲", "brands": ["格力高"]})
    def mk(no, status, push="", **kw):
        o = FxOrder(order_no=no, distributor_id=d["id"], status=status, order_date=datetime(2026, 10, 20),
                    total_amount=Decimal("100"), push_status=push, **kw)
        db.add(o)
        db.commit()
        return o
    return {
        "pending": mk("SO-1", "submitted"),
        "pending2": mk("SO-2", "submitted"),
        "approved": mk("SO-3", "approved"),
        "failed": mk("SO-4", "approved", "failed"),
        "sent": mk("SO-5", "approved", "sent"),
        "draft": mk("SO-6", "draft"),
        "dist": d,
    }


def test_list_orders_filter_by_push_status(seeded, db):
    got = dist.list_orders(db, push_status="failed")
    assert [o["order_no"] for o in got] == ["SO-4"]           # 只有推送失败的算异常单
    assert [o["order_no"] for o in dist.list_orders(db, push_status="sent")] == ["SO-5"]
    # 未推送的不算异常（push_status 为空）
    assert "SO-3" not in [o["order_no"] for o in dist.list_orders(db, push_status="failed")]
    # 状态 + 推送结果可以叠加
    assert dist.list_orders(db, status="submitted", push_status="failed") == []


def test_batch_approve_only_touches_submitted(seeded, db):
    r = dist.batch_transition_orders(db, [seeded["pending"].id, seeded["pending2"].id,
                                          seeded["approved"].id, seeded["draft"].id], "approve")
    assert r["updated"] == 2
    assert db.get(FxOrder, seeded["pending"].id).status == "approved"
    assert db.get(FxOrder, seeded["pending2"].id).status == "approved"
    # 不合法的单独列出来，且整批不回滚（合法的那两张已经改了）
    bad = sorted(f["id"] for f in r["failed"])
    assert bad == sorted([seeded["approved"].id, seeded["draft"].id])
    assert any("不允许" in f["error"] for f in r["failed"])
    # 原始状态没被顺手改坏
    assert db.get(FxOrder, seeded["draft"].id).status == "draft"


def test_batch_cancel_and_unknown_action(seeded, db):
    r = dist.batch_transition_orders(db, [seeded["pending"].id], "cancel")
    assert r["updated"] == 1 and db.get(FxOrder, seeded["pending"].id).status == "cancelled"
    with pytest.raises(ValueError):
        dist.batch_transition_orders(db, [seeded["pending"].id], "delete_all")
    # 空列表：什么都不做，不报错
    assert dist.batch_transition_orders(db, [], "approve") == {"updated": 0, "failed": []}
    # 不存在的 id 也不会炸整批
    r2 = dist.batch_transition_orders(db, [9999, seeded["pending2"].id], "approve")
    assert r2["updated"] == 1 and r2["failed"][0]["id"] == 9999


def test_batch_push_skips_unapproved_and_already_sent(seeded, db, monkeypatch):
    called = []
    monkeypatch.setattr(dist, "push_order_to_wangdian", lambda db_, oid: called.append(oid))
    r = dist.batch_push_orders(db, [seeded["failed"].id, seeded["approved"].id,
                                    seeded["sent"].id, seeded["draft"].id])
    assert r["pushed"] == 2                                  # failed + 未推送的 approved
    assert r["skipped"] == 2                                 # 已推送过 + 草稿（未审核）
    assert called == [seeded["failed"].id, seeded["approved"].id]
    # 已推送过的绝不会被重复推
    assert seeded["sent"].id not in called


def test_batch_push_collects_failures(seeded, db, monkeypatch):
    def boom(db_, oid):
        raise ValueError("旺店通未配置")
    monkeypatch.setattr(dist, "push_order_to_wangdian", boom)
    r = dist.batch_push_orders(db, [seeded["failed"].id, seeded["draft"].id])
    assert r["pushed"] == 0 and r["skipped"] == 1
    assert r["failed"] and "旺店通未配置" in r["failed"][0]["error"]
    # 失败不改变订单原有推送状态（重推前状态保持可见，方便排查）
    assert db.get(FxOrder, seeded["failed"].id).push_status == "failed"
