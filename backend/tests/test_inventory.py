"""库存对接 + 满足率三态 + 采购单成本过滤修正。

覆盖：
1. 满足率三态：满足库存 / 到仓待入库 / 库存不足（单仓模式）
2. 昨天库存基线（fx_stock_snapshots 取 < 今天 的最新一张）
3. 今日入库（已入库 stockin_num + 到仓待入库 arrive_num-stockin_num，仅已收货状态）
4. 采购单成本过滤修正：待审核(10) 不再计入成本
5. 库存快照同步：聚合 + 落库（用假 client 绕过旺店通）
"""
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    FxOrder,
    FxOrderLine,
    FxProduct,
    FxPurchaseItem,
    FxPurchaseOrder,
    FxStockSnapshot,
)
from app.services import inventory
from app.services.wdt_purchase import PURCHASE_EXCLUDE_COST
from app.services.wdt_purchase import refresh_product_purchase_prices, aggregate_purchase_prices


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _seed_product(db, sku="SKU1", spec="SP1"):
    p = FxProduct(sku=sku, name="测试商品", spec_no=spec, brand="测试", source="manual")
    db.add(p)
    db.commit()
    return p


def _seed_order(db, spec="SP1", qty=10, ono="FX-TEST-1"):
    o = FxOrder(order_no=ono, distributor_id=1, status="approved", total_amount=Decimal("0"))
    db.add(o)
    db.flush()
    db.add(FxOrderLine(order_id=o.id, spec_no=spec, goods_name="测试商品", quantity=Decimal(str(qty))))
    db.commit()
    return o


def test_yesterday_stock_baseline():
    db = _db()
    today = datetime.now().strftime("%Y-%m-%d")
    yest = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    older = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    db.add_all([
        FxStockSnapshot(spec_no="SP1", warehouse="", snap_date=older, qty=Decimal("5")),
        FxStockSnapshot(spec_no="SP1", warehouse="", snap_date=yest, qty=Decimal("30")),  # 应取这张
    ])
    db.commit()
    assert inventory.yesterday_stock(db, "SP1", today) == Decimal("30")
    assert inventory.yesterday_stock(db, "NOPE", today) == Decimal("0")


def test_today_inbound_only_received_and_today():
    db = _db()
    today = datetime.now().strftime("%Y-%m-%d")
    now = datetime.now()
    # 今日已收货：arrive 10, stockin 8 → 已入库 8，到仓待入库 2
    po_ok = FxPurchaseOrder(purchase_no="CG-OK", status="90", check_time=now, abnormal=0)
    db.add(po_ok)
    db.flush()
    db.add(FxPurchaseItem(order_id=po_ok.id, spec_no="SP1", arrive_num=Decimal("10"), stockin_num=Decimal("8")))
    # 待审核(10)：不应计入
    po_wait = FxPurchaseOrder(purchase_no="CG-WAIT", status="10", check_time=now, abnormal=0)
    db.add(po_wait)
    db.flush()
    db.add(FxPurchaseItem(order_id=po_wait.id, spec_no="SP1", arrive_num=Decimal("100"), stockin_num=Decimal("100")))
    # 昨天收货：不应计入今日
    po_old = FxPurchaseOrder(purchase_no="CG-OLD", status="90", check_time=now - timedelta(days=1), abnormal=0)
    db.add(po_old)
    db.flush()
    db.add(FxPurchaseItem(order_id=po_old.id, spec_no="SP1", arrive_num=Decimal("50"), stockin_num=Decimal("50")))
    db.commit()

    stockin, pending = inventory.today_inbound(db, "SP1", today)
    assert stockin == Decimal("8"), stockin
    assert pending == Decimal("2"), pending


def test_fulfillment_three_states():
    db = _db()
    today = datetime.now().strftime("%Y-%m-%d")
    yest = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    _seed_product(db, sku="SKU1", spec="SP1")
    db.add(FxStockSnapshot(spec_no="SP1", warehouse="", snap_date=yest, qty=Decimal("5")))  # 昨天库存 5
    # 今日已收货：stockin 2（实际可用=7）；pending 3（到仓待入库）
    po = FxPurchaseOrder(purchase_no="CG-1", status="90", check_time=datetime.now(), abnormal=0)
    db.add(po)
    db.flush()
    db.add(FxPurchaseItem(order_id=po.id, spec_no="SP1", arrive_num=Decimal("5"), stockin_num=Decimal("2")))
    db.commit()

    # 需求 7 → 实际可用 7 → 满足库存
    o1 = _seed_order(db, spec="SP1", qty=7, ono="FX-1")
    r1 = inventory.fulfillment_for_order(db, o1.id, today)
    assert r1["overall_state"] == "满足库存", r1["overall_state"]

    # 需求 9 → 实际 7，但 7+3=10 >= 9 → 到仓待入库
    o2 = _seed_order(db, spec="SP1", qty=9, ono="FX-2")
    r2 = inventory.fulfillment_for_order(db, o2.id, today)
    assert r2["overall_state"] == "到仓待入库", r2["overall_state"]

    # 需求 20 → 实际 7 + pending 3 = 10 < 20 → 库存不足
    o3 = _seed_order(db, spec="SP1", qty=20, ono="FX-3")
    r3 = inventory.fulfillment_for_order(db, o3.id, today)
    assert r3["overall_state"] == "库存不足", r3["overall_state"]


def test_cost_filter_excludes_pending_po():
    """采购单成本过滤修正：待审核(10) 单不得计入商品采购价。"""
    db = _db()
    _seed_product(db, sku="SKU1", spec="SP1")
    now = datetime.now()
    # 已完成(90)：price=5.00（应被采用）
    po_ok = FxPurchaseOrder(purchase_no="CG-OK", status="90", check_time=now, abnormal=0)
    db.add(po_ok)
    db.flush()
    db.add(FxPurchaseItem(order_id=po_ok.id, spec_no="SP1", unit_price=Decimal("5.00"), num=Decimal("1")))
    # 待审核(10)：price=999（绝不能污染成本）
    po_wait = FxPurchaseOrder(purchase_no="CG-WAIT", status="10", check_time=now, abnormal=0)
    db.add(po_wait)
    db.flush()
    db.add(FxPurchaseItem(order_id=po_wait.id, spec_no="SP1", unit_price=Decimal("999.00"), num=Decimal("1")))
    db.commit()

    refresh_product_purchase_prices(db, months=12, apply_to_base_price="none")
    prod = db.scalar(select(FxProduct).where(FxProduct.spec_no == "SP1"))
    assert prod.purchase_price == Decimal("5.00"), prod.purchase_price
    assert "10" in PURCHASE_EXCLUDE_COST


class _FakeClient:
    configured = True

    def stock_query(self, start, end, page_size=100):
        return [
            {"spec_no": "SP1", "stock_num": 100.0},
            {"spec_no": "SP2", "available_num": 50.0},
        ]


def test_sync_stock_snapshot_aggregates_and_stores():
    db = _db()
    _seed_product(db, sku="SKU1", spec="SP1")
    today = datetime.now().strftime("%Y-%m-%d")
    res = inventory.sync_stock_snapshot(db, client=_FakeClient(), snap_date=today, dry_run=False)
    assert res["skus"] == 2
    rows = db.scalars(select(FxStockSnapshot).where(FxStockSnapshot.snap_date == today)).all()
    by_spec = {r.spec_no: r.qty for r in rows}
    assert by_spec["SP1"] == Decimal("100")
    assert by_spec["SP2"] == Decimal("50")
    # 重跑同日期应幂等（清后再写）
    inventory.sync_stock_snapshot(db, client=_FakeClient(), snap_date=today, dry_run=False)
    again = db.scalar(
        select(func.count()).select_from(FxStockSnapshot).where(FxStockSnapshot.snap_date == today)
    )
    assert again == 2, again


def test_snapshot_zero_is_real_and_recent_window_wins():
    db = _db()
    class WindowClient:
        configured = True
        def __init__(self):
            self.calls = 0
        def stock_query(self, start, end, page_size=100):
            self.calls += 1
            if self.calls == 1:
                return [
                    {"spec_no": "SP1", "stock_num": 0, "available_num": 88},
                    {"spec_no": "SP2", "available_num": 7},
                    {"spec_no": "SP3"},
                ]
            return [
                {"spec_no": "SP1", "stock_num": 120},
                {"spec_no": "SP2", "available_num": 12},
            ]

    result = inventory.sync_stock_snapshot(
        db, client=WindowClient(), lookback_days=65, dry_run=True
    )
    assert result["skus"] == 2
    assert result["total_qty"] == "7"
