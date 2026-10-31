"use client";

import { isToday, isYesterday, subMonths, subWeeks } from "date-fns";
import { motion } from "framer-motion";
import {
  ChevronDownIcon,
  ChevronRightIcon,
  MessageCircleIcon,
} from "lucide-react";
import { usePathname, useRouter } from "next/navigation";
import type { User } from "next-auth";
import {
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
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
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  useSidebar,
} from "@/components/ui/sidebar";
import { bankAgentBase, bankAuthHeaders } from "@/hooks/use-bank-auth";
import type { Chat } from "@/lib/db/schema";
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

// 双面板(会话/微信 ClawBot)布局持久化:比例与折叠态存 localStorage,
// 刷新/换设备标签页保持上次布局。SSR 安全:initializer 里判 window。
const RATIO_KEY = "sidebar-wx-ratio";
const WEB_COLLAPSED_KEY = "sidebar-web-collapsed";
const WX_COLLAPSED_KEY = "sidebar-wx-collapsed";
const WX_RATIO_DEFAULT = 0.38; // 微信分区默认占两区可用高度的 38%
const WX_RATIO_MIN = 0.12;
const WX_RATIO_MAX = 0.85;

const readLS = (key: string, fallback: string) =>
  typeof window === "undefined"
    ? fallback
    : (window.localStorage.getItem(key) ?? fallback);

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
    // 2s 轮询:与 useDeviceSync 的 1.5s 会话内容同步同节奏——微信 ClawBot
    // 侧每轮对话(relay 回合)登记进目录后,列表标题/新会话约 2 秒内跟进,
    // 与"正在看着的会话内容刷新"体感一致(实时);焦点重验证仍刻意关闭
    { fallbackData: [], refreshInterval: 2000, revalidateOnFocus: false }
  );

  const router = useRouter();
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [showDeleteDialog, setShowDeleteDialog] = useState(false);

  // ---- 双面板布局:上下两区可折叠,中间分隔条可拖拽调相对高度 ----
  const containerRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{ startY: number; startRatio: number } | null>(null);
  const [wxRatio, setWxRatio] = useState<number>(
    () =>
      Number.parseFloat(readLS(RATIO_KEY, String(WX_RATIO_DEFAULT))) ||
      WX_RATIO_DEFAULT
  );
  const [webCollapsed, setWebCollapsed] = useState<boolean>(
    () => readLS(WEB_COLLAPSED_KEY, "0") === "1"
  );
  const [wxCollapsed, setWxCollapsed] = useState<boolean>(
    () => readLS(WX_COLLAPSED_KEY, "0") === "1"
  );
  useEffect(() => {
    window.localStorage.setItem(WEB_COLLAPSED_KEY, webCollapsed ? "1" : "0");
  }, [webCollapsed]);
  useEffect(() => {
    window.localStorage.setItem(WX_COLLAPSED_KEY, wxCollapsed ? "1" : "0");
  }, [wxCollapsed]);

  const handleDividerDown = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      e.currentTarget.setPointerCapture(e.pointerId);
      dragRef.current = { startRatio: wxRatio, startY: e.clientY };
    },
    [wxRatio]
  );
  const handleDividerMove = useCallback(
    (e: ReactPointerEvent<HTMLDivElement>) => {
      if (!dragRef.current || !containerRef.current) {
        return;
      }
      const height = containerRef.current.clientHeight || 1;
      const next =
        dragRef.current.startRatio +
        (e.clientY - dragRef.current.startY) / height;
      const clamped = Math.min(WX_RATIO_MAX, Math.max(WX_RATIO_MIN, next));
      setWxRatio(clamped);
      window.localStorage.setItem(RATIO_KEY, String(clamped));
    },
    []
  );
  const handleDividerUp = useCallback(() => {
    dragRef.current = null;
  }, []);
  const toggleWeb = useCallback(() => {
    setWebCollapsed((v) => !v);
  }, []);
  const toggleWx = useCallback(() => {
    setWxCollapsed((v) => !v);
  }, []);

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

  // ---- 会话重命名:手动命名后 title_locked,标题不再随内容变化 ----
  const [renameTarget, setRenameTarget] = useState<{
    id: string;
    title: string;
  } | null>(null);
  const [renameValue, setRenameValue] = useState("");
  const [renaming, setRenaming] = useState(false);

  const handleShowRename = useCallback((chatId: string, title: string) => {
    setRenameTarget({ id: chatId, title });
    setRenameValue(title);
  }, []);

  const handleRename = useCallback(async () => {
    const target = renameTarget;
    const title = renameValue.trim();
    if (!target || !title || title === target.title) {
      setRenameTarget(null);
      return;
    }
    setRenaming(true);
    try {
      const res = await fetch(
        `${bankAgentBase()}/api/threads/${encodeURIComponent(target.id)}`,
        {
          body: JSON.stringify({ title }),
          headers: { ...bankAuthHeaders(), "Content-Type": "application/json" },
          method: "PATCH",
        }
      );
      if (!res.ok) {
        const body = (await res.json().catch(() => null)) as {
          error?: string;
        } | null;
        toast.error(body?.error ?? "重命名失败，请稍后重试");
        return;
      }
      setRenameTarget(null);
      mutate();
      if (searchUrl) {
        globalMutate(searchUrl);
      }
      toast.success("会话已重命名，标题已固定");
    } catch {
      toast.error("重命名失败，请检查网络");
    } finally {
      setRenaming(false);
    }
  }, [globalMutate, mutate, renameTarget, renameValue, searchUrl]);

  const closeRename = useCallback(() => {
    setRenameTarget(null);
  }, []);

  // 遮罩/ESC 关闭:重命名请求进行中不允许关(防半途丢状态)
  const closeRenameFromOverlay = useCallback(
    (open: boolean) => {
      if (!open && !renaming) {
        setRenameTarget(null);
      }
    },
    [renaming]
  );

  const handleRenameInput = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setRenameValue(e.target.value);
    },
    []
  );

  const handleRenameKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter" && renameValue.trim() && !renaming) {
        handleRename();
      }
    },
    [handleRename, renameValue, renaming]
  );

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

  // 搜索态:结果按相关度混排,不做双面板分区(来源分区在搜索里没有意义)
  if (searching) {
    return (
      <SidebarGroup className="group-data-[collapsible=icon]:hidden">
        <SidebarGroupLabel className="text-[10px] font-semibold tracking-[0.12em] text-sidebar-foreground/70">
          {`搜索结果 ${searchData?.total ?? chatsFromHistory.length} 条`}
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
                      onRename={handleShowRename}
                      setOpenMobile={setOpenMobile}
                      snippet={chat.snippet}
                    />
                  ))}
                </div>
              ))}
              {chatsFromHistory.length === 0 ? (
                <div className="px-3 py-4 text-center text-[12px] leading-relaxed text-sidebar-foreground/60">
                  {`没有找到与「${debouncedQuery}」相关的会话,换个关键词试试`}
                </div>
              ) : null}
            </div>
          </SidebarMenu>
        </SidebarGroupContent>
      </SidebarGroup>
    );
  }

  const bothExpanded = !webCollapsed && !wxCollapsed;

  return (
    <>
      <SidebarGroup className="flex min-h-0 flex-1 flex-col group-data-[collapsible=icon]:hidden">
        <div className="flex h-full min-h-0 flex-col" ref={containerRef}>
          <HistorySection
            collapsed={webCollapsed}
            count={webChats.length}
            grow={wxCollapsed ? 1 : 1 - wxRatio}
            onToggle={toggleWeb}
            title="会话"
          >
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
                    onRename={handleShowRename}
                    setOpenMobile={setOpenMobile}
                  />
                ))}
              </div>
            ))}

            {webChats.length === 0 ? (
              <div className="px-3 py-4 text-center text-[12px] leading-relaxed text-sidebar-foreground/60">
                还没有会话,点上方 + 新建一个吧
              </div>
            ) : (
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
          </HistorySection>

          {/* 分隔条:两区都展开时可拖拽调相对高度(下拖放大微信区);折叠时只是分界线 */}
          <div
            className={
              bothExpanded
                ? "group relative h-2 shrink-0 touch-none select-none"
                : "h-px shrink-0 bg-sidebar-foreground/10"
            }
            onLostPointerCapture={handleDividerUp}
            onPointerDown={bothExpanded ? handleDividerDown : undefined}
            onPointerMove={bothExpanded ? handleDividerMove : undefined}
            onPointerUp={handleDividerUp}
            style={bothExpanded ? { cursor: "row-resize" } : undefined}
            title={bothExpanded ? "拖动调整两个板块的相对高度" : undefined}
          >
            {bothExpanded ? (
              <div className="absolute inset-x-8 top-1/2 h-0.5 -translate-y-1/2 rounded-full bg-sidebar-foreground/10 transition-colors group-hover:bg-brand-red/50" />
            ) : null}
          </div>

          <HistorySection
            collapsed={wxCollapsed}
            count={wechatChats.length}
            grow={webCollapsed ? 1 : wxRatio}
            icon={<MessageCircleIcon className="size-3 shrink-0" />}
            onToggle={toggleWx}
            title="微信 ClawBot"
          >
            {wechatChats.map((chat) => (
              <ChatItem
                chat={chat}
                isActive={chat.id === id}
                key={chat.id}
                onDelete={handleShowDeleteDialog}
                onRename={handleShowRename}
                setOpenMobile={setOpenMobile}
              />
            ))}
            {wechatChats.length === 0 ? (
              <div className="px-3 py-4 text-center text-[12px] leading-relaxed text-sidebar-foreground/60">
                配置微信 ClawBot 后,微信里的对话会出现在这里
              </div>
            ) : null}
          </HistorySection>
        </div>
      </SidebarGroup>

      <Dialog
        onOpenChange={closeRenameFromOverlay}
        open={renameTarget !== null}
      >
        <DialogContent className="sm:max-w-sm" data-testid="rename-dialog">
          <DialogHeader className="pb-1">
            <DialogTitle className="text-[15px]">重命名会话</DialogTitle>
            <DialogDescription className="text-[12px] leading-relaxed">
              手动命名后标题将固定,不再随对话内容自动变化
              (微信会话默认跟随你最新一条消息)。
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-3 px-1 pb-1">
            <Input
              autoFocus
              data-testid="rename-input"
              maxLength={60}
              onChange={handleRenameInput}
              onKeyDown={handleRenameKeyDown}
              value={renameValue}
            />
            <div className="flex justify-end gap-2">
              <Button
                disabled={renaming}
                onClick={closeRename}
                size="sm"
                variant="ghost"
              >
                取消
              </Button>
              <Button
                data-testid="rename-confirm"
                disabled={renaming || !renameValue.trim()}
                onClick={handleRename}
                size="sm"
              >
                {renaming ? <LoaderIcon /> : null}
                保存
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

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

/** 双面板里的一个区:标题固定在滚动区外(冻结),列表独立滚动。 */
function HistorySection({
  title,
  icon,
  count,
  collapsed,
  onToggle,
  grow,
  children,
}: {
  title: string;
  icon?: ReactNode;
  count: number;
  collapsed: boolean;
  onToggle: () => void;
  /** flex 比例(相对另一区);折叠时本区高度由标题行决定,不参与分配 */
  grow: number;
  children: ReactNode;
}) {
  const header = (
    <button
      aria-expanded={!collapsed}
      className="flex w-full items-center gap-1 px-2 py-1.5 text-left text-[10px] font-semibold tracking-[0.12em] text-sidebar-foreground/70 transition-colors hover:text-sidebar-foreground"
      onClick={onToggle}
      type="button"
    >
      {collapsed ? (
        <ChevronRightIcon className="size-3 shrink-0" />
      ) : (
        <ChevronDownIcon className="size-3 shrink-0" />
      )}
      {icon}
      <span className="flex-1 truncate">{title}</span>
      <span className="rounded-full bg-sidebar-foreground/10 px-1.5 text-[9px] leading-4 tabular-nums">
        {count}
      </span>
    </button>
  );

  if (collapsed) {
    return <div className="shrink-0">{header}</div>;
  }
  return (
    <section
      className="flex min-h-0 flex-col"
      style={{ flexBasis: 0, flexGrow: grow }}
    >
      {header}
      <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
        {children}
      </div>
    </section>
  );
}
