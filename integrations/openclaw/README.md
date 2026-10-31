# OpenClaw / 微信 ClawBot 接入

让 BankAgent 出现在微信聊天列表里:用户在手机上对 bot 说"查一下我的余额",
消息经微信官方通道 → 本机 OpenClaw → BankAgent 的 49 个 MCP 工具执行 → 原样回传。

```
手机微信 ⇄ 微信官方中继(ilinkai.weixin.qq.com)
              ⇄ 本机 OpenClaw 网关(:18789)          ← 出站长连,无需公网/穿透
                  ⇄ 技能 bankagent-relay → POST /api/wechat/relay
                      ⇄ BankAgent 后端(:8800) 图执行 + 审计
```

**合规性**:走微信官方「ClawBot」插件(I→设置→插件,腾讯 2026-03 发布,
逐步放量)。个人号协议/客户端 hook 类方案一律不用。

---

## 一、前置条件

| 项 | 要求 | 说明 |
|----|------|------|
| Node.js | ≥ 22.19;用最新 OpenClaw 需 ≥ 24.22.1 | 低于 24.22.1 会自动装 2026.6.35 |
| 微信 | 客户端有「我 → 设置 → 插件 → 微信 ClawBot」入口 | 灰度功能,没有入口就只能等 |
| BankAgent | 演示栈已起(尤其 8800) | `启动演示-Windows.bat` |
| 模型 | OpenClaw 自己需要一个可用 LLM | 见下方"第 1 步",与 BankAgent 的 key 相互独立 |

## 二、三步装好

```bash
# ① 自动检测/安装 OpenClaw + 微信插件 + 转发技能
.venv/Scripts/python.exe scripts/setup_clawbot.py
#   只体检不改动:
.venv/Scripts/python.exe scripts/setup_clawbot.py --check

# ② 给 OpenClaw 配模型(脚本不会替你填 key)
#   在 ~/.openclaw/openclaw.json 的 models.providers 下加一个供应商,例如:
#   "deepseek": { "baseUrl": "https://api.deepseek.com/v1", "apiKey": "sk-...",
#                 "api": "openai-completions",
#                 "models": [{ "id": "deepseek-chat", "name": "DeepSeek Chat",
#                              "contextWindow": 131072, "maxTokens": 8192,
#                              "input": ["text"] }] }
openclaw models set deepseek/deepseek-chat

# ③ 扫码 + 绑定
#   webui(http://127.0.0.1:3000)顶栏「微信 ClawBot」按钮扫码;
#   或:openclaw channels login --channel openclaw-weixin
#   然后在微信 bot 对话里发:绑定 <你的手机号>
```

装完后用这两条确认:微信里发「查一下我的余额」应收到真实余额,
`openclaw channels status` 应显示 `openclaw-weixin ... running`。

## 三、踩过的坑(实测,按遇到概率排序)

1. **终端 ASCII 二维码扫不动**——字符画被字体压扁导致几何畸变。
   必须用图片二维码:webui 的按钮/`~/.openclaw/clawbot_qr.py` 都渲染 PNG。
2. **`npm install -g openclaw` 静默失败**(Windows 上全局目录被占用/权限)。
   `setup_clawbot.py` 会自动回退到独立前缀 `~/openclaw-new`;手动装时用
   `npm install --prefix ~/openclaw-new openclaw@<版本>`。
3. **新版 OpenClaw 要 Node ≥24.22.1**,否则 preinstall 直接报错退出。
   Node 24.13 这类"看起来够新"的版本也会被拒 → 用 2026.6.35。
4. **旧配置会让新网关起不来**:`channels.telegram.streaming` 等历史键在新版
   校验失败 → `openclaw doctor --fix`。
5. **Telegram 通道会把网关拖崩**:它断线重连时卡住主流程(实测导致
   "微信 bot 暂无法连接 openclaw")→ 不用就在配置里 `channels.telegram.enabled=false`。
6. **OpenClaw 自带模型凭证过期**:老配置的 minimax/google 可能早已失效,
   表现为微信里 bot 报错、日志 `authentication_error invalid api key`
   → 换一个确定可用的供应商并 `models set`。
7. **登录进程 10–20 秒才出码**(先跑 doctor 自检),弹窗里要给它时间。
8. **二维码是一次性凭证**:约 1–2 分钟失效,自动换新几次后退出。
   别指望复制链接长期有效。
9. **技能里的 `print` 必须 flush**——若自己改技能脚本并读它的管道输出,
   块缓冲会让读端一直等不到内容。

## 四、已知限制(诚实清单)

- **连接识别是"机器级",不是"用户级"**:`channels status` 只告诉你这台机器的
  `openclaw-weixin` 是否 running,不给"哪些微信用户已连"的名单。所以 webui 弹窗
  显示「✓ 微信已连接」意味着**通道通了**,不等于"当前登录的银行账户绑过微信"。
- **openid 可能退化成 `claw-default`**:技能拿不到发送者昵称时用兜底 ID,
  多用户场景会串号(共用同一份会话与绑定)。单人演示无影响;要多人用需先
  把真实 openid 打通(OpenClaw 的 session key 里有,如
  `agent:main:openclaw-weixin:direct:<openid>@im.wechat`)。
- **多账号扫码行为未验证**:第二个微信号扫同一个 bot 的码是"新增"还是"顶替",
  微信侧没有文档说明,请自测后再对外承诺。
- **电脑必须开着**:消息的"大脑"在本机,关机/断网即失联。
- **回复比 webui 慢几秒**:消息先过 OpenClaw 自己的调度模型,再由技能转发。

## 五、排障

```bash
openclaw channels status                    # 通道是否 running
openclaw sessions --json --limit 5          # 看过哪些微信用户(含真实 openid)
openclaw logs                               # 网关日志
curl http://127.0.0.1:8800/api/wechat/clawbot/state   # BankAgent 视角的连接态
curl -X POST http://127.0.0.1:8800/api/wechat/relay \
  -H "Content-Type: application/json" \
  -d '{"openid":"test","text":"查一下我的余额"}'      # 绕过微信直接验转发链路
```

技能文件位置:`~/.openclaw/workspace/skills/bankagent-relay/`
(改仓库里的 `integrations/openclaw/skills/bankagent-relay/` 后重跑安装脚本即可)。
