# 登录注册 + 支付密码体系 实施方案

## 决策（已确认）
- **观光模式**：未登录可闲聊/看介绍；说到具体业务（转账/理财/卡片/联动等）时提示"请先登录"。陈明预置登录凭证：登录密码 `Demo@12345`、支付密码 `888888`。
- **支付密码范围 = 动钱 + 敏感操作**：转账执行、理财申购/赎回、联动预算锁定与购买、**卡片挂失与限额调整**需输支付密码；其余（AA、联系人、查询、账单分析）登录即可用。

## 一、后端（8800 + bank_core，我来做）

**1. `agent/auth.py` 新建**（8800 侧轻量账号体系，不复活模板的 Auth.js）：
- bank.db 加两张表：`auth_users`(user_id 主键→users, identifier 唯一, identifier_type phone|email, password_hash, pay_hash, created_at, last_login_at)、`sessions`(token, user_id, expires_at 7天)
- 密码策略服务端强校验：登录密码≥8位且含大写/小写/数字/符号；支付密码 6 位纯数字；identifier 手机号(1开头11位)或邮箱正则
- 哈希用标准库 PBKDF2-SHA256+随机盐（零新依赖）

**2. 端点（agent/api.py）**：POST /api/auth/register（昵称+identifier+登录密码+支付密码 → 建 users 行 + **0 元活期账户** + auth 行，管理后台立即可见）、POST /api/auth/login（发 token）、POST /api/auth/logout、GET /api/auth/me。注册手机号同步写 users.phone（邮箱注册则 users.phone 留空，管理台从 auth_users 读）。

**3. 多用户贯通**：
- mcp_server 加 `BANK_USER_ID` 环境变量（默认1），四个服务按它绑定用户；新增 MCP 工具 `verify_pay_password`（校验当前用户支付密码，**审计入参脱敏为"***"**，工具数 41→42，同步 EXPECTED_TOOL_COUNT 与测试断言）
- agent/bank.py `load_bank_tools(db, user_id)` 把 BANK_USER_ID 注入 stdio env
- agent/api.py：ChatRequest 加 `token` → 解析 user_id；**graphs/tools 按用户缓存**（dict，图对象轻量）；threads 表加 user_id 列（增量 ALTER，老行默认1），/api/threads 按登录用户过滤，history 校验归属
- 陈明补 auth 行（Demo@12345 / 888888），写进演示指南

**4. 编排层（graph.py）**：
- state 加 `auth_user_id`（api 注入）；各业务管线入口检查未登录 → 新 notice `login_required`（"请先登录后再办理业务"，附登录按钮高亮提示）
- `_make_gate_node` 加 `pay_required` 参数：question 变"…请输入 6 位支付密码确认"；应答解析=取消词→取消，否则调 verify_pay_password → 对→执行 / 错→"支付密码错误"重问（沿用两问封顶，超限按取消，绝不误动钱）
- 应用于：t_gate、理财申购/赎回闸、联动锁定闸、联动购买闸、卡片挂失+限额闸
- SSE 载荷加 `pay_required: true` 字段（前端渲染密码框）

**5. 测试**：tests/test_auth.py（密码策略拒绝清单/注册登录会话/token 过期/支付密码验证工具/观光 login_required/错密码重问与封顶）+ 全量回归。

## 二、前端（webui，派 1 个代理做，我验收）

- **chat-header.tsx**："Deploy with Vercel"广告按钮和移动端 Vercel logo 换成**登录按钮**；已登录显示昵称+登出菜单
- **重写 login/register 页**（指向 8800）：注册=昵称+手机号或邮箱+登录密码×2+支付密码×2（实时提示规则）；登录=账号+密码；token 存 localStorage `bank-token`
- **use-active-chat**：/api/chat、history、threads 请求带 token
- **bank-parts.tsx**：pay_required 的确认卡加 6 位密码输入框（点确认=发送密码），取消按钮照旧
- 管理台（admin index.html）：个人台总览加"**注册信息**"卡（注册方式/账号/注册时间/最近登录）；用户列表手机号与账号按 **JR/T 0171 脱敏显示**（139****0000 / a***@x.com）——顺手完成此前专业项①

## 三、验收剧本（浏览器实测）
注册新用户 → 管理台用户列表立即可见（0 元账户+脱敏账号）→ 管理员调账充值 → 前端登录该用户 → 转账（输支付密码）成功 / 输错一次重问 / 两次错自动取消 → 管理台流水对账 → 陈明登录凭证走全流程。未登录说"转账" → 提示先登录。

## 边界与不做
- 不做找回密码/短信验证码/图形验证码（比赛范围外，记差距清单话术）
- 老的 7 位演示用户不补登录凭证（纯展示数据）；管理台看完整手机号的"权限查看"不做，脱敏为默认
- 全量回归必须绿；8800/8789 重启后生效