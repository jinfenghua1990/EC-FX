"""旺店通商品档案同步的单元测试。

重点覆盖项目里踩过的坑：
- total_count 不可信（大结果集返回 0），不能用来判停
- 同步不能覆盖人工维护的基准价
- 重复同步必须幂等（以 spec_no 为唯一键）
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxProduct
from app.services import wdt_products as wp


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


GOODS = [
    {
        "goods_no": "G001",
        "goods_name": "55G*36百奇巧克力味装饰饼干",
        "class_name": "百奇",
        "brand_name": "格力高",
        "unit_name": "箱",
        "spec_list": [
            {"spec_no": "512PO10023", "spec_name": "55G*36", "barcode": "6901845045062", "retail_price": "209.88"},
            {"spec_no": "512PO10028", "spec_name": "50G*36", "barcode": "6901845045079", "sale_price": "199.50"},
        ],
    },
    {
        "goods_no": "G002",
        "goods_name": "48G*24百醇",
        "class_name": "百醇",
        "brand_name": "格力高",
        "spec_list": [{"spec_no": "512PO20001", "spec_name": "48G*24", "barcode": "6901845000001"}],
    },
]


class FakeClient:
    """模拟 WangdianClient：configured 为 True，_request 按 page_no 返回预置分页。"""

    configured = True

    def __init__(self, pages, total_count=0):
        self.pages = pages
        self.total_count = total_count
        self.calls = 0

    def _request(self, endpoint, params):
        self.calls += 1
        page_no = int(params.get("page_no") or 0)
        rows = self.pages[page_no] if page_no < len(self.pages) else []
        # total_count 故意给 0：真实接口在大结果集时会这样，用它判停会提前终止
        return {"goods_list": rows, "total_count": self.total_count}


def test_normalize_flattens_specs_to_one_row_each():
    products = wp.normalize_products(GOODS)
    assert [p["sku"] for p in products] == ["512PO10023", "512PO10028", "512PO20001"]
    first = products[0]
    assert first["spec_no"] == "512PO10023"
    assert first["goods_no"] == "G001"
    assert first["category"] == "百奇"
    assert first["brand"] == "格力高"
    assert first["unit"] == "箱"
    assert first["barcode"] == "6901845045062"
    # 规格级价格优先：retail_price / sale_price 两个键都要能取到
    assert first["base_price"] == Decimal("209.88")
    assert products[1]["base_price"] == Decimal("199.50")
    # 没有价格时留空（None），不编造 0
    assert products[2]["base_price"] is None


def test_normalize_skips_spec_without_code():
    goods = [{"goods_no": "G001", "goods_name": "无编码货品", "spec_list": [{"spec_name": "x"}]}]
    assert wp.normalize_products(goods) == []


def test_upsert_is_idempotent_by_spec_no(db):
    products = wp.normalize_products(GOODS)
    first = wp.upsert_products(db, products)
    assert (first["created"], first["updated"]) == (3, 0)
    assert db.scalar(select(func.count()).select_from(FxProduct)) == 3

    second = wp.upsert_products(db, products)
    assert (second["created"], second["updated"]) == (0, 3)
    # 不产生重复商品
    assert db.scalar(select(func.count()).select_from(FxProduct)) == 3


def test_sync_marks_source_and_synced_at(db):
    wp.upsert_products(db, wp.normalize_products(GOODS))
    row = db.scalar(select(FxProduct).where(FxProduct.sku == "512PO10023"))
    assert row.source == "wdt"
    assert row.spec_no == "512PO10023"
    assert row.wdt_synced_at is not None


def test_upsert_keeps_manual_price_by_default(db):
    db.add(FxProduct(sku="512PO10023", name="人工维护商品", base_price=Decimal("188.00")))
    db.commit()

    result = wp.upsert_products(db, wp.normalize_products(GOODS))
    row = db.scalar(select(FxProduct).where(FxProduct.sku == "512PO10023"))
    # 人工价被保留，旺店通价不覆盖
    assert row.base_price == Decimal("188.00")
    assert result["price_kept"] == 1


def test_upsert_overwrite_price_when_asked(db):
    db.add(FxProduct(sku="512PO10023", name="人工维护商品", base_price=Decimal("188.00")))
    db.commit()

    wp.upsert_products(db, wp.normalize_products(GOODS), overwrite_price=True)
    row = db.scalar(select(FxProduct).where(FxProduct.sku == "512PO10023"))
    assert row.base_price == Decimal("209.88")


def test_dry_run_writes_nothing(db):
    result = wp.upsert_products(db, wp.normalize_products(GOODS), dry_run=True)
    assert result["dry_run"] is True
    assert result["created"] == 3
    assert db.scalar(select(func.count()).select_from(FxProduct)) == 0


def test_fetch_does_not_trust_total_count(db, monkeypatch):
    def mk(no):
        return {"goods_no": no, "goods_name": f"货品{no}", "spec_list": [{"spec_no": f"SKU-{no}", "spec_name": "x"}]}

    page0 = [mk("G001"), mk("G002")]
    page1 = [mk("G003"), mk("G004")]
    fake = FakeClient(pages=[page0, page1], total_count=0)
    monkeypatch.setattr(wp, "WangdianClient", lambda: fake)

    rows, calls = wp.fetch_goods(
        fake, start="2026-01-01 00:00:00", end="2026-01-20 00:00:00", page_size=2
    )
    # total_count=0 时若据此判停只会拿到第一页；正确实现应翻到空页才停
    assert len(rows) == 4
    assert calls == 3


def test_windows_never_exceed_30_days_and_have_no_gap():
    """goods_query.php 单次跨度上限 30 天（实测超一天即报 2180），窗口必须首尾相接。"""
    from datetime import datetime

    start = datetime(2025, 1, 1, 0, 0, 0)
    end = datetime(2025, 4, 15, 0, 0, 0)
    windows = list(wp._time_windows(start, end))
    assert windows, "至少要切出一个窗口"
    # 窗口是从 end 往回扫的：第一个是最新窗口，最后一个是（被裁剪到 start 的）最早窗口
    assert windows[0][1] == end
    assert windows[-1][0] == start
    for begin, finish in windows:
        assert (finish - begin).days <= wp.MAX_WINDOW_DAYS - 1, f"窗口超 30 天: {begin} ~ {finish}"
    for i in range(len(windows) - 1):
        # 相邻窗口之间不能有缝隙（闭区间，下一窗结束 = 上一窗开始 - 1 秒）
        assert (windows[i][0] - windows[i + 1][1]).total_seconds() == 1


def test_fetch_merges_same_goods_no_keeping_latest():
    """同一 goods_no 会在多个时间窗重复出现，去重后应保留最后修改时间最新的一份。"""

    class TwoWindowClient:
        configured = True

        def __init__(self):
            self.calls = 0

        def _request(self, endpoint, params):
            self.calls += 1
            page_no = int(params.get("page_no") or 0)
            if page_no > 0:
                return {"goods_list": []}
            if self.calls == 1:
                return {"goods_list": [{"goods_no": "G1", "goods_name": "旧名称", "goods_modified": "2025-01-01 00:00:00", "spec_list": []}]}
            return {"goods_list": [{"goods_no": "G1", "goods_name": "新名称", "goods_modified": "2025-03-01 00:00:00", "spec_list": []}]}

    client = TwoWindowClient()
    rows, _ = wp.fetch_goods(client, start="2025-01-01 00:00:00", end="2025-03-01 00:00:00")
    assert len(rows) == 1
    assert rows[0]["goods_name"] == "新名称"


def test_zero_price_falls_through_to_next_field():
    """旺店通未维护的价格返回 "0.0000"，必须当作无值继续找下一个字段。"""
    goods = [
        {
            "goods_no": "G1",
            "goods_name": "只有批发价有值",
            "spec_list": [{"spec_no": "S1", "retail_price": "0.0000", "wholesale_price": "12.3100"}],
        }
    ]
    product = wp.normalize_products(goods)[0]
    assert product["base_price"] == Decimal("12.31")
    assert product["price_source"] == "wholesale_price"


def test_all_zero_prices_leave_price_empty():
    goods = [
        {
            "goods_no": "G1",
            "goods_name": "全都未维护",
            "spec_list": [{"spec_no": "S1", "retail_price": "0.0000", "wholesale_price": "0.0000"}],
        }
    ]
    product = wp.normalize_products(goods)[0]
    assert product["base_price"] is None
    assert product["price_source"] == ""


def test_sync_raises_when_wdt_not_configured(db, monkeypatch):
    class Unconfigured:
        configured = False

    monkeypatch.setattr(wp, "WangdianClient", lambda: Unconfigured())
    with pytest.raises(RuntimeError, match="旺店通凭证未配置"):
        wp.sync_products_from_wdt(db)


def test_full_sync_end_to_end(db, monkeypatch):
    fake = FakeClient(pages=[GOODS])
    monkeypatch.setattr(wp, "WangdianClient", lambda: fake)

    result = wp.sync_products_from_wdt(db, start="2026-01-01 00:00:00", end="2026-01-20 00:00:00")
    assert result["ok"] is True
    assert result["goods_count"] == 2
    assert result["created"] == 3
    # 返回原始字段名，便于真实同步后核对字段映射
    assert "goods_keys" in result["raw_fields"]
    assert "spec_keys" in result["raw_fields"]
    # 记录价格取自哪个字段，便于核对（旺店通大量商品只有批发价有值）
    assert "price_sources" in result
