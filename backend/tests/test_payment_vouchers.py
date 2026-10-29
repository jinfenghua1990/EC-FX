"""客户打款凭证：OCR 文本解析 + 入账 + 应收账款。

业务口径（2026-10-28 用户明确）
------------------------------
1. 客户打款后把凭证（截图）粘进来，系统自动识别金额/日期/付款方；
2. 识别结果只是「辅助填数」，必须人点确认才写订单的 paid_amount；
3. 同一张凭证不能重复入账；一张订单可以挂多张凭证（分次打款）。
"""
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import FxDistributor, FxOrder, FxProduct
from app.services import vouchers as v
from app.services.distribution import create_order


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(v, "VOUCHER_ROOT", tmp_path / "vouchers")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture()
def order(db):
    d = FxDistributor(code="D001", name="分销商一", active=True)
    db.add(d)
    db.flush()
    p = FxProduct(sku="SKU-1", name="商品一", spec="盒", base_price=Decimal("100"))
    db.add(p)
    db.commit()
    view = create_order(db, d.id, [{"product_id": p.id, "quantity": 10}])  # 1000.00
    return db.get(FxOrder, view["id"])


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200  # 内容不重要，存盘/哈希用的是字节


# ---------------------------------------------------------------- 解析

def test_parse_wechat_transfer():
    text = """微信支付
转账给 上海优华
转账金额
¥12,550.00
付款方 王师傅
转账时间 2026-10-27 14:23:45
转账单号 1000050001202610270234567890
备注 10月货款"""
    out = v.parse_payment_text(text)
    assert out["amount"] == Decimal("12550.00")
    assert out["paid_at"] == "2026-10-27 14:23"
    assert out["pay_method"] == "微信支付"
    assert out["note"] == "10月货款"
    assert "上海优华" in out["payee"]


def test_parse_alipay_receipt():
    text = """支付宝
转账成功
+1,200.00
付款方
杭州某某食品有限公司
创建时间
2026年10月26日 09:05
附言 预付30%"""
    out = v.parse_payment_text(text)
    assert out["amount"] == Decimal("1200.00")
    assert out["paid_at"] == "2026-10-26 09:05"
    assert out["pay_method"] == "支付宝支付"
    assert out["payer"] == "杭州某某食品有限公司"
    assert out["note"] == "预付30%"


def test_parse_bank_receipt_uses_largest_amount():
    """银行回单上有手续费/余额，取最大的那笔才是本金。"""
    text = """中国银行 电子回单
交易日期 2026-10-25
付款人 上海优华贸易有限公司
收款人 红盒子供应链
交易金额 人民币 8,000.00
手续费 5.00
账户余额 132,004.18"""
    out = v.parse_payment_text(text)
    assert out["amount"] == Decimal("8000.00")
    assert out["paid_at"] == "2026-10-25"
    assert out["pay_method"] == "中国银行"
    assert out["payer"] == "上海优华贸易有限公司"


def test_parse_empty_text_returns_blanks():
    out = v.parse_payment_text("")
    assert out["amount"] is None
    assert out["paid_at"] == ""
    assert out["payer"] == ""


# ---------------------------------------------------------------- 存取

def test_add_voucher_saves_file_and_detects(db, order):
    """没有 OCR 引擎时也要能把图存下来，状态标 unavailable，不抛异常。"""
    res = v.add_voucher(db, content=PNG, filename="打款.png", mime="image/png",
                        order_id=order.id, run_ocr_now=False)
    assert res["order_id"] == order.id
    assert res["size"] == len(PNG)
    assert res["url"].endswith("/file")
    path = v.file_path(db.get(v.FxPaymentVoucher, res["id"]).stored_path)
    assert path.is_file()


def test_reject_non_image(db, order):
    with pytest.raises(ValueError, match="只支持图片"):
        v.add_voucher(db, content=b"hello", filename="a.txt", mime="text/plain",
                      order_id=order.id)


def test_reject_oversize(db, order):
    with pytest.raises(ValueError, match="太大"):
        v.add_voucher(db, content=b"x" * (v.MAX_BYTES + 1), filename="a.png",
                      mime="image/png", order_id=order.id)


# ---------------------------------------------------------------- 入账

def test_apply_voucher_accumulates_and_sets_status(db, order):
    r1 = v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                       order_id=order.id, run_ocr_now=False)
    v.update_voucher(db, r1["id"], {"amount": "300.00", "paid_at": "2026-10-27 10:00"})
    out = v.apply_voucher(db, r1["id"])
    assert out["order"]["paid_amount"] == "300.00"
    assert out["order"]["payment_status"] == "partial"

    # 第二笔补齐 → 已收到
    r2 = v.add_voucher(db, content=PNG + b"2", filename="b.png", mime="image/png",
                       order_id=order.id, run_ocr_now=False)
    v.update_voucher(db, r2["id"], {"amount": "700.00"})
    out2 = v.apply_voucher(db, r2["id"])
    assert out2["order"]["paid_amount"] == "1000.00"
    assert out2["order"]["payment_status"] == "received"


def test_apply_twice_is_rejected(db, order):
    r = v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                      order_id=order.id, run_ocr_now=False)
    v.update_voucher(db, r["id"], {"amount": "500.00"})
    v.apply_voucher(db, r["id"])
    with pytest.raises(ValueError, match="已经入账"):
        v.apply_voucher(db, r["id"])


def test_apply_without_amount_is_rejected(db, order):
    r = v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                      order_id=order.id, run_ocr_now=False)
    with pytest.raises(ValueError, match="金额"):
        v.apply_voucher(db, r["id"])


def test_apply_unclaimed_voucher_is_rejected(db, order):
    r = v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                      distributor_id=order.distributor_id, run_ocr_now=False)
    with pytest.raises(ValueError, match="还没挂到订单"):
        v.apply_voucher(db, r["id"])


def test_unapply_rolls_back(db, order):
    r = v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                      order_id=order.id, run_ocr_now=False)
    v.update_voucher(db, r["id"], {"amount": "400.00"})
    v.apply_voucher(db, r["id"])
    v.unapply_voucher(db, r["id"])
    db.refresh(order)
    assert Decimal(order.paid_amount) == Decimal("0")
    assert order.payment_status == "unpaid"


def test_applied_voucher_cannot_be_deleted(db, order):
    r = v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                      order_id=order.id, run_ocr_now=False)
    v.update_voucher(db, r["id"], {"amount": "100.00"})
    v.apply_voucher(db, r["id"])
    with pytest.raises(ValueError, match="已经入账"):
        v.delete_voucher(db, r["id"])


# ---------------------------------------------------------------- 应收账款

def test_receivables_lists_open_amount_and_vouchers(db, order):
    r = v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                      order_id=order.id, run_ocr_now=False)
    v.update_voucher(db, r["id"], {"amount": "250.00"})
    v.apply_voucher(db, r["id"])

    data = v.receivables(db)
    item = next(x for x in data["items"] if x["id"] == order.id)
    assert item["total_amount"] == "1000.00"
    assert item["paid_amount"] == "250.00"
    assert item["open_amount"] == "750.00"
    assert item["payment_status"] == "partial"
    assert item["voucher_count"] == 1
    assert data["totals"]["outstanding"] == "750.00"
    assert data["totals"]["with_voucher"] == 1


def test_unclaimed_vouchers_surface_in_receivables(db, order):
    v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                  distributor_id=order.distributor_id, run_ocr_now=False)
    unclaimed = v.unclaimed_vouchers(db)
    assert len(unclaimed) == 1
    assert unclaimed[0]["distributor_name"] == "分销商一"
    assert v.voucher_stats(db)["unclaimed"] == 1


def test_receivables_filter_by_status(db, order):
    assert v.receivables(db, status="received")["items"] == []
    assert len(v.receivables(db, status="unpaid")["items"]) == 1


def test_applied_voucher_cannot_change_amount_or_move_order(db, order):
    receipt = v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                            order_id=order.id, run_ocr_now=False)
    v.update_voucher(db, receipt["id"], {"amount": "300.00"})
    v.apply_voucher(db, receipt["id"])
    another = FxOrder(order_no="OTHER", distributor_id=order.distributor_id,
                      total_amount=Decimal("500.00"))
    db.add(another)
    db.commit()
    with pytest.raises(ValueError, match="先撤销入账"):
        v.update_voucher(db, receipt["id"], {"amount": "500.00"})
    with pytest.raises(ValueError, match="先撤销入账"):
        v.update_voucher(db, receipt["id"], {"order_id": another.id})
    db.refresh(order)
    assert order.paid_amount == Decimal("300.00")
    saved = db.get(v.FxPaymentVoucher, receipt["id"])
    assert saved.order_id == order.id and saved.amount == Decimal("300.00")


def test_set_mode_does_not_overwrite_another_applied_receipt(db, order):
    a = v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                      order_id=order.id, run_ocr_now=False)
    b = v.add_voucher(db, content=PNG, filename="b.png", mime="image/png",
                      order_id=order.id, run_ocr_now=False)
    v.apply_voucher(db, a["id"], amount="200")
    with pytest.raises(ValueError, match="不能覆盖"):
        v.apply_voucher(db, b["id"], amount="800", mode="set")
    assert db.get(v.FxPaymentVoucher, b["id"]).applied == 0
    assert db.get(FxOrder, order.id).paid_amount == Decimal("200.00")
    v.unapply_voucher(db, a["id"])
    assert db.get(FxOrder, order.id).paid_amount == Decimal("0.00")


def test_cannot_apply_over_order_total(db, order):
    a = v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                      order_id=order.id, run_ocr_now=False)
    with pytest.raises(ValueError, match="超过订单金额"):
        v.apply_voucher(db, a["id"], amount="1001")
    assert db.get(FxOrder, order.id).paid_amount == Decimal("0.00")


def test_unapply_refuses_to_hide_inconsistent_balance(db, order):
    a = v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                      order_id=order.id, run_ocr_now=False)
    v.apply_voucher(db, a["id"], amount="300")
    order.paid_amount = Decimal("50")
    db.commit()
    with pytest.raises(ValueError, match="账面数据不一致"):
        v.unapply_voucher(db, a["id"])
    assert db.get(v.FxPaymentVoucher, a["id"]).applied == 1
    assert db.get(FxOrder, order.id).paid_amount == Decimal("50.00")


def test_order_manual_status_cannot_erase_applied_receipt(db, order):
    from app.services.distribution import update_order_fulfillment
    a = v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                      order_id=order.id, run_ocr_now=False)
    v.apply_voucher(db, a["id"], amount="200")
    with pytest.raises(ValueError, match="不能低于已入账凭证"):
        update_order_fulfillment(db, order.id, {"paid_amount": "0", "payment_status": "unpaid"})
    db.rollback()


def test_voucher_order_customer_must_match(db, order):
    other = FxDistributor(code="D-OTHER", name="其他客户")
    db.add(other)
    db.commit()
    with pytest.raises(ValueError, match="客户不一致"):
        v.add_voucher(db, content=PNG, filename="a.png", mime="image/png",
                      order_id=order.id, distributor_id=other.id, run_ocr_now=False)
