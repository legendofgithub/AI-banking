"use client";

/*
 * 两组入口共用「点击走既有会话发送链路」的处理:
 * - QUICK_TAGS:首屏「相关问题」卡的四个能力入口;
 * - FLOAT_TAGS:手机端输入框上方常驻的两张悬浮问题卡(与相关问题卡不重复)。
 *
 * 只做入口映射,不新增业务:say 必须是路由认得的关键词原句,
 * 避免标签好看但点不动(理财/账单沿用首页快捷入口的实测话术)。
 */

import { useCallback } from "react";
import { useActiveChat } from "@/hooks/use-active-chat";
import { cn } from "@/lib/utils";

export type QuickTag = {
  key: string;
  /** 展示文案(标签上写的) */
  label: string;
  /** 真正发给 agent 的话术:用路由认得的关键词原句,避免标签好看但点不动 */
  say: string;
};

export const QUICK_TAGS: QuickTag[] = [
  { key: "transfer", label: "智能转账", say: "我要转账" },
  { key: "card", label: "卡片管理", say: "卡片管理" },
  { key: "subscription", label: "订阅代扣管理", say: "订阅/代扣管理" },
  { key: "linkage", label: "跨场景联动计划", say: "联动计划" },
];

/** 输入框上方的两张悬浮问题卡:理财与账单两条主线,say 沿用首页快捷入口实测话术 */
export const FLOAT_TAGS: QuickTag[] = [
  { key: "wealth", label: "理财管理", say: "帮我看看理财产品" },
  { key: "bill", label: "账单统计分析", say: "上个月花了多少" },
];

/** 标签点击的统一处理:busy(等回复/流式输出)期间禁用,避免并发发起 */
export function useQuickTagHandler() {
  const { chatId, sendMessage, openPayeePicker, status } = useActiveChat();
  const busy = status === "submitted" || status === "streaming";

  return useCallback(
    (tag: QuickTag) => {
      if (busy) {
        return;
      }
      if (tag.key === "transfer") {
        openPayeePicker();
        return;
      }
      window.history.pushState(
        {},
        "",
        `${process.env.NEXT_PUBLIC_BASE_PATH ?? ""}/chat/${chatId}`
      );
      sendMessage({ parts: [{ text: tag.say, type: "text" }], role: "user" });
    },
    [busy, chatId, openPayeePicker, sendMessage]
  );
}

export function QuickTags() {
  const { status } = useActiveChat();
  const onTag = useQuickTagHandler();
  const busy = status === "submitted" || status === "streaming";

  return (
    <div
      className="no-scrollbar flex shrink-0 gap-1.5 overflow-x-auto px-3 pb-2"
      data-testid="quick-tags"
    >
      {FLOAT_TAGS.map((tag) => (
        <button
          className={cn(
            "flex-1 shrink-0 rounded-full border border-brand-blue/15 bg-white/80 px-2.5 py-2 text-[12.5px] leading-none text-foreground backdrop-blur-sm dark:border-white/10 dark:bg-white/5",
            "transition-colors active:bg-brand-red-soft md:hover:border-brand-red/30 md:hover:text-brand-red",
            busy && "opacity-45"
          )}
          data-testid={`quick-tag-${tag.key}`}
          disabled={busy}
          key={tag.key}
          onClick={() => onTag(tag)}
          type="button"
        >
          {tag.label}
        </button>
      ))}
    </div>
  );
}
