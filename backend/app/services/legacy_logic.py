from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    ImportBatch,
    LegacyIncomeRow,
    LegacyInventoryRow,
    LegacyLogisticsRow,
    LegacyPrepaidRow,
    LegacyPurchaseRow,
    LegacyRebateRow,
    LegacyShipmentRow,
)

D = Decimal
BASE_PURCHASE_DISCOUNT = D("0.8")
WAREHOUSE_MONTHLY = D("0.85") * D("30") * D("400")  # 原表 = 10,200
PERSONNEL_MONTHLY = (D("24000") + D("20000")) * D("1.45")  # 原表 = 63,800


@dataclass(frozen=True)
class StoreRule:
    shop: str
    platform: str
    sales_discount: Decimal
    brand_filter: str = ""
    note: str = ""


STORE_RULES: tuple[StoreRule, ...] = (
    StoreRule("红盒子-快手-格力高零食旗舰店", "快手", D("0.52"), note="原表按照0.52折计算"),
    StoreRule("梦奇思-抖音-梦奇思零食专营店", "抖音", D("0.52"), note="原表按照0.52折计算"),
    StoreRule("聚小翻-PDD安小悠食品专营店", "拼多多", D("0.65"), "格力高"),
    StoreRule("聚小翻-PDD聚小翻零食专营店", "拼多多", D("0.65"), "格力高"),
    StoreRule("归益-拼多多-归益零食专营店", "拼多多", D("0.65"), "格力高"),
    StoreRule("聚小翻-得物-聚小翻（上海）贸易有限公司", "得物", D("0.65")),
    StoreRule("聚小翻-快手-聚小翻食品专营店", "快手", D("0.65")),
    StoreRule("聚小翻-京东-慧采", "京东", D("0.65")),
    StoreRule("聚小翻-1688-批发", "1688/支付宝", D("0.65")),
    StoreRule("红盒子-格力高-寄样渠道", "寄样", D("0.65"), note="原表无结算收入来源，收入按0"),
    StoreRule("红盒子-格力高-分销渠道", "分销", D("0.65")),
)

REBATE_FALLBACK = {
    "快手": {"运营费": D("0.05"), "物流费": D("0.159"), "广告费": D("0.086") + D("0.115")},
    "抖音": {"运营费": D("0.05"), "物流费": D("0.17"), "广告费": D("0.08") + D("0.11")},
}
REBATE_TAX_FACTOR = D("1.06")


def _n(v) -> Decimal:
    return D(str(v or 0))


def latest_legacy_batch(db: Session) -> ImportBatch | None:
    return db.scalar(
        select(ImportBatch)
        .where(ImportBatch.kind == "legacy_workbook", ImportBatch.status == "imported")
        .order_by(ImportBatch.id.desc())
        .limit(1)
    )


def _sum(db: Session, model, column, *conditions) -> Decimal:
    value = db.scalar(select(func.coalesce(func.sum(column), 0)).select_from(model).where(*conditions))
    return _n(value)


def _store_row(db: Session, batch_id: int, month: str, rule: StoreRule) -> dict:
    income_cond = [LegacyIncomeRow.batch_id == batch_id, LegacyIncomeRow.period == month, LegacyIncomeRow.shop_name == rule.shop]
    shipment_cond = [LegacyShipmentRow.batch_id == batch_id, LegacyShipmentRow.recon_month == month, LegacyShipmentRow.shop_name == rule.shop]
    if rule.brand_filter:
        income_cond.append(LegacyIncomeRow.brand == rule.brand_filter)
        shipment_cond.append(LegacyShipmentRow.brand == rule.brand_filter)
    revenue = _sum(db, LegacyIncomeRow, LegacyIncomeRow.amount, *income_cond)
    goods_cost = _sum(db, LegacyShipmentRow, LegacyShipmentRow.goods_cost, *shipment_cond)
    logistics = _sum(
        db, LegacyLogisticsRow, LegacyLogisticsRow.amount,
        LegacyLogisticsRow.batch_id == batch_id,
        LegacyLogisticsRow.period == month,
        LegacyLogisticsRow.shop_name == rule.shop,
    )
    standard_untaxed = goods_cost / BASE_PURCHASE_DISCOUNT if goods_cost else D("0")
    negotiated_cost = standard_untaxed * rule.sales_discount
    adjusted_profit = revenue - negotiated_cost - logistics
    standard_discount = revenue / standard_untaxed if standard_untaxed else D("0")
    return {
        "shop_name": rule.shop,
        "platform": rule.platform,
        "brand_filter": rule.brand_filter or None,
        "sales_discount_rate": float(rule.sales_discount),
        "baseline_purchase_discount": float(BASE_PURCHASE_DISCOUNT),
        "revenue": float(revenue),
        "goods_cost_8_discount_untaxed": float(goods_cost),
        "standard_untaxed_amount": float(standard_untaxed),
        "negotiated_cost": float(negotiated_cost),
        "logistics": float(logistics),
        "adjusted_profit": float(adjusted_profit),
        "standard_discount_ratio": float(standard_discount),
        "note": rule.note,
    }


def _expected_rebate(db: Session, batch_id: int, month: str, legacy_ttl_only: bool = True) -> tuple[dict[str, Decimal], Decimal, str]:
    conditions = [LegacyRebateRow.batch_id == batch_id, LegacyRebateRow.period == month]
    if legacy_ttl_only:
        # TTL!68:70 明确只 SUMIFS 返利核销情况 H:H；H 是左侧“快手”区，抖音 X:X 未被 TTL 引用。
        conditions.append(LegacyRebateRow.channel == "快手")
    rows = db.execute(
        select(LegacyRebateRow.category, func.coalesce(func.sum(LegacyRebateRow.expected_amount_tax), 0))
        .where(*conditions)
        .group_by(LegacyRebateRow.category)
    ).all()
    by_cat = {str(cat or ""): _n(v) for cat, v in rows if cat}
    total = sum(by_cat.values(), D("0"))
    if total:
        label = "TTL原表口径：返利核销情况H列（快手）" if legacy_ttl_only else "返利核销情况：快手+抖音全部应核销"
        return by_cat, total, label

    # API 阶段没有返利明细时，用采购核销月 + 渠道规则回算。
    by_cat = {"运营费": D("0"), "物流费": D("0"), "广告费": D("0")}
    channels = ("快手",) if legacy_ttl_only else tuple(REBATE_FALLBACK)
    for channel in channels:
        rules = REBATE_FALLBACK[channel]
        basis = _sum(
            db, LegacyPurchaseRow, LegacyPurchaseRow.invoice_amount_untaxed,
            LegacyPurchaseRow.batch_id == batch_id,
            LegacyPurchaseRow.recon_month == month,
            LegacyPurchaseRow.channel == channel,
        )
        for cat, rate in rules.items():
            by_cat[cat] += basis * rate * REBATE_TAX_FACTOR
    label = "TTL回算：快手采购利计×返利规则×1.06" if legacy_ttl_only else "全渠道回算：采购利计×渠道返利规则×1.06"
    return by_cat, sum(by_cat.values(), D("0")), label


def monthly_summary(db: Session, month: str) -> dict:
    batch = latest_legacy_batch(db)
    if not batch:
        return {"ready": False, "month": month, "message": "请先导入核销表0610.xlsx"}

    stores = [_store_row(db, batch.id, month, rule) for rule in STORE_RULES]
    revenue = sum((_n(x["revenue"]) for x in stores), D("0"))
    goods_cost = sum((_n(x["goods_cost_8_discount_untaxed"]) for x in stores), D("0"))
    logistics = sum((_n(x["logistics"]) for x in stores), D("0"))

    # TTL 月度第58行：结算收入 - 货品成本。物流在第62/66行作为支出扣除。
    sales_gross_profit = revenue - goods_cost
    standard_discount_ratio = revenue / (goods_cost / BASE_PURCHASE_DISCOUNT) if goods_cost else D("0")

    expected_by_cat, expected_rebate, rebate_source = _expected_rebate(db, batch.id, month, True)
    all_expected_by_cat, all_expected_rebate, all_rebate_source = _expected_rebate(db, batch.id, month, False)
    legacy_arrived = _sum(
        db, LegacyRebateRow, LegacyRebateRow.invoice_amount,
        LegacyRebateRow.batch_id == batch.id,
        LegacyRebateRow.period == month,
        LegacyRebateRow.channel == "快手",
        LegacyRebateRow.invoice_status == "已经到账",
    )
    # 原表存在“开票到账=已经开票、到账时间=已经到账”的数据；单独提示，不擅自改原表口径。
    observed_arrived = _sum(
        db, LegacyRebateRow, LegacyRebateRow.invoice_amount,
        LegacyRebateRow.batch_id == batch.id,
        LegacyRebateRow.period == month,
        LegacyRebateRow.channel == "快手",
        LegacyRebateRow.arrival_marker == "已经到账",
    )
    all_legacy_arrived = _sum(
        db, LegacyRebateRow, LegacyRebateRow.invoice_amount,
        LegacyRebateRow.batch_id == batch.id,
        LegacyRebateRow.period == month,
        LegacyRebateRow.invoice_status == "已经到账",
    )
    all_observed_arrived = _sum(
        db, LegacyRebateRow, LegacyRebateRow.invoice_amount,
        LegacyRebateRow.batch_id == batch.id,
        LegacyRebateRow.period == month,
        LegacyRebateRow.arrival_marker == "已经到账",
    )
    prepaid = _sum(
        db, LegacyPrepaidRow, LegacyPrepaidRow.amount,
        LegacyPrepaidRow.batch_id == batch.id,
        LegacyPrepaidRow.period == month,
    )
    purchase_payment = _sum(
        db, LegacyPurchaseRow, LegacyPurchaseRow.invoice_amount_tax,
        LegacyPurchaseRow.batch_id == batch.id,
        LegacyPurchaseRow.recon_month == month,
    )
    inventory = _sum(
        db, LegacyInventoryRow, LegacyInventoryRow.amount,
        LegacyInventoryRow.batch_id == batch.id,
        LegacyInventoryRow.period == month,
    )
    expense_total = WAREHOUSE_MONTHLY + logistics + prepaid + PERSONNEL_MONTHLY

    expected_profit = sales_gross_profit + expected_rebate - expense_total
    arrived_profit = sales_gross_profit + legacy_arrived - expense_total
    observed_arrived_profit = sales_gross_profit + observed_arrived - expense_total
    all_channel_expected_profit = sales_gross_profit + all_expected_rebate - expense_total

    return {
        "ready": True,
        "month": month,
        "batch": {"id": batch.id, "filename": batch.filename, "rows": batch.row_count},
        "logic": {
            "baseline_purchase_discount": 0.8,
            "rebate_tax_factor": 1.06,
            "warehouse_monthly": float(WAREHOUSE_MONTHLY),
            "personnel_monthly": float(PERSONNEL_MONTHLY),
            "description": "严格按0610核销表月度口径：收入-8折未税货品成本；物流进入支出；店铺毛利另按0.52/0.65折算。",
        },
        "totals": {
            "settlement_revenue": float(revenue),
            "goods_cost_8_discount_untaxed": float(goods_cost),
            "sales_gross_profit": float(sales_gross_profit),
            "standard_discount_ratio": float(standard_discount_ratio),
            "logistics": float(logistics),
            "warehouse": float(WAREHOUSE_MONTHLY),
            "prepaid": float(prepaid),
            "personnel": float(PERSONNEL_MONTHLY),
            "expense_total": float(expense_total),
            "expected_rebate": float(expected_rebate),
            "all_channel_expected_rebate": float(all_expected_rebate),
            "legacy_arrived_rebate": float(legacy_arrived),
            "observed_arrived_rebate": float(observed_arrived),
            "all_channel_legacy_arrived_rebate": float(all_legacy_arrived),
            "all_channel_observed_arrived_rebate": float(all_observed_arrived),
            "expected_rebate_profit": float(expected_profit),
            "all_channel_expected_rebate_profit": float(all_channel_expected_profit),
            "legacy_arrived_rebate_profit": float(arrived_profit),
            "observed_arrived_rebate_profit": float(observed_arrived_profit),
            "purchase_payment_tax": float(purchase_payment),
            "ending_inventory": float(inventory),
        },
        "rebate": {
            "source": rebate_source,
            "all_channel_source": all_rebate_source,
            "expected_by_category": {k: float(v) for k, v in expected_by_cat.items()},
            "all_channel_expected_by_category": {k: float(v) for k, v in all_expected_by_cat.items()},
            "expected_total": float(expected_rebate),
            "all_channel_expected_total": float(all_expected_rebate),
            "ttl_omitted_douyin_expected": float(all_expected_rebate - expected_rebate),
            "legacy_arrived_total": float(legacy_arrived),
            "observed_arrival_marker_total": float(observed_arrived),
            "difference_expected_vs_legacy_arrived": float(expected_rebate - legacy_arrived),
            "status_warning": (
                ("TTL第68-72行只引用快手返利列，未引用抖音返利列；本月被TTL遗漏的抖音应核销返利为 "
                 f"{float(all_expected_rebate - expected_rebate):,.2f} 元。 " if all_expected_rebate != expected_rebate else "") +
                ("原表TTL按‘开票到账’列=已经到账统计实际到账；当前文件部分记录在‘到账时间’列写了已经到账，"
                 "因此系统同时保留原表口径与观察口径。" if observed_arrived != legacy_arrived else "")
            ),
        },
        "stores": stores,
    }


def yearly_series(db: Session, year: int) -> dict:
    months = [f"{year}-{m:02d}" for m in range(1, 13)]
    rows = []
    for month in months:
        s = monthly_summary(db, month)
        if not s.get("ready"):
            return s
        t = s["totals"]
        rows.append({
            "month": month,
            "revenue": t["settlement_revenue"],
            "goods_cost": t["goods_cost_8_discount_untaxed"],
            "sales_gross_profit": t["sales_gross_profit"],
            "expected_rebate": t["expected_rebate"],
            "legacy_arrived_rebate": t["legacy_arrived_rebate"],
            "expense_total": t["expense_total"],
            "expected_rebate_profit": t["expected_rebate_profit"],
            "legacy_arrived_rebate_profit": t["legacy_arrived_rebate_profit"],
        })
    return {"ready": True, "year": year, "months": rows}
