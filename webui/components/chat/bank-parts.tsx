"use client";

import {
  type ChangeEvent,
  type CSSProperties,
  type ReactNode,
  useCallback,
  useState,
} from "react";
import { ReceiptButton } from "@/components/chat/receipt-dialog";
import { AgentPlanVisualization } from "./agent-plan-visualization";
import { useActiveChat } from "@/hooks/use-active-chat";

/*
 * 银行 generative UI 部件 —— 与 agent/api.py 的 data-* 帧契约一一对应:
 *   data-transfer-confirmation  转账确认卡(金色)
 *   data-split-confirmation     AA 收款确认卡
 *   data-settle-confirmation    AA 成员结算确认卡
 *   data-contact-confirmation   收款人录入确认卡(姓名/手机号/备注)
 *   data-contact-choices        同名联系人选择(卡片式消歧)
 *   data-linkage-plan           跨场景联动计划确认卡(预算锁定闸门,金色系)
 *   data-linkage-action         联动到期动作确认卡(逐项购买闸门,金色系)
 *   data-plan                   生日预算计划确认卡(普通确认不扣款;取消订阅/锁定预算另过密码闸门)
 *   data-analysis-plan          Agent Core 只读分析计划卡(目标/事实行/步骤/复盘/上次记忆,无按钮)
 *   data-thinking               Agent Core 思维链(识别意图→调取记忆→计划→守卫→执行→复盘,可折叠面板)
 *   data-wealth-confirmation    理财确认卡(kind=subscribe|redeem;order=产品/金额/
 *                               费率/锁定期/风险/付款账户 或 持仓/估值/费用/到手)
 *   data-card-confirmation      卡片确认卡(kind=apply|limits|status;card=card_id/
 *                               尾号/类型/当前限额或状态/目标值;挂失加红字警示)
 *   data-subscription-list      订阅/代扣总览卡(逐项金额/下次扣费/年化+取消入口;
 *                               配套后台接入中,先经 /preview/subscriptions 演示)
 *   data-subscription-cancel    取消代扣确认卡(金色闸门,支付密码核验)
 *   data-ask-slot               缺槽提示
 *   data-gate-pending           本轮停在人工闸门(后端仍下发;前端不渲染提示胶囊)
 * 闸门答复 = 把「确认/取消」作为下一条用户消息发出(后端自动作为 resume 值恢复执行)。
 */

interface TransferCardData {
  amount_yuan?: string;
  confirm_hint?: string;
  memo?: string;
  order_id?: number;
  pay_required?: boolean;
  policy_note?: string;
  scheduled_at?: string | null;
  to_name?: string;
}

interface LinkageAction {
  amount_yuan?: string;
  days_before?: number;
  idx?: number;
  merchant?: string;
  run_at?: string | null;
  what?: string;
}

interface LinkagePlanCardData {
  actions?: LinkageAction[];
  budget_yuan?: string;
  event?: { title?: string; date?: string } | null;
  lock_order?: {
    order_id?: number;
    status?: string;
    amount_yuan?: string;
    from_account?: string;
    to_name?: string;
    memo?: string;
  } | null;
  pay_required?: boolean;
  plan_id?: number;
  progress?: string;
  status?: string;
  title?: string;
}

interface LinkageActionCardData extends LinkageAction {
  pay_required?: boolean;
  plan_id?: number;
  plan_title?: string;
}

/*
 * 生日预算计划卡(api.py confirm_plan → data-plan):
 * plan 视图冻结在 agent/plan.py::plan_card_view —— 只做拷贝,不含内部字段
 * (event_found/candidates 等绝不下发)。字段与 plan_card_view 一一对应,
 * 另由 api.py 追加 pay_required(恒 false:确认计划本身不扣款)。
 */
interface PlanFacts {
  days_left?: number;
  disposable_yuan?: string;
  event_date?: string;
  event_title?: string;
  expense_yuan?: string | null;
  formula?: string;
  income_yuan?: string | null;
  safety_buffer_yuan?: string;
  source?: string;
  upcoming_yuan?: string;
}

interface PlanStep {
  done?: boolean;
  gate?: string | null;
  idx?: number;
  risk?: string;
  title?: string;
}

/* 候选订阅视图(plan.py::_candidate_view 的键);selected_cut 额外带 reason */
interface PlanCutCandidate {
  amount_yuan?: string;
  annual_yuan?: string;
  category?: string;
  merchant?: string;
  next_charge_date?: string;
  period_text?: string;
  reason?: string;
  sub_id?: number;
}

interface PlanCardData {
  constraints?: string[];
  cut_candidates?: PlanCutCandidate[];
  facts?: PlanFacts;
  goal?: string;
  note?: string;
  pay_required?: boolean;
  plan_id?: string;
  plan_version?: number;
  recommended_budget_yuan?: string;
  requested_budget_yuan?: string;
  selected_cut?: PlanCutCandidate | null;
  shortfall_yuan?: string;
  steps?: PlanStep[];
}

interface ContactCandidate {
  name?: string;
  note?: string;
  phone?: string;
  relation?: string;
}

/*
 * Agent Core 只读分析计划卡(agent/analysis.py → data-analysis-plan)。
 * 与金色闸门卡的区别:它**没有任何按钮**——分析结论只做展示,动钱仍走各自闸门。
 * facts 是后端算好的事实行(label/value/hint),前端不做任何金额运算。
 */
interface AnalysisFactRow {
  hint?: string | null;
  label?: string;
  value?: string;
}

interface AnalysisStep {
  gate?: string | null;
  id?: string;
  risk?: string;
  skill?: string;
  status?: string;
  title?: string;
}

interface AnalysisPlanData {
  diff?: {
    added?: string[];
    changed?: Record<string, string[]>;
    removed?: string[];
  } | null;
  facts?: AnalysisFactRow[];
  goal?: string;
  hint?: string;
  intent?: string;
  memory_used?: string[];
  persona?: string;
  plan_id?: string;
  plan_version?: number;
  reflection?: { status?: string; summary?: string };
  steps?: AnalysisStep[];
  trace?: Array<{ at?: string; note?: string; phase?: string }>;
}

/*
 * Agent Core 思维链(agent/analysis.py → data-thinking)。
 * 把"识别意图 → 调取记忆 → 制定计划 → 安全守卫 → 执行 → 复盘"暴露给用户,
 * 让用户看到 Agent 是怎么想的。每个 thought 带 phase(阶段名)与 status(状态标记),
 * 前端据此着色。面板默认折叠,点击展开。
 */
interface ThinkingThought {
  phase?: string;
  status?: string;
  text?: string;
}

interface ThinkingData {
  thoughts?: ThinkingThought[];
}

/*
 * 理财闸门(api.py confirm_wealth → data-wealth-confirmation):
 * {kind: subscribe|redeem, order: ...}——order 视图由 graph.py _w_order_view 组装:
 * 申购带 product/code/amount_yuan/fee_yuan/lock_days/risk_level/from_account_name,
 * 赎回带 product/code/est_value_yuan/fee_yuan/redeem_net_yuan/risk_level。
 */
interface WealthOrder {
  amount_yuan?: string;
  code?: string;
  est_value_yuan?: string;
  fee_yuan?: string;
  from_account_name?: string;
  lock_days?: number;
  product?: string;
  redeem_net_yuan?: string;
  risk_level?: number | string;
}

interface WealthCardData {
  confirm_hint?: string;
  kind?: string;
  order?: WealthOrder;
  pay_required?: boolean;
}

/*
 * 卡片闸门(api.py confirm_card → data-card-confirmation):
 * {kind: apply|limits|status, card: ...}——card 视图由 graph.py k_apply/k_limits/
 * k_status 组装:apply 只有 card_type;limits 带当前/新限额对照;status 带
 * current_status/target(挂失 target=lost 时头部加红字警示,后端还有第二道闸)。
 */
interface CardInfoView {
  card_id?: number;
  card_type?: string;
  current_daily_limit_yuan?: string;
  current_per_tx_limit_yuan?: string;
  current_status?: string;
  kind?: string;
  new_daily_limit_yuan?: string | null;
  new_per_tx_limit_yuan?: string | null;
  tail?: string;
  target?: string;
}

interface CardConfirmationData {
  card?: CardInfoView;
  confirm_hint?: string;
  kind?: string;
  pay_required?: boolean;
}

const CARD_TYPE_CN: Record<string, string> = {
  credit: "信用卡",
  debit: "借记卡",
};

const CARD_STATUS_CN: Record<string, string> = {
  active: "正常",
  frozen: "冻结",
  locked: "锁定",
  lost: "挂失",
};

function cardTypeCn(t: string | undefined): string | undefined {
  return t ? (CARD_TYPE_CN[t] ?? t) : undefined;
}

function cardStatusCn(s: string | undefined): string | undefined {
  return s ? (CARD_STATUS_CN[s] ?? s) : undefined;
}

function riskLevelText(v: number | string | undefined): string | undefined {
  // 申购视图 risk_level 是产品库的数字等级(2),赎回已拼好 "R2" —— 统一成 R 标记
  if (v === undefined || v === "") {
    return;
  }
  return typeof v === "number" ? `R${v}` : v;
}

function useGateReply() {
  const { sendMessage, status } = useActiveChat();
  const [answered, setAnswered] = useState(false);
  const busy = status === "submitted" || status === "streaming";
  const reply = (text: string) => {
    if (answered || busy) {
      return;
    }
    setAnswered(true);
    sendMessage({ parts: [{ text, type: "text" }], role: "user" });
  };
  return { answered, busy, reply };
}

function GoldCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="w-[min(100%,420px)] rounded-2xl border border-amber-300/70 bg-gradient-to-br from-amber-50 to-yellow-50 p-4 shadow-[var(--shadow-card)] dark:border-amber-500/40 dark:from-amber-950/40 dark:to-yellow-950/30">
      <div className="mb-2 flex items-center gap-2 text-[13px] font-semibold text-amber-700 dark:text-amber-300">
        <span aria-hidden>🏦</span>
        {title}
      </div>
      {children}
    </div>
  );
}

function Row({ label, value }: { label: string; value: ReactNode }) {
  if (value === null || value === undefined || value === "") {
    return null;
  }
  return (
    <div className="flex items-baseline justify-between gap-4 text-[13px]">
      <span className="shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 text-right font-medium text-foreground [overflow-wrap:anywhere]">
        {value}
      </span>
    </div>
  );
}

/*
 * 闸门按钮组:
 * - 普通确认:回「确认」/「取消」。
 * - pay_required=true(动钱/敏感操作):「确认」前必须输入 6 位支付密码,
 *   输入满 6 位数字后按钮才可用,点击以密码值本身作为消息发出(后端把它
 *   作为 resume 值核验支付密码);「取消」行为不变。
 */
function ConfirmButtons({ payRequired = false }: { payRequired?: boolean }) {
  const { answered, busy, reply } = useGateReply();
  const [payPassword, setPayPassword] = useState("");
  const disabled = answered || busy;
  const payReady = /^\d{6}$/.test(payPassword);
  const handleCancel = useCallback(() => reply("取消"), [reply]);
  const handlePlainConfirm = useCallback(() => reply("确认"), [reply]);
  const handlePayConfirm = useCallback(() => {
    if (payReady) {
      reply(payPassword);
    }
  }, [payPassword, payReady, reply]);
  const handlePayChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    setPayPassword(e.target.value.replace(/\D/g, "").slice(0, 6));
  }, []);

  return (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      {payRequired ? (
        <input
          aria-label="支付密码"
          className="h-8 w-36 rounded-lg border border-amber-300/70 bg-white/80 px-2.5 text-sm tracking-[0.35em] text-foreground outline-none transition placeholder:text-xs placeholder:tracking-normal placeholder:text-muted-foreground focus:border-amber-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-amber-500/40 dark:bg-amber-950/40"
          disabled={disabled}
          inputMode="numeric"
          maxLength={6}
          onChange={handlePayChange}
          placeholder="支付密码"
          /* 不用 type=password:Chromium 会弹"保存密码/同步已暂停"系统
             提示(实测 Edge 演示时强干扰);text+text-security 同为圆点遮罩 */
          style={{ WebkitTextSecurity: "disc" } as CSSProperties}
          type="text"
          value={payPassword}
        />
      ) : null}
      <button
        className="rounded-lg bg-amber-500 px-4 py-1.5 text-sm font-medium text-white transition hover:bg-amber-600 disabled:cursor-not-allowed disabled:opacity-50"
        disabled={disabled || (payRequired && !payReady)}
        onClick={payRequired ? handlePayConfirm : handlePlainConfirm}
        type="button"
      >
        确认
      </button>
      <button
        className="rounded-lg border border-border/60 px-4 py-1.5 text-sm text-muted-foreground transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
        disabled={disabled}
        onClick={handleCancel}
        type="button"
      >
        取消
      </button>
    </div>
  );
}

function TransferCard({ data }: { data: TransferCardData }) {
  return (
    <GoldCard title="转账确认">
      <div className="flex flex-col gap-1.5">
        <Row label="收款人" value={data.to_name} />
        <Row
          label="金额"
          value={<span className="text-base">¥{data.amount_yuan}</span>}
        />
        <Row label="订单号" value={data.order_id} />
        <Row label="定时" value={data.scheduled_at} />
        <Row label="备注" value={data.memo} />
        {data.policy_note ? (
          <div className="mt-1 rounded-md bg-amber-100/60 px-2 py-1 text-xs text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">
            {data.policy_note}
          </div>
        ) : null}
      </div>
      <ConfirmButtons payRequired={data.pay_required === true} />
      <div className="mt-2 text-xs text-muted-foreground">
        {data.confirm_hint ?? "动钱前请再次确认"}
      </div>
      {/* 播报即凭证:每笔动钱随时可查完整审计链(只读) */}
      {data.order_id ? <ReceiptButton orderId={data.order_id} /> : null}
    </GoldCard>
  );
}

function GenericConfirmCard({
  data,
  labelMap,
  title,
}: {
  data: Record<string, unknown>;
  // 键名→中文标签;未命中的键原样显示(后端新增字段不至于丢信息)
  labelMap?: Record<string, string>;
  title: string;
}) {
  // pay_required 是闸门控制字段而非展示内容,不渲染成键值行
  const entries = Object.entries(data ?? {}).filter(
    ([k, v]) =>
      k !== "pay_required" &&
      v !== null &&
      v !== undefined &&
      typeof v !== "object"
  );
  return (
    <GoldCard title={title}>
      <div className="flex flex-col gap-1.5">
        {entries.map(([k, v]) => (
          <Row key={k} label={labelMap?.[k] ?? k} value={String(v)} />
        ))}
      </div>
      <ConfirmButtons payRequired={data.pay_required === true} />
    </GoldCard>
  );
}

function ContactCard({ data }: { data: ContactCandidate }) {
  return (
    <GoldCard title="收款人录入确认">
      <div className="flex flex-col gap-1.5">
        <Row label="姓名" value={data.name} />
        <Row label="手机号" value={data.phone} />
        <Row label="备注" value={data.note || "无"} />
      </div>
      <ConfirmButtons />
      <div className="mt-2 text-xs text-muted-foreground">
        确认后保存为你的常用收款人,之后可直接按姓名转账
      </div>
    </GoldCard>
  );
}

function LinkagePlanCard({ data }: { data: LinkagePlanCardData }) {
  const actions = data.actions ?? [];
  const lock = data.lock_order ?? {};
  return (
    <GoldCard title="跨场景联动计划确认">
      <div className="flex flex-col gap-1.5">
        <Row label="计划" value={data.title} />
        <Row
          label="事件"
          value={
            data.event?.date
              ? `${data.event?.title ?? ""} ${data.event.date}`
              : data.event?.title
          }
        />
        <Row
          label="预算"
          value={<span className="text-base">¥{data.budget_yuan}</span>}
        />
        <div className="mt-1 flex flex-col gap-1">
          {actions.map((action, index) => (
            <div
              className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-0.5 rounded-md bg-amber-100/50 px-2 py-1 text-[13px] dark:bg-amber-900/20"
              key={action.idx ?? index}
            >
              <span className="min-w-0 font-medium [overflow-wrap:anywhere]">
                {action.what}
                {action.merchant ? `(${action.merchant})` : ""}
              </span>
              <span className="text-right text-muted-foreground [overflow-wrap:anywhere]">
                ¥{action.amount_yuan} · 提前 {action.days_before} 天
                {action.run_at ? ` · ${action.run_at.slice(0, 10)}` : ""}
              </span>
            </div>
          ))}
        </div>
        <Row
          label="预算锁定单"
          value={
            lock.order_id
              ? `#${lock.order_id} ${lock.status ?? ""} ${
                  lock.from_account ? `(${lock.from_account} 预留)` : ""
                }`
              : undefined
          }
        />
      </div>
      <ConfirmButtons payRequired={data.pay_required === true} />
      <div className="mt-2 text-xs text-muted-foreground">
        确认后执行预算锁定单(从活期预留到理财专户);取消则撤销整个计划与提醒
      </div>
    </GoldCard>
  );
}

function LinkageActionCard({ data }: { data: LinkageActionCardData }) {
  return (
    <GoldCard title="联动到期购买确认">
      <div className="flex flex-col gap-1.5">
        <Row label="计划" value={data.plan_title} />
        <Row label="事项" value={data.what} />
        <Row label="商户" value={data.merchant} />
        <Row
          label="金额"
          value={<span className="text-base">¥{data.amount_yuan}</span>}
        />
      </div>
      <ConfirmButtons payRequired={data.pay_required === true} />
      <div className="mt-2 text-xs text-muted-foreground">
        确认后按计划真实扣款购买;取消则跳过这一项
      </div>
    </GoldCard>
  );
}

/* 计划步骤的风险标签(plan.py build_plan 的 risk 取值:READ/LOW/HIGH) */
const PLAN_RISK_STYLE: Record<string, { className: string; label: string }> = {
  HIGH: {
    className:
      "bg-red-100/70 text-red-700 dark:bg-red-950/40 dark:text-red-300",
    label: "动钱",
  },
  LOW: {
    className:
      "bg-blue-100/70 text-blue-700 dark:bg-blue-950/40 dark:text-blue-300",
    label: "提醒",
  },
  READ: {
    className:
      "bg-emerald-100/70 text-emerald-700 dark:bg-emerald-950/40 dark:text-emerald-300",
    label: "只读",
  },
};

/* facts.source → 人话(与 plan.py compute_disposable 的三种口径一一对应) */
const PLAN_SOURCE_CN: Record<string, string> = {
  balance: "活期余额(月报不可用,已回退)",
  monthly_report: "本月月报",
  prev_month: "上月整月(本月工资未到账)",
};

function planDaysText(daysLeft: number | undefined): string {
  if (daysLeft === undefined) {
    return "";
  }
  if (daysLeft < 0) {
    return "已过期";
  }
  if (daysLeft === 0) {
    return "今天";
  }
  return daysLeft === 1 ? "明天" : `${daysLeft} 天后`;
}

function PlanCard({ data }: { data: PlanCardData }) {
  const facts = data.facts ?? {};
  const steps = data.steps ?? [];
  const candidates = data.cut_candidates ?? [];
  const selected = data.selected_cut ?? null;
  const eventText =
    [facts.event_title, facts.event_date, planDaysText(facts.days_left)]
      .filter(Boolean)
      .join(" ") || undefined;
  const shortfallText =
    data.shortfall_yuan && data.shortfall_yuan !== "0.00"
      ? `¥${data.shortfall_yuan}`
      : undefined;
  // 1-9 号本月工资未到账时,plan.py 回退用上月整月收支;标签必须跟着口径走,
  // 否则卡片数字和同一张卡的公式("上月收入…")自相矛盾。
  const prevMonth = facts.source === "prev_month";
  return (
    <GoldCard title="生日预算计划确认">
      <div className="flex flex-col gap-1.5">
        <Row label="目标" value={data.goal} />
        <Row
          label="约束"
          value={data.constraints?.length ? data.constraints.join(" · ") : "无"}
        />
        <Row label="事件" value={eventText} />
        <Row
          label="数据依据"
          value={PLAN_SOURCE_CN[facts.source ?? ""] ?? facts.source}
        />
        <Row
          label={prevMonth ? "上月收入" : "本月收入"}
          value={facts.income_yuan && `¥${facts.income_yuan}`}
        />
        <Row
          label={prevMonth ? "上月已支出" : "本月已支出"}
          value={facts.expense_yuan && `¥${facts.expense_yuan}`}
        />
        <Row
          label="生日前订阅扣款"
          value={facts.upcoming_yuan && `¥${facts.upcoming_yuan}`}
        />
        <Row
          label="安全垫"
          value={facts.safety_buffer_yuan && `¥${facts.safety_buffer_yuan}`}
        />
        <Row
          label="可支配额度"
          value={
            facts.disposable_yuan && (
              <span className="text-base">¥{facts.disposable_yuan}</span>
            )
          }
        />
      </div>
      {facts.formula ? (
        <div className="mt-1 rounded-md bg-white/60 px-2 py-1 text-xs text-muted-foreground [overflow-wrap:anywhere] dark:bg-amber-950/30">
          {facts.formula}
        </div>
      ) : null}
      <div className="mt-2 flex flex-col gap-1 rounded-lg bg-amber-100/50 px-2 py-1.5 dark:bg-amber-900/20">
        <Row
          label="原预算"
          value={data.requested_budget_yuan && `¥${data.requested_budget_yuan}`}
        />
        <Row
          label="建议锁定"
          value={
            <span className="text-base">
              ¥{data.recommended_budget_yuan ?? "—"}
            </span>
          }
        />
        <Row label="差额" value={shortfallText} />
      </div>
      {steps.length ? (
        <div className="mt-2 flex flex-col gap-1">
          <div className="text-xs text-muted-foreground">执行步骤</div>
          {steps.map((step, index) => {
            const risk = PLAN_RISK_STYLE[step.risk ?? ""] ?? {
              className: "bg-muted text-muted-foreground",
              label: step.risk || "未知",
            };
            return (
              <div
                className="flex items-start justify-between gap-3 rounded-md bg-amber-100/50 px-2 py-1 text-[13px] dark:bg-amber-900/20"
                key={step.idx ?? index}
              >
                <span className="min-w-0 [overflow-wrap:anywhere]">
                  <span className="mr-1 text-muted-foreground">
                    {index + 1}.
                  </span>
                  {step.title}
                  {step.done ? (
                    <span className="ml-1 text-[11px] text-emerald-700 dark:text-emerald-300">
                      已完成
                    </span>
                  ) : null}
                </span>
                <span
                  className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] ${risk.className}`}
                >
                  {risk.label}
                </span>
              </div>
            );
          })}
        </div>
      ) : null}
      {candidates.length ? (
        <div className="mt-2 flex flex-col gap-1">
          <div className="text-xs text-muted-foreground">
            可取消的会员订阅(只列真实会员)
          </div>
          {candidates.map((candidate, index) => {
            const picked =
              candidate.sub_id !== undefined &&
              selected?.sub_id === candidate.sub_id;
            return (
              <div
                className={`rounded-md border px-2 py-1 text-[13px] ${
                  picked
                    ? "border-amber-400/80 bg-amber-100/70 dark:border-amber-500/40 dark:bg-amber-900/30"
                    : "border-transparent bg-amber-100/40 dark:bg-amber-900/10"
                }`}
                key={candidate.sub_id ?? candidate.merchant ?? index}
              >
                <div className="flex items-baseline justify-between gap-3">
                  <span className="min-w-0 font-medium [overflow-wrap:anywhere]">
                    {candidate.merchant}
                    {picked ? (
                      <span className="ml-1 rounded-full bg-amber-500 px-1.5 py-0.5 text-[10px] font-normal text-white">
                        计划取消
                      </span>
                    ) : null}
                  </span>
                  <span className="shrink-0 text-right text-muted-foreground">
                    ¥{candidate.amount_yuan}/{candidate.period_text ?? "期"}
                  </span>
                </div>
                <div className="mt-0.5 flex flex-wrap items-baseline justify-between gap-x-3 text-[11px] text-muted-foreground">
                  <span>下次扣费 {candidate.next_charge_date ?? "—"}</span>
                  <span>年化 ¥{candidate.annual_yuan ?? "—"}</span>
                </div>
              </div>
            );
          })}
        </div>
      ) : null}
      {selected?.reason ? (
        <div className="mt-1 rounded-md bg-amber-100/60 px-2 py-1 text-xs text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">
          {selected.reason}
        </div>
      ) : null}
      {data.note ? (
        <div className="mt-1 rounded-md bg-amber-100/60 px-2 py-1 text-xs text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">
          {data.note}
        </div>
      ) : null}
      {/* 计划闸门是普通确认(pay_required 恒 false):确认本身不扣款 */}
      <ConfirmButtons payRequired={false} />
      <div className="mt-2 text-xs text-muted-foreground">
        确认计划不扣款，取消订阅/锁定预算仍需支付密码。回复「预算改成800」可调整预算，回复「取消」放弃。
      </div>
    </GoldCard>
  );
}

function WealthCard({ data }: { data: WealthCardData }) {
  const order = data.order ?? {};
  const productName = `${order.product ?? ""}${
    order.code ? `(${order.code})` : ""
  }`;
  return (
    <GoldCard title={data.kind === "redeem" ? "理财赎回确认" : "理财申购确认"}>
      <div className="flex flex-col gap-1.5">
        <Row
          label={data.kind === "redeem" ? "持仓产品" : "产品"}
          value={productName}
        />
        {data.kind === "redeem" ? (
          <>
            <Row
              label="当前估值"
              value={<span className="text-base">¥{order.est_value_yuan}</span>}
            />
            <Row
              label="预计费用"
              value={order.fee_yuan && `¥${order.fee_yuan}`}
            />
            <Row
              label="预计到手"
              value={
                order.redeem_net_yuan && (
                  <span className="text-base">¥{order.redeem_net_yuan}</span>
                )
              }
            />
          </>
        ) : (
          <>
            <Row
              label="金额"
              value={<span className="text-base">¥{order.amount_yuan}</span>}
            />
            <Row
              label="预计费用"
              value={order.fee_yuan && `¥${order.fee_yuan}`}
            />
            <Row
              label="锁定期"
              value={order.lock_days !== undefined && `${order.lock_days} 天`}
            />
            <Row label="付款账户" value={order.from_account_name} />
          </>
        )}
        <Row label="风险等级" value={riskLevelText(order.risk_level)} />
      </div>
      <ConfirmButtons payRequired={data.pay_required === true} />
      <div className="mt-2 text-xs text-muted-foreground">
        {data.confirm_hint ?? "回复「确认」执行;回复「取消」放弃"}
      </div>
    </GoldCard>
  );
}

function CardConfirmCard({ data }: { data: CardConfirmationData }) {
  const card = data.card ?? {};
  const kind = data.kind ?? card.kind;
  const lost = kind === "status" && card.target === "lost";
  return (
    <GoldCard
      title={
        kind === "apply"
          ? "办卡确认"
          : kind === "limits"
            ? "限额调整确认"
            : "卡片状态确认"
      }
    >
      {lost ? (
        <div className="mb-2 rounded-md bg-red-100/70 px-2 py-1 text-xs font-medium text-red-700 dark:bg-red-950/40 dark:text-red-300">
          挂失不可逆,已要求双重确认
        </div>
      ) : null}
      <div className="flex flex-col gap-1.5">
        {kind === "apply" ? (
          <Row label="卡类型" value={cardTypeCn(card.card_type)} />
        ) : (
          <>
            <Row label="卡号尾号" value={card.tail} />
            <Row label="类型" value={cardTypeCn(card.card_type)} />
          </>
        )}
        {kind === "limits" ? (
          <>
            <Row
              label="日限额"
              value={
                card.new_daily_limit_yuan &&
                `¥${card.current_daily_limit_yuan ?? "?"} → ¥${card.new_daily_limit_yuan}`
              }
            />
            <Row
              label="单笔限额"
              value={
                card.new_per_tx_limit_yuan &&
                `¥${card.current_per_tx_limit_yuan ?? "?"} → ¥${card.new_per_tx_limit_yuan}`
              }
            />
          </>
        ) : null}
        {kind === "status" ? (
          <>
            <Row label="当前状态" value={cardStatusCn(card.current_status)} />
            <Row label="目标状态" value={cardStatusCn(card.target)} />
          </>
        ) : null}
      </div>
      <ConfirmButtons payRequired={data.pay_required === true} />
      <div className="mt-2 text-xs text-muted-foreground">
        {data.confirm_hint ?? "回复「确认」执行;回复「取消」放弃"}
      </div>
    </GoldCard>
  );
}

function ContactChoiceRow({
  busy,
  candidate,
  reply,
}: {
  busy: boolean;
  candidate: ContactCandidate;
  reply: (text: string) => void;
}) {
  const handlePick = useCallback(() => {
    reply(
      `转给${candidate.name ?? ""}${candidate.phone ? `,手机 ${candidate.phone}` : ""}`
    );
  }, [candidate, reply]);
  return (
    <button
      className="flex items-center justify-between gap-3 rounded-lg border border-border/50 px-3 py-2 text-left text-[13px] transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
      disabled={busy}
      onClick={handlePick}
      type="button"
    >
      <span className="min-w-0 font-medium [overflow-wrap:anywhere]">
        {candidate.name}
      </span>
      <span className="text-right text-muted-foreground [overflow-wrap:anywhere]">
        {[candidate.relation, candidate.note].filter(Boolean).join(" · ") ||
          candidate.phone}
      </span>
    </button>
  );
}

function ContactChoices({
  data,
}: {
  data: { candidates?: ContactCandidate[] };
}) {
  const { answered, busy, reply } = useGateReply();
  const candidates = data.candidates ?? [];
  return (
    <div className="w-[min(100%,420px)] rounded-2xl border border-border/50 bg-card p-4 shadow-[var(--shadow-card)]">
      <div className="mb-2 text-[13px] font-semibold text-foreground">
        找到多位联系人,请选择转账对象:
      </div>
      <div className="flex flex-col gap-2">
        {candidates.map((candidate) => (
          <ContactChoiceRow
            busy={answered || busy}
            candidate={candidate}
            key={`${candidate.name}-${candidate.phone}`}
            reply={reply}
          />
        ))}
      </div>
    </div>
  );
}

/* AA 收款确认卡(定制版):GenericConfirmCard 会把 participants 数组整行丢掉、
 * 剩余字段直出英文键名(title/total_yuan),这里换成中文标签+参与者明细。 */
const SETTLE_LABELS: Record<string, string> = {
  bill_id: "账单编号",
  contact_name: "结算成员",
  share_yuan: "分摊金额",
  title: "账单名称",
};

interface SplitCardData {
  participants?: { name?: string; share_yuan?: string }[];
  pay_required?: boolean;
  title?: string;
  total_yuan?: string;
}

function SplitCard({ data }: { data: SplitCardData }) {
  return (
    <GoldCard title="AA 收款确认">
      <div className="flex flex-col gap-1.5">
        <Row label="账单名称" value={data.title} />
        <Row
          label="总金额"
          value={<span className="text-base">¥{data.total_yuan}</span>}
        />
      </div>
      {data.participants?.length ? (
        <div className="mt-2 flex flex-col gap-1">
          <div className="text-xs text-muted-foreground">参与人分摊</div>
          {data.participants.map((p) => (
            <div
              className="flex justify-between gap-4 rounded-md bg-amber-100/40 px-2 py-1 text-[13px] dark:bg-amber-900/20"
              key={`${p.name}-${p.share_yuan}`}
            >
              <span className="text-foreground">{p.name}</span>
              <span className="font-medium text-foreground">
                ¥{p.share_yuan}
              </span>
            </div>
          ))}
        </div>
      ) : null}
      <ConfirmButtons payRequired={data.pay_required === true} />
    </GoldCard>
  );
}

/*
 * 订阅/代扣场景部件(赛题六场景最后一个;前端先行,配套后台接入前用
 * /preview/subscriptions 假数据演示,字段结构与将来编排层输出对齐):
 *   data-subscription-list   订阅总览卡:逐项金额/下次扣费/年化 + 一键取消入口
 *   data-subscription-cancel 取消代扣确认卡:金色闸门,需支付密码
 */
export interface SubscriptionItem {
  amount_yuan?: string;
  annual_yuan?: string;
  category?: string;
  merchant_name?: string;
  next_charge_date?: string;
  note?: string;
  period_text?: string;
}

interface SubscriptionListData {
  annual_total_yuan?: string;
  hint?: string;
  items?: SubscriptionItem[];
  monthly_total_yuan?: string;
}

interface SubscriptionCancelData extends SubscriptionItem {
  confirm_hint?: string;
  pay_required?: boolean;
}

/* 单条订阅行(提取成组件:循环里的 onClick 需要各自的稳定回调) */
function SubscriptionRow({
  disabled,
  item,
  onCancel,
}: {
  disabled: boolean;
  item: SubscriptionItem;
  onCancel: (item: SubscriptionItem) => void;
}) {
  const handleClick = useCallback(() => onCancel(item), [item, onCancel]);
  return (
    <div className="rounded-xl border border-amber-200/60 bg-white/60 p-2.5 dark:border-amber-500/20 dark:bg-amber-950/20">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate text-[13px] font-medium text-foreground">
            {item.merchant_name}
          </span>
          {item.category ? (
            <span className="shrink-0 rounded-full bg-amber-100/80 px-1.5 py-0.5 text-[10px] text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
              {item.category}
            </span>
          ) : null}
        </div>
        <span className="shrink-0 text-[13px] font-semibold text-foreground">
          ¥{item.amount_yuan}
          <span className="text-[11px] font-normal text-muted-foreground">
            /{item.period_text ?? "期"}
          </span>
        </span>
      </div>
      <div className="mt-1 flex items-center justify-between gap-3 text-[11px] text-muted-foreground">
        <span className="min-w-0 [overflow-wrap:anywhere]">
          下次扣费 {item.next_charge_date ?? "—"} · 年化 ¥
          {item.annual_yuan ?? "—"}
        </span>
        <button
          className="shrink-0 rounded-md border border-amber-300/70 px-2 py-0.5 text-[11px] text-amber-700 transition hover:bg-amber-100/70 disabled:cursor-not-allowed disabled:opacity-50 dark:border-amber-500/40 dark:text-amber-300 dark:hover:bg-amber-900/30"
          disabled={disabled}
          onClick={handleClick}
          type="button"
        >
          取消代扣
        </button>
      </div>
      {item.note ? (
        <div className="mt-1 text-[11px] text-amber-700 dark:text-amber-300">
          {item.note}
        </div>
      ) : null}
    </div>
  );
}

/* 卡体(与聊天上下文无关的部分):列表 + 合计 */
function SubscriptionListBody({
  data,
  disabled,
  onCancel,
}: {
  data: SubscriptionListData;
  disabled: boolean;
  onCancel: (item: SubscriptionItem) => void;
}) {
  const items = data.items ?? [];
  return (
    <GoldCard title="订阅/代扣总览">
      <div className="flex flex-col gap-2">
        {items.map((item) => (
          <SubscriptionRow
            disabled={disabled}
            item={item}
            key={item.merchant_name}
            onCancel={onCancel}
          />
        ))}
      </div>
      <div className="mt-2.5 flex flex-col gap-1 border-t border-amber-200/60 pt-2 dark:border-amber-500/20">
        <Row
          label="本月合计"
          value={<span className="text-base">¥{data.monthly_total_yuan}</span>}
        />
        <Row label="年化合计" value={`¥${data.annual_total_yuan ?? "—"}`} />
      </div>
      {data.hint ? (
        <div className="mt-2 text-xs text-muted-foreground">{data.hint}</div>
      ) : null}
    </GoldCard>
  );
}

/* 聊天模式外壳:取消入口 = 把意图作为下一条用户消息发出(需聊天上下文,
 * 与其它闸门卡同规则) */
function SubscriptionListChat({ data }: { data: SubscriptionListData }) {
  const { answered, busy, reply } = useGateReply();
  const handleCancel = useCallback(
    (item: SubscriptionItem) =>
      reply(`取消${item.merchant_name ?? ""}的自动扣费`),
    [reply]
  );
  return (
    <SubscriptionListBody
      data={data}
      disabled={answered || busy}
      onCancel={handleCancel}
    />
  );
}

/* ---- 代扣预审(事前知情):即将扣费清单 + 放行/跳过/解约三决策 ---- */
interface PrecheckItem {
  amount_yuan?: string;
  base_amount_yuan?: string;
  category?: string;
  days_left?: number;
  merchant_name?: string;
  next_charge_date?: string;
  period_text?: string;
  price_note?: string;
  sub_id?: number;
}

interface PrecheckView {
  hint?: string;
  items?: PrecheckItem[];
}

function _whenText(daysLeft: number | undefined): string {
  const d = daysLeft ?? 0;
  if (d <= 0) {
    return "今天";
  }
  if (d === 1) {
    return "明天";
  }
  return `${d} 天后`;
}

function PrecheckCard({ data }: { data: PrecheckView }) {
  const { answered, busy, reply } = useGateReply();
  const disabled = answered || busy;
  const make = useCallback(
    (item: PrecheckItem, verb: string) => () =>
      reply(`${verb}${item.merchant_name ?? ""}的本期代扣`),
    [reply]
  );
  const makeCancel = useCallback(
    (item: PrecheckItem) => () =>
      reply(`取消${item.merchant_name ?? ""}的自动扣费`),
    [reply]
  );
  const items = data.items ?? [];
  return (
    <div className="w-[min(100%,420px)] rounded-2xl border border-amber-300/70 bg-gradient-to-br from-amber-50 to-yellow-50 p-4 shadow-[var(--shadow-card)] dark:border-amber-500/40 dark:from-amber-950/40 dark:to-yellow-950/30">
      <div className="mb-2 flex items-center gap-2 text-[13px] font-semibold text-amber-700 dark:text-amber-300">
        <span aria-hidden>🔔</span>
        代扣预审 · 即将扣费 {items.length} 项
      </div>
      <div className="flex flex-col gap-2">
        {items.map((item) => (
          <div
            className="rounded-xl border border-amber-200/60 bg-white/60 p-2.5 dark:border-amber-500/20 dark:bg-amber-950/20"
            key={item.sub_id ?? item.merchant_name}
          >
            <div className="flex items-center justify-between gap-3">
              <div className="flex min-w-0 items-center gap-2">
                <span className="truncate text-[13px] font-medium text-foreground">
                  {item.merchant_name}
                </span>
                {item.category ? (
                  <span className="shrink-0 rounded-full bg-amber-100/80 px-1.5 py-0.5 text-[10px] text-amber-700 dark:bg-amber-900/40 dark:text-amber-300">
                    {item.category}
                  </span>
                ) : null}
              </div>
              <span className="shrink-0 text-[13px] font-semibold text-foreground">
                ¥{item.amount_yuan}
                <span className="text-[11px] font-normal text-muted-foreground">
                  /{item.period_text ?? "期"}
                </span>
              </span>
            </div>
            <div className="mt-1 flex items-center justify-between gap-3 text-[11px] text-muted-foreground">
              <span className="min-w-0 [overflow-wrap:anywhere]">
                {_whenText(item.days_left)}扣费({item.next_charge_date})
                {item.price_note ? ` · ${item.price_note}` : ""}
              </span>
            </div>
            <div className="mt-2 flex flex-wrap gap-2">
              <button
                className="rounded-lg bg-amber-500 px-3 py-1 text-xs font-medium text-white transition hover:bg-amber-600 disabled:cursor-not-allowed disabled:opacity-50"
                disabled={disabled}
                onClick={make(item, "放行")}
                type="button"
              >
                放行本期
              </button>
              <button
                className="rounded-lg border border-amber-300/70 px-3 py-1 text-xs text-amber-700 transition hover:bg-amber-100/70 disabled:cursor-not-allowed disabled:opacity-50 dark:border-amber-500/40 dark:text-amber-300 dark:hover:bg-amber-900/30"
                disabled={disabled}
                onClick={make(item, "跳过")}
                type="button"
              >
                跳过本期
              </button>
              <button
                className="rounded-lg border border-border/60 px-3 py-1 text-xs text-muted-foreground transition hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
                disabled={disabled}
                onClick={makeCancel(item)}
                type="button"
              >
                解约代扣
              </button>
            </div>
          </div>
        ))}
      </div>
      {data.hint ? (
        <div className="mt-2 text-xs text-muted-foreground">{data.hint}</div>
      ) : null}
    </div>
  );
}

export function SubscriptionListCard({
  data,
  onPickCancel,
}: {
  data: SubscriptionListData;
  /* 演示页注入的本地回调;不传(生产)=把「取消 XX 的自动扣费」作为
   * 下一条用户消息发出,由编排层接管。传了则完全不碰聊天上下文
   * (演示页没有 ActiveChatProvider,hook 一调就 throw)。 */
  onPickCancel?: (item: SubscriptionItem) => void;
}) {
  if (onPickCancel) {
    return (
      <SubscriptionListBody
        data={data}
        disabled={false}
        onCancel={onPickCancel}
      />
    );
  }
  return <SubscriptionListChat data={data} />;
}

/* 取消代扣确认卡:默认走聊天闸门(ConfirmButtons);演示页传 onResolve
 * 时切换为本地交互(自持密码框,不依赖 useChat 上下文)。 */
export function SubscriptionCancelCard({
  data,
  onResolve,
}: {
  data: SubscriptionCancelData;
  /* 本地模式回调:确认传 6 位密码,取消传 "cancel" */
  onResolve?: (answer: string) => void;
}) {
  const [payPassword, setPayPassword] = useState("");
  const payReady = /^\d{6}$/.test(payPassword);
  const handleLocalConfirm = useCallback(() => {
    if (payReady) {
      onResolve?.(payPassword);
    }
  }, [onResolve, payPassword, payReady]);
  const handleLocalCancel = useCallback(
    () => onResolve?.("cancel"),
    [onResolve]
  );
  const handleLocalChange = useCallback((e: ChangeEvent<HTMLInputElement>) => {
    setPayPassword(e.target.value.replace(/\D/g, "").slice(0, 6));
  }, []);
  const localControls = onResolve ? (
    <div className="mt-3 flex flex-wrap items-center gap-2">
      <input
        aria-label="支付密码"
        className="h-8 w-36 rounded-lg border border-amber-300/70 bg-white/80 px-2.5 text-sm tracking-[0.35em] text-foreground outline-none transition placeholder:text-xs placeholder:tracking-normal placeholder:text-muted-foreground focus:border-amber-500 dark:border-amber-500/40 dark:bg-amber-950/40"
        inputMode="numeric"
        maxLength={6}
        onChange={handleLocalChange}
        placeholder="支付密码"
        style={{ WebkitTextSecurity: "disc" } as CSSProperties}
        type="text"
        value={payPassword}
      />
      <button
        className="rounded-lg bg-amber-500 px-4 py-1.5 text-sm font-medium text-white transition hover:bg-amber-600 disabled:cursor-not-allowed disabled:opacity-50"
        disabled={!payReady}
        onClick={handleLocalConfirm}
        type="button"
      >
        确认取消
      </button>
      <button
        className="rounded-lg border border-border/60 px-4 py-1.5 text-sm text-muted-foreground transition hover:bg-muted"
        onClick={handleLocalCancel}
        type="button"
      >
        取消
      </button>
    </div>
  ) : (
    <ConfirmButtons payRequired={data.pay_required !== false} />
  );
  return (
    <GoldCard title="取消代扣确认">
      <div className="flex flex-col gap-1.5">
        <Row label="商户" value={data.merchant_name} />
        <Row label="分类" value={data.category} />
        <Row
          label="每期扣费"
          value={`¥${data.amount_yuan ?? "—"} /${data.period_text ?? "期"}`}
        />
        <Row label="下次扣费" value={data.next_charge_date} />
        <Row label="取消后年省" value={`¥${data.annual_yuan ?? "—"}`} />
      </div>
      {data.note ? (
        <div className="mt-1 rounded-md bg-amber-100/60 px-2 py-1 text-xs text-amber-700 dark:bg-amber-900/30 dark:text-amber-300">
          {data.note}
        </div>
      ) : null}
      {localControls}
      <div className="mt-2 text-xs text-muted-foreground">
        {data.confirm_hint ?? "取消后代扣协议立即生效终止,已有会员期不受影响"}
      </div>
    </GoldCard>
  );
}

const SLOT_LABELS: Record<string, string> = {
  action: "想办的业务",
  actions: "要准备的东西",
  amount: "金额",
  amount_yuan: "金额",
  ask: "想看的报告",
  card: "卡片",
  card_id: "卡片",
  card_type: "卡类型",
  contact: "收款人",
  date: "日期",
  event_date: "事件日期",
  holding: "赎回的持仓",
  keyword: "产品关键词",
  limits: "新限额",
  // 2026-10-26 补漏:联动预算/AA 份额与参与人/定时时刻/总金额/商户此前
  // 漏登记,缺槽提示会把 budget_yuan 这类英文键裸露给用户(实测截图)
  budget_yuan: "预算",
  merchant: "商户",
  participants: "参与人",
  scheduled_at: "定时时刻",
  shares: "分摊份额",
  total_yuan: "总金额",
  members: "参与人",
  name: "姓名",
  note: "备注",
  payee: "收款人",
  period: "时间范围",
  phone: "手机号",
  product: "产品",
  status_target: "目标状态",
  time: "时间",
  title: "标题",
  to: "收款人",
};

function AskSlot({ data }: { data: { missing?: string[]; options?: string[] } }) {
  const { answered, busy, reply } = useGateReply();
  const [other, setOther] = useState("");
  const disabled = answered || busy;
  const missing = (data.missing ?? []).map((slot) =>
    // 风险测评缺槽令牌带题号(assess_qN,graph.py w_extract 注释),统一显示中文
    slot.startsWith("assess_q") ? "风险测评答案" : (SLOT_LABELS[slot] ?? slot)
  );
  const options = data.options ?? [];
  const sendOther = useCallback(() => {
    const text = other.trim();
    if (text) {
      reply(text);
    }
  }, [other, reply]);
  return (
    <div className="w-fit max-w-full rounded-xl border border-blue-200/70 bg-blue-50/60 px-3 py-2 text-[13px] text-blue-700 dark:border-blue-500/30 dark:bg-blue-950/30 dark:text-blue-300">
      还差:{missing.join("、")}
      {options.length > 0 ? (
        <>
          {/*
           * 枚举型反问:选项点击即以该文本回话(免打字,走 useGateReply 与
           * 闸门卡同一条发送链路);底部固定一个「其他」自由输入区——选项
           * 没覆盖的需求仍可用自然语言描述,两种入口二选一。
           */}
          <div className="mt-2 flex flex-wrap gap-1.5">
            {options.map((option) => (
              <button
                className="rounded-full border border-blue-300/80 bg-white/80 px-3 py-1 text-[12px] transition hover:border-blue-500 hover:bg-blue-100 disabled:cursor-not-allowed disabled:opacity-50 dark:border-blue-500/40 dark:bg-blue-900/30 dark:hover:bg-blue-900/60"
                disabled={disabled}
                key={option}
                onClick={() => reply(option)}
                type="button"
              >
                {option}
              </button>
            ))}
          </div>
          <div className="mt-2 flex items-center gap-1.5">
            <input
              className="min-w-0 flex-1 rounded-lg border border-blue-300/70 bg-white/80 px-2 py-1 text-[12px] outline-none transition placeholder:text-blue-400 focus:border-blue-500 disabled:opacity-50 dark:border-blue-500/40 dark:bg-blue-950/40"
              disabled={disabled}
              onChange={(e) => setOther(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  sendOther();
                }
              }}
              placeholder="其他:直接输入你想办的业务"
              value={other}
            />
            <button
              className="shrink-0 rounded-lg bg-blue-600 px-2.5 py-1 text-[12px] text-white transition hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50"
              disabled={disabled || !other.trim()}
              onClick={sendOther}
              type="button"
            >
              发送
            </button>
          </div>
        </>
      ) : (
        <span className="ml-1">直接回复补充即可</span>
      )}
    </div>
  );
}

const ANALYSIS_STATUS_CN: Record<string, string> = {
  blocked: "被拦下",
  delegated: "已转交链路",
  done: "已完成",
  failed: "未通过",
  pending: "待执行",
  running: "执行中",
  skipped: "已跳过",
};

const ANALYSIS_RISK_STYLE: Record<
  string,
  { className: string; label: string }
> = {
  HIGH: { className: "bg-red-100 text-red-700 dark:bg-red-950/40 dark:text-red-300", label: "需密码" },
  LOW: { className: "bg-muted text-muted-foreground", label: "低" },
  MED: { className: "bg-orange-100 text-orange-700 dark:bg-orange-950/40 dark:text-orange-300", label: "中" },
  READ: { className: "bg-sky-100 text-sky-700 dark:bg-sky-950/40 dark:text-sky-300", label: "只读" },
};

/*
 * Agent Core 只读分析计划卡:目标 → 事实行 → 步骤 → 复盘摘要 → 上次记忆。
 * 只读,不带任何确认按钮:要取消订阅/申购请另行发起,仍要过支付密码。
 */
function AnalysisPlanCard({ data }: { data: AnalysisPlanData }) {
  const facts = data.facts ?? [];
  const steps = data.steps ?? [];
  const memory = data.memory_used ?? [];
  const reflection = data.reflection ?? {};
  const diff = data.diff;
  // 重规划 diff:只展示"改了哪些步骤",不渲染内部字段
  const diffText = diff
    ? [
        diff.changed && Object.keys(diff.changed).length
          ? `修改步骤 ${Object.keys(diff.changed).join("、")}`
          : "",
        diff.added?.length ? `新增步骤 ${diff.added.join("、")}` : "",
        diff.removed?.length ? `删除步骤 ${diff.removed.join("、")}` : "",
      ]
        .filter(Boolean)
        .join("；")
    : "";
  return (
    <div className="w-[min(100%,420px)] rounded-2xl border border-sky-200/70 bg-gradient-to-br from-sky-50 to-blue-50 p-4 shadow-[var(--shadow-card)] dark:border-sky-500/30 dark:from-sky-950/30 dark:to-blue-950/20">
      <div className="mb-2 flex items-center gap-2 text-[13px] font-semibold text-sky-700 dark:text-sky-300">
        <span aria-hidden>🧭</span>
        {data.intent === "wealth" ? "理财持仓分析" : "账单分析"}
        {data.plan_version && data.plan_version > 1 ? (
          <span className="rounded-full bg-white/70 px-1.5 py-0.5 text-[10px] font-normal text-muted-foreground dark:bg-sky-950/40">
            第 {data.plan_version} 版
          </span>
        ) : null}
      </div>
      {data.persona ? (
        <div className="mb-2 flex items-start gap-1.5 rounded-lg border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[12px] leading-5 text-amber-700 dark:text-amber-300">
          <span aria-hidden>🎭</span>
          <span className="min-w-0 [overflow-wrap:anywhere]">
            人设已加载：{data.persona}
            <span className="ml-1 text-[10px] text-muted-foreground">
              （只影响语气，不改事实）
            </span>
          </span>
        </div>
      ) : null}
      <div className="flex flex-col gap-1.5">
        <Row label="目标" value={data.goal} />
        {facts.map((row, index) => (
          <Row
            key={`${row.label ?? "fact"}-${index}`}
            label={row.label ?? ""}
            value={
              <span>
                {row.value}
                {row.hint ? (
                  <span className="ml-1 text-[11px] font-normal text-muted-foreground">
                    ({row.hint})
                  </span>
                ) : null}
              </span>
            }
          />
        ))}
      </div>
      {steps.length ? (
        <div className="mt-2 flex flex-col gap-1">
          <div className="text-xs text-muted-foreground">计划步骤</div>
          {steps.map((step, index) => {
            const risk = ANALYSIS_RISK_STYLE[step.risk ?? ""] ?? {
              className: "bg-muted text-muted-foreground",
              label: step.risk || "未知",
            };
            return (
              <div
                className="flex items-start justify-between gap-3 rounded-md bg-sky-100/50 px-2 py-1 text-[13px] dark:bg-sky-900/20"
                key={step.id ?? index}
              >
                <span className="min-w-0 [overflow-wrap:anywhere]">
                  <span className="mr-1 text-muted-foreground">
                    {index + 1}.
                  </span>
                  {step.title}
                  <span className="ml-1 text-[11px] text-muted-foreground">
                    {ANALYSIS_STATUS_CN[step.status ?? ""] ?? step.status}
                  </span>
                </span>
                <span
                  className={`shrink-0 rounded-full px-1.5 py-0.5 text-[10px] ${risk.className}`}
                >
                  {risk.label}
                </span>
              </div>
            );
          })}
        </div>
      ) : null}
      {diffText ? (
        <div className="mt-2 rounded-md bg-amber-100/60 px-2 py-1 text-xs text-amber-800 [overflow-wrap:anywhere] dark:bg-amber-900/30 dark:text-amber-200">
          计划调整:{diffText}(已完成步骤不重跑)
        </div>
      ) : null}
      {reflection.summary ? (
        <div className="mt-2 rounded-md bg-white/60 px-2 py-1 text-xs text-muted-foreground [overflow-wrap:anywhere] dark:bg-sky-950/30">
          复盘:{reflection.summary}
        </div>
      ) : null}
      {memory.length ? (
        <div className="mt-2 flex flex-wrap gap-1">
          <span className="text-xs text-muted-foreground">上次记忆:</span>
          {memory.map((item) => (
            <span
              className="rounded-full bg-sky-100/70 px-1.5 py-0.5 text-[11px] text-sky-700 dark:bg-sky-900/30 dark:text-sky-200"
              key={item}
            >
              {item}
            </span>
          ))}
        </div>
      ) : null}
      {data.hint ? (
        <div className="mt-2 text-[11px] text-muted-foreground [overflow-wrap:anywhere]">
          {data.hint}
        </div>
      ) : null}
    </div>
  );
}

/*
 * 思维链面板:把 Agent 的推理步骤(识别意图/调取记忆/计划/守卫/执行/复盘)按时间顺序
 * 列出来,带阶段名与状态着色。默认折叠,点标题展开——既是"看它怎么想",也避免刷屏。
 */
function ThinkingPanel({ data }: { data: ThinkingData }) {
  const thoughts = data.thoughts ?? [];
  // 直接渲染富规划可视化：9 阶段闭环 + 真实思维链 + 安全护栏。
  // 即便思维链为空也展示规划方法论，避免「看不到」。
  return <AgentPlanVisualization thoughts={thoughts} />;
}

export function BankDataPart({ type, data }: { type: string; data?: unknown }) {
  if (type === "data-transfer-confirmation") {
    return <TransferCard data={(data ?? {}) as TransferCardData} />;
  }
  if (type === "data-split-confirmation") {
    return <SplitCard data={(data ?? {}) as SplitCardData} />;
  }
  if (type === "data-settle-confirmation") {
    return (
      <GenericConfirmCard
        data={(data ?? {}) as Record<string, unknown>}
        labelMap={SETTLE_LABELS}
        title="AA 结算确认"
      />
    );
  }
  if (type === "data-subscription-list") {
    return <SubscriptionListCard data={(data ?? {}) as SubscriptionListData} />;
  }
  if (type === "data-subscription-cancel") {
    return (
      <SubscriptionCancelCard data={(data ?? {}) as SubscriptionCancelData} />
    );
  }
  if (type === "data-contact-choices") {
    return (
      <ContactChoices
        data={(data ?? {}) as { candidates?: ContactCandidate[] }}
      />
    );
  }
  if (type === "data-contact-confirmation") {
    return <ContactCard data={(data ?? {}) as ContactCandidate} />;
  }
  if (type === "data-linkage-plan") {
    return <LinkagePlanCard data={(data ?? {}) as LinkagePlanCardData} />;
  }
  if (type === "data-linkage-action") {
    return <LinkageActionCard data={(data ?? {}) as LinkageActionCardData} />;
  }
  if (type === "data-plan") {
    return <PlanCard data={(data ?? {}) as PlanCardData} />;
  }
  if (type === "data-wealth-confirmation") {
    return <WealthCard data={(data ?? {}) as WealthCardData} />;
  }
  if (type === "data-card-confirmation") {
    return <CardConfirmCard data={(data ?? {}) as CardConfirmationData} />;
  }
  if (type === "data-subscription-precheck") {
    return <PrecheckCard data={(data ?? {}) as PrecheckView} />;
  }
  if (type === "data-analysis-plan") {
    return <AnalysisPlanCard data={(data ?? {}) as AnalysisPlanData} />;
  }
  if (type === "data-thinking") {
    // 九宫格步骤面板已下线:思考过程改由模型原生 reasoning_content 驱动
    // (message-reasoning 流式面板,DeepSeek 效果)。分支保留守住契约
    // (前后端 data-* 集合必须一致),但不再渲染任何内容。
    return null;
  }
  if (type === "data-ask-slot") {
    return (
      <AskSlot
        data={(data ?? {}) as { missing?: string[]; options?: string[] }}
      />
    );
  }
  if (type === "data-gate-pending") {
    // 不再渲染黄胶囊:确认卡已有按钮、播报也已说明如何确认/取消,底部再挂
    // 一行「等待你确认」纯属重复。SSE 部件保持下发(契约不变,其他客户端可用)。
    return null;
  }
  return null;
}
