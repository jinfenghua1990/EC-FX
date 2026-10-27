from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import StockoutItem, StockoutOrder
from app.services.module_hub import create_adjustment
from app.services.sales_outbound import sales_outbound_detail, sales_outbound_facets, sales_outbound_orders, sales_outbound_overview


def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def seed(db):
    a = StockoutOrder(
        stockout_no="SO-A", trade_no="T-A", src_trade_no="SRC-A", shop_name="快手旗舰店", platform_id="ks",
        consign_time=datetime(2026, 9, 8, 10), status="95", refund_status="0",
        goods_count=Decimal("2"), goods_total_amount=Decimal("200"), goods_total_cost=Decimal("100"),
        post_cost=Decimal("10"), package_cost=Decimal("2"), actual_receivable=Decimal("190"),
    )
    b = StockoutOrder(
        stockout_no="SO-B", trade_no="T-B", src_trade_no="SRC-B", shop_name="抖音旗舰店", platform_id="dy",
        consign_time=datetime(2026, 9, 9, 11), status="95", refund_status="1",
        goods_count=Decimal("1"), goods_total_amount=Decimal("120"), goods_total_cost=Decimal("60"),
        post_cost=Decimal("8"), package_cost=Decimal("1"), actual_receivable=Decimal("110"),
    )
    db.add_all([a, b]); db.flush()
    db.add(StockoutItem(order_id=a.id, spec_no="SKU-1", barcode="690000000001", goods_name="测试商品", goods_count=Decimal("2"), sell_price=Decimal("100"), share_amount=Decimal("200"), cost_price=Decimal("50")))
    db.commit()


def test_sales_workspace_overview_uses_effective_values():
    db = db_session(); seed(db)
    create_adjustment(db, {"target_type":"order","target_key":"SO-A","period":"2026-09","field_name":"goods_total_amount","operation":"add","decimal_value":"20","reason":"测试调整"})
    d = sales_outbound_overview(db, "2026-09")
    assert d["orders"] == 2
    assert d["adjusted_orders"] == 1
    assert d["raw_totals"]["goods_total_amount"] == 320.0
    assert d["effective_totals"]["goods_total_amount"] == 340.0
    assert d["effective_gross_profit"] == 159.0


def test_sales_workspace_filters_and_pagination():
    db = db_session(); seed(db)
    d = sales_outbound_orders(db, "2026-09", shop="快手旗舰店", page=1, page_size=50)
    assert d["total"] == 1
    assert d["rows"][0]["stockout_no"] == "SO-A"
    d2 = sales_outbound_orders(db, "2026-09", keyword="SRC-B", page=1, page_size=50)
    assert d2["total"] == 1
    assert d2["rows"][0]["stockout_no"] == "SO-B"


def test_sales_workspace_detail_includes_items_and_facets():
    db = db_session(); seed(db)
    d = sales_outbound_detail(db, "SO-A")
    assert d["items"][0]["barcode"] == "690000000001"
    f = sales_outbound_facets(db, "2026-09")
    assert f["shops"] == ["快手旗舰店", "抖音旗舰店"]
    assert f["platforms"] == ["dy", "ks"]
