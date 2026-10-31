import Link from "next/link";
import { memo, useCallback } from "react";
import type { Chat } from "@/lib/db/schema";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../ui/dropdown-menu";
import {
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
} from "../ui/sidebar";
import { MoreHorizontalIcon, PencilEditIcon, TrashIcon } from "./icons";

const PureChatItem = ({
  chat,
  isActive,
  onDelete,
  onRename,
  setOpenMobile,
  snippet,
}: {
  chat: Chat;
  isActive: boolean;
  onDelete: (chatId: string) => void;
  /** 点「重命名」:父层弹对话框改标题;手动命名后标题不再随内容变化 */
  onRename: (chatId: string, currentTitle: string) => void;
  setOpenMobile: (open: boolean) => void;
  /** 内容搜索命中片段(手机端会话总览搜索时展示) */
  snippet?: string;
}) => {
  const closeMobile = useCallback(() => {
    setOpenMobile(false);
  }, [setOpenMobile]);

  const handleDelete = useCallback(() => {
    onDelete(chat.id);
  }, [chat.id, onDelete]);

  const handleRename = useCallback(() => {
    onRename(chat.id, chat.title);
  }, [chat.id, chat.title, onRename]);

  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        asChild
        className={`rounded-none text-[13px] text-sidebar-foreground/50 transition-all duration-150 hover:bg-transparent hover:text-sidebar-foreground data-active:bg-transparent data-active:font-normal data-active:text-sidebar-foreground/50 data-[active=true]:text-brand-red-deep data-[active=true]:font-medium data-[active=true]:border-b data-[active=true]:border-dashed data-[active=true]:border-brand-red/35 data-[active=true]:shadow-[inset_2px_0_0_var(--brand-red)] dark:data-[active=true]:text-brand-red ${
          snippet ? "h-auto py-1" : "h-8"
        }`}
        isActive={isActive}
      >
        <Link href={`/chat/${chat.id}`} onClick={closeMobile}>
          <span className="flex min-w-0 flex-col">
            <span className="truncate">{chat.title}</span>
            {snippet ? (
              <span className="truncate text-[10px] leading-tight text-sidebar-foreground/40">
                {snippet}
              </span>
            ) : null}
          </span>
        </Link>
      </SidebarMenuButton>

      <DropdownMenu modal={true}>
        <DropdownMenuTrigger asChild>
          <SidebarMenuAction
            className="mr-0.5 rounded-md text-sidebar-foreground/50 ring-0 transition-colors duration-150 focus-visible:ring-0 hover:text-sidebar-foreground data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground"
            showOnHover={!isActive}
          >
            <MoreHorizontalIcon />
            <span className="sr-only">More</span>
          </SidebarMenuAction>
        </DropdownMenuTrigger>

        <DropdownMenuContent align="end" side="bottom">
          <DropdownMenuItem onSelect={handleRename}>
            <PencilEditIcon size={15} />
            <span>重命名</span>
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={handleDelete} variant="destructive">
            <TrashIcon />
            <span>Delete</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </SidebarMenuItem>
  );
};

export const ChatItem = memo(PureChatItem, (prevProps, nextProps) => {
  if (prevProps.chat.id !== nextProps.chat.id) {
    return false;
  }
  if (prevProps.chat.title !== nextProps.chat.title) {
    return false;
  }
  if (prevProps.snippet !== nextProps.snippet) {
    return false;
  }
  if (prevProps.isActive !== nextProps.isActive) {
    return false;
  }
  return true;
});
