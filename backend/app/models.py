from datetime import datetime
from decimal import Decimal
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

MONEY = Numeric(18, 4)
RATE = Numeric(10, 6)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="imported", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class StockoutOrder(Base):
    __tablename__ = "stockout_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), default="wdt_api", index=True)
    stockout_id: Mapped[str] = mapped_column(String(40), default="")
    stockout_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    trade_no: Mapped[str] = mapped_column(String(40), default="", index=True)
    src_trade_no: Mapped[str] = mapped_column(String(255), default="", index=True)
    src_tids: Mapped[str] = mapped_column(String(255), default="", index=True)
    shop_no: Mapped[str] = mapped_column(String(40), default="", index=True)
    shop_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    platform_id: Mapped[str] = mapped_column(String(20), default="", index=True)
    consign_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    pay_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="")
    refund_status: Mapped[str] = mapped_column(String(16), default="")
    goods_count: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    goods_total_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    goods_total_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    receivable: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    actual_receivable: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    paid: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    post_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    package_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    modified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    items: Mapped[list["StockoutItem"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class StockoutItem(Base):
    __tablename__ = "stockout_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("stockout_orders.id", ondelete="CASCADE"), index=True)
    rec_id: Mapped[str] = mapped_column(String(40), default="")
    spec_no: Mapped[str] = mapped_column(String(80), default="", index=True)
    barcode: Mapped[str] = mapped_column(String(80), default="", index=True)
    goods_no: Mapped[str] = mapped_column(String(80), default="", index=True)
    goods_name: Mapped[str] = mapped_column(String(255), default="")
    spec_name: Mapped[str] = mapped_column(String(255), default="")
    src_tid: Mapped[str] = mapped_column(String(80), default="", index=True)
    src_oid: Mapped[str] = mapped_column(String(80), default="", index=True)
    goods_count: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    sell_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    total_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    share_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    cost_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    share_post: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    refund_status: Mapped[str] = mapped_column(String(16), default="")
    order: Mapped[StockoutOrder] = relationship(back_populates="items")


class SettlementRow(Base):
    __tablename__ = "settlement_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    platform: Mapped[str] = mapped_column(String(40), default="", index=True)
    shop_name: Mapped[str] = mapped_column(String(128), default="", index=True)
    order_no: Mapped[str] = mapped_column(String(120), default="", index=True)
    settle_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    gross_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    platform_fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    refund_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    other_fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    net_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class BankRow(Base):
    __tablename__ = "bank_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    txn_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    reference_no: Mapped[str] = mapped_column(String(120), default="", index=True)
    counterparty: Mapped[str] = mapped_column(String(255), default="", index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    memo: Mapped[str] = mapped_column(String(500), default="")
    raw_json: Mapped[str] = mapped_column(Text, default="{}")


class ReconResult(Base):
    __tablename__ = "recon_results"
    __table_args__ = (UniqueConstraint("settlement_row_id", name="uq_recon_settlement"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    settlement_row_id: Mapped[int] = mapped_column(ForeignKey("settlement_rows.id"), index=True)
    stockout_order_id: Mapped[int | None] = mapped_column(ForeignKey("stockout_orders.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    match_method: Mapped[str] = mapped_column(String(40), default="")
    expected_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    settled_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    difference: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    note: Mapped[str] = mapped_column(String(500), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# --- 0610 核销表兼容层：保留 Excel 原始口径，不依赖旺店通 API ---
class LegacyShipmentRow(Base):
    __tablename__ = "legacy_shipment_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    recon_month: Mapped[str] = mapped_column(String(7), default="", index=True)
    actual_profit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    settlement_income: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    standard_cost_untaxed: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    fee_ratio: Mapped[Decimal] = mapped_column(RATE, default=Decimal("0"))
    order_type: Mapped[str] = mapped_column(String(80), default="")
    stockout_no: Mapped[str] = mapped_column(String(80), default="", index=True)
    order_no: Mapped[str] = mapped_column(String(120), default="", index=True)
    src_order_no: Mapped[str] = mapped_column(String(1024), default="", index=True)
    consign_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    shop_name: Mapped[str] = mapped_column(String(160), default="", index=True)
    warehouse: Mapped[str] = mapped_column(String(160), default="")
    logistics_company: Mapped[str] = mapped_column(String(100), default="")
    logistics_no: Mapped[str] = mapped_column(String(120), default="")
    goods_cost: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"), index=True)
    order_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    goods_qty: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    estimated_profit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    shipment_profit: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    brand: Mapped[str] = mapped_column(String(80), default="", index=True)


class LegacyIncomeRow(Base):
    __tablename__ = "legacy_income_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    source_sheet: Mapped[str] = mapped_column(String(40), index=True)
    platform: Mapped[str] = mapped_column(String(30), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    shop_name: Mapped[str] = mapped_column(String(160), default="", index=True)
    order_no: Mapped[str] = mapped_column(String(160), default="", index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    brand: Mapped[str] = mapped_column(String(80), default="", index=True)
    txn_type: Mapped[str] = mapped_column(String(120), default="")
    raw_note: Mapped[str] = mapped_column(String(500), default="")


class LegacyLogisticsRow(Base):
    __tablename__ = "legacy_logistics_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    shop_name: Mapped[str] = mapped_column(String(160), index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))


class LegacyPrepaidRow(Base):
    __tablename__ = "legacy_prepaid_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    channel: Mapped[str] = mapped_column(String(80), default="")
    category: Mapped[str] = mapped_column(String(120), default="")
    amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    note: Mapped[str] = mapped_column(String(500), default="")


class LegacyInventoryRow(Base):
    __tablename__ = "legacy_inventory_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    warehouse: Mapped[str] = mapped_column(String(160), default="")
    barcode: Mapped[str] = mapped_column(String(100), default="")
    goods_name: Mapped[str] = mapped_column(String(255), default="")


class LegacyPurchaseRow(Base):
    __tablename__ = "legacy_purchase_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    recon_month: Mapped[str] = mapped_column(String(7), index=True)
    purchase_month: Mapped[str] = mapped_column(String(7), index=True)
    channel: Mapped[str] = mapped_column(String(40), index=True)
    product_code: Mapped[str] = mapped_column(String(100), default="")
    barcode: Mapped[str] = mapped_column(String(100), default="")
    goods_name: Mapped[str] = mapped_column(String(255), default="")
    discount_rate: Mapped[Decimal] = mapped_column(RATE, default=Decimal("0.8"))
    qty_units: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    list_amount_untaxed: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    invoice_amount_untaxed: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    invoice_amount_tax: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    subbrand: Mapped[str] = mapped_column(String(80), default="", index=True)


class LegacyRebateRow(Base):
    __tablename__ = "legacy_rebate_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    channel: Mapped[str] = mapped_column(String(40), index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    basis_amount_untaxed: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    category: Mapped[str] = mapped_column(String(80), index=True)
    subcategory: Mapped[str] = mapped_column(String(120), default="")
    rate: Mapped[Decimal] = mapped_column(RATE, default=Decimal("0"))
    expected_amount_tax: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    invoice_category: Mapped[str] = mapped_column(String(80), default="")
    invoice_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    invoice_status: Mapped[str] = mapped_column(String(40), default="", index=True)
    arrival_marker: Mapped[str] = mapped_column(String(80), default="")
    actual_total: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    difference: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))

# --- V0.7 模块化数据中心：原始资料入口 / 手工调整 / 采购返利规则 ---
class ModuleRawRecord(Base):
    __tablename__ = "module_raw_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    module: Mapped[str] = mapped_column(String(40), index=True)
    row_no: Mapped[int] = mapped_column(Integer, default=0)
    period: Mapped[str] = mapped_column(String(7), default="", index=True)
    external_key: Mapped[str] = mapped_column(String(255), default="", index=True)
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ModuleMappingProfile(Base):
    __tablename__ = "module_mapping_profiles"
    __table_args__ = (UniqueConstraint("module", "name", name="uq_module_mapping_profile"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    module: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(120), default="默认映射")
    mapping_json: Mapped[str] = mapped_column(Text, default="{}")
    active: Mapped[int] = mapped_column(Integer, default=1, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ModuleStandardRecord(Base):
    __tablename__ = "module_standard_records"
    __table_args__ = (UniqueConstraint("batch_id", "row_no", name="uq_module_standard_batch_row"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id", ondelete="CASCADE"), index=True)
    module: Mapped[str] = mapped_column(String(40), index=True)
    row_no: Mapped[int] = mapped_column(Integer, default=0)
    period: Mapped[str] = mapped_column(String(7), default="", index=True)
    external_key: Mapped[str] = mapped_column(String(255), default="", index=True)
    standard_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(32), default="valid", index=True)
    error_text: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ManualAdjustment(Base):
    __tablename__ = "manual_adjustments"
    id: Mapped[int] = mapped_column(primary_key=True)
    module: Mapped[str] = mapped_column(String(40), default="sales-outbound", index=True)
    target_type: Mapped[str] = mapped_column(String(24), default="order", index=True)
    target_key: Mapped[str] = mapped_column(String(255), index=True)
    period: Mapped[str] = mapped_column(String(7), default="", index=True)
    field_name: Mapped[str] = mapped_column(String(80), default="")
    operation: Mapped[str] = mapped_column(String(24), default="add")
    decimal_value: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    text_value: Mapped[str] = mapped_column(String(1000), default="")
    reason: Mapped[str] = mapped_column(String(1000), default="")
    active: Mapped[int] = mapped_column(Integer, default=1, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# --- 红盒子供销平台：分销业务数据模型 ---
# 状态直接存字符串，展示层负责中文翻译；monetary 全部 Decimal。
class FxDistributor(Base):
    __tablename__ = "fx_distributors"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160), index=True)
    contact: Mapped[str] = mapped_column(String(80), default="")
    phone: Mapped[str] = mapped_column(String(60), default="")
    email: Mapped[str] = mapped_column(String(160), default="")
    region: Mapped[str] = mapped_column(String(120), default="")
    note: Mapped[str] = mapped_column(String(1000), default="")
    active: Mapped[int] = mapped_column(Integer, default=1, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FxProduct(Base):
    __tablename__ = "fx_products"
    id: Mapped[int] = mapped_column(primary_key=True)
    # sku 是系统内的唯一商品编码；从旺店通同步时取规格编码 spec_no
    sku: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    spec: Mapped[str] = mapped_column(String(160), default="")
    unit: Mapped[str] = mapped_column(String(20), default="箱")
    base_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    note: Mapped[str] = mapped_column(String(500), default="")
    active: Mapped[int] = mapped_column(Integer, default=1, index=True)
    # --- 旺店通商品档案字段（由「从旺店通同步商品」回填，手工建品时留空）---
    spec_no: Mapped[str] = mapped_column(String(160), default="", index=True)
    goods_no: Mapped[str] = mapped_column(String(80), default="", index=True)
    barcode: Mapped[str] = mapped_column(String(60), default="")
    category: Mapped[str] = mapped_column(String(100), default="")
    brand: Mapped[str] = mapped_column(String(100), default="")
    source: Mapped[str] = mapped_column(String(20), default="manual", index=True)
    wdt_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FxOrder(Base):
    __tablename__ = "fx_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("fx_distributors.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    order_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    total_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    remark: Mapped[str] = mapped_column(String(1000), default="")
    # 预留：未来推送旺店通时的原始单号 tid
    wdt_tid: Mapped[str] = mapped_column(String(120), default="")
    # 推送旺店通的记录："" 未推送 / sent 已推送 / failed 失败，message 存最后一次结果
    push_status: Mapped[str] = mapped_column(String(16), default="")
    push_message: Mapped[str] = mapped_column(String(500), default="")
    pushed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    distributor: Mapped[FxDistributor] = relationship()
    lines: Mapped[list["FxOrderLine"]] = relationship(back_populates="order", cascade="all, delete-orphan")


class FxOrderLine(Base):
    __tablename__ = "fx_order_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("fx_orders.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("fx_products.id"), nullable=True, index=True)
    # 下单时快照，商品资料后续变化不影响历史订单
    goods_no: Mapped[str] = mapped_column(String(80), default="")
    spec_no: Mapped[str] = mapped_column(String(160), default="")
    goods_name: Mapped[str] = mapped_column(String(255), default="")
    spec_name: Mapped[str] = mapped_column(String(160), default="")
    quantity: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    unit_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    subtotal: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    order: Mapped[FxOrder] = relationship(back_populates="lines")


class FxPrice(Base):
    """经销商专属价（协议价）。

    同一商品对不同分销商给不同价：有覆盖价用覆盖价，没有就用商品基础价。
    (distributor_id, product_id) 唯一， upsert 语义；删除覆盖价即回到基础价。
    """
    __tablename__ = "fx_prices"
    __table_args__ = (UniqueConstraint("distributor_id", "product_id", name="uq_fx_price_dp"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("fx_distributors.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("fx_products.id"), index=True)
    price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    note: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class RebateRule(Base):
    __tablename__ = "rebate_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand: Mapped[str] = mapped_column(String(100), default="*", index=True)
    channel: Mapped[str] = mapped_column(String(60), default="*", index=True)
    category: Mapped[str] = mapped_column(String(80), default="渠道返利")
    basis_field: Mapped[str] = mapped_column(String(80), default="invoice_amount_untaxed")
    rate: Mapped[Decimal] = mapped_column(RATE, default=Decimal("0"))
    tax_factor: Mapped[Decimal] = mapped_column(RATE, default=Decimal("1"))
    effective_from: Mapped[str] = mapped_column(String(7), default="", index=True)
    effective_to: Mapped[str] = mapped_column(String(7), default="", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    active: Mapped[int] = mapped_column(Integer, default=1, index=True)
    note: Mapped[str] = mapped_column(String(1000), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

