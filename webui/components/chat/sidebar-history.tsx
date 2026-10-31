"use client";

import { isToday, isYesterday, subMonths, subWeeks } from "date-fns";
import { MessageCircleIcon } from "lucide-react";
import { motion } from "framer-motion";
import { usePathname, useRouter } from "next/navigation";
import type { User } from "next-auth";
import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import useSWR, { useSWRConfig } from "swr";
import useSWRInfinite from "swr/infinite";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  useSidebar,
} from "@/components/ui/sidebar";
import type { Chat } from "@/lib/db/schema";
import { bankAgentBase, bankAuthHeaders } from "@/hooks/use-bank-auth";
import { fetcher } from "@/lib/utils";
import { LoaderIcon } from "./icons";
import { ChatItem } from "./sidebar-history-item";

type HistoryChat = Chat & { snippet?: string };

type GroupedChats = {
  today: HistoryChat[];
  yesterday: HistoryChat[];
  lastWeek: HistoryChat[];
  lastMonth: HistoryChat[];
  older: HistoryChat[];
};

export type ChatHistory = {
  chats: (Chat & { snippet?: string })[];
  hasMore: boolean;
  query?: string;
  total?: number;
};

const PAGE_SIZE = 20;

const groupChatsByDate = (chats: HistoryChat[]): GroupedChats => {
  const now = new Date();
  const oneWeekAgo = subWeeks(now, 1);
  const oneMonthAgo = subMonths(now, 1);

  return chats.reduce(
    (groups, chat) => {
      const chatDate = new Date(chat.createdAt);

      if (isToday(chatDate)) {
        groups.today.push(chat);
      } else if (isYesterday(chatDate)) {
        groups.yesterday.push(chat);
      } else if (chatDate > oneWeekAgo) {
        groups.lastWeek.push(chat);
      } else if (chatDate > oneMonthAgo) {
        groups.lastMonth.push(chat);
      } else {
        groups.older.push(chat);
      }

      return groups;
    },
    {
      lastMonth: [],
      lastWeek: [],
      older: [],
      today: [],
      yesterday: [],
    } as GroupedChats
  );
};

export function getChatHistoryPaginationKey(
  pageIndex: number,
  previousPageData: ChatHistory
) {
  if (previousPageData && previousPageData.hasMore === false) {
    return null;
  }

  // 会话目录来自 agent 后端(服务端持久化),offset 分页:
  // 手机端会话总览滚动到底会继续加载,直到接口返回 hasMore=false。
  // 登录态由全局 fetcher 带 x-bank-token 头。
  const agentBase = `${typeof window === "undefined" ? "http://127.0.0.1" : `${window.location.protocol}//${window.location.hostname}`}:${process.env.NEXT_PUBLIC_AGENT_API_PORT ?? "8800"}`;
  const page = Math.max(0, pageIndex);
  return `${agentBase}/api/threads?limit=${PAGE_SIZE}&offset=${page * PAGE_SIZE}`;
}

export function SidebarHistory({
  user,
  searchQuery = "",
}: {
  user: User | undefined;
  searchQuery?: string;
}) {
  const { setOpenMobile } = useSidebar();
  const pathname = usePathname();
  const id = pathname?.startsWith("/chat/") ? pathname.split("/")[2] : null;
  const { mutate: globalMutate } = useSWRConfig();

  // 输入防抖:每 250ms 才打一次全文检索(后端要读会话 checkpoint)
  const [debouncedQuery, setDebouncedQuery] = useState("");
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(searchQuery.trim()), 250);
    return () => clearTimeout(timer);
  }, [searchQuery]);
  const searching = debouncedQuery.length > 0;
  const agentBase = `${typeof window === "undefined" ? "http://127.0.0.1" : `${window.location.protocol}//${window.location.hostname}`}:${process.env.NEXT_PUBLIC_AGENT_API_PORT ?? "8800"}`;
  const searchUrl =
    user && searching
      ? `${agentBase}/api/threads?limit=200&q=${encodeURIComponent(debouncedQuery)}`
      : null;
  const { data: searchData, isLoading: searchLoading } = useSWR<ChatHistory>(
    searchUrl,
    fetcher,
    { revalidateOnFocus: false }
  );

  const {
    data: paginatedChatHistories,
    setSize,
    isValidating,
    isLoading,
    mutate,
  } = useSWRInfinite<ChatHistory>(
    user && !searching ? getChatHistoryPaginationKey : () => null,
    fetcher,
    { fallbackData: [], revalidateOnFocus: false }
  );

  const router = useRouter();
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);

  const chatsFromHistory = searching
    ? (searchData?.chats ?? [])
    : (paginatedChatHistories ?? []).flatMap((page) => page.chats);
  // 微信(ClawBot/公众号)会话:thread_id 以 wx: 开头,单独成区(在会话分组下方);
  // 搜索时不拆——结果按相关度混排,来源分区没有意义。
  const isWechatChat = (c: HistoryChat) => c.id.startsWith("wx:");
  const wechatChats = searching ? [] : chatsFromHistory.filter(isWechatChat);
  const webChats = searching
    ? chatsFromHistory
    : chatsFromHistory.filter((c) => !isWechatChat(c));
  const groupedChats = groupChatsByDate(webChats);
  const historyLoading = searching ? searchLoading : isLoading;

  const hasReachedEnd = searching
    ? true
    : paginatedChatHistories
      ? paginatedChatHistories.some((page) => page.hasMore === false)
      : false;

  const hasEmptyChatHistory = searching
    ? Boolean(searchData) && chatsFromHistory.length === 0
    : paginatedChatHistories
      ? paginatedChatHistories.every((page) => page.chats.length === 0)
      : false;

  const historyGroups = [
    { chats: groupedChats.today, label: "今天" },
    { chats: groupedChats.yesterday, label: "昨天" },
    { chats: groupedChats.lastWeek, label: "最近 7 天" },
    { chats: groupedChats.lastMonth, label: "最近 30 天" },
    { chats: groupedChats.older, label: "更早" },
  ].filter((group) => group.chats.length > 0);

  const handleDelete = useCallback(async () => {
    const chatToDelete = deleteId;
    if (!chatToDelete) {
      return;
    }
    const isCurrentChat = pathname === `/chat/${chatToDelete}`;

    setShowDeleteDialog(false);

    // 会话目录在 8800(bank agent),不是 Next 的本地聊天库——必须打对端点,
    // 否则只是前端乐观移除,刷新后会话又回来(2026-10-26 修复)。
    try {
      const res = await fetch(
        `${bankAgentBase()}/api/threads/${encodeURIComponent(chatToDelete)}`,
        { headers: bankAuthHeaders(), method: "DELETE" }
      );
      if (!res.ok) {
        const body = (await res.json().catch(() => null)) as {
          error?: string;
        } | null;
        toast.error(body?.error ?? "删除失败，请稍后重试");
        return;
      }
      if (isCurrentChat) {
        router.replace("/");
      }
      mutate();
      if (searchUrl) {
        globalMutate(searchUrl);
      }
      toast.success("会话已删除");
    } catch {
      toast.error("删除失败，请检查网络");
    }
  }, [deleteId, globalMutate, mutate, pathname, router, searchUrl]);

  const handleShowDeleteDialog = useCallback((chatId: string) => {
    setDeleteId(chatId);
    setShowDeleteDialog(true);
  }, []);

  const handleViewportEnter = useCallback(() => {
    if (!isValidating && !hasReachedEnd) {
      setSize((size) => size + 1);
    }
  }, [hasReachedEnd, isValidating, setSize]);

  if (!user) {
    return (
      <SidebarGroup className="group-data-[collapsible=icon]:hidden">
        <SidebarGroupContent>
          <div className="flex w-full flex-row items-center justify-center gap-2 px-2 text-[13px] text-sidebar-foreground/60">
            登录后可保存历史会话(当前演示已免登录,正常不会看到本提示)
          </div>
        </SidebarGroupContent>
      </SidebarGroup>
    );
  }

  if (historyLoading) {
    return (
      <SidebarGroup className="group-data-[collapsible=icon]:hidden">
        <SidebarGroupLabel className="text-[10px] font-semibold tracking-[0.12em] text-sidebar-foreground/70">
          {searching ? "搜索中…" : "会话"}
        </SidebarGroupLabel>
        <SidebarGroupContent>
          <div className="flex flex-col gap-0.5 px-1">
            {[44, 32, 28, 64, 52].map((item) => (
              <div
                className="flex h-8 items-center gap-2 rounded-lg px-2"
                key={item}
              >
                <div
                  className="h-3 max-w-(--skeleton-width) flex-1 animate-pulse rounded-md bg-sidebar-foreground/[0.06]"
                  style={
                    {
                      "--skeleton-width": `${item}%`,
                    } as React.CSSProperties
                  }
                />
              </div>
            ))}
          </div>
        </SidebarGroupContent>
      </SidebarGroup>
    );
  }

  if (hasEmptyChatHistory) {
    return (
      <SidebarGroup className="group-data-[collapsible=icon]:hidden">
        <SidebarGroupLabel className="text-[10px] font-semibold tracking-[0.12em] text-sidebar-foreground/70">
          {searching ? "搜索结果" : "会话"}
        </SidebarGroupLabel>
        <SidebarGroupContent>
          <div className="flex w-full flex-row items-center justify-center gap-2 px-3 py-4 text-center text-[12px] leading-relaxed text-sidebar-foreground/60">
            {searching
              ? `没有找到与「${debouncedQuery}」相关的会话,换个关键词试试`
              : "还没有会话,点上方 + 新建一个吧"}
          </div>
        </SidebarGroupContent>
      </SidebarGroup>
    );
  }

  return (
    <>
      <SidebarGroup className="group-data-[collapsible=icon]:hidden">
        <SidebarGroupLabel className="text-[10px] font-semibold tracking-[0.12em] text-sidebar-foreground/70">
          {searching
            ? `搜索结果 ${searchData?.total ?? chatsFromHistory.length} 条`
            : "会话"}
        </SidebarGroupLabel>
        <SidebarGroupContent>
          <SidebarMenu>
            <div className="flex flex-col gap-4">
              {historyGroups.map((group) => (
                <div key={group.label}>
                  <div className="px-2 py-1 text-[10px] font-semibold tracking-[0.12em] text-sidebar-foreground/70">
                    {group.label}
                  </div>
                  {group.chats.map((chat) => (
                    <ChatItem
                      chat={chat}
                      isActive={chat.id === id}
                      key={chat.id}
                      onDelete={handleShowDeleteDialog}
                      setOpenMobile={setOpenMobile}
                      snippet={searching ? chat.snippet : undefined}
                    />
                  ))}
                </div>
              ))}

              {/* 微信 ClawBot 分区:微信侧对话(wx: 会话)单独归档,点开即完整历史 */}
              {wechatChats.length > 0 ? (
                <div>
                  <div className="flex items-center gap-1.5 px-2 py-1 text-[10px] font-semibold tracking-[0.12em] text-sidebar-foreground/70">
                    <MessageCircleIcon className="size-3" />
                    微信 ClawBot
                  </div>
                  {wechatChats.map((chat) => (
                    <ChatItem
                      chat={chat}
                      isActive={chat.id === id}
                      key={chat.id}
                      onDelete={handleShowDeleteDialog}
                      setOpenMobile={setOpenMobile}
                    />
                  ))}
                </div>
              ) : null}
            </div>
          </SidebarMenu>

          {searching ? null : (
            <>
              <motion.div onViewportEnter={handleViewportEnter} />

              {hasReachedEnd ? null : (
                <div className="mt-1 flex flex-row items-center gap-2 px-4 py-2 text-sidebar-foreground/50">
                  <div className="animate-spin">
                    <LoaderIcon />
                  </div>
                  <div className="text-[11px]">Loading...</div>
                </div>
              )}
            </>
          )}
        </SidebarGroupContent>
      </SidebarGroup>

      <AlertDialog onOpenChange={setShowDeleteDialog} open={showDeleteDialog}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Are you absolutely sure?</AlertDialogTitle>
            <AlertDialogDescription>
              This action cannot be undone. This will permanently delete your
              chat and remove it from our servers.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleDelete}>
              Continue
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
