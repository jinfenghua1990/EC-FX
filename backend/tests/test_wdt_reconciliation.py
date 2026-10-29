"""分销订单与旺店通出库单按规格编码核对（含历史旧数据兼容）。"""
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxDistributor, FxProduct, FxOrder, FxOrderLine, StockoutOrder, StockoutItem
from app.services.distribution import reconcile_orders_stockout


def test_reconciliation_matches_wdt_spec_and_legacy_snapshot():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    distributor = FxDistributor(code="D1", name="客户")
    product = FxProduct(sku="SPEC-001", spec_no="SPEC-001", goods_no="GOODS-001",
                        name="商品", spec="36盒")
    db.add_all([distributor, product])
    db.flush()
    for index, legacy in enumerate((False, True), start=1):
        order = FxOrder(order_no=f"FX-ORDER-{index}", distributor_id=distributor.id,
                        status="approved", total_amount=Decimal("50"),
                        wdt_tid=f"TID-{index}")
        order.lines = [FxOrderLine(
            product_id=product.id,
            goods_no="SPEC-001" if legacy else "GOODS-001",
            spec_no="36盒" if legacy else "SPEC-001",
            goods_name="商品", spec_name="36盒", quantity=Decimal("5"),
            unit_price=Decimal("10"), subtotal=Decimal("50"),
        )]
        stockout = StockoutOrder(stockout_no=f"CK-{index}", trade_no=f"TID-{index}",
                                 goods_total_amount=Decimal("50"))
        stockout.items = [StockoutItem(
            spec_no="SPEC-001", goods_no="GOODS-001",
            goods_name="商品", goods_count=Decimal("5"),
        )]
        db.add_all([order, stockout])
    db.commit()
    result = reconcile_orders_stockout(db)
    assert result["stats"]["match"] == 2, result
    assert result["stats"]["mismatch"] == 0, result
    db.close()
