"""生日预算计划：纯函数计算层（不调 LLM、不碰数据库、不读全局状态）。

调用方（``p_snapshot``）负责把只读事实读好再传进来；本模块只做确定性计算：
同一个输入永远得到同一份计划底稿。金额一律用整数分运算，边界统一输出
``cents_to_yuan`` 的两位小数字符串元（如 ``"3500.00"``）。

三个铁律在本模块的落点：
- 订阅候选只列真实会员：房租/理财/还款/话费类周期账单确定性排除；
- 预算不足只在用户明确要求省钱时推荐砍单，且推荐额绝不超过可支配+本期释放额；
- 可支配为 0 时不产出任何可执行的锁定步骤（``blocked=True``）。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from bank_core.money import cents_to_yuan, yuan_to_cents

# 可支配额度里永远先扣掉的安全垫（元，两位小数字符串）
DEFAULT_SAFETY_BUFFER_YUAN = "500.00"

# 用户没给礼物动作时的默认剧本（每次深拷贝，避免调用方改到模块常量）
DEFAULT_ACTIONS: tuple[dict[str, Any], ...] = (
    {"what": "鲜花", "merchant": "花店", "days_before": 2, "amount_yuan": "300"},
    {"what": "蛋糕", "merchant": "蛋糕店", "days_before": 1, "amount_yuan": "200"},
)

# 计划卡 facts 的白名单键（与前端 PlanFacts 的 10 个成员一一对应；
# candidates/event_found 等内部字段绝不能漏进卡片）
PLAN_FACT_KEYS: tuple[str, ...] = (
    "event_title", "event_date", "days_left", "income_yuan", "expense_yuan",
    "upcoming_yuan", "safety_buffer_yuan", "disposable_yuan", "source", "formula",
)

# 可砍订阅的排除规则（顺序短路，命中即跳过）
_CUT_BLOCK_CATEGORIES = frozenset({"转账", "理财", "还款", "房租"})
# 房租/房东/理财/还款类商户：种子库里都会被 detect_subscriptions 挖成"订阅"
_CUT_BLOCK_MERCHANTS = (
    "房租", "房东", "张阿姨", "理财专户", "余额+货币基金", "自动理财",
    "信用卡还款", "还款", "贷款", "房贷", "车贷",
)
# 非会员的周期账单（话费/水电/物业）：不把 128 元的中国移动当第一砍单候选
_NON_MEMBER_MERCHANTS = (
    "中国移动", "中国联通", "中国电信", "话费", "宽带",
    "燃气", "供电", "水电", "物业",
)

# upcoming_precharges 没有月报的内部划转过滤：理财申购/还款/账户互转要自己剔除
_INTERNAL_CATEGORIES = frozenset({"转账", "理财", "还款"})

# 可支配额度扣减项里的"生日前订阅扣款"文案前缀
_CUT_CONSTRAINT = "只取消真实会员订阅(房租/理财/还款不可选)"

_CUT_WORDS = ("砍订阅", "取消订阅", "省钱", "钱不够")
# 多字否定词；单字"不/别"只在紧邻触发词时算否定（"特别想砍订阅"不能被"别"误判）
_CUT_NEGATIONS = ("不用", "不要", "不需要", "先不", "不想", "不是", "无需", "不会", "不打算")
_RENT_CONSTRAINTS = ("别动房租", "不动房租", "房租不能动", "房租别动")

_BUDGET_EDIT_RE = re.compile(
    r"(预算|金额|上限).{0,8}"
    r"(?:改成|改为|调整到|调整为|降到|降为|变成|设为|定成|定在)"
    r"\s*([0-9]+(?:\.[0-9]{1,2})?)"
)
# 裸数字：最多 5 位整数、禁前导零、可带 1-2 位小数（6 位支付密码必然不匹配）
_BUDGET_BARE_RE = re.compile(r"^\s*((?:0|[1-9][0-9]{0,4})(?:\.[0-9]{1,2})?)\s*元?\s*$")
_BUDGET_EDIT_MAX_CENTS = 10_000_000 * 100
_BUDGET_BARE_MAX_CENTS = 100_000 * 100


# --------------------------------------------------------------------- 基础工具

def _as_list(value: Any) -> list:
    """把 None/dict/单元素 归一成 list，避免把 dict 迭代成键名。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return [value]
    return []


def _cents0(value: Any) -> int:
    """零容忍的元→分：None/""/"0"/"0.00"/非法一律 0。

    ``bank_core.money.yuan_to_cents`` 对 0 会抛 ValueError（monthly_report 在
    当月无收支时正好返回 "0.00"），规划层不能让它把整条链路打断。
    """
    if value is None or isinstance(value, bool):
        return 0
    try:
        return yuan_to_cents(value)
    except (TypeError, ValueError, ArithmeticError):
        return 0


def _period_days(value: Any) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError):
        return 30
    return days if days > 0 else 30


def _period_text(days: Any) -> str:
    """周期天数 → 人话（与 agent/graph.py 的 _period_text 同口径，但本模块自带）。"""
    d = _period_days(days)
    if d == 1:
        return "天"
    if 28 <= d <= 31:
        return "月"
    if d in (360, 365, 366):
        return "年"
    return f"{d}天"


def _candidate_view(row: dict) -> dict:
    """订阅行 → 前端候选视图（键名即 PlanCutCandidate 契约）。"""
    amount = _cents0(row.get("amount_yuan"))
    period = _period_days(row.get("period_days"))
    annual = _cents0(row.get("annual_yuan") or row.get("annual_cost_yuan"))
    if annual <= 0:
        annual = round(365 / period * amount)
    raw_id = row.get("sub_id")
    if raw_id in (None, ""):
        raw_id = row.get("id")
    try:
        sub_id = int(raw_id)
    except (TypeError, ValueError):
        sub_id = 0
    return {
        "sub_id": sub_id,
        "merchant": str(row.get("merchant") or ""),
        "category": str(row.get("category") or "订阅"),
        "amount_yuan": cents_to_yuan(amount),
        "period_text": _period_text(period),
        "next_charge_date": str(row.get("next_charge_date") or ""),
        "annual_yuan": cents_to_yuan(annual),
    }


def _sort_candidates(candidates: list[dict]) -> list[dict]:
    """金额降序 → 年化降序 → sub_id 升序（确定性，不依赖输入顺序）。"""
    return sorted(
        candidates,
        key=lambda c: (-_cents0(c.get("amount_yuan")),
                       -_cents0(c.get("annual_yuan")),
                       int(c.get("sub_id") or 0)),
    )


# --------------------------------------------------------------------- 事件匹配

def _days_key(event: dict) -> tuple:
    try:
        d = int(event.get("days_left"))
    except (TypeError, ValueError):
        d = 10 ** 6
    # 未来最近的优先；过去的按绝对值由近到远兜底
    return (0, d, "") if d >= 0 else (1, -d, str(event.get("date") or ""))


def find_event(events: list[dict] | None, title: str | None,
               event_date: str | None) -> dict | None:
    """在 list_events 输出里定位用户说的那个事件。

    优先级：同名同日 > 生日事件最近（同日/无日期时） > 同日 > 标题互含。

    日期守卫（规格 §5.3 的复核阻塞项）：**只要给了 event_date，候选必须同日期**。
    否则用户明说的 10月25日 会被库里同名的旧事件（年度事件已滚到明年）静默覆盖，
    提醒最终排到错误年份；"妈妈生日"也不能被绑定到"林悦的生日"。
    """
    items = [e for e in _as_list(events) if isinstance(e, dict)]
    title = str(title or "").strip()
    date = str(event_date or "").strip()
    generic = title in ("", "生日", "我的生日")

    if date:
        same_day = [e for e in items if str(e.get("date") or "") == date]
        # 1) 同日 + 同名
        for e in same_day:
            if str(e.get("title") or "") == title:
                return e
        # 2) 同日 + 泛称"生日" → 生日事件（最近的一条）
        if generic:
            birthdays = [e for e in same_day
                         if str(e.get("event_type") or "") == "birthday"]
            if birthdays:
                return min(birthdays, key=_days_key)
            # 3) 同日（泛称时允许任意同日事件兜底）
            if same_day:
                return min(same_day, key=_days_key)
        # 4) 同日 + 标题互含（不接受不同日期的同名事件）
        for e in same_day:
            et = str(e.get("title") or "")
            if title and (title in et or et in title):
                return e
        return None

    # 未给日期：同名 > 最近的生日事件 > 标题互含
    for e in items:
        if title and str(e.get("title") or "") == title:
            return e
    if generic or "生日" in title:
        birthdays = [e for e in items if str(e.get("event_type") or "") == "birthday"]
        if birthdays:
            return min(birthdays, key=_days_key)
    for e in items:
        et = str(e.get("title") or "")
        if title and (title in et or et in title):
            return e
    return None


# --------------------------------------------------------------------- 用户意图

def wants_cut(text: str | None) -> bool:
    """用户是否明确要求砍订阅/省钱（带 4 字否定词窗口）。

    实测坑：裸子串会把「不用砍订阅,帮我规划生日,预算8000」判成 True，
    在用户明确拒绝时仍推荐取消订阅。
    """
    t = str(text or "")
    for word in _CUT_WORDS:
        start = 0
        while True:
            i = t.find(word, start)
            if i < 0:
                break
            tail = t[max(0, i - 5):i][-4:]  # 触发词前最多 4 个字
            negated = (tail.endswith("别") or tail.endswith("不")
                       or any(n in tail for n in _CUT_NEGATIONS))
            if not negated:
                return True
            start = i + len(word)
    return False


def constraints_of(text: str | None) -> list[str]:
    """确定性提取资金约束（当前只有"不动房租"一类）。"""
    t = str(text or "")
    if any(k in t for k in _RENT_CONSTRAINTS):
        return ["不动房租"]
    return []


# --------------------------------------------------------------------- 订阅/预扣

def filter_cut_candidates(subscriptions: list[dict] | None) -> list[dict]:
    """只保留真实会员订阅，按"本期可释放金额"降序。

    排除：非 active、金额非正、category∈(转账/理财/还款/房租)、
    房租/理财/还款类商户、话费水电等非会员周期账单。
    """
    out: list[dict] = []
    for row in _as_list(subscriptions):
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "") != "active":
            continue
        if _cents0(row.get("amount_yuan")) <= 0:
            continue
        if str(row.get("category") or "") in _CUT_BLOCK_CATEGORIES:
            continue
        merchant = str(row.get("merchant") or "")
        if any(k in merchant for k in _CUT_BLOCK_MERCHANTS):
            continue
        if any(k in merchant for k in _NON_MEMBER_MERCHANTS):
            continue
        out.append(_candidate_view(row))
    return _sort_candidates(out)


def filter_upcoming_outflows(items: list[dict] | None,
                             account_names: tuple[str, ...] | list[str] = ()) -> list[dict]:
    """剔除 upcoming_precharges 里的内部资金划转（理财申购/还款/账户互转）。

    该工具没有 monthly_report 的 _INTERNAL_FILTER；新播种库 30 天窗口里
    「理财专户 2000 + 余额+货币基金 2000」是自有资金，直接求和会把可支配
    额度系统性低估 4000 元。
    """
    names = {str(n) for n in _as_list(account_names) if n}
    out: list[dict] = []
    for item in _as_list(items):
        if not isinstance(item, dict):
            continue
        if str(item.get("category") or "") in _INTERNAL_CATEGORIES:
            continue
        if str(item.get("merchant") or "") in names:
            continue
        out.append(item)
    return out


# --------------------------------------------------------------------- 可支配额度

def compute_disposable(income_yuan: Any = None, expense_yuan: Any = None,
                       upcoming_items: list[dict] | None = (),
                       safety_buffer_yuan: Any = DEFAULT_SAFETY_BUFFER_YUAN,
                       source: str = "monthly_report",
                       balance_yuan: Any = None) -> dict:
    """可支配额度 = 收入/余额 - 已支出 - 生日前订阅扣款 - 安全垫（整数分）。

    source:
    - ``monthly_report``：本月月报（默认）
    - ``prev_month``：本月工资未到账时回退上一个完整月
    - ``balance``：月报不可用时回退活期余额（收入/支出输出 None）

    ``upcoming_items`` 必须是 ``filter_upcoming_outflows`` 的输出。
    """
    source = str(source or "monthly_report")
    if source not in ("monthly_report", "prev_month", "balance"):
        source = "monthly_report"

    income_cents = _cents0(income_yuan)
    expense_cents = _cents0(expense_yuan)
    balance_cents = _cents0(balance_yuan)
    upcoming_cents = sum(_cents0(i.get("amount_yuan"))
                         for i in _as_list(upcoming_items) if isinstance(i, dict))
    buffer_cents = _cents0(safety_buffer_yuan)

    if source == "balance":
        base_cents = balance_cents
    else:
        base_cents = income_cents - expense_cents
    disposable_cents = max(0, base_cents - upcoming_cents - buffer_cents)

    income_text = (cents_to_yuan(income_cents)
                   if source in ("monthly_report", "prev_month") else None)
    expense_text = (cents_to_yuan(expense_cents)
                    if source in ("monthly_report", "prev_month") else None)
    upcoming_text = cents_to_yuan(upcoming_cents)
    buffer_text = cents_to_yuan(buffer_cents)
    disposable_text = cents_to_yuan(disposable_cents)

    if source == "prev_month":
        formula = (f"可支配 = 上月收入 ¥{income_text} - 上月已支出 ¥{expense_text}"
                   f" - 生日前订阅扣款 ¥{upcoming_text} - 安全垫 ¥{buffer_text}"
                   f" = ¥{disposable_text}（本月工资未到账，按上月整月测算）")
    elif source == "balance":
        formula = (f"可支配 = 活期余额 ¥{cents_to_yuan(balance_cents)}"
                   f" - 生日前订阅扣款 ¥{upcoming_text} - 安全垫 ¥{buffer_text}"
                   f" = ¥{disposable_text}（月报不可用，已回退活期余额）")
    else:
        formula = (f"可支配 = 本月收入 ¥{income_text} - 本月已支出 ¥{expense_text}"
                   f" - 生日前订阅扣款 ¥{upcoming_text} - 安全垫 ¥{buffer_text}"
                   f" = ¥{disposable_text}")

    return {
        "income_yuan": income_text,
        "expense_yuan": expense_text,
        "upcoming_yuan": upcoming_text,
        "safety_buffer_yuan": buffer_text,
        "disposable_yuan": disposable_text,
        "source": source,
        "formula": formula,
    }


# --------------------------------------------------------------------- 计划底稿

def _normalize_actions(actions: list[dict] | None) -> tuple[list[dict], bool]:
    """规范化抽取到的礼物动作；全部非法时回退默认剧本。

    返回 ``(actions, used_default)``。金额保留抽取值的原始字符串（默认动作是
    "300"/"200"，与既有卡片文案「约 ¥300」一致）。
    """
    out: list[dict] = []
    for raw in _as_list(actions):
        if not isinstance(raw, dict):
            continue
        what = str(raw.get("what") or "").strip()
        amount = _cents0(raw.get("amount_yuan"))
        if not what or amount <= 0:
            continue
        try:
            days_before = int(raw.get("days_before", 2))
        except (TypeError, ValueError):
            days_before = 2
        out.append({
            "what": what,
            "merchant": str(raw.get("merchant") or "商户"),
            "days_before": days_before,
            "amount_yuan": str(raw.get("amount_yuan")),
        })
    if out:
        return out, False
    return [dict(a) for a in DEFAULT_ACTIONS], True


def build_plan(*, plan_id: str | None = None, plan_version: int = 1,
               goal: str = "", constraints: list[str] | None = None,
               wants_cut: bool = False, facts: dict | None = None,
               requested_budget_yuan: Any = None,
               actions: list[dict] | None = None,
               cut_candidates: list[dict] | None = None,
               degrade_reason: str | None = None) -> dict:
    """按可支配额度分支生成计划底稿（纯计算，不产生任何写操作）。

    分支：
    1. 预算 <= 可支配：不砍订阅，recommended = 预算；
    2. 预算 > 可支配 + 明确要砍 + 有候选：选金额最大的候选，
       recommended = min(预算, 可支配 + 该候选本期金额)；
    3. 预算 > 可支配但不要砍/无候选/砍单降级：recommended = max(0, 可支配)；
    4. recommended <= 0：blocked，不生成可执行锁定步骤。
    """
    raw_facts = facts if isinstance(facts, dict) else {}
    try:
        days_left = int(raw_facts.get("days_left") or 0)
    except (TypeError, ValueError):
        days_left = 0
    fact_view = {
        "event_title": str(raw_facts.get("event_title") or ""),
        "event_date": str(raw_facts.get("event_date") or ""),
        "days_left": days_left,
        "income_yuan": raw_facts.get("income_yuan"),
        "expense_yuan": raw_facts.get("expense_yuan"),
        "upcoming_yuan": str(raw_facts.get("upcoming_yuan") or "0.00"),
        "safety_buffer_yuan": str(raw_facts.get("safety_buffer_yuan")
                                  or DEFAULT_SAFETY_BUFFER_YUAN),
        "disposable_yuan": str(raw_facts.get("disposable_yuan") or "0.00"),
        "source": str(raw_facts.get("source") or "monthly_report"),
        "formula": str(raw_facts.get("formula") or ""),
    }

    cons: list[str] = []
    if isinstance(constraints, str):
        cons = [constraints] if constraints.strip() else []
    else:
        cons = [str(c) for c in _as_list(constraints) if str(c).strip()]
    if wants_cut and _CUT_CONSTRAINT not in cons:
        cons.append(_CUT_CONSTRAINT)

    requested = _cents0(requested_budget_yuan)
    disposable = _cents0(fact_view["disposable_yuan"])
    acts, _used_default = _normalize_actions(actions)

    cands = _sort_candidates([
        _candidate_view(c) for c in _as_list(cut_candidates) if isinstance(c, dict)
    ])

    selected_cut: dict | None = None
    degrade = str(degrade_reason or "").strip()
    past = days_left < 0

    if past:
        recommended = 0
        note = "生日日期已过，无法规划本次生日；请确认下一次生日日期。"
    elif requested <= disposable:
        recommended = requested
        note = "预算在可支配额度内，无需取消订阅。确认计划不会扣款。"
    elif wants_cut and not degrade and cands:
        selected_cut = dict(cands[0])
        cut_cents = _cents0(selected_cut.get("amount_yuan"))
        recommended = min(requested, disposable + cut_cents)
        selected_cut["reason"] = (
            f"原预算 ¥{cents_to_yuan(requested)} 超出可支配 ¥{cents_to_yuan(disposable)}，"
            f"取消后本期可释放 ¥{cents_to_yuan(cut_cents)}"
        )
        note = (
            f"原预算 ¥{cents_to_yuan(requested)} 超出可支配 ¥{cents_to_yuan(disposable)}，"
            f"建议取消「{selected_cut['merchant']}」会员（年省 ¥{selected_cut['annual_yuan']}），"
            f"锁定预算调整为 ¥{cents_to_yuan(recommended)}。"
        )
    elif degrade:
        recommended = max(0, disposable)
        note = (f"订阅保留（{degrade}），是否继续锁定预算 "
                f"¥{cents_to_yuan(recommended)}？")
    else:
        recommended = max(0, disposable)
        shortfall_text = cents_to_yuan(max(0, requested - recommended))
        note = (
            f"原预算超出可支配 ¥{cents_to_yuan(disposable)}，差额 ¥{shortfall_text}，"
            f"但没有可取消的会员订阅，锁定预算下调为 ¥{cents_to_yuan(recommended)}。"
        )

    blocked = recommended <= 0
    if blocked and not past:
        recommended = 0
        selected_cut = None
        basis = ("上月" if fact_view["source"] == "prev_month"
                 else "活期" if fact_view["source"] == "balance" else "本月")
        if disposable <= 0:
            note = f"{basis}可支配为 ¥0.00，无法锁定预算；建议先取消订阅或降低预算。"
        else:
            # 可支配足够但预算为 0：不能把"本月可支配为 0"这种假话说给用户
            note = "预算金额必须大于 0，无法生成可执行计划；请调整预算后再确认。"

    # 补充说明（确定性拼接，金额全部整数分）
    actions_total = sum(_cents0(a.get("amount_yuan")) for a in acts)
    if not past and not blocked:
        if actions_total > recommended:
            note += (f" 礼物预计 ¥{cents_to_yuan(actions_total)} 超过锁定预算 "
                     f"¥{cents_to_yuan(recommended)}，超出部分到期从活期直接扣。")
        upcoming = _cents0(fact_view["upcoming_yuan"])
        if upcoming:
            note += f" 已扣除生日前预计订阅扣款 ¥{cents_to_yuan(upcoming)}。"

    steps: list[dict] = [{
        "idx": 0,
        "title": "读取账单、账户与订阅事实(只读)",
        "risk": "READ",
        "gate": None,
        "done": True,
    }]
    if selected_cut and not blocked:
        steps.append({
            "idx": len(steps),
            "title": (f"取消「{selected_cut['merchant']}」自动扣费"
                      f"(年省 ¥{selected_cut['annual_yuan']})"),
            "risk": "HIGH",
            "gate": "confirm_sub_cancel",
            "done": False,
        })
    if recommended > 0 and not blocked:
        steps.append({
            "idx": len(steps),
            "title": f"锁定生日预算 ¥{cents_to_yuan(recommended)}",
            "risk": "HIGH",
            "gate": "confirm_linkage",
            "done": False,
        })
    if not past:
        for action in acts:
            steps.append({
                "idx": len(steps),
                "title": (f"生日前{action['days_before']}天提醒订购{action['what']}"
                          f"(约 ¥{action['amount_yuan']})"),
                "risk": "LOW",
                "gate": None,
                "done": False,
            })

    return {
        "plan_id": str(plan_id or _new_plan_id()),
        "plan_version": int(plan_version or 1),
        "goal": str(goal or ""),
        "constraints": cons,
        "facts": fact_view,
        "requested_budget_yuan": cents_to_yuan(requested),
        "recommended_budget_yuan": cents_to_yuan(recommended),
        "shortfall_yuan": cents_to_yuan(max(0, requested - recommended)),
        "cut_candidates": cands,
        "selected_cut": selected_cut,
        "steps": steps,
        "actions": acts,
        "note": note,
        "blocked": blocked,
    }


def plan_card_view(plan: dict) -> dict:
    """计划底稿 → 前端计划卡精确视图（只做拷贝，不含内部字段）。"""
    plan = plan if isinstance(plan, dict) else {}
    return {
        "plan_id": plan.get("plan_id"),
        "plan_version": plan.get("plan_version"),
        "goal": plan.get("goal"),
        "constraints": list(plan.get("constraints") or []),
        "facts": dict(plan.get("facts") or {}),
        "requested_budget_yuan": plan.get("requested_budget_yuan"),
        "recommended_budget_yuan": plan.get("recommended_budget_yuan"),
        "shortfall_yuan": plan.get("shortfall_yuan"),
        "cut_candidates": [dict(c) for c in plan.get("cut_candidates") or []],
        "selected_cut": dict(plan["selected_cut"]) if plan.get("selected_cut") else None,
        "steps": [dict(s) for s in plan.get("steps") or []],
        "note": plan.get("note"),
    }


# --------------------------------------------------------------------- 预算编辑

def parse_budget_edit(text: Any) -> str | None:
    """识别计划卡上的预算编辑，返回规范化两位小数字符串；否则 None。

    支持「预算改成800」「预算调整为 800」「金额降到800」「800」。
    裸数字限制：≤5 位整数、禁前导零、<10 万元——6 位支付密码（888888/012345）
    绝不会被当预算编辑。该函数必须在 ``parse_confirmation`` 之前调用
    （后者的犹豫词表含"改"）。
    """
    t = str(text or "").strip()
    if not t:
        return None

    match = _BUDGET_EDIT_RE.search(t)
    if match:
        cents = _cents0(match.group(2))
        if cents <= 0 or cents > _BUDGET_EDIT_MAX_CENTS:
            return None
        return cents_to_yuan(cents)

    match = _BUDGET_BARE_RE.match(t)
    if match:
        cents = _cents0(match.group(1))
        if cents <= 0 or cents >= _BUDGET_BARE_MAX_CENTS:
            return None
        return cents_to_yuan(cents)

    return None


def make_plan_id(thread_id: str | None, turn_text: str | None) -> str:
    """同一 thread + 同一句原话 → 同一个 plan_id（同一轮预算编辑重算时复用）。"""
    raw = f"{thread_id or ''}|{turn_text or ''}".encode("utf-8")
    return "plan-" + hashlib.sha1(raw).hexdigest()[:10]


def _new_plan_id() -> str:
    return "plan-" + uuid.uuid4().hex[:10]


__all__ = [
    "DEFAULT_ACTIONS",
    "DEFAULT_SAFETY_BUFFER_YUAN",
    "PLAN_FACT_KEYS",
    "build_plan",
    "compute_disposable",
    "constraints_of",
    "filter_cut_candidates",
    "filter_upcoming_outflows",
    "find_event",
    "make_plan_id",
    "parse_budget_edit",
    "plan_card_view",
    "wants_cut",
]
