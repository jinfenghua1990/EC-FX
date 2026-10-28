# -*- coding: utf-8 -*-
"""格力高 B2B 团单 Excel → EC-HX 分销订单 导入脚本（幂等，可重复执行）。

来源：格力高-B2B团单-2026年.xlsx（按月 sheet，每个 sheet 内多个客户团单块）。
月份范围：**只收 2026-09、2026-10 两张 sheet**（见 KEEP_YM 白名单），其余一律不进。
映射规则：
  一个客户团单块  -> 1 个 FxOrder（status=approved，remark 带「团单导入·{sheet}」前缀）
  块的收货信息    -> FxDistributor（**按收货人（联系人）建档**，一个联系人 = 一个分销商；
                     收货地址只是仓库、付款公司只是谁付的钱，都不作客户身份；
                     块内没有任何联系人的归到「待确认客户」，等人工指定）
                     收货地址写进独立的 delivery_address 字段，note 只留来源标记
  明细行          -> FxOrderLine（条码匹配 fx_products.barcode/spec_no，匹配不上也保留行）
  付款公司        -> fx_orders.pay_company（**一单一值**：一个团单块只有一个付款公司）。
                     分销商档案上的「付款账户」是「这个客户名下有哪些公司」的集合，两者不同概念
  出库-旺店通单号 -> fx_orders.wdt_tid（块内全部收集、逗号拼接；历史单已在旺店通出过库，不再走系统推送）
  推送状态        -> 有单号 = push_status='sent'（原表就是旺店通出库记录，不需要再推）；
                     无单号 = 'history'（历史单，原表没记录单号，既不冒充已推送也不进待推送）
  系统内部单号    -> FX+YYYYMMDD+3位流水（如 FX20261028001），独立于旺店通单号，
                     便于一个客户多个旺店通单号合并付款的场景
  订单日期        -> 块内多数付款日期，缺省用 sheet 月份的 1 号
重复执行：**订单**全部重建（remark LIKE '团单导入%' 的先删后灌），
**分销商档案**按「联系人 → 电话 → 名称」匹配已有记录，只刷新收货信息，
名称 / 编码 / 付款账户 / 默认收款抬头一律保留（人工改过的不冲掉）；
表里已经没有的联系人，其无订单档案会被清理。不影响手工建的其他数据。

用法：
  python import_glico_orders.py <xlsx路径>
"""
import re
import sys
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, date
from pathlib import Path

from openpyxl import load_workbook

DB = Path(r"C:/Users/jinfe/Documents/默认项目/EC-HX/data/local/reconciliation.db")
BRAND = "格力高/Glico"
SKIP_SHEETS = {"报价单"}
# 用户 2026-10-28 定：**只收 2026 年 9 月、10 月这两个月**，其他一律不进。
# 用白名单而不是「>= 起始月」，这样以后表里新增 11 月/12 月 sheet 也不会被顺带收进来。
KEEP_YM = {(2026, 9), (2026, 10)}


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
    """与后端 _next_distributor_code 保持一致：从 H-01 起找第一个没人用的号（不跳号）。"""
    row = cur.execute("select code from fx_distributors").fetchall()
    used = {r[0] for r in row if r[0]}
    n = 1
    while f"H-{n:02d}" in used:
        n += 1
    return f"H-{n:02d}"


# 收货点原文 → 纯地址。块头那格有三种写法：
#   ①「上海威太贸易有限公司-送货地址：上海市松江区潮富路78号、186-1620-4493、王师傅」
#   ②「上海市松江区盛龙路755号7栋1楼、13817654550、林」
#   ③ 带「------自提 / 自提+刮码」后缀
# 电话和收货人名都不进收货地址字段，它们在档案里另有位置（电话 / 联系人）。
_PHONE_RE = re.compile(r"1[3-9]\d[\d-]{8,13}")
_ADDR_HINT = re.compile(r"\d{6,}|路|号|栋|楼|地址|区|镇|街|村")


def split_receipt(cell):
    """把一段收货点原文拆成（纯地址, 送货方）。"""
    s = re.sub(r"\s+", "", cell or "")
    s = re.sub(r"[-—]*(?:自提|刮码)(?:[+＋](?:自提|刮码))*$", "", s).strip("-— ")
    shipper = ""
    m = re.search(r"^(.*?)[-—]?送货地址[:：]", s)
    if m:
        shipper = m.group(1).strip("-— ")
        s = s[m.end():]
    parts = []
    for seg in re.split(r"[、，,]", s):
        seg = seg.strip()
        if not seg or _PHONE_RE.search(seg):
            continue
        if len(seg) <= 12 and not _ADDR_HINT.search(seg):
            continue
        parts.append(seg)
    return "、".join(parts), shipper


def delivery_address_of(ships):
    """多个收货点（以「；」分隔）汇总成（地址串, 送货方）。"""
    addrs, shippers = [], []
    for part in (ships or "").split("；"):
        part = part.strip()
        if not part:
            continue
        addr, shipper = split_receipt(part)
        if addr:
            addrs.append(addr)
        if shipper and shipper not in shippers:
            shippers.append(shipper)
    return "；".join(addrs), "、".join(shippers)


def shipper_of_note(note):
    """从旧备注里取「送货方：X」——重跑导入时别把它冲掉。"""
    m = re.search(r"送货方[:：]\s*(.+?)(?:｜|$)", note or "")
    return m.group(1).strip() if m else ""


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


WDT_NO_RE = re.compile(r"^20\d{10,12}$")
# 长得像单号、但其实不是旺店通出库单号的列（老表里常与单号并排出现）
WDT_NOT = ("签收", "退货", "下单", "付款", "日期", "时间", "线上订单")


def _guess_wdt_col(rows, colmap):
    """按值的形态猜哪一列是旺店通出库单号。

    表头缺失的月份表（10月、10月上）只能这么认。判据：
    - 值形如 202410241710（20 开头 + 年月日时分）；
    - 列名不含「签收/退货/下单/付款/日期/时间/线上订单」；
    - 命中条数最多的那列胜出（并列取最左）。
    """
    label_of = {}
    for name, j in colmap.items():
        label_of.setdefault(j, name)
    score = {}
    for r in rows:
        for j, v in enumerate(r):
            if v is None:
                continue
            s = str(v).strip()
            if not WDT_NO_RE.match(s):
                continue
            # 年月日时分的合法范围粗校验，挡掉纯数字金额/条码
            if not (2020 <= int(s[:4]) <= 2027 and 1 <= int(s[4:6]) <= 12 and 1 <= int(s[6:8]) <= 31):
                continue
            if any(t in label_of.get(j, "") for t in WDT_NOT):
                continue
            score[j] = score.get(j, 0) + 1
    if not score:
        return None
    return max(sorted(score.items()), key=lambda kv: kv[1])[0]


def parse_sheet(ws):
    """返回 [{addr, lines[]}, ...]；addr 是块头那格的收货信息原文，lines 为 dict。"""
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
    # 旺店通出库单号列：各月表头命名不统一（出库-旺店通销售 / 对应旺店通出库单号 /
    # 旺店通销售出库单 / 旺店通销售出库单），10月、10月上 两张表更是**整列没有表头**，
    # 单号直接躺在第 15/16 列。所以表头匹配不中时，退回按值形态认列：
    # 旺店通单号形如 202410241710（YYYYMMDDHHMM，12 位；个别 10/14 位），
    # 排除「签收时间/退货单/下单时间/付款日期」这类同样长得很像的列。
    wdt_col = find_col("出库-旺店通销售", "出库-旺店通", "旺店通销售出库单", "对应旺店通出库单号", "旺店通销售出库单号")
    if wdt_col is None:
        wdt_col = next((j for k, j in colmap.items() if "旺店通" in k), None)
    if wdt_col is None:
        wdt_col = _guess_wdt_col(rows[header_idx + 1:], colmap)
    subtotal_col = find_col("销售小计")
    if subtotal_col is None:  # 旧表用两个「小计」（成本/销售），取最后一个
        xs = [j for k, j in colmap.items() if "小计" in k]
        subtotal_col = max(xs) if xs else None

    # 收货信息（块头行）两种形态都要认：
    #   ① 「上海威太贸易有限公司-送货地址：上海市松江区潮富路78号、186-1620-4493、王师傅」
    #   ② 「上海市松江区盛龙路755号7栋1楼、13817654550、林」——26年3/4/5/8/9/10月 有一批块
    #      整行没有「送货地址」四个字，只有裸地址（还常带「------自提 / 自提+刮码」后缀）
    ADDR_SHAPE = re.compile(r"[\u4e00-\u9fa5]{2,8}(?:路|街|道)\d+号")

    def ship_cell(r):
        """本行若是块头行，返回收货信息原文（单个单元格，不是整行拼接）；否则 None。"""
        cells = [str(v).strip() for v in r if v is not None and str(v).strip()]
        if not cells:
            return None
        if any(re.fullmatch(r"-?\d+(\.\d+)?", c) for c in cells):
            return None                      # 含纯数字格 → 合计行/明细行，不是块头
        for c in cells:
            if "送货地" in c:
                return c
        if ADDR_SHAPE.search(cells[0]):
            return cells[0]                  # 裸地址：原样返回，不伪造「送货地址：」前缀
        return None

    def starts_new_block(i):
        """第 i 行后面 1~5 行内若出现「条形码」表头行，说明 i 是一段新团单的块头。

        这是块边界的**结构判据**，比只看文字可靠。用户 2026-10-28 定：**碰到异常不要合并**。
        同一收货点（如盛龙路755号/林）在 26年10月 里连续出现 5 次，每次都带自己的
        付款公司、自己的旺店通出库单号，是 5 张独立团单；按收货地址合并会把
        林的正祥付款算到王师傅头上（这正是「王师傅付款账户里冒出正祥」的原因）。
        """
        for k in range(i + 1, min(i + 6, len(rows))):
            cells = [str(v).strip() for v in rows[k] if v is not None]
            if "条形码" in cells:
                return True
        return False

    blocks, cur_blk = [], None
    for i, r in enumerate(rows):
        if i != header_idx:
            addr = ship_cell(r)
            if addr and starts_new_block(i):
                cur_blk = {"addr": addr, "lines": []}
                blocks.append(cur_blk)
                continue
        if i <= header_idx:
            continue
        cat_col = col("类别") if col("类别") is not None else col("商品编码")
        cat = r[cat_col] if cat_col is not None and cat_col < len(r) else None
        if cat is None or not str(cat).strip() or "类别" in str(cat) or str(cat).strip() == "商品编码":
            continue
        if cur_blk is None:  # 明细行出现在任何送货地行之前：先兜一个空块
            cur_blk = {"addr": "", "lines": []}
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
                # 允许「202610221539-1」这种拆单后缀（26年10月 r151-154 就有一个）
                if wdt_no and not re.match(r"^\d{8,14}(-\d{1,2})?$", wdt_no):
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
    # 只清**订单**，不再删分销商档案。
    # 用户 2026-10-28 手工把 3 个档案从联系人姓名改成了公司名（上海威太 / 湖南正祥 /
    # 上海优华），还设了默认收款抬头。重跑导入如果连档案一起删掉重建，这些人工改动
    # 会被冲回联系人姓名、默认抬头也没了。所以这里先按「联系人 / 电话 / 名称」把现有
    # 档案快照下来，重建时只刷新收货信息，name / code / 付款账户 / 收款抬头一律保留。
    prev_all, prev_by_contact, prev_by_phone, prev_by_name = [], {}, {}, {}
    for r in cur.execute(
        "select id, code, name, contact, phone, pay_accounts, note from fx_distributors "
        "where note like 'B2B团单导入%'"
    ):
        rec = {
            "id": r[0], "code": r[1], "name": r[2],
            "contact": (r[3] or "").strip(), "phone": (r[4] or "").strip(),
            "pay_accounts": r[5] or "", "note": r[6] or "",
        }
        prev_all.append(rec)
        if rec["contact"]:
            prev_by_contact.setdefault(rec["contact"], rec)
        if rec["phone"]:
            prev_by_phone.setdefault(rec["phone"], rec)
        prev_by_name.setdefault(rec["name"], rec)

    cur.execute(
        "delete from fx_order_lines where order_id in "
        "(select id from fx_orders where remark like '团单导入%')"
    )
    cur.execute("delete from fx_orders where remark like '团单导入%'")
    conn.commit()

    wb = load_workbook(xlsx, read_only=True, data_only=True)
    dist_cache = {}
    stats = Counter()
    unmatched = Counter()
    month_totals = {}
    push_counts = Counter()
    # 旺店通单号首次出现在哪个块：同一个号落在两个块 = 同一批出库被认领两次
    wdt_owner = {}

    def norm_payer(name):
        """付款方名归一：谢建华的变体（谢建华-已付款、谢建华&彩霞…）归一成谢建华。"""
        name = (name or "").strip()
        if not name:
            return ""
        if re.match(r"^谢建华", name):
            return "谢建华"
        return name

    def receiver_of(addr):
        """从送货块头提取（收货电话, 收货人名）。

        块头形如「公司-送货地址：上海市松江区潮富路78号、186-1620-4493、王师傅」
        或「送货地址：…，汪于成，18321003298------自提」。
        末尾的「------自提」「自提+刮码」「刮码」是发货方式备注，不属于联系人。
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
                    cand = re.sub(r"[-—]*(?:自提|刮码)(?:[+＋](?:自提|刮码))*\s*$", "", segs[j]).strip(" -—")
                    cand = re.split(r"\s", cand)[0]  # 去掉可能粘在一起的公司名尾巴
                    if cand and len(cand) <= 12 and not re.search(r"\d{6,}|路|号|栋|楼|地址", cand):
                        rcv = cand
                        break
        return phone, rcv

    # ---- 第一遍：把每块的收货信息解析成（收货电话, 联系人） ----
    # 用户 2026-10-28 定：**分销商就是联系人**（他实际只对接 3 个人）。
    # 收货地址只是仓库、付款公司只是谁付的钱，都不是客户身份。
    raw = []
    skipped_sheets = []
    for sheet in wb.sheetnames:
        if sheet in SKIP_SHEETS:
            continue
        ym = sheet_month(sheet)
        if ym not in KEEP_YM:
            skipped_sheets.append(sheet)
            stats["sheets_skipped"] += 1
            continue
        blocks = parse_sheet(wb[sheet])
        if not blocks:
            stats["sheets_skipped"] += 1
            continue
        stats["sheets_ok"] += 1

        def norm_ship(addr):
            """收货点去重用的规范形式：去空白、去「------自提 / 自提+刮码」后缀。

            同一个仓库在表里会带不同后缀出现多次（26年10月 盛龙路755号 出现 3 种写法），
            对分销商档案来说它们是**同一个收货点**。
            """
            s = re.sub(r"\s+", "", addr or "")
            s = re.sub(r"[-—]*(?:自提|刮码)(?:[+＋](?:自提|刮码))*$", "", s)
            return s.strip("-— ")

        for blk in blocks:
            phone, rcv = receiver_of(blk["addr"])
            raw.append({
                "sheet": sheet, "blk": blk,
                "ship": norm_ship(blk["addr"]),
                "phone": phone or "", "rcv": rcv or "",
            })

    # 称呼归并：同一收货电话下「汪先生」和「汪于成」是同一个人，只留更完整的那个。
    # 只处理 X先生/X女士/X小姐/X师傅 这类敬称，且要求首字相同——
    # 盛龙路 13817654550 上的「王师傅」和「林」首字不同，不会被误并。
    HONORIFIC = re.compile(r"^(.+?)(先生|女士|小姐|师傅)$")
    by_phone = defaultdict(list)
    for r in raw:
        if r["phone"] and r["rcv"]:
            by_phone[r["phone"]].append(r["rcv"])
    alias = {}
    for _phone, names in by_phone.items():
        full = [n for n in names if not HONORIFIC.match(n)]
        for n in names:
            m = HONORIFIC.match(n)
            if not m:
                continue
            head = m.group(1)
            cand = next((f for f in full if f and f[:1] == head[:1] and f != n), None)
            if cand:
                alias[n] = cand

    def contact_key(r):
        return alias.get(r["rcv"], r["rcv"]) or r["phone"] or "__unknown__"

    # ---- 第二遍：按联系人归组 ----
    groups = {}
    for r in raw:
        key = contact_key(r)
        g = groups.setdefault(key, {
            "name": key, "payers": Counter(), "ships": Counter(),
            "phones": Counter(), "blocks": [], "first_sheet": r["sheet"],
        })
        g["payers"].update(Counter(norm_payer(l["pay_company"]) for l in r["blk"]["lines"] if l["pay_company"]))
        if r["phone"]:
            g["phones"][r["phone"]] += 1
        if r["ship"]:
            g["ships"][r["ship"]] += 1
        g["blocks"].append((r["sheet"], r["blk"]))

    # 没有联系人的块统一挂「待确认客户」，等人工指定
    if "__unknown__" in groups:
        groups["__unknown__"]["name"] = "待确认客户"

    # ---- 第三遍：每个联系人建一个分销商，组内每块建一张订单 ----
    used_ids = set()
    for key, g in groups.items():
        name = g["name"]
        contact = "" if key == "__unknown__" else name
        phone = "/".join(p for p, _ in g["phones"].most_common())
        # 付款账户 = 该联系人名下出现过的所有付款方（不含自己）
        accounts = [p for p, _ in g["payers"].most_common() if p and p != name]
        ships = "；".join(s for s, _ in g["ships"].most_common())
        # 收货地址单独一栏（delivery_address），备注只留来源标记
        addr, shipper = delivery_address_of(ships)
        # 认人优先：联系人 → 电话 → 名称。命中已有档案就只刷新收货信息，
        # 名字/编码/付款账户/默认收款抬头全部保留（用户手工改过的东西不动）。
        prev = (
            prev_by_contact.get(contact)
            or prev_by_phone.get(phone)
            or prev_by_name.get(name)
        )
        # 表里没写送货方（旧表只有裸地址）就沿用档案备注里已有的那个
        shipper = shipper or shipper_of_note(prev["note"] if prev else "")
        note = "B2B团单导入" + (f"｜送货方：{shipper}" if shipper else "")
        if prev:
            dist_id = prev["id"]
            used_ids.add(dist_id)
            # 表里没记录收货点时不动档案里已填的地址
            cur.execute(
                "update fx_distributors set contact=?, phone=?, note=?, "
                "delivery_address=case when ?<>'' then ? else delivery_address end, "
                "updated_at=datetime('now') where id=?",
                (prev["contact"] or contact, phone or prev["phone"], note, addr, addr, dist_id),
            )
            # 档案里的付款账户不覆盖；表里新冒出来的付款方只提示，不擅自加进去
            have = {a.strip() for a in prev["pay_accounts"].split(",") if a.strip()}
            fresh = [a for a in accounts if a not in have]
            if fresh:
                stats.setdefault("new_payers", []).append(f"{prev['name']}: {'、'.join(fresh)}")
            shown_accounts = prev["pay_accounts"]
            shown_name = prev["name"]
        else:
            code = next_distributor_code(cur)
            cur.execute(
                "insert into fx_distributors(code,name,contact,phone,email,tax_no,brands,region,pay_accounts,note,delivery_address,active,created_at,updated_at)"
                " values(?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
                (code, name, contact, phone, "", "", BRAND, "", ",".join(accounts), note, addr, 1),
            )
            dist_id = cur.lastrowid
            used_ids.add(dist_id)
            stats["distributors_created"] += 1
            shown_accounts = ",".join(accounts)
            shown_name = name
        stats["dist_groups"] = stats.get("dist_groups", [])
        stats["dist_groups"].append(
            f"{shown_name}（联系人 {contact or '-'}）｜电话:{phone or '-'}"
            f"｜付款账户:{shown_accounts or '无'}｜收货点{len(g['ships'])}个"
        )

        for sheet, blk in g["blocks"]:
            lines = blk["lines"]
            ym = sheet_month(sheet)
            y, m = ym if ym else (2025, 1)
            # 订单日期：块内付款日期多数派 > sheet 月 1 号；串月（如旧表序列号误判）回落 sheet 月
            ds = Counter(l["pay_date"] for l in lines if l["pay_date"])
            odate = ds.most_common(1)[0][0] if ds else None
            # 原表有「付款日期」= 这单的钱已经收到；没有就是没收到（不凭空标已收）
            has_pay_date = bool(odate)
            if odate and abs((odate.year - y) * 12 + (odate.month - m)) > 1:
                odate = None
                has_pay_date = False
            if not odate:
                odate = date(y, m, 1)
            # 旺店通出库单号：块内全部收集（一块多次出库对应多个单号），逗号拼接
            wdts = sorted({l["wdt_no"] for l in lines if l["wdt_no"]})
            wdt_full = ",".join(wdts)
            wdt = wdt_full[:120]
            # 同一张旺店通销售单只能对应一张分销订单。2026-10-28 查到原表把
            # 202610201430 同时写在两个送货块里，导入后生成两张明细重叠、金额
            # 互相打架的重复单。这里显式查重：保留原单号（数据真实），但把冲突
            # 块打进 stats，并在 push_message 里标出来，不让它悄悄过去。
            dup_wdts = [t for t in wdts if t in wdt_owner]
            if dup_wdts:
                stats.setdefault("duplicate_wdt_blocks", []).append(
                    f"{sheet}·{str(blk['addr'])[:30]}："
                    + "、".join(f"{t} 与 {wdt_owner[t]} 共用" for t in dup_wdts)
                )
            for t in wdts:
                wdt_owner.setdefault(t, f"{sheet}·{str(blk['addr'])[:20]}")
            # 推送状态：原表这一列叫「出库-旺店通销售」，有单号就说明这单早就在
            # 旺店通出过库了，属于历史单，系统里不该再出现「推送旺店通」按钮。
            # 没有单号的（2月/26年2月两张表压根没有这一列）单独标 history，
            # 既不冒充已推送，也不再混在待推送里。
            if wdt:
                push_status = "sent"
                push_message = (
                    f"历史团单·原表已含旺店通单号 {wdts[0]}"
                    + (f" 等 {len(wdts)} 个" if len(wdts) > 1 else "")
                    + ("；⚠ 与其它送货块共用同一单号，疑似重复单，需人工核对" if dup_wdts else "")
                    + "，无需重复推送"
                )[:480]
                pushed_at = odate.isoformat()
            else:
                push_status = "history"
                push_message = "历史团单·原表未记录旺店通单号"[:480]
                pushed_at = None
            addr = blk["addr"][:300]
            handling = Counter(l["handling"] for l in lines if l["handling"])
            htxt = "、".join(f"{k}x{v}" for k, v in handling.most_common(3))
            # 付款公司：用户 2026-10-28 定「一个订单里面不可能有多个付款公司」。
            # 实测两张表 16 个块全部只有一家付款公司；万一以后原表混了两家，
            # 取行数多数派落到订单上，同时把异常打出来，不静默吞掉。
            payers = Counter(l["pay_company"] for l in lines if l["pay_company"])
            pay_company = payers.most_common(1)[0][0] if payers else ""
            if len(payers) > 1:
                stats.setdefault("multi_payer_blocks", []).append(
                    f"{sheet}·{str(addr)[:30]}：{'、'.join(f'{k}({v}行)' for k, v in payers.most_common())}"
                )
            order_no = next_order_no(cur, odate)
            total = round(sum(l["subtotal"] for l in lines), 2)
            remark = f"团单导入·{sheet}·送货地:{addr}" + (f"·处理:{htxt}" if htxt else "")
            # 订单标准字段（0024/0025 加的列）：重灌时必须一起写，否则会把
            # 迁移里回填好的原始单号/发货状态/收款状态又冲回默认值。
            cur.execute(
                "insert into fx_orders(order_no,distributor_id,status,order_date,total_amount,remark,"
                "pay_company,original_order_no,source_channel,"
                "payment_status,paid_amount,payment_date,"
                "shipping_status,shipped_at,logistics_company,logistics_no,"
                "wdt_tid,wdt_shop_no,push_status,push_message,pushed_at,created_at,updated_at)"
                " values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
                (
                    order_no, dist_id, "approved", odate.isoformat(), total, remark,
                    pay_company,                      # 一单一值：这单谁付的钱
                    wdt_full[:255],                   # 原始单号（原表「出库-旺店通销售」全量）
                    "团单Excel",
                    "received" if has_pay_date else "unpaid",
                    total if has_pay_date else 0,
                    odate.isoformat() if has_pay_date else None,
                    # 原表有出库单号 = 已经发过货；发货时间/物流原表没有，留空不编
                    "shipped" if wdt else "unshipped",
                    None, "", "",
                    wdt, "", push_status, push_message, pushed_at,
                ),
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
            push_counts[push_status] += 1
            month_totals.setdefault(f"{y}-{m:02d}", [0, 0.0])
            month_totals[f"{y}-{m:02d}"][0] += 1
            month_totals[f"{y}-{m:02d}"][1] += total

    # 表里已经没有的联系人：把它上次导入留下的档案清掉（只清没有任何订单的，
    # 有订单的说明还有别的数据引用它，留着）。手工改过名字的也会一起保留/清理，
    # 因为判断依据是「还有没有订单」，不是「名字变没变」。
    stale = [
        r["id"] for r in prev_all
        if r["id"] not in used_ids
        and cur.execute(
            "select count(*) from fx_orders where distributor_id=?", (r["id"],)
        ).fetchone()[0] == 0
    ]
    if stale:
        marks = ",".join("?" * len(stale))
        cur.execute(f"delete from fx_distributor_pay_links where distributor_id in ({marks})", stale)
        cur.execute(f"delete from fx_distributors where id in ({marks})", stale)
        stats["distributors_removed"] = len(stale)

    wb.close()
    conn.commit()

    print("=== 导入完成（按联系人归组） ===")
    print(f"sheet 解析成功: {stats['sheets_ok']}，跳过: {stats['sheets_skipped']}")
    if skipped_sheets:
        keep = "、".join(f"{y}-{m:02d}" for y, m in sorted(KEEP_YM))
        print(f"  跳过（不在 {keep} 白名单内）: {', '.join(skipped_sheets)}")
    print(f"分销商: {stats['distributors_created']} 个新建，"
          f"{len(used_ids) - stats['distributors_created']} 个沿用已有档案（名称/编码/收款抬头保留）")
    if stats.get("distributors_removed"):
        print(f"  清理已不在表里的档案: {stats['distributors_removed']} 个")
    for line in stats["dist_groups"]:
        print("  " + line)
    if stats.get("new_payers"):
        print("  ⚠ 表里出现了档案中没有的付款方（未自动加入，需要的话请手工添加）：")
        for line in stats["new_payers"]:
            print("    - " + line)
    print(f"订单: {stats['orders']}，明细行: {stats['lines']}")
    if stats.get("multi_payer_blocks"):
        print("  ⚠ 有块里混了多个付款公司（已取行数最多的一家落到订单上，请核对）：")
        for line in stats["multi_payer_blocks"]:
            print("    - " + line)
    if stats.get("duplicate_wdt_blocks"):
        print("  ⚠ 有多张订单共用同一个旺店通单号（同一批出库被重复认领，请核对后删除/拆分明细）：")
        for line in stats["duplicate_wdt_blocks"]:
            print("    - " + line)
    print(
        f"推送状态: 已推送旺店通(原表有单号) {push_counts.get('sent', 0)}，"
        f"历史单(原表无单号) {push_counts.get('history', 0)}，"
        f"待推送 {push_counts.get('', 0)}"
    )
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
