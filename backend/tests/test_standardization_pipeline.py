import json
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import ImportBatch, ModuleRawRecord, ModuleStandardRecord, StockoutOrder
from app.services.module_hub import (
    auto_mapping,
    calculate_purchase_rebate,
    create_adjustment,
    create_rebate_rule,
    effective_stockout_rows,
    module_readiness,
    normalize_module_batch,
)


def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_purchase_mapping_and_normalization():
    db = db_session()
    batch = ImportBatch(kind="module:purchase", filename="purchase.xlsx", sha256="p"*64, status="raw_staged", row_count=1)
    db.add(batch); db.flush()
    raw = {
        "采购单号": "PO-001", "采购日期": "2026-09-08", "品牌": "格力高", "渠道": "快手",
        "采购未税金额": "1,000.00", "采购含税金额": "1060", "商品名称": "测试商品",
    }
    db.add(ModuleRawRecord(batch_id=batch.id, module="purchase", row_no=2, period="2026-09", external_key="PO-001", raw_json=json.dumps(raw, ensure_ascii=False)))
    db.commit()
    mapping = auto_mapping("purchase", list(raw.keys()))
    result = normalize_module_batch(db, batch.id, mapping=mapping)
    assert result["ok"] is True
    assert result["valid_rows"] == 1
    rec = db.query(ModuleStandardRecord).one()
    data = json.loads(rec.standard_json)
    assert data["purchase_no"] == "PO-001"
    assert data["brand"] == "格力高"
    assert data["invoice_amount_untaxed"] == "1000.00"
    assert rec.period == "2026-09"


def test_purchase_rebate_prefers_standardized_purchase_data():
    db = db_session()
    batch = ImportBatch(kind="module:purchase", filename="p.xlsx", sha256="q"*64, status="standardized", row_count=1)
    db.add(batch); db.flush()
    db.add(ModuleStandardRecord(
        batch_id=batch.id, module="purchase", row_no=2, period="2026-09", external_key="PO-1", status="valid",
        standard_json=json.dumps({
            "purchase_no": "PO-1", "brand": "格力高", "channel": "快手",
            "invoice_amount_untaxed": "1000", "invoice_amount_tax": "1060", "list_amount_untaxed": "1200"
        }, ensure_ascii=False),
    ))
    db.commit()
    create_rebate_rule(db, {
        "brand": "格力高", "channel": "快手", "basis_field": "invoice_amount_untaxed",
        "rate": "0.05", "tax_factor": "1.06", "effective_from": "2026-01",
    })
    result = calculate_purchase_rebate(db, "2026-09")
    assert result["ready"] is True
    assert result["basis_source"] == "purchase_orders"
    assert result["purchase_source"] == "purchase_standard_records"
    assert result["legacy_fallback"] is False
    assert round(result["rebate_total"], 2) == 53.00


def test_sales_adjustment_supports_platform_and_multiplier():
    db = db_session()
    db.add(StockoutOrder(
        stockout_no="SO-2", shop_name="测试店", platform_id="douyin", consign_time=datetime(2026, 9, 8),
        goods_total_amount=Decimal("100"), goods_total_cost=Decimal("50"), post_cost=Decimal("5"),
    ))
    db.commit()
    create_adjustment(db, {
        "target_type": "platform", "target_key": "douyin", "period": "2026-09",
        "field_name": "goods_total_cost", "operation": "multiply", "decimal_value": "0.9",
        "reason": "平台月度核销系数测试",
    })
    rows = effective_stockout_rows(db, "2026-09")
    assert rows[0]["raw"]["goods_total_cost"] == 50.0
    assert rows[0]["effective"]["goods_total_cost"] == 45.0


def test_readiness_separates_raw_and_standard_layers():
    db = db_session()
    batch = ImportBatch(kind="module:expenses", filename="e.xlsx", sha256="e"*64, status="raw_staged", row_count=1)
    db.add(batch); db.flush()
    db.add(ModuleRawRecord(batch_id=batch.id, module="expenses", row_no=2, period="2026-09", external_key="仓储", raw_json='{"月份":"2026-09","费用类别":"仓储","费用金额":100}'))
    db.commit()
    states = {x["module"]: x for x in module_readiness(db)}
    assert states["expenses"]["state"] == "mapping"
    assert states["expenses"]["raw_rows"] == 1
    assert states["expenses"]["standard_rows"] == 0


def test_rebate_supports_multiple_components_without_double_counting_same_category():
    db = db_session()
    batch = ImportBatch(kind="module:purchase", filename="p2.xlsx", sha256="z"*64, status="standardized", row_count=1)
    db.add(batch); db.flush()
    db.add(ModuleStandardRecord(
        batch_id=batch.id, module="purchase", row_no=2, period="2026-09", external_key="PO-2", status="valid",
        standard_json=json.dumps({
            "purchase_no": "PO-2", "brand": "格力高", "channel": "快手",
            "invoice_amount_untaxed": "1000", "invoice_amount_tax": "1060", "list_amount_untaxed": "1200"
        }, ensure_ascii=False),
    ))
    db.commit()
    create_rebate_rule(db, {"brand":"*","channel":"*","category":"运营费","basis_field":"invoice_amount_untaxed","rate":"0.01","priority":200})
    create_rebate_rule(db, {"brand":"格力高","channel":"快手","category":"运营费","basis_field":"invoice_amount_untaxed","rate":"0.05","priority":10})
    create_rebate_rule(db, {"brand":"格力高","channel":"快手","category":"广告费","basis_field":"invoice_amount_untaxed","rate":"0.10","priority":20})
    result = calculate_purchase_rebate(db, "2026-09")
    assert round(result["rebate_total"], 2) == 150.00
    assert round(result["category_totals"]["运营费"], 2) == 50.00
    assert round(result["category_totals"]["广告费"], 2) == 100.00


def test_rebate_is_separated_by_purchase_channel():
    db = db_session()
    batch = ImportBatch(kind="module:purchase", filename="channel.xlsx", sha256="c"*64, status="standardized", row_count=2)
    db.add(batch); db.commit(); db.refresh(batch)
    rows = [
        {"purchase_no":"P-KS","purchase_month":"2026-09","channel":"快手","brand":"格力高","invoice_amount_untaxed":"1000"},
        {"purchase_no":"P-DY","purchase_month":"2026-09","channel":"抖音","brand":"格力高","invoice_amount_untaxed":"1000"},
    ]
    for i, data in enumerate(rows, 1):
        db.add(ModuleStandardRecord(batch_id=batch.id,module="purchase",row_no=i,period="2026-09",external_key=data["purchase_no"],standard_json=json.dumps(data, ensure_ascii=False),status="valid"))
    db.commit()
    create_rebate_rule(db,{"brand":"*","channel":"快手","category":"运营费","basis_field":"invoice_amount_untaxed","rate":"0.05"})
    create_rebate_rule(db,{"brand":"*","channel":"抖音","category":"运营费","basis_field":"invoice_amount_untaxed","rate":"0.08"})
    result = calculate_purchase_rebate(db,"2026-09")
    assert round(result["channel_totals"]["快手"],2) == 50.00
    assert round(result["channel_totals"]["抖音"],2) == 80.00
    assert round(result["rebate_total"],2) == 130.00
    assert result["matched_purchase_rows"] == 2


def test_rebate_exact_channel_and_brand_override_wildcards_at_same_priority():
    db = db_session()
    batch = ImportBatch(kind="module:purchase", filename="priority.xlsx", sha256="r"*64, status="standardized", row_count=1)
    db.add(batch); db.commit(); db.refresh(batch)
    data = {"purchase_no":"P-1","purchase_month":"2026-09","channel":"快手","brand":"格力高","invoice_amount_untaxed":"1000"}
    db.add(ModuleStandardRecord(batch_id=batch.id,module="purchase",row_no=1,period="2026-09",external_key="P-1",standard_json=json.dumps(data, ensure_ascii=False),status="valid"))
    db.commit()
    # 全部规则保持默认 priority=100；仍必须是“渠道优先，品牌次优先”。
    create_rebate_rule(db,{"brand":"*","channel":"*","category":"运营费","basis_field":"invoice_amount_untaxed","rate":"0.01"})
    create_rebate_rule(db,{"brand":"*","channel":"快手","category":"运营费","basis_field":"invoice_amount_untaxed","rate":"0.03"})
    create_rebate_rule(db,{"brand":"格力高","channel":"快手","category":"运营费","basis_field":"invoice_amount_untaxed","rate":"0.05"})
    result = calculate_purchase_rebate(db,"2026-09")
    assert round(result["rebate_total"],2) == 50.00
    assert result["lines"][0]["rate"] == 0.05
