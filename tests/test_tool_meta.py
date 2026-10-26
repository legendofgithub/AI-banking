"""工具身份证注册表对账 + 白名单视图测试（fail-closed 三道闸）。

对应的迭代内容（借鉴 Claude Code 的 Tool 契约/角色隔离，知识库
ClaudeCode-Source-Analysis 方法论落地第一弹）：
- **对账闸**：tool_meta.REGISTRY 必须与 FastMCP 实际注册的 45 个工具
  逐一对账（多/少/风险等级漂移都红），docstring 标签与注册表口径一致；
- **不变量闸**：动钱面、两步类、不可逆类、敏感类各自收敛在显式清单里；
- **白名单闸**：CORE_RUNNABLE 分析技能只能看到只读工具（跨层守卫），
  BankTools.scoped() 视图白名单外调用必须炸（ToolScopeError），
  run_skill 在真实链路上确实套上了 scoped 视图。

mcp_server 导入会在模块级 init_db：先 setdefault BANK_CORE_DB 到 tmp 固定
路径，避免测试期间碰 data/bank.db（与 _mcp_inprocess 的隔离语义一致）。
"""

from __future__ import annotations

import asyncio
import os
import tempfile

os.environ.setdefault(
    "BANK_CORE_DB",
    os.path.join(tempfile.gettempdir(), "bank_toolmeta_test.db"))

import pytest

from agent.bank import REQUIRED_TOOLS, BankTools, ScopedBankTools, ToolScopeError
from agent.skills import CORE_RUNNABLE, SKILLS, run_skill
from bank_core import tool_meta
from bank_core.tool_meta import (
    MONEY_TOOLS,
    REGISTRY,
    SCENARIOS,
    parse_docstring_risk,
    read_only_tools,
    tools_for_scenario,
    validate_registry,
)


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ 对账闸

def _real_tools_with_doc_risk() -> dict[str, str | None]:
    from bank_core import mcp_server
    tools = run(mcp_server.mcp.list_tools())
    return {t.name: parse_docstring_risk(getattr(t.fn, "__doc__", None))
            for t in tools}


def test_registry_matches_fastmcp_exactly():
    """注册表与真实注册的 45 个工具逐一对账：少登记/多登记/风险漂移都算失败。"""
    expected = _real_tools_with_doc_risk()
    assert len(expected) == 45, f"实际注册 {len(expected)} 个工具，与预期 45 不符"
    assert validate_registry(expected) == []


def test_every_docstring_carries_risk_tag():
    """45 个工具的 docstring 必须全部带 [READ]/[LOW]/[MED]/[HIGH] 标签。"""
    expected = _real_tools_with_doc_risk()
    untagged = sorted(n for n, r in expected.items() if r is None)
    assert untagged == [], f"这些工具的 docstring 缺风险标签: {untagged}"


def test_required_tools_all_registered_in_meta():
    """转账/AA 硬依赖工具（agent.bank.REQUIRED_TOOLS）必须都有身份证。"""
    missing = [n for n in REQUIRED_TOOLS if n not in REGISTRY]
    assert missing == []


# ------------------------------------------------------------------ 不变量闸

def test_money_surface_is_exactly_four_tools():
    """真正动钱的工具只有 4 个：转账确认/理财申购/赎回/联动动作执行。

    这是最小动钱面——新增动钱工具必须改这里（改的时候自然会想清楚）。
    """
    assert MONEY_TOOLS == {
        "confirm_transfer_order", "subscribe_product",
        "redeem_product", "execute_linkage_action",
    }


def test_special_flags_are_pinned():
    """两步/不可逆/敏感三类旗标收敛在显式清单。"""
    two_step = {n for n, m in REGISTRY.items() if m.two_step}
    assert two_step == {
        "create_transfer_order", "subscribe_product",
        "redeem_product", "create_linkage_plan",
    }
    destructive = {n for n, m in REGISTRY.items() if m.destructive}
    assert destructive == {"set_card_status"}  # 挂失不可逆
    sensitive = {n for n, m in REGISTRY.items() if m.sensitive}
    assert sensitive == {"verify_pay_password"}


def test_high_risk_implies_gated():
    """所有 HIGH 工具都不可并发、不可只读——闸门语义的兜底断言。"""
    for n, m in REGISTRY.items():
        if m.risk == "HIGH":
            assert not m.concurrency_safe, n
            assert not m.read_only, n


def test_fail_closed_defaults():
    """fail-closed：新登记的工具若不声明安全属性，一律按危险处理。"""
    m = tool_meta.ToolMeta("some_new_tool", "MED", scenarios=("common",))
    assert m.read_only is False
    assert m.concurrency_safe is False
    assert m.money is False


# ------------------------------------------------------------------ 白名单闸

def test_core_runnable_skills_only_touch_readonly_tools():
    """跨层守卫：Core 可自主执行的分析技能，白名单必须全部是只读工具。

    这是 skills.py「Core 不直接跑资金技能」承诺的数据级校验——
    就算日后有人往 analyze_* 白名单里塞进动钱工具，这里直接红。
    """
    ro = read_only_tools()
    for skill in sorted(CORE_RUNNABLE):
        outside = [t for t in SKILLS[skill].tools if t not in ro]
        assert outside == [], f"技能 {skill} 白名单里有非只读工具: {outside}"


def test_every_skill_whitelist_tools_exist_in_registry():
    """六个技能白名单里的工具名都必须有身份证（防手滑写错工具名）。"""
    unknown = sorted(
        t for spec in SKILLS.values() for t in spec.tools if t not in REGISTRY)
    assert unknown == []


# ------------------------------------------------------------------ 场景挂载

def test_tools_for_scenario_excludes_money_by_default():
    """场景过滤 fail-closed：默认不下发动钱工具，显式 include_money 才给。"""
    for scenario in sorted(SCENARIOS):
        tools = tools_for_scenario(scenario)
        assert tools, f"场景 {scenario} 过滤结果为空"
        leaked = [t for t in tools if t in MONEY_TOOLS]
        assert leaked == [], f"场景 {scenario} 意外包含动钱工具: {leaked}"
    assert "confirm_transfer_order" in tools_for_scenario("transfer",
                                                          include_money=True)


def test_tools_for_scenario_rejects_unknown():
    with pytest.raises(ValueError):
        tools_for_scenario("no_such_scene")


# ------------------------------------------------------------------ 对账函数自身

def test_validate_registry_reports_all_three_drifts():
    """validate_registry 的三类漂移消息都要能报出来（对账器自己的单测）。"""
    real_name = next(iter(REGISTRY))  # get_accounts，risk=READ
    problems = validate_registry({
        "brand_new_tool": "READ",        # 已注册但没登记 → 缺少元数据
        real_name: "HIGH",               # 风险等级对不上 → 风险漂移
        "ghost_tool": None,              # 期望里有但没注册 → 缺少元数据 + 其余全变多余
    })
    assert any("缺少元数据" in p for p in problems)
    assert any("注册表多余" in p for p in problems)
    assert any("风险漂移" in p for p in problems)


# ------------------------------------------------------------------ scoped 视图

class _FakeTool:
    """最小工具替身：ainvoke 返回固定值，供 BankTools/Scoped 视图测试。"""

    def __init__(self, payload):
        self.payload = payload

    async def ainvoke(self, args=None):
        return [{"type": "text", "text": f'{{"ok": "{self.payload}"}}'}]


def _bank_tools() -> BankTools:
    return BankTools({
        "get_accounts": _FakeTool("accounts"),
        "monthly_report": _FakeTool("report"),
        "confirm_transfer_order": _FakeTool("money!"),
    }, db_path="test.db")


def test_scoped_allows_whitelisted_call():
    view = _bank_tools().scoped({"get_accounts", "monthly_report"},
                                label="test:分析")
    assert isinstance(view, ScopedBankTools)
    assert view.names == ["get_accounts", "monthly_report"]
    assert view.db_path == "test.db"
    result = run(view.call("get_accounts"))
    assert result == {"ok": "accounts"}


def test_scoped_blocks_outside_call_loudly():
    """白名单外调用必须炸（ToolScopeError），而不是静默返回——编排层 bug 要当场现形。"""
    view = _bank_tools().scoped({"monthly_report"}, label="test:分析")
    with pytest.raises(ToolScopeError, match="confirm_transfer_order"):
        run(view.call("confirm_transfer_order"))


def test_scoped_still_returns_bank_errors_as_dicts():
    """视图不改变 BankTools 语义：工具未加载等银行侧错误仍以 {"error": ...} 返回，不抛。"""
    parent = BankTools({"monthly_report": _FakeTool("report")}, db_path="test.db")
    view = parent.scoped({"get_accounts"}, label="test")
    result = run(view.call("get_accounts"))  # 白名单内，但父集合里没这个工具
    assert isinstance(result, dict) and "error" in result


# ------------------------------------------------------------------ run_skill 接线

class _ScopedFakeTools:
    """带 scoped 能力的最小替身：记录 scoped 调用并放行白名单内工具。"""

    def __init__(self, mapping):
        self.mapping = dict(mapping)
        self.scoped_log: list[tuple[frozenset, str]] = []

    def scoped(self, allow, *, label="scoped"):
        self.scoped_log.append((frozenset(allow), label))
        return self

    async def call(self, name, /, **args):
        return self.mapping.get(name, {"error": f"工具 {name} 未注册"})


def test_run_skill_applies_scoped_view_with_catalog_whitelist():
    """run_skill 必须把工具锁进技能目录白名单——scoped 的 allow 就是 spec.tools。"""
    fake = _ScopedFakeTools({
        "monthly_report": {"month": "2026-09", "top_categories": [
            {"category": "餐饮", "amount_yuan": "100.00", "pct": 100.0,
             "count": 1}]},
        "category_summary": {"items": [{"category": "餐饮"}]},
        "top_merchants": [{"merchant": "山姆"}],
        "detect_anomalies": [],
        "list_subscriptions": [],
    })
    result = run(run_skill(fake, "analyze_bill", {}))
    assert result.ok, result.reason
    assert len(fake.scoped_log) == 1
    allow, label = fake.scoped_log[0]
    assert allow == frozenset(SKILLS["analyze_bill"].tools)
    assert label == "skill:analyze_bill"
    # 技能只调了白名单内的工具（tools_called 是工具名列表）
    assert set(result.tools_called) <= allow
