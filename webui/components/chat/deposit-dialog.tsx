"use client";

/*
 * 充钱/入金弹窗（2026-10-21）：
 * 新注册用户余额为 0，演示阶段允许把资金转入本人账户。资金来源两种：
 *   1) 模拟外部账户（凭空产生演示余额，不接真实银行卡）；
 *   2) 个人信息里绑定过的银行卡（从卡的虚拟余额扣减，卡内余额是演示值）。
 * 刻意不接真实银行接口——入账不是动用户现有资金，不需要支付密码；
 * 但仍要求登录、单笔限额、幂等键防重复入账。
 *
 * 入口：左下角设置 → 充钱。
 * 通过 window 事件 open-deposit 打开本弹窗（弹窗挂在 chat layout，
 * 桌面/手机侧栏收起时也能打开）。
 */

import { BanknoteIcon } from "lucide-react";
import Link from "next/link";
import {
  type ChangeEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  bankAgentBase,
  bankAuthHeaders,
  getStoredToken,
} from "@/hooks/use-bank-auth";

type Account = {
  balance_yuan: string;
  id: number;
  name: string;
  type: string;
};

type BoundCard = {
  balance_yuan: string;
  bank_name: string;
  card_id: number;
  card_no_masked: string;
};

type AccountsResponse = {
  accounts?: Account[];
  error?: string;
};

type BoundCardsResponse = {
  cards?: BoundCard[];
};

type DepositResult = {
  account_id: number;
  amount_yuan: string;
  balance_after_yuan: string;
  card_balance_after_yuan?: string;
  card_id?: number;
  duplicate: boolean;
  source?: string;
  transaction_id: number;
  error?: string;
};

function newIdempotencyKey(): string {
  if (
    typeof crypto !== "undefined" &&
    typeof crypto.randomUUID === "function"
  ) {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export function DepositDialog() {
  const [open, setOpen] = useState(false);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [boundCards, setBoundCards] = useState<BoundCard[]>([]);
  const [accountId, setAccountId] = useState("");
  const [source, setSource] = useState("external");
  const [amount, setAmount] = useState("");
  const [note, setNote] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [needLogin, setNeedLogin] = useState(false);
  const idempotencyKeyRef = useRef("");

  const resetIdempotencyKey = useCallback(() => {
    idempotencyKeyRef.current = newIdempotencyKey();
  }, []);

  useEffect(() => {
    const openDeposit = () => setOpen(true);
    window.addEventListener("open-deposit", openDeposit);
    return () => window.removeEventListener("open-deposit", openDeposit);
  }, []);

  useEffect(() => {
    if (!open) {
      return;
    }
    setError("");
    setSuccess("");
    setAmount("");
    setNote("");
    resetIdempotencyKey();
    if (!getStoredToken()) {
      setNeedLogin(true);
      setAccounts([]);
      setBoundCards([]);
      setAccountId("");
      setSource("external");
      return;
    }
    setNeedLogin(false);
    setSource("external");
    let cancelled = false;
    (async () => {
      try {
        const [accRes, cardRes] = await Promise.all([
          fetch(`${bankAgentBase()}/api/accounts`, {
            headers: bankAuthHeaders(),
          }),
          fetch(`${bankAgentBase()}/api/bound-cards`, {
            headers: bankAuthHeaders(),
          }).catch(() => null),
        ]);
        const json = (await accRes
          .json()
          .catch(() => null)) as AccountsResponse | null;
        if (cancelled) {
          return;
        }
        if (!accRes.ok) {
          setError(json?.error ?? "账户加载失败");
          return;
        }
        const list = json?.accounts ?? [];
        setAccounts(list);
        setAccountId(list[0] ? String(list[0].id) : "");
        if (cardRes?.ok) {
          const cardJson = (await cardRes
            .json()
            .catch(() => null)) as BoundCardsResponse | null;
          if (!cancelled) {
            setBoundCards(cardJson?.cards ?? []);
          }
        } else {
          setBoundCards([]);
        }
      } catch {
        if (!cancelled) {
          setError("无法连接服务器,请稍后重试");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, resetIdempotencyKey]);

  const handleAccountChange = useCallback(
    (event: ChangeEvent<HTMLSelectElement>) => {
      setAccountId(event.target.value);
      setSuccess("");
      resetIdempotencyKey();
    },
    [resetIdempotencyKey]
  );

  const handleSourceChange = useCallback(
    (event: ChangeEvent<HTMLSelectElement>) => {
      setSource(event.target.value);
      setSuccess("");
      resetIdempotencyKey();
    },
    [resetIdempotencyKey]
  );

  const selectedCard = boundCards.find(
    (card) => source === `card:${card.card_id}`
  );
  const selectedCardId = selectedCard?.card_id;

  const handleGoBind = useCallback(() => {
    setOpen(false);
    window.dispatchEvent(new Event("open-bound-cards"));
  }, []);

  const handleAmountChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      setAmount(event.target.value);
      setSuccess("");
      resetIdempotencyKey();
    },
    [resetIdempotencyKey]
  );

  const handleNoteChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => {
      setNote(event.target.value);
      resetIdempotencyKey();
    },
    [resetIdempotencyKey]
  );

  const handleSubmit = useCallback(async () => {
    const value = amount.trim();
    if (!/^\d+(\.\d{1,2})?$/.test(value) || Number(value) <= 0) {
      setError("请输入大于 0 的金额,最多两位小数");
      return;
    }
    if (!accountId) {
      setError("请选择入账账户");
      return;
    }
    setLoading(true);
    setError("");
    setSuccess("");
    try {
      const res = await fetch(`${bankAgentBase()}/api/deposit`, {
        body: JSON.stringify({
          account_id: Number(accountId),
          amount_yuan: value,
          ...(selectedCardId ? { card_id: selectedCardId } : {}),
          idempotency_key: idempotencyKeyRef.current,
          note: note.trim(),
        }),
        headers: {
          "Content-Type": "application/json",
          ...bankAuthHeaders(),
        },
        method: "POST",
      });
      const json = (await res.json().catch(() => null)) as DepositResult | null;
      if (!res.ok || !json) {
        setError(json?.error ?? "入金失败,请稍后重试");
        return;
      }
      setAccounts((current) =>
        current.map((account) =>
          account.id === json.account_id
            ? { ...account, balance_yuan: json.balance_after_yuan }
            : account
        )
      );
      const cardId = json.card_id;
      const cardAfter = json.card_balance_after_yuan;
      if (cardId && cardAfter) {
        setBoundCards((current) =>
          current.map((card) =>
            card.card_id === cardId
              ? { ...card, balance_yuan: cardAfter }
              : card
          )
        );
      }
      const cardNote = cardAfter ? `,银行卡余额 ${cardAfter} 元` : "";
      setSuccess(
        `入金成功,当前余额 ${json.balance_after_yuan} 元${cardNote}${
          json.duplicate ? "(重复请求已忽略)" : ""
        }`
      );
      setAmount("");
      resetIdempotencyKey();
      toast.success(json.source === "card" ? "银行卡转入成功" : "充钱成功");
    } catch {
      setError("无法连接服务器,请稍后重试");
    } finally {
      setLoading(false);
    }
  }, [accountId, amount, note, resetIdempotencyKey, selectedCardId]);

  return (
    <Dialog onOpenChange={setOpen} open={open}>
      <DialogContent className="sm:max-w-md" data-testid="deposit-dialog">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <BanknoteIcon className="size-4 text-gold-ink dark:text-gold" />
            充钱/入金
          </DialogTitle>
          <DialogDescription>
            演示环境:可从模拟外部账户或个人信息中绑定的银行卡转入本人账户。
            银行卡余额是演示虚拟值,不接真实银行接口。
          </DialogDescription>
        </DialogHeader>

        {needLogin ? (
          <div className="flex flex-col items-center gap-3 py-4 text-center">
            <p className="text-[13px] text-muted-foreground">
              请先登录后再充钱。
            </p>
            <Link href="/login">
              <Button type="button">去登录</Button>
            </Link>
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-2">
              <Label htmlFor="deposit-source">资金来源</Label>
              <select
                className="h-9 w-full rounded-lg border border-input bg-input/30 px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
                data-testid="deposit-source"
                id="deposit-source"
                onChange={handleSourceChange}
                value={source}
              >
                <option value="external">模拟外部账户（演示）</option>
                {boundCards.map((card) => (
                  <option key={card.card_id} value={`card:${card.card_id}`}>
                    {card.bank_name} 尾号{card.card_no_masked.slice(-4)} ·
                    虚拟余额 {card.balance_yuan} 元
                  </option>
                ))}
              </select>
              {selectedCard ? (
                <p className="text-[11px] text-muted-foreground">
                  该卡本次可用虚拟余额{" "}
                  <span className="font-mono text-foreground/80">
                    {selectedCard.balance_yuan}
                  </span>{" "}
                  元
                </p>
              ) : null}
              {boundCards.length === 0 ? (
                <p className="text-[11px] text-muted-foreground">
                  还没有绑定银行卡?
                  <button
                    className="ml-1 text-gold-ink underline-offset-2 hover:underline dark:text-gold"
                    onClick={handleGoBind}
                    type="button"
                  >
                    去绑定
                  </button>
                </p>
              ) : null}
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="deposit-account">入账账户</Label>
              <select
                className="h-9 w-full rounded-lg border border-input bg-input/30 px-3 text-sm outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50"
                data-testid="deposit-account"
                id="deposit-account"
                onChange={handleAccountChange}
                value={accountId}
              >
                {accounts.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.name}（{account.type}）· 余额{" "}
                    {account.balance_yuan} 元
                  </option>
                ))}
              </select>
              {accounts.length === 0 ? (
                <p className="text-xs text-muted-foreground">暂无可用账户</p>
              ) : null}
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="deposit-amount">充钱金额（元）</Label>
              <Input
                data-testid="deposit-amount"
                id="deposit-amount"
                inputMode="decimal"
                onChange={handleAmountChange}
                placeholder="如 1000"
                value={amount}
              />
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="deposit-note">备注（选填）</Label>
              <Input
                id="deposit-note"
                maxLength={200}
                onChange={handleNoteChange}
                placeholder="如 体验金/演示入金"
                value={note}
              />
            </div>

            {error ? (
              <p
                className="rounded-lg bg-destructive/10 px-3 py-2 text-[13px] text-destructive"
                data-testid="deposit-error"
              >
                {error}
              </p>
            ) : null}
            {success ? (
              <p
                className="rounded-lg bg-emerald-500/10 px-3 py-2 text-[13px] text-emerald-600 dark:text-emerald-400"
                data-testid="deposit-success"
              >
                {success}
              </p>
            ) : null}

            <Button
              className="rounded-lg bg-primary text-white shadow-[0_8px_18px_-10px_rgba(30,82,165,0.7)] hover:bg-primary/90 disabled:opacity-50"
              data-testid="deposit-submit"
              disabled={loading || accounts.length === 0}
              onClick={handleSubmit}
              type="button"
            >
              {loading ? "入金中…" : "确认入金"}
            </Button>
            <p className="text-[11px] leading-relaxed text-muted-foreground">
              单笔上限 100,000
              元;模拟外部账户是演示资金,银行卡来源从虚拟余额扣减,
              都不接真实银行、不需要支付密码。
            </p>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
