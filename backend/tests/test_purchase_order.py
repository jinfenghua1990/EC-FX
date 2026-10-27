"""采购单入库 + 采购价聚合：口径、异常单剔除、补零价行为。

背景（一定先读，否则很容易把价格口径搞反）
--------------------------------------
商品档案接口只有 wholesale / retail / market / lowest 四价，没有采购价。
wholesale_price 被当「品牌原价（含税）」，但它等于**实际拿货价**（已打折），
而不是牌价。所以以前「拿牌价 ×0.8 反推成本」这条思路本身就是错的。

真实成本只能从采购单来：`purchase_order_query.php` → `details_list[].price`，
含税、按基本单位、已折后。实测它与本地 base_price 在 40 个有价 SKU 中 35 个精确命中。

本测试锁定 4 件事
----------------
1. 只认 price/tax_price，不认 purchase_price（后者口径不稳定）
2. 异常单（次品/残次/报废…）不参与算价，但仍入库保留溯源
3. 同一 SKU 多次采购取「最近一次」，不平均
4. 补零价只补 base_price=0 的，不覆盖已维护的价
"""
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxProduct, FxPurchaseOrder
from app.services.distribution import _price_gap
from app.services.wdt_purchase import (
    _abnormal_reason,
    _dt,
    aggregate_prices_from_orders,
    refresh_product_purchase_prices,
    upsert_purchase_orders,
)


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


def _order(no="CG001", spec="6901", price="4.6641", status="90", check="2026-10-20 10:00:00",
           warehouse="磐石-格力高", remark="", provider="格力高", amount=""):
    # amount 留空时按 price × 36 估，用来区分各仓采购金额（选主仓要用）
    line_amount = amount or str(Decimal(price) * 36)
    return {
        "purchase_id": no[-6:],
        "purchase_no": no,
        "warehouse_name": warehouse,
        "warehouse_no": "PS-1",
        "provider_name": provider,
        "provider_no": "GL",
        "status": status,
        "check_time": check,
        "created": check,
        "goods_count": "36",
        "goods_amount": line_amount,
        "remark": remark,
        "details_list": [
            {
                "rec_id": "1",
                "spec_no": spec,
                "goods_no": spec,
                "goods_name": "百奇蜜桃味装饰饼干55g",
                "spec_name": "默认规格",
                "brand_name": "格力高",
                "base_unit": "盒",
                "purchase_unit": "36/箱",
                "unit_ratio": "36",
                "num": "36",
                "num2": "1",
                "price": price,
                "tax_price": price,
                "amount": line_amount,
                "purchase_price": "999.99",  # 故意给个离谱值：不能被当成成本
                "last_price": "4.66",
                "lowest_price": "4.60",
                "arrive_num": "0",
                "stockin_num": "36",
                "warehouse_id": "36",
            }
        ],
    }


def _product(db, sku="6901", base="0", purchase="0"):
    row = FxProduct(
        sku=sku,
        name="百奇蜜桃味装饰饼干55g",
        base_price=Decimal(base),
        purchase_price=Decimal(purchase),
        spec_no=sku,
    )
    db.add(row)
    db.commit()
    return row


# ---------------------------------------------------------------- 单值工具
def test_dt_treats_zero_date_as_null():
    """旺店通未审核单给 "0000-00-00 00:00:00"，必须当空，不能崩。"""
    assert _dt("0000-00-00 00:00:00") is None
    assert _dt("") is None
    assert _dt("2026-10-20 10:00:00") == datetime(2026, 10, 20, 10, 0, 0)


@pytest.mark.parametrize(
    "warehouse,remark,expect",
    [
        ("磐石-格力高", "", ""),
        ("磐石-瑞士莲（京东专供-次品)", "", "次品"),
        ("磐石-格力高", "残次42袋-需要等次月统一处理", "残次"),
        ("磐石-格力高", "赠品不入账", "赠品"),
    ],
)
def test_abnormal_reason(warehouse, remark, expect):
    assert _abnormal_reason(_order(warehouse=warehouse, remark=remark)) == expect


def test_price_gap():
    """品牌原价 − 采购价：任一侧为 0 时留空，避免把「没价」显示成「差 0」。"""
    assert _price_gap(Decimal("10"), Decimal("0")) == ""
    assert _price_gap(Decimal("0"), Decimal("10")) == ""
    assert _price_gap(Decimal("10"), Decimal("8.5")) == "1.50"
    assert _price_gap(Decimal("88"), Decimal("103")) == "-15.00"


# ---------------------------------------------------------------- 聚合口径
def test_uses_price_not_purchase_price():
    """成本只认 price/tax_price；purchase_price=999.99 是干扰项，不能进结果。"""
    prices = aggregate_prices_from_orders([_order(price="4.6641")])
    assert prices["6901"]["price"] == Decimal("4.6641")
    assert prices["6901"]["price"] != Decimal("999.99")


def test_excludes_abnormal_orders():
    """次品单价格不能当成本：SKU 只有异常单时，宁可不给价。"""
    orders = [_order(warehouse="磐石-瑞士莲（京东专供-次品)", price="103")]
    assert aggregate_prices_from_orders(orders) == {}


def test_excludes_unapproved_orders():
    """status=0 未审核，价格未生效。"""
    assert aggregate_prices_from_orders([_order(status="0")]) == {}


def test_latest_purchase_wins_and_range_kept():
    """多次采购取最近一次价，保留区间（min/max/次数）供页面提示波动。

    varies 的语义（2026-10-27 按业务口径调整）：
    只有超出 ±1 元正常浮动才算「价格变了」。5.00→4.50→4.80 全程波动 0.5 元，
    属于用户明确说的「上下 1 元的浮动是正常的」，因此 varies 应为 False。
    """
    orders = [
        _order(no="CG001", price="5.00", check="2026-08-01 10:00:00"),
        _order(no="CG002", price="4.50", check="2026-09-01 10:00:00"),
        _order(no="CG003", price="4.80", check="2026-10-01 10:00:00"),
    ]
    info = aggregate_prices_from_orders(orders)["6901"]
    assert info["price"] == Decimal("4.80")
    assert info["times"] == 3
    assert info["min_price"] == Decimal("4.50")
    assert info["max_price"] == Decimal("5.00")
    assert info["varies"] is False  # 0.5 元波动在 ±1 元容忍度内
    assert info["purchase_no"] == "CG003"


def test_varies_when_price_moves_more_than_one_yuan():
    """超过 1 元才算真有价格变动（成本涨/跌要反映）。"""
    orders = [
        _order(no="CG001", price="5.00", check="2026-08-01 10:00:00"),
        _order(no="CG002", price="7.50", check="2026-10-01 10:00:00"),
    ]
    info = aggregate_prices_from_orders(orders)["6901"]
    assert info["price"] == Decimal("7.50")
    assert info["varies"] is True


def test_special_price_not_used_as_cost():
    """特价单（低于正常价 1 元以上）不能当常规成本。

    用户口径：「假如有一天特别便宜，说明当时拿了一个特价商品」。
    正常价 5.00，某天 2.00 拿了一批特价 —— 如果按最新价算成本，
    毛利会被这一笔虚高一倍多，所以成本仍按正常价。
    """
    orders = [
        _order(no="CG001", price="5.00", check="2026-08-01 10:00:00"),
        _order(no="CG002", price="5.00", check="2026-09-01 10:00:00"),
        _order(no="CG003", price="2.00", check="2026-10-01 10:00:00"),  # 特价
    ]
    info = aggregate_prices_from_orders(orders)["6901"]
    assert info["latest_price"] == Decimal("2.00")   # 最新确实是特价
    assert info["price"] == Decimal("5.00")          # 但成本按正常价
    assert info["special_count"] == 1
    assert "特价" in info["special_note"]


def test_price_rise_is_reflected_as_cost():
    """涨价超过 1 元要用最新价 —— 成本涨了必须跟着反映，不能当特价处理。"""
    orders = [
        _order(no="CG001", price="5.00", check="2026-08-01 10:00:00"),
        _order(no="CG002", price="6.50", check="2026-10-01 10:00:00"),
    ]
    info = aggregate_prices_from_orders(orders)["6901"]
    assert info["price"] == Decimal("6.50")
    assert info["special_count"] == 0


def test_prices_grouped_by_warehouse():
    """同一 SKU 在不同仓的价格分开算，不混成一个「最新价」。

    实测场景：瑞士莲 600g 在京东专供仓 103、自家调拨仓 88，两条线并行存在。
    跨仓取最新会得出「88 涨到 103」这种不存在的结论。
    """
    orders = [
        _order(no="CG001", price="88.00", check="2026-01-01 10:00:00", warehouse="自家仓", amount="8800"),
        _order(no="CG002", price="103.00", check="2026-10-01 10:00:00", warehouse="京东仓", amount="10300"),
    ]
    info = aggregate_prices_from_orders(orders)["6901"]
    assert info["warehouses"] == 2
    warehouses = {e["warehouse"]: e["price"] for e in info["by_warehouse"]}
    assert warehouses == {"自家仓": Decimal("88.00"), "京东仓": Decimal("103.00")}
    # 主仓按采购金额最大选（京东仓 10300 > 自家仓 8800）
    assert info["warehouse"] == "京东仓"
    assert info["price"] == Decimal("103.00")


def test_virtual_warehouse_excluded_from_primary():
    """无实物仓（虚拟仓/仅记录仓）不能当成本基准，金额再大也排除。"""
    orders = [
        _order(no="CG001", price="14.34", check="2026-01-01 10:00:00", warehouse="HHZ-虚拟仓(仅运营用,无实物)", amount="99999"),
        _order(no="CG002", price="15.00", check="2026-01-02 10:00:00", warehouse="归益-东莞桂格仓", amount="1000"),
    ]
    info = aggregate_prices_from_orders(orders)["6901"]
    assert info["warehouse"] == "归益-东莞桂格仓"
    assert info["price"] == Decimal("15.00")
    excluded = {e["warehouse"]: e["excluded"] for e in info["by_warehouse"]}
    assert excluded["HHZ-虚拟仓(仅运营用,无实物)"] == 1


def test_single_price_not_varies():
    orders = [_order(no="CG001"), _order(no="CG002")]
    assert aggregate_prices_from_orders(orders)["6901"]["varies"] is False


def test_zero_price_detail_skipped():
    assert aggregate_prices_from_orders([_order(price="0")]) == {}


# ---------------------------------------------------------------- 入库
def test_upsert_creates_order_and_items(db):
    result = upsert_purchase_orders(db, [_order()])
    assert result["created"] == 1
    assert result["items"] == 1
    order = db.scalar(select(FxPurchaseOrder).where(FxPurchaseOrder.purchase_no == "CG001"))
    assert order is not None
    assert len(order.items) == 1
    assert order.items[0].unit_price == Decimal("4.6641")
    # purchase_price 原样保留（溯源用），但绝不等于 unit_price
    assert order.items[0].purchase_price == Decimal("999.99")


def test_upsert_is_idempotent(db):
    upsert_purchase_orders(db, [_order()])
    result = upsert_purchase_orders(db, [_order()])
    assert result["created"] == 0
    assert result["updated"] == 1
    rows = db.scalars(select(FxPurchaseOrder)).all()
    assert len(rows) == 1
    assert len(rows[0].items) == 1  # 明细重建，不重复


def test_upsert_marks_abnormal(db):
    upsert_purchase_orders(db, [_order(warehouse="磐石-瑞士莲（京东专供-次品)")])
    order = db.scalar(select(FxPurchaseOrder))
    assert order.abnormal == 1
    assert "次品" in order.abnormal_reason


def test_upsert_dry_run_writes_nothing(db):
    result = upsert_purchase_orders(db, [_order()], dry_run=True)
    assert result["created"] == 1
    assert db.scalar(select(FxPurchaseOrder)) is None


# ---------------------------------------------------------------- 写回商品
def test_refresh_writes_purchase_price_only(db):
    product = _product(db, base="5.05", purchase="0")
    prices = {"6901": {"spec_no": "6901", "price": Decimal("4.66"), "date": datetime(2026, 10, 20),
                       "purchase_no": "CG001", "provider": "格力高", "times": 1,
                       "min_price": Decimal("4.66"), "max_price": Decimal("4.66"), "varies": False}}
    result = refresh_product_purchase_prices(db, prices=prices, apply_to_base_price="none")
    assert result["updated"] == 1
    assert result["base_filled"] == 0
    db.refresh(product)
    assert product.purchase_price == Decimal("4.66")
    assert product.base_price == Decimal("5.05")  # 品牌原价不动


def test_refresh_empty_only_fills_zero_base(db):
    """apply_to_base_price=empty：只补零价，已有价的商品一个都不动。"""
    zero = _product(db, sku="A1", base="0")
    keep = _product(db, sku="A2", base="9.99")
    prices = {
        "A1": {"spec_no": "A1", "price": Decimal("4.66"), "date": datetime(2026, 10, 20),
               "purchase_no": "CG001", "provider": "", "times": 1,
               "min_price": Decimal("4.66"), "max_price": Decimal("4.66"), "varies": False},
        "A2": {"spec_no": "A2", "price": Decimal("5.00"), "date": datetime(2026, 10, 20),
               "purchase_no": "CG002", "provider": "", "times": 1,
               "min_price": Decimal("5.00"), "max_price": Decimal("5.00"), "varies": False},
    }
    result = refresh_product_purchase_prices(db, prices=prices, apply_to_base_price="empty")
    assert result["base_filled"] == 1
    db.refresh(zero)
    db.refresh(keep)
    assert zero.base_price == Decimal("4.66")
    assert keep.base_price == Decimal("9.99")  # 不覆盖


def test_refresh_overwrite_forces_base(db):
    row = _product(db, base="9.99")
    prices = {"6901": {"spec_no": "6901", "price": Decimal("4.66"), "date": datetime(2026, 10, 20),
                       "purchase_no": "CG001", "provider": "", "times": 1,
                       "min_price": Decimal("4.66"), "max_price": Decimal("4.66"), "varies": False}}
    result = refresh_product_purchase_prices(db, prices=prices, apply_to_base_price="overwrite")
    assert result["base_overwritten"] == 1
    db.refresh(row)
    assert row.base_price == Decimal("4.66")


def test_refresh_rejects_unknown_mode():
    with pytest.raises(ValueError):
        refresh_product_purchase_prices(None, apply_to_base_price="whatever")


def test_refresh_matches_by_spec_no_field(db):
    """手工建品时 sku 可能不等于 spec_no，要用 spec_no 字段兜底匹配。"""
    row = FxProduct(sku="MANUAL-01", name="手工商品", base_price=Decimal("0"),
                    spec_no="6901", purchase_price=Decimal("0"))
    db.add(row)
    db.commit()
    prices = {"6901": {"spec_no": "6901", "price": Decimal("4.66"), "date": datetime(2026, 10, 20),
                       "purchase_no": "CG001", "provider": "", "times": 1,
                       "min_price": Decimal("4.66"), "max_price": Decimal("4.66"), "varies": False}}
    result = refresh_product_purchase_prices(db, prices=prices)
    assert result["updated"] == 1
    db.refresh(row)
    assert row.purchase_price == Decimal("4.66")


def test_refresh_reports_unmatched(db):
    prices = {"9999": {"spec_no": "9999", "price": Decimal("1.00"), "date": datetime(2026, 10, 20),
                       "purchase_no": "CG001", "provider": "", "times": 1,
                       "min_price": Decimal("1.00"), "max_price": Decimal("1.00"), "varies": False}}
    result = refresh_product_purchase_prices(db, prices=prices)
    assert result["updated"] == 0
    assert result["unmatched_count"] == 1
    assert "9999" in result["unmatched_samples"]
