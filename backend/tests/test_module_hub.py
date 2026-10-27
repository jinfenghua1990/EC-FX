from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ImportBatch, LegacyPurchaseRow, StockoutOrder
from app.services.module_hub import calculate_purchase_rebate, create_adjustment, create_rebate_rule, effective_stockout_rows, module_catalog, normalize_month


def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_module_catalog_has_stable_endpoints():
    rows = {x["key"]: x for x in module_catalog()}
    assert rows["sales-outbound"]["sync_endpoint"] == "/api/data/sales-outbound/sync"
    assert rows["purchase"]["upload_endpoint"] == "/api/data/purchase/upload"
    assert rows["rebate"]["calculate_endpoint"] == "/api/data/rebate/calculate"


def test_sales_adjustment_does_not_change_raw_order():
    db = db_session()
    order = StockoutOrder(
        stockout_no="SO-1", shop_name="测试店", consign_time=datetime(2026, 9, 8),
        goods_total_amount=Decimal("100"), goods_total_cost=Decimal("50"), post_cost=Decimal("5"),
    )
    db.add(order); db.commit()
    create_adjustment(db, {
        "target_type": "order", "target_key": "SO-1", "period": "2026-09",
        "field_name": "goods_total_amount", "operation": "add", "decimal_value": "20",
        "reason": "核销口径临时调整",
    })
    rows = effective_stockout_rows(db, "2026-09")
    assert rows[0]["raw"]["goods_total_amount"] == 100.0
    assert rows[0]["effective"]["goods_total_amount"] == 120.0
    assert db.query(StockoutOrder).filter_by(stockout_no="SO-1").one().goods_total_amount == Decimal("100")


def test_rebate_is_calculated_from_purchase_basis_and_rule():
    db = db_session()
    batch = ImportBatch(kind="legacy_workbook", filename="x.xlsx", sha256="a"*64, status="imported", row_count=1)
    db.add(batch); db.flush()
    db.add(LegacyPurchaseRow(
        batch_id=batch.id, recon_month="2026-09", purchase_month="2026-09", channel="快手",
        subbrand="格力高", invoice_amount_untaxed=Decimal("1000"), invoice_amount_tax=Decimal("1060"),
        list_amount_untaxed=Decimal("1200"), discount_rate=Decimal("0.8"), qty_units=Decimal("1"),
    ))
    db.commit()
    create_rebate_rule(db, {
        "brand": "格力高", "channel": "快手", "basis_field": "invoice_amount_untaxed",
        "rate": "0.05", "tax_factor": "1.06", "effective_from": "2026-01",
        "note": "测试返利",
    })
    result = calculate_purchase_rebate(db, "2026-09")
    assert result["ready"] is True
    assert round(result["rebate_total"], 2) == 53.00
    assert result["basis_source"] == "purchase_orders"


def test_normalize_month_handles_unpadded_and_variants():
    assert normalize_month("2026-9") == "2026-09"
    assert normalize_month("2026-09") == "2026-09"
    assert normalize_month("2026/9") == "2026-09"
    assert normalize_month("2026.9") == "2026-09"
    assert normalize_month("2026年9月") == "2026-09"
    assert normalize_month("") == ""
    # 无法识别的格式原样返回，不擅自改写
    assert normalize_month("2026-13") == "2026-13"


def test_rebate_rule_create_normalizes_effective_month():
    db = db_session()
    rule = create_rebate_rule(db, {
        "brand": "格力高", "channel": "快手", "basis_field": "invoice_amount_untaxed",
        "rate": "0.05", "tax_factor": "1.06", "effective_from": "2026-9", "effective_to": "2026-9",
    })
    assert rule.effective_from == "2026-09"
    assert rule.effective_to == "2026-09"


def test_rebate_rule_unpadded_effective_month_matches_correctly():
    db = db_session()
    batch = ImportBatch(kind="legacy_workbook", filename="x.xlsx", sha256="a"*64, status="imported", row_count=1)
    db.add(batch); db.flush()
    for recon_month in ("2026-09", "2026-10"):
        db.add(LegacyPurchaseRow(
            batch_id=batch.id, recon_month=recon_month, purchase_month=recon_month, channel="快手",
            subbrand="格力高", invoice_amount_untaxed=Decimal("1000"), invoice_amount_tax=Decimal("1060"),
            list_amount_untaxed=Decimal("1200"), discount_rate=Decimal("0.8"), qty_units=Decimal("1"),
        ))
    db.commit()
    # 用户手填非补零的生效区间：仅 2026 年 9 月有效
    create_rebate_rule(db, {
        "brand": "格力高", "channel": "快手", "basis_field": "invoice_amount_untaxed",
        "rate": "0.05", "tax_factor": "1.06", "effective_from": "2026-9", "effective_to": "2026-9",
    })
    sept = calculate_purchase_rebate(db, "2026-09")
    assert sept["ready"] is True
    assert round(sept["rebate_total"], 2) == 53.00   # 区间命中 9 月
    oct_result = calculate_purchase_rebate(db, "2026-10")
    assert oct_result["ready"] is True
    assert round(oct_result["rebate_total"], 2) == 0.00  # 区间结束于 9 月，10 月不应误命中
    assert oct_result["unmatched_rows"] == 1
