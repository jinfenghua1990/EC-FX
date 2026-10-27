"""销售出库手工调整的回归测试。

原 test_module_hub.py 里与核销返利相关、与模块数据中心相关的用例已随功能下线删除，
这里保留仍然有效的两条：调整不改 RAW、月份写法归一。
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import StockoutOrder
from app.services.adjustments import create_adjustment, effective_stockout_rows, normalize_month


def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


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
        "reason": "口径临时调整",
    })
    rows = effective_stockout_rows(db, "2026-09")
    assert rows[0]["raw"]["goods_total_amount"] == 100.0
    assert rows[0]["effective"]["goods_total_amount"] == 120.0
    # 关键：调整只作用于「生效值」，旺店通原始值必须纹丝不动
    assert db.query(StockoutOrder).filter_by(stockout_no="SO-1").one().goods_total_amount == Decimal("100")


def test_normalize_month_handles_unpadded_and_variants():
    assert normalize_month("2026-9") == "2026-09"
    assert normalize_month("2026-09") == "2026-09"
    assert normalize_month("2026/9") == "2026-09"
    assert normalize_month("2026.9") == "2026-09"
    assert normalize_month("2026年9月") == "2026-09"
    assert normalize_month("") == ""
    # 无法识别的格式原样返回，不擅自改写
    assert normalize_month("2026-13") == "2026-13"
