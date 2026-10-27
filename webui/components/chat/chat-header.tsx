"use client";

/*
 * 顶栏只留返回/会话总览与右上角动作区(手机端登录 + 管理台)。
 * 登录/登出入口 2026-10-20 起收编到左下角「设置」弹窗
 * (components/chat/sidebar-settings.tsx)。
 *
 * 2026-10-27 手机端改版:白底 + 左返回首页 + 居中服务名 + 右侧会话总览,
 * 与客服会话的公开范式一致;桌面端仍是品牌深蓝渐变,不受影响。
 *
 * 原模板的 Private/Public 可见性开关已移除(2026-10-21):本项目的会话
 * 历史由 8800 按登录用户隔离,接口固定返回 private,模板那套切换既没有
 * 真实公开入口、也依赖并不存在的 NextAuth 会话,是纯 UI 假象。
 */

import { ChevronLeftIcon, PanelLeftIcon, ShieldCheckIcon } from "lucide-react";
import Link from "next/link";
import { memo } from "react";
import { Button } from "@/components/ui/button";
import { useSidebar } from "@/components/ui/sidebar";
import { MobileLoginButton } from "./mobile-login-button";

/* 右上角动作区:手机端登录(扫码,位于后台入口左边) + 管理台。
   均仅 PC 端显示——前者是"电脑演示 → 手机接力"的入口,手机端自己没有
   扫码刚需;管理台 8789 只绑 127.0.0.1,新标签打开的是本机地址。 */
function HeaderActions() {
  return (
    <div className="flex items-center gap-2 md:ml-auto">
      <MobileLoginButton />
      <a
        aria-label="后台入口"
        className="hidden h-8 shrink-0 items-center gap-1.5 rounded-lg border border-white/25 bg-white/10 px-2.5 text-[12px] text-white/90 transition-colors hover:bg-white/20 hover:text-white md:inline-flex"
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
      <header className="sticky top-0 flex h-12 items-center bg-gradient-to-r from-ink to-ink-3 px-3 shadow-[var(--shadow-card)]">
        <HeaderActions />
      </header>
    );
  }

  return (
    <header className="sticky top-0 z-20 flex h-12 items-center border-b border-[#EBEEF2] bg-white px-1 md:h-14 md:gap-2 md:border-0 md:bg-gradient-to-r md:from-ink md:to-ink-3 md:px-3 md:shadow-[var(--shadow-card)]">
      {/* 手机端:返回银行首页(与首页「问一问」入口对称) */}
      <Link
        aria-label="返回首页"
        className="relative z-10 flex size-9 items-center justify-center rounded-full text-[#1F2329] transition-colors active:bg-[#F0F1F3] md:hidden"
        href="/home"
      >
        <ChevronLeftIcon className="size-5" />
      </Link>

      {/* 手机端:居中服务名(不吃点击) */}
      <div className="pointer-events-none absolute inset-x-0 flex justify-center md:hidden">
        <span className="font-semibold text-[16px] text-[#1F2329]">
          智能客服
        </span>
      </div>

      {/* 手机端:会话总览(原左侧入口,让位给返回后移到右侧) */}
      <div className="ml-auto flex md:hidden">
        <Button
          aria-label="会话总览"
          className="size-9 text-[#1F2329] hover:bg-[#F0F1F3] hover:text-[#1F2329]"
          data-testid="mobile-session-button"
          onClick={toggleSidebar}
          size="icon-sm"
          variant="ghost"
        >
          <PanelLeftIcon className="size-4" />
        </Button>
      </div>

      <HeaderActions />
    </header>
  );
}

export const ChatHeader = memo(PureChatHeader);
