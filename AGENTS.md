# AGENTS.md —— AI Banking 项目说明书

> 任何 AI 会话开始前先读这份。项目背景与路线决策见
> [docs/AI-Banking-Agent-产品研发计划.md](docs/AI-Banking-Agent-产品研发计划.md),
> 生产化差距与答辩证据见 [docs/production-readiness.md](docs/production-readiness.md)。
> 2026 深圳国际金融科技大赛 · AI Banking Agent 赛道:对话式银行助手,六大场景
> (转账/账单/订阅/理财/卡片/跨场景联动),演示主用户陈明。

## 开发铁律(每次改动必须遵守)

1. **改完就 commit**:每完成一个功能/修复,生成一个 Git commit(本仓库 2026-10-01 才建立,
   别再回到"零快照裸奔"状态)。commit 前确认 `git status` 里没有 `.env.local`、`*.sqlite`。
2. **改完必须过测试**:改动要配套新增/更新测试,并跑全量
   `.venv/Scripts/python.exe -m pytest tests -q`(当前 111 个,全绿才能交付)。
3. **Python 一律用 venv**:`.venv/Scripts/python.exe`。裸 pip 指向不存在的 Python 3.14(损坏);
   系统 Python312 没装项目依赖(服务用它能起来是历史环境假象,重启必失败)。
4. **动钱链路改动手动验证**:涉及转账/支付密码/注册登录的改动,除了 pytest,必须在浏览器里
   实际走一遍(webui 页面或 `cd webui && node e2e-check.mjs`)。单元测试拦不住前端表单问题。
5. **资金安全四条不许破坏**(bank_core 现有设计,详见 README"资金安全铁律"):
   动钱两步走(建单→确认才扣款)、幂等防重放、理财风险闸门(超 C 等级拒购)、全量审计日志。

## 服务拓扑(四服务,常驻)

| 端口 | 服务 | 启动(在项目根目录) |
|------|------|----------------------|
| 3000 | webui 聊天前端(Next.js) | 已在跑;改前端代码实测无需重启 |
| 8800 | agent API(LangGraph 编排+账号) | `.venv/Scripts/python.exe -m agent.api` |
| 8788 | 演示网银(web_api) | `.venv/Scripts/python.exe -m bank_core.web_api` |
| 8789 | 管理台 | `.venv/Scripts/python.exe -m bank_core.admin_api` |

- 重启后端**必须用 venv python**(见铁律 3)。
- 清理进程**先 netstat 查 PID 再按 PID 杀**,绝不 `taskkill /IM node.exe` 全杀(会误杀用户常驻的 webui)。
- 内存紧张(16GB,常只剩 2-4GB):webui 用 `next build` + `next start`,**不要用 `next dev --turbo`**(必 OOM)。

## 目录地图

```
bank_core/    银行能力层:42 个 MCP 工具(账本/分析/理财/事件/联动)+ 管理台/网银 API
agent/        LangGraph 编排层:8800 API、auth 账号体系、模型选择
webui/        聊天前端(vercel/chatbot 模板魔改:账号/支付密码闸/银行卡片)
admin_console/ 管理台前端(单页 HTML,8789)
demo/         演示网银前端(8788)
tests/        pytest 全量回归
scripts/      冒烟/评测脚本
data/         运行库 bank.db 等(不入库,可重播种)
```

## 账号体系口径(2026-10-01 起)

注册=实名:真实姓名 + 18 位身份证(GB 11643 校验码,一张证件一个账户)+ 手机号必填
(即登录账号)+ 邮箱选填。users 表有 id_card/email 列(老库自动迁移)。演示账号:
陈明 13800002233 / 密码 Demo@12345 / 支付密码 888888。

## 环境坑速查

- 裸 pip 坏;GitHub 直连已通(2026-10-01 修:删过无效代理键 `https.proxy`;若日后又断,
  先看 Clash/FlClash 是否开着、`http.proxy` 是否指向活端口,再考虑镜像)。
- `pnpm install` 必须加 `--registry=https://registry.npmmirror.com`。
- 重播种(重置演示数据)前先停占用 bank.db 的服务(Windows 下库文件被占用)。
- ZCode 内嵌浏览器(IAB)点按钮常超时:验证用 `elementFromPoint` 确认可点后,
  `form.requestSubmit()` / `el.click()`(evaluate)兜底,效果等价。

## 常用命令

```bash
# 全量测试(交付前必跑)
.venv/Scripts/python.exe -m pytest tests -q

# 重播种演示数据(先停 8788/8789/8800)
.venv/Scripts/python.exe -m bank_core.seed

# 前端 e2e(转账全流程+截图)
cd webui && node e2e-check.mjs
```
