"use client";

/*
 * 顶栏只留侧栏开关、会话可见性与管理台入口。登录/登出入口 2026-10-20 起
 * 收编到左下角「设置」弹窗(components/chat/sidebar-settings.tsx)。
 */

import { PanelLeftIcon, ShieldCheckIcon } from "lucide-react";
import { memo } from "react";
import { Button } from "@/components/ui/button";
import { useSidebar } from "@/components/ui/sidebar";
import { VisibilitySelector, type VisibilityType } from "./visibility-selector";

/* 管理台入口(演示/测试便利):仅 PC 端显示,手机端不出现该按钮。
   管理台 8789 只绑 127.0.0.1,新标签打开的是本机地址。 */
function AdminConsoleLink() {
  return (
    <a
      aria-label="后台入口"
      className="ml-auto hidden h-8 shrink-0 items-center gap-1.5 rounded-lg border border-border/60 bg-card/60 px-2.5 text-[12px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground md:inline-flex"
      data-testid="admin-console-link"
      href="http://127.0.0.1:8789/"
      rel="noopener noreferrer"
      target="_blank"
      title="打开管理台(仅本机 127.0.0.1:8789 可访问)"
    >
      <ShieldCheckIcon className="size-3.5" />
      <span>后台入口</span>
    </a>
  );
}

function PureChatHeader({
  chatId,
  selectedVisibilityType,
  isReadonly,
}: {
  chatId: string;
  selectedVisibilityType: VisibilityType;
  isReadonly: boolean;
}) {
  const { state, toggleSidebar, isMobile } = useSidebar();

  if (state === "collapsed" && !isMobile) {
    // 折叠侧栏时原实现整条顶栏不渲染;后台入口是测试刚需,
    // 保留一条极简顶栏(只有右上角入口,不挤占内容)。
    return (
      <header className="sticky top-0 flex h-12 items-center bg-sidebar px-3">
        <AdminConsoleLink />
      </header>
    );
  }

  return (
    <header className="sticky top-0 flex h-14 items-center gap-2 bg-sidebar px-3">
      <Button
        className="md:hidden"
        onClick={toggleSidebar}
        size="icon-sm"
        variant="ghost"
      >
        <PanelLeftIcon className="size-4" />
      </Button>

      {!isReadonly && (
        <VisibilitySelector
          chatId={chatId}
          selectedVisibilityType={selectedVisibilityType}
        />
      )}

      <AdminConsoleLink />
    </header>
  );
}

export const ChatHeader = memo(
  PureChatHeader,
  (prevProps, nextProps) =>
    prevProps.chatId === nextProps.chatId &&
    prevProps.selectedVisibilityType === nextProps.selectedVisibilityType &&
    prevProps.isReadonly === nextProps.isReadonly
);
