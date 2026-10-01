# 免登录会话持久化方案(方案 A)

## 背景结论
后端会话持久化已就绪(`/api/threads` 会话目录 + `/api/history` 消息回放,SQLite 双库),前端侧边栏也已接线。唯一障碍:`(chat)/layout.tsx` 把 `session?.user`(恒为空,因中间件已拆)传给侧边栏,`sidebar-history.tsx:165` 因此显示"Login to save and revisit previous chats!"且不拉取会话目录。登录页是无数据库的死胡同,直接绕开。

**登录信息的存储答案:免登录 = 不存;会话信息 = 服务端 SQLite(agent_ckpt.sqlite 检查点 + agent_threads.sqlite 目录),前端零状态,任何浏览器打开都能续聊。真账号体系(Postgres + Drizzle)按研发计划留给 M3 用户画像阶段。**

## 改动清单(共 3 处修改 + 2 处清理)

1. **`webui/app/(chat)/layout.tsx`** — 无会话时注入演示用户:
   ```tsx
   const user = session?.user ?? { email: "guest-0", id: "guest-0", name: "访客", type: "guest" };
   <AppSidebar user={user} />
   ```
   侧边栏立即显示服务端会话目录,登录提示永不出现。(实施时先读 `app-sidebar.tsx` 的 User 类型定义,注入对象需满足;必要时微调类型。)

2. **`webui/components/chat/sidebar-user-nav.tsx`** — 演示模式下隐藏死胡同的"Login to your account / Sign out"按钮,仅保留主题切换;用户名显示"访客"。

3. **`webui/components/chat/sidebar-history.tsx`** — 不改逻辑(注入用户后 `!user` 分支自然不可达),仅把提示文案兜底改为中文,防止未来回退。

4. **清理死代码**:删除 `webui/app/(chat)/api/history/route.ts` 与 `api/messages/route.ts` 两个空桩(前端已直连 agent 后端,删前 grep 确认无其他调用方)。`/login`、`/register` 页面文件保留不动(无入口链接,零风险)。

## 验证(两轮)
1. 回归:`node e2e-check.mjs` 全流程仍绿(打字→确认卡→扣款)。
2. 新增会话恢复测试(扩展 e2e 脚本或临时脚本):发消息 → **刷新页面** → 侧边栏出现该会话 → 点击 → 消息从 `/api/history` 完整恢复 → 截图取证。
3. 顺手确认内嵌浏览器侧边栏也可见会话目录(无 Cookie 依赖,IAB 可用)。

## 不做的事
- 不恢复任何登录/注册流程(留 M3:Postgres + NextAuth credentials,按研发计划路线)。
- 不动 agent 后端(已就绪)。