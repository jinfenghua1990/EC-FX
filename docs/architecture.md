# 红盒子供销平台 · 架构

> 最后更新：2026-10-27（核销系统下线后）

## 数据流

```text
                        ┌──────────────────────────────────────────┐
旺店通 openapi2 ────────┤ goods_query.php  → 商品档案 fx_products   │
   （签名鉴权）          │ trade_push.php   → 分销订单推送           │
                        └──────────────────────────────────────────┘

旺店通销售出库 ──> stockout_orders / stockout_items ──> 经营总览
   （API 或 XLSX 导入）              │                    发货明细
                                     │
                      manual_adjustments（手工调整，只改生效值）
                                     │
                                     └──> 生效口径 = RAW + 调整
```

分销业务独立成链：

```text
fx_distributors（分销商）
      │
      ├── fx_prices（经销商专属价：有覆盖价用覆盖价，否则用 fx_products.base_price）
      │
      └── fx_orders / fx_order_lines（订单与明细，下单时快照商品资料）
                     │
                     └──> 推送到旺店通 trade_push.php（记录 push_status）
```

## 主键策略

- 销售出库：`stockout_no` 唯一（旺店通出库单号）
- 分销订单：`order_no` 唯一，格式 `FXyyyymmdd-0001`
- 商品：`fx_products.sku` 唯一，等于旺店通规格编码 `spec_no`
- 平台订单匹配：优先 `src_trade_no / src_tids / trade_no`
- SKU 识别：`spec_no` 主识别，`barcode` 辅助

## 数据真实性约束

- 旺店通 RAW 数据一律不覆盖：手工调整写入 `manual_adjustments`，只影响「生效值」
- 旺店通凭证缺失时接口直接报错，不回退到假数据
- 未实现的页面统一进 `/coming-soon` 占位页，明确标注「待开发」，**不展示任何虚构数字**

## 已下线（2026-10-27）

核销系统整体移除。相关表 `settlement_rows`、`bank_rows`、`recon_results`、`rebate_rules`、
`legacy_*`（6 张）、`module_*`（3 张）、`import_batches` 共 15 张已由迁移
`0009_drop_reconciliation_tables` 删除。历史算法文档见 `docs/legacy/`，仅作记录。
