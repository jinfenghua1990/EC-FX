"""分销下单一键流程：创建 → 自动审核 → 推送旺店通。

业务口径（2026-10-27 用户明确）
------------------------------
1. 用户要的是「下单界面」：选分销商、按目录填数量，提交后**自动推送旺店通**，
   之后仓储/发货都在旺店通，本系统只看订单信息。
2. 下单默认单价取值：经销商专属价 > 一级分销价(>0) > 商品基础价。
3. 推送失败不回滚订单（单据已落库、状态为已审核），报错带回给前端供重推。
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.services.distribution as dist
from app.db import Base
from app.models import FxDistributor, FxProduct
from app.services.distribution import create_and_push_order


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture()
def seeded(db):
    d = FxDistributor(code="D001", name="分销商一", active=True)
    db.add(d)
    db.flush()
    p1 = FxProduct(sku="SKU-1", name="商品一", spec="盒", base_price=Decimal("10"))
    p2 = FxProduct(sku="SKU-2", name="商品二", spec="盒", base_price=Decimal("5"), tier1_price=Decimal("4.5"))
    db.add_all([p1, p2])
    db.commit()
    return db, d, p1, p2


class _FakeClient:
    """可编程假旺店通客户端：push_ok=False 时模拟推送报错。"""
    configured = True
    missing: list[str] = []

    def __init__(self, push_ok=True):
        self.push_ok = push_ok

    def sales_trade_push(self, trade):
        if not self.push_ok:
            raise RuntimeError("连接超时")
        return {"code": 0, "tid": "WDT-123", "message": "ok"}


def test_create_and_push_success_flow(seeded, monkeypatch):
    """下单即推成功：订单落库为已审核、wdt_tid 回填、pushed=True。"""
    db, d, p1, p2 = seeded
    monkeypatch.setattr(dist.settings, "wdt_shop_no", "SHOP-TEST")
    monkeypatch.setattr(dist, "WangdianClient", lambda: _FakeClient(push_ok=True))

    result = create_and_push_order(
        db, d.id,
        lines=[{"product_id": p1.id, "quantity": 3}, {"product_id": p2.id, "quantity": 10}],
    )
    order = result["order"]
    assert result["pushed"] is True
    assert result["push_error"] == ""
    assert order["status"] == "approved"
    assert order["wdt_tid"] == "WDT-123"
    assert order["push_status"] == "sent"
    # 明细单价默认套一级价/基础价：3×10 + 10×4.5 = 75
    assert Decimal(str(order["total_amount"])) == Decimal("75.00")


def test_create_and_push_failure_keeps_order(seeded, monkeypatch):
    """推送失败：订单保留（已审核）+ push_status=failed，报错带出供重推。"""
    db, d, p1, _ = seeded
    monkeypatch.setattr(dist.settings, "wdt_shop_no", "SHOP-TEST")
    monkeypatch.setattr(dist, "WangdianClient", lambda: _FakeClient(push_ok=False))

    result = create_and_push_order(db, d.id, lines=[{"product_id": p1.id, "quantity": 1}])
    assert result["pushed"] is False
    assert "推送旺店通失败" in result["push_error"]
    assert result["order"]["status"] == "approved"
    assert result["order"]["push_status"] == "failed"


def test_order_default_price_prefers_tier1(seeded):
    """单价留空时默认价：一级价(>0) 优先于基础价。"""
    db, d, p1, p2 = seeded
    view = dist.create_order(db, d.id, lines=[{"product_id": p2.id, "quantity": 2}])
    line = view["lines"][0]
    assert Decimal(str(line["unit_price"])) == Decimal("4.50")


def test_order_default_price_prefers_distributor_override(seeded):
    """经销商专属价优先于一级价。"""
    db, d, p1, p2 = seeded
    from app.models import FxPrice
    db.add(FxPrice(distributor_id=d.id, product_id=p2.id, price=Decimal("4.2")))
    db.commit()
    view = dist.create_order(db, d.id, lines=[{"product_id": p2.id, "quantity": 2}])
    assert Decimal(str(view["lines"][0]["unit_price"])) == Decimal("4.20")


def test_next_order_no_uses_max_seq_not_count(seeded):
    """删单后序号不回绕：取当天最大序号+1，而不是条数+1。"""
    db, d, p1, _ = seeded
    from datetime import datetime
    from app.models import FxOrder
    prefix = datetime.now().strftime("FX%Y%m%d")
    db.add_all([
        FxOrder(order_no=f"{prefix}-0001", distributor_id=d.id, total_amount=Decimal("1")),
        FxOrder(order_no=f"{prefix}-0003", distributor_id=d.id, total_amount=Decimal("1")),
    ])
    db.commit()
    # 模拟 -0002 被删：按条数会再编 0002 撞唯一约束之外还跳号错乱
    db.query(FxOrder).filter_by(order_no=f"{prefix}-0001").delete()
    db.commit()
    assert dist._next_order_no(db) == f"{prefix}-0004"


def test_push_uses_order_shop_over_default(seeded, monkeypatch):
    """订单上记的店铺优先于系统默认配置（推送报文里应是订单的店铺）。"""
    db, d, p1, _ = seeded
    monkeypatch.setattr(dist.settings, "wdt_shop_no", "DEFAULT-SHOP")
    seen: dict = {}

    class _Spy(_FakeClient):
        def sales_trade_push(self, trade):
            seen.update(trade)
            return {"code": 0, "tid": "WDT-1", "message": "ok"}

    monkeypatch.setattr(dist, "WangdianClient", lambda: _Spy())
    result = create_and_push_order(db, d.id, lines=[{"product_id": p1.id, "quantity": 1}], wdt_shop_no="hhz-xxqd")
    assert result["pushed"] is True
    assert seen["shop_no"] == "hhz-xxqd"


def test_push_falls_back_to_default_shop(seeded, monkeypatch):
    """订单没记店铺时回退系统默认配置。"""
    db, d, p1, _ = seeded
    monkeypatch.setattr(dist.settings, "wdt_shop_no", "DEFAULT-SHOP")
    seen: dict = {}

    class _Spy(_FakeClient):
        def sales_trade_push(self, trade):
            seen.update(trade)
            return {"code": 0, "tid": "WDT-2", "message": "ok"}

    monkeypatch.setattr(dist, "WangdianClient", lambda: _Spy())
    result = create_and_push_order(db, d.id, lines=[{"product_id": p1.id, "quantity": 1}])
    assert result["pushed"] is True
    assert seen["shop_no"] == "DEFAULT-SHOP"


def test_push_without_any_shop_fails_clearly(seeded, monkeypatch):
    """两边都没店铺：明确报错，订单保留可补选后重推。"""
    db, d, p1, _ = seeded
    monkeypatch.setattr(dist.settings, "wdt_shop_no", "")
    monkeypatch.setattr(dist, "WangdianClient", lambda: _FakeClient())
    result = create_and_push_order(db, d.id, lines=[{"product_id": p1.id, "quantity": 1}])
    assert result["pushed"] is False
    assert "未选择推送店铺" in result["push_error"]
    assert result["order"]["status"] == "approved"
