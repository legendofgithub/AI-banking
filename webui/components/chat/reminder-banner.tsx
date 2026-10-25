"use client";

import type { ChangeEvent } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useActiveChat } from "@/hooks/use-active-chat";

/*
 * 到期提醒入口(跨场景联动的"系统主动找用户"半边):
 * - 挂载时与点击「到期提醒」时 fetch `${agentBase}/api/reminders`(后端触发
 *   run_due_tasks),有到期项则显示小横幅:提醒文本 + [发给助手处理];
 * - 「发给助手处理」把提醒文本作为普通消息发送(照建议按钮的发送模式:
 *   pushState 到 /chat/<id> + sendMessage),后端 '(到期提醒)' 前缀会分流进
 *   联动到期处理(l_due 逐动作闸门);
 * - 「演示快进」日期输入 + 按钮:fetch /api/reminders?as_of=所选时刻,把"今天"
 *   拨到生日前几天即可现场演示提醒触发(比赛演示专用)。
 * - 面板可拉伸可折叠(2026-10-25):底部拖拽手柄调高度(44px~半屏,写入
 *   localStorage),双击手柄或点「收起」折叠成一颗小胶囊,给聊天区腾地方;
 *   状态跨刷新记忆。
 * agentBase 取法照 hooks/use-active-chat.tsx:跟随页面主机,端口走
 * NEXT_PUBLIC_AGENT_API_PORT(默认 8800)。
 */

const agentBase = `${typeof window === "undefined" ? "http://127.0.0.1" : `${window.location.protocol}//${window.location.hostname}`}:${process.env.NEXT_PUBLIC_AGENT_API_PORT ?? "8800"}`;

const UI_STORE_KEY = "reminder-banner-ui";
const MIN_PANEL_H = 44;

interface ReminderItem {
  kind?: string;
  text: string;
}

function ReminderRow({
  busy,
  onSend,
  text,
}: {
  busy: boolean;
  onSend: (text: string) => void;
  text: string;
}) {
  // 每条提醒各自携带文本:行内消化成稳定引用,避免每次渲染重建 props
  const handleSend = useCallback(() => onSend(text), [onSend, text]);
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-amber-300/70 bg-gradient-to-r from-amber-50 to-yellow-50 px-3 py-2 text-[13px] shadow-[var(--shadow-card)] dark:border-amber-500/40 dark:from-amber-950/40 dark:to-yellow-950/30">
      <span className="text-amber-800 dark:text-amber-200">{text}</span>
      <button
        className="rounded-lg bg-amber-500 px-3 py-1 text-xs font-medium text-white transition hover:bg-amber-600 disabled:cursor-not-allowed disabled:opacity-50"
        disabled={busy}
        onClick={handleSend}
        type="button"
      >
        发给助手处理
      </button>
    </div>
  );
}

export function ReminderBanner() {
  const { chatId, sendMessage, status } = useActiveChat();
  const [reminders, setReminders] = useState<ReminderItem[]>([]);
  const [dismissed, setDismissed] = useState(false);
  const [fastForward, setFastForward] = useState("");
  // 面板拉伸/折叠:panelH=null 表示自适应内容高度;localStorage 记忆跨刷新
  const [panelH, setPanelH] = useState<number | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{
    startY: number;
    startH: number;
    latest: number;
  } | null>(null);
  const busy = status === "submitted" || status === "streaming";

  useEffect(() => {
    try {
      const raw = window.localStorage.getItem(UI_STORE_KEY);
      if (!raw) {
        return;
      }
      const s = JSON.parse(raw) as { h?: unknown; c?: unknown };
      if (typeof s.h === "number" && s.h >= MIN_PANEL_H) {
        setPanelH(s.h);
      }
      if (typeof s.c === "boolean") {
        setCollapsed(s.c);
      }
    } catch {
      // 坏数据当没设置
    }
  }, []);

  const persistUi = useCallback((h: number | null, c: boolean) => {
    try {
      window.localStorage.setItem(UI_STORE_KEY, JSON.stringify({ c, h }));
    } catch {
      // 隐私模式等写不进就只在本次会话生效
    }
  }, []);

  const handleCollapse = useCallback(() => {
    setCollapsed(true);
    persistUi(panelH, true);
  }, [panelH, persistUi]);

  const handleExpand = useCallback(() => {
    setCollapsed(false);
    persistUi(panelH, false);
  }, [panelH, persistUi]);

  const handleHandleDown = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (e.button !== 0) {
        return;
      }
      e.preventDefault();
      const startH = panelH ?? wrapRef.current?.offsetHeight ?? 120;
      dragRef.current = { latest: startH, startH, startY: e.clientY };
      const move = (ev: PointerEvent) => {
        const drag = dragRef.current;
        if (!drag) {
          return;
        }
        // 向下拖=面板变高;上限半屏,下限按钮行高度
        const max = Math.round(window.innerHeight * 0.5);
        drag.latest = Math.min(
          max,
          Math.max(MIN_PANEL_H, drag.startH + (ev.clientY - drag.startY))
        );
        setPanelH(drag.latest);
      };
      const up = () => {
        const drag = dragRef.current;
        dragRef.current = null;
        document.removeEventListener("pointermove", move);
        document.removeEventListener("pointerup", up);
        if (drag) {
          persistUi(drag.latest, collapsed);
        }
      };
      document.addEventListener("pointermove", move);
      document.addEventListener("pointerup", up);
    },
    [collapsed, panelH, persistUi]
  );

  const load = useCallback(async (asOf?: string) => {
    // 失败静默(load 内部兜底):提醒入口挂了不能打扰聊天主链路(后端同样保底不 500)
    const query = asOf ? `?as_of=${encodeURIComponent(asOf)}` : "";
    try {
      const res = await fetch(`${agentBase}/api/reminders${query}`);
      if (!res.ok) {
        return;
      }
      const body = await res.json();
      setReminders(Array.isArray(body?.reminders) ? body.reminders : []);
      setDismissed(false);
    } catch {
      setReminders([]);
    }
  }, []);

  useEffect(() => {
    load(); // 挂载时查一次到期提醒(load 自带错误兜底)
  }, [load]);

  const sendToAssistant = useCallback(
    (text: string) => {
      if (busy || !text) {
        return;
      }
      // 照建议按钮(suggested-actions)的发送模式:先落到 /chat/<id> 再发消息
      window.history.pushState(
        {},
        "",
        `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/chat/${chatId}`
      );
      sendMessage({ parts: [{ text, type: "text" }], role: "user" });
      setDismissed(true);
    },
    [busy, chatId, sendMessage]
  );

  const handleRefresh = useCallback(() => {
    load();
  }, [load]);

  const handleDateChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    setFastForward(e.target.value);
  }, []);

  const handleFastForward = useCallback(() => {
    // datetime-local 给 'YYYY-MM-DDTHH:MM',后端契约要 'YYYY-MM-DDTHH:MM:SS'
    if (!fastForward) {
      return;
    }
    load(fastForward.length === 16 ? `${fastForward}:00` : fastForward);
  }, [fastForward, load]);

  const handleDismiss = useCallback(() => {
    setDismissed(true);
  }, []);

  const visible = !dismissed && reminders.length > 0;

  // 折叠态:整条收成一颗小胶囊,给聊天区让位;点击恢复
  if (collapsed) {
    return (
      <div
        className="mx-auto w-full max-w-4xl px-2 pt-1 md:px-4"
        data-testid="reminder-entry"
      >
        <button
          aria-label="展开提醒与演示快进面板"
          className="rounded-full border border-border/50 bg-card/60 px-3 py-0.5 text-[11px] text-muted-foreground transition hover:bg-card hover:text-foreground"
          onClick={handleExpand}
          type="button"
        >
          🔔 提醒 / 演示快进 ⌄
        </button>
      </div>
    );
  }

  return (
    <div
      className="mx-auto w-full max-w-4xl px-2 pt-1 md:px-4"
      data-testid="reminder-entry"
    >
      <div
        className="overflow-y-auto"
        data-testid="reminder-panel"
        ref={wrapRef}
        style={panelH ? { maxHeight: `${panelH}px` } : undefined}
      >
        <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
          <button
            className="rounded-full border border-border/50 bg-card/60 px-2.5 py-0.5 transition hover:bg-card"
            onClick={handleRefresh}
            type="button"
          >
            🔔 到期提醒
          </button>
          <input
            aria-label="演示快进日期"
            className="rounded-full border border-border/50 bg-card/60 px-2.5 py-0.5 text-[11px] text-foreground"
            onChange={handleDateChange}
            type="datetime-local"
            value={fastForward}
          />
          <button
            className="rounded-full border border-dashed border-border/60 px-2.5 py-0.5 transition hover:bg-card disabled:cursor-not-allowed disabled:opacity-50"
            disabled={!fastForward}
            onClick={handleFastForward}
            title="演示用:把今天拨到所选时刻再查提醒(先在左侧选日期时间)"
            type="button"
          >
            ⏩ 演示快进
          </button>
          <button
            aria-label="收起提醒面板"
            className="ml-auto rounded-full px-2 py-0.5 text-[11px] text-muted-foreground/60 transition hover:bg-muted hover:text-muted-foreground"
            onClick={handleCollapse}
            title="收起面板(拖拽下方手柄可调高度)"
            type="button"
          >
            收起 ⌃
          </button>
        </div>

        {visible ? (
          <div className="mt-2 flex flex-col gap-1.5">
            {reminders.map((reminder) => (
              <ReminderRow
                busy={busy}
                key={reminder.text}
                onSend={sendToAssistant}
                text={reminder.text}
              />
            ))}
            <button
              className="w-fit text-xs text-muted-foreground/70 transition hover:text-muted-foreground"
              onClick={handleDismiss}
              type="button"
            >
              收起提醒
            </button>
          </div>
        ) : null}
      </div>

      {/* 拉伸手柄:向下拖变高,向上拖变矮;双击收起整个面板。
          拖拽是鼠标/触屏专属交互,键盘用户走上方「收起」按钮,故 aria-hidden */}
      <div
        aria-hidden="true"
        className="group -my-0.5 flex h-3 cursor-ns-resize items-center justify-center rounded touch-none select-none"
        data-testid="reminder-resize-handle"
        onDoubleClick={handleCollapse}
        onPointerDown={handleHandleDown}
        title="拖拽调整高度 · 双击收起面板"
      >
        <span className="h-0.5 w-8 rounded-full bg-border transition-colors group-hover:bg-muted-foreground/50" />
      </div>
    </div>
  );
}
