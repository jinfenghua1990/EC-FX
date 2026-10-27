# 架构 V0.1

```text
旺店通 API ─────┐
旺店通导出 XLSX ├─> stockout_orders / stockout_items ─┐
                │                                      │
平台结算 XLSX ──┴─> settlement_rows ─> recon_results ─┼─> 月度利润
银行流水 XLSX ─────> bank_rows ────────────────────────┘  （下一阶段做二级核销）
```

## 主键策略

- 销售出库：`stockout_no` 唯一
- 平台订单匹配：优先 `src_trade_no / src_tids / trade_no`
- SKU：`spec_no` 主识别，`barcode` 辅助
- 文件：SHA256 幂等，重复导入不重复写入
