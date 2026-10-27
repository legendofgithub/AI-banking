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
    <span className="ml-1.5 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-brand-red-solid px-1 text-[11px] font-semibold text-white shadow-[0_4px_10px_-4px_rgba(211,46,25,0.7)]">
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
    <div className="relative isolate flex min-h-dvh flex-col overflow-hidden bg-aurora">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-[520px] overflow-hidden"
      >
        <div className="aurora-drift absolute -left-28 -top-32 size-[420px] rounded-full bg-[radial-gradient(circle,rgba(255,176,192,0.5),transparent_66%)]" />
        <div className="aurora-drift absolute -right-36 top-8 size-[460px] rounded-full bg-[radial-gradient(circle,rgba(150,214,255,0.46),transparent_66%)] [animation-delay:-9s]" />
      </div>

      {/* 顶栏:玻璃白底 + 深蓝文字 + 红色品牌点,对齐参考图的导航语言 */}
      <header className="glass-bar safe-area-top sticky top-0 z-10 flex min-h-14 items-center gap-2 border-b border-white/60 px-4 shadow-[0_1px_0_rgba(255,255,255,0.7),0_8px_24px_-20px_rgba(30,82,165,0.45)]">
        <div className="flex size-8 items-center justify-center rounded-xl bg-brand-red-solid text-white shadow-[0_6px_16px_-6px_rgba(211,46,25,0.7)]">
          <Landmark className="size-4" />
        </div>
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-[14px] font-semibold leading-tight text-ink dark:text-brand-blue">
            {data?.nickname ? `${data.nickname}，欢迎回来` : "AI 银行助手"}
          </span>
          <span className="truncate text-[11px] leading-tight text-muted-foreground">
            <span className="sm:hidden">你的银行数字员工</span>
            <span className="hidden sm:inline">
              你的银行数字员工 · AI 处理常规，人负责例外
            </span>
          </span>
        </div>
        <Link
          className="sheen ml-auto inline-flex h-9 shrink-0 items-center gap-1.5 whitespace-nowrap rounded-lg bg-brand-red-solid px-3 text-[12px] font-medium text-white shadow-[0_8px_20px_-10px_rgba(211,46,25,0.7)] transition-colors hover:bg-brand-red-deep sm:h-8"
          href="/chat"
        >
          <SparklesIcon className="size-3.5" />
          进入对话
        </Link>
      </header>

      <main className="mx-auto flex w-full max-w-2xl flex-col gap-4 px-4 pt-5">
        {guest ? (
          <div className="glass-panel gradient-hairline rounded-2xl p-8 text-center">
            <p className="text-[15px] font-medium text-foreground">
              登录后查看你的银行
            </p>
            <p className="mt-1 text-[13px] text-muted-foreground">
              资产、待办、订阅提醒都在这里等你
            </p>
            <Link
              className="sheen mt-4 inline-flex h-10 items-center rounded-xl bg-brand-red-solid px-6 text-[14px] font-medium text-white shadow-[0_10px_24px_-12px_rgba(211,46,25,0.7)] transition-colors hover:bg-brand-red-deep"
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
            {/* 资产卡:红-紫-蓝主视觉,页面唯一的持续光效 */}
            <section
              aria-label="总资产"
              className="brand-surface glow-brand relative overflow-hidden rounded-2xl p-5 text-white"
            >
              <div
                aria-hidden="true"
                className="pointer-events-none absolute -right-12 -top-20 size-56 rounded-full border border-white/20"
              />
              <div
                aria-hidden="true"
                className="pointer-events-none absolute -right-4 -top-6 size-28 rounded-full bg-[radial-gradient(circle,rgba(255,255,255,0.3),transparent_68%)]"
              />
              <div className="relative flex items-center gap-1.5 text-[12px] text-white/85">
                <ShieldCheckIcon className="size-3.5" />
                总资产(演示)
              </div>
              <div className="relative mt-1 text-[34px] font-bold leading-tight tabular-nums">
                ¥{data.total_yuan}
              </div>
              <div className="relative mt-3 flex flex-col gap-1 border-t border-white/25 pt-3">
                {data.accounts.map((a) => (
                  <div
                    className="flex items-center justify-between text-[13px]"
                    key={a.name}
                  >
                    <span className="text-white/80">{a.name}</span>
                    <span className="font-medium tabular-nums">
                      ¥{a.balance_yuan}
                    </span>
                  </div>
                ))}
              </div>
            </section>

            {/* 待办:agent 主动来找用户,而不是等用户开口 */}
            <section
              aria-label="今日待办"
              className="glass-panel gradient-hairline rounded-2xl p-4"
            >
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
                      <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-brand-red-soft text-brand-red">
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
                        className="shrink-0 rounded-lg bg-primary px-3 py-1.5 text-[12px] font-medium text-white shadow-[0_6px_14px_-8px_rgba(30,82,165,0.6)] transition hover:bg-primary/90"
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
                {QUICK_ACTIONS.map((a, i) => (
                  <button
                    className="group flex flex-col items-center gap-1.5 rounded-xl border border-white/60 bg-white/70 px-2 py-3 text-[12px] text-foreground backdrop-blur-sm transition-all hover:-translate-y-0.5 hover:border-brand-red/30 hover:bg-white/90 hover:shadow-[var(--shadow-float)] dark:border-white/10 dark:bg-white/[0.04] dark:hover:border-brand-red/30 dark:hover:bg-white/[0.08]"
                    key={a.label}
                    onClick={() => goChat(router, a.q)}
                    type="button"
                  >
                    <span
                      className={`flex size-9 items-center justify-center rounded-full transition-colors ${
                        i % 2 === 0
                          ? "bg-brand-red-soft text-brand-red group-hover:bg-brand-red-solid group-hover:text-white"
                          : "bg-brand-blue-soft text-brand-blue group-hover:bg-brand-blue group-hover:text-white"
                      }`}
                    >
                      {a.icon}
                    </span>
                    {a.label}
                  </button>
                ))}
              </div>
            </section>

            {/* 最近交易 */}
            <section
              aria-label="最近交易"
              className="glass-panel gradient-hairline rounded-2xl p-4"
            >
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

    </div>
  );
}
