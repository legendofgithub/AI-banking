"use client";

/*
 * 四个高频能力的常驻入口:手机端挂在输入框上方一排胶囊,
 * 首屏「相关问题」卡复用同一份清单(QUICK_TAGS),两处文案不会走散。
 *
 * 只做入口映射,不新增业务:点击后仍走既有会话发送链路;
 * 「智能转账」与建议胶囊同路——先弹收款人选择器,再建单 → 确认 → 支付密码。
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
      {QUICK_TAGS.map((tag) => (
        <button
          className={cn(
            "shrink-0 rounded-full border border-[#E4E7EC] bg-white px-2.5 py-1.5 text-[11.5px] leading-none text-[#333A45]",
            "transition-colors active:bg-[#EFF3FA] md:hover:border-[#C9D8F5] md:hover:text-[#1E52A5]",
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
