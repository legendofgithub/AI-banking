"use client";

/*
 * 银行首页(范式改造第一屏):打开产品先看到"你的银行",agent 退居全域层。
 * 数据来自 GET /api/home(只读聚合:资产/待确认单/将扣费订阅/进行中计划/最近交易)。
 * 待办与快捷入口跳 /chat 并带 sessionStorage 预填(multimodal-input 挂载时消费)。
 */

import {
  ArrowRightIcon,
  BanknoteIcon,
  CalendarClockIcon,
  CreditCardIcon,
  Landmark,
  LineChartIcon,
  LoaderIcon,
  PiggyBankIcon,
  RepeatIcon,
  SendIcon,
  ShieldCheckIcon,
  SparklesIcon,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { bankAgentBase, bankAuthHeaders, getStoredToken } from "@/hooks/use-bank-auth";

type HomeData = {
  nickname: string;
  accounts: { name: string; balance_yuan: string }[];
  total_yuan: string;
  pending_orders: {
    order_id: number;
    to_name: string;
    amount_yuan: string;
    status: string;
  }[];
  upcoming_subscriptions: {
    merchant: string;
    amount_yuan: string;
    days_left: number;
  }[];
  active_plans: { plan_id: number; title: string }[];
  recent_transactions: {
    ts: string;
    direction: "in" | "out";
    amount_yuan: string;
    counterparty: string;
    tx_type: string;
  }[];
};

const QUICK_ACTIONS: { label: string; icon: React.ReactNode; q: string }[] = [
  { label: "我要转账", icon: <SendIcon className="size-4" />, q: "我要转账" },
  { label: "账单分析", icon: <LineChartIcon className="size-4" />, q: "上个月花了多少" },
  { label: "理财", icon: <PiggyBankIcon className="size-4" />, q: "帮我看看理财产品" },
  { label: "卡片管理", icon: <CreditCardIcon className="size-4" />, q: "卡片管理" },
  { label: "订阅管家", icon: <RepeatIcon className="size-4" />, q: "帮我看看订阅都花多少钱" },
  { label: "生日计划", icon: <CalendarClockIcon className="size-4" />, q: "帮我准备生日" },
];

function goChat(router: ReturnType<typeof useRouter>, q: string) {
  sessionStorage.setItem("bank-prefill", q);
  router.push("/chat");
}

function TodoBadge({ n }: { n: number }) {
  return (
    <span className="ml-1.5 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-[#D60C1A] px-1 text-[11px] font-semibold text-white">
      {n}
    </span>
  );
}

export default function BankHomePage() {
  const router = useRouter();
  const [data, setData] = useState<HomeData | null>(null);
  const [guest, setGuest] = useState(false);

  const load = useCallback(async () => {
    if (!getStoredToken()) {
      setGuest(true);
      setData(null);
      return;
    }
    try {
      const res = await fetch(`${bankAgentBase()}/api/home`, {
        headers: bankAuthHeaders(),
      });
      if (res.status === 401) {
        setGuest(true);
        setData(null);
        return;
      }
      const json = (await res.json().catch(() => null)) as HomeData | null;
      if (json) {
        setData(json);
        setGuest(false);
      }
    } catch {
      setGuest(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const pendingN = data?.pending_orders.length ?? 0;
  const subsN = data?.upcoming_subscriptions.length ?? 0;
  const plansN = data?.active_plans.length ?? 0;
  const todoN = pendingN + subsN + plansN;

  return (
    <div className="flex min-h-dvh flex-col bg-[linear-gradient(180deg,#EEF3FF_0%,#F8FAFE_220px, var(--background)_420px)] pb-16 md:pb-0">
      {/* 顶栏:蓝渐变头部(与对话页同语言) */}
      <header className="sticky top-0 z-10 flex h-14 items-center gap-2 bg-gradient-to-r from-ink to-ink-3 px-4 shadow-[var(--shadow-card)]">
        <div className="flex size-8 items-center justify-center rounded-lg bg-white/15 text-white">
          <Landmark className="size-4" />
        </div>
        <div className="flex flex-col">
          <span className="text-[14px] font-semibold leading-tight text-white">
            {data?.nickname ? `${data.nickname}，欢迎回来` : "AI 银行助手"}
          </span>
          <span className="text-[11px] leading-tight text-white/70">
            你的银行数字员工 · AI 处理常规，人负责例外
          </span>
        </div>
        <Link
          className="ml-auto inline-flex h-8 items-center gap-1.5 rounded-lg border border-white/25 bg-white/10 px-3 text-[12px] font-medium text-white/90 transition-colors hover:bg-white/20 hover:text-white"
          href="/chat"
        >
          <SparklesIcon className="size-3.5" />
          进入对话
        </Link>
      </header>

      <main className="mx-auto flex w-full max-w-2xl flex-col gap-4 px-4 pt-5">
        {guest ? (
          <div className="rounded-2xl border border-gold-line/50 bg-card p-8 text-center shadow-[var(--shadow-card)]">
            <p className="text-[15px] font-medium text-foreground">
              登录后查看你的银行
            </p>
            <p className="mt-1 text-[13px] text-muted-foreground">
              资产、待办、订阅提醒都在这里等你
            </p>
            <Link
              className="mt-4 inline-flex h-10 items-center rounded-xl bg-gradient-to-r from-ink to-ink-3 px-6 text-[14px] font-medium text-white"
              href="/login"
            >
              去登录
            </Link>
          </div>
        ) : !data ? (
          <div className="flex h-40 items-center justify-center text-muted-foreground">
            <LoaderIcon className="size-5 animate-spin" />
          </div>
        ) : (
          <>
            {/* 资产卡:蓝色渐变大卡,银行语言的核心件 */}
            <section
              aria-label="总资产"
              className="rounded-2xl bg-gradient-to-br from-ink via-ink-2 to-ink-3 p-5 text-white shadow-[var(--shadow-float)]"
            >
              <div className="flex items-center gap-1.5 text-[12px] text-white/70">
                <ShieldCheckIcon className="size-3.5" />
                总资产(演示)
              </div>
              <div className="mt-1 text-[34px] font-bold leading-tight tabular-nums">
                ¥{data.total_yuan}
              </div>
              <div className="mt-3 flex flex-col gap-1 border-t border-white/15 pt-3">
                {data.accounts.map((a) => (
                  <div
                    className="flex items-center justify-between text-[13px]"
                    key={a.name}
                  >
                    <span className="text-white/75">{a.name}</span>
                    <span className="font-medium tabular-nums">
                      ¥{a.balance_yuan}
                    </span>
                  </div>
                ))}
              </div>
            </section>

            {/* 待办:agent 主动来找用户,而不是等用户开口 */}
            <section aria-label="今日待办" className="rounded-2xl border border-gold-line/40 bg-card p-4 shadow-[var(--shadow-card)]">
              <div className="flex items-center justify-between">
                <h2 className="flex items-center gap-1.5 text-[14px] font-semibold text-foreground">
                  今日待办
                  {todoN > 0 ? <TodoBadge n={todoN} /> : null}
                </h2>
                <span className="text-[11px] text-muted-foreground">
                  每一步都需你确认，绝不自动扣款
                </span>
              </div>
              {todoN === 0 ? (
                <p className="mt-3 text-[13px] text-muted-foreground">
                  暂无待办，一切安好。
                </p>
              ) : (
                <ul className="mt-2 flex flex-col divide-y divide-border/60">
                  {pendingN > 0 ? (
                    <li className="flex items-center gap-3 py-2.5">
                      <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-gold-bg text-gold-ink">
                        <BanknoteIcon className="size-4" />
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="text-[13px] font-medium text-foreground">
                          {pendingN} 笔转账待确认
                          <span className="ml-1.5 text-muted-foreground">
                            含 {data.pending_orders[0].to_name} ¥
                            {data.pending_orders[0].amount_yuan}
                          </span>
                        </p>
                        <p className="text-[11px] text-muted-foreground">
                          确认需支付密码,可随时取消
                        </p>
                      </div>
                      <Link
                        className="shrink-0 rounded-lg bg-gold px-3 py-1.5 text-[12px] font-medium text-white transition hover:bg-[#3352b8]"
                        href="/pending-transfers"
                      >
                        去确认
                      </Link>
                    </li>
                  ) : null}
                  {subsN > 0
                    ? data.upcoming_subscriptions.map((s) => (
                        <li className="flex items-center gap-3 py-2.5" key={s.merchant}>
                          <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-gold-bg text-gold-ink">
                            <RepeatIcon className="size-4" />
                          </span>
                          <div className="min-w-0 flex-1">
                            <p className="text-[13px] font-medium text-foreground">
                              {s.merchant} 将于
                              {s.days_left === 0
                                ? "今天"
                                : s.days_left === 1
                                  ? "明天"
                                  : `${s.days_left} 天后`}
                              扣费 ¥{s.amount_yuan}
                            </p>
                            <p className="text-[11px] text-muted-foreground">
                              可放行本期 / 跳过 / 一键取消
                            </p>
                          </div>
                          <button
                            className="shrink-0 rounded-lg border border-gold/50 px-3 py-1.5 text-[12px] font-medium text-gold-ink transition hover:bg-gold-bg"
                            onClick={() =>
                              goChat(router, `帮我处理订阅扣费:${s.merchant}`)
                            }
                            type="button"
                          >
                            处理
                          </button>
                        </li>
                      ))
                    : null}
                  {plansN > 0
                    ? data.active_plans.map((p) => (
                        <li className="flex items-center gap-3 py-2.5" key={p.plan_id}>
                          <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-gold-bg text-gold-ink">
                            <CalendarClockIcon className="size-4" />
                          </span>
                          <div className="min-w-0 flex-1">
                            <p className="text-[13px] font-medium text-foreground">
                              计划进行中:{p.title}
                            </p>
                            <p className="text-[11px] text-muted-foreground">
                              到期逐项提醒,每项单独确认
                            </p>
                          </div>
                          <button
                            className="shrink-0 rounded-lg border border-gold/50 px-3 py-1.5 text-[12px] font-medium text-gold-ink transition hover:bg-gold-bg"
                            onClick={() =>
                              goChat(router, `看一下计划《${p.title}》的安排`)
                            }
                            type="button"
                          >
                            查看
                          </button>
                        </li>
                      ))
                    : null}
                </ul>
              )}
            </section>

            {/* 快捷入口:一句话直达六大场景 */}
            <section aria-label="快捷入口">
              <div className="grid grid-cols-3 gap-2.5 sm:grid-cols-6">
                {QUICK_ACTIONS.map((a) => (
                  <button
                    className="flex flex-col items-center gap-1.5 rounded-xl border border-gold-line/40 bg-card px-2 py-3 text-[12px] text-foreground transition-all hover:-translate-y-0.5 hover:border-gold/40 hover:bg-gold-bg/70 hover:text-gold-ink hover:shadow-[var(--shadow-card)]"
                    key={a.label}
                    onClick={() => goChat(router, a.q)}
                    type="button"
                  >
                    <span className="flex size-9 items-center justify-center rounded-full bg-gold-bg text-gold-ink">
                      {a.icon}
                    </span>
                    {a.label}
                  </button>
                ))}
              </div>
            </section>

            {/* 最近交易 */}
            <section aria-label="最近交易" className="rounded-2xl border border-border/50 bg-card p-4 shadow-[var(--shadow-card)]">
              <h2 className="text-[14px] font-semibold text-foreground">
                最近交易
              </h2>
              {data.recent_transactions.length === 0 ? (
                <p className="mt-2 text-[13px] text-muted-foreground">
                  暂无交易记录
                </p>
              ) : (
                <ul className="mt-1 flex flex-col divide-y divide-border/60">
                  {data.recent_transactions.map((t, i) => (
                    <li
                      className="flex items-center justify-between gap-3 py-2.5"
                      key={`${t.ts}-${i}`}
                    >
                      <div className="min-w-0">
                        <p className="truncate text-[13px] font-medium text-foreground">
                          {t.counterparty || t.tx_type}
                        </p>
                        <p className="text-[11px] text-muted-foreground">
                          {String(t.ts).slice(0, 10)}
                        </p>
                      </div>
                      <span
                        className={`shrink-0 text-[14px] font-semibold tabular-nums ${
                          t.direction === "in"
                            ? "text-[color:var(--success)]"
                            : "text-foreground"
                        }`}
                      >
                        {t.direction === "in" ? "+" : "-"}¥{t.amount_yuan}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              <Link
                className="mt-2 inline-flex items-center gap-1 text-[12px] text-gold-ink hover:underline"
                href="/chat"
                onClick={() =>
                  sessionStorage.setItem("bank-prefill", "看下最近的账单")
                }
              >
                查看账单分析
                <ArrowRightIcon className="size-3.5" />
              </Link>
            </section>
          </>
        )}
      </main>

      {/* 手机端底部 tab:首页 / 对话(银行 App 范式) */}
      <nav className="fixed inset-x-0 bottom-0 z-10 flex h-16 items-stretch border-t border-border/60 bg-card md:hidden">
        <Link
          className="flex flex-1 flex-col items-center justify-center gap-0.5 text-[11px] font-medium text-gold-ink"
          href="/home"
        >
          <Landmark className="size-5" />
          首页
        </Link>
        <Link
          className="flex flex-1 flex-col items-center justify-center gap-0.5 text-[11px] text-muted-foreground"
          href="/chat"
        >
          <SparklesIcon className="size-5" />
          对话
        </Link>
      </nav>
    </div>
  );
}
