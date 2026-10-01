"use client";

/*
 * 订阅/代扣场景 · 纯前端演示页(假数据,不连 8800)。
 * 赛题六场景的最后一个:按「前端先行」流程,先用真实组件+模拟数据
 * 验证交互形态(总览卡 → 取消确认卡 → 支付密码 → 完成),用户过目后
 * 再做配套后台(MCP 订阅工具 + 编排意图),届时首页场景卡改回真实
 * 发消息、本页删除。卡片组件即最终资产(bank-parts.tsx),不是一次性 Demo。
 */

import Link from "next/link";
import { type ReactNode, useCallback, useState } from "react";
import {
  SubscriptionCancelCard,
  type SubscriptionItem,
  SubscriptionListCard,
} from "@/components/chat/bank-parts";

const DEMO_ITEMS: SubscriptionItem[] = [
  {
    amount_yuan: "30.00",
    annual_yuan: "360",
    category: "视频会员",
    merchant_name: "腾讯视频VIP",
    next_charge_date: "2026-11-02",
    note: "近 3 个月从 25 元涨到 30 元,涨幅 20%",
    period_text: "月",
  },
  {
    amount_yuan: "12.80",
    annual_yuan: "153.60",
    category: "音乐",
    merchant_name: "网易云音乐",
    next_charge_date: "2026-10-15",
    period_text: "月",
  },
  {
    amount_yuan: "21.00",
    annual_yuan: "252",
    category: "云存储",
    merchant_name: "iCloud 50G",
    next_charge_date: "2026-10-08",
    period_text: "月",
  },
  {
    amount_yuan: "128.00",
    annual_yuan: "1,536",
    category: "通讯",
    merchant_name: "中国移动套餐",
    next_charge_date: "2026-10-03",
    period_text: "月",
  },
];

const DEMO_LIST = {
  annual_total_yuan: "2,301.60",
  hint: "以上为自动识别的周期性扣费,点「取消代扣」可随时终止协议",
  items: DEMO_ITEMS,
  monthly_total_yuan: "191.80",
};

function Bubble({
  children,
  side,
}: {
  children: ReactNode;
  side: "user" | "ai";
}) {
  return (
    <div
      className={
        side === "user" ? "flex justify-end" : "flex flex-col items-start gap-2"
      }
    >
      <div
        className={
          side === "user"
            ? "max-w-[80%] rounded-2xl bg-amber-500/90 px-3.5 py-2 text-[13px] text-white"
            : "max-w-[85%] rounded-2xl border border-border/50 bg-card px-3.5 py-2 text-[13px] text-foreground"
        }
      >
        {children}
      </div>
    </div>
  );
}

/* 演示状态机:列表 → 选定某项进入取消确认 → 密码/取消后出结果 */
type DemoStage =
  | { phase: "confirm"; item: SubscriptionItem }
  | { phase: "done"; item: SubscriptionItem; ok: boolean }
  | { phase: "list" };

export default function SubscriptionPreviewPage() {
  const [stage, setStage] = useState<DemoStage>({ phase: "list" });

  const handlePickCancel = useCallback(
    (item: SubscriptionItem) => setStage({ item, phase: "confirm" }),
    []
  );
  const handleResolve = useCallback((answer: string) => {
    setStage((s) =>
      s.phase === "confirm"
        ? { item: s.item, ok: answer !== "cancel", phase: "done" }
        : s
    );
  }, []);

  return (
    <div className="mx-auto flex min-h-dvh max-w-2xl flex-col gap-4 p-6">
      <div className="rounded-xl border border-blue-200/70 bg-blue-50/60 px-4 py-2.5 text-[13px] text-blue-700 dark:border-blue-500/30 dark:bg-blue-950/30 dark:text-blue-300">
        🔧 前端演示 · 模拟数据 —— 配套后台(订阅工具+编排意图)接入后,
        这里将变成真实对话流,首页「订阅/代扣管理」卡恢复直发消息。
      </div>

      <div>
        <h1 className="text-xl font-semibold tracking-tight">
          订阅/代扣管理 · 场景预览
        </h1>
        <p className="mt-1 text-[13px] text-muted-foreground">
          赛题六大场景的最后一块:自动识别订阅扣费 → 续费提醒 → 一键取消。
          下面用演示账号「陈明」的订阅数据走一遍完整交互。
        </p>
      </div>

      <div className="flex flex-col gap-4 rounded-2xl border border-border/40 bg-sidebar/40 p-4">
        <Bubble side="user">帮我看看订阅都在花哪些钱,有没有涨价的</Bubble>

        <Bubble side="ai">
          帮你盘点了 4 笔周期性扣费,本月合计 ¥191.80、年化 ¥2,301.60。
          其中腾讯视频近 3 个月从 25 元悄悄涨到了 30 元:
        </Bubble>
        <SubscriptionListCard
          data={DEMO_LIST}
          onPickCancel={handlePickCancel}
        />

        {stage.phase === "confirm" ? (
          <>
            <Bubble side="ai">
              确认要取消「{stage.item.merchant_name}」的自动扣费吗?
              为防止误操作,请输入支付密码:
            </Bubble>
            <SubscriptionCancelCard
              data={{
                ...stage.item,
                confirm_hint: "演示环境:任意 6 位数字即可通过",
                pay_required: true,
              }}
              onResolve={handleResolve}
            />
          </>
        ) : null}

        {stage.phase === "done" ? (
          stage.ok ? (
            <Bubble side="ai">
              ✅ 已取消「{stage.item.merchant_name}」的自动扣费,协议即刻终止,
              预计每年省下 ¥{stage.item.annual_yuan}。已同步记录到你的账户变动。
            </Bubble>
          ) : (
            <Bubble side="ai">
              好的,已保留「{stage.item.merchant_name}」的自动扣费,不做任何变更。
            </Bubble>
          )
        ) : null}
      </div>

      <div className="text-[12px] text-muted-foreground">
        字段结构与后端 subscriptions 表对齐(商户/分类/周期金额/下次扣费/年化),
        配套后台只需把假数据换成真实工具返回。{" "}
        <Link
          className="underline underline-offset-4 hover:text-foreground"
          href="/"
        >
          返回首页
        </Link>
      </div>
    </div>
  );
}
