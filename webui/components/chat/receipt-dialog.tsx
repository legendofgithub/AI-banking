"use client";

/*
 * 播报即凭证:转账订单的完整审计链弹窗(2026-10-20)。
 * 「别的 AI 说话免责,我们的 AI 每句话后面跟着账本」——聊天里的转账卡
 * 点「查看凭证」,拉 GET /api/orders/{id}/receipt 拼出三表审计链:
 * 建单(状态机)→ 扣款交易(external_ref)→ 资金流水(前后余额+operator)
 * + 工具调用轨迹(风险级)。纯只读 REST,不动 SSE 契约。
 */

import { useCallback, useState } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { bankAgentBase, bankAuthHeaders } from "@/hooks/use-bank-auth";

interface ReceiptData {
  audit_trail?: { risk?: string; ts?: string; tool?: string }[];
  ledger?: {
    after_yuan?: string;
    before_yuan?: string;
    delta_yuan?: string;
    note?: string;
    operator?: string;
    ref?: string;
    tool?: string;
    ts?: string;
  } | null;
  order?: {
    amount_yuan?: string;
    created_at?: string;
    executed_at?: string;
    idempotency_key?: string;
    memo?: string;
    order_id?: number;
    scheduled_at?: string;
    status?: string;
    to_name?: string;
  };
  transaction?: {
    amount_yuan?: string;
    balance_after_yuan?: string;
    direction?: string;
    external_ref?: string;
    ts?: string;
    tx_type?: string;
  } | null;
}

const STATUS_CN: Record<string, string> = {
  cancelled: "已取消",
  executed: "已执行",
  failed: "失败",
  pending_confirm: "待确认",
  scheduled: "定时中",
};

function Stamp({ label, time }: { label: string; time?: string }) {
  return (
    <div className="flex gap-2 text-[12px]">
      <span className="shrink-0 font-medium text-gold-ink dark:text-gold">
        {label}
      </span>
      <span className="min-w-0 break-words text-muted-foreground">
        {time || "—"}
      </span>
    </div>
  );
}

export function ReceiptButton({ orderId }: { orderId: number }) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<ReceiptData | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setError("");
    setData(null);
    setOpen(true);
    try {
      const res = await fetch(
        `${bankAgentBase()}/api/orders/${orderId}/receipt`,
        { headers: bankAuthHeaders() }
      );
      const json = (await res.json()) as ReceiptData & { error?: string };
      if (!res.ok || json.error) {
        setError(json.error || `查询失败(${res.status})`);
        return;
      }
      setData(json);
    } catch {
      setError("网络异常,凭证暂时取不到");
    }
  }, [orderId]);

  const o = data?.order;
  return (
    <>
      <button
        className="mt-1 text-xs text-gold-ink underline-offset-2 transition hover:underline dark:text-gold"
        data-testid="receipt-button"
        onClick={load}
        type="button"
      >
        查看凭证(订单号 {orderId})
      </button>
      <Dialog onOpenChange={setOpen} open={open}>
        <DialogContent
          className="max-h-[75dvh] gap-0 overflow-y-auto rounded-2xl border border-border/60 bg-card p-0 ring-border/40 sm:max-w-md"
          data-testid="receipt-dialog"
        >
          <DialogHeader className="px-5 pt-4 pb-1">
            <DialogTitle className="flex items-center gap-2 text-[15px] font-semibold text-gold-ink dark:text-gold">
              <span aria-hidden>🧾</span>
              转账凭证 · 订单号 {o?.order_id ?? orderId}
              {o?.status ? (
                <span className="rounded-full bg-gold/10 px-1.5 py-0.5 text-[10px] text-gold-ink dark:bg-gold/15 dark:text-gold">
                  {STATUS_CN[o.status] ?? o.status}
                </span>
              ) : null}
            </DialogTitle>
          </DialogHeader>
          <div className="px-5 pb-5">
            {error ? (
              <div className="text-[13px] text-red-600" role="alert">
                {error}
              </div>
            ) : data ? (
              <>
                <div className="flex flex-col gap-1 text-[13px]">
                  <div className="flex justify-between gap-4">
                    <span className="text-muted-foreground">收款人</span>
                    <span className="font-medium">{o?.to_name ?? "—"}</span>
                  </div>
                  <div className="flex justify-between gap-4">
                    <span className="text-muted-foreground">金额</span>
                    <span className="font-medium">
                      ¥{o?.amount_yuan ?? "—"}
                    </span>
                  </div>
                  {o?.memo ? (
                    <div className="flex justify-between gap-4">
                      <span className="text-muted-foreground">备注</span>
                      <span className="min-w-0 break-words text-right">
                        {o.memo}
                      </span>
                    </div>
                  ) : null}
                </div>
                <div className="mt-3 border-t border-border/60 pt-2">
                  <div className="mb-1 text-xs font-medium text-foreground">
                    审计链(时间序)
                  </div>
                  <div className="flex flex-col gap-1">
                    <Stamp label="① 建单" time={o?.created_at} />
                    {data.transaction ? (
                      <Stamp
                        label="② 扣款落账"
                        time={`${data.transaction.ts} · 余额 ${data.transaction.balance_after_yuan} 元`}
                      />
                    ) : (
                      <Stamp label="② 扣款落账" time="未发生(未执行)" />
                    )}
                    {data.ledger ? (
                      <Stamp
                        label="③ 资金流水"
                        time={`${data.ledger.before_yuan} → ${data.ledger.after_yuan} 元 · 操作方 ${data.ledger.operator} · ${data.ledger.tool}`}
                      />
                    ) : null}
                  </div>
                </div>
                {(data.audit_trail?.length ?? 0) > 0 ? (
                  <div className="mt-3 border-t border-border/60 pt-2">
                    <div className="mb-1 text-xs font-medium text-foreground">
                      工具调用轨迹
                    </div>
                    <div className="flex flex-col gap-0.5">
                      {data.audit_trail?.map((a, i) => (
                        <div
                          className="flex items-center justify-between gap-3 text-[11px] text-muted-foreground"
                          key={`${a.ts}-${a.tool}-${i}`}
                        >
                          <span className="min-w-0 truncate">
                            {a.ts} · {a.tool}
                          </span>
                          <span className="shrink-0 rounded bg-amber-100/70 px-1 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                            {a.risk}
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                ) : null}
                {o?.idempotency_key ? (
                  <div className="mt-3 border-t border-border/60 pt-2 text-[11px] break-all text-muted-foreground">
                    幂等键 {o.idempotency_key}(同指令重发不二次扣款)
                  </div>
                ) : null}
              </>
            ) : (
              <div className="text-[13px] text-muted-foreground">
                正在调取审计链…
              </div>
            )}
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}
