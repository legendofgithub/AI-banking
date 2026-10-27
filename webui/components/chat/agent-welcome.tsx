"use client";

/*
 * 空会话首屏的欢迎块:对齐公开的客服会话范式——
 * 左侧圆形头像 + 服务名 + 白色气泡 + 下方「相关问题」卡。
 *
 * 纯表达层:气泡里只写产品真实具备的能力(建单/确认/支付密码/审计留痕),
 * 四个问题入口复用 QUICK_TAGS,与输入框上方的常驻标签同一份清单。
 */

import { HelpCircleIcon, Landmark } from "lucide-react";
import { QUICK_TAGS, useQuickTagHandler } from "./quick-tags";

export function AgentWelcome() {
  const onQuickTag = useQuickTagHandler();

  return (
    <div className="flex items-start gap-2.5" data-testid="agent-welcome">
      <div className="brand-surface mt-[18px] flex size-9 shrink-0 items-center justify-center rounded-full text-white shadow-[0_8px_20px_-10px_rgba(211,46,25,0.55)] ring-2 ring-white dark:ring-background">
        <Landmark size={16} />
      </div>

      <div className="min-w-0 flex-1">
        <div className="mb-1 text-[11px] leading-none text-muted-foreground">
          智能客服
        </div>

        <div className="rounded-2xl rounded-tl-md border border-brand-blue/10 bg-card px-3.5 py-3 text-[13.5px] leading-[1.8] text-foreground shadow-[0_1px_2px_rgba(16,24,40,0.04)]">
          <p>
            您好，我是智能客服小小微，可以解答转账、卡片管理、订阅代扣、跨场景联动这几类问题。
          </p>
          <p className="mt-2">
            我是 AI Banking Agent：你把要办的事说清楚，我负责判断、建单、执行和播报。凡是动钱的操作，都先请你确认；付款要输支付密码；每一步都有审计留痕。AI 处理常规，人负责例外。
          </p>
          <p className="mt-2">您是否要问以下问题：</p>
        </div>

        {/* 相关问题:红色标题 + 暖白渐变底,四项与常驻标签同源。
            桌面端隐藏——桌面下方本就是六格建议卡片,再来一张卡是同义重复。 */}
        <div
          className="brand-surface-soft gradient-hairline mt-2.5 overflow-hidden rounded-2xl border border-transparent md:hidden"
          data-testid="suggested-actions"
        >
          <div className="flex items-center justify-between px-3.5 pt-2.5 pb-1">
            <span className="font-semibold text-[15px] text-ink dark:text-brand-blue">相关问题</span>
            <span className="flex size-6 items-center justify-center rounded-full bg-brand-red-soft text-brand-red">
              <HelpCircleIcon className="size-3.5" />
            </span>
          </div>
          <div className="px-1.5 pb-1.5">
            {QUICK_TAGS.map((tag) => (
              <button
                className="block w-full rounded-xl px-2 py-2.5 text-left text-[13px] text-foreground leading-snug transition-colors hover:bg-white/70 active:bg-white/80"
                key={tag.key}
                onClick={() => onQuickTag(tag)}
                type="button"
              >
                {tag.label}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
