"""改价流水（fx_price_history）：一级分销价每次真变更都留底。

口径（2026-10-28 用户要求「底层账本用最强的」）：
1. 三条改价路径都要记：表格手改 / 批量折扣率 / 编辑商品弹窗；
2. 值没变不记（避免脏流水）；
3. 批量折扣要带折扣率 + 改价那一刻的标价快照。
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxPriceHistory, FxProduct
from app.services import distribution as dist
from app.services.product_maintenance import tier1_by_discount


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture()
def priced(db):
    """一个有标价的商品。"""
    p = FxProduct(sku="P-HIS-1", name="格力高测试豆", brand="格力高", list_price=Decimal("5.83"))
    db.add(p)
    db.commit()
    return p


def _rows(db, pid):
    return db.scalars(
        select(FxPriceHistory).where(FxPriceHistory.product_id == pid).order_by(FxPriceHistory.id)
    ).all()


def test_manual_save_logs_history(db, priced):
    """表格手改：记 old→new，source=manual，标价快照带上。"""
    dist.set_tier1_prices(db, [{"product_id": priced.id, "tier1_price": "5.00"}])
    rows = _rows(db, priced.id)
    assert len(rows) == 1
    assert rows[0].old_value == "" and rows[0].new_value == "5.00"
    assert rows[0].source == "manual"
    assert rows[0].list_price == Decimal("5.83")


def test_manual_save_no_change_no_row(db, priced):
    """值没变不记流水；清空待定也照记（0→空 也是一次变更）。"""
    dist.set_tier1_prices(db, [{"product_id": priced.id, "tier1_price": ""}])  # 待定→待定? 原值0
    assert len(_rows(db, priced.id)) == 0  # 原 0 → 清空 = 没变

    dist.set_tier1_prices(db, [{"product_id": priced.id, "tier1_price": "5.00"}])
    dist.set_tier1_prices(db, [{"product_id": priced.id, "tier1_price": "5.00"}])  # 同值再存
    rows = _rows(db, priced.id)
    assert len(rows) == 1

    dist.set_tier1_prices(db, [{"product_id": priced.id, "tier1_price": ""}])  # 清空 = 变更
    rows = _rows(db, priced.id)
    assert len(rows) == 2
    assert rows[1].old_value == "5.00" and rows[1].new_value == ""


def test_batch_discount_logs_rate_and_snapshot(db, priced):
    """批量折扣：source=batch，rate 存归一化小数（0.85），标价快照为改价那一刻。"""
    result = tier1_by_discount(db, [priced.id], "8.5", dry_run=False)
    assert result["updated"] == 1
    rows = _rows(db, priced.id)
    assert len(rows) == 1
    assert rows[0].source == "batch"
    assert rows[0].rate == "0.8500"
    assert rows[0].old_value == "" and rows[0].new_value == "4.96"  # 5.83 × 0.85 = 4.955 → 4.96
    assert rows[0].list_price == Decimal("5.83")


def test_update_product_logs_history(db, priced):
    """编辑商品弹窗改一级价：也留底。"""
    dist.update_product(db, priced.id, {"tier1_price": "4.50"})
    rows = _rows(db, priced.id)
    assert len(rows) == 1
    assert rows[0].new_value == "4.50"
    assert rows[0].source == "manual"
