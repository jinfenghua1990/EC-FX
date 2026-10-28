# -*- coding: utf-8 -*-
"""格力高 B2B 团单 Excel → EC-HX 分销订单 导入脚本（幂等，可重复执行）。

来源：格力高-B2B团单-2026年.xlsx（按月 sheet，每个 sheet 内多个客户团单块）。
映射规则：
  一个客户团单块  -> 1 个 FxOrder（status=approved，remark 带「团单导入·{sheet}」前缀）
  块的付款公司    -> FxDistributor（按名去重，brands=格力高/Glico，note=B2B团单导入）
  明细行          -> FxOrderLine（条码匹配 fx_products.barcode/spec_no，匹配不上也保留行）
  出库-旺店通单号 -> fx_orders.wdt_tid（历史单已在旺店通出过库，不再走系统推送）
  订单日期        -> 块内多数付款日期，缺省用 sheet 月份的 1 号
重复执行会先清掉上一次导入（remark LIKE '团单导入%'）再重灌，不影响手工数据。

用法：
  python import_glico_orders.py <xlsx路径>
"""
import re
import sys
import sqlite3
from collections import Counter
from datetime import datetime, date
from pathlib import Path

from openpyxl import load_workbook

DB = Path(r"C:/Users/jinfe/Documents/默认项目/EC-HX/data/local/reconciliation.db")
BRAND = "格力高/Glico"
SKIP_SHEETS = {"报价单"}


def to_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def to_date(v):
    """付款日期/签收时间：20261012 / '20261012' / 202610101326（取前8位）/ Excel 序列号。"""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    if isinstance(v, (int, float)):
        n = float(v)
        if 30000 <= n <= 60000:  # Excel 序列日期（1899-12-30 起算）
            try:
                return date.fromordinal(date(1899, 12, 30).toordinal() + int(n))
            except (ValueError, OverflowError):
                return None
        s = str(int(v))
    else:
        s = str(v or "").strip()
        m = re.search(r"\d{8}", s)
        s = m.group(0) if m else ""
    if len(s) >= 8 and s[:8].isdigit():
        try:
            return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        except ValueError:
            return None
    return None


def sheet_month(name):
    """sheet 名 -> (年, 月)。'26年10月'->(2026,10)；'10月上'/'12月' 等(2025)。"""
    m = re.search(r"(\d{2})年(\d{1,2})月", name)
    if m:
        return 2000 + int(m.group(1)), int(m.group(2))
    m = re.match(r"(\d{1,2})月", name)
    if m:
        return 2025, int(m.group(1))
    return None


def next_distributor_code(cur):
    row = cur.execute(
        "select code from fx_distributors where code like 'D%'"
    ).fetchall()
    used = {r[0] for r in row}
    n = 1
    while f"D{n:03d}" in used:
        n += 1
    return f"D{n:03d}"


def next_order_no(cur, d: date):
    prefix = f"FX{d.strftime('%Y%m%d')}"
    rows = cur.execute(
        "select order_no from fx_orders where order_no like ?", (prefix + "-%",)
    ).fetchall()
    mx = 0
    for (no,) in rows:
        try:
            mx = max(mx, int(no.rsplit("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"{prefix}-{mx + 1:04d}"


def match_product(cur, barcode):
    b = str(barcode or "").strip()
    if not b:
        return None
    row = cur.execute(
        "select id, sku, goods_no from fx_products where barcode=? or spec_no=? limit 1",
        (b, b),
    ).fetchone()
    return row


def parse_sheet(ws):
    """返回 [(company, addr, order_date, wdt_tid, lines[]), ...]，lines 为 dict。"""
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    # 找表头行（两级表头：上一行是合并的组标题，如「客户订货-情况」「发货情况」）
    # 宽松条件：条形码+品名 必有，类别/商品编码 至少一个（旧 sheet 没有「类别」列）
    header_idx, colmap = None, {}
    for i, r in enumerate(rows[:12]):
        cells = [str(v).strip() if v is not None else "" for v in r]
        if "条形码" in cells and "品名" in cells and ("类别" in cells or "商品编码" in cells):
            header_idx = i
            # 先并入上一行的组标题（列位置在前两级都非空时以字段行优先）
            if i > 0:
                for j, v in enumerate(rows[i - 1]):
                    c = str(v).strip() if v is not None else ""
                    if c:
                        colmap.setdefault(c, j)
            for j, c in enumerate(cells):
                if c:
                    colmap[c] = j
            break
    if header_idx is None:
        return []
    col = lambda name: colmap.get(name)
    if col("条形码") is None or col("品名") is None:
        return []

    def find_col(*tokens, exact_first=True):
        """按优先级找列：先精确名，再「包含」匹配。返回第一个命中的列号。"""
        for t in tokens:
            if t in colmap:
                return colmap[t]
        for t in tokens:
            for k, j in colmap.items():
                if t in k:
                    return j
        return None

    # 数量列优先级：实发 > 发货 > 到货 > 订货 > 合计（同名重复列取先出现的）
    qty_col = find_col("实发总计", "发货数量", "实发", "发货情况", "实际到货", "客户订货", "订单数量", "订单合计", "原订货", "合计/箱", "合计")
    price_col = find_col("供货价/箱", "供货价")
    subtotal_col = find_col("销售小计")
    if subtotal_col is None:  # 旧表用两个「小计」（成本/销售），取最后一个
        xs = [j for k, j in colmap.items() if "小计" in k]
        subtotal_col = max(xs) if xs else None

    blocks, cur_blk = [], None
    for r in rows[header_idx + 1:]:
        joined = " ".join(str(v) for v in r if v is not None)
        if "送货地" in joined or "送货地址" in joined:
            addr = joined.strip()
            comp = ""
            for v in r:
                s = str(v or "")
                if re.search(r"(有限公司|贸易|实业|医药|食品)", s) and "送货" not in s and len(s) < 40 and "地址" not in s:
                    comp = s.strip()
                    break
            if not comp:  # 公司名可能嵌在送货地文本里（如「XX有限公司-送货地址：…」）
                mcomp = re.search(r"([\u4e00-\u9fa5A-Za-z0-9（）()]+(?:有限公司|贸易有限公司|实业有限公司|医药有限公司|食品有限公司))", addr)
                if mcomp:
                    comp = mcomp.group(1)
            cur_blk = {"company": comp, "addr": addr, "lines": []}
            blocks.append(cur_blk)
            continue
        cat_col = col("类别") if col("类别") is not None else col("商品编码")
        cat = r[cat_col] if cat_col is not None and cat_col < len(r) else None
        if cat is None or not str(cat).strip() or "类别" in str(cat) or str(cat).strip() == "商品编码":
            continue
        if cur_blk is None:  # 明细行出现在任何送货地行之前：先兜一个空块
            cur_blk = {"company": "", "addr": "", "lines": []}
            blocks.append(cur_blk)

        def gcol(j):
            if j is None or j >= len(r):
                return None
            return r[j]

        qty = to_num(gcol(qty_col))
        if not qty:
            continue
        price = to_num(gcol(price_col)) or 0
        subtotal = to_num(gcol(subtotal_col))
        if subtotal is None:
            subtotal = round(qty * price, 2)

        def g(name, alt=None):
            j = col(name)
            if j is None and alt:
                j = col(alt)
            if j is None or j >= len(r):
                return ""
            v = r[j]
            return "" if v is None else str(v).strip()

        # 付款公司/付款日期：公司可能串到付款日期列，日期列只认 8 位数字；旧表叫「付款方」（可能是人名）
        pay_company = g("付款公司") or g("付款方")
        pay_date_raw = gcol(col("付款日期"))
        if pay_date_raw and re.search(r"公司|贸易|实业|医药|食品", str(pay_date_raw)):
            pay_company = pay_company or str(pay_date_raw).strip()
            pay_date_raw = None
        if pay_company and re.search(r"\d{8}", str(pay_date_raw or "")) is None and re.search(r"公司|贸易|实业|医药|食品", pay_company) is None:
            # 旧表「付款方」是人名（如 钟军）：仍作为客户名保留
            pass
        pd = to_date(pay_date_raw)
        wdt_no = g("出库-旺店通销售") or g("出库-旺店通")
        if not wdt_no:
            wdt_no = g("旺店通销售出库单")
        if wdt_no and not re.match(r"^\d{8,}$", wdt_no):
            wdt_no = ""
        cur_blk["lines"].append({
            "category": str(cat).strip(),
            "barcode": g("条形码"),
            "name": g("品名")[:250],
            "box": g("箱规格"),
            "handling": g("处理方式"),
            "batch": g("批次情况"),
            "qty": qty,
            "price": price or 0,
            "subtotal": subtotal or 0,
            "pay_date": pd,
            "pay_company": pay_company,
            "wdt_no": wdt_no,
        })
    return [b for b in blocks if b["lines"]]


def main(xlsx):
    conn = sqlite3.connect(DB)
    cur = conn.cursor()

    # ---- 清理上次导入（幂等） ----
    cur.execute(
        "delete from fx_order_lines where order_id in "
        "(select id from fx_orders where remark like '团单导入%')"
    )
    cur.execute("delete from fx_orders where remark like '团单导入%'")
    cur.execute(
        "delete from fx_distributors where note='B2B团单导入' and id not in "
        "(select distributor_id from fx_orders)"
    )
    conn.commit()

    wb = load_workbook(xlsx, read_only=True, data_only=True)
    dist_cache = {}
    stats = Counter()
    unmatched = Counter()
    month_totals = {}

    for sheet in wb.sheetnames:
        if sheet in SKIP_SHEETS:
            continue
        ym = sheet_month(sheet)
        blocks = parse_sheet(wb[sheet])
        if not blocks:
            stats["sheets_skipped"] += 1
            continue
        y, m = ym if ym else (2025, 1)
        for blk in blocks:
            lines = blk["lines"]
            # 客户名：块内付款公司多数派 > 块头识别 > 兜底
            comps = Counter(l["pay_company"] for l in lines if l["pay_company"])
            company = comps.most_common(1)[0][0] if comps else blk["company"]
            if not company:
                company = f"未识别客户-{y}年{m}月-{len(blocks)}"
            # 同一客户的名字变体归一（如「谢建华-已付款」「谢建华&彩霞」→ 谢建华）
            if company != "谢建华" and re.match(r"^谢建华[-、&＆*·]", company):
                company = "谢建华"
            # 分销商
            if company not in dist_cache:
                row = cur.execute(
                    "select id from fx_distributors where name=?", (company,)
                ).fetchone()
                if row:
                    dist_cache[company] = row[0]
                else:
                    code = next_distributor_code(cur)
                    cur.execute(
                        "insert into fx_distributors(code,name,contact,phone,email,tax_no,brands,region,note,active,created_at,updated_at)"
                        " values(?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
                        (code, company, "", "", "", "", BRAND, "", "B2B团单导入", 1),
                    )
                    dist_cache[company] = cur.lastrowid
                    stats["distributors_created"] += 1
            dist_id = dist_cache[company]
            # 订单日期：块内付款日期多数派 > sheet 月 1 号；串月（如旧表序列号误判）回落 sheet 月
            ds = Counter(l["pay_date"] for l in lines if l["pay_date"])
            odate = ds.most_common(1)[0][0] if ds else None
            if odate and abs((odate.year - y) * 12 + (odate.month - m)) > 1:
                odate = None
            if not odate:
                odate = date(y, m, 1)
            # 旺店通出库单号：取块内第一个
            wdt = next((l["wdt_no"] for l in lines if l["wdt_no"]), "")
            addr = blk["addr"][:300]
            handling = Counter(l["handling"] for l in lines if l["handling"])
            htxt = "、".join(f"{k}x{v}" for k, v in handling.most_common(3))
            order_no = next_order_no(cur, odate)
            total = round(sum(l["subtotal"] for l in lines), 2)
            remark = f"团单导入·{sheet}·送货地:{addr}" + (f"·处理:{htxt}" if htxt else "")
            cur.execute(
                "insert into fx_orders(order_no,distributor_id,status,order_date,total_amount,remark,wdt_tid,wdt_shop_no,push_status,push_message,pushed_at,created_at,updated_at)"
                " values(?,?,?,?,?,?,?,?,?,?,NULL,datetime('now'),datetime('now'))",
                (order_no, dist_id, "approved", odate.isoformat(), total, remark, wdt, "", "", ""),
            )
            oid = cur.lastrowid
            nlines = 0
            for l in lines:
                prod = match_product(cur, l["barcode"])
                if prod:
                    pid, sku, gno = prod
                else:
                    pid, sku, gno = None, "", ""
                    unmatched[l["barcode"] or "(无条码)"] += 1
                spec = "·".join(x for x in [l["box"] and f"箱规{l['box']}", l["handling"], l["batch"]] if x)
                cur.execute(
                    "insert into fx_order_lines(order_id,product_id,goods_no,spec_no,goods_name,spec_name,quantity,unit_price,subtotal)"
                    " values(?,?,?,?,?,?,?,?,?)",
                    (oid, pid, gno, l["barcode"], l["name"], spec[:158], l["qty"], l["price"], l["subtotal"]),
                )
                nlines += 1
            stats["orders"] += 1
            stats["lines"] += nlines
            if prod:
                stats["lines_matched"] += 0  # 占位
            month_totals.setdefault(f"{y}-{m:02d}", [0, 0.0])
            month_totals[f"{y}-{m:02d}"][0] += 1
            month_totals[f"{y}-{m:02d}"][1] += total
        stats["sheets_ok"] += 1
    wb.close()
    conn.commit()

    print("=== 导入完成 ===")
    print(f"sheet 解析成功: {stats['sheets_ok']}，跳过: {stats['sheets_skipped']}")
    print(f"分销商新建: {stats['distributors_created']}")
    print(f"订单: {stats['orders']}，明细行: {stats['lines']}")
    for k in sorted(month_totals):
        n, amt = month_totals[k]
        print(f"  {k}: {n} 单, ¥{amt:,.0f}")
    if unmatched:
        print(f"未匹配商品条码 {len(unmatched)} 种:")
        for b, c in unmatched.most_common(15):
            print(f"  {b} x{c}")
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else
         r"C:/Users/jinfe/WorkBuddy/2026-10-27-14-26-55/格力高-B2B团单-2026年-副本.xlsx")
