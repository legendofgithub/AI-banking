"use client";

/*
 * 悬空转账强制提醒(右下角弹窗,无法关闭):
 * - 登录用户名下有 pending_confirm/scheduled 转账单时,右下角常驻提醒卡,
 *   没有关闭按钮——按设计,悬空资金必须被处理(确认/取消),提醒不许被划掉;
 * - 单子确认完(对话里或落地页上)列表清空,弹窗自动消失;
 * - 数据源 GET ${agentBase}/api/pending-orders(带登录 token);挂载时与
 *   每轮对话结束(status 回到 ready)各查一次;
 * - 右侧「去确认」按钮跳 /pending-transfers 落地页(输支付密码执行)。
 */

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { useActiveChat } from "@/hooks/use-active-chat";
import { bankAgentBase, useBankAuth } from "@/hooks/use-bank-auth";

interface PendingOrder {
  amount_yuan: string;
  created_at: string;
  memo: string;
  order_id: number;
  scheduled_at: string;
  status: string;
  to_name: string;
}

export function PendingTransferToast() {
  const router = useRouter();
  const { ready, token, user } = useBankAuth();
  const { status } = useActiveChat();
  const [orders, setOrders] = useState<PendingOrder[]>([]);
  const askedRef = useRef("");

  const load = useCallback(async () => {
    if (!token) {
      setOrders([]);
      return;
    }
    try {
      const res = await fetch(
        `${bankAgentBase()}/api/pending-orders?token=${encodeURIComponent(token)}`
      );
      const json = (await res.json().catch(() => null)) as {
        orders?: PendingOrder[];
      } | null;
      setOrders(json?.orders ?? []);
    } catch {
      // 网络异常保持现状(下次状态变化再查),不打扰用户
    }
  }, [token]);

  // 挂载(登录态就绪)后查一次;之后每轮对话结束(status 回 ready)复查,
  // 建单停闸门 → 弹窗立即出现;确认执行 → 弹窗立即消失
  useEffect(() => {
    if (!ready) {
      return;
    }
    const key = `${token ?? ""}|${user?.user_id ?? ""}`;
    if (askedRef.current === key && status !== "ready") {
      return;
    }
    askedRef.current = key;
    load();
  }, [load, ready, status, token, user?.user_id]);

  const goConfirm = useCallback(
    () => router.push("/pending-transfers"),
    [router]
  );

  const pending = orders.filter((o) => o.status === "pending_confirm");
  const scheduled = orders.filter((o) => o.status === "scheduled");
  if (pending.length + scheduled.length === 0) {
    return null;
  }

  return (
    <div
      aria-live="assertive"
      className="fixed right-4 bottom-4 z-50 w-[min(92vw,360px)] rounded-2xl border border-amber-300/80 bg-gradient-to-br from-amber-50 to-yellow-50 p-4 shadow-[var(--shadow-float)] dark:border-amber-500/50 dark:from-amber-950/60 dark:to-yellow-950/40"
      role="alert"
    >
      <div className="mb-2 flex items-center justify-between gap-3">
        <div className="flex items-center gap-1.5 text-[13px] font-semibold text-amber-700 dark:text-amber-300">
          <span aria-hidden>⚠️</span>
          {user?.nickname
            ? `${user.nickname},你有 ${pending.length} 笔转账未确认`
            : `你有 ${pending.length} 笔转账未确认`}
        </div>
        <button
          className="shrink-0 rounded-lg bg-amber-500 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-amber-600"
          onClick={goConfirm}
          type="button"
        >
          去确认 →
        </button>
      </div>
      <div className="flex flex-col gap-1.5">
        {pending.slice(0, 3).map((o) => (
          <div
            className="flex items-center justify-between gap-3 rounded-lg bg-white/70 px-2.5 py-1.5 text-[12px] dark:bg-amber-900/30"
            key={o.order_id}
          >
            <span className="min-w-0 truncate text-foreground">
              给 {o.to_name}
              {o.memo ? ` · ${o.memo}` : ""}
            </span>
            <span className="shrink-0 font-semibold text-foreground">
              ¥{o.amount_yuan}
            </span>
          </div>
        ))}
        {scheduled.length > 0 ? (
          <div className="px-2.5 text-[11px] text-muted-foreground">
            另有 {scheduled.length} 笔定时转账,到期后会提醒确认
          </div>
        ) : null}
        {pending.length > 3 ? (
          <div className="px-2.5 text-[11px] text-muted-foreground">
            …等共 {pending.length} 笔,点「去确认」查看全部
          </div>
        ) : null}
      </div>
      <div className="mt-2 text-[11px] text-amber-700/80 dark:text-amber-300/80">
        动钱等你亲自确认——去确认需输入支付密码,提醒不可关闭
      </div>
    </div>
  );
}
