# Agent 真实 LLM 评估报告

- 时间:2026-09-27T17:42:07  模型:`glm-5.3-flash`  剧本集:`full`
- 阈值:success_rate >= 0.80
- 临时库根目录(失败可对账):`C:\Users\asus\AppData\Local\Temp\eval-agent-u_mdarxe`

## 汇总

| 指标 | 值 |
|---|---|
| 通过 / 总数 | 19 / 23 |
| 成功率 | 82.61% |
| 平均轮次(用户消息/剧本) | 2.35 |
| 平均工具调用 | 3.3 |
| 平均耗时 | 23.6s |
| 总耗时 | 542.7s |

## 按场景分组

### 转账(8 个:通过 6,成功率 75%,平均轮次 2.88,平均工具 4.75,平均 29.3s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| transfer_simple | PASS | 2 | 5 | 16.9 |  |
| transfer_cancel | PASS | 2 | 5 | 14.7 |  |
| transfer_overlimit | PASS | 1 | 3 | 19.8 |  |
| transfer_clarify | PASS | 4 | 5 | 20.2 |  |
| gate_hedge | PASS | 3 | 5 | 15.5 |  |
| transfer_duplicate | FAIL | 3 | 5 | 25.3 | notice.kind 应为 duplicate_order,实际 None(notice={}) |
| transfer_disambiguate | FAIL | 6 | 6 | 70.0 | 两个同名王芳应触发选人反问(pick_contact) |
| scheduled_transfer | PASS | 2 | 4 | 52.1 |  |

### AA收款(2 个:通过 1,成功率 50%,平均轮次 2.5,平均工具 1.0,平均 20.6s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| aa_create | PASS | 2 | 1 | 11.4 |  |
| settle_flow | FAIL | 3 | 1 | 29.8 | notice.kind 应为 split_progress,实际 None(notice={}) |

### 联系人(1 个:通过 1,成功率 100%,平均轮次 3.0,平均工具 1.0,平均 11.3s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| contact_add | PASS | 3 | 1 | 11.3 |  |

### 闲聊(1 个:通过 1,成功率 100%,平均轮次 1.0,平均工具 0.0,平均 6.2s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| chat_fallback | PASS | 1 | 0 | 6.2 |  |

### 联动(3 个:通过 3,成功率 100%,平均轮次 3.0,平均工具 5.33,平均 39.1s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| linkage_create | PASS | 2 | 5 | 39.9 |  |
| linkage_due | PASS | 5 | 8 | 36.2 |  |
| linkage_cancel | PASS | 2 | 3 | 41.2 |  |

### 账单(2 个:通过 2,成功率 100%,平均轮次 1.0,平均工具 1.0,平均 11.9s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| bill_monthly | PASS | 1 | 1 | 9.8 |  |
| bill_anomaly | PASS | 1 | 1 | 14.1 |  |

### 理财(3 个:通过 2,成功率 67%,平均轮次 2.0,平均工具 3.33,平均 24.1s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| wealth_recommend | PASS | 1 | 2 | 17.8 |  |
| wealth_subscribe | PASS | 2 | 4 | 16.4 |  |
| wealth_redeem_cancel | FAIL | 3 | 4 | 38.0 | notice.kind 应为 wealth_cancelled,实际 None(notice={}) |

### 卡片(3 个:通过 3,成功率 100%,平均轮次 1.67,平均工具 2.33,平均 12.0s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| card_list | PASS | 1 | 1 | 9.3 |  |
| card_limit | PASS | 2 | 3 | 13.1 |  |
| card_lock | PASS | 2 | 3 | 13.7 |  |

## 失败归因

| 归因 | 次数 |
|---|---|
| 业务断言不符(DB/notice 终态) | 4 |

逐条明细:

- **transfer_duplicate**[业务断言不符(DB/notice 终态)]:notice.kind 应为 duplicate_order,实际 None(notice={})
- **transfer_disambiguate**[业务断言不符(DB/notice 终态)]:两个同名王芳应触发选人反问(pick_contact)
- **settle_flow**[业务断言不符(DB/notice 终态)]:notice.kind 应为 split_progress,实际 None(notice={})
- **wealth_redeem_cancel**[业务断言不符(DB/notice 终态)]:notice.kind 应为 wealth_cancelled,实际 None(notice={})

**EVAL RESULT: pass=19/23 success_rate=0.83**
