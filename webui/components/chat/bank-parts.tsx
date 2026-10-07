"use client";

import {
  type ChangeEvent,
  type CSSProperties,
  type ReactNode,
  useCallback,
  useState,
} from "react";
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
 *   data-wealth-confirmation    理财确认卡(kind=subscribe|redeem;order=产品/金额/
 *                               费率/锁定期/风险/付款账户 或 持仓/估值/费用/到手)
 *   data-card-confirmation      卡片确认卡(kind=apply|limits|status;card=card_id/
 *                               尾号/类型/当前限额或状态/目标值;挂失加红字警示)
 *   data-subscription-list      订阅/代扣总览卡(逐项金额/下次扣费/年化+取消入口;
 *                               配套后台接入中,先经 /preview/subscriptions 演示)
 *   data-subscription-cancel    取消代扣确认卡(金色闸门,支付密码核验)
 *   data-ask-slot               缺槽提示
 *   data-gate-pending           本轮停在人工闸门
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

interface ContactCandidate {
  name?: string;
  note?: string;
  phone?: string;
  relation?: string;
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
    <div className="flex justify-between gap-4 text-[13px]">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium text-foreground">{value}</span>
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
              className="flex items-center justify-between gap-3 rounded-md bg-amber-100/50 px-2 py-1 text-[13px] dark:bg-amber-900/20"
              key={action.idx ?? index}
            >
              <span className="font-medium">
                {action.what}
                {action.merchant ? `(${action.merchant})` : ""}
              </span>
              <span className="text-muted-foreground">
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
      <span className="font-medium">{candidate.name}</span>
      <span className="text-right text-muted-foreground">
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
  bill_id?: number;
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
        <Row label="账单编号" value={data.bill_id} />
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
        <span>
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

function AskSlot({ data }: { data: { missing?: string[] } }) {
  const missing = (data.missing ?? []).map((slot) =>
    // 风险测评缺槽令牌带题号(assess_qN,graph.py w_extract 注释),统一显示中文
    slot.startsWith("assess_q") ? "风险测评答案" : (SLOT_LABELS[slot] ?? slot)
  );
  return (
    <div className="w-fit rounded-xl border border-blue-200/70 bg-blue-50/60 px-3 py-2 text-[13px] text-blue-700 dark:border-blue-500/30 dark:bg-blue-950/30 dark:text-blue-300">
      还差:{missing.join("、")} —— 直接回复补充即可
    </div>
  );
}

function GatePending() {
  return (
    <div className="w-fit rounded-full border border-amber-300/60 bg-amber-50/80 px-3 py-1 text-xs text-amber-700 dark:border-amber-500/30 dark:bg-amber-950/30 dark:text-amber-300">
      ⏸ 等待你确认 —— 回复「确认」执行,回复「取消」放弃
    </div>
  );
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
  if (type === "data-wealth-confirmation") {
    return <WealthCard data={(data ?? {}) as WealthCardData} />;
  }
  if (type === "data-card-confirmation") {
    return <CardConfirmCard data={(data ?? {}) as CardConfirmationData} />;
  }
  if (type === "data-ask-slot") {
    return <AskSlot data={(data ?? {}) as { missing?: string[] }} />;
  }
  if (type === "data-gate-pending") {
    return <GatePending />;
  }
  return null;
}
