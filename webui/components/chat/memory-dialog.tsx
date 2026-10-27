"use client";

/*
 * 「Agent 记忆」检阅弹窗(2026-10-26 可视化升级):
 * Agent 记住了你什么,按主题"一档一文件"直接展示——参考 Anthropic
 * memory tool 的文件式记忆:可看(view)、可忘(delete)、更新时间全程留痕。
 * 数据源:8800 GET /api/memory。
 *
 * 本次升级(配套后端对话提炼器,见 agent/graph.py):
 * - 模块从 5 档扩到 9 档:画像/关于你/偏好忌讳/想做的事/账单/理财/生日/
 *   对话记忆/计划历史——"点开就能看到很全面的记忆总结";
 * - 每档配可视化:消费结构条形图、持仓分布环图、计划成败环图、
 *   对话时间线、任务进度条、偏好标签墙(全部内嵌 SVG/CSS,零图表库依赖);
 * - 图表数据来自各模块 content 里的明细字段(top_categories/holdings_brief
 *   /items),lines 文本渲染保留为兜底。
 *
 * 入口:左下角设置 → Agent 记忆。通过 window 事件 open-memory 打开
 * (弹窗挂在 chat layout,桌面/手机侧栏收起时也能打开)。
 */

import {
  AlertTriangleIcon,
  BrainIcon,
  CalendarIcon,
  MessageSquareIcon,
  StarIcon,
  TargetIcon,
  TrashIcon,
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  bankAgentBase,
  bankAuthHeaders,
  getStoredToken,
} from "@/hooks/use-bank-auth";

type MemItem = {
  kind?: string;
  text?: string;
  at?: string;
  key?: string;
};

type MemoryModule = {
  module: string;
  label: string;
  updated_at: string;
  lines: string[];
  content?: Record<string, unknown>;
};

type ActiveTask = {
  goal: string;
  target_yuan: string;
  saved_yuan: string;
  deadline: string;
  next_review_at: string;
  status: string;
};

type MemoryResponse = {
  modules: MemoryModule[];
  active_task: ActiveTask | null;
  error?: string;
};

const EMPTY: MemoryResponse = { active_task: null, modules: [] };

// 图表配色(蓝/青/蓝紫/翠/琥珀/紫/灰),条形图与环图共用一套;
// 红色放最后,避免分类色被误读成风险或亏损
const PALETTE = [
  "#3E60CE",
  "#0EA5E9",
  "#6C97E9",
  "#10B981",
  "#F59E0B",
  "#8B5CF6",
  "#64748B",
  "#D32E19",
];

const PROFILE_FIELDS: [string, string][] = [
  ["name", "姓名"],
  ["risk_level", "风险偏好"],
  ["preferred_type", "偏好产品"],
  ["top_category", "消费大头"],
  ["holding_count", "持仓数"],
  ["subscription_count", "订阅数"],
];

const PREF_KIND_META: Record<string, { label: string; cls: string }> = {
  attitude: {
    cls: "border-blue-500/40 bg-blue-500/10 text-blue-700 dark:text-blue-300",
    label: "风险态度",
  },
  dislike: {
    cls: "border-red-500/40 bg-red-500/10 text-red-700 dark:text-red-300",
    label: "不喜欢",
  },
  habit: { cls: "border-border bg-muted/60", label: "习惯" },
  like: {
    cls: "border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300",
    label: "喜欢",
  },
};

const PLAN_RESULT_META: Record<
  string,
  { label: string; cls: string; color: string }
> = {
  cancelled: {
    cls: "bg-slate-500/15 text-slate-500",
    color: "#64748b",
    label: "取消",
  },
  done: {
    cls: "bg-emerald-500/15 text-emerald-600",
    color: "#10b981",
    label: "成功",
  },
  failed: {
    cls: "bg-red-500/15 text-red-600",
    color: "#ef4444",
    label: "失败",
  },
  locked: {
    cls: "bg-amber-500/15 text-amber-600",
    color: "#f59e0b",
    label: "已锁定",
  },
  success: {
    cls: "bg-emerald-500/15 text-emerald-600",
    color: "#10b981",
    label: "成功",
  },
};

function fmtStamp(iso: string): string {
  // "2026-10-23T14:22:33" → "10-23 14:22"(什么时候记的,一眼可核)
  return iso.length >= 16 ? `${iso.slice(5, 10)} ${iso.slice(11, 16)}` : iso;
}

function fmtDay(iso: string | undefined): string {
  return iso && iso.length >= 10 ? iso.slice(5, 10) : "";
}

function yuan(text: string): string {
  const n = Number(text);
  return Number.isFinite(n) ? n.toLocaleString("zh-CN") : text;
}

function num(v: unknown): number {
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

function itemsOf(m: MemoryModule): MemItem[] {
  const items = m.content?.items;
  return Array.isArray(items)
    ? (items as MemItem[]).filter((i) => i && typeof i.text === "string")
    : [];
}

function rowsOf(list: unknown, labelKey: string, valueKey: string) {
  // 把 [{labelKey, valueKey}, …] 明细列表整理成图表行(过滤空标签)
  if (!Array.isArray(list)) {
    return [];
  }
  return (list as Record<string, unknown>[])
    .map((r) => ({
      label: String(r?.[labelKey] ?? ""),
      value: num(r?.[valueKey]),
    }))
    .filter((r) => r.label);
}

// ---------------------------------------------------------------- 图表组件

function HBars({ rows }: { rows: { label: string; value: number }[] }) {
  // 消费结构水平条形图(div 实现,随容器自适应,金额右对齐)
  const max = Math.max(...rows.map((r) => r.value), 1);
  return (
    <div className="space-y-1.5">
      {rows.map((r, i) => (
        <div className="flex items-center gap-2" key={`${r.label}-${i}`}>
          <span className="w-[4.5rem] shrink-0 truncate text-right text-[11px] text-muted-foreground">
            {r.label}
          </span>
          <div className="h-4 flex-1 overflow-hidden rounded-md bg-muted/50">
            <div
              className="h-full rounded-md"
              style={{
                background: PALETTE[i % PALETTE.length],
                width: `${Math.max(4, (r.value / max) * 100)}%`,
              }}
            />
          </div>
          <span className="w-[4.5rem] shrink-0 text-right text-[11px] tabular-nums">
            ¥{yuan(String(r.value))}
          </span>
        </div>
      ))}
    </div>
  );
}

function Donut({
  rows,
  unit = "¥",
}: {
  rows: { label: string; value: number }[];
  unit?: string;
}) {
  // 持仓/成败占比环图(SVG pathLength=100 技巧,中心显示合计)
  const total = rows.reduce((s, r) => s + r.value, 0);
  if (total <= 0) {
    return null;
  }
  let cumulative = 0;
  const segments = rows.map((r, i) => {
    const pct = (r.value / total) * 100;
    const seg = (
      <circle
        cx="18"
        cy="18"
        fill="none"
        key={`${r.label}-${i}`}
        pathLength={100}
        r="15.9155"
        stroke={PALETTE[i % PALETTE.length]}
        strokeDasharray={`${Math.max(pct - 0.6, 0.4)} ${100 - Math.max(pct - 0.6, 0.4)}`}
        strokeDashoffset={25 - cumulative}
        strokeLinecap="butt"
        strokeWidth="5.5"
      />
    );
    cumulative += pct;
    return seg;
  });
  return (
    <div className="flex items-center gap-3">
      <div className="relative size-[104px] shrink-0">
        <svg className="size-full -rotate-0" viewBox="0 0 36 36">
          <circle
            cx="18"
            cy="18"
            fill="none"
            r="15.9155"
            stroke="hsl(var(--muted))"
            strokeWidth="5.5"
          />
          {segments}
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-[10px] text-muted-foreground">合计</span>
          <span className="text-[12px] font-semibold tabular-nums">
            {unit}
            {yuan(String(Math.round(total)))}
          </span>
        </div>
      </div>
      <ul className="min-w-0 flex-1 space-y-1">
        {rows.map((r, i) => (
          <li
            className="flex items-center gap-1.5 text-[12px]"
            key={`${r.label}-legend-${i}`}
          >
            <span
              className="size-2 shrink-0 rounded-full"
              style={{ background: PALETTE[i % PALETTE.length] }}
            />
            <span className="min-w-0 flex-1 truncate">{r.label}</span>
            <span className="shrink-0 tabular-nums text-muted-foreground">
              {unit}
              {yuan(String(r.value))}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function Progress({ saved, target }: { saved: number; target: number }) {
  const pct =
    target > 0 ? Math.min(100, Math.round((saved / target) * 100)) : 0;
  return (
    <div className="mt-2">
      <div className="h-2.5 overflow-hidden rounded-full bg-muted/60">
        <div
          className="h-full rounded-full bg-gradient-to-r from-amber-500 to-emerald-500 transition-all"
          style={{ width: `${Math.max(pct, 2)}%` }}
        />
      </div>
      <p className="mt-1.5 text-[11px] text-muted-foreground">
        已存 ¥{yuan(String(saved))} / 目标 ¥{yuan(String(target))} · {pct}%
      </p>
    </div>
  );
}

// ------------------------------------------------------------- 模块渲染器

function Chip({ children }: { children: ReactNode }) {
  return (
    <span className="inline-flex items-center rounded-md border border-border/70 bg-muted/40 px-1.5 py-0.5 text-[11px] leading-4">
      {children}
    </span>
  );
}

function ModuleCard({
  m,
  onForget,
  forgetting,
  children,
  highlight = false,
}: {
  m: MemoryModule;
  onForget: (m: MemoryModule) => void;
  forgetting: boolean;
  children: ReactNode;
  highlight?: boolean;
}) {
  return (
    <div
      className={
        highlight
          ? "rounded-xl border border-amber-500/30 bg-gradient-to-br from-amber-500/10 to-amber-600/5 p-3.5"
          : "rounded-xl border border-border/60 bg-card/60 p-3.5"
      }
      data-testid={`memory-module-${m.module}`}
    >
      <div className="flex items-center gap-2">
        <span className="text-[13px] font-medium">{m.label}</span>
        <span className="text-[11px] text-muted-foreground">
          {fmtStamp(m.updated_at)}
        </span>
        <Button
          aria-label={`忘记${m.label}`}
          className="ml-auto h-6 gap-1 rounded-md px-2 text-[11px] text-muted-foreground hover:text-red-500"
          disabled={forgetting}
          onClick={() => onForget(m)}
          size="sm"
          variant="ghost"
        >
          <TrashIcon className="size-3" />
          忘记
        </Button>
      </div>
      <div className="mt-2">{children}</div>
    </div>
  );
}

function Lines({ lines }: { lines: string[] }) {
  if (lines.length === 0) {
    return (
      <p className="text-[12px] text-muted-foreground">
        （这一档还没有可展示的内容）
      </p>
    );
  }
  return (
    <ul className="space-y-1">
      {lines.map((line) => (
        <li className="text-[13px] leading-5 text-foreground/90" key={line}>
          {line}
        </li>
      ))}
    </ul>
  );
}

function renderModuleBody(m: MemoryModule): ReactNode {
  const content = m.content ?? {};
  const items = itemsOf(m);
  switch (m.module) {
    case "profile": {
      const pairs = PROFILE_FIELDS.filter(([k]) => {
        const v = content[k];
        return v !== undefined && v !== null && String(v) !== "";
      }).map(([k, label]) => ({ label, value: String(content[k]) }));
      if (pairs.length === 0) {
        return <Lines lines={m.lines} />;
      }
      return (
        <div className="grid grid-cols-2 gap-1.5">
          {pairs.map((p) => (
            <div
              className="flex items-baseline gap-1.5 rounded-lg bg-background/50 px-2 py-1.5"
              key={p.label}
            >
              <span className="text-[11px] text-muted-foreground">
                {p.label}
              </span>
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium">
                {p.value}
              </span>
            </div>
          ))}
        </div>
      );
    }
    case "facts":
      if (items.length === 0) {
        return <Lines lines={m.lines} />;
      }
      return (
        <div className="flex flex-wrap gap-1.5">
          {items.map((it, i) => (
            <span
              className="inline-flex items-baseline gap-1 rounded-lg border border-violet-500/30 bg-violet-500/10 px-2 py-1"
              key={`${it.text}-${i}`}
            >
              <span className="text-[10px] font-medium text-violet-600 dark:text-violet-300">
                {it.kind || "事实"}
              </span>
              <span className="text-[12px]">{it.text}</span>
            </span>
          ))}
        </div>
      );
    case "preferences":
      if (items.length === 0) {
        return <Lines lines={m.lines} />;
      }
      return (
        <div className="flex flex-wrap gap-1.5">
          {items.map((it, i) => {
            const meta = PREF_KIND_META[it.kind || ""] ?? {
              cls: "border-border bg-muted/60",
              label: it.kind || "",
            };
            return (
              <span
                className={`inline-flex items-baseline gap-1 rounded-lg border px-2 py-1 ${meta.cls}`}
                key={`${it.text}-${i}`}
              >
                <span className="text-[10px] font-medium opacity-80">
                  {meta.label}
                </span>
                <span className="text-[12px]">{it.text}</span>
              </span>
            );
          })}
        </div>
      );
    case "goals":
      if (items.length === 0) {
        return <Lines lines={m.lines} />;
      }
      return (
        <ul className="space-y-1.5">
          {items.map((it, i) => (
            <li className="flex items-baseline gap-2" key={`${it.text}-${i}`}>
              <StarIcon className="mt-0.5 size-3.5 shrink-0 text-amber-500" />
              <span className="min-w-0 flex-1 text-[13px] leading-5">
                {it.text}
              </span>
              <span className="shrink-0 text-[10px] text-muted-foreground">
                {fmtDay(it.at)}
              </span>
            </li>
          ))}
        </ul>
      );
    case "bill": {
      const bars = rowsOf(content.top_categories, "category", "amount_yuan");
      const subs = Array.isArray(content.subscription_names)
        ? (content.subscription_names as string[]).filter(Boolean)
        : [];
      const anom = Array.isArray(content.anomaly_notes)
        ? (content.anomaly_notes as string[]).filter(Boolean)
        : [];
      return (
        <div className="space-y-2.5">
          {bars.length > 0 ? (
            <div>
              <p className="mb-1.5 text-[11px] font-medium text-muted-foreground">
                消费结构{content.period ? `（${String(content.period)}）` : ""}
              </p>
              <HBars rows={bars} />
            </div>
          ) : null}
          <div className="flex flex-wrap gap-1.5">
            {num(content.anomaly_count) > 0 ? (
              <Chip>
                <AlertTriangleIcon className="mr-1 size-3 text-red-500" />
                {String(content.anomaly_count)} 笔异常
              </Chip>
            ) : null}
            {num(content.subscription_count) > 0 ? (
              <Chip>{String(content.subscription_count)} 笔订阅</Chip>
            ) : null}
            {subs.map((s) => (
              <Chip key={s}>{s}</Chip>
            ))}
          </div>
          {anom.length > 0 ? (
            <ul className="space-y-1">
              {anom.map((a) => (
                <li
                  className="text-[12px] text-red-600 dark:text-red-400"
                  key={a}
                >
                  · {a}
                </li>
              ))}
            </ul>
          ) : null}
          {bars.length === 0 ? <Lines lines={m.lines} /> : null}
        </div>
      );
    }
    case "wealth": {
      const donut = rowsOf(content.holdings_brief, "product", "est_value_yuan");
      return (
        <div className="space-y-2.5">
          {donut.length > 0 ? (
            <div>
              <p className="mb-1.5 text-[11px] font-medium text-muted-foreground">
                持仓分布
              </p>
              <Donut rows={donut} />
            </div>
          ) : null}
          <div className="flex flex-wrap gap-1.5">
            {content.risk_level ? (
              <Chip>风险等级 {String(content.risk_level)}</Chip>
            ) : null}
            {num(content.holding_count) > 0 ? (
              <Chip>持仓 {String(content.holding_count)} 笔</Chip>
            ) : null}
            {content.preferred_type ? (
              <Chip>最大持仓「{String(content.preferred_type)}」</Chip>
            ) : null}
            {content.needs_assessment === true ? (
              <Chip>待做风险测评</Chip>
            ) : null}
          </div>
          {donut.length === 0 ? <Lines lines={m.lines} /> : null}
        </div>
      );
    }
    case "timeline": {
      if (items.length === 0) {
        return <Lines lines={m.lines} />;
      }
      return (
        <ol className="relative ml-1.5 space-y-2 border-l border-border/70 pl-3.5">
          {items.map((it, i) => (
            <li className="relative" key={`${it.text}-${i}`}>
              <span className="absolute top-[5px] -left-[19px] size-2 rounded-full border-2 border-background bg-blue-500" />
              <div className="flex items-baseline gap-1.5">
                <MessageSquareIcon className="size-3 shrink-0 translate-y-0.5 text-muted-foreground" />
                <span className="shrink-0 rounded bg-blue-500/10 px-1 text-[10px] text-blue-600 dark:text-blue-300">
                  {it.kind || "对话"}
                </span>
                <span className="min-w-0 flex-1 text-[12.5px] leading-5">
                  {it.text}
                </span>
                <span className="shrink-0 text-[10px] text-muted-foreground">
                  {fmtDay(it.at)}
                </span>
              </div>
            </li>
          ))}
        </ol>
      );
    }
    case "plan_history": {
      // 计划条目字段是 {goal, result, at} 不是 {text, kind},单独映射
      const raw = Array.isArray(content.items)
        ? (content.items as Record<string, unknown>[]).filter(
            (r) => r && typeof r === "object"
          )
        : [];
      const plans: MemItem[] = raw.map((r) => ({
        at: typeof r.at === "string" ? r.at : undefined,
        kind: String(r.result ?? ""),
        text: String(r.goal ?? ""),
      }));
      if (plans.length === 0) {
        return <Lines lines={m.lines} />;
      }
      const statusRows = Object.entries(
        plans.reduce<Record<string, { label: string; value: number }>>(
          (acc, it) => {
            const key = String(it.kind || "");
            if (!acc[key]) {
              acc[key] = {
                label: PLAN_RESULT_META[key]?.label ?? (key || "其他"),
                value: 0,
              };
            }
            acc[key].value += 1;
            return acc;
          },
          {}
        )
      ).map(([k, v]) => ({
        ...v,
        color: PLAN_RESULT_META[k]?.color,
      }));
      // 成败环图需要数值行;给 Donut 传自定义配色(按状态取色)
      const total = plans.length;
      let cumulative = 0;
      const segments = statusRows.map((r, i) => {
        const pct = (r.value / total) * 100;
        const color = r.color ?? PALETTE[i % PALETTE.length];
        const seg = (
          <circle
            cx="18"
            cy="18"
            fill="none"
            key={r.label}
            pathLength={100}
            r="15.9155"
            stroke={color}
            strokeDasharray={`${Math.max(pct - 0.6, 0.4)} ${100 - Math.max(pct - 0.6, 0.4)}`}
            strokeDashoffset={25 - cumulative}
            strokeLinecap="butt"
            strokeWidth="5.5"
          />
        );
        cumulative += pct;
        return seg;
      });
      return (
        <div className="space-y-2.5">
          <div className="flex items-center gap-3">
            <div className="relative size-[84px] shrink-0">
              <svg className="size-full" viewBox="0 0 36 36">
                <circle
                  cx="18"
                  cy="18"
                  fill="none"
                  r="15.9155"
                  stroke="hsl(var(--muted))"
                  strokeWidth="5.5"
                />
                {segments}
              </svg>
              <div className="absolute inset-0 flex flex-col items-center justify-center">
                <span className="text-[15px] font-semibold tabular-nums">
                  {total}
                </span>
                <span className="text-[9px] text-muted-foreground">个计划</span>
              </div>
            </div>
            <ul className="min-w-0 flex-1 space-y-1">
              {statusRows.map((r, i) => (
                <li
                  className="flex items-center gap-1.5 text-[12px]"
                  key={r.label}
                >
                  <span
                    className="size-2 shrink-0 rounded-full"
                    style={{
                      background: r.color ?? PALETTE[i % PALETTE.length],
                    }}
                  />
                  <span className="flex-1">{r.label}</span>
                  <span className="tabular-nums text-muted-foreground">
                    {r.value} 次
                  </span>
                </li>
              ))}
            </ul>
          </div>
          <ol className="relative ml-1.5 space-y-1.5 border-l border-border/70 pl-3.5">
            {plans.map((it, i) => {
              const meta = PLAN_RESULT_META[String(it.kind || "")];
              return (
                <li className="relative" key={`${it.text}-${i}`}>
                  <span
                    className="absolute top-[6px] -left-[19px] size-2 rounded-full border-2 border-background"
                    style={{ background: meta?.color ?? "#94a3b8" }}
                  />
                  <div className="flex items-baseline gap-1.5">
                    <TargetIcon className="size-3 shrink-0 translate-y-0.5 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate text-[12.5px] leading-5">
                      {it.text}
                    </span>
                    {meta ? (
                      <Badge
                        className={`h-4 shrink-0 px-1 text-[9px] ${meta.cls}`}
                        variant="secondary"
                      >
                        {meta.label}
                      </Badge>
                    ) : null}
                    <span className="shrink-0 text-[10px] text-muted-foreground">
                      {fmtDay(it.at)}
                    </span>
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      );
    }
    default:
      return <Lines lines={m.lines} />;
  }
}

// ------------------------------------------------------------------ 弹窗

export function MemoryDialog() {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<MemoryResponse>(EMPTY);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [needLogin, setNeedLogin] = useState(false);
  const [forgetTarget, setForgetTarget] = useState<MemoryModule | null>(null);
  const [forgetting, setForgetting] = useState(false);
  const [forgetAllOpen, setForgetAllOpen] = useState(false);
  const [forgettingAll, setForgettingAll] = useState(false);
  const [endTaskOpen, setEndTaskOpen] = useState(false);
  const [endingTask, setEndingTask] = useState(false);

  useEffect(() => {
    const openMemory = () => setOpen(true);
    window.addEventListener("open-memory", openMemory);
    return () => window.removeEventListener("open-memory", openMemory);
  }, []);

  const load = useCallback(async () => {
    if (!getStoredToken()) {
      setNeedLogin(true);
      setData(EMPTY);
      return;
    }
    setNeedLogin(false);
    setLoading(true);
    setError("");
    try {
      const res = await fetch(`${bankAgentBase()}/api/memory`, {
        cache: "no-store",
        headers: bankAuthHeaders(),
      });
      const body = await res.json();
      if (!res.ok) {
        setError(body.error || "读取记忆失败");
        return;
      }
      setData({
        active_task: body.active_task ?? null,
        modules: Array.isArray(body.modules) ? body.modules : [],
      });
    } catch {
      setError("读取记忆失败，请稍后再试");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) {
      void load();
    }
  }, [open, load]);

  const forget = useCallback(async () => {
    if (!forgetTarget) {
      return;
    }
    setForgetting(true);
    try {
      const res = await fetch(
        `${bankAgentBase()}/api/memory/${forgetTarget.module}`,
        { headers: bankAuthHeaders(), method: "DELETE" }
      );
      const body = await res.json();
      if (!res.ok) {
        toast.error(body.error || "忘记失败，请稍后再试");
        return;
      }
      setData({
        active_task: body.active_task ?? null,
        modules: Array.isArray(body.modules) ? body.modules : [],
      });
      toast.success(`已忘记「${forgetTarget.label}」`);
      setForgetTarget(null);
    } catch {
      toast.error("忘记失败，请稍后再试");
    } finally {
      setForgetting(false);
    }
  }, [forgetTarget]);

  const modules = data.modules;
  const task = data.active_task;
  const hasAnything = modules.length > 0 || task;

  // 总览统计:档案数 + 条目型记忆总数(画像/技能档按有值字段数粗略并入)
  const itemCount = modules.reduce((s, m) => {
    const items = itemsOf(m);
    if (items.length > 0) {
      return s + items.length;
    }
    return s + m.lines.length;
  }, 0);
  const lastUpdate = modules.reduce(
    (acc, m) => (m.updated_at > acc ? m.updated_at : acc),
    ""
  );

  const forgetAll = useCallback(async () => {
    setForgettingAll(true);
    try {
      const res = await fetch(`${bankAgentBase()}/api/memory`, {
        headers: bankAuthHeaders(),
        method: "DELETE",
      });
      const body = await res.json();
      if (!res.ok) {
        toast.error(body.error || "忘记失败，请稍后再试");
        return;
      }
      setData({
        active_task: body.active_task ?? null,
        modules: Array.isArray(body.modules) ? body.modules : [],
      });
      toast.success("已忘记全部记忆档案");
    } catch {
      toast.error("忘记失败，请稍后再试");
    } finally {
      setForgettingAll(false);
    }
  }, []);

  const endTask = useCallback(async () => {
    setEndingTask(true);
    try {
      const res = await fetch(`${bankAgentBase()}/api/memory/active-task`, {
        headers: bankAuthHeaders(),
        method: "DELETE",
      });
      const body = await res.json();
      if (!res.ok) {
        toast.error(body.error || "结束失败，请稍后再试");
        return;
      }
      setData({
        active_task: body.active_task ?? null,
        modules: Array.isArray(body.modules) ? body.modules : [],
      });
      toast.success("长程任务已结束");
    } catch {
      toast.error("结束失败，请稍后再试");
    } finally {
      setEndingTask(false);
    }
  }, []);

  return (
    <>
      <Dialog onOpenChange={setOpen} open={open}>
        <DialogContent className="flex max-h-[85vh] w-[94vw] max-w-2xl flex-col gap-0 overflow-hidden p-0">
          <DialogHeader className="border-b border-border/50 px-5 py-4">
            <DialogTitle className="flex items-center gap-2 text-base">
              <BrainIcon className="size-4 text-amber-500" />
              Agent 记忆
              {modules.length > 0 ? (
                <Button
                  aria-label="忘记全部记忆"
                  className="ml-auto h-6 gap-1 rounded-md px-2 text-[11px] text-muted-foreground hover:text-red-500"
                  disabled={forgettingAll}
                  onClick={() => setForgetAllOpen(true)}
                  size="sm"
                  variant="ghost"
                >
                  <TrashIcon className="size-3" />
                  全部忘记
                </Button>
              ) : null}
            </DialogTitle>
            <DialogDescription className="text-xs">
              按主题一档，可看可忘 · 更新时间全程留痕 · 只作用于你的账号
            </DialogDescription>
          </DialogHeader>

          {/* 原生 overflow-y-auto,不用 Radix ScrollArea:它在 flex 撑高的
              容器里 h-full 解析不出、viewport 长成内容高度,弹窗 overflow-hidden
              一裁就永远滚不动(2026-10-24 PC 实测) */}
          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="space-y-3 px-4 py-4">
              {needLogin ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  登录网银账号后，才能看到 Agent 记住了你什么。
                </p>
              ) : loading ? (
                <p className="py-8 text-center text-sm text-muted-foreground">
                  正在翻开记忆…
                </p>
              ) : error ? (
                <p className="py-8 text-center text-sm text-red-500">{error}</p>
              ) : hasAnything ? (
                <>
                  {/* 记忆总览:档案数/条目数/最近更新一屏可核 */}
                  <div className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border border-border/60 bg-muted/30 px-3.5 py-2.5">
                    <span className="text-[12px]">
                      <span className="text-lg font-semibold tabular-nums">
                        {modules.length}
                      </span>
                      <span className="ml-1 text-muted-foreground">
                        个记忆档案
                      </span>
                    </span>
                    <span className="text-[12px]">
                      <span className="text-lg font-semibold tabular-nums">
                        {itemCount}
                      </span>
                      <span className="ml-1 text-muted-foreground">条记忆</span>
                    </span>
                    {lastUpdate ? (
                      <span className="ml-auto inline-flex items-center gap-1 text-[11px] text-muted-foreground">
                        <CalendarIcon className="size-3" />
                        最近记录 {fmtStamp(lastUpdate)}
                      </span>
                    ) : null}
                  </div>

                  {modules.map((m) => (
                    <ModuleCard
                      forgetting={forgetting}
                      highlight={m.module === "profile"}
                      key={m.module}
                      m={m}
                      onForget={setForgetTarget}
                    >
                      {renderModuleBody(m)}
                    </ModuleCard>
                  ))}

                  {task ? (
                    <div
                      className="rounded-xl border border-border/60 bg-card/60 p-3.5"
                      data-testid="memory-active-task"
                    >
                      <div className="flex items-center gap-2">
                        <span className="text-[13px] font-medium">
                          长程任务
                        </span>
                        <Badge
                          className="h-5 px-1.5 text-[10px]"
                          variant="secondary"
                        >
                          进行中
                        </Badge>
                        <Button
                          aria-label="结束长程任务"
                          className="ml-auto h-6 gap-1 rounded-md px-2 text-[11px] text-muted-foreground hover:text-red-500"
                          disabled={endingTask}
                          onClick={() => setEndTaskOpen(true)}
                          size="sm"
                          variant="ghost"
                        >
                          <TrashIcon className="size-3" />
                          结束
                        </Button>
                      </div>
                      <p className="mt-2 text-[13px] leading-5">{task.goal}</p>
                      <Progress
                        saved={num(task.saved_yuan)}
                        target={num(task.target_yuan)}
                      />
                      <p className="mt-1 text-[12px] leading-5 text-muted-foreground">
                        截止 {task.deadline}
                        {task.next_review_at
                          ? ` · 下次复盘 ${task.next_review_at}`
                          : ""}
                      </p>
                    </div>
                  ) : null}
                </>
              ) : (
                <p className="py-8 text-center text-sm leading-6 text-muted-foreground">
                  还没有任何记忆。
                  <br />
                  和我聊聊天、说说你的喜好和计划、做做账单/理财分析，
                  这里会越记越全。
                </p>
              )}
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <AlertDialog
        onOpenChange={(v) => {
          if (!v && !forgetting) {
            setForgetTarget(null);
          }
        }}
        open={forgetTarget !== null}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>忘记「{forgetTarget?.label}」？</AlertDialogTitle>
            <AlertDialogDescription>
              Agent
              将不再用这一档记忆为你做建议；之后的对话产生了新经验仍会重新记。此操作不可撤销。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={forgetting}>先留着</AlertDialogCancel>
            <AlertDialogAction
              disabled={forgetting}
              onClick={(e) => {
                e.preventDefault();
                void forget();
              }}
            >
              {forgetting ? "正在忘记…" : "忘记"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        onOpenChange={(v) => {
          if (!v && !forgettingAll) {
            setForgetAllOpen(false);
          }
        }}
        open={forgetAllOpen}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>忘记全部记忆档案？</AlertDialogTitle>
            <AlertDialogDescription>
              画像、关于你、偏好、目标、账单/理财/生日经验、对话记忆、计划历史全部清空，Agent
              将回到"不认识你"的初始状态；之后的对话产生了新经验仍会重新记。进行中的长程任务不受影响。此操作不可撤销。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={forgettingAll}>
              先留着
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={forgettingAll}
              onClick={(e) => {
                e.preventDefault();
                void forgetAll();
              }}
            >
              {forgettingAll ? "正在忘记…" : "全部忘记"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      <AlertDialog
        onOpenChange={(v) => {
          if (!v && !endingTask) {
            setEndTaskOpen(false);
          }
        }}
        open={endTaskOpen}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>结束这个长程任务？</AlertDialogTitle>
            <AlertDialogDescription>
              任务「{task?.goal ?? ""}
              」将标记为已取消，进度与提醒停止；档案留痕不删除。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={endingTask}>继续存</AlertDialogCancel>
            <AlertDialogAction
              disabled={endingTask}
              onClick={(e) => {
                e.preventDefault();
                void endTask();
              }}
            >
              {endingTask ? "正在结束…" : "结束任务"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
