"""技能目录 + 技能适配器：把 44 个银行 MCP 工具收敛成 6 个可控技能。

为什么要这一层（迭代计划 §4、自主规划计划 §4.2）
------------------------------------------------
LLM/Planner 不应直接面对 44 个风险不一、粒度参差的底层工具。这里向上只暴露
**六个技能**，每个技能声明：描述 / 输入 / 风险 / 是否幂等 / 是否必须闸门 /
成功条件 / 允许调用的底层工具白名单。Core 只看技能，执行器负责盯闸门。

分两批接入（迭代计划 §4 表格）：
- ``analyze_bill`` / ``analyze_wealth`` / ``plan_birthday``：**首批接入 Core**，
  只读或两个既有受闸门链路组合；
- ``execute_transfer`` / ``execute_card`` / ``execute_subscription``：
  **一行不改**——它们仍由既有子图执行，Core 只把它们当作"必须过支付密码闸门
  的傻瓜执行"技能登记在册，**不会**自己去跑（见 ``run_skill`` 的拒绝分支）。

金额铁律：本模块只搬运工具返回值，不重算任何金额；占比/合计由确定性代码算，
且走 ``bank_core.money`` 的整数分运算。
"""

from __future__ import annotations

from typing import Any

from agent.core import SkillResult, SkillSpec

# 等级 → 可购最高 R 等级（与 graph.py::_LEVEL_MAX_RISK 同口径，本层自带避免越层依赖）
_LEVEL_MAX_RISK: dict[str, int] = {"C1": 1, "C2": 2, "C3": 3, "C4": 4, "C5": 5}
# 未测评时的适当性上限（bank_core.wealth.subscribe_product：无测评限购 R1）
_NO_ASSESSMENT_MAX_RISK = 1

# ------------------------------------------------------------------ 技能目录

SKILLS: dict[str, SkillSpec] = {
    skill.name: skill
    for skill in (
        SkillSpec(
            name="analyze_bill",
            description="读取月度账单/分类占比/Top 商户/异常交易/订阅清单，"
                        "给出哪里花多了、哪些订阅可省（只读，不取消任何东西）",
            kind="analysis",
            risk="READ",
            tools=("monthly_report", "category_summary", "top_merchants",
                   "detect_anomalies", "list_subscriptions"),
            idempotent=True,
            gate=None,
            success="返回月报/分类/Top 商户/异常/订阅，且统计期间与请求一致",
            preconditions=("已登录",),
        ),
        SkillSpec(
            name="analyze_wealth",
            description="读取风险测评等级/持仓市值与盈亏/集中度，"
                        "按 ≤ 风险等级筛 2-3 只候选产品（只读，不申购不赎回）",
            kind="analysis",
            risk="READ",
            tools=("get_risk_profile", "get_holdings", "list_wealth_products",
                   "compare_products"),
            idempotent=True,
            gate=None,
            success="有风险等级且持仓/产品已读取；候选产品风险等级全部 ≤ 用户等级",
            preconditions=("已登录",),
        ),
        SkillSpec(
            name="plan_birthday",
            description="按事件/收支/订阅生成生日预算计划；涉及资金的步骤仍需计划确认 + 支付密码",
            kind="analysis",     # 第一期：分析 + 交给既有受闸门链路
            risk="HIGH",
            tools=("list_events", "monthly_report", "list_subscriptions",
                   "list_upcoming_precharges", "create_linkage_plan",
                   "cancel_subscription"),
            idempotent=False,
            gate="confirm_plan",
            success="预算锁定单 executed、提醒已排程、未动房租",
            preconditions=("事件日期在未来 0-365 天", "预算 > 0"),
            readonly=False,
        ),
        SkillSpec(
            name="execute_transfer",
            description="转账/AA：收款人、金额、限额校验后建单 → 用户确认 → 支付密码扣款",
            kind="execution",
            risk="HIGH",
            tools=("resolve_contact", "policy_check", "create_transfer_order",
                   "confirm_transfer_order"),
            idempotent=True,
            gate="confirm_transfer",
            success="转账单 executed 且恰好扣款一次（幂等防重放）",
            preconditions=("收款人唯一或已消歧", "未超限额", "支付密码正确"),
            readonly=False,
        ),
        SkillSpec(
            name="execute_card",
            description="办卡/改限额/改状态（挂失双闸）：既有卡片子图执行",
            kind="execution",
            risk="HIGH",
            tools=("list_cards", "apply_card", "set_card_limits", "set_card_status"),
            idempotent=True,
            gate="confirm_card",
            success="卡片操作已执行且限额/状态已生效",
            preconditions=("卡片归属当前用户", "支付密码正确"),
            readonly=False,
        ),
        SkillSpec(
            name="execute_subscription",
            description="订阅取消/代扣放行跳过：既有订阅子图执行，取消需支付密码",
            kind="execution",
            risk="HIGH",
            tools=("list_subscriptions", "cancel_subscription"),
            idempotent=True,
            gate="confirm_sub_cancel",
            success="订阅状态已变更且不误伤其他订阅",
            preconditions=("订阅归属当前用户", "支付密码正确"),
            readonly=False,
        ),
    )
}

# 只有这三个技能能被 Core 自主执行（其余必须走既有子图 + 支付密码）
CORE_RUNNABLE: frozenset[str] = frozenset({"analyze_bill", "analyze_wealth"})


def skill_catalog() -> list[dict]:
    """给治理/测试用的技能目录快照（每项都是可 JSON 序列化的 dict）。"""
    return [SKILLS[name].to_dict() for name in sorted(SKILLS)]


# ------------------------------------------------------------------ 适配器

def _err(payload: Any) -> str | None:
    return payload.get("error") if isinstance(payload, dict) else None


def tool_in_skill(skill: str, tool: str) -> bool:
    """工具白名单校验：技能不允许调用目录外的底层工具。"""
    spec = SKILLS.get(skill)
    return bool(spec) and tool in spec.tools


async def run_skill(tools: Any, skill: str, args: dict | None = None) -> SkillResult:
    """按技能名执行（核心入口）。``tools`` 是 ``agent.bank.BankTools``。

    返回的 ``SkillResult.ok`` 是**成功条件是否满足**，不是"工具没报错"：
    - 技能不在目录 → 直接拒绝；
    - 资金类技能（``execute_*``）→ 拒绝自主执行，必须由既有子图带闸门走；
    - 只读分析技能 → 跑白名单工具，任一主查询失败即不算成功。
    """
    args = dict(args or {})
    spec = SKILLS.get(skill)
    if spec is None:
        return SkillResult(skill=skill, ok=False,
                           reason=f"技能目录里没有 skill：{skill}")
    if skill not in CORE_RUNNABLE:
        return SkillResult(
            skill=skill, ok=False,
            reason=(f"技能 {skill} 属于受闸门保护的资金操作，"
                    f"Core 不直接执行，交由既有 {spec.gate} 链路办理"))
    # 白名单视图（fail-closed，借鉴 Claude Code 角色隔离）：真实 BankTools 会把
    # 本技能锁死在目录白名单内——分析路径就算日后写错，也物理摸不到动钱工具。
    # tests 里的最小替身（FakeTools）没有 scoped 能力则跳过，白名单一致性
    # 由 tests/test_tool_meta.py 在测试层兜住。
    scoped_factory = getattr(tools, "scoped", None)
    if scoped_factory is not None:
        tools = scoped_factory(frozenset(spec.tools), label=f"skill:{skill}")
    if skill == "analyze_bill":
        return await _run_analyze_bill(tools, args)
    return await _run_analyze_wealth(tools, args)


async def _run_analyze_bill(tools: Any, args: dict) -> SkillResult:
    """账单分析：月报 + 分类 + Top 商户 + 异常 + 订阅（全部只读工具）。"""
    called: list[str] = []
    data: dict[str, Any] = {}

    month = str(args.get("month") or "").strip()
    start = str(args.get("start") or "").strip()
    end = str(args.get("end") or "").strip()
    try:
        days = int(args.get("days") or 90)
    except (TypeError, ValueError):
        days = 90
    days = max(1, min(365, days))

    report = await tools.call("monthly_report", month=month)
    called.append("monthly_report")
    err = _err(report)
    if err or not isinstance(report, dict):
        return SkillResult(skill="analyze_bill", ok=False,
                           reason=f"月报读取失败：{err or '返回格式异常'}",
                           tools_called=called)
    if month and str(report.get("month") or "") != month:
        return SkillResult(skill="analyze_bill", ok=False,
                           reason=f"月报期间不一致：请求 {month}，返回 "
                                  f"{report.get('month')}",
                           tools_called=called)
    data["report"] = report

    if start and end:
        categories = await tools.call("category_summary", start=start, end=end)
        called.append("category_summary")
        if not _err(categories):
            data["categories"] = categories

        merchants = await tools.call("top_merchants", start=start, end=end, top_n=5)
        called.append("top_merchants")
        if not _err(merchants):
            data["merchants"] = merchants

    anomalies = await tools.call("detect_anomalies", days=days)
    called.append("detect_anomalies")
    if not _err(anomalies):
        data["anomalies"] = anomalies

    subs = await tools.call("list_subscriptions")
    called.append("list_subscriptions")
    if not _err(subs):
        data["subscriptions"] = subs

    # 兜底：期间没解析出起止日（或分类为空）时，用月报自带分类让事实行仍然成立
    cats = ((data.get("categories") or {}).get("items") or [])
    merchants = data.get("merchants") or []
    if not cats and isinstance(report.get("top_categories"), list):
        fallback = {"items": list(report["top_categories"])}
        data["categories"] = fallback
        cats = fallback["items"]

    # 成功条件：至少拿到一条分类或商户事实（否则这份分析没有可播报的事实）
    if not cats and not merchants:
        return SkillResult(skill="analyze_bill", ok=False, data=data,
                           reason="分类/商户均无数据，成功条件未满足",
                           tools_called=called)
    return SkillResult(skill="analyze_bill", ok=True, data=data,
                       tools_called=called)


async def _run_analyze_wealth(tools: Any, args: dict) -> SkillResult:
    """理财分析：风险等级 + 持仓 + ≤ 等级的候选产品（只读）。

    适当性硬约束：**候选产品必须全部 R ≤ 用户风险等级**；未测评时按 bank_core
    的口径限购 R1（不是无评级推荐）。
    """
    called: list[str] = []
    data: dict[str, Any] = {}

    profile = await tools.call("get_risk_profile")
    called.append("get_risk_profile")
    has_profile = isinstance(profile, dict) and bool(profile.get("level"))
    if has_profile:
        level = str(profile["level"])
        max_risk = _LEVEL_MAX_RISK.get(level, _NO_ASSESSMENT_MAX_RISK)
        data["profile"] = profile
    else:
        max_risk = _NO_ASSESSMENT_MAX_RISK
        data["profile"] = None
        data["needs_assessment"] = True
    data["max_risk_level"] = max_risk

    holdings = await tools.call("get_holdings")
    called.append("get_holdings")
    if _err(holdings):
        return SkillResult(skill="analyze_wealth", ok=False, data=data,
                           reason=f"持仓读取失败：{_err(holdings)}",
                           tools_called=called)
    data["holdings"] = holdings if isinstance(holdings, list) else []

    products = await tools.call("list_wealth_products", max_risk_level=max_risk)
    called.append("list_wealth_products")
    if _err(products):
        return SkillResult(skill="analyze_wealth", ok=False, data=data,
                           reason=f"产品列表读取失败：{_err(products)}",
                           tools_called=called)
    rows = products if isinstance(products, list) else []

    # 复盘硬条件：候选产品风险等级不得高于用户等级（此处做二次确定性守卫）
    safe: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = str(row.get("risk_level") or "").upper().replace("R", "")
        try:
            r = int(raw)
        except (TypeError, ValueError):
            continue
        if r <= max_risk:
            safe.append(row)
    data["products"] = safe
    data["products_filtered"] = len(rows) - len(safe)

    return SkillResult(skill="analyze_wealth", ok=True, data=data,
                       tools_called=called)


__all__ = [
    "CORE_RUNNABLE",
    "SKILLS",
    "run_skill",
    "skill_catalog",
    "tool_in_skill",
]
