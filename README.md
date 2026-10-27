# AI Banking Agent —— 全自动银行助手（bank_core）

> 2026 深圳国际金融科技大赛 · AI Banking Agent 赛道 ｜ 研发计划见 [docs/AI-Banking-Agent-产品研发计划.md](docs/AI-Banking-Agent-产品研发计划.md)

`bank_core` 是整套系统的**银行能力层**：一间随时可重置的沙箱银行，含账本、卡片、
理财、订阅、用户事件与审计日志，并以标准 **MCP Server** 对外暴露 48 个工具，
供 LangGraph 编排层（或任何 MCP 客户端）调用。

## 快速开始

```bash
# —— macOS —— 装依赖(一次性)
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd webui && npm install && cd ..      # 或 pnpm install

# 一键路线(Windows/macOS 同一口径):首次运行会自动生成 webui/.env.local
# (全程只差粘贴一个 LLM API Key,AUTH_SECRET 本机随机)并自动播种演示库;
# 之后每次运行都是全量重启当前代码。
# (Windows: 启动演示-Windows.bat 需在 cmd/资源管理器里运行,Git Bash 下用 cmd //c 启动演示-Windows.bat)
启动演示-Windows.bat                # Windows
./启动演示-Mac.command          # macOS(双击亦可;首次使用需 chmod +x,见下)
# 启动后只保留一个进度窗口,四个服务在后台无窗口运行;日志在 logs/ 下。
# 关掉进度窗口/终端都不影响服务:
#   Windows 停止: 停止演示-Windows.bat        macOS 停止: ./停止演示-Mac.command

# —— Windows —— 装依赖(一次性)
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
cd webui && pnpm install --registry=https://registry.npmmirror.com && cd ..

# —— 或手动分步(路径以 Windows 为例,macOS 把 .venv/Scripts/* 换成 .venv/bin/*) ——
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

> macOS 首次使用 `.command` 脚本：若从压缩包/git archive 解出来的文件没有执行权限，
> 在终端里执行一次 `chmod +x 启动演示-Mac.command 停止演示-Mac.command` 即可；之后双击就能启动/停止。

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

## 对话前端（webui，:3000）

- 自然语言覆盖六大场景：转账/AA、账单、理财、卡片、订阅、生日联动；回复流式输出，
  金额/卡片/闸门由 `data-*` 部件渲染。
- 账号体系：实名注册、登录密码 + 支付密码、会话 7 天；动钱统一走建单→确认→支付密码。
- **Agent 记忆（设置 → Agent 记忆）**：九档记忆一屏检阅——用户画像/关于你/偏好忌讳/
  想做的事/账单/理财/生日经验/对话时间线/计划历史。每轮对话经路由器**确定性提炼**
  （零 LLM 调用）落库：城市/职业/年龄/收入/家庭等自述事实，喜欢/讨厌/风险态度，
  目标意向，以及带意图标签的每轮一句话时间线；账单/理财分析复盘时附 Top 分类、
  持仓明细供弹窗画图。弹窗内含记忆总览、消费结构条形图、持仓分布环图、计划成败
  环图、偏好标签墙、任务进度条（纯 SVG，零图表库）；每档可单独"忘记"、可一键清空。
- **缺槽反问选择题**：联动/生日规划补槽时，预算/日期以可点选项呈现（预留档位/
  明天后天这周六），动作（物料清单）千人千面、只做自由输入;每卡带"其他"自由
  输入兜底。消息出现"计划"即进联动意图（三道硬闸：资金意图不被劫持、长程任务
  优先、到期分流不变）。
- 手机端：375px 适配、会话总览弹窗（搜索/命名新建）、扫码免密登录。
- 跨设备同步：同一账号的手机和电脑自动跟随当前会话，消息、待答问题和转账确认卡
  约 1.5 秒内双向同步。
- 大模型管理：设置里可接入 DeepSeek/OpenAI/Claude/自定义端点（Key + Base URL），
  连接状态存本机浏览器，重新登录无需重填。
- 会话目录：按用户隔离、全文搜索（标题/消息正文）、滚动分页；新建会话强制先命名（≤10 字）。

## 架构定位

```
LangGraph 编排层（agent/，多节点 / 10 路由意图 / 9 类人工闸门）
        │ MCP (stdio / http)
        ▼
┌──────────────────────────────────────────────┐
│ mcp_server.py   48 个工具，标注 READ/LOW/MED/HIGH │
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
| 生日联动 | "林悦的生日"（播种日 + 4 天，每年重复） | 预留资金 + 提前 2 天礼物提醒 |
| 到期转账 | 3 天后 500 元家用定时转账 | 到期提醒 → 用户确认执行 |

## 测试

```
.venv/Scripts/python.exe -m pytest tests -q      # 609 个用例全绿(2026-10-27 实测)
.venv/Scripts/python.exe -m pytest tests --collect-only -q   # 只标定用例数,秒出

# 用例数不写固定值,一律以 --collect-only 实测为准(历史文档里的 205/245/133/413/481 均已过期)
# 全量推荐加 --basetemp=.pytest_tmp/<批次号>(批次号递增、目录全新),
# 否则 pytest 清理旧临时库触发 safe-delete 守卫 → 用例全过却 rc=1 的假失败。

tests/test_bank_core.py     26 个 —— 金额换算、转账两步走/余额不足/幂等/日限额、
                            AA 对账、挂失不可逆、理财风险闸门(含无测评限购 R1)、
                            申购幂等键、种子确定性、余额=入-出不变量、订阅/异常戏眼、
                            月报勾稽、事件联动
tests/test_agent_graph.py   58 个 —— 全图端到端(闸门/澄清/消歧/联动/账单/理财/卡片/订阅、
                            "计划"关键词路由三道硬闸、联动动作容错规整、缺槽选择题)
tests/test_api_stream.py    29 个 —— SSE 协议帧、确认卡片、会话目录/搜索/同步、悬空单落地页
tests/test_auth.py          31 个 —— 实名注册、密码策略、会话与支付密码核验
tests/test_admin.py         15 个 —— 管理台资金/变更记录、调账、按用户隔离
tests/test_review_fixes.py   8 个 —— 严格确认语义、犹豫答复绝不执行、幂等重放
tests/test_linkage.py        9 个 —— 跨场景联动建/到期/取消与时间旅行
tests/test_e2e_transfer.py   5 个 —— 端到端 A~E(立即/同名/超限/定时/AA)
tests/test_llm_autoselect.py 8 个 —— 模型自动选型、连接参数与 Claude 适配
tests/test_contract.py      30 个 —— 跨层接口契约守卫(闸门→SSE 部件→前端渲染、
                            线协议词表、播报白名单、**部件载荷字段级冻结**——前端
                            interface 与后端键集漂移直接红灯),多人并行的"合同测试"
tests/test_precheck.py       6 个 —— 代扣预审(事前知情):滚动数学/涨价对照/
                            ack 抑制/跳过顺延/老库自愈补列
tests/test_env_template.py  13 个 —— "clone 后只填一个 Key"契约守卫:.gitignore
                            通配拦截所有 .env 变体(git check-ignore 实测)、
                            模板键齐全且默认值可用、setup_env.py 随机密钥/防覆盖
tests/test_demo_launcher.py  7 个 —— 一键启动/停止、端口 PID、跳过构建
tests/test_report_followup.py 5 个 —— 播报后的业务追问与收尾
tests/test_analysis_plan.py  33 个 —— 只读分析技能:账单/理财事实行、卡片视图、
                            风险过滤(候选风险等级必须 ≤ 用户等级)、空数据不装成功
tests/test_core.py          30 个 —— Agent Core 契约:Plan/Guard 校验、拓扑执行序、
                            Observe-Reflect、重规划只改受影响步骤(纯函数,不建库不调 LLM)
tests/test_plan.py          28 个 —— agent.plan 纯函数:事件匹配/订阅候选/可支配/
                            计划分支/预算编辑
tests/test_memory.py        38 个 —— 长期记忆九模块 UPSERT、条目型追加(key 去重/截顶)、
                            用户隔离、失败软处理、未登录不写、计划历史队列限 5 条
tests/test_memory_capture.py 26 个 —— 对话提炼器(2026-10-26):偏好/事实/目标/反目标
                            正则提取与误报闸、每轮落库编排、时间线、渲染与检阅
tests/test_persona.py       11 个 —— 人设提示词存取、默认态、越界防御、温度透传
tests/test_tool_meta.py     16 个 —— 44 工具身份证对账(多/少/风险等级漂移都红)、
                            动钱面/两步类/不可逆类/敏感类清单、scoped 白名单视图
tests/test_votes.py          8 个 —— 消息投票:投/换/取消往返、GET 恢复、管理台汇总
tests/test_task_store.py     7 个 —— 长程任务存储层(纯 sqlite)
tests/test_longtask.py       5 个 —— 长程任务图级闭环:建档 → 到期唤醒 → 复盘留痕
tests/test_token_usage.py   14 个 —— Token 估算纯函数(CJK 1 字符≈1 token / 其余≈0.25):
                            异形输入、百万字符秒级返回、6000 阈值边界稳定
tests/test_guarded_call.py  21 个 —— 工具调用咽喉:记账字段与旧 _jr 逐字段等价、
                            args 快照隔离("我怎么把它弄坏"探测抓到的真 bug 回归)
tests/test_history_compact.py 25 个 —— 历史三层压缩:L1 占位清理(4 条/500 字符)、
                            L2 阈值摘要(6000 token/熔断 3 次)、L3 不做
tests/test_prompt_channels.py 8 个 —— 通道分离:System Prompt 严格等于静态常量、
                            动态上下文(persona/记忆/事实)一律走 HumanMessage
```
