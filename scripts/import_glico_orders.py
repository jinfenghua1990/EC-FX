# -*- coding: utf-8 -*-
"""格力高 B2B 团单 Excel → EC-HX 分销订单 导入脚本（幂等，可重复执行）。

来源：格力高-B2B团单-2026年.xlsx（按月 sheet，每个 sheet 内多个客户团单块）。
映射规则：
  一个客户团单块  -> 1 个 FxOrder（status=approved，remark 带「团单导入·{sheet}」前缀）
  块的付款公司    -> FxDistributor（按名去重，brands=格力高/Glico，note=B2B团单导入）
  明细行          -> FxOrderLine（条码匹配 fx_products.barcode/spec_no，匹配不上也保留行）
  出库-旺店通单号 -> fx_orders.wdt_tid（块内全部收集、逗号拼接；历史单已在旺店通出过库，不再走系统推送）
  系统内部单号    -> FX+YYYYMMDD+3位流水（如 FX20261028001），独立于旺店通单号，
                     便于一个客户多个旺店通单号合并付款的场景
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
    """系统内部单号：FX + YYYYMMDD + 3位流水，如 FX20261028001。
    一个客户可能多个旺店通单号合并付款，所以旺店通单号只作关联字段（wdt_tid），
    内部单号保持独立稳定。"""
    prefix = f"FX{d.strftime('%Y%m%d')}"
    mx = 0
    for (no,) in cur.execute(
        "select order_no from fx_orders where order_no like ?", (prefix + "%",)
    ).fetchall():
        tail = no[len(prefix):].replace("-", "")
        if tail.isdigit():
            mx = max(mx, int(tail))
    return f"{prefix}{mx + 1:03d}"


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
    # 旺店通出库单号列：新表叫「出库-旺店通销售」，旧表叫「对应旺店通出库单号」，
    # 兜底按「包含旺店通」找列
    wdt_col = find_col("出库-旺店通销售", "出库-旺店通", "旺店通销售出库单", "对应旺店通出库单号")
    if wdt_col is None:
        wdt_col = next((j for k, j in colmap.items() if "旺店通" in k), None)
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
        wdt_no = ""
        if wdt_col is not None:
            raw = gcol(wdt_col)
            if raw is not None:
                # 去 Excel 粘连的不换行空格；老 sheet 数据行下还会重复出现表头文字
                wdt_no = str(raw).replace("\xa0", "").strip()
                if wdt_no and not re.match(r"^\d{8,14}$", wdt_no):
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

    def norm_payer(name):
        """付款方名归一：谢建华的变体（谢建华-已付款、谢建华&彩霞…）归一成谢建华。"""
        name = (name or "").strip()
        if not name:
            return ""
        if re.match(r"^谢建华", name):
            return "谢建华"
        return name

    def receiver_of(addr):
        """从送货块头提取（收货电话key, 收货人名）。

        块头形如「公司-送货地址：上海市松江区潮富路78号、186-1620-4493、王师傅」
        或「送货地址：…，汪于成，18321003298------自提」。电话是最稳的收货点标识。
        """
        if not addr:
            return None, ""
        m = re.search(r"1[3-9]\d[\d-]{8,13}", addr)
        if not m:
            return None, ""
        phone = re.sub(r"\D", "", m.group(0))
        segs = [s.strip() for s in re.split(r"[、，]", addr) if s.strip()]
        pi = next((i for i, s in enumerate(segs) if m.group(0) in s), None)
        rcv = ""
        if pi is not None:
            for j in (pi + 1, pi - 1):  # 收货人名在电话后一段（王师傅）或前一段（汪于成）
                if 0 <= j < len(segs):
                    cand = re.sub(r"-+自提$", "", segs[j]).strip()
                    cand = re.split(r"\s", cand)[0]  # 去掉可能粘在一起的公司名尾巴
                    if cand and len(cand) <= 12 and not re.search(r"\d{6,}|路|号|栋|楼|地址", cand):
                        rcv = cand
                        break
        return phone, rcv

    # ---- 第一遍：收拢所有月表的团单块，按「收货人（送货电话）」归组 ----
    # 用户 2026-10-28 定的规则：同一个收货点 = 同一个客户，
    # 块里五花八门的付款公司（威太仓下出现过 9 种付款方）只是这个客户的付款账户。
    # 没有收货信息的块，退回按付款公司归组。
    groups = {}
    for sheet in wb.sheetnames:
        if sheet in SKIP_SHEETS:
            continue
        blocks = parse_sheet(wb[sheet])
        if not blocks:
            stats["sheets_skipped"] += 1
            continue
        stats["sheets_ok"] += 1
        for blk in blocks:
            phone, rcv = receiver_of(blk["addr"])
            payers = Counter(norm_payer(l["pay_company"]) for l in blk["lines"] if l["pay_company"])
            if phone:
                key = "rcv:" + phone
            else:
                payer = payers.most_common(1)[0][0] if payers else norm_payer(blk["company"])
                key = "pay:" + (payer or f"未识别-{sheet}")
            g = groups.setdefault(key, {
                "phone": phone, "names": Counter(), "payers": Counter(),
                "receivers": Counter(), "blocks": [], "first_sheet": sheet,
            })
            if blk["company"]:
                g["names"][norm_payer(blk["company"])] += 1
            g["payers"].update(payers)
            if rcv:
                g["receivers"][rcv] += 1
            g["blocks"].append((sheet, blk))

    # ---- 组名归并：不同 key 算出同一个客户名时（如 优华 有收货块+无收货块），
    # 合并成一组——付款账户取并集、收货信息取有值的那个，绝不互相覆盖。 ----
    def resolve_name(g):
        return (g["names"].most_common(1)[0][0] if g["names"]
                else g["payers"].most_common(1)[0][0] if g["payers"]
                else f"未识别客户-{g['first_sheet']}")

    by_name = {}
    for key, g in groups.items():
        name = resolve_name(g)
        if name in by_name:
            tgt = by_name[name]
            tgt["payers"].update(g["payers"])
            tgt["receivers"].update(g["receivers"])
            tgt["phone"] = tgt["phone"] or g["phone"]
            tgt["blocks"].extend(g["blocks"])
        else:
            g["name"] = name
            by_name[name] = g

    # ---- 第二遍：每个收货组建一个分销商，组内每块建一张订单 ----
    for name, g in by_name.items():
        contact = "/".join(r for r, _ in g["receivers"].most_common())
        phone = g["phone"] or ""
        # 付款账户 = 该客户名下出现过的所有付款方（不含客户名自身）
        accounts = [p for p, _ in g["payers"].most_common() if p and p != name]
        row = cur.execute("select id from fx_distributors where name=?", (name,)).fetchone()
        if row:
            dist_id = row[0]
            # 已存在（如手工建过）：付款账户取并集，收货信息只在原来为空时补
            old = cur.execute("select contact, phone, pay_accounts from fx_distributors where id=?", (dist_id,)).fetchone()
            merged = []
            for acc in (old[2] or "").split(",") + accounts:
                acc = acc.strip()
                if acc and acc not in merged:
                    merged.append(acc)
            cur.execute(
                "update fx_distributors set contact=?, phone=?, pay_accounts=? where id=?",
                (old[0] or contact, old[1] or phone, ",".join(merged), dist_id),
            )
        else:
            code = next_distributor_code(cur)
            cur.execute(
                "insert into fx_distributors(code,name,contact,phone,email,tax_no,brands,region,pay_accounts,note,active,created_at,updated_at)"
                " values(?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
                (code, name, contact, phone, "", "", BRAND, "", ",".join(accounts), "B2B团单导入", 1),
            )
            dist_id = cur.lastrowid
            stats["distributors_created"] += 1
        stats["dist_groups"] = stats.get("dist_groups", [])
        stats["dist_groups"].append(f"{name}｜收货:{contact or '-'} {phone}｜付款账户{len(accounts)}个:{','.join(accounts[:6])}{'…' if len(accounts) > 6 else ''}")

        for sheet, blk in g["blocks"]:
            lines = blk["lines"]
            ym = sheet_month(sheet)
            y, m = ym if ym else (2025, 1)
            # 订单日期：块内付款日期多数派 > sheet 月 1 号；串月（如旧表序列号误判）回落 sheet 月
            ds = Counter(l["pay_date"] for l in lines if l["pay_date"])
            odate = ds.most_common(1)[0][0] if ds else None
            if odate and abs((odate.year - y) * 12 + (odate.month - m)) > 1:
                odate = None
            if not odate:
                odate = date(y, m, 1)
            # 旺店通出库单号：块内全部收集（一块多次出库对应多个单号），逗号拼接
            wdts = sorted({l["wdt_no"] for l in lines if l["wdt_no"]})
            wdt = ",".join(wdts)[:120]
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
            month_totals.setdefault(f"{y}-{m:02d}", [0, 0.0])
            month_totals[f"{y}-{m:02d}"][0] += 1
            month_totals[f"{y}-{m:02d}"][1] += total
    wb.close()
    conn.commit()

    print("=== 导入完成（按收货人归组） ===")
    print(f"sheet 解析成功: {stats['sheets_ok']}，跳过: {stats['sheets_skipped']}")
    print(f"客户（收货组）: {stats['distributors_created']} 个新建")
    for line in stats["dist_groups"]:
        print("  " + line)
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
