"""牌价导入 + 成本待核对状态。

业务口径（2026-10-27 用户明确）
------------------------------
1. **牌价只有格力高需要维护**，其他品牌不维护。用途只有一个：算分销折扣率。
2. **一切以采购入库价为主算利润**，牌价不参与利润计算。
3. 源单价格录错时，系统里标「待核对」，该商品不算毛利。

牌价换算（实测两条路径等价）
   单价(含税) ÷ 规格 = 每盒含税牌价      209.880324 ÷ 36 = 5.83
   盒(未税) × 1.13   = 每盒含税牌价      5.1593 × 1.13 = 5.83
验证：5.83 × 0.8 = 4.664，正好等于格力高采购单实付价 → 确认牌价口径正确。
"""
from decimal import Decimal

import pytest
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxProduct
from app.services.distribution import _tier1_discount, _tier1_margin
from app.services.product_cost import import_list_prices, set_cost_status


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _make_xlsx(tmp_path, rows):
    """造一份跟品牌订货单同表头的 Excel。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "订货单最新"
    ws.append(
        ["大类", "产品代码", "数量", "条码", "类型", "实开数量", "缺品数量", "特价",
         "产 品 名 称", "规格", "盒（未税）", "单价(含税)", "订货额(含税)", "实际开单(含税)"]
    )
    for row in rows:
        ws.append(row)
    path = tmp_path / "order.xlsx"
    wb.save(path)
    return path


def test_list_price_is_box_price_divided_by_spec(tmp_path, db):
    """牌价 = 单价(含税) ÷ 规格，换算成按盒（与采购价同单位）。"""
    path = _make_xlsx(
        tmp_path,
        [["百奇", "512PO10145", "", "6901845048438", "恒温", 0, 0, 1,
          "47G*36百奇巧克力味装饰饼干26S", 36, 5.1593, 209.880324, 0, 0]],
    )
    db.add(FxProduct(sku="6901845048438", name="百奇巧克力 47g", barcode="6901845048438",
                     purchase_price=Decimal("4.66")))
    db.commit()

    result = import_list_prices(db, path, dry_run=False)
    assert result["updated"] == 1
    product = db.query(FxProduct).filter_by(sku="6901845048438").one()
    # 209.880324 / 36 = 5.830009 → 5.83
    assert product.list_price == Decimal("5.83")
    # 采购价 / 牌价 ≈ 0.8，验证 8 折拿货
    assert (Decimal("4.66") / product.list_price).quantize(Decimal("0.01")) == Decimal("0.80")


def test_falls_back_to_box_ex_tax_when_spec_missing(tmp_path, db):
    """规格列为空时退回 盒(未税) × 1.13，不能直接丢掉这一行。"""
    path = _make_xlsx(
        tmp_path,
        [["百奇", "X1", "", "6900000000001", "恒温", 0, 0, 1, "某饼干", "", 5.1593, 0, 0, 0]],
    )
    db.add(FxProduct(sku="6900000000001", name="某饼干", barcode="6900000000001"))
    db.commit()
    import_list_prices(db, path, dry_run=False)
    product = db.query(FxProduct).filter_by(sku="6900000000001").one()
    assert product.list_price == Decimal("5.83")


def test_unmatched_barcode_does_not_crash(tmp_path, db):
    """表里有、系统里没有的条码只统计，不报错。"""
    path = _make_xlsx(
        tmp_path,
        [
            ["百奇", "X1", "", "6900000000001", "恒温", 0, 0, 1, "有的", 36, 5.1593, 185.76, 0, 0],
            ["百奇", "X2", "", "6900000000002", "恒温", 0, 0, 1, "没有的", 36, 5.1593, 185.76, 0, 0],
        ],
    )
    db.add(FxProduct(sku="6900000000001", name="有的", barcode="6900000000001"))
    db.commit()
    result = import_list_prices(db, path, dry_run=False)
    assert result["updated"] == 1
    assert result["unmatched_count"] == 1
    assert result["unmatched"][0]["barcode"] == "6900000000002"


def test_barcode_read_as_float_is_trimmed(tmp_path, db):
    """Excel 数字单元格会把条码变成 6901845048438.0，必须去掉尾部 .0 才能匹配。"""
    path = _make_xlsx(
        tmp_path,
        [["百奇", "X1", "", 6901845048438, "恒温", 0, 0, 1, "某饼干", 36, 5.1593, 185.76, 0, 0]],
    )
    db.add(FxProduct(sku="6901845048438", name="某饼干", barcode="6901845048438"))
    db.commit()
    result = import_list_prices(db, path, dry_run=False)
    assert result["updated"] == 1


def test_missing_header_raises(tmp_path):
    """表头缺「条码」直接报错，别静默当成 0 条。"""
    wb = Workbook()
    ws = wb.active
    ws.append(["错误表头", "别的列"])
    ws.append(["x", "y"])
    path = tmp_path / "bad.xlsx"
    wb.save(path)
    with pytest.raises(ValueError, match="条码"):
        import_list_prices(None, path)


# ---------------------------------------------------------------- 待核对
def test_pending_cost_hides_margin():
    """成本待核对时不算毛利 —— 拿错价算利润比不算更糟。"""
    result = _tier1_margin(Decimal("16.88"), Decimal("19.00"), "purchase", "pending")
    assert result["tier1_margin"] == ""
    assert result["tier1_cost_pending"] is True


def test_normal_cost_shows_margin():
    result = _tier1_margin(Decimal("16.88"), Decimal("19.00"), "purchase", "")
    assert result["tier1_margin"] == "2.12"
    assert result["tier1_cost_pending"] is False


def test_set_cost_status_marks_and_clears(db):
    db.add_all([
        FxProduct(sku="A1", name="甲", barcode="A1"),
        FxProduct(sku="A2", name="乙", barcode="A2"),
        FxProduct(sku="B1", name="丙", barcode="B1"),
    ])
    db.commit()

    marked = set_cost_status(db, skus=["A1", "A2"], status="pending", note="源单录反")
    assert marked["updated"] == 2
    assert db.query(FxProduct).filter_by(sku="A1").one().cost_status == "pending"
    assert db.query(FxProduct).filter_by(sku="B1").one().cost_status == ""

    cleared = set_cost_status(db, skus=["A1", "A2"], status="")
    assert cleared["updated"] == 2
    assert db.query(FxProduct).filter_by(sku="A1").one().cost_status == ""
    assert db.query(FxProduct).filter_by(sku="A1").one().cost_note == ""


def test_set_cost_status_by_brand(db):
    db.add_all([
        FxProduct(sku="S1", name="瑞士莲甲", brand="瑞士莲/lindt"),
        FxProduct(sku="S2", name="瑞士莲乙", brand="瑞士莲/lindt"),
        FxProduct(sku="G1", name="格力高丙", brand="格力高/Glico"),
    ])
    db.commit()
    result = set_cost_status(db, brand="瑞士莲", status="pending", note="待核")
    assert result["updated"] == 2


def test_set_cost_status_requires_scope(db):
    with pytest.raises(ValueError):
        set_cost_status(db, status="pending")


# ---------------------------------------------------------------- 折扣率
def test_tier1_discount_in_chinese_discount():
    """折扣率按「几折」表达：分销 4.95 / 牌价 5.83 = 8.49 折。"""
    assert _tier1_discount(Decimal("4.95"), Decimal("5.83")) == "8.49"
    assert _tier1_discount(Decimal("5.83"), Decimal("5.83")) == "10.00"


def test_tier1_discount_empty_without_list_price():
    """牌价没维护（非格力高品牌）就留空，不返回 0 或报错。"""
    assert _tier1_discount(Decimal("4.95"), Decimal("0")) == ""
    assert _tier1_discount(Decimal("0"), Decimal("5.83")) == ""
