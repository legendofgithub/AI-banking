"use client";

import { LogOutIcon, PanelLeftIcon, UserIcon } from "lucide-react";
import Link from "next/link";
import { memo, useCallback, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useSidebar } from "@/components/ui/sidebar";
import { useBankAuth } from "@/hooks/use-bank-auth";
import { VisibilitySelector, type VisibilityType } from "./visibility-selector";

/*
 * 右上角登录入口:未登录显示金色描边「登录」按钮(→ /login);
 * 已登录显示昵称胶囊,下拉菜单可登出(撤销会话 + 清本地 + 回首页)。
 */
function AuthMenu() {
  const { user, logout } = useBankAuth();
  const [open, setOpen] = useState(false);
  const handleLogout = useCallback(async () => {
    setOpen(false);
    await logout();
  }, [logout]);

  if (!user) {
    return (
      <Button
        asChild
        className="ml-auto rounded-lg border border-amber-500/60 bg-amber-500/10 px-4 text-amber-600 transition hover:bg-amber-500/20 hover:text-amber-500 dark:text-amber-300 dark:hover:text-amber-200"
        size="sm"
        variant="ghost"
      >
        <Link href="/login">登录</Link>
      </Button>
    );
  }

  return (
    <div className="ml-auto">
      <DropdownMenu onOpenChange={setOpen} open={open}>
        <DropdownMenuTrigger
          className="inline-flex h-8 items-center gap-1.5 rounded-full border border-amber-500/50 bg-amber-500/10 px-3 text-[13px] font-medium text-amber-600 transition hover:bg-amber-500/20 dark:text-amber-300"
          data-testid="user-pill"
        >
          <UserIcon className="size-3.5" />
          <span className="max-w-32 truncate">{user.nickname || "已登录"}</span>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-40">
          <DropdownMenuItem onClick={handleLogout}>
            <LogOutIcon className="size-4" />
            登出
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
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
    return null;
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

      <AuthMenu />
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
