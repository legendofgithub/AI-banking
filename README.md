# AI Banking Agent —— 练功假银行（bank_core）

> 2026 深圳国际金融科技大赛 · AI Banking Agent 赛道 ｜ 研发计划见 [docs/AI-Banking-Agent-产品研发计划.md](docs/AI-Banking-Agent-产品研发计划.md)

`bank_core` 是整套系统的**银行能力层**：一间随时可重置的沙箱银行，含账本、卡片、
理财、订阅、用户事件与审计日志，并以标准 **MCP Server** 对外暴露 42 个工具，
供 LangGraph 编排层（或任何 MCP 客户端）调用。

## 快速开始

```bash
# Windows (Git Bash)
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt

# 生成 12 个月种子数据（确定性，可反复重置）
.venv/Scripts/python -m bank_core.seed

# 启动 MCP 服务
.venv/Scripts/python -m bank_core.mcp_server             # stdio 模式
.venv/Scripts/python -m bank_core.mcp_server --http 8765 # HTTP 模式（联调用）

# 启动可视化试用界面（演示网银）
.venv/Scripts/python -m bank_core.web_api   # 打开 http://127.0.0.1:8788

# 跑测试
.venv/Scripts/python -m pytest tests -q
```

## 试用界面（demo/index.html）

`web_api` 提供的"演示网银"，8 个标签页把六大场景全部点得到：

| 标签页 | 能试什么 |
|--------|---------|
| 总览 | 账户余额、最近交易（余额链与账单时间序一致） |
| 智能转账 | 建单 → 金色确认卡 → 确认/取消；支持定时转账；超限会被风控拦截 |
| 账单分析 | 月度收支/储蓄率/环比/分类条形图（已排除内部划转） |
| 订阅管家 | 周期扣费挖掘、年化成本、腾讯视频涨价提示、一键取消代扣 |
| 异常检测 | 重复扣款/大额离群/凌晨大额三条规则的证据 |
| 理财 | C3 风险等级过滤、超标产品禁用、申购/赎回两步确认 |
| 卡片管理 | 锁定/解锁/挂失（挂失不可逆） |
| 日历联动 | 生日等事件、联动计划草稿、模拟到期任务触发 |

> 注意：重播种前先停掉 web_api（Windows 下数据库文件被占用）。

数据库默认落在 `data/bank.db`，可用环境变量 `BANK_CORE_DB` 改路径。

## 架构定位

```
LangGraph 编排层（agent/，已在跑：70 节点 / 10 路由意图 / 9 类人工闸门）
        │ MCP (stdio / http)
        ▼
┌──────────────────────────────────────────────┐
│ mcp_server.py   42 个工具，标注 READ/LOW/MED/HIGH │
├──────────────────────────────────────────────┤
│ ledger.py 账本·转账两步走·AA·卡片                │
│ analysis.py 分类统计·月报·异常检测·订阅挖掘       │
│ wealth.py 产品库·风险闸门·申赎两步走             │
│ events.py 用户事件·提醒任务·联动建议草稿         │
├──────────────────────────────────────────────┤
│ db.py SQLite（金额一律整数"分"） + audit 全量留痕 │
└──────────────────────────────────────────────┘
```

## 资金安全铁律（代码已强制）

1. **动钱两步走**：`create_transfer_order` 只建单（`pending_confirm`），
   `confirm_transfer_order` 才扣款；确认时二次校验余额、单笔/日累计限额
2. **幂等防重放**：转账 `idempotency_key` 重复建单直接返回已有订单；
   理财申购同款（编排层生成键、`transactions.external_ref` UNIQUE 兜底），
   同指令重放拒绝二次扣款
3. **理财风险闸门**：未做风险测评只能申购 R1（现金管理类），R2 及以上先测评；
   有测评则超过 C 等级直接拒绝；申赎同样两步走
4. **只建议不越权**：联动计划（`suggest_linkage`）只产出待确认步骤草稿；
   定时转账到期只转"待确认"，绝不自动扣款
5. **全程审计**：每次工具调用入参/结果写 `audit_log`（写操作无遗漏；支付密码脱敏为 `***`）
6. **支付密码防爆破**：待确认转账页连续输错 4 次支付密码即锁定该账户转账功能
   （`users.transfer_locked`），建单/确认在 `ledger` 层统一卡点（对话建单、页面确认、
   联动执行全线生效），锁后正确密码也拒；输对一次即清零错误计数；解锁只有管理员通道
   `POST /api/transfer-unlock`（audit `HIGH` + change_log 双留痕）

## 种子数据里埋好的演示"戏眼"

| 戏眼 | 位置 | 演示话术 |
|------|------|---------|
| 订阅涨价 | 腾讯视频VIP 25→30 元（近3个月） | "检测到您的视频会员悄悄涨价了" |
| 重复扣款 | 迅雷白金会员 15 元 ×3（3周前，3天内） | 异常检测 R1 |
| 凌晨大额 | 京东 4,999 元 @03:47（5周前） | 异常检测 R3 |
| 周期支出 | 房租 6,800/月、话费 128/月 等 5+ 项订阅 | 订阅年化成本盘点 |
| 生日联动 | "林悦的生日"（19 天后，每年重复） | 预留资金 + 提前 2 天礼物提醒 |
| 到期转账 | 3 天后 500 元家用定时转账 | 到期提醒 → 用户确认执行 |

## 测试

```
.venv/Scripts/python.exe -m pytest tests -q      # 133 个用例全绿(2026-10-09)

tests/test_bank_core.py     23 个 —— 金额换算、转账两步走/余额不足/幂等/日限额、
                            AA 对账、挂失不可逆、理财风险闸门(含无测评限购 R1)、
                            申购幂等键、种子确定性、余额=入-出不变量、订阅/异常戏眼、
                            月报勾稽、事件联动
tests/test_agent_graph.py   26 个 —— 全图端到端(闸门/澄清/消歧/联动/账单/理财/卡片/订阅)
tests/test_api_stream.py    14 个 —— SSE 协议帧、确认卡片、会话目录、悬空单落地页
tests/test_auth.py          28 个 —— 实名注册、密码策略、会话与支付密码核验
tests/test_admin.py         10 个 —— 管理台资金/变更记录、调账、按用户隔离
tests/test_review_fixes.py   8 个 —— 严格确认语义、犹豫答复绝不执行、幂等重放
tests/test_linkage.py        9 个 —— 跨场景联动建/到期/取消与时间旅行
tests/test_e2e_transfer.py   5 个 —— 端到端 A~E(立即/同名/超限/定时/AA)
tests/test_llm_autoselect.py 5 个 —— 模型自动选型与缓存
tests/test_contract.py       5 个 —— 跨层接口契约守卫(闸门→SSE 部件→前端渲染、
                            线协议词表、播报白名单),多人并行开发的"合同测试"
```
