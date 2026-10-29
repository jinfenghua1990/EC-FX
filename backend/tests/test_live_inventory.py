"""旺店通实时库存按选定仓库和商品规格核对，不使用跨仓汇总或昨日快照。"""
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxDistributor, FxOrder, FxOrderLine, FxProduct
from app.services.live_inventory import (
    LiveInventoryError, preview_live_inventory, verify_order_for_push,
    fulfillment_live_for_order,
)
from app.services.wangdian import WangdianClient


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add_all([
        FxProduct(sku="LOCAL-SKU-1", goods_no="G001", spec_no="SPEC-001", name="商品1"),
        FxProduct(sku="LOCAL-SKU-2", goods_no="G002", spec_no="SPEC-002", name="商品2"),
        FxDistributor(code="D1", name="客户1"),
    ])
    db.commit()
    yield db
    db.close()


class FakeWdt:
    configured = True
    def __init__(self):
        self.calls = []
        self.available = {
            ("WH-A", "SPEC-001"): "2.0000",
            ("WH-A", "SPEC-002"): "1.0000",
            ("WH-B", "SPEC-001"): "99.0000",
        }
        self.bad = None

    def warehouses(self):
        return [
            {"warehouse_no": "WH-A", "name": "仓库A", "is_disabled": "0"},
            {"warehouse_no": "WH-B", "name": "仓库B", "is_disabled": "0"},
            {"warehouse_no": "DISABLED", "name": "停用仓", "is_disabled": "1"},
        ]

    def stock_by_spec_warehouse(self, warehouse_no, spec_no):
        self.calls.append((warehouse_no, spec_no))
        if self.bad == "connection":
            raise RuntimeError("接口异常")
        if self.bad == "wrong_warehouse":
            return [{"warehouse_no": "WH-B", "spec_no": spec_no,
                     "avaliable_num": "900", "stock_num": "900"}]
        if self.bad == "missing_available":
            return [{"warehouse_no": warehouse_no, "spec_no": spec_no,
                     "stock_num": "900"}]
        qty = self.available.get((warehouse_no, spec_no))
        return [] if qty is None else [{
            "warehouse_no": warehouse_no, "spec_no": spec_no,
            "stock_num": "300.0000", "avaliable_num": qty,
        }]


def _product(session, spec):
    return session.query(FxProduct).filter_by(spec_no=spec).one()


def test_selected_warehouse_only_not_sum_cross_warehouse(session):
    client = FakeWdt()
    product = _product(session, "SPEC-001")
    a = preview_live_inventory(session, "WH-A",
        [{"product_id": product.id, "quantity": "3"}], client)
    assert a["warehouse_no"] == "WH-A"
    assert a["items"][0]["available"] == "2.0000"
    assert a["items"][0]["physical"] == "300.0000"
    assert not a["enough"]
    b = preview_live_inventory(session, "WH-B",
        [{"product_id": product.id, "quantity": "3"}], client)
    assert b["enough"] and b["items"][0]["available"] == "99.0000"
    assert client.calls == [("WH-A", "SPEC-001"), ("WH-B", "SPEC-001")]


def test_absent_sku_is_zero_only_after_successful_exact_query(session):
    client = FakeWdt()
    p = _product(session, "SPEC-002")
    result = preview_live_inventory(session, "WH-B",
        [{"product_id": p.id, "quantity": 1}], client)
    assert result["items"][0]["available"] == "0"
    assert result["items"][0]["found"] is False
    assert not result["enough"]


def test_wrong_warehouse_and_missing_available_fail_closed(session):
    p = _product(session, "SPEC-001")
    for invalid in ("wrong_warehouse", "missing_available", "connection"):
        client = FakeWdt()
        client.bad = invalid
        with pytest.raises((LiveInventoryError, RuntimeError)):
            preview_live_inventory(session, "WH-A",
                [{"product_id": p.id, "quantity": 1}], client)
    client = FakeWdt()
    with pytest.raises(LiveInventoryError, match="不在旺店通启用仓库"):
        preview_live_inventory(session, "DISABLED",
            [{"product_id": p.id, "quantity": 1}], client)


def test_two_lines_same_spec_sum_demand_before_compare(session):
    client = FakeWdt()
    p = _product(session, "SPEC-001")
    result = preview_live_inventory(session, "WH-A",
        [{"product_id": p.id, "quantity": "1.5"},
         {"product_id": p.id, "quantity": "1.5"}], client)
    assert not result["enough"]
    assert result["items"][0]["demand_total"] == "3.0"
    assert client.calls == [("WH-A", "SPEC-001")]


def test_zero_available_cannot_fall_back_to_physical(session):
    client = FakeWdt()
    client.available[("WH-A", "SPEC-001")] = "0"
    p = _product(session, "SPEC-001")
    result = preview_live_inventory(session, "WH-A",
        [{"product_id": p.id, "quantity": 1}], client)
    assert result["items"][0]["physical"] == "300.0000"
    assert result["items"][0]["available"] == "0"
    assert not result["enough"]


def test_existing_order_uses_its_selected_warehouse(session):
    client = FakeWdt()
    p = _product(session, "SPEC-001")
    d = session.query(FxDistributor).first()
    order = FxOrder(order_no="FXLIVE-1", distributor_id=d.id,
                    wdt_warehouse_no="WH-A", status="approved",
                    total_amount=Decimal("20"))
    order.lines = [FxOrderLine(product_id=p.id, spec_no="SPEC-001", goods_no="G001",
                               quantity=Decimal("3"))]
    session.add(order)
    session.commit()
    report = fulfillment_live_for_order(session, order.id, client)
    assert report["warehouse_no"] == "WH-A"
    assert report["overall_state"] == "库存不足"
    with pytest.raises(LiveInventoryError, match="库存不足"):
        verify_order_for_push(session, order, client)
    order.wdt_warehouse_no = "WH-B"
    session.commit()
    report = fulfillment_live_for_order(session, order.id, client)
    assert report["overall_state"] == "满足库存"
    assert report["lines"][0]["available"] == "99.0000"


def test_wdt_request_uses_exact_warehouse_and_spec_and_stocks_node(monkeypatch):
    client = WangdianClient()
    calls = []
    def request(endpoint, params):
        calls.append((endpoint, dict(params)))
        return {"stocks": [{
            "warehouse_no": "WH-A", "spec_no": "SPEC-001",
            "stock_num": "12", "avaliable_num": "3"
        }]}
    monkeypatch.setattr(client, "_request", request)
    rows = client.stock_by_spec_warehouse("WH-A", "SPEC-001")
    assert len(rows) == 1
    assert calls == [("stock_query.php", {
        "warehouse_no": "WH-A", "spec_no": "SPEC-001", "is_deleted": 1,
        "spec_is_deleted": 0, "page_no": 0, "page_size": 100,
    })]
    monkeypatch.setattr(client, "_request", lambda *args: {"stock_list": []})
    with pytest.raises(RuntimeError, match="stocks"):
        client.stock_by_spec_warehouse("WH-A", "SPEC-001")
