"""团单导入脚本：收货地址写进独立字段，不再塞进备注。"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "import_glico_orders.py"


@pytest.fixture(scope="module")
def glico():
    spec = importlib.util.spec_from_file_location("glico_import", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 收货点解析
def test_receipt_shape_with_shipper(glico):
    """①「公司-送货地址：地址、电话、收货人」：送货方与地址要分开。"""
    addr, shipper = glico.split_receipt(
        "上海威太贸易有限公司-送货地址：上海市松江区潮富路78号、186-1620-4493、王师傅"
    )
    assert addr == "上海市松江区潮富路78号"
    assert shipper == "上海威太贸易有限公司"


def test_receipt_shape_bare_address(glico):
    """②「地址、电话、收货人」：裸地址形态，电话和人名都不能进地址。"""
    addr, shipper = glico.split_receipt("上海市松江区盛龙路755号7栋1楼、13817654550、林")
    assert addr == "上海市松江区盛龙路755号7栋1楼"
    assert shipper == ""


def test_receipt_self_pick_suffix_dropped(glico):
    """③ 带「------自提 / 自提+刮码」后缀的地址要去掉后缀。"""
    addr, _ = glico.split_receipt("上海市松江区盛龙路755号7栋1楼------自提+刮码")
    assert addr == "上海市松江区盛龙路755号7栋1楼"


def test_delivery_address_of_multiple_points(glico):
    addr, shipper = glico.delivery_address_of(
        "上海市松江区盛龙路755号7栋1楼、13817654550、林；上海市青浦区白鹤镇鹤祥路36号2号楼"
    )
    assert addr == "上海市松江区盛龙路755号7栋1楼；上海市青浦区白鹤镇鹤祥路36号2号楼"
    assert shipper == ""


def test_shipper_of_note_keeps_existing(glico):
    """重跑导入时，档案备注里已有的「送货方：X」不能被冲掉。"""
    assert glico.shipper_of_note(
        "B2B团单导入｜送货方：上海威太贸易有限公司"
    ) == "上海威太贸易有限公司"
    assert glico.shipper_of_note("B2B团单导入") == ""


# ---------------------------------------------------------------- 跑一遍真导入
def _mini_xlsx(path: Path) -> Path:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "26年10月"
    header = ["条形码", "品名", "类别", "实发总计", "供货价/箱", "付款公司",
              "付款日期", "出库-旺店通销售"]
    ws.append(["客户订货-情况"])
    ws.append(header)
    # 块 1：已有档案的联系人（王师傅）—— 走「沿用档案」分支
    ws.append(["上海威太贸易有限公司-送货地址：上海市松江区潮富路78号、18616204493、王师傅"])
    ws.append(header)
    ws.append(["6901234567890", "测试棒棒糖", "棒棒糖", 10, 50,
               "上海威太贸易有限公司", "20261012", "202410241710"])
    # 块 2：新联系人 —— 走「新建档案」分支
    ws.append(["上海市松江区测试路88号1楼、13800000009、SMOKE收货人"])
    ws.append(header)
    ws.append(["6901234567891", "测试夹心棒", "棒棒糖", 5, 60,
               "上海测试贸易有限公司", "20261013", ""])
    wb.save(path)
    return path


def test_import_writes_delivery_address_and_keeps_archive(glico, tmp_path, monkeypatch):
    import shutil
    import sqlite3

    # 按真实库的结构复制一份空库：迁移链在全新库上跑不通（0007 里 create_all 会把
    # 当前模型的列全建出来，0010 再补 pack_qty 就撞列），脚本又是照真实库写的 SQL。
    src = ROOT / "data" / "local" / "reconciliation.db"
    if not src.exists():
        pytest.skip("需要本地真实库结构（data/local/reconciliation.db）")
    db_path = tmp_path / "import_test.db"
    shutil.copyfile(src, db_path)
    conn = sqlite3.connect(db_path)
    for t in ("fx_order_lines", "fx_orders", "fx_distributor_pay_links", "fx_distributors"):
        conn.execute(f"delete from {t}")
    conn.commit()
    conn.close()

    # 预置一个「历史档案」：编码/名称/付款账户/送货方都是手工改过的，不该被冲掉
    conn = sqlite3.connect(db_path)
    conn.execute(
        "insert into fx_distributors(code,name,contact,phone,email,tax_no,brands,region,"
        "pay_accounts,note,delivery_address,active,created_at,updated_at) "
        "values(?,?,?,?,?,?,?,?,?,?,?,1,datetime('now'),datetime('now'))",
        ("H-01", "上海威太", "王师傅", "18616204493", "", "", "格力高/Glico", "",
         "上海威太贸易有限公司", "B2B团单导入｜送货方：上海威太贸易有限公司", "旧地址"),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(glico, "DB", db_path)
    glico.main(str(_mini_xlsx(tmp_path / "mini.xlsx")))

    conn = sqlite3.connect(db_path)
    rows = {r[0]: r for r in conn.execute(
        "select name,code,contact,phone,note,delivery_address,pay_accounts from fx_distributors"
    )}
    conn.close()

    # 已有档案：地址刷新成表里的，编码/名称/付款账户/送货方全保留
    old = rows["上海威太"]
    assert old[1] == "H-01"
    assert old[5] == "上海市松江区潮富路78号"
    assert old[4] == "B2B团单导入｜送货方：上海威太贸易有限公司"
    assert old[6] == "上海威太贸易有限公司"

    # 新建档案：地址进独立字段，备注只留来源标记
    new = rows["SMOKE收货人"]
    assert new[5] == "上海市松江区测试路88号1楼"
    assert new[4] == "B2B团单导入"
    assert new[1] == "H-02"   # 自动编号接在 H-01 之后，不跳号不撞号
