"use client";

/*
 * 空会话首屏。
 * 2026-10-27:由居中大标题改为客服会话范式(头像 + 服务名 + 白色气泡 + 相关问题卡),
 * 具体版式在 agent-welcome.tsx;此文件保留原导出名,避免改动引用点。
 */

import { AgentWelcome } from "./agent-welcome";

export const Greeting = () => <AgentWelcome />;
