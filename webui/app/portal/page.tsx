"use client";

/*
 * 服务门户(/portal):/home 的桌面宽屏姊妹页,参考微众银行官网排版。
 * - 手机扫码/App 用的是 /home(一行未改);本页用于电脑端展示项目全貌:
 *   横版海报 hero + 服务宫格 + 今日待办 + 理财专区 + 账单统计 + 最近交易,
 *   页底另有普惠宣言模块(左图右文,宣传小小微与普惠金融定位)。
 * - 数据端点:GET /api/home(登录聚合)、/api/wealth/products(产品目录)、
 *   /api/analysis/monthly(上月账单),全部 x-bank-token 头、401 落游客引导。
 * - 所有可点卡片与 /home 同口径:写入 bank-autosend 后进 /chat,
 *   agent 直接收话开跑(见 multimodal-input.tsx 的消费逻辑)。
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
  SettingsIcon,
  ShieldCheckIcon,
  SparklesIcon,
  TrendingDownIcon,
  TrendingUpIcon,
} from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  DropdownMenu,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  bankAgentBase,
  bankAuthHeaders,
  getStoredToken,
  useBankAuth,
} from "@/hooks/use-bank-auth";
import { SettingsMenu } from "@/components/chat/sidebar-settings";

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

type WealthProduct = {
  id: number;
  name: string;
  p_type: "money_fund" | "bond" | "mixed" | "gold" | "deposit";
  risk_level: string;
  expected_return_pct: number;
  min_subscribe_yuan: string;
  lock_days: number;
  intro: string;
};

type MonthlyReport = {
  month: string;
  income_yuan: string;
  expense_yuan: string;
  net_yuan: string;
  mom_income_pct: number | null;
  mom_expense_pct: number | null;
  top_categories: { category: string; amount_yuan: string; pct: number }[];
  savings_rate_pct: number | null;
};

const P_TYPE_LABEL: Record<WealthProduct["p_type"], string> = {
  money_fund: "货币基金",
  bond: "债券",
  mixed: "混合",
  gold: "黄金",
  deposit: "存款",
};

const QUICK_ACTIONS: {
  label: string;
  desc: string;
  icon: React.ReactNode;
  q: string;
}[] = [
  {
    label: "我要转账",
    desc: "说清楚收款人和金额,建单确认后到账",
    icon: <SendIcon className="size-5" />,
    q: "我要转账",
  },
  {
    label: "账单分析",
    desc: "上月收支、储蓄率与分类构成一问即出",
    icon: <LineChartIcon className="size-5" />,
    q: "上个月花了多少",
  },
  {
    label: "理财",
    desc: "按你的风险等级匹配产品并说明费率",
    icon: <PiggyBankIcon className="size-5" />,
    q: "帮我看看理财产品",
  },
  {
    label: "卡片管理",
    desc: "查卡片状态、调限额,动账先确认",
    icon: <CreditCardIcon className="size-5" />,
    q: "卡片管理",
  },
  {
    label: "订阅管家",
    desc: "挖掘周期扣费、提示涨价、一键取消",
    icon: <RepeatIcon className="size-5" />,
    q: "帮我看看订阅都花多少钱",
  },
  {
    label: "生日计划",
    desc: "跨场景联动:到期自动生成待办清单",
    icon: <CalendarClockIcon className="size-5" />,
    q: "帮我准备生日",
  },
];

function goChat(router: ReturnType<typeof useRouter>, q: string) {
  sessionStorage.setItem("bank-autosend", q);
  router.push("/chat");
}

function TodoBadge({ n }: { n: number }) {
  return (
    <span className="ml-1.5 inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-brand-red-solid px-1 text-[11px] font-semibold text-white shadow-[0_4px_10px_-4px_rgba(211,46,25,0.7)]">
      {n}
    </span>
  );
}

function SectionTitle({
  id,
  icon,
  title,
  hint,
}: {
  id: string;
  icon: React.ReactNode;
  title: string;
  hint?: string;
}) {
  return (
    <div id={id} className="mb-4 flex scroll-mt-20 items-end justify-between">
      <h2 className="flex items-center gap-2 text-[17px] font-semibold text-ink dark:text-foreground">
        <span className="flex size-7 items-center justify-center rounded-lg bg-brand-red-soft text-brand-red">
          {icon}
        </span>
        {title}
      </h2>
      {hint ? (
        <span className="text-[12px] text-muted-foreground">{hint}</span>
      ) : null}
    </div>
  );
}

/** 风险徽标配色:R1/R2 蓝、R3 琥珀、R4/R5 红(与品牌红蓝体系兼容) */
function riskBadgeClass(risk: string): string {
  const level = Number(risk.replace("R", "")) || 1;
  if (level >= 4) {
    return "bg-brand-red-soft text-brand-red";
  }
  if (level === 3) {
    return "bg-warning/10 text-warning";
  }
  return "bg-brand-blue-soft text-brand-blue";
}

function MomDelta({ pct }: { pct: number | null }) {
  if (pct === null) {
    return <span className="text-[11px] text-muted-foreground">环比 —</span>;
  }
  const up = pct >= 0;
  return (
    <span
      className={`inline-flex items-center gap-0.5 text-[11px] font-medium ${
        up ? "text-brand-red" : "text-emerald-600 dark:text-emerald-400"
      }`}
    >
      {up ? (
        <TrendingUpIcon className="size-3" />
      ) : (
        <TrendingDownIcon className="size-3" />
      )}
      环比 {up ? "+" : ""}
      {pct}%
    </span>
  );
}

export default function PortalPage() {
  const router = useRouter();
  const { user } = useBankAuth();
  const [data, setData] = useState<HomeData | null>(null);
  const [products, setProducts] = useState<WealthProduct[] | null>(null);
  const [report, setReport] = useState<MonthlyReport | null>(null);
  const [guest, setGuest] = useState(false);

  // 顶栏头像悬浮下拉:document mousemove + 几何坐标判定。Radix modal 打开时
  // body pointer-events:none,指针在弹层外移动时 mouseover 会穿透到 html,
  // 事件目标不可靠;因此不看事件目标,只看坐标是否落在头像或菜单矩形内——
  // 在内即开/保持,在外 200ms 后收起(指针停止移动时不产生事件,状态即最后判定)。
  const [accountOpen, setAccountOpen] = useState(false);
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      const trigger = document.querySelector('[aria-label="账号与设置"]');
      const menu = document.querySelector('[data-testid="settings-menu"]');
      const inside = (el: Element | null) => {
        if (!el) {
          return false;
        }
        const r = el.getBoundingClientRect();
        return (
          e.clientX >= r.left &&
          e.clientX <= r.right &&
          e.clientY >= r.top &&
          e.clientY <= r.bottom
        );
      };
      if (inside(trigger) || inside(menu)) {
        if (closeTimer.current) {
          clearTimeout(closeTimer.current);
          closeTimer.current = null;
        }
        setAccountOpen(true);
      } else if (!closeTimer.current) {
        closeTimer.current = setTimeout(() => {
          closeTimer.current = null;
          setAccountOpen(false);
        }, 200);
      }
    };
    document.addEventListener("mousemove", onMove);
    return () => {
      document.removeEventListener("mousemove", onMove);
      if (closeTimer.current) {
        clearTimeout(closeTimer.current);
        closeTimer.current = null;
      }
    };
  }, []);
  useEffect(
    () => () => {
      if (closeTimer.current) {
        clearTimeout(closeTimer.current);
      }
    },
    []
  );

  const load = useCallback(async () => {
    if (!getStoredToken()) {
      setGuest(true);
      return;
    }
    try {
      const res = await fetch(`${bankAgentBase()}/api/home`, {
        headers: bankAuthHeaders(),
      });
      if (res.status === 401) {
        setGuest(true);
        return;
      }
      const json = (await res.json().catch(() => null)) as HomeData | null;
      if (json) {
        setData(json);
        setGuest(false);
      }
    } catch {
      setGuest(true);
      return;
    }
    // 两个展示型端点失败不阻断主内容,专区各自留空态
    void fetch(`${bankAgentBase()}/api/wealth/products`, {
      headers: bankAuthHeaders(),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((j: { products?: WealthProduct[] } | null) =>
        setProducts(j?.products ?? [])
      )
      .catch(() => setProducts([]));
    void fetch(`${bankAgentBase()}/api/analysis/monthly`, {
      headers: bankAuthHeaders(),
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((j: MonthlyReport | null) => setReport(j))
      .catch(() => setReport(null));
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const pendingN = data?.pending_orders.length ?? 0;
  const subsN = data?.upcoming_subscriptions.length ?? 0;
  const plansN = data?.active_plans.length ?? 0;
  const todoN = pendingN + subsN + plansN;

  return (
    <div className="min-h-dvh bg-aurora">
      {/* 顶部导航条:桌面放大字标;手机端收缩为单行(safe-area 让位状态栏) */}
      <header className="glass-bar safe-area-top sticky top-0 z-20 border-b border-white/60 dark:border-white/10">
        <div className="mx-auto flex h-14 w-full max-w-6xl items-center gap-3 px-4 md:h-16 md:gap-4 md:px-5">
          <div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-brand-red-solid text-white shadow-[0_6px_16px_-6px_rgba(211,46,25,0.7)] md:size-10 md:rounded-2xl">
            <Landmark className="size-4 md:size-5" />
          </div>
          <span className="whitespace-nowrap text-[16px] font-bold tracking-wide text-ink dark:text-brand-blue md:text-[21px]">
            小小微银行助手
          </span>
          <nav className="ml-4 hidden items-center gap-2 text-[15px] font-medium text-muted-foreground md:flex">
            <a
              className="rounded-lg px-3 py-2 transition-colors hover:bg-brand-blue-soft hover:text-brand-blue"
              href="#services"
            >
              银行服务
            </a>
            <a
              className="rounded-lg px-3 py-2 transition-colors hover:bg-brand-blue-soft hover:text-brand-blue"
              href="#wealth"
            >
              理财专区
            </a>
            <a
              className="rounded-lg px-3 py-2 transition-colors hover:bg-brand-blue-soft hover:text-brand-blue"
              href="#analysis"
            >
              账单统计
            </a>
          </nav>
          {/* 账号/设置下拉:悬浮头像自动弹出(点击同样可开),移出 220ms 后收起 */}
          <div className="ml-auto flex shrink-0 items-center gap-2">
            {guest ? (
              <Link
                className="sheen inline-flex h-9 shrink-0 items-center gap-1.5 rounded-lg bg-brand-red-solid px-3.5 text-[13px] font-medium md:h-10 md:px-5 md:text-[14px] text-white shadow-[0_8px_20px_-10px_rgba(211,46,25,0.7)] transition-colors hover:bg-brand-red-deep"
                href="/login"
              >
                登录
              </Link>
            ) : (
              <Link
                className="sheen inline-flex h-9 shrink-0 items-center gap-1.5 rounded-lg bg-brand-red-solid px-3.5 text-[13px] font-medium md:h-10 md:px-5 md:text-[14px] text-white shadow-[0_8px_20px_-10px_rgba(211,46,25,0.7)] transition-colors hover:bg-brand-red-deep"
                href="/chat"
              >
                <SparklesIcon className="size-4" />
                进入对话
              </Link>
            )}
            <DropdownMenu
              onOpenChange={setAccountOpen}
              open={accountOpen}
            >
              <DropdownMenuTrigger
                aria-label="账号与设置"
                className="flex size-9 items-center justify-center rounded-full outline-none transition-colors hover:bg-brand-red-soft focus-visible:ring-2 focus-visible:ring-brand-red/40 dark:text-foreground md:size-10"
              >
                {user ? (
                  <span className="brand-surface flex size-8 items-center justify-center rounded-full text-[14px] font-semibold text-white shadow-[0_4px_12px_-6px_rgba(211,46,25,0.55)]">
                    {user.nickname?.trim() ? user.nickname[0] : "客"}
                  </span>
                ) : (
                  <SettingsIcon className="size-5" />
                )}
              </DropdownMenuTrigger>
              <SettingsMenu align="end" side="bottom" />
            </DropdownMenu>
          </div>
        </div>
      </header>

      {/* 横版海报:全宽照片 + 左侧压暗渐变 + 大标题(渐变同时盖掉原图小字) */}
      <section className="relative isolate overflow-hidden">
        <div
          aria-hidden="true"
          className="absolute inset-0 -z-10 bg-cover bg-center"
          style={{ backgroundImage: "url(/images/hero-banner.png)" }}
        />
        <div
          aria-hidden="true"
          className="absolute inset-0 -z-10 bg-gradient-to-r from-[#0E2A66]/95 via-[#0E2A66]/40 via-42% to-transparent"
        />
        <div className="mx-auto flex w-full max-w-6xl flex-col items-start gap-4 px-5 py-16 md:py-24">
          <span className="inline-flex items-center gap-1.5 rounded-full bg-white/15 px-3 py-1 text-[12px] font-medium text-white backdrop-blur-sm">
            <ShieldCheckIcon className="size-3.5" />
            每一步都需你确认 · 全程审计留痕
          </span>
          <h1 className="text-4xl font-bold leading-tight text-white md:text-5xl">
            小小微银行助手
          </h1>
          <p className="max-w-xl text-[15px] leading-relaxed text-white/85">
            把要办的事说清楚,数字员工替你跑流程——转账、订阅、理财、账单,
            AI 处理常规,人负责例外。
          </p>
          <div className="mt-2 flex flex-wrap items-center gap-3">
            <Link
              className="sheen inline-flex h-11 items-center gap-2 rounded-xl bg-brand-red-solid px-6 text-[15px] font-medium text-white shadow-[0_12px_28px_-10px_rgba(211,46,25,0.8)] transition-colors hover:bg-brand-red-deep"
              href="/chat"
            >
              <SparklesIcon className="size-4" />
              进入对话
            </Link>
            <a
              className="inline-flex h-11 items-center gap-2 rounded-xl border border-white/40 px-6 text-[15px] font-medium text-white backdrop-blur-sm transition-colors hover:bg-white/10"
              href="#services"
            >
              浏览服务
              <ArrowRightIcon className="size-4" />
            </a>
          </div>
          {data ? (
            <p className="mt-4 text-[13px] text-white/75">
              {data.nickname ? `${data.nickname}，欢迎回来` : null}
              {data.nickname ? " · " : ""}
              总资产 ¥{data.total_yuan}
            </p>
          ) : null}
        </div>
      </section>

      <main className="mx-auto flex w-full max-w-6xl flex-col gap-12 px-5 py-10">
        {guest ? (
          <div className="glass-panel gradient-hairline rounded-2xl p-10 text-center">
            <p className="text-[16px] font-medium text-foreground">
              登录后查看你的银行
            </p>
            <p className="mt-1 text-[13px] text-muted-foreground">
              资产、待办、理财与账单统计都在这里等你
            </p>
            <Link
              className="sheen mt-5 inline-flex h-11 items-center rounded-xl bg-brand-red-solid px-7 text-[14px] font-medium text-white shadow-[0_10px_24px_-12px_rgba(211,46,25,0.7)] transition-colors hover:bg-brand-red-deep"
              href="/login"
            >
              去登录
            </Link>
          </div>
        ) : (
          <>
            {/* 银行服务:今日待办(左) + 服务宫格(右),桌面双栏占满宽度 */}
            <section>
              <SectionTitle
                id="services"
                icon={<Landmark className="size-4" />}
                title="银行服务"
                hint="点击任意服务,数字员工直接开跑"
              />
              <div className="grid grid-cols-1 items-start gap-5 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
                {/* 今日待办 */}
                <div className="glass-panel gradient-hairline rounded-2xl p-5">
                  <div className="flex items-center justify-between">
                    <h3 className="flex items-center text-[15px] font-semibold text-foreground">
                      今日待办
                      {todoN > 0 ? <TodoBadge n={todoN} /> : null}
                    </h3>
                    <span className="text-[11px] text-muted-foreground">
                      绝不自动扣款
                    </span>
                  </div>
                  {todoN === 0 ? (
                    <p className="mt-6 text-[13px] text-muted-foreground">
                      暂无待办，一切安好。
                    </p>
                  ) : (
                    <ul className="mt-3 flex flex-col divide-y divide-border/60">
                      {pendingN > 0 ? (
                        <li className="flex items-center gap-3 py-3">
                          <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-brand-red-soft text-brand-red">
                            <BanknoteIcon className="size-4" />
                          </span>
                          <div className="min-w-0 flex-1">
                            <p className="text-[13px] font-medium text-foreground">
                              {pendingN} 笔转账待确认
                            </p>
                            <p className="truncate text-[11px] text-muted-foreground">
                              待确认 {data?.pending_orders[0].to_name} ¥
                              {data?.pending_orders[0].amount_yuan}
                            </p>
                          </div>
                        </li>
                      ) : null}
                      {subsN > 0 ? (
                        <li className="flex items-center gap-3 py-3">
                          <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-brand-blue-soft text-brand-blue">
                            <RepeatIcon className="size-4" />
                          </span>
                          <div className="min-w-0 flex-1">
                            <p className="text-[13px] font-medium text-foreground">
                              {subsN} 项订阅 3 天内扣费
                            </p>
                            <p className="truncate text-[11px] text-muted-foreground">
                              {data?.upcoming_subscriptions[0].merchant} ¥
                              {data?.upcoming_subscriptions[0].amount_yuan}
                            </p>
                          </div>
                        </li>
                      ) : null}
                      {plansN > 0 ? (
                        <li className="flex items-center gap-3 py-3">
                          <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-brand-blue-soft text-brand-blue">
                            <CalendarClockIcon className="size-4" />
                          </span>
                          <div className="min-w-0 flex-1">
                            <p className="text-[13px] font-medium text-foreground">
                              {plansN} 个联动计划进行中
                            </p>
                            <p className="truncate text-[11px] text-muted-foreground">
                              {data?.active_plans[0].title}
                            </p>
                          </div>
                        </li>
                      ) : null}
                    </ul>
                  )}
                  <button
                    className="mt-2 inline-flex items-center gap-1 text-[12px] text-brand-blue hover:underline"
                    type="button"
                    onClick={() => goChat(router, "今天有什么待办要处理")}
                  >
                    让助手逐条处理
                    <ArrowRightIcon className="size-3" />
                  </button>
                </div>

                {/* 服务宫格 3×2 */}
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
                  {QUICK_ACTIONS.map((a) => (
                    <button
                      className="glass-panel sheen group flex flex-col items-start gap-2 rounded-2xl p-4 text-left transition-transform hover:-translate-y-0.5 hover:shadow-[var(--shadow-float)]"
                      key={a.label}
                      type="button"
                      onClick={() => goChat(router, a.q)}
                    >
                      <span className="flex size-10 items-center justify-center rounded-xl bg-gradient-to-br from-brand-red-soft to-brand-blue-soft text-brand-red transition-colors group-hover:bg-brand-red group-hover:text-white">
                        {a.icon}
                      </span>
                      <span className="text-[14px] font-semibold text-foreground">
                        {a.label}
                      </span>
                      <span className="text-[12px] leading-relaxed text-muted-foreground">
                        {a.desc}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            </section>

            {/* 理财专区 */}
            <section>
              <SectionTitle
                id="wealth"
                icon={<PiggyBankIcon className="size-4" />}
                title="理财专区"
                hint={
                  products
                    ? `${products.length} 只在售 · 购买前需通过风险测评`
                    : "读取产品目录中…"
                }
              />
              {!products ? (
                <div className="flex h-32 items-center justify-center text-muted-foreground">
                  <LoaderIcon className="size-5 animate-spin" />
                </div>
              ) : products.length === 0 ? (
                <div className="glass-panel rounded-2xl p-8 text-center text-[13px] text-muted-foreground">
                  产品目录暂不可用
                </div>
              ) : (
                <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
                  {products.slice(0, 8).map((p) => (
                    <button
                      className="glass-panel gradient-hairline flex flex-col items-start gap-3 rounded-2xl p-4 text-left transition-transform hover:-translate-y-0.5 hover:shadow-[var(--shadow-float)]"
                      key={p.id}
                      type="button"
                      onClick={() => goChat(router, `介绍一下${p.name}`)}
                    >
                      <div className="flex w-full items-center justify-between gap-2">
                        <span className="rounded-md bg-brand-blue-soft px-2 py-0.5 text-[11px] font-medium text-brand-blue">
                          {P_TYPE_LABEL[p.p_type]}
                        </span>
                        <span
                          className={`rounded-md px-2 py-0.5 text-[11px] font-semibold ${riskBadgeClass(p.risk_level)}`}
                        >
                          {p.risk_level}
                        </span>
                      </div>
                      <span className="line-clamp-1 text-[14px] font-semibold text-foreground">
                        {p.name}
                      </span>
                      <div className="flex items-end gap-1">
                        <span className="text-2xl font-bold leading-none text-brand-red">
                          {p.expected_return_pct}%
                        </span>
                        <span className="pb-0.5 text-[11px] text-muted-foreground">
                          业绩基准
                        </span>
                      </div>
                      <div className="flex w-full justify-between text-[11px] text-muted-foreground">
                        <span>起购 ¥{p.min_subscribe_yuan}</span>
                        <span>
                          {p.lock_days > 0 ? `锁定 ${p.lock_days} 天` : "随存随取"}
                        </span>
                      </div>
                    </button>
                  ))}
                </div>
              )}
            </section>

            {/* 账单统计 */}
            <section>
              <SectionTitle
                id="analysis"
                icon={<LineChartIcon className="size-4" />}
                title="账单统计分析"
                hint={report ? `${report.month} · 已剔除内部划转` : undefined}
              />
              {!report ? (
                <div className="flex h-32 items-center justify-center text-muted-foreground">
                  <LoaderIcon className="size-5 animate-spin" />
                </div>
              ) : (
                <div className="grid grid-cols-1 items-stretch gap-5 lg:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]">
                  <div className="glass-panel gradient-hairline grid grid-cols-2 gap-px overflow-hidden rounded-2xl bg-border/40">
                    <div className="bg-card/80 p-5">
                      <p className="text-[12px] text-muted-foreground">本月收入</p>
                      <p className="mt-1 text-xl font-bold text-foreground">
                        ¥{report.income_yuan}
                      </p>
                      <MomDelta pct={report.mom_income_pct} />
                    </div>
                    <div className="bg-card/80 p-5">
                      <p className="text-[12px] text-muted-foreground">本月支出</p>
                      <p className="mt-1 text-xl font-bold text-foreground">
                        ¥{report.expense_yuan}
                      </p>
                      <MomDelta pct={report.mom_expense_pct} />
                    </div>
                    <div className="bg-card/80 p-5">
                      <p className="text-[12px] text-muted-foreground">当月结余</p>
                      <p className="mt-1 text-xl font-bold text-brand-blue">
                        ¥{report.net_yuan}
                      </p>
                    </div>
                    <div className="bg-card/80 p-5">
                      <p className="text-[12px] text-muted-foreground">储蓄率</p>
                      <p className="mt-1 text-xl font-bold text-brand-red">
                        {report.savings_rate_pct === null
                          ? "—"
                          : `${report.savings_rate_pct}%`}
                      </p>
                    </div>
                  </div>
                  <div className="glass-panel rounded-2xl p-5">
                    <h3 className="text-[15px] font-semibold text-foreground">
                      支出构成 Top 分类
                    </h3>
                    <ul className="mt-4 flex flex-col gap-3">
                      {report.top_categories.map((c) => (
                        <li className="flex items-center gap-3" key={c.category}>
                          <span className="w-20 shrink-0 truncate text-[12px] text-muted-foreground">
                            {c.category}
                          </span>
                          <span className="h-2.5 min-w-0 flex-1 overflow-hidden rounded-full bg-brand-blue-soft/70">
                            <span
                              className="block h-full rounded-full bg-gradient-to-r from-brand-red to-brand-violet"
                              style={{ width: `${Math.max(c.pct, 2)}%` }}
                            />
                          </span>
                          <span className="w-24 shrink-0 text-right text-[12px] tabular-nums text-foreground">
                            ¥{c.amount_yuan}
                            <span className="ml-1 text-muted-foreground">
                              {c.pct}%
                            </span>
                          </span>
                        </li>
                      ))}
                    </ul>
                    <button
                      className="mt-4 inline-flex items-center gap-1 text-[12px] text-brand-blue hover:underline"
                      type="button"
                      onClick={() => goChat(router, "上个月花了多少")}
                    >
                      让助手深入解读这份账单
                      <ArrowRightIcon className="size-3" />
                    </button>
                  </div>
                </div>
              )}
            </section>

            {/* 最近交易 */}
            <section>
              <SectionTitle
                id="transactions"
                icon={<BanknoteIcon className="size-4" />}
                title="最近交易"
                hint="与账单时间序一致"
              />
              <div className="glass-panel gradient-hairline overflow-hidden rounded-2xl">
                <ul className="divide-y divide-border/60">
                  {(data?.recent_transactions ?? []).map((t, i) => (
                    <li
                      className="flex items-center gap-3 px-5 py-3.5"
                      key={`${t.ts}-${i}`}
                    >
                      <span
                        className={`flex size-9 shrink-0 items-center justify-center rounded-xl text-[13px] font-semibold ${
                          t.direction === "in"
                            ? "bg-emerald-50 text-emerald-600 dark:bg-emerald-950/50 dark:text-emerald-400"
                            : "bg-brand-red-soft text-brand-red"
                        }`}
                      >
                        {t.direction === "in" ? "收" : "支"}
                      </span>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-[13px] font-medium text-foreground">
                          {t.counterparty}
                        </p>
                        <p className="text-[11px] text-muted-foreground">
                          {t.ts} · {t.tx_type}
                        </p>
                      </div>
                      <span
                        className={`text-[14px] font-semibold tabular-nums ${
                          t.direction === "in"
                            ? "text-emerald-600 dark:text-emerald-400"
                            : "text-foreground"
                        }`}
                      >
                        {t.direction === "in" ? "+" : "-"}¥{t.amount_yuan}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            </section>
          </>
        )}
      </main>

      {/* 普惠宣言:左图右文;浅蓝底板自视口约 1/3 处延伸到右缘,照片压在底板上(分层排版) */}
      <section className="relative isolate py-14 md:py-20">
        <div
          aria-hidden="true"
          className="absolute inset-y-10 left-[31%] right-0 -z-10 hidden bg-brand-blue-soft md:block dark:bg-white/[0.04]"
        />
        <div className="mx-auto grid w-full max-w-6xl items-center gap-8 px-5 md:grid-cols-2 md:gap-14">
          <img
            alt="小店店主正在制作咖啡"
            className="aspect-[686/476] w-full object-cover shadow-[0_24px_48px_-24px_rgba(30,82,165,0.35)]"
            loading="lazy"
            src="/images/inclusive-cafe.png"
          />
          <div>
            <h2 className="text-[26px] font-bold leading-snug text-ink md:text-[30px] dark:text-foreground">
              小小微，把银行装进对话里
              <br />
              让普惠金融触手可及
            </h2>
            <div className="mt-5 flex items-center">
              <span className="h-px w-12 bg-ink/60" />
              <span className="h-px flex-1 bg-border/70" />
            </div>
            <p className="mt-5 text-[13.5px] leading-relaxed text-muted-foreground">
              普惠金融，是把服务的门槛降到每个人够得着的地方。小小微银行助手面向每一位普通用户：不必研究复杂的功能菜单，用一句家常话说明想办的事，数字员工就能替你完成转账、订阅管理、理财匹配与账单解读，让专业的金融服务看得懂、用得上、办得成。
            </p>
            <p className="mt-3 text-[13.5px] leading-relaxed text-muted-foreground">
              我们同样坚守可信的底线：每一步都需你确认、全程审计留痕，资金动账绝不自动执行，把选择权始终交还用户。以科技降低服务的成本与门槛，提升金融服务的覆盖率、可得性和满意度——这是小小微对普惠金融最朴素的回答。
            </p>
            <Link
              className="mt-6 inline-flex items-center gap-1.5 text-[14px] font-medium text-brand-blue transition-colors hover:text-ink dark:hover:text-brand-blue"
              href="/chat"
            >
              和小小微聊聊
              <ArrowRightIcon className="size-4" />
            </Link>
          </div>
        </div>
      </section>

      <footer className="border-t border-white/60 py-6 text-center text-[12px] text-muted-foreground dark:border-white/10">
        小小微银行助手 · AI 负责理解世界，代码负责改变世界
      </footer>
    </div>
  );
}
