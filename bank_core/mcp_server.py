"""MCP 工具层：把假银行暴露为标准 MCP Server（七大场景 41 个工具）。

运行：
  python -m bank_core.mcp_server              # stdio（供 LangGraph/DSH 等 MCP 客户端挂载）
  python -m bank_core.mcp_server --http 8765  # streamable-http，供远程/前端联调

约定：
- 金额参数一律字符串"元"（如 "5000" / "99.9"），内部换算成分，杜绝浮点误差
- 动钱工具（HIGH 风险）全部两步走：先建单返回 pending_confirm，确认后才执行
- 每个工具 docstring 标注风险等级：READ（只读）/ LOW / MED / HIGH（动钱）
"""

from __future__ import annotations

import json
import os
import sys

from fastmcp import FastMCP

from .analysis import AnalysisService
from .db import audit, connect, init_db, now_iso
from .events import EventService
from .ledger import LedgerError, LedgerService, POLICY
from .linkage import LinkageService
from .money import yuan_to_cents
from .wealth import WealthService

mcp = FastMCP("bank-core", instructions=(
    "AI Banking Agent 练功假银行。金额单位为元（字符串）。"
    "动钱操作必须先建单（pending_confirm）并把单据展示给用户，"
    "用户确认后才能调用 confirm 类工具执行。"
    "动钱/敏感操作的确认需要用户输入 6 位支付密码，用 verify_pay_password 校验。"))

# 多用户绑定：编排层按登录用户在子进程 env 注入 BANK_USER_ID(默认 1=陈明)。
# 每次工具调用各自新起 stdio 子进程 → 每个子进程天然绑定一个用户。
_USER_ID = int(os.environ.get("BANK_USER_ID", "1"))

_conn = init_db()
_ledger = LedgerService(_conn, _USER_ID)
_analysis = AnalysisService(_conn, _USER_ID)
_wealth = WealthService(_conn, _USER_ID)
_events = EventService(_conn, _USER_ID)
_linkage = LinkageService(_conn, _USER_ID)


def _yuan(s: str) -> int:
    return yuan_to_cents(s)


# ============================== 1. 账户与流水 [READ] ==============================

@mcp.tool
def get_accounts() -> list[dict]:
    """[READ] 查询用户所有账户及实时余额。"""
    return _ledger.list_accounts()


@mcp.tool
def get_transactions(start: str | None = None, end: str | None = None,
                     category: str | None = None, counterparty: str | None = None,
                     direction: str | None = None, limit: int = 50) -> list[dict]:
    """[READ] 查询交易流水。start/end 为 'YYYY-MM-DD'；direction: in/out；
    category 如 '餐饮'；counterparty 如 '美团'。默认最近 50 笔。"""
    s = f"{start}T00:00:00" if start else None
    e = f"{end}T23:59:59" if end else None
    return _ledger.get_transactions(s, e, category, counterparty, direction, limit)


# ============================== 2. 智能转账 ==============================

@mcp.tool
def resolve_contact(name: str | None = None, phone: str | None = None) -> list[dict]:
    """[READ] 按姓名或手机号查找收款人。返回多个同名联系人时应向用户澄清选哪个。"""
    return _ledger.resolve_contact(name, phone)


@mcp.tool
def add_contact(name: str, phone: str, note: str = "") -> dict:
    """[LOW] 新增收款人:姓名+手机号+备注 录入一行。手机号须 1 开头 11 位;
    重复手机号会报错。只写用户资料,不动钱。"""
    return _ledger.add_contact(name, phone, note)


@mcp.tool
def policy_check(amount_yuan: str, to_contact_id: int | None = None,
                 from_account_id: int | None = None) -> dict:
    """[READ] 转账前风控预检（限额/日累计/白名单）。返回 approved 与 reasons。"""
    return _ledger.policy_check(_yuan(amount_yuan), to_contact_id, from_account_id)


@mcp.tool
def create_transfer_order(from_account_id: int, amount_yuan: str,
                          to_contact_id: int | None = None, to_name: str = "",
                          to_account_tail: str = "", memo: str = "",
                          scheduled_at: str | None = None,
                          idempotency_key: str | None = None) -> dict:
    """[HIGH·建单] 创建转账单（不动钱）。立即转返回 pending_confirm；
    scheduled_at('YYYY-MM-DDTHH:MM:SS') 返回 scheduled 定时单。
    ⚠️ 建单后必须把单据要点（收款人/金额/时间）展示给用户确认。"""
    return _ledger.create_transfer_order(
        from_account_id, _yuan(amount_yuan), to_contact_id, to_name,
        to_account_tail, memo, scheduled_at, idempotency_key)


@mcp.tool
def confirm_transfer_order(order_id: int) -> dict:
    """[HIGH·动钱] 确认执行转账单。仅在用户明确同意后调用；内部二次校验余额与限额。"""
    return _ledger.confirm_transfer_order(order_id)


@mcp.tool
def cancel_transfer_order(order_id: int) -> dict:
    """[MED] 取消未执行的转账单（含定时单）。"""
    return _ledger.cancel_transfer_order(order_id)


@mcp.tool
def get_policy() -> dict:
    """[READ] 当前风控策略（单笔/日限额等），供 Agent 向用户解释拦截原因。"""
    return {k: f"{v/100:.0f}元" if k.endswith("_cents") else v for k, v in POLICY.items()}


@mcp.tool
def verify_pay_password(pay_password: str) -> dict:
    """[READ·敏感] 校验当前用户的 6 位支付密码（动钱/敏感操作闸门用）。
    返回 {"verified": true/false, "user_id"}。密码错误不报错只返回 false，
    由编排层决定重问或取消——绝不把失败当异常吞掉闸门语义。
    ⚠️ 审计留痕时密码必须脱敏（本工具已内置）。"""
    from .auth_core import verify_pay_password as _verify
    ok = _verify(_conn, _USER_ID, pay_password)
    # 审计脱敏：支付密码绝不入明文（bank_core 级红线）
    audit(_conn, "verify_pay_password", {"pay_password": "***"},
          {"verified": ok, "user_id": _USER_ID}, risk="READ")
    return {"verified": ok, "user_id": _USER_ID}


@mcp.tool
def create_split_bill(title: str, total_yuan: str,
                      participants: list[dict]) -> dict:
    """[MED] 发起 AA 收款。participants=[{"name":"老王","share_yuan":"88.5"},...]，
    分摊合计必须等于总额。返回账单与各人应付。"""
    parts = [{"name": p["name"], "share_cents": _yuan(p["share_yuan"])}
             for p in participants]
    return _ledger.create_split_bill(title, _yuan(total_yuan), parts)


@mcp.tool
def get_split_bill(bill_id: int) -> dict:
    """[READ] 查询 AA 收款进度（谁已付谁未付）。"""
    return _ledger.get_split_bill(bill_id)


@mcp.tool
def settle_split_bill_item(bill_id: int, contact_name: str) -> dict:
    """[LOW] 标记某位参与人已付（模拟对方付款回调，演示用）。"""
    return _ledger.settle_split_bill_item(bill_id, contact_name)


# ============================== 3. 账单分析 ==============================

@mcp.tool
def category_summary(start: str, end: str, direction: str = "out") -> dict:
    """[READ] 分类统计。start/end 为 'YYYY-MM-DD'。返回各类别金额/笔数/占比。"""
    return _analysis.category_summary(f"{start}T00:00:00", f"{end}T23:59:59", direction)


@mcp.tool
def top_merchants(start: str, end: str, top_n: int = 10) -> list[dict]:
    """[READ] 消费金额 Top 商户。"""
    return _analysis.top_merchants(f"{start}T00:00:00", f"{end}T23:59:59", top_n)


@mcp.tool
def monthly_report(month: str) -> dict:
    """[READ] 月度收支报告（'YYYY-MM'）：收支/结余/环比/储蓄率/分类 Top。"""
    return _analysis.monthly_report(month)


@mcp.tool
def detect_anomalies(days: int = 90) -> list[dict]:
    """[READ] 异常交易检测（确定性规则）：重复扣款/大额离群/凌晨大额。
    返回规则名+证据+建议，请原样转述证据，不要自行编造数字。"""
    return _analysis.detect_anomalies(days)


# ============================== 4. 订阅/代扣 ==============================

@mcp.tool
def detect_subscriptions(window_days: int = 365) -> list[dict]:
    """[READ] 挖掘周期性扣费（订阅/房租类，默认看一年窗口），含年化成本与涨价提示，并入库。"""
    return _analysis.detect_subscriptions(window_days)


@mcp.tool
def list_subscriptions() -> list[dict]:
    """[READ] 列出已识别订阅（含已取消）。"""
    return _analysis.list_subscriptions()


@mcp.tool
def cancel_subscription(subscription_id: int) -> dict:
    """[MED] 一键取消订阅代扣。⚠️ 取消前必须向用户复述商户名与年化金额并获同意。"""
    return _analysis.cancel_subscription(subscription_id)


# ============================== 5. 理财操作 ==============================

@mcp.tool
def list_wealth_products(p_type: str | None = None,
                         max_risk_level: int | None = None,
                         keyword: str | None = None) -> list[dict]:
    """[READ] 理财产品列表。p_type: money_fund/bond/mixed/gold/deposit；
    建议按用户风险等级过滤 max_risk_level。"""
    return _wealth.list_products(p_type, max_risk_level, keyword)


@mcp.tool
def get_product(product_id: int) -> dict:
    """[READ] 产品详情（费率/锁定期/业绩基准）。推荐时必须引用真实费率数据。"""
    return _wealth.get_product(product_id)


@mcp.tool
def compare_products(product_ids: list[int]) -> list[dict]:
    """[READ] 多产品对比（收益/风险/费率/锁定期并排）。"""
    return _wealth.compare_products(product_ids)


@mcp.tool
def get_risk_profile() -> dict | None:
    """[READ] 用户风险测评结果（C1-C5）。无则建议先引导测评。"""
    return _wealth.get_risk_profile()


@mcp.tool
def set_risk_profile(answers: dict[str, int]) -> dict:
    """[MED] 风险测评落库。answers={"投资经验":1-5,"亏损容忍":1-5,...}（约5题）。"""
    return _wealth.set_risk_profile(answers)


@mcp.tool
def get_holdings() -> list[dict]:
    """[READ] 当前持仓与收益。"""
    return _wealth.get_holdings()


@mcp.tool
def subscribe_product(product_id: int, amount_yuan: str, from_account_id: int,
                      confirmed: bool = False,
                      idempotency_key: str | None = None) -> dict:
    """[HIGH·两步] 申购理财。confirmed=False 返回待确认单；
    ⚠️ 用户同意后再以 confirmed=True 真正扣款。风险超限会直接拒绝；
    未完成风险测评时仅可申购 R1（现金管理类），R2 及以上须先做测评。
    idempotency_key: 编排层传入的重放键，同键重放直接拒绝，不重复扣款。"""
    return _wealth.subscribe_product(product_id, _yuan(amount_yuan),
                                     from_account_id, confirmed, idempotency_key)


@mcp.tool
def redeem_product(holding_id: int, confirmed: bool = False) -> dict:
    """[HIGH·两步] 全额赎回持仓。confirmed=False 返回待确认单（含到手金额与费用）。"""
    return _wealth.redeem_product(holding_id, confirmed)


# ============================== 6. 卡片管理 ==============================

@mcp.tool
def list_cards() -> list[dict]:
    """[READ] 名下卡片与限额/状态。"""
    return _ledger.list_cards()


@mcp.tool
def apply_card(card_type: str = "debit") -> dict:
    """[MED] 申请办卡（演示秒批）。card_type: debit/credit。"""
    return _ledger.apply_card(card_type)


@mcp.tool
def set_card_limits(card_id: int, daily_limit_yuan: str | None = None,
                    per_tx_limit_yuan: str | None = None) -> dict:
    """[MED] 调整卡片限额。⚠️ 调整前应向用户复述新限额。"""
    return _ledger.set_card_limits(
        card_id,
        _yuan(daily_limit_yuan) if daily_limit_yuan else None,
        _yuan(per_tx_limit_yuan) if per_tx_limit_yuan else None)


@mcp.tool
def set_card_status(card_id: int, status: str) -> dict:
    """[MED] 卡片状态：locked（锁定）/ active（解锁）/ lost（挂失，不可逆）。
    ⚠️ 挂失必须双重确认。"""
    return _ledger.set_card_status(card_id, status)


# ============================== 7. 事件与联动 ==============================

@mcp.tool
def list_events(upcoming_days: int | None = None) -> list[dict]:
    """[READ] 用户事件（生日/纪念日/发薪日等）。upcoming_days 只看近期。"""
    return _events.list_events(upcoming_days)


@mcp.tool
def add_event(event_type: str, title: str, event_date: str,
              repeat_yearly: bool = False, note: str = "") -> dict:
    """[LOW] 记录用户事件（跨场景联动的记忆）。event_type:
    birthday/anniversary/payday/bill_day/custom；event_date 'YYYY-MM-DD'。"""
    return _events.add_event(event_type, title, event_date, repeat_yearly, note)


@mcp.tool
def schedule_reminder(title: str, run_at: str, payload_json: str = "{}") -> dict:
    """[LOW] 创建提醒任务。run_at 'YYYY-MM-DDTHH:MM:SS'。"""
    return _events.schedule_reminder(title, run_at, json.loads(payload_json or "{}"))


@mcp.tool
def list_scheduled_tasks(status: str = "pending") -> list[dict]:
    """[READ] 待办任务（提醒/到期转账）。"""
    return _events.list_scheduled_tasks(status)


@mcp.tool
def run_due_tasks(as_of: str | None = None) -> list[dict]:
    """[LOW] 触发到期任务（演示同步触发；生产为调度器轮询）。
    定时转账到期只转为待确认，绝不自动扣款。
    as_of 可选 'YYYY-MM-DDTHH:MM:SS'（演示时间旅行，把"今天"拨到指定时刻），
    默认当前时间。返回项携带 payload：联动提醒含 plan_id/action_idx。"""
    return _events.run_due_tasks(as_of)


@mcp.tool
def suggest_linkage(event_id: int) -> dict:
    """[READ] 生成事件联动计划草稿（如生日→预留资金+提前2天礼物提醒）。
    只产出建议，执行每一步仍需用户确认。"""
    return _events.suggest_linkage(event_id)


# ============================== 8. 跨场景联动计划 ==============================

@mcp.tool
def create_linkage_plan(event_id: int, title: str, budget_yuan: str,
                        actions: list[dict]) -> dict:
    """[HIGH·两步] 创建跨场景联动计划（生日剧本）。预算锁定=从活期(账户1)向
    理财专户(账户2)建一笔转账单（收款人写自己、备注'生日预留'）——只建单
    pending_confirm 不动钱，须用户确认后才划转；每个动作按事件日期-days_before
    生成提醒任务。actions=[{"what":"鲜花","merchant":"花店","amount_yuan":"300",
    "days_before":2},...]（金额一律字符串元）。
    ⚠️ 建计划后必须把要点（事件/预算/动作/提醒时间/锁定单）展示给用户确认。"""
    acts = [{"type": a.get("type", "order"), "what": a.get("what", "礼物"),
             "merchant": a.get("merchant", "商户"),
             "amount_cents": _yuan(a["amount_yuan"]),
             "days_before": int(a.get("days_before", 2))} for a in actions]
    return _linkage.create_linkage_plan(event_id, title, _yuan(budget_yuan), acts)


@mcp.tool
def get_linkage_plan(plan_id: int) -> dict:
    """[READ] 查询联动计划全景：预算锁定单状态 + 各动作/提醒任务进度。"""
    return _linkage.get_linkage_plan(plan_id)


@mcp.tool
def execute_linkage_action(plan_id: int, action_idx: int) -> dict:
    """[HIGH·动钱] 执行联动计划的一个动作（如订购鲜花）：从活期账户真实扣款，
    生成 online 购买流水。⚠️ 仅在用户明确同意该笔购买后调用；
    已执行/计划非 active 会被拒绝，重复执行有幂等拦截。"""
    return _linkage.execute_linkage_action(plan_id, action_idx)


@mcp.tool
def cancel_linkage_plan(plan_id: int) -> dict:
    """[MED] 取消联动计划：未完成的计划置 cancelled，未执行的预算锁定转账单
    与剩余提醒任务一并撤销。"""
    return _linkage.cancel_linkage_plan(plan_id)


def main() -> None:
    args = sys.argv[1:]
    if "--http" in args:
        idx = args.index("--http")
        port = int(args[idx + 1]) if len(args) > idx + 1 and args[idx + 1].isdigit() else 8765
        mcp.run(transport="http", host="127.0.0.1", port=port)
    else:
        mcp.run()  # stdio


if __name__ == "__main__":
    main()
