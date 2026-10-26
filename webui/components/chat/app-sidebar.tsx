"use client";

import {
  ChevronLeftIcon,
  MessageSquareIcon,
  PanelLeftIcon,
  LandmarkIcon,
  PenSquareIcon,
  PlusIcon,
  SearchIcon,
  TrashIcon,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import type { User } from "next-auth";
import {
  type ChangeEvent,
  type FormEvent,
  useCallback,
  useEffect,
  useState,
} from "react";
import { toast } from "sonner";
import { useSWRConfig } from "swr";
import { unstable_serialize } from "swr/infinite";
import {
  getChatHistoryPaginationKey,
  SidebarHistory,
} from "@/components/chat/sidebar-history";
import { SidebarSettings } from "@/components/chat/sidebar-settings";
import { SidebarScrollbar } from "@/components/chat/sidebar-scrollbar";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
  SidebarTrigger,
  useSidebar,
} from "@/components/ui/sidebar";
import { bankAgentBase, bankAuthHeaders } from "@/hooks/use-bank-auth";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "../ui/alert-dialog";
import { Button } from "../ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";
import { Input } from "../ui/input";
import { Tooltip, TooltipContent, TooltipTrigger } from "../ui/tooltip";

export function AppSidebar({ user }: { user: User | undefined }) {
  const router = useRouter();
  const { isMobile, openMobile, setOpenMobile, toggleSidebar } = useSidebar();
  const { mutate } = useSWRConfig();
  const [showDeleteAllDialog, setShowDeleteAllDialog] = useState(false);
  const [query, setQuery] = useState("");
  const [newChatOpen, setNewChatOpen] = useState(false);
  const [newChatTitle, setNewChatTitle] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");

  const closeMobile = useCallback(() => {
    setOpenMobile(false);
  }, [setOpenMobile]);

  const handleToggleSidebar = useCallback(() => {
    toggleSidebar();
  }, [toggleSidebar]);

  // 新建会话(PC「New chat」与手机端「+」同一条流程):先命名再建会话,
  // 名称最长 10 个字(前后端同口径),防止自动标题把会话列表弄乱。
  const openNewChat = useCallback(() => {
    setNewChatOpen(true);
    setNewChatTitle("");
    setCreateError("");
  }, []);

  const cancelNewChat = useCallback(() => {
    setNewChatOpen(false);
    setNewChatTitle("");
    setCreateError("");
  }, []);

  const handleNewChatDialogChange = useCallback(
    (open: boolean) => {
      if (open) {
        openNewChat();
      } else {
        cancelNewChat();
      }
    },
    [cancelNewChat, openNewChat]
  );

  const handleQueryChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    setQuery(e.target.value);
  }, []);

  const handleTitleChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    setNewChatTitle(e.target.value.slice(0, 10));
    setCreateError("");
  }, []);

  const handleCreateNamedChat = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      const title = newChatTitle.trim();
      if (!title || creating) {
        return;
      }
      if (title.length > 10) {
        setCreateError("会话名称最多 10 个字");
        return;
      }
      setCreating(true);
      setCreateError("");
      try {
        const res = await fetch(`${bankAgentBase()}/api/threads`, {
          body: JSON.stringify({ title }),
          headers: { ...bankAuthHeaders(), "Content-Type": "application/json" },
          method: "POST",
        });
        const json = (await res.json().catch(() => null)) as {
          id?: string;
          error?: string;
        } | null;
        if (!res.ok || !json?.id) {
          setCreateError(json?.error ?? `创建失败(${res.status})`);
          return;
        }
        setNewChatOpen(false);
        setNewChatTitle("");
        setOpenMobile(false);
        router.push(`/chat/${json.id}`);
      } catch {
        setCreateError("网络异常,会话没有创建成功");
      } finally {
        setCreating(false);
      }
    },
    [creating, newChatTitle, router, setOpenMobile]
  );

  // 手机端弹窗关闭即清场:下次打开是干净的搜索/列表状态
  // (桌面端命名弹窗用 onOpenChange 自己收尾,不受 openMobile 影响)
  useEffect(() => {
    if (isMobile && !openMobile) {
      setQuery("");
      setNewChatOpen(false);
      setNewChatTitle("");
      setCreateError("");
      setCreating(false);
    }
  }, [isMobile, openMobile]);

  const handleShowDeleteAllDialog = useCallback(() => {
    setShowDeleteAllDialog(true);
  }, []);

  const handleDeleteAll = useCallback(async () => {
    setShowDeleteAllDialog(false);
    try {
      const res = await fetch(`${bankAgentBase()}/api/threads`, {
        headers: bankAuthHeaders(),
        method: "DELETE",
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => null)) as {
          error?: string;
        } | null;
        toast.error(body?.error ?? "清空失败，请稍后重试");
        return;
      }
      router.replace("/");
      mutate(unstable_serialize(getChatHistoryPaginationKey), [], {
        revalidate: false,
      });
      toast.success("已清空全部会话");
    } catch {
      toast.error("清空失败，请检查网络");
    }
  }, [mutate, router]);

  const mobileHeader = isMobile ? (
    newChatOpen ? (
      <form className="flex flex-col gap-1.5" onSubmit={handleCreateNamedChat}>
        <div className="flex items-center gap-2">
          <Button
            aria-label="返回会话列表"
            onClick={cancelNewChat}
            size="icon-sm"
            type="button"
            variant="ghost"
          >
            <ChevronLeftIcon />
          </Button>
          <div className="relative flex-1">
            <Input
              aria-label="会话名称"
              autoFocus
              className="rounded-lg pr-12"
              data-testid="session-name-input"
              maxLength={10}
              onChange={handleTitleChange}
              placeholder="给会话起个名字"
              value={newChatTitle}
            />
            <span className="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-[10px] text-muted-foreground">
              {newChatTitle.length}/10
            </span>
          </div>
          <Button
            data-testid="session-create-button"
            disabled={!newChatTitle.trim() || creating}
            size="sm"
            type="submit"
          >
            {creating ? "创建中…" : "创建"}
          </Button>
        </div>
        <p className="pl-10 text-[11px] text-muted-foreground">
          先命名再进入会话,最多 10 个字
        </p>
        {createError ? (
          <p className="pl-10 text-[11px] text-red-600 dark:text-red-300">
            {createError}
          </p>
        ) : null}
      </form>
    ) : (
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <SearchIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            aria-label="搜索会话"
            className="rounded-lg pl-9"
            data-testid="session-search"
            onChange={handleQueryChange}
            placeholder="搜索会话名称或内容"
            value={query}
          />
        </div>
        <Button
          aria-label="新建会话"
          data-testid="session-new-button"
          onClick={openNewChat}
          size="icon-sm"
          title="新建会话"
          type="button"
          variant="outline"
        >
          <PlusIcon />
        </Button>
      </div>
    )
  ) : null;

  return (
    <>
      <Sidebar collapsible="icon" mobileHeader={mobileHeader}>
        <SidebarHeader className="pb-0 pt-3">
          <SidebarMenu>
            <SidebarMenuItem className="flex flex-row items-center justify-between">
              <div className="group/logo relative flex items-center justify-center">
                <SidebarMenuButton
                  asChild
                  className="size-8 !px-0 items-center justify-center group-data-[collapsible=icon]:group-hover/logo:opacity-0"
                  tooltip="AI 银行助手"
                >
                  <Link href="/" onClick={closeMobile}>
                    <MessageSquareIcon className="size-4 text-sidebar-foreground/50" />
                  </Link>
                </SidebarMenuButton>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <SidebarMenuButton
                      className="pointer-events-none absolute inset-0 size-8 opacity-0 group-data-[collapsible=icon]:pointer-events-auto group-data-[collapsible=icon]:group-hover/logo:opacity-100"
                      onClick={handleToggleSidebar}
                    >
                      <PanelLeftIcon className="size-4" />
                    </SidebarMenuButton>
                  </TooltipTrigger>
                  <TooltipContent className="hidden md:block" side="right">
                    Open sidebar
                  </TooltipContent>
                </Tooltip>
              </div>
              <div className="group-data-[collapsible=icon]:hidden">
                <SidebarTrigger className="text-sidebar-foreground/60 transition-colors duration-150 hover:text-sidebar-foreground" />
              </div>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup className="pt-1">
            <SidebarGroupContent>
              <SidebarMenu>
                <SidebarMenuItem>
                  <SidebarMenuButton
                    className="h-8 rounded-lg border border-sidebar-border text-[13px] text-sidebar-foreground/70 transition-colors duration-150 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
                    data-testid="sidebar-home"
                    onClick={() => {
                      setOpenMobile(false);
                      router.push("/home");
                    }}
                    tooltip="银行首页"
                  >
                    <LandmarkIcon className="size-4" />
                    <span className="font-medium">银行首页</span>
                  </SidebarMenuButton>
                </SidebarMenuItem>
                {isMobile ? null : (
                  <SidebarMenuItem>
                    <SidebarMenuButton
                      className="h-8 rounded-lg border border-sidebar-border text-[13px] text-sidebar-foreground/70 transition-colors duration-150 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground"
                      onClick={openNewChat}
                      tooltip="New Chat"
                    >
                      <PenSquareIcon className="size-4" />
                      <span className="font-medium">新对话</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                )}
                {user ? (
                  <SidebarMenuItem>
                    <SidebarMenuButton
                      className="rounded-lg text-sidebar-foreground/40 transition-colors duration-150 hover:bg-destructive/10 hover:text-destructive"
                      onClick={handleShowDeleteAllDialog}
                      tooltip="清空所有对话"
                    >
                      <TrashIcon className="size-4" />
                      <span className="text-[13px]">Delete all</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ) : null}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
          <SidebarHistory searchQuery={query} user={user} />
          {/* 会话多了超出可视高度时的常驻自绘滚动条(原生条是悬浮式,静止不可见) */}
          <SidebarScrollbar />
        </SidebarContent>
        <SidebarFooter className="sticky bottom-0 z-10 border-t border-sidebar-border bg-sidebar pt-2 pb-3 shadow-[0_-8px_18px_-14px_rgba(0,0,0,0.35)]">
          {user ? <SidebarSettings /> : null}
        </SidebarFooter>
        <SidebarRail />
      </Sidebar>

      {isMobile ? null : (
        <Dialog onOpenChange={handleNewChatDialogChange} open={newChatOpen}>
          <DialogContent
            className="sm:max-w-sm"
            data-testid="pc-new-chat-dialog"
          >
            <DialogHeader>
              <DialogTitle>新建会话</DialogTitle>
              <DialogDescription>
                先给会话起个名字,最多 10 个字;创建后不会被首条消息自动改名。
              </DialogDescription>
            </DialogHeader>
            <form
              className="flex flex-col gap-3"
              onSubmit={handleCreateNamedChat}
            >
              <div className="relative">
                <Input
                  aria-label="会话名称"
                  autoFocus
                  className="rounded-lg pr-12"
                  data-testid="pc-session-name-input"
                  maxLength={10}
                  onChange={handleTitleChange}
                  placeholder="例如:转账与账单整理"
                  value={newChatTitle}
                />
                <span className="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-[10px] text-muted-foreground">
                  {newChatTitle.length}/10
                </span>
              </div>
              {createError ? (
                <p className="rounded-md bg-red-50 px-2.5 py-1.5 text-[11px] text-red-600 dark:bg-red-950/40 dark:text-red-300">
                  {createError}
                </p>
              ) : null}
              <div className="flex justify-end gap-2">
                <Button onClick={cancelNewChat} type="button" variant="ghost">
                  取消
                </Button>
                <Button
                  data-testid="pc-session-create-button"
                  disabled={!newChatTitle.trim() || creating}
                  type="submit"
                >
                  {creating ? "创建中…" : "创建并进入"}
                </Button>
              </div>
            </form>
          </DialogContent>
        </Dialog>
      )}

      <AlertDialog
        onOpenChange={setShowDeleteAllDialog}
        open={showDeleteAllDialog}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete all chats?</AlertDialogTitle>
            <AlertDialogDescription>
              This action cannot be undone. This will permanently delete all
              your chats and remove them from our servers.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction onClick={handleDeleteAll}>
              Delete All
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
