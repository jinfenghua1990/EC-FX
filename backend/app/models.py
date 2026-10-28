from datetime import datetime
from decimal import Decimal
from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

MONEY = Numeric(18, 4)
RATE = Numeric(10, 6)


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


class ManualAdjustment(Base):
    """销售出库调整记录：只改「生效值」，不动旺店通 RAW 数据。"""
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
    # 收货地址：发货送到这里。2026-10-28 起从备注里独立出来单独一个字段
    # （原来只能写进 note，例如「B2B团单导入｜收货：上海市松江区盛龙路755号7栋1楼、13817654550、林」）。
    delivery_address: Mapped[str] = mapped_column(String(500), default="")
    # 税号：开票用。新建时可作为识别码，允许为空（很多小分销商没有/不提供）
    tax_no: Mapped[str] = mapped_column(String(80), default="", index=True)
    # brands：该分销商被授权分销的品牌，逗号分隔存（如 "格力高,桂格"）。
    # 空 = 还没录授权品牌：顶部选了具体品牌时它不出现（只在「全部品牌」下可见），
    # 绝不默认成「全品牌通吃」，免得给它看到别的品牌的货。
    brands: Mapped[str] = mapped_column(String(500), default="")
    # pay_accounts：旧版付款账户快照（付款方公司/个人名，逗号分隔）。
    # 2026-10-28 起付款账户明细改存 fx_distributor_pay_accounts；这一列只保留兼容，
    # 每次保存/合并会同步账户名，旧代码/旧导入模板仍可读。
    pay_accounts: Mapped[str] = mapped_column(String(800), default="")
    # region（区域）已停用：2026-10-28 起页面不再显示也不再录入，旧数据保留在库里不删
    region: Mapped[str] = mapped_column(String(120), default="")
    note: Mapped[str] = mapped_column(String(1000), default="")
    active: Mapped[int] = mapped_column(Integer, default=1, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 付款账户明细：一个客户可以挂多个付款抬头（多对多，见 fx_distributor_pay_links）。
    pay_links: Mapped[list["FxDistributorPayLink"]] = relationship(
        back_populates="distributor",
        cascade="all, delete-orphan",
        order_by="FxDistributorPayLink.sort_order",
    )


class FxPayEntity(Base):
    """付款抬头主档（全平台唯一，一份资料多方共用）。

    2026-10-28 重构：以前每个分销商名下各存一份「湖南正祥医药有限公司」，
    同一抬头重复三四条、税号银行信息各改各的。现在抬头独立成主数据：
    - 同名抬头全库只一条，谁都可以关联使用（多维度交叉：抬头 ↔ 多个分销商）；
    - 在任何一个客户里补全税号/开户行，其他关联客户立刻同步；
    - 旧表 fx_distributor_pay_accounts 保留作历史快照，不再写入。
    """
    __tablename__ = "fx_pay_entities"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 付款方名称（公司全称或个人姓名），全局唯一——去重合并的锚点
    name: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    # 该付款主体自己的税号，和客户主档税号可以不同
    tax_no: Mapped[str] = mapped_column(String(80), default="")
    bank_name: Mapped[str] = mapped_column(String(160), default="")
    bank_branch: Mapped[str] = mapped_column(String(160), default="")
    bank_account: Mapped[str] = mapped_column(String(80), default="")
    # 银行账户户名 / 收款人；客户收款时可用来核对「谁收、谁付」
    account_holder: Mapped[str] = mapped_column(String(160), default="")
    # company = 对公，personal = 对私
    account_type: Mapped[str] = mapped_column(String(20), default="company")
    note: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FxDistributorPayLink(Base):
    """分销商 ↔ 付款抬头 关联（多对多交叉）。

    同一付款抬头（如关联公司代付）可以挂在多个分销商名下；每个分销商
    各自维护默认抬头和收款备注，抬头本身的税号/银行资料只在主档存一份。
    """
    __tablename__ = "fx_distributor_pay_links"
    __table_args__ = (
        UniqueConstraint("distributor_id", "entity_id", name="uq_fx_pay_link"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    distributor_id: Mapped[int] = mapped_column(
        ForeignKey("fx_distributors.id", ondelete="CASCADE"), index=True
    )
    entity_id: Mapped[int] = mapped_column(
        ForeignKey("fx_pay_entities.id", ondelete="RESTRICT"), index=True
    )
    is_default: Mapped[int] = mapped_column(Integer, default=0)
    # 客户收款备注/用途：如「货款」「预付款」「关联公司代付」，只做说明，不存金额
    collection_note: Mapped[str] = mapped_column(String(500), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    distributor: Mapped["FxDistributor"] = relationship(back_populates="pay_links")
    entity: Mapped["FxPayEntity"] = relationship(lazy="joined")


class FxBrand(Base):
    """品牌主数据：顶部品牌切换、商品品牌、分销商授权品牌共用同一份清单。"""
    __tablename__ = "fx_brands"

    id: Mapped[int] = mapped_column(primary_key=True)
    # 品牌全名（产品档案里的原值，如「格力高/Glico」）
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    # 顶栏和卡片上显示的短名；空 = 自动取 name 第一个「/」前的部分
    short_name: Mapped[str] = mapped_column(String(60), default="")
    code: Mapped[str] = mapped_column(String(60), default="")
    # 品牌 LOGO 文件名（brand-logos/ 目录）或完整 URL；空 = 用品牌首字徽标
    logo: Mapped[str] = mapped_column(String(255), default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[int] = mapped_column(Integer, default=1, index=True)
    note: Mapped[str] = mapped_column(String(500), default="")
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
    # 箱规：1 箱 = pack_qty 个 unit。旺店通同步取 aux_unit / aux_unit_name（如「36/箱」），手工建品可自己填。
    # pack_qty=0 表示没维护箱规，页面显示「—」，不当成 1 箱。
    pack_qty: Mapped[int] = mapped_column(Integer, default=0)
    pack_unit: Mapped[str] = mapped_column(String(20), default="箱")
    # base_price = 品牌原价（含税，旺店通批发价），是成本基准；
    # tier1_price = 一级分销价（含税），我们卖给一级分销商的价，人工设定，0 = 未定价。
    # 毛利 = tier1_price - base_price，页面直接算，不存快照（改价后实时跟着变）。
    base_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    tier1_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    # --- 采购价（含税，按基本单位/盒）：从旺店通「采购单」采集，是真实拿货成本 ---
    # 语义：purchase_order_query.php → details_list[].price（= tax_price，已含税，
    # 且已是折后实付价，例如格力高牌价含税 5.83/盒、8 折后 4.66/盒，采购单里就是 4.66）。
    # 与 base_price 同口径同单位，可直接对比。0 = 从未采购过/无采购记录。
    # 注意：不要拿 details_list[].purchase_price 当成本 —— 实测它是历史不含税价，
    # 瑞士莲 600g 上 price=103 而 purchase_price=92.42，比值 1.1145 不是 1.13，口径不可靠。
    # 取价按「仓库」分组（同一 SKU 在不同仓是不同价，跨仓混算会得出假涨跌）。
    # purchase_price 存的是「主成本仓」的最近一次价；其余仓的价见 fx_purchase_prices 表。
    purchase_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    purchase_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    purchase_no: Mapped[str] = mapped_column(String(40), default="")
    # 主价的来源：哪个仓、哪个供应商。页面悬浮提示用，避免「这价哪来的」说不清。
    purchase_warehouse: Mapped[str] = mapped_column(String(160), default="")
    purchase_provider: Mapped[str] = mapped_column(String(160), default="")
    # 手工指定成本仓（空 = 自动选）。各品牌取价逻辑不同，由用户按品牌统一下发。
    # 填了就在算价时强制用这个仓的价，不受「自动选主仓」规则影响。
    cost_warehouse: Mapped[str] = mapped_column(String(160), default="")
    # --- 牌价（含税，按基本单位）：只用于算分销折扣率，不参与利润计算 ---
    # 2026-10-27 用户明确：牌价只有格力高需要维护（从品牌订货单 Excel 的「盒(未税)×1.13」来），
    # 其他品牌不维护，留 0。别把它当成成本或 base_price 的替代品。
    # 为什么不复用 base_price：base_price 取旺店通 wholesale_price，实测语义不统一
    # （百醇48g 存的是采购价 5.05、轻松熊礼盒存的是牌价 29.00、瑞士莲600g 存的是旧价 88.00）。
    list_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    # --- 成本核对状态 ---
    # "" = 正常；"pending" = 采购价存疑，待人工核对（该商品不出毛利，避免拿错价算利润）。
    # 触发场景：源单把两个 SKU 单价录反（CG202512090001/2 的婚礼装 41 元、悠享装 200g 165 元）。
    cost_status: Mapped[str] = mapped_column(String(16), default="", index=True)
    cost_note: Mapped[str] = mapped_column(String(300), default="")
    note: Mapped[str] = mapped_column(String(500), default="")
    active: Mapped[int] = mapped_column(Integer, default=1, index=True)
    # 热销标记：1 = 下单页展示，0 = 不在下单页展示。与 active 独立，停用后即使标记也不下单。
    is_hot: Mapped[int] = mapped_column(Integer, default=0, index=True)
    # --- 旺店通商品档案字段（由「从旺店通同步商品」回填，手工建品时留空）---
    spec_no: Mapped[str] = mapped_column(String(160), default="", index=True)
    goods_no: Mapped[str] = mapped_column(String(80), default="", index=True)
    barcode: Mapped[str] = mapped_column(String(60), default="")
    # category = 分类（细的：百奇/百醇/麦片/咖啡…）；top_category = 大类（更粗的一层）。
    # 两者并存、互不覆盖 —— 用户 2026-10-28 明确「分类要保留，后续有用」，大类单独一列自己维护。
    category: Mapped[str] = mapped_column(String(100), default="")
    top_category: Mapped[str] = mapped_column(String(100), default="", index=True)
    brand: Mapped[str] = mapped_column(String(100), default="")
    source: Mapped[str] = mapped_column(String(20), default="manual", index=True)
    wdt_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FxPriceHistory(Base):
    """改价流水（只增不改）：一级分销价的每次变更都留底，方便对账追溯。

    底层账本口径：
    - 一行 = 一次「商品的一级分销价发生变化」；
    - old_value/new_value 存字符串（空 = 待定/清空），金额含税按盒；
    - rate = 当时使用的折扣率（批量折扣时记 归一化小数如 0.85；手工改价记空）；
    - list_price 存改价那一刻的标价快照（折扣率的分母，防止事后标价变动说不清）；
    - source：manual=表格手改/编辑弹窗，batch=批量折扣率，import=导入。
    """

    __tablename__ = "fx_price_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(Integer, index=True)
    sku: Mapped[str] = mapped_column(String(60), default="", index=True)
    old_value: Mapped[str] = mapped_column(String(40), default="")
    new_value: Mapped[str] = mapped_column(String(40), default="")
    rate: Mapped[str] = mapped_column(String(20), default="")
    list_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    source: Mapped[str] = mapped_column(String(20), default="manual")
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class FxPurchaseOrder(Base):
    """采购单（旺店通 purchase_order_query.php 原单）。

    为什么要单独落表
    ----------------
    1. 商品成本只能从采购单取（商品档案接口没有采购价），每次算价都调接口不可行；
    2. 采购单后续要跟商品、分销订单做关联（成本核算、批次追溯），
       必须先在系统里有一份可查询的原单，而不是一个「算完就扔」的中间结果。

    以 wdt_purchase_id（旺店通 purchase_id）为唯一键 upsert，幂等可重跑。
    """
    __tablename__ = "fx_purchase_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    wdt_purchase_id: Mapped[str] = mapped_column(String(40), default="", index=True)
    purchase_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    purchase_outer_no: Mapped[str] = mapped_column(String(60), default="")
    provider_no: Mapped[str] = mapped_column(String(60), default="", index=True)
    provider_name: Mapped[str] = mapped_column(String(160), default="")
    warehouse_no: Mapped[str] = mapped_column(String(60), default="", index=True)
    warehouse_name: Mapped[str] = mapped_column(String(160), default="")
    purchaser_name: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(16), default="", index=True)
    # 审核时间 = 价格生效时点；未审核单旺店通返回 0000-00-00，存空
    check_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    modified: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expect_arrive_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    goods_count: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    goods_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    tax_goods_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    post_fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    other_fee: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    remark: Mapped[str] = mapped_column(String(1000), default="")
    # 非正常采购（次品/残次/赠品…）：价格不能当成本用，聚合时排除
    abnormal: Mapped[int] = mapped_column(Integer, default=0, index=True)
    abnormal_reason: Mapped[str] = mapped_column(String(200), default="")
    raw_json: Mapped[str] = mapped_column(Text, default="{}")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    items: Mapped[list["FxPurchaseItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan"
    )


class FxPurchaseItem(Base):
    """采购单明细。

    价格字段口径（实测，别再猜）：
    - unit_price = 旺店通 details_list[].price（= tax_price）：**含税、按基本单位（盒/袋）、
      已是折后实付价**。格力高百奇蜜桃 55g 牌价含税 5.83/盒，8 折后 4.6641，采购单里就是 4.6641。
      这是唯一可信的成本口径，与 FxProduct.base_price（牌价）同单位可直接比。
    - purchase_price = 旺店通 details_list[].purchase_price：**历史不含税价，口径不稳定**。
      瑞士莲 600g：price=103 / purchase_price=92.42，比值 1.1145 既非 13% 也非固定值。
      保留原值仅供溯源，禁止当成本用。
    - last_price / lowest_price：上次采购价 / 历史最低价，仅供比价。
    """
    __tablename__ = "fx_purchase_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(
        ForeignKey("fx_purchase_orders.id", ondelete="CASCADE"), index=True
    )
    rec_id: Mapped[str] = mapped_column(String(40), default="")
    spec_no: Mapped[str] = mapped_column(String(80), default="", index=True)
    goods_no: Mapped[str] = mapped_column(String(80), default="", index=True)
    goods_name: Mapped[str] = mapped_column(String(255), default="")
    spec_name: Mapped[str] = mapped_column(String(160), default="")
    brand_name: Mapped[str] = mapped_column(String(100), default="")
    base_unit: Mapped[str] = mapped_column(String(20), default="")
    purchase_unit: Mapped[str] = mapped_column(String(40), default="")
    unit_ratio: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    # num = 基本单位数量；num2 = 采购单位（箱）数量
    num: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    num2: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    unit_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    tax_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    tax_rate: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    purchase_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    last_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    lowest_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    arrive_num: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    stockin_num: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    warehouse_id: Mapped[str] = mapped_column(String(40), default="")
    order: Mapped[FxPurchaseOrder] = relationship(back_populates="items")


class FxPurchasePrice(Base):
    """同一 SKU 在不同仓的采购价（按仓分组后的结果表）。

    为什么必须按仓分组
    ------------------
    同一个 SKU 会在多个仓被采购，价格并不相同，而且不是「先后涨价」：
    - 瑞士莲软心黑巧 600g 在「磐石-瑞士莲（京东专供)」与「聚小翻-磐石上海仓」
      同期并存 88 / 98 / 103 三个价，取「全表最新一条」会得到假的涨跌结论。
    - 珠海莲婚礼装 1.25kg 与悠享装 200g 在 CG202512090001/2 两张单里单价被互相
      录反（41 / 165），跨仓/跨单取最新价时极易把错价当趋势。

    所以：每个 (sku, warehouse_name) 各自取「该仓最近一次正常采购价」，
    再由 pick 规则选出主成本仓写回 fx_products.purchase_price。

    total_amount 用来判断哪个仓是这个 SKU 的主进货渠道（金额最大的通常就是主仓）。
    is_primary 标记当前被选中作为成本的仓；cost_warehouse 手工指定时不覆盖该标记，
    但 primary 会跟随手工指定变化。
    """
    __tablename__ = "fx_purchase_prices"
    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(60), index=True)
    brand: Mapped[str] = mapped_column(String(100), default="", index=True)
    warehouse_name: Mapped[str] = mapped_column(String(160), default="", index=True)
    provider_name: Mapped[str] = mapped_column(String(160), default="")
    # 该仓该 SKU 最近一次正常采购的含税单价（按基本单位）
    price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    purchase_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    purchase_no: Mapped[str] = mapped_column(String(40), default="")
    # 该仓累计采购金额 / 次数 / 价区间，用于选主仓与页面提示
    total_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    times: Mapped[int] = mapped_column(Integer, default=0)
    min_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    max_price: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    is_primary: Mapped[int] = mapped_column(Integer, default=0, index=True)
    # 排除标记：虚拟仓、仅记录仓（无实物）不参与选主仓
    excluded: Mapped[int] = mapped_column(Integer, default=0)
    exclude_reason: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FxStockSnapshot(Base):
    """库存快照（每日拍一张）：解决「昨天库存」基线问题。

    旺店通 stock_query.php 返回的是「当前库存」，不是历史。要算用户要的
    「昨天库存 + 今天入库 = 实际可用」，必须自己维护一份按日期的快照：
    - 每天同步一次 → 存为当天的 closing stock
    - 算满足率时，「昨天库存」= 最新一张 snap_date < 今天的快照
    - 单仓模式：warehouse 留空，所有聚合不区分仓库（用户明确只有 1 个仓库）
    """

    __tablename__ = "fx_stock_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    # spec_no = 旺店通规格码，与 fx_purchase_items.spec_no / fx_order_lines.spec_no 对齐
    spec_no: Mapped[str] = mapped_column(String(160), default="", index=True)
    # sku = 系统内商品编码（有匹配时回填，便于反查）
    sku: Mapped[str] = mapped_column(String(60), default="", index=True)
    warehouse: Mapped[str] = mapped_column(String(160), default="")
    # 快照代表的日期（当天 closing stock），格式 YYYY-MM-DD
    snap_date: Mapped[str] = mapped_column(String(10), default="", index=True)
    # 库存数量（按基本单位/盒）
    qty: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    source: Mapped[str] = mapped_column(String(20), default="wdt_stock_query")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        UniqueConstraint("spec_no", "warehouse", "snap_date", name="uq_fx_stock_snap"),
    )


class FxOrder(Base):
    __tablename__ = "fx_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    distributor_id: Mapped[int] = mapped_column(ForeignKey("fx_distributors.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    order_date: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    total_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    remark: Mapped[str] = mapped_column(String(1000), default="")
    # 这单是谁付的钱：一个订单只有一个付款公司（用户 2026-10-28 定）。
    # 分销商档案上的「付款账户」是「这个客户名下有哪些公司」的集合，两者不是一个概念。
    pay_company: Mapped[str] = mapped_column(String(160), default="")
    # 内部 FX 单号之外，旺店通 / 客户 Excel 里的原始订单号；一单可能拆成多个单号，逗号分隔。
    # 与 wdt_tid 分开：wdt_tid 是推送旺店通用的单号，历史团单里它就是原始出库单号；
    # 新单推送后 wdt_tid 可能等于内部单号，不能拿它冒充原始订单号。
    original_order_no: Mapped[str] = mapped_column(String(255), default="")
    # 来源：手工 / 旺店通 / 团单Excel；只做标识和排查，不参与金额计算
    source_channel: Mapped[str] = mapped_column(String(32), default="")
    # 下单页录的订单头信息（用户 2026-10-28 要求订单维度一次录全，别再靠备注口口相传）。
    # order_channel 是**业务渠道**（线下/团购/直播…），由下单人填；
    # 上面的 source_channel 是**系统来源**（手工/旺店通/团单Excel），由程序写，两者别混。
    order_channel: Mapped[str] = mapped_column(String(32), default="")
    ship_warehouse: Mapped[str] = mapped_column(String(80), default="")
    # 给仓库的额外要求：打包方式、随货资料、时效等，自由文本
    ship_note: Mapped[str] = mapped_column(String(500), default="")
    # 配送方式："" 未指定 / pickup 自提 / delivery 送货。自提不产生物流单号。
    delivery_type: Mapped[str] = mapped_column(String(16), default="")
    # 刮码：0 不刮（默认）/ 1 需要刮掉条码。客户防窜货、防追溯的常规要求。
    scratch_code: Mapped[int] = mapped_column(Integer, default=0)
    # 预留：未来推送旺店通时的原始单号 tid
    wdt_tid: Mapped[str] = mapped_column(String(120), default="")
    # 推送目标店铺：下单时选定（如 hhz-xxqd=红盒子-格力高-分销渠道）；空 = 用系统默认配置
    wdt_shop_no: Mapped[str] = mapped_column(String(40), default="")
    # 推送目标仓库编号（如 HHZ-Glico=红盒子-格力高磐石仓）；空 = 回退 .env WDT_WAREHOUSE_NO。
    # ship_warehouse 存中文名给人看，本字段才是旺店通 trade_push.php 的 warehouse_no。
    wdt_warehouse_no: Mapped[str] = mapped_column(String(40), default="")
    # 收款状态：unpaid 未收到 / partial 部分收款 / received 已收到 / refunded 已退款
    payment_status: Mapped[str] = mapped_column(String(16), default="unpaid", index=True)
    paid_amount: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    payment_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 发货状态：unshipped 未发货 / partial 部分发货 / shipped 已发货 / returned 已退回
    shipping_status: Mapped[str] = mapped_column(String(16), default="unshipped", index=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    logistics_company: Mapped[str] = mapped_column(String(100), default="")
    logistics_no: Mapped[str] = mapped_column(String(120), default="")
    # 客户签收时间（源表「签收时间」有就展示，没有留空）
    signed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class FxPaymentVoucher(Base):
    """客户打款凭证（截图 / 回单照片）。

    用户 2026-10-28 要求：客户付款后把打款凭证粘到系统里，自动识别金额和日期，
    落到「应收账款」里，每个订单都能挂自己的凭证。

    设计口径：
    - 图片原文件存磁盘（data/local/vouchers/YYYY-MM/），库里只存相对路径，
      避免把二进制塞进 SQLite 把库撑大；
    - `order_id` 允许为空：先收到钱、还没认领到具体订单时先挂到分销商名下，
      之后再认领（认领只改 order_id，不重新 OCR）；
    - OCR 只做「辅助填数」：识别结果存进来给人核对，绝不自动改订单金额，
      必须由人点「确认收款」才写 paid_amount（见 services/vouchers.py:apply_voucher）；
    - `applied` 标记这张凭证是否已计入某张订单的收款，防止同一张图重复入账。
    """

    __tablename__ = "fx_payment_vouchers"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("fx_orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    distributor_id: Mapped[int] = mapped_column(ForeignKey("fx_distributors.id"), index=True)
    # 原始文件名（只作展示），磁盘上的名字是 uuid，避免中文/重名冲突
    filename: Mapped[str] = mapped_column(String(255), default="")
    # 相对 VOUCHER_ROOT 的路径，如 2026-10/3f2a....jpg
    stored_path: Mapped[str] = mapped_column(String(255), default="")
    mime: Mapped[str] = mapped_column(String(80), default="")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    # OCR 状态：pending 待识别 / done 成功 / failed 失败 / unavailable 本机没有 OCR 引擎
    ocr_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    ocr_engine: Mapped[str] = mapped_column(String(32), default="")
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    # 识别出来的打款信息（人可改，改完再确认收款）
    amount: Mapped[Decimal | None] = mapped_column(MONEY, nullable=True)
    paid_at: Mapped[str] = mapped_column(String(20), default="")   # YYYY-MM-DD HH:MM
    payer: Mapped[str] = mapped_column(String(160), default="")     # 付款方 / 付款人
    pay_method: Mapped[str] = mapped_column(String(32), default="")  # 微信 / 支付宝 / 银行转账…
    note: Mapped[str] = mapped_column(String(255), default="")       # 摘要 / 备注
    # 0 = 只存凭证没入账，1 = 已计入订单收款
    applied: Mapped[int] = mapped_column(Integer, default=0, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
