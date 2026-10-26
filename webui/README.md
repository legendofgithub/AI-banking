# webui —— AI 银行助手对话前端

AI Banking Agent 的对话前端(Next.js + AI SDK):自然语言覆盖转账、账单分析、
理财、卡片、订阅/代扣、跨场景联动六大场景;动钱操作一律经过确认卡与支付
密码两步闸门。

- 开发:`pnpm dev`(先按根 README 启动 8800 Agent API)
- 构建:`pnpm build`;e2e:`pnpm test`(Playwright)

架构与安全设计见根目录 README 与 docs/。前端基于 Vercel 开源 chatbot 模板
改造,许可证见本目录 LICENSE。
