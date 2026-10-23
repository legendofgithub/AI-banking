"use client";

/**
 * AgentPlanVisualization
 * ------------------------------------------------------------------
 * Agent Core 规划可视化：把"感知 → 规划 → 守卫 → … → 记忆"的 9 阶段闭环，
 * 与 backend `agent/analysis.py · build_thinking()` 产出的真实思维链
 * （data-thinking / thinking_view）一起，渲染成客户能一眼看懂、可回放的卡片。
 *
 * 设计约束（对齐 webui 现有规范）：
 * - Tailwind v4 + CSS 变量主题令牌（bg-card / border-border / text-muted-foreground …
 *   在 .dark 下自动切换），不引入新 UI 依赖；
 * - 仅用 emoji 作图标，避免新增图标 import；
 * - 移动优先响应式：基础为窄屏布局，sm/md 起展开为多列；
 * - 动效用 mount 状态 + 过渡 + 逐条 transitionDelay 实现（无需新增 keyframes），
 *   并对 prefers-reduced-motion 做降级（直接全量显示）。
 */

import { useCallback, useState } from "react";
import { cn } from "@/lib/utils";

type Thought = { phase?: string; status?: string; text?: string };

/** 9 阶段规划闭环（取 state.py 顶部注释，恒定不变） */
const PHASES: { zh: string; en: string; ico: string; d: string }[] = [
  {
    d: "理解原话，抽取金额 / 时间 / 对象等事实",
    en: "Perceive",
    ico: "👁",
    zh: "感知",
  },
  {
    d: "生成计划：目标、约束、步骤、成功条件、闸门",
    en: "Plan",
    ico: "🗺",
    zh: "规划",
  },
  {
    d: "静态校验：拦越权工具、强制高风险闸门、禁自动扣款",
    en: "Guard",
    ico: "🛡",
    zh: "守卫",
  },
  {
    d: "人类可读卡片呈现，请用户确认（确认 ≠ 支付密码）",
    en: "PlanCard",
    ico: "📋",
    zh: "计划卡",
  },
  {
    d: "按拓扑顺序调技能；动钱交给带闸门的子图",
    en: "Act",
    ico: "⚡",
    zh: "执行",
  },
  {
    d: "对照「成功条件」而非「工具没报错」判定结果",
    en: "Observe",
    ico: "🔍",
    zh: "观察",
  },
  {
    d: "对照成功条件复盘，给出结论与局部修复建议",
    en: "Reflect",
    ico: "🧭",
    zh: "复盘",
  },
  {
    d: "局部重规划（已完成不重跑），展示 plan diff，封顶 3 次",
    en: "Replan",
    ico: "🔁",
    zh: "重规划",
  },
  {
    d: "确定性摘要写入长期记忆，下一轮可复用",
    en: "Remember",
    ico: "💾",
    zh: "记忆",
  },
];

const STATUS_STYLE: Record<string, { dot: string; chip: string }> = {
  blocked: {
    chip: "bg-rose-100 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300",
    dot: "bg-rose-500",
  },
  delegated: {
    chip: "bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300",
    dot: "bg-amber-500",
  },
  done: {
    chip: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300",
    dot: "bg-emerald-500",
  },
  failed: {
    chip: "bg-rose-100 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300",
    dot: "bg-rose-500",
  },
  info: {
    chip: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
    dot: "bg-slate-400",
  },
  passed: {
    chip: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300",
    dot: "bg-emerald-500",
  },
  success: {
    chip: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300",
    dot: "bg-emerald-500",
  },
};

const GUARDS = [
  {
    desc: "每一步都标风险等级，动钱（HIGH）必须有闸门，只读（READ）永远零资金变动。",
    ico: "🚦",
    pills: [
      {
        c: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
        t: "READ 只读",
      },
      {
        c: "bg-sky-100 text-sky-700 dark:bg-sky-950/50 dark:text-sky-300",
        t: "LOW",
      },
      {
        c: "bg-sky-100 text-sky-700 dark:bg-sky-950/50 dark:text-sky-300",
        t: "MED",
      },
      {
        c: "bg-rose-100 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300",
        t: "HIGH 动钱",
      },
    ],
    title: "四档风险分级",
  },
  {
    desc: "高风险步骤只能引用白名单内的闸门，且必须由用户输支付密码——AI 永不替代确认。",
    ico: "🔐",
    pills: [
      {
        c: "bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300",
        t: "confirm_plan",
      },
      {
        c: "bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300",
        t: "confirm_transfer",
      },
      {
        c: "bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300",
        t: "confirm_wealth",
      },
      {
        c: "bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300",
        t: "confirm_sub_cancel",
      },
      {
        c: "bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300",
        t: "confirm_card",
      },
      {
        c: "bg-amber-100 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300",
        t: "confirm_linkage",
      },
    ],
    title: "6 类合法闸门",
  },
  {
    desc: "Planner 连「规划自动确认」的资格都没有，直接调用扣款 / 改限额类底层工具会被守卫判违规。",
    ico: "⛔",
    pills: [
      {
        c: "bg-rose-100 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300",
        t: "confirm_transfer_order",
      },
      {
        c: "bg-rose-100 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300",
        t: "confirm_wealth_order",
      },
      {
        c: "bg-rose-100 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300",
        t: "cancel_subscription",
      },
      {
        c: "bg-rose-100 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300",
        t: "set_card_limits",
      },
      {
        c: "bg-rose-100 text-rose-700 dark:bg-rose-950/50 dark:text-rose-300",
        t: "…共 8 项",
      },
    ],
    title: "8 个禁规划工具",
  },
  {
    desc: "金额、占比、日期、限额全由代码算；LLM 只做意图理解与播报，绝不输出数字或做算术。",
    ico: "🧮",
    pills: [
      {
        c: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
        t: "LLM = 理解 + 播报",
      },
      {
        c: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300",
        t: "Code = 算账 + 守卫",
      },
    ],
    title: "确定性计算铁律",
  },
];

export function AgentPlanVisualization({ thoughts }: { thoughts?: Thought[] }) {
  const [open, setOpen] = useState(true);
  const [showGuard, setShowGuard] = useState(false);

  const list = thoughts ?? [];

  const toggleOpen = useCallback(() => setOpen((v) => !v), []);
  const toggleGuard = useCallback(() => setShowGuard((v) => !v), []);

  // 进度由真实思维流驱动(不再用定时器机械走一遍):取已出现思维里最靠后
  // 的阶段,把它及其之前的所有阶段点亮——思维流到哪,进度就走到哪。
  const PHASE_INDEX: Record<string, number> = {
    制定计划: 1,
    复盘: 6,
    安全守卫: 2,
    识别意图: 0,
    调取记忆: 1,
    重新规划: 7,
  };
  const reached = list.reduce((m, t) => {
    const i = PHASE_INDEX[t.phase ?? ""];
    const idx = typeof i === "number" ? i : 4; // 执行类(技能/act)缺省落到「执行」
    return Math.max(m, idx);
  }, 0);
  const activeStep = Math.min(reached + 1, PHASES.length);

  return (
    <div className="w-[min(100%,520px)] overflow-hidden rounded-2xl border border-border bg-card shadow-[var(--shadow-card)]">
      {/* 头部（可折叠） */}
      <button
        className="flex w-full items-center justify-between gap-2 px-3.5 py-2.5 text-left"
        onClick={toggleOpen}
        type="button"
      >
        <span className="flex items-center gap-2 text-[13px] font-semibold text-foreground">
          <span aria-hidden>🧠</span>
          Agent 规划可视化
          <span className="rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground">
            {list.length} 步思维链
          </span>
        </span>
        <span className="text-[11px] text-muted-foreground">
          {open ? "收起" : "展开"}
        </span>
      </button>

      {open ? (
        <div className="border-t border-border px-3.5 pb-3.5 pt-3">
          {/* —— 9 阶段闭环 —— */}
          <div className="mb-1 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
            <span>规划闭环</span>
            <span className="h-px flex-1 bg-border" />
          </div>
          <div className="grid grid-cols-3 gap-1.5 sm:grid-cols-3">
            {PHASES.map((p, i) => {
              const on = i < activeStep;
              return (
                <div
                  className={cn(
                    "relative rounded-xl border p-2 transition-all duration-500",
                    on
                      ? "border-primary/60 bg-primary/5 ring-1 ring-primary/30"
                      : "border-border bg-muted/40 opacity-60"
                  )}
                  key={p.en}
                >
                  <div
                    className={cn(
                      "absolute -top-1.5 left-2 flex size-[18px] items-center justify-center rounded-full text-[10px] font-bold transition-colors duration-500",
                      on
                        ? "bg-primary text-primary-foreground"
                        : "bg-muted text-muted-foreground"
                    )}
                  >
                    {i + 1}
                  </div>
                  <div className="mt-1 text-[15px] leading-none">{p.ico}</div>
                  <div className="mt-1 text-[12px] font-semibold text-foreground">
                    {p.zh}
                  </div>
                  <div className="text-[9px] uppercase tracking-wide text-muted-foreground">
                    {p.en}
                  </div>
                </div>
              );
            })}
          </div>

          {/* —— 思维链时间线（真实数据） —— */}
          {list.length > 0 ? (
            <div className="mt-3">
              <div className="mb-1 flex items-center gap-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                <span>思维链</span>
                <span className="h-px flex-1 bg-border" />
              </div>
              <ol className="border-l border-border pl-3">
                {list.map((t) => {
                  const s =
                    STATUS_STYLE[t.status ?? "info"] ?? STATUS_STYLE.info;
                  return (
                    <li
                      className="relative pv-think-in pb-2.5"
                      key={`${t.phase}-${(t.text ?? "step").slice(0, 12)}`}
                    >
                      <span
                        className={cn(
                          "absolute -left-[18px] top-1.5 size-2 rounded-full",
                          s.dot
                        )}
                      />
                      <span
                        className={cn(
                          "inline-block rounded-full px-1.5 py-0.5 text-[10px] font-semibold",
                          s.chip
                        )}
                      >
                        {t.phase ?? "步骤"}
                      </span>
                      <div className="mt-0.5 text-[12px] leading-[1.6] text-foreground/90 [overflow-wrap:anywhere]">
                        {t.text}
                      </div>
                    </li>
                  );
                })}
              </ol>
            </div>
          ) : null}

          {/* —— 安全护栏（可折叠） —— */}
          <button
            className="mt-2 flex w-full items-center justify-between gap-2 rounded-lg bg-muted/50 px-2.5 py-1.5 text-[12px] font-medium text-foreground transition-colors hover:bg-muted"
            onClick={toggleGuard}
            type="button"
          >
            <span className="flex items-center gap-1.5">
              🛡 为什么安全 · 规划全貌
            </span>
            <span className="text-muted-foreground">
              {showGuard ? "收起" : "展开"}
            </span>
          </button>
          {showGuard ? (
            <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
              {GUARDS.map((g) => (
                <div
                  className="rounded-xl border border-border bg-muted/30 p-2.5"
                  key={g.title}
                >
                  <div className="flex items-center gap-1.5 text-[13px] font-semibold text-foreground">
                    <span aria-hidden>{g.ico}</span>
                    {g.title}
                  </div>
                  <p className="mt-1 text-[11.5px] leading-[1.5] text-muted-foreground">
                    {g.desc}
                  </p>
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {g.pills.map((p) => (
                      <span
                        className={cn(
                          "rounded-full px-1.5 py-0.5 text-[10px] font-medium",
                          p.c
                        )}
                        key={p.t}
                      >
                        {p.t}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
              {/* LLM vs 代码 职责划分 */}
              <div className="rounded-xl border border-border bg-muted/30 p-2.5 sm:col-span-2">
                <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                  <div>
                    <div className="text-[12px] font-semibold text-primary">
                      🤖 AI · LLM 负责
                    </div>
                    <ul className="mt-1 space-y-0.5 text-[11px] leading-[1.5] text-muted-foreground">
                      <li>· 从原话抽取目标与约束（事实级）</li>
                      <li>· 生成可执行计划草稿</li>
                      <li>· 把结果转成人话播报</li>
                      <li>· 失败提出局部重规划建议</li>
                    </ul>
                  </div>
                  <div>
                    <div className="text-[12px] font-semibold text-emerald-600 dark:text-emerald-400">
                      🛡 系统 · 代码负责
                    </div>
                    <ul className="mt-1 space-y-0.5 text-[11px] leading-[1.5] text-muted-foreground">
                      <li>· 静态守卫拦截越权与缺闸门</li>
                      <li>· 金额 / 日期 / 限额全由代码算</li>
                      <li>· 动钱必须经支付密码闸门</li>
                      <li>· 成功条件与审计全量留痕</li>
                    </ul>
                  </div>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
