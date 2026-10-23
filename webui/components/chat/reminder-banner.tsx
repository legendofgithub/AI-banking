"use client";

import { useCallback, useEffect, useState } from "react";
import { useActiveChat } from "@/hooks/use-active-chat";

/*
 * 到期提醒(跨场景联动的"系统主动找用户"半边):
 * - 页面挂载时 fetch `${agentBase}/api/reminders`(后端触发 run_due_tasks),
 *   有到期项则显示小横幅:提醒文本 + [发给助手处理];
 * - 「发给助手处理」把提醒文本作为普通消息发送(照建议按钮的发送模式:
 *   pushState 到 /chat/<id> + sendMessage),后端 '(到期提醒)' 前缀会分流进
 *   联动到期处理(l_due 逐动作闸门);
 * - 顶部不再提供手动刷新/演示快进控件(2026-10-21):到期检查只在挂载时自动
 *   执行;演示时间旅行走后端 /api/reminders?as_of=… 或脚本。
 * agentBase 取法照 hooks/use-active-chat.tsx:跟随页面主机,端口走
 * NEXT_PUBLIC_AGENT_API_PORT(默认 8800)。
 */

const agentBase = `${typeof window === "undefined" ? "http://127.0.0.1" : `${window.location.protocol}//${window.location.hostname}`}:${process.env.NEXT_PUBLIC_AGENT_API_PORT ?? "8800"}`;

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
  const busy = status === "submitted" || status === "streaming";

  const load = useCallback(async () => {
    // 失败静默(load 内部兜底):提醒入口挂了不能打扰聊天主链路(后端同样保底不 500)
    try {
      const res = await fetch(`${agentBase}/api/reminders`);
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

  const handleDismiss = useCallback(() => {
    setDismissed(true);
  }, []);

  const visible = !dismissed && reminders.length > 0;
  if (!visible) {
    return null;
  }

  return (
    <div
      className="mx-auto w-full max-w-4xl px-2 pt-2 md:px-4"
      data-testid="reminder-entry"
    >
      <div className="flex flex-col gap-1.5">
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
    </div>
  );
}
