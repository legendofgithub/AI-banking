"use client";

/*
 * 顶栏只留侧栏开关与右上角动作区(手机端登录 + 管理台)。
 * 登录/登出入口 2026-10-20 起收编到左下角「设置」弹窗
 * (components/chat/sidebar-settings.tsx)。
 *
 * 原模板的 Private/Public 可见性开关已移除(2026-10-21):本项目的会话
 * 历史由 8800 按登录用户隔离,接口固定返回 private,模板那套切换既没有
 * 真实公开入口、也依赖并不存在的 NextAuth 会话,是纯 UI 假象。
 */

import { PanelLeftIcon, ShieldCheckIcon } from "lucide-react";
import { memo } from "react";
import { Button } from "@/components/ui/button";
import { useSidebar } from "@/components/ui/sidebar";
import { MobileLoginButton } from "./mobile-login-button";

/* 右上角动作区:手机端登录(扫码,位于后台入口左边) + 管理台。
   均仅 PC 端显示——前者是"电脑演示 → 手机接力"的入口,手机端自己没有
   扫码刚需;管理台 8789 只绑 127.0.0.1,新标签打开的是本机地址。 */
function HeaderActions() {
  return (
    <div className="ml-auto flex items-center gap-2">
      <MobileLoginButton />
      <a
        aria-label="后台入口"
        className="hidden h-8 shrink-0 items-center gap-1.5 rounded-lg border border-border/60 bg-card/60 px-2.5 text-[12px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground md:inline-flex"
        data-testid="admin-console-link"
        href="http://127.0.0.1:8789/"
        rel="noopener noreferrer"
        target="_blank"
        title="打开管理台(仅本机 127.0.0.1:8789 可访问)"
      >
        <ShieldCheckIcon className="size-3.5" />
        <span>后台入口</span>
      </a>
    </div>
  );
}

function PureChatHeader() {
  const { state, toggleSidebar, isMobile } = useSidebar();

  if (state === "collapsed" && !isMobile) {
    // 折叠侧栏时原实现整条顶栏不渲染;后台入口是测试刚需,
    // 保留一条极简顶栏(只有右上角动作区,不挤占内容)。
    return (
      <header className="sticky top-0 flex h-12 items-center bg-sidebar px-3">
        <HeaderActions />
      </header>
    );
  }

  return (
    <header className="sticky top-0 flex h-14 items-center gap-2 bg-sidebar px-3">
      <Button
        aria-label="会话总览"
        className="md:hidden"
        data-testid="mobile-session-button"
        onClick={toggleSidebar}
        size="icon-sm"
        variant="ghost"
      >
        <PanelLeftIcon className="size-4" />
      </Button>

      <HeaderActions />
    </header>
  );
}

export const ChatHeader = memo(PureChatHeader);
