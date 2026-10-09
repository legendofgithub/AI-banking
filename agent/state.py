"""LangGraph 对话状态定义(M1:Router + 转账子图 + AA 收款)。

约定:
- 金额在编排层只以"元的字符串"存在(与 bank_core MCP 工具入参一致),
  任何断言/校验需要"分"时用 bank_core.money.yuan_to_cents 确定性换算,
  绝不让 LLM 输出"分"或做算术(铁律 1:LLM 编排、代码计算)。
- messages 用 add_messages 归并;bank_calls 用 operator.add 追加,
  形成 agent 侧的调用轨迹(bank 侧另有 audit_log 全量留痕,铁律 4)。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class TransferSlots(TypedDict, total=False):
    """转账槽位(LLM 抽取,值为 None 表示暂缺)。"""
    payee: str | None            # 收款人姓名(或称呼)
    payee_phone: str | None      # 收款人手机号(可与姓名互补)
    amount_yuan: str | None      # 金额,"元"的数字字符串,如 "500" / "99.9"
    when: str                    # "now" | "scheduled"
    scheduled_at: str | None     # 定时时刻 'YYYY-MM-DDTHH:MM:SS'
    memo: str | None             # 备注


class SplitSlots(TypedDict, total=False):
    """AA 收款槽位。even=True 表示均摊,share 由确定性代码计算。"""
    title: str | None
    total_yuan: str | None
    participants: list[dict] | None   # [{"name": "老王", "share_yuan": "88.5"?}]
    even: bool                        # True: 未给 share,按人头均摊


class ContactDraft(TypedDict, total=False):
    """收款人录入槽位(AI 引导用户逐项提供,每组存一行)。"""
    name: str | None                  # 姓名
    phone: str | None                 # 手机号(1 开头 11 位,代码校验)
    note: str | None                  # 备注(可跳过,存空串)


class LinkageSlots(TypedDict, total=False):
    """跨场景联动抽取槽位(生日/纪念日剧本:预留预算 + 提前准备动作)。"""
    event_title: str | None           # 事件名,如 "林悦的生日";缺省按 "生日" 语义建事件
    event_date: str | None            # 'YYYY-MM-DD'(确定性代码校验格式)
    budget_yuan: str | None           # 预算金额,"元"的数字字符串(锁定单金额)
    actions: list[dict] | None        # [{"what","merchant","days_before","amount_yuan"}]


class BillSlots(TypedDict, total=False):
    """账单分析抽取槽位(只读场景,无闸门)。

    命名说明:状态键不叫 bill——bill 已被 AA 收款建单回执占用,且 ss_extract
    跨轮读取它定位"最近账单",语义冲突会互相踩;这里照转账管线的 slots 风格
    用 bill_slots(键名 bill_missing 保持任务书口径)。
    period 只存用户原文短语(如"上个月"),换算成具体 start/end/month 全由
    确定性代码完成(铁律:日期计算绝不给 LLM)。
    """
    ask: str | None                   # monthly_report | category_summary | top_merchants | anomaly_scan
    period: str | None                # 时间范围原文短语;'YYYY-MM' 也原样存
    category: str | None              # 用户点名的关注分类(播报上下文用)
    period_parsed: dict | None        # 确定性解析结果 {"month","start","end","days"}


class WealthSlots(TypedDict, total=False):
    """理财抽取槽位(查询/测评/申购/赎回;申购赎回动钱必过 confirm_wealth 闸门)。"""
    action: str | None                # query | assess | subscribe | redeem
    keyword: str | None               # 产品名/关键词(定位产品或持仓)
    product_id: int | None            # 产品 id(LLM 不可靠时由关键词定位补)
    amount_yuan: str | None           # 申购金额,"元"的数字字符串
    holding_id: int | None            # 赎回目标持仓 id
    p_type: str | None                # money_fund/bond/mixed/gold/deposit
    assess_answers: dict | None       # 测评进度 {"q1":1..5,...};集齐 5 题才落库
    kind: str | None                  # 闸门阶段态: subscribe | redeem(卡片视图分流用)
    order: dict | None                # 闸门复述要素(产品/金额/费率/锁定期/风险/账户)
    product_options: list | dict | None  # 多产品/多持仓命中时的候选(澄清后重定位)


class CardSlots(TypedDict, total=False):
    """卡片管理抽取槽位(办卡/限额/状态;写操作过 confirm_card 闸门,挂失双闸)。"""
    action: str | None                # list | apply | limits | status
    card_id: int | None               # 目标卡 id(多卡未指明时由线索/唯一卡确定性解析)
    card_hint: str | None             # 卡号线索原文:尾号8821/信用卡/第2张
    card_type: str | None             # debit | credit(apply 用)
    daily_limit_yuan: str | None      # 新日限额,"元"的数字字符串
    per_tx_limit_yuan: str | None     # 新单笔限额


class SubscriptionSlots(TypedDict, total=False):
    """订阅/代扣抽取槽位(查清单只读;取消过 confirm_sub_cancel 支付密码闸门)。"""
    action: str | None                # list | cancel
    merchant: str | None              # 取消目标商户名(列表不需要)
    sub_id: int | None                # 匹配到的订阅 id(s_pick 确定性解析)
    sub: dict | None                  # 取消闸门复述视图(商户/每期金额/下次扣费/年省)
    status_target: str | None         # locked | active | lost(挂失不可逆,双闸)
    card_view: dict | None            # 闸门卡片视图(kind/card_id/尾号/类型/当前值/目标值)


class AgentState(TypedDict, total=False):
    """整图共享状态。全部键可选:LangGraph 按键覆盖,不重建 dict。"""
    # ---- 对话 ----
    messages: Annotated[list[AnyMessage], add_messages]
    intent: str | None                 # transfer | split_bill | split_settle | contact_add |
                                       # linkage | bill_analysis | wealth | card | chat
    auth_user_id: int | None           # 登录用户 id(api 按 token 注入;None=观光模式,
                                       # 业务意图统一被 auth_guard 拦截引导登录)

    # ---- 转账子图 ----
    slots: TransferSlots               # 已抽取槽位(每次全量覆盖)
    missing: list[str]                 # 待补槽位键名;非空 → 反问
    candidates: list[dict] | None      # 同名联系人 ≥2 时的候选清单
    contact: dict | None               # 消歧后选定的收款人(contacts 行)
    account_id: int | None             # 付款账户(确定性代码选定)
    policy: dict | None                # policy_check 结果
    order: dict | None                 # create_transfer_order 返回的订单
    decision: str                      # 人工闸门决议 "yes" | "no"

    # ---- AA 收款 ----
    split: SplitSlots                  # AA 抽取槽位
    split_missing: list[str]           # AA 待补/待纠正项
    bill: dict | None                  # create_split_bill 返回的账单
    settle: dict | None                # 结算上下文 {"bill_id": int, "contact_name": str}

    # ---- 收款人录入 ----
    contact_draft: ContactDraft        # 录入槽位(姓名/手机号/备注)
    contact_missing: list[str]         # 待补录入项;非空 → 反问

    # ---- 跨场景联动(l_ 前缀节点) ----
    # plan:create_linkage_plan 的回执(dict)——含 plan_id/title/status/event/
    # budget_yuan/lock_order/actions,闸门卡片视图与播报事实都取自它;
    # 确认锁定后用 get_linkage_plan 刷新(锁定单状态会变 executed)。
    plan: dict | None
    # due:到期处理上下文(dict)——{"plan_id", "title",
    #   "actions": [各 action 视图(idx/what/merchant/amount_yuan)],
    #   "pending": [待过闸门的 action 下标,队首为当前], "skipped": [...],
    #   "executed": [...], "action": 当前闸门动作视图(含 plan_title/plan_id)}。
    # l_due(准备)→ l_due_gate(逐动作闸门)⇄ l_due_exec/l_due_skip 推进队列。
    due: dict | None
    linkage: LinkageSlots              # 联动抽取槽位(每次全量合并)
    linkage_missing: list[str]         # 联动待补项(event_date/budget_yuan/actions);非空 → 反问

    # ---- 账单分析(b_ 前缀节点,只读) ----
    bill_slots: BillSlots              # 账单抽取槽位(键名说明见 BillSlots 注释)
    bill_missing: list[str]            # 待补项(ask/period);非空 → 反问

    # ---- 理财(w_ 前缀节点) ----
    wealth: WealthSlots                # 理财抽取槽位 + 闸门阶段态(kind/order)
    wealth_missing: list[str]          # 待补项(action/product/amount_yuan/holding/assess_qN)

    # ---- 卡片管理(k_ 前缀节点) ----
    card: CardSlots                    # 卡片抽取槽位 + 闸门卡片视图(card_view)
    card_missing: list[str]            # 待补项(action/card/card_type/limits/status_target)

    # ---- 订阅/代扣(s_ 前缀节点) ----
    sub: SubscriptionSlots             # 订阅抽取槽位(列表/取消)
    sub_missing: list[str]             # 待补项(action/merchant);非空 → 反问
    sub_view: dict | None              # 列表卡片视图(api 在 s_list 节点更新时发帧)

    # ---- 播报与留痕 ----
    notice: dict | None                # 播报依据的事实 {"kind": ..., ...}
    bank_calls: Annotated[list[dict], operator.add]   # agent 侧银行调用轨迹
    turn_text: str                     # 本轮触发指令原文(转账幂等键组成)
    clarify_round: int                 # 缺槽反问轮次(封顶 MAX_CLARIFY_ROUNDS)
    gate_round: int                    # 人工闸门追问轮次(封顶 MAX_GATE_ASKS)
    pick_round: int                    # 同名消歧追问轮次(封顶 MAX_GATE_ASKS)


# 播报事实 kind 枚举(graph 内构造,report 节点消费)
NOTICE_KINDS = (
    "executed",          # 转账已执行
    "cancelled",         # 订单已取消
    "scheduled_created", # 定时单已建立(未扣款)
    "blocked",           # policy_check 拒绝
    "contact_missing",   # 查无收款人
    "contact_unresolved",# 反问两次仍无法消歧
    "bank_error",        # 银行工具返回错误
    "duplicate_order",   # 幂等命中且原单已终态(防重放)
    "clarify_gaveup",    # 反问超限放弃
    "split_created",     # AA 已发起
    "split_cancelled",   # AA 已取消(未发起)
    "split_progress",    # AA 结算进度
    "settle_cancelled",  # AA 结算确认被拒(未标记已付)
    "no_bill_context",   # 结算时找不到账单上下文
    "contact_added",     # 收款人已录入(新一行)
    "contact_dup",       # 手机号重复,未录入
    "contact_cancelled", # 录入确认被拒(未保存)
    "linkage_locked",    # 联动预算锁定单已确认划转(计划生效)
    "linkage_cancelled", # 联动计划已取消(锁定单/提醒一并撤销)
    "linkage_plan_done", # 到期动作全部完成,计划 done
    "linkage_progress",  # 到期处理进度(部分完成/有跳过)
    "no_due_plan",       # 到期消息没匹配到可处理的 active 计划
    # ---- 账单分析 ----
    "bill_report",       # 账单工具结果(月报/分类/Top/异常),交播报原样转述
    # ---- 理财 ----
    "no_risk_profile",   # 查无风险测评,引导先测评
    "wealth_products",   # 产品推荐列表(带风险等级过滤)
    "wealth_compare",    # 产品对比结果
    "wealth_holdings",   # 持仓查询结果
    "wealth_assessed",   # 风险测评已落库(C1-C5)
    "wealth_subscribed", # 申购已确认执行(真实扣款)
    "wealth_redeemed",   # 赎回已确认执行(资金到账)
    "wealth_cancelled",  # 申购/赎回在闸门被拒或追问超限(零资金变动)
    "no_product",        # 没定位到理财产品
    "no_holding",        # 没定位到可赎回持仓
    # ---- 卡片管理 ----
    "card_listed",       # 卡片清单
    "card_applied",      # 办卡完成(演示秒批)
    "card_limits_set",   # 限额已调整
    "card_status_set",   # 状态已变更(锁/解锁/挂失)
    "card_cancelled",    # 卡片操作在闸门被拒或追问超限(未执行)
    "no_card",           # 没定位到卡片
    # ---- 账号 ----
    "login_required",    # 观光模式说到业务:请先登录(零工具调用,零资金风险)
)


def initial_state(user_text: str) -> dict[str, Any]:
    """构造一次新对话轮的输入(供 graph.ainvoke 使用)。"""
    from langchain_core.messages import HumanMessage
    return {"messages": [HumanMessage(content=user_text)]}
