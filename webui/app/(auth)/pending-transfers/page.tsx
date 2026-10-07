"use client";

/*
 * 悬空转账确认落地页(右下角强制提醒弹窗「去确认」跳到这里):
 * 列出当前登录用户名下待确认/定时的转账单;待确认单需输入 6 位支付密码
 * 才能执行(与对话闸门同一套安全规则,POST {agentBase}/api/orders/confirm);
 * 定时单未到期只展示。确认成功后该单转已执行并从列表消失,右下角弹窗随之消失。
 */

import Link from "next/link";
import {
  type ChangeEvent,
  type CSSProperties,
  type FormEvent,
  useCallback,
  useEffect,
  useState,
} from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  bankAgentBase,
  bankAuthHeaders,
  useBankAuth,
} from "@/hooks/use-bank-auth";

interface PendingOrder {
  amount_yuan: string;
  created_at: string;
  memo: string;
  order_id: number;
  scheduled_at: string;
  status: string;
  to_name: string;
}

function OrderRow({
  onConfirm,
  order,
  result,
}: {
  onConfirm: (order: PendingOrder, payPassword: string) => void;
  order: PendingOrder;
  result: { ok: boolean; text: string } | null;
}) {
  const [payPassword, setPayPassword] = useState("");
  const payReady = /^\d{6}$/.test(payPassword);

  const handleChange = useCallback(
    (e: ChangeEvent<HTMLInputElement>) =>
      setPayPassword(e.target.value.replace(/\D/g, "").slice(0, 6)),
    []
  );
  const handleSubmit = useCallback(
    (e: FormEvent) => {
      e.preventDefault();
      if (payReady) {
        onConfirm(order, payPassword);
      }
    },
    [onConfirm, order, payPassword, payReady]
  );

  if (order.status === "scheduled") {
    return (
      <div className="rounded-2xl border border-border/50 bg-card p-4">
        <div className="flex items-center justify-between gap-4">
          <div className="text-[14px] font-medium text-foreground">
            定时转账 · 给 {order.to_name}
          </div>
          <div className="text-base font-semibold text-foreground">
            ¥{order.amount_yuan}
          </div>
        </div>
        <div className="mt-1 text-xs text-muted-foreground">
          计划执行 {order.scheduled_at || order.created_at} ·{" "}
          {order.memo || "无备注"} —— 到期后会自动转为待确认,再来这里或让 AI
          助手提醒你确认
        </div>
      </div>
    );
  }

  return (
    <form
      className="rounded-2xl border border-amber-300/70 bg-gradient-to-br from-amber-50 to-yellow-50 p-4 dark:border-amber-500/40 dark:from-amber-950/40 dark:to-yellow-950/30"
      onSubmit={handleSubmit}
    >
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-2 text-[14px] font-semibold text-amber-700 dark:text-amber-300">
          <span aria-hidden>🏦</span>给 {order.to_name}
        </div>
        <div className="text-lg font-semibold text-foreground">
          ¥{order.amount_yuan}
        </div>
      </div>
      <div className="mt-1.5 flex flex-col gap-0.5 text-xs text-muted-foreground">
        <span>建单时间 {order.created_at}</span>
        {order.memo ? <span>备注:{order.memo}</span> : null}
      </div>
      {result?.ok ? (
        <div className="mt-3 rounded-lg bg-emerald-100/70 px-3 py-2 text-[13px] text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300">
          ✅ {result.text}
        </div>
      ) : (
        <>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <div className="flex flex-col gap-1">
              <Label htmlFor={`pay-${order.order_id}`}>支付密码</Label>
              <Input
                autoComplete="off"
                className="w-36 tracking-[0.35em]"
                id={`pay-${order.order_id}`}
                inputMode="numeric"
                maxLength={6}
                onChange={handleChange}
                placeholder="6 位数字"
                /* 支付密码不用 type=password:Chromium 会弹"保存密码/同步
                   已暂停"系统提示,比赛演示强干扰;text+text-security 同为
                   圆点遮罩且零弹窗 */
                style={{ WebkitTextSecurity: "disc" } as CSSProperties}
                type="text"
                value={payPassword}
              />
            </div>
            <Button
              className="mt-5 rounded-lg bg-amber-500 text-white hover:bg-amber-600 disabled:opacity-50"
              disabled={!payReady}
              type="submit"
            >
              确认转账
            </Button>
          </div>
          {result && !result.ok ? (
            <p className="mt-2 text-xs text-destructive">{result.text}</p>
          ) : null}
        </>
      )}
    </form>
  );
}

export default function PendingTransfersPage() {
  const { ready, token, user } = useBankAuth();
  const [orders, setOrders] = useState<PendingOrder[] | null>(null);
  const [results, setResults] = useState<
    Record<number, { ok: boolean; text: string }>
  >({});

  const load = useCallback(async () => {
    if (!token) {
      setOrders([]);
      return;
    }
    try {
      const res = await fetch(`${bankAgentBase()}/api/pending-orders`, {
        headers: bankAuthHeaders(),
      });
      const json = (await res.json().catch(() => null)) as {
        orders?: PendingOrder[];
      } | null;
      setOrders(json?.orders ?? []);
    } catch {
      setOrders([]);
    }
  }, [token]);

  useEffect(() => {
    if (ready) {
      load();
    }
  }, [load, ready]);

  const handleConfirm = useCallback(
    async (order: PendingOrder, payPassword: string) => {
      setResults((r) => ({
        ...r,
        [order.order_id]: { ok: true, text: "确认中…" },
      }));
      try {
        const res = await fetch(`${bankAgentBase()}/api/orders/confirm`, {
          body: JSON.stringify({
            order_id: order.order_id,
            pay_password: payPassword,
            token,
          }),
          headers: { "Content-Type": "application/json" },
          method: "POST",
        });
        const json = (await res.json().catch(() => null)) as {
          error?: string;
          status?: string;
        } | null;
        if (!res.ok) {
          setResults((r) => ({
            ...r,
            [order.order_id]: {
              ok: false,
              text: json?.error ?? "确认失败,请稍后重试",
            },
          }));
          return;
        }
        setResults((r) => ({
          ...r,
          [order.order_id]: {
            ok: true,
            text: `已执行:¥${order.amount_yuan} 已转给 ${order.to_name}`,
          },
        }));
        // 刷新列表(已执行单消失;右下角弹窗的下一轮查询也会随之清空)。
        // 延迟 3s:让 ✅ 成功提示先被看清楚,再收走已执行的订单卡
        setTimeout(() => load(), 3000);
      } catch {
        setResults((r) => ({
          ...r,
          [order.order_id]: { ok: false, text: "无法连接服务器,请稍后重试" },
        }));
      }
    },
    [load, token]
  );

  const pendingCount =
    orders?.filter((o) => o.status === "pending_confirm").length ?? 0;

  return (
    <>
      <h1 className="text-2xl font-semibold tracking-tight">待确认转账</h1>
      <p className="text-sm text-muted-foreground">
        {user?.nickname ? `${user.nickname},这里` : "这里"}是你名下悬空待确认的
        转账单——确认需输入支付密码,与对话中的确认卡同一套安全规则
      </p>

      <div className="mt-6 flex flex-col gap-4">
        {!ready || orders === null ? (
          <p className="text-sm text-muted-foreground">加载中…</p>
        ) : token ? (
          orders.length === 0 ? (
            <div className="rounded-xl border border-border/50 bg-card p-4 text-sm text-muted-foreground">
              {Object.keys(results).length > 0
                ? "全部处理完毕,没有待确认的转账了 ✅"
                : "当前没有待确认的转账 ✅"}
            </div>
          ) : (
            <>
              <p className="text-[13px] text-muted-foreground">
                待确认 {pendingCount} 笔
                {orders.length > pendingCount
                  ? ` · 定时 ${orders.length - pendingCount} 笔`
                  : ""}
              </p>
              {orders.map((o) => (
                <OrderRow
                  key={o.order_id}
                  onConfirm={handleConfirm}
                  order={o}
                  result={results[o.order_id] ?? null}
                />
              ))}
            </>
          )
        ) : (
          <div className="rounded-xl border border-border/50 bg-card p-4 text-sm text-muted-foreground">
            请先
            <Link
              className="mx-1 text-foreground underline underline-offset-4"
              href="/login"
            >
              登录
            </Link>
            后查看你的待确认转账
          </div>
        )}

        <p className="text-center text-[13px] text-muted-foreground">
          <Link
            className="text-foreground underline-offset-4 hover:underline"
            href="/"
          >
            返回首页
          </Link>
        </p>
      </div>
    </>
  );
}
