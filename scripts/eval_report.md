# Agent 真实 LLM 评估报告

- 时间:2026-10-09T09:08:04  模型:`deepseek-chat`  剧本集:`full`
- 阈值:success_rate >= 0.80
- 临时库根目录(失败可对账):`C:\Users\asus\AppData\Local\Temp\eval-agent-m5mxiead`

## 汇总

| 指标 | 值 |
|---|---|
| 通过 / 总数 | 22 / 25 |
| 成功率 | 88.00% |
| 平均轮次(用户消息/剧本) | 2.36 |
| 平均工具调用 | 3.84 |
| 平均耗时 | 10.2s |
| 总耗时 | 255.4s |

## 按场景分组

### 转账(8 个:通过 6,成功率 75%,平均轮次 3.0,平均工具 5.75,平均 14.1s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| transfer_simple | PASS | 2 | 6 | 14.1 |  |
| transfer_cancel | PASS | 2 | 5 | 11.7 |  |
| transfer_overlimit | PASS | 1 | 3 | 8.8 |  |
| transfer_clarify | PASS | 4 | 6 | 15.3 |  |
| gate_hedge | PASS | 3 | 7 | 15.4 |  |
| transfer_duplicate | FAIL | 3 | 6 | 15.1 | notice.kind 应为 duplicate_order,实际 None(notice={}) |
| transfer_disambiguate | FAIL | 7 | 8 | 21.0 | 两个同名王芳应触发选人反问(pick_contact) |
| scheduled_transfer | PASS | 2 | 5 | 11.2 |  |

### AA收款(2 个:通过 1,成功率 50%,平均轮次 2.5,平均工具 1.0,平均 6.3s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| aa_create | PASS | 2 | 1 | 5.7 |  |
| settle_flow | FAIL | 3 | 1 | 6.9 | notice.kind 应为 split_progress,实际 None(notice={}) |

### 联系人(1 个:通过 1,成功率 100%,平均轮次 3.0,平均工具 1.0,平均 5.8s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| contact_add | PASS | 3 | 1 | 5.8 |  |

### 闲聊(1 个:通过 1,成功率 100%,平均轮次 1.0,平均工具 0.0,平均 4.7s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| chat_fallback | PASS | 1 | 0 | 4.7 |  |

### 联动(3 个:通过 3,成功率 100%,平均轮次 3.0,平均工具 6.0,平均 13.2s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| linkage_create | PASS | 2 | 5 | 11.0 |  |
| linkage_due | PASS | 5 | 10 | 19.9 |  |
| linkage_cancel | PASS | 2 | 3 | 8.8 |  |

### 账单(2 个:通过 2,成功率 100%,平均轮次 1.0,平均工具 1.0,平均 5.5s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| bill_monthly | PASS | 1 | 1 | 5.5 |  |
| bill_anomaly | PASS | 1 | 1 | 5.5 |  |

### 理财(3 个:通过 3,成功率 100%,平均轮次 2.33,平均工具 4.67,平均 10.9s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| wealth_recommend | PASS | 1 | 2 | 6.4 |  |
| wealth_subscribe | PASS | 2 | 5 | 10.6 |  |
| wealth_redeem_cancel | PASS | 4 | 7 | 15.8 |  |

### 卡片(3 个:通过 3,成功率 100%,平均轮次 1.67,平均工具 3.0,平均 7.7s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| card_list | PASS | 1 | 1 | 5.2 |  |
| card_limit | PASS | 2 | 4 | 9.2 |  |
| card_lock | PASS | 2 | 4 | 8.7 |  |

### 订阅(2 个:通过 2,成功率 100%,平均轮次 1.5,平均工具 2.0,平均 6.5s)

| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |
|---|---|---|---|---|---|
| subscription_list | PASS | 1 | 1 | 5.3 |  |
| subscription_cancel | PASS | 2 | 3 | 7.8 |  |

## 失败归因

| 归因 | 次数 |
|---|---|
| 业务断言不符(DB/notice 终态) | 3 |

逐条明细:

- **transfer_duplicate**[业务断言不符(DB/notice 终态)]:notice.kind 应为 duplicate_order,实际 None(notice={})
- **transfer_disambiguate**[业务断言不符(DB/notice 终态)]:两个同名王芳应触发选人反问(pick_contact)
- **settle_flow**[业务断言不符(DB/notice 终态)]:notice.kind 应为 split_progress,实际 None(notice={})

**EVAL RESULT: pass=22/25 success_rate=0.88**
