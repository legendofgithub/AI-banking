"use client";

/*
 * 左下角设置入口(2026-10-20 收编):原侧栏底部主题切换与右上角登录入口
 * 都并到这里——账号区(登录/注册/退出登录) + 界面语言/主题/缩放三组
 * 二级菜单(设计参照用户上传的面板稿:图标 + 文字 + 右箭头)。
 * 登录态判定走 bank-token(见 hooks/use-bank-auth)。
 *
 * 2026-10-28 复用化:菜单内容抽成 SettingsMenu(与触发器解绑),三处共用:
 * - 侧栏底部(SidebarSettings,桌面聊天)
 * - 门户页顶栏账号下拉(app/portal,需 layout 挂载同款弹窗组)
 * - 手机聊天顶栏(chat-header 移动端动作区)
 * 注意:充钱/绑卡/记忆/人设四项走 window 事件,弹窗必须已挂载
 * (chat layout 与 portal layout 都挂了);大模型管理依赖 ChatModelProvider。
 */

import {
  BanknoteIcon,
  BotIcon,
  BrainIcon,
  CheckIcon,
  ChevronDownIcon,
  CreditCardIcon,
  GlobeIcon,
  LogOutIcon,
  MonitorIcon,
  MoonIcon,
  SettingsIcon,
  SparklesIcon,
  SunIcon,
  UserIcon,
  ZoomInIcon,
} from "lucide-react";
import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
} from "@/components/ui/sidebar";
import { useBankAuth } from "@/hooks/use-bank-auth";
import { useChatModel } from "@/hooks/use-chat-model";

const ZOOM_KEY = "ui-zoom";
const ZOOM_OPTIONS = [
  { key: "small", label: "小", value: "0.9" },
  { key: "default", label: "标准", value: "1" },
  { key: "large", label: "大", value: "1.1" },
] as const;
type ZoomKey = (typeof ZOOM_OPTIONS)[number]["key"];

const THEME_LABELS: Record<string, string> = {
  dark: "深色",
  light: "浅色",
  system: "跟随系统",
};

/** 二级菜单行:图标 + 名称 + 右侧当前值(未挂载/未设置时留空防 hydration 错位) */
function SettingRow({
  icon,
  label,
  value,
}: {
  icon: React.ReactNode;
  label: string;
  value: string;
}) {
  return (
    <>
      {icon}
      <span className="flex flex-1 items-center justify-between pr-1">
        <span>{label}</span>
        <span className="text-xs text-muted-foreground">{value}</span>
      </span>
    </>
  );
}

/** 设置菜单内容(不含触发器):账号区 + 业务入口 + 模型/语言/主题/缩放。
 * onMouseEnter/onMouseLeave 供悬浮式触发器(门户头像)透传,控制开合。 */
export function SettingsMenu({
  side = "top",
  align = "start",
  onMouseEnter,
  onMouseLeave,
}: {
  side?: "top" | "bottom";
  align?: "start" | "end" | "center";
  onMouseEnter?: React.MouseEventHandler<HTMLDivElement>;
  onMouseLeave?: React.MouseEventHandler<HTMLDivElement>;
}) {
  const router = useRouter();
  const { user, logout } = useBankAuth();
  const { selectedModel, setModelSettingsOpen } = useChatModel();
  const { setTheme, theme } = useTheme();
  const [mounted, setMounted] = useState(false);
  const [zoom, setZoom] = useState<ZoomKey>("default");

  useEffect(() => {
    setMounted(true);
    const saved = window.localStorage.getItem(ZOOM_KEY) as ZoomKey | null;
    if (saved && ZOOM_OPTIONS.some((option) => option.key === saved)) {
      setZoom(saved);
    }
  }, []);

  // 缩放走 html 根元素 CSS zoom,整页等比缩放(含弹窗/悬浮球),本地持久化
  useEffect(() => {
    if (!mounted) {
      return;
    }
    const option = ZOOM_OPTIONS.find((item) => item.key === zoom);
    if (option) {
      document.documentElement.style.setProperty("zoom", option.value);
    }
  }, [mounted, zoom]);

  const handleZoomChange = useCallback((key: string) => {
    setZoom(key as ZoomKey);
    window.localStorage.setItem(ZOOM_KEY, key);
  }, []);

  const handleLogout = useCallback(() => {
    logout();
  }, [logout]);

  const handleLogin = useCallback(() => {
    router.push("/login");
  }, [router]);

  const handleRegister = useCallback(() => {
    router.push("/register");
  }, [router]);

  const handleOpenModelSettings = useCallback(() => {
    setModelSettingsOpen(true);
  }, [setModelSettingsOpen]);

  const handleOpenDeposit = useCallback(() => {
    // 弹窗挂在 chat layout / portal layout,侧栏收起/移动端时也能打开
    window.dispatchEvent(new Event("open-deposit"));
  }, []);

  const handleOpenBoundCards = useCallback(() => {
    window.dispatchEvent(new Event("open-bound-cards"));
  }, []);

  const handleOpenMemory = useCallback(() => {
    // Agent 记住了你什么:检阅弹窗(挂在 chat/portal layout,与充钱/绑卡同款链路)
    window.dispatchEvent(new Event("open-memory"));
  }, []);

  const handleOpenPersona = useCallback(() => {
    // 给 Agent 设人设(性格/说话方式/温度):编辑弹窗同款链路
    window.dispatchEvent(new Event("open-persona"));
  }, []);

  const nickname = user?.nickname?.trim() || "";
  const themeValue = mounted ? (theme ?? "system") : "";
  const zoomValue =
    ZOOM_OPTIONS.find((option) => option.key === zoom)?.label ?? "";

  return (
    <DropdownMenuContent
      align={align}
      className="w-64 rounded-lg border border-border/60 bg-card/95 backdrop-blur-xl shadow-[var(--shadow-float)]"
      data-testid="settings-menu"
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      side={side}
    >
      {/* 账号区:登录/登出全站唯一入口(右上角胶囊已移除) */}
      <div className="flex items-center gap-2.5 px-3 py-2.5">
        {user ? (
          <div className="brand-surface flex size-8 shrink-0 items-center justify-center rounded-full text-[13px] font-semibold text-white shadow-[0_4px_12px_-6px_rgba(211,46,25,0.55)]">
            {nickname ? nickname[0] : "客"}
          </div>
        ) : (
          <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-sidebar-foreground/10">
            <UserIcon className="size-4 text-muted-foreground" />
          </div>
        )}
        <div className="min-w-0 flex-1">
          <div className="truncate text-[13px] font-medium">
            {user ? nickname || "已登录用户" : "未登录"}
          </div>
          <div className="text-[11px] text-muted-foreground">
            {user ? "网银账号" : "登录后体验转账、订阅等场景"}
          </div>
        </div>
      </div>
      {user ? (
        <>
          <DropdownMenuItem
            data-testid="settings-deposit"
            onSelect={handleOpenDeposit}
          >
            <BanknoteIcon />
            充钱
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="settings-bound-cards"
            onSelect={handleOpenBoundCards}
          >
            <CreditCardIcon />
            绑定银行卡
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="settings-persona"
            onSelect={handleOpenPersona}
          >
            <SparklesIcon />
            人设
          </DropdownMenuItem>
          <DropdownMenuItem
            data-testid="settings-memory"
            onSelect={handleOpenMemory}
          >
            <BrainIcon />
            Agent 记忆
          </DropdownMenuItem>
          <DropdownMenuItem
            className="text-red-500 focus:text-red-500"
            data-testid="settings-logout"
            onSelect={handleLogout}
          >
            <LogOutIcon />
            退出登录
          </DropdownMenuItem>
        </>
      ) : (
        <div className="flex gap-2 px-3 pb-2">
          <Button
            className="h-7 flex-1 rounded-lg bg-brand-red-solid text-white shadow-[0_6px_14px_-8px_rgba(211,46,25,0.7)] hover:bg-brand-red-deep"
            data-testid="settings-login"
            onClick={handleLogin}
            size="sm"
            type="button"
          >
            登录
          </Button>
          <Button
            className="h-7 flex-1 rounded-lg"
            data-testid="settings-register"
            onClick={handleRegister}
            size="sm"
            type="button"
            variant="outline"
          >
            注册
          </Button>
        </div>
      )}
      <DropdownMenuSeparator />
      {/* 大模型管理:供应商连接 + 模型清单统一在设置弹窗管理(2026-10-21) */}
      <DropdownMenuItem
        data-testid="settings-model"
        onSelect={handleOpenModelSettings}
      >
        <SettingRow
          icon={<BotIcon />}
          label="大模型管理"
          value={mounted ? selectedModel.name : ""}
        />
      </DropdownMenuItem>
      <DropdownMenuSeparator />
      {/* 界面语言:当前仅中文,英文留位不假开 */}
      <DropdownMenuSub>
        <DropdownMenuSubTrigger data-testid="settings-language">
          <SettingRow
            icon={<GlobeIcon />}
            label="界面语言"
            value={mounted ? "简体中文" : ""}
          />
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent>
          <DropdownMenuRadioGroup value="zh">
            <DropdownMenuRadioItem value="zh">简体中文</DropdownMenuRadioItem>
          </DropdownMenuRadioGroup>
          <DropdownMenuItem disabled>English(建设中)</DropdownMenuItem>
        </DropdownMenuSubContent>
      </DropdownMenuSub>
      {/* 界面主题:原左下角主题切换收编于此 */}
      <DropdownMenuSub>
        <DropdownMenuSubTrigger data-testid="settings-theme">
          <SettingRow
            icon={<MoonIcon />}
            label="界面主题"
            value={mounted ? (THEME_LABELS[themeValue] ?? "") : ""}
          />
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent>
          <DropdownMenuRadioGroup
            onValueChange={setTheme}
            value={themeValue || undefined}
          >
            <DropdownMenuRadioItem
              data-testid="settings-theme-light"
              value="light"
            >
              <SunIcon />
              浅色
            </DropdownMenuRadioItem>
            <DropdownMenuRadioItem
              data-testid="settings-theme-dark"
              value="dark"
            >
              <MoonIcon />
              深色
            </DropdownMenuRadioItem>
            <DropdownMenuRadioItem
              data-testid="settings-theme-system"
              value="system"
            >
              <MonitorIcon />
              跟随系统
            </DropdownMenuRadioItem>
          </DropdownMenuRadioGroup>
        </DropdownMenuSubContent>
      </DropdownMenuSub>
      {/* 界面缩放:整页等比(CSS zoom),演示投屏时切大字号很常用 */}
      <DropdownMenuSub>
        <DropdownMenuSubTrigger data-testid="settings-zoom">
          <SettingRow
            icon={<ZoomInIcon />}
            label="界面缩放"
            value={mounted ? zoomValue : ""}
          />
        </DropdownMenuSubTrigger>
        <DropdownMenuSubContent>
          <DropdownMenuRadioGroup
            onValueChange={handleZoomChange}
            value={zoom}
          >
            <DropdownMenuRadioItem
              data-testid="settings-zoom-small"
              value="small"
            >
              小
            </DropdownMenuRadioItem>
            <DropdownMenuRadioItem
              data-testid="settings-zoom-default"
              value="default"
            >
              标准
            </DropdownMenuRadioItem>
            <DropdownMenuRadioItem
              data-testid="settings-zoom-large"
              value="large"
            >
              大
            </DropdownMenuRadioItem>
          </DropdownMenuRadioGroup>
        </DropdownMenuSubContent>
      </DropdownMenuSub>
    </DropdownMenuContent>
  );
}

/** 独立触发器:门户顶栏/手机聊天顶栏用,菜单从下方弹出;左下角触发器用 align="start" 向右展开。 */
export function SettingsDropdownButton({
  "aria-label": ariaLabel = "设置",
  align = "end",
  children,
  className,
}: {
  "aria-label"?: string;
  align?: "start" | "end" | "center";
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={ariaLabel}
          className={className}
          size="icon-sm"
          type="button"
          variant="ghost"
        >
          {children}
        </Button>
      </DropdownMenuTrigger>
      <SettingsMenu align={align} side="bottom" />
    </DropdownMenu>
  );
}

export function SidebarSettings() {
  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton
              aria-label="设置"
              className="h-8 rounded-lg bg-transparent px-2 text-sidebar-foreground/70 transition-colors duration-150 hover:text-sidebar-foreground data-open:bg-sidebar-accent data-open:text-sidebar-accent-foreground"
              data-testid="settings-button"
            >
              <SettingsIcon className="size-4" />
              <span className="text-[13px]">设置</span>
            </SidebarMenuButton>
          </DropdownMenuTrigger>
          <SettingsMenu side="top" />
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  );
}

/* ===== 手机端设置弹窗 =====
 * 下拉版在手机窄屏上有硬伤:菜单 w-64 贴边弹出,语言/主题/缩放还是二级
 * 子菜单,直接飞出屏幕。移动端改为页面中央 Dialog(与充钱/绑卡/人设同款
 * 范式),三个界面偏好做成行内展开,不再嵌套任何浮层。
 * 桌面不受影响:侧栏/门户仍用上面的下拉版。 */

function MobileSection({
  children,
  icon,
  label,
  testid,
  value,
}: {
  children: React.ReactNode;
  icon: React.ReactNode;
  label: string;
  testid: string;
  value: string;
}) {
  // 展开状态分区自持:允许同时展开多区,手机上省一次父级状态联动
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-xl border border-border/50">
      <button
        className="flex w-full items-center gap-2.5 rounded-xl px-3 py-2.5 text-[13px] text-foreground transition-colors hover:bg-muted/60"
        data-testid={testid}
        onClick={() => setOpen((current) => !current)}
        type="button"
      >
        {icon}
        <span className="flex-1 text-left">{label}</span>
        <span className="text-xs text-muted-foreground">{value}</span>
        <ChevronDownIcon
          className={`size-3.5 text-muted-foreground transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open ? (
        <div className="flex flex-col gap-0.5 border-t border-border/40 px-1.5 py-1.5">
          {children}
        </div>
      ) : null}
    </div>
  );
}

function MobileOption({
  checked,
  disabled,
  label,
  onSelectValue,
  testid,
  value,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onSelectValue?: (value: string) => void;
  testid: string;
  value: string;
}) {
  return (
    <button
      className={`flex items-center gap-2 rounded-lg px-2.5 py-2 text-left text-[13px] transition-colors ${
        disabled
          ? "text-muted-foreground/50"
          : checked
            ? "bg-brand-blue-soft font-medium text-foreground dark:bg-brand-blue/15"
            : "hover:bg-muted/60"
      }`}
      data-testid={testid}
      disabled={disabled}
      onClick={onSelectValue ? () => onSelectValue(value) : undefined}
      type="button"
    >
      <CheckIcon
        className={`size-3.5 text-brand-blue ${checked ? "" : "invisible"}`}
      />
      {label}
    </button>
  );
}

export function MobileSettingsDialog({
  "aria-label": ariaLabel = "设置",
  children,
  className,
}: {
  "aria-label"?: string;
  children: React.ReactNode;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        aria-label={ariaLabel}
        className={className}
        onClick={() => setOpen(true)}
        size="icon-sm"
        type="button"
        variant="ghost"
      >
        {children}
      </Button>
      <MobileSettingsDialogBody onOpenChange={setOpen} open={open} />
    </>
  );
}

/** 可控版弹窗本体:portal 顶栏头像这类自带触发样式的地方,自己渲染触发器后直接挂这一块 */
export function MobileSettingsDialogBody({
  onOpenChange,
  open,
}: {
  onOpenChange: (open: boolean) => void;
  open: boolean;
}) {
  const router = useRouter();
  const { user, logout } = useBankAuth();
  const { selectedModel, setModelSettingsOpen } = useChatModel();
  const { setTheme, theme } = useTheme();
  const [mounted, setMounted] = useState(false);
  const [zoom, setZoom] = useState<ZoomKey>("default");

  useEffect(() => {
    setMounted(true);
    const saved = window.localStorage.getItem(ZOOM_KEY) as ZoomKey | null;
    if (saved && ZOOM_OPTIONS.some((option) => option.key === saved)) {
      setZoom(saved);
    }
  }, []);

  // 缩放与下拉版同一份存储(ui-zoom)同一套生效方式:html 根元素 CSS zoom
  useEffect(() => {
    if (!mounted) {
      return;
    }
    const option = ZOOM_OPTIONS.find((item) => item.key === zoom);
    if (option) {
      document.documentElement.style.setProperty("zoom", option.value);
    }
  }, [mounted, zoom]);

  const handleZoom = useCallback((key: string) => {
    setZoom(key as ZoomKey);
    window.localStorage.setItem(ZOOM_KEY, key);
  }, []);

  // 各行先关弹窗再触发动作:充钱/绑卡/人设/记忆/模型走的是挂在 layout 上的
  // 弹窗(window 事件或 provider 开关),晚于本弹窗卸载也照常打开
  const handleDepositClick = useCallback(() => {
    onOpenChange(false);
    window.dispatchEvent(new Event("open-deposit"));
  }, [onOpenChange]);
  const handleBoundCardsClick = useCallback(() => {
    onOpenChange(false);
    window.dispatchEvent(new Event("open-bound-cards"));
  }, [onOpenChange]);
  const handlePersonaClick = useCallback(() => {
    onOpenChange(false);
    window.dispatchEvent(new Event("open-persona"));
  }, [onOpenChange]);
  const handleMemoryClick = useCallback(() => {
    onOpenChange(false);
    window.dispatchEvent(new Event("open-memory"));
  }, [onOpenChange]);
  const handleLogoutClick = useCallback(() => {
    onOpenChange(false);
    logout();
  }, [logout, onOpenChange]);
  const handleLoginClick = useCallback(() => {
    onOpenChange(false);
    router.push("/login");
  }, [onOpenChange, router]);
  const handleRegisterClick = useCallback(() => {
    onOpenChange(false);
    router.push("/register");
  }, [onOpenChange, router]);
  const handleModelClick = useCallback(() => {
    onOpenChange(false);
    setModelSettingsOpen(true);
  }, [onOpenChange, setModelSettingsOpen]);

  const nickname = user?.nickname?.trim() || "";
  const themeValue = mounted ? (theme ?? "system") : "";
  const zoomLabel =
    ZOOM_OPTIONS.find((option) => option.key === zoom)?.label ?? "";

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="max-h-[85vh] w-[92vw] max-w-sm overflow-y-auto rounded-2xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-base">
            <SettingsIcon className="size-4 text-brand-blue" />
            设置
          </DialogTitle>
          <DialogDescription className="text-xs">
            账号、大模型与界面偏好
          </DialogDescription>
        </DialogHeader>

        <div className="flex flex-col gap-1.5">
          {/* 账号区:与下拉版同源(bank-token 登录态) */}
          <div className="flex items-center gap-2.5 rounded-xl bg-muted/50 px-3 py-2.5">
            {user ? (
              <div className="brand-surface flex size-8 shrink-0 items-center justify-center rounded-full text-[13px] font-semibold text-white shadow-[0_4px_12px_-6px_rgba(211,46,25,0.55)]">
                {nickname ? nickname[0] : "客"}
              </div>
            ) : (
              <div className="flex size-8 shrink-0 items-center justify-center rounded-full bg-sidebar-foreground/10">
                <UserIcon className="size-4 text-muted-foreground" />
              </div>
            )}
            <div className="min-w-0 flex-1">
              <div className="truncate text-[13px] font-medium">
                {user ? nickname || "已登录用户" : "未登录"}
              </div>
              <div className="text-[11px] text-muted-foreground">
                {user ? "网银账号" : "登录后体验转账、订阅等场景"}
              </div>
            </div>
          </div>

          {user ? (
            <>
              <button
                className="flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-[13px] text-foreground transition-colors hover:bg-muted/60"
                data-testid="settings-deposit"
                onClick={handleDepositClick}
                type="button"
              >
                <BanknoteIcon className="size-4" />
                充钱
              </button>
              <button
                className="flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-[13px] text-foreground transition-colors hover:bg-muted/60"
                data-testid="settings-bound-cards"
                onClick={handleBoundCardsClick}
                type="button"
              >
                <CreditCardIcon className="size-4" />
                绑定银行卡
              </button>
              <button
                className="flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-[13px] text-foreground transition-colors hover:bg-muted/60"
                data-testid="settings-persona"
                onClick={handlePersonaClick}
                type="button"
              >
                <SparklesIcon className="size-4" />
                人设
              </button>
              <button
                className="flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-[13px] text-foreground transition-colors hover:bg-muted/60"
                data-testid="settings-memory"
                onClick={handleMemoryClick}
                type="button"
              >
                <BrainIcon className="size-4" />
                Agent 记忆
              </button>
              <button
                className="flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-[13px] text-red-500 transition-colors hover:bg-red-500/10"
                data-testid="settings-logout"
                onClick={handleLogoutClick}
                type="button"
              >
                <LogOutIcon className="size-4" />
                退出登录
              </button>
            </>
          ) : (
            <div className="flex gap-2 px-1 py-1">
              <Button
                className="h-8 flex-1 rounded-lg bg-brand-red-solid text-white shadow-[0_6px_14px_-8px_rgba(211,46,25,0.7)] hover:bg-brand-red-deep"
                data-testid="settings-login"
                onClick={handleLoginClick}
                size="sm"
                type="button"
              >
                登录
              </Button>
              <Button
                className="h-8 flex-1 rounded-lg"
                data-testid="settings-register"
                onClick={handleRegisterClick}
                size="sm"
                type="button"
                variant="outline"
              >
                注册
              </Button>
            </div>
          )}

          <div className="my-0.5 h-px bg-border/50" />

          <button
            className="flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-[13px] text-foreground transition-colors hover:bg-muted/60"
            data-testid="settings-model"
            onClick={handleModelClick}
            type="button"
          >
            <BotIcon className="size-4" />
            <span className="flex flex-1 items-center justify-between pr-1">
              <span>大模型管理</span>
              <span className="text-xs text-muted-foreground">
                {mounted ? selectedModel.name : ""}
              </span>
            </span>
          </button>

          <MobileSection
            icon={<GlobeIcon className="size-4" />}
            label="界面语言"
            testid="settings-language"
            value={mounted ? "简体中文" : ""}
          >
            <MobileOption checked label="简体中文" testid="settings-language-zh" value="zh" />
            <MobileOption checked disabled label="English(建设中)" testid="settings-language-en" value="en" />
          </MobileSection>

          <MobileSection
            icon={<MoonIcon className="size-4" />}
            label="界面主题"
            testid="settings-theme"
            value={mounted ? (THEME_LABELS[themeValue] ?? "") : ""}
          >
            <MobileOption
              checked={themeValue === "light"}
              label="浅色"
              onSelectValue={setTheme}
              testid="settings-theme-light"
              value="light"
            />
            <MobileOption
              checked={themeValue === "dark"}
              label="深色"
              onSelectValue={setTheme}
              testid="settings-theme-dark"
              value="dark"
            />
            <MobileOption
              checked={themeValue === "system"}
              label="跟随系统"
              onSelectValue={setTheme}
              testid="settings-theme-system"
              value="system"
            />
          </MobileSection>

          <MobileSection
            icon={<ZoomInIcon className="size-4" />}
            label="界面缩放"
            testid="settings-zoom"
            value={mounted ? zoomLabel : ""}
          >
            {ZOOM_OPTIONS.map((option) => (
              <MobileOption
                checked={zoom === option.key}
                key={option.key}
                label={option.label}
                onSelectValue={handleZoom}
                testid={`settings-zoom-${option.key}`}
                value={option.key}
              />
            ))}
          </MobileSection>
        </div>
      </DialogContent>
    </Dialog>
  );
}
