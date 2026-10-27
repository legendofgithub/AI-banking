"use client";

/*
 * 演示绑卡弹窗(2026-10-21):
 * 用户在个人信息/设置里录入银行名称、卡号、开户人,系统只做 16-19 位数字
 * 格式校验并掩码存储(完整卡号不入库),绑定成功即分配固定虚拟余额
 * (50,000 元)用于"充钱"演示。不校验真实卡号、不查询真实银行余额。
 *
 * 入口:设置 → 绑定银行卡。弹窗挂在 chat layout,手机侧栏收起也能打开。
 */

import { CreditCardIcon } from "lucide-react";
import Link from "next/link";
import { type ChangeEvent, useCallback, useEffect, useState } from "react";
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

type BoundCard = {
  balance_yuan: string;
  bank_name: string;
  card_id: number;
  card_no_masked: string;
  created_at: string;
  holder_name: string;
  status: string;
};

type BoundCardsResponse = {
  cards?: BoundCard[];
  error?: string;
};

function BoundCardRow({
  card,
  onUnbind,
}: {
  card: BoundCard;
  onUnbind: (cardId: number) => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const handleAskUnbind = useCallback(() => setConfirming(true), []);
  const handleCancelUnbind = useCallback(() => setConfirming(false), []);
  const handleConfirmUnbind = useCallback(() => {
    setConfirming(false);
    onUnbind(card.card_id);
  }, [card.card_id, onUnbind]);
  return (
    <li
      className="flex flex-wrap items-center gap-2 rounded-lg border border-border/40 bg-background/50 px-2.5 py-2"
      data-testid="bound-card-item"
    >
      <CreditCardIcon className="size-4 shrink-0 text-gold-ink dark:text-gold" />
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="text-[13px] font-medium">{card.bank_name}</span>
          <span className="font-mono text-[11px] text-muted-foreground">
            {card.card_no_masked}
          </span>
        </div>
        <div className="mt-0.5 flex flex-wrap gap-x-3 text-[11px] text-muted-foreground">
          <span>开户人 {card.holder_name}</span>
          <span>
            虚拟余额{" "}
            <span className="font-mono text-foreground/80">
              {card.balance_yuan}
            </span>{" "}
            元
          </span>
        </div>
      </div>
      {confirming ? (
        <div className="flex shrink-0 gap-1">
          <Button
            className="h-7 rounded-lg bg-destructive px-2.5 text-[12px] text-white hover:bg-destructive/90"
            data-testid="bound-card-unbind-confirm"
            onClick={handleConfirmUnbind}
            size="sm"
            type="button"
          >
            确认解绑
          </Button>
          <Button
            className="h-7 rounded-lg px-2.5 text-[12px]"
            onClick={handleCancelUnbind}
            size="sm"
            type="button"
            variant="ghost"
          >
            取消
          </Button>
        </div>
      ) : (
        <Button
          className="h-7 shrink-0 rounded-lg px-2.5 text-[12px]"
          data-testid="bound-card-unbind"
          onClick={handleAskUnbind}
          size="sm"
          type="button"
          variant="ghost"
        >
          解绑
        </Button>
      )}
    </li>
  );
}

export function BoundCardsDialog() {
  const [open, setOpen] = useState(false);
  const [cards, setCards] = useState<BoundCard[]>([]);
  const [bankName, setBankName] = useState("");
  const [cardNo, setCardNo] = useState("");
  const [holderName, setHolderName] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [needLogin, setNeedLogin] = useState(false);

  useEffect(() => {
    const openBoundCards = () => setOpen(true);
    window.addEventListener("open-bound-cards", openBoundCards);
    return () => window.removeEventListener("open-bound-cards", openBoundCards);
  }, []);

  const loadCards = useCallback(async () => {
    const res = await fetch(`${bankAgentBase()}/api/bound-cards`, {
      headers: bankAuthHeaders(),
    });
    const json = (await res
      .json()
      .catch(() => null)) as BoundCardsResponse | null;
    if (!res.ok) {
      setError(json?.error ?? "银行卡加载失败");
      return;
    }
    setCards(json?.cards ?? []);
  }, []);

  useEffect(() => {
    if (!open) {
      return;
    }
    setError("");
    setBankName("");
    setCardNo("");
    setHolderName("");
    if (!getStoredToken()) {
      setNeedLogin(true);
      setCards([]);
      return;
    }
    setNeedLogin(false);
    loadCards().catch(() => setError("无法连接服务器,请稍后重试"));
  }, [open, loadCards]);

  const handleBankNameChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => setBankName(event.target.value),
    []
  );
  const handleCardNoChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => setCardNo(event.target.value),
    []
  );
  const handleHolderNameChange = useCallback(
    (event: ChangeEvent<HTMLInputElement>) => setHolderName(event.target.value),
    []
  );

  const handleBind = useCallback(async () => {
    if (!(bankName.trim() && cardNo.trim() && holderName.trim())) {
      setError("请填写银行名称、银行卡号和开户人");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${bankAgentBase()}/api/bound-cards`, {
        body: JSON.stringify({
          bank_name: bankName.trim(),
          card_no: cardNo.trim(),
          holder_name: holderName.trim(),
        }),
        headers: {
          "Content-Type": "application/json",
          ...bankAuthHeaders(),
        },
        method: "POST",
      });
      const json = (await res.json().catch(() => null)) as
        | BoundCard
        | { error?: string }
        | null;
      if (!res.ok || !json) {
        setError(
          (json as { error?: string } | null)?.error ?? "绑定失败,请稍后重试"
        );
        return;
      }
      setBankName("");
      setCardNo("");
      setHolderName("");
      await loadCards();
      toast.success("银行卡已绑定(演示虚拟余额)");
    } catch {
      setError("无法连接服务器,请稍后重试");
    } finally {
      setLoading(false);
    }
  }, [bankName, cardNo, holderName, loadCards]);

  const handleUnbind = useCallback(
    async (cardId: number) => {
      setError("");
      try {
        const res = await fetch(
          `${bankAgentBase()}/api/bound-cards/${cardId}`,
          { headers: bankAuthHeaders(), method: "DELETE" }
        );
        const json = (await res.json().catch(() => null)) as {
          error?: string;
        } | null;
        if (!res.ok) {
          setError(json?.error ?? "解绑失败");
          return;
        }
        await loadCards();
        toast.success("银行卡已解绑");
      } catch {
        setError("无法连接服务器,请稍后重试");
      }
    },
    [loadCards]
  );

  return (
    <Dialog onOpenChange={setOpen} open={open}>
      <DialogContent
        className="max-h-[85vh] overflow-y-auto sm:max-w-md"
        data-testid="bound-cards-dialog"
      >
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <CreditCardIcon className="size-4 text-gold-ink dark:text-gold" />
            绑定银行卡
          </DialogTitle>
          <DialogDescription>
            演示阶段只记录银行、卡号后四位和开户人,不校验真实卡号、不查询真实余额;
            绑定后分配 50,000 元虚拟余额,可在「充钱」里选择这张卡转入。
          </DialogDescription>
        </DialogHeader>

        {needLogin ? (
          <div className="flex flex-col items-center gap-3 py-4 text-center">
            <p className="text-[13px] text-muted-foreground">
              请先登录后再绑定银行卡。
            </p>
            <Link href="/login">
              <Button type="button">去登录</Button>
            </Link>
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            <section>
              <div className="mb-2 text-[13px] font-medium">已绑定</div>
              <ul className="flex flex-col gap-2">
                {cards.length > 0 ? (
                  cards.map((card) => (
                    <BoundCardRow
                      card={card}
                      key={card.card_id}
                      onUnbind={handleUnbind}
                    />
                  ))
                ) : (
                  <li className="rounded-lg border border-dashed border-border/60 px-3 py-3 text-[12px] text-muted-foreground">
                    还没有绑定银行卡
                  </li>
                )}
              </ul>
            </section>

            <section className="flex flex-col gap-3">
              <div className="text-[13px] font-medium">添加一张卡(演示)</div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="bound-bank-name">银行名称</Label>
                <Input
                  data-testid="bound-bank-name"
                  id="bound-bank-name"
                  maxLength={30}
                  onChange={handleBankNameChange}
                  placeholder="如 招商银行"
                  value={bankName}
                />
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="bound-card-no">银行卡号</Label>
                <Input
                  autoComplete="off"
                  className="font-mono"
                  data-testid="bound-card-no"
                  id="bound-card-no"
                  maxLength={23}
                  onChange={handleCardNoChange}
                  placeholder="16-19 位数字(演示,不校验真伪)"
                  value={cardNo}
                />
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor="bound-holder-name">开户人姓名</Label>
                <Input
                  data-testid="bound-holder-name"
                  id="bound-holder-name"
                  maxLength={30}
                  onChange={handleHolderNameChange}
                  placeholder="需与银行卡开户人一致(演示)"
                  value={holderName}
                />
              </div>
              {error ? (
                <p
                  className="rounded-lg bg-destructive/10 px-3 py-2 text-[13px] text-destructive"
                  data-testid="bound-card-error"
                >
                  {error}
                </p>
              ) : null}
              <Button
                className="rounded-lg bg-primary text-white shadow-[0_8px_18px_-10px_rgba(30,82,165,0.7)] hover:bg-primary/90 disabled:opacity-50"
                data-testid="bound-card-submit"
                disabled={loading}
                onClick={handleBind}
                type="button"
              >
                {loading ? "绑定中…" : "绑定银行卡"}
              </Button>
              <p className="text-[11px] leading-relaxed text-muted-foreground">
                虚拟余额仅用于演示,不是真实银行卡余额;完整卡号不会入库。
              </p>
            </section>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
