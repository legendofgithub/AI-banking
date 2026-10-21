"""只读分析技能测试：账单/理财事实行、卡片视图、风险过滤、空数据、工具白名单。

覆盖迭代计划 §13.2 的三件事：
- **卡片与复盘纯函数**：同一份观察结果必然得到同一张卡（全程无 LLM）；
- **风险过滤**：候选产品风险等级必须全部 ≤ 用户等级（适当性红线）；
- **空数据不装成功**：工具返回值缺失时事实行写明"无数据"，主查询失败则整步失败。

异步用例沿用既有 `asyncio.run(...)` 写法（项目没装 pytest-asyncio）。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.analysis import (
    ANALYSIS_HINT,
    bill_facts,
    build_analysis_card,
    diff_text,
    failed_note,
    wealth_facts,
)
from agent.core import Reflection, SkillResult, add_step, new_plan, observe
from agent.skills import (
    CORE_RUNNABLE,
    SKILLS,
    run_skill,
    skill_catalog,
    tool_in_skill,
)


def run(coro):
    return asyncio.run(coro)


class FakeTools:
    """最小 BankTools 替身：按名字返回预置结果并记录调用。

    与真实 BankTools.call 同语义：**错误以 {"error": ...} 返回**，不抛异常。
    """

    def __init__(self, mapping: dict):
        self.mapping = dict(mapping)
        self.calls: list[tuple[str, dict]] = []

    async def call(self, name: str, /, **args):
        self.calls.append((name, args))
        return self.mapping.get(name, {"error": f"工具 {name} 未注册"})

    @property
    def called_names(self) -> list[str]:
        return [n for n, _ in self.calls]


# ------------------------------------------------------------------ 造数据

REPORT = {"month": "2026-09", "income_yuan": "18500.00",
          "expense_yuan": "9800.00", "net_yuan": "8700.00",
          "mom_expense_pct": 12.5,
          "top_categories": [{"category": "餐饮", "amount_yuan": "3200.00",
                              "pct": 32.6, "count": 40}]}
CATEGORIES = {"items": [{"category": "餐饮", "amount_yuan": "3200.00",
                         "pct": 32.6, "count": 40},
                        {"category": "交通", "amount_yuan": "800.00",
                         "pct": 8.1, "count": 12}]}
MERCHANTS = [{"merchant": "山姆", "amount_yuan": "2600.00", "count": 6}]
ANOMALIES = [{"rule": "R2_大额离群", "evidence": "某商户本笔远超历史均值"}]
SUBS = [{"sub_id": 3, "merchant": "腾讯视频", "amount_yuan": "30.00",
         "status": "active"}]

BILL_TOOLS = FakeTools({
    "monthly_report": REPORT,
    "category_summary": CATEGORIES,
    "top_merchants": MERCHANTS,
    "detect_anomalies": ANOMALIES,
    "list_subscriptions": SUBS,
})

HOLDINGS = [{"holding_id": 1, "product": "稳健纯债基金", "code": "000001",
             "risk_level": "R2", "principal_yuan": "10000.00",
             "est_value_yuan": "10180.00", "profit_yuan": "180.00",
             "profit_pct": 1.8}]
PRODUCTS = [
    {"id": 1, "name": "现金管理", "risk_level": "R1", "expected_return_pct": 2.1},
    {"id": 2, "name": "稳健纯债", "risk_level": "R2", "expected_return_pct": 3.5},
    {"id": 3, "name": "股票混合", "risk_level": "R4", "expected_return_pct": 8.0},
]


PROFILE_C3 = {"level": "C3", "score": 14}


def wealth_tools(profile=PROFILE_C3, products=None) -> FakeTools:
    """profile 显式传 None 表示"用户未做风险测评"（真实工具的返回也是 None）。"""
    return FakeTools({
        "get_risk_profile": profile,
        "get_holdings": HOLDINGS,
        "list_wealth_products": products if products is not None else PRODUCTS,
    })


# ------------------------------------------------------------ 账单事实行

def test_bill_facts_rows_are_deterministic():
    rows = {r["label"]: r["value"] for r in bill_facts(
        report=REPORT, categories=CATEGORIES, merchants=MERCHANTS,
        anomalies=ANOMALIES, subscriptions=SUBS)}
    assert rows["统计期间"] == "2026-09"
    assert rows["收入"] == "¥18500.00"
    assert rows["支出"] == "¥9800.00"
    assert rows["结余"] == "¥8700.00"
    assert rows["支出环比"] == "+12.5%"
    assert rows["最大支出分类"] == "餐饮 ¥3200.00（32.6%）"
    assert rows["Top 商户"] == "山姆 ¥2600.00"
    assert rows["异常交易"] == "1 笔"
    assert rows["订阅支出"] == "1 项 • 建议↓"


def test_bill_facts_empty_data_says_no_data():
    rows = {r["label"]: r["value"] for r in bill_facts()}
    assert rows["统计期间"] == "月报不可用"
    assert rows["收入"] == "¥0.00"
    assert rows["最大支出分类"] == "无数据"
    assert rows["Top 商户"] == "无数据"
    assert rows["异常交易"] == "0 笔"


def test_bill_facts_mom_none_renders_dash_not_zero():
    rows = {r["label"]: r["value"] for r in
            bill_facts(report={"month": "2026-09", "mom_expense_pct": None})}
    assert rows["支出环比"] == "—"


def test_bill_facts_picks_max_category_not_first_row():
    cats = {"items": [{"category": "小额", "amount_yuan": "10.00", "pct": 1.0},
                      {"category": "大额", "amount_yuan": "9999.00", "pct": 99.0}]}
    rows = {r["label"]: r["value"] for r in
            bill_facts(report=REPORT, categories=cats)}
    assert rows["最大支出分类"].startswith("大额")


# ------------------------------------------------------------ 理财事实行

def test_wealth_facts_show_value_pnl_and_concentration():
    rows = {r["label"]: r["value"] for r in
            wealth_facts(profile={"level": "C3"}, holdings=HOLDINGS,
                         products=PRODUCTS[:2], max_risk_level=3)}
    assert rows["风险等级"] == "C3"
    assert rows["持仓市值"] == "¥10180.00"
    assert rows["累计盈亏"] == "¥180.00"
    assert rows["最大持仓占比"] == "100.0%（稳健纯债基金）"
    assert rows["候选产品"] == "2 只"


def test_wealth_facts_without_profile_marks_unassessed():
    rows = {r["label"]: r["value"] for r in
            wealth_facts(profile=None, holdings=[], products=[],
                         max_risk_level=1)}
    assert rows["风险等级"] == "未测评"
    assert rows["持仓市值"] == "¥0.00"
    assert rows["最大持仓占比"] == "无数据"


def test_wealth_facts_sums_multiple_holdings_in_cents():
    rows = {r["label"]: r["value"] for r in
            wealth_facts(profile={"level": "C2"},
                         holdings=HOLDINGS + [
                             {"product": "货币基金", "principal_yuan": "5000.00",
                              "est_value_yuan": "5000.10",
                              "profit_yuan": "0.10"}],
                         products=[], max_risk_level=2)}
    assert rows["持仓市值"] == "¥15180.10"
    assert rows["累计盈亏"] == "¥180.10"


# ------------------------------------------------------------ 分析卡视图

def _observed_plan(skills=("analyze_bill",)) -> dict:
    plan = new_plan(plan_id="aplan-1", goal="分析上个月账单",
                    constraints=["不动房租"], memory_used=["上次关注餐饮"])
    for i, skill in enumerate(skills, start=1):
        plan = add_step(plan, step_id=f"s{i}", skill=skill, args={},
                        depends_on=([f"s{i - 1}"] if i > 1 else []),
                        risk="READ", gate=None, success="返回数据")
    return plan


def test_analysis_card_shape_is_frozen():
    card = build_analysis_card(_observed_plan(), intent="bill",
                               bill={"report": REPORT,
                                     "categories": CATEGORIES})
    assert set(card) == {"plan_id", "plan_version", "goal", "intent", "hint",
                         "facts", "steps", "reflection", "memory_used",
                         "trace", "diff"}
    assert card["plan_id"] == "aplan-1"
    assert card["intent"] == "bill"
    assert card["memory_used"] == ["上次关注餐饮"]
    assert card["hint"] == ANALYSIS_HINT


def test_analysis_card_fact_row_shape():
    card = build_analysis_card(_observed_plan(), intent="bill",
                               bill={"report": REPORT})
    assert card["facts"]
    for row in card["facts"]:
        assert set(row) == {"label", "value", "hint"}


def test_analysis_card_step_row_shape_and_status():
    plan = observe(_observed_plan(("analyze_bill", "analyze_wealth")), "s1",
                   SkillResult(skill="analyze_bill", ok=True))
    card = build_analysis_card(plan, intent="bill", bill={"report": REPORT})
    assert [s["id"] for s in card["steps"]] == ["s1", "s2"]
    for step in card["steps"]:
        assert set(step) == {"id", "skill", "title", "risk", "gate", "status"}
    assert card["steps"][0]["status"] == "done"
    assert card["steps"][0]["title"].startswith("读取账单")


def test_analysis_card_switch_to_wealth_facts():
    card = build_analysis_card(_observed_plan(), intent="wealth",
                               wealth={"profile": {"level": "C3"},
                                       "holdings": HOLDINGS,
                                       "products": PRODUCTS[:2],
                                       "max_risk_level": 3})
    labels = [r["label"] for r in card["facts"]]
    assert "风险等级" in labels
    assert "统计期间" not in labels


def test_analysis_card_carries_replan_diff():
    """重规划"改了什么"必须在卡片上可见(迭代计划 §12 验收第 4 条)。"""
    plan = _observed_plan()
    amended = {**plan, "steps": [
        {**plan["steps"][0], "args": {"month": "2026-08"}}],
               "plan_version": 2}
    amended["diff_from_version"] = {"added": [], "removed": [],
                                    "changed": {"s1": ["args"]}}
    card = build_analysis_card(amended, intent="bill",
                               bill={"report": REPORT})
    assert card["diff"]["changed"] == {"s1": ["args"]}
    assert diff_text(card["diff"]) == "修改步骤 s1"


def test_diff_text_empty_without_replan():
    assert diff_text(None) == ""
    assert diff_text({"added": [], "removed": [], "changed": {}}) == ""


def test_analysis_card_carries_reflection():
    refl = Reflection(status="success", summary="全部步骤通过成功条件")
    card = build_analysis_card(_observed_plan(), intent="bill",
                               bill={"report": REPORT}, reflection=refl)
    assert card["reflection"] == {"status": "success",
                                  "summary": "全部步骤通过成功条件"}


def test_failed_note_names_the_failed_step():
    plan = observe(_observed_plan(), "s1",
                   SkillResult(skill="analyze_bill", ok=False,
                               reason="期间不一致"))
    assert failed_note(plan) == "步骤 s1 未通过成功条件"
    assert failed_note(_observed_plan()) == ""


# ------------------------------------------------------------ 技能目录

def test_catalog_has_six_skills():
    assert len(skill_catalog()) == 6
    assert set(SKILLS) == {"analyze_bill", "analyze_wealth", "plan_birthday",
                           "execute_transfer", "execute_card",
                           "execute_subscription"}


def test_every_high_risk_skill_declares_a_gate():
    for name, spec in SKILLS.items():
        if spec.risk == "HIGH":
            assert spec.gate, f"{name} 是高风险技能却没有声明闸门"


def test_only_analysis_skills_are_core_runnable():
    assert CORE_RUNNABLE == {"analyze_bill", "analyze_wealth"}


def test_tool_whitelist_check():
    assert tool_in_skill("analyze_bill", "monthly_report") is True
    assert tool_in_skill("analyze_bill", "confirm_transfer_order") is False
    assert tool_in_skill("不存在", "monthly_report") is False


# ------------------------------------------------------------ 账单技能适配器

def test_analyze_bill_success_and_tools_whitelisted():
    async def scenario():
        result = await run_skill(BILL_TOOLS, "analyze_bill",
                                 {"month": "2026-09", "start": "2026-09-01",
                                  "end": "2026-10-01", "days": 90})
        assert result.ok is True
        assert result.data["report"]["month"] == "2026-09"
        for tool in result.tools_called:
            assert tool_in_skill("analyze_bill", tool), tool
        assert set(BILL_TOOLS.called_names) == {
            "monthly_report", "category_summary", "top_merchants",
            "detect_anomalies", "list_subscriptions"}
    run(scenario())


def test_analyze_bill_fails_on_report_error():
    async def scenario():
        tools = FakeTools({**BILL_TOOLS.mapping,
                           "monthly_report": {"error": "库忙，稍后再试"}})
        result = await run_skill(tools, "analyze_bill", {"month": "2026-09"})
        assert result.ok is False
        assert "月报读取失败" in result.reason
    run(scenario())


def test_analyze_bill_fails_on_period_mismatch():
    """成功条件含"期间一致"：工具没报错但期间对不上，照样不算成功。"""
    async def scenario():
        result = await run_skill(BILL_TOOLS, "analyze_bill",
                                 {"month": "2026-08"})
        assert result.ok is False
        assert "月报期间不一致" in result.reason
    run(scenario())


def test_analyze_bill_falls_back_to_report_categories():
    async def scenario():
        tools = FakeTools({**BILL_TOOLS.mapping,
                           "category_summary": {"items": []},
                           "top_merchants": []})
        result = await run_skill(tools, "analyze_bill",
                                 {"month": "2026-09", "start": "2026-09-01",
                                  "end": "2026-10-01"})
        # 月报自带 top_categories 兜底 → 仍算成功，但事实行来自月报
        assert result.ok is True
        assert result.data["categories"]["items"][0]["category"] == "餐饮"
    run(scenario())


def test_analyze_bill_tolerates_partial_tool_error():
    """订阅/异常失败不影响主链路成功，但会在数据里缺席（不会编数字）。"""
    async def scenario():
        tools = FakeTools({**BILL_TOOLS.mapping,
                           "list_subscriptions": {"error": "订阅表被占用"},
                           "detect_anomalies": {"error": "异常扫描超时"}})
        result = await run_skill(tools, "analyze_bill",
                                 {"month": "2026-09", "start": "2026-09-01",
                                  "end": "2026-10-01"})
        assert result.ok is True
        assert "subscriptions" not in result.data
        assert "anomalies" not in result.data
    run(scenario())


# ------------------------------------------------------------ 理财技能适配器

def test_analyze_wealth_filters_products_above_level():
    async def scenario():
        result = await run_skill(wealth_tools(), "analyze_wealth", {})
        assert result.ok is True
        assert result.data["max_risk_level"] == 3
        assert [p["name"] for p in result.data["products"]] == [
            "现金管理", "稳健纯债"]
        assert result.data["products_filtered"] == 1
    run(scenario())


def test_analyze_wealth_without_profile_locks_to_r1():
    async def scenario():
        result = await run_skill(wealth_tools(profile=None),
                                 "analyze_wealth", {})
        assert result.ok is True
        assert result.data["needs_assessment"] is True
        assert result.data["max_risk_level"] == 1
        assert [p["name"] for p in result.data["products"]] == ["现金管理"]
    run(scenario())


def test_analyze_wealth_no_product_escapes_level_filter():
    """即使工具返回了越级产品，适配器也必须在出参前过滤掉（二次确定性守卫）。"""
    async def scenario():
        result = await run_skill(wealth_tools(profile={"level": "C1"},
                                              products=PRODUCTS),
                                 "analyze_wealth", {})
        assert result.ok is True
        assert all(p["risk_level"] == "R1" for p in result.data["products"])
    run(scenario())


def test_analyze_wealth_fails_on_holdings_error():
    async def scenario():
        tools = FakeTools({**wealth_tools().mapping,
                           "get_holdings": {"error": "持仓查询失败"}})
        result = await run_skill(tools, "analyze_wealth", {})
        assert result.ok is False
        assert "持仓读取失败" in result.reason
    run(scenario())


# ------------------------------------------------------------ 资金技能不可自治

@pytest.mark.parametrize("skill", ["plan_birthday", "execute_transfer",
                                   "execute_card", "execute_subscription"])
def test_money_skills_are_never_executed_by_core(skill: str):
    """迭代计划 §8：资金技能保持受闸门的傻瓜执行，Core 不得自行开跑。"""
    async def scenario():
        tools = FakeTools(dict(BILL_TOOLS.mapping))
        result = await run_skill(tools, skill, {})
        assert result.ok is False
        assert "受闸门保护" in result.reason or "既有" in result.reason
        assert tools.calls == []
    run(scenario())


def test_unknown_skill_is_rejected():
    async def scenario():
        result = await run_skill(BILL_TOOLS, "read_your_private_key", {})
        assert result.ok is False
        assert "技能目录里没有" in result.reason
    run(scenario())
