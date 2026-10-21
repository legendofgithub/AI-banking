"""agent.core 契约测试：Plan / Guard / 拓扑执行序 / 观察复盘 / 重规划 diff。

为什么单独立一个文件（迭代计划 §13.2「新增测试」的落地）
------------------------------------------------------
Core 的契约是"改进项目能否自称 Agent"的地基：计划能不能被校验、高风步骤
能不能被拦、重规划能不能只改受影响步骤。这些必须在不建库、不调 LLM 的前提下
毫秒级验证，所以直接对 ``agent/core.py`` 的纯函数下断言。

三条银行红线在这里被钉死：
1. Planner 规划的越权工具 / 自动确认措辞 → Guard 必须拦下来；
2. HIGH 步骤没有闸门 → 不允许执行；
3. 重规划只输出 diff 并保留已完成步骤的观察结果，不整段重跑。
"""

from __future__ import annotations

import pytest

from agent.core import (
    MAX_REPLANS,
    MAX_STEPS,
    PLAN_DONE,
    STEP_DELEGATED,
    STEP_DONE,
    STEP_FAILED,
    SkillResult,
    SkillSpec,
    add_step,
    add_trace,
    bump_version,
    diff_plans,
    get_step,
    new_plan,
    observe,
    reflect,
    schedule,
    set_step,
    validate_plan,
)

# 测试用技能目录：与 agent/skills.py 的风险/闸门口径保持一致
REGISTRY: dict[str, SkillSpec] = {
    spec.name: spec for spec in (
        SkillSpec(name="analyze_bill", description="账单", kind="analysis",
                  risk="READ", success="返回月报与分类"),
        SkillSpec(name="analyze_wealth", description="理财", kind="analysis",
                  risk="READ", success="返回持仓与候选产品"),
        SkillSpec(name="plan_birthday", description="生日", kind="analysis",
                  risk="HIGH", gate="confirm_plan", success="锁定单 executed"),
    )
}


def _bill_plan() -> dict:
    plan = new_plan(plan_id="p1", goal="分析上个月账单", intent="bill")
    plan = add_step(plan, step_id="s1", skill="analyze_bill",
                    args={"month": "2026-09"}, depends_on=[],
                    risk="READ", gate=None, success="返回月报")
    return plan


def _wealth_step(plan: dict, *, dep: str = "s1") -> dict:
    return add_step(plan, step_id="s2", skill="analyze_wealth",
                    args={}, depends_on=[dep], risk="READ", gate=None,
                    success="返回持仓与候选产品")


def _birthday_step(plan: dict, *, dep: str = "s1") -> dict:
    return add_step(plan, step_id="s3", skill="plan_birthday",
                    args={"budget_yuan": "1000"}, depends_on=[dep],
                    risk="HIGH", gate="confirm_plan", success="锁定单 executed")


# ------------------------------------------------------------ Guard：合法计划

def test_valid_plan_passes_guard():
    plan = _bill_plan()
    assert validate_plan(plan, REGISTRY) == []


def test_two_skill_plan_passes_guard():
    """验收第 3 条：一个计划可以组合两个以上技能（账单 + 理财）。"""
    plan = _wealth_step(_bill_plan())
    assert validate_plan(plan, REGISTRY) == []
    assert schedule(plan) == ["s1", "s2"]


def test_bill_then_birthday_plan_passes_guard():
    plan = _birthday_step(_bill_plan())
    assert validate_plan(plan, REGISTRY) == []
    assert schedule(plan) == ["s1", "s3"]


# ------------------------------------------------------------ Guard：拦截

def test_missing_goal_and_steps_are_rejected():
    errors = validate_plan(new_plan(), REGISTRY)
    assert any("goal" in e for e in errors)
    assert any("steps" in e for e in errors)


def test_too_many_steps_is_rejected():
    plan = new_plan(goal="压测")
    for i in range(MAX_STEPS + 1):
        plan = add_step(plan, step_id=f"s{i}", skill="analyze_bill",
                        success="ok", risk="READ")
    assert any("超过上限" in e for e in validate_plan(plan, REGISTRY))


def test_unknown_skill_is_rejected():
    plan = add_step(new_plan(goal="x"), step_id="s1", skill="read_all_money",
                    success="ok", risk="READ")
    assert any("技能目录外" in e for e in validate_plan(plan, REGISTRY))


def test_high_risk_step_without_gate_is_rejected():
    plan = add_step(new_plan(goal="x"), step_id="s1", skill="plan_birthday",
                    success="ok", risk="HIGH", gate=None)
    assert any("缺少闸门" in e for e in validate_plan(plan, REGISTRY))


def test_illegal_gate_is_rejected():
    plan = add_step(new_plan(goal="x"), step_id="s1", skill="plan_birthday",
                    success="ok", risk="HIGH", gate="auto_approve")
    assert any("闸门非法" in e for e in validate_plan(plan, REGISTRY))


def test_missing_success_criteria_is_rejected():
    plan = add_step(new_plan(goal="x"), step_id="s1", skill="analyze_bill",
                    risk="READ")
    assert any("缺少成功条件" in e for e in validate_plan(plan, REGISTRY))


def test_duplicate_and_dangling_step_ids_are_rejected():
    plan = _bill_plan()
    plan = add_step(plan, step_id="s1", skill="analyze_bill", risk="READ",
                    success="ok")
    assert any("id 重复" in e for e in validate_plan(plan, REGISTRY))

    plan2 = add_step(_bill_plan(), step_id="s2", skill="analyze_wealth",
                     depends_on=["不存在"], risk="READ", success="ok")
    assert any("依赖了不存在的步骤" in e for e in validate_plan(plan2, REGISTRY))


def test_cyclic_dependency_is_rejected():
    plan = _bill_plan()
    plan = add_step(plan, step_id="s2", skill="analyze_wealth",
                    depends_on=["s3"], risk="READ", success="ok")
    plan = add_step(plan, step_id="s3", skill="analyze_wealth",
                    depends_on=["s2"], risk="READ", success="ok")
    assert any("依赖存在环" in e for e in validate_plan(plan, REGISTRY))


def test_confirm_tools_are_forbidden_in_plan():
    """Planner 绝不能直接规划受闸门保护的确认/扣款类底层工具。"""
    plan = _bill_plan()
    plan = set_step(plan, "s1", tools_called=["confirm_transfer_order"])
    assert any("受限底层工具" in e for e in validate_plan(plan, REGISTRY))

    plan2 = set_step(add_step(new_plan(goal="x"), step_id="s1",
                              skill="analyze_bill", risk="READ", success="ok"),
                     "s1", tools=["cancel_subscription"])
    assert any("试图规划受限工具" in e for e in validate_plan(plan2, REGISTRY))


def test_auto_confirm_wording_in_goal_is_rejected():
    plan = add_step(new_plan(goal="自动确认并自动扣款"), step_id="s1",
                    skill="analyze_bill", risk="READ", success="ok")
    assert any("越权措辞" in e for e in validate_plan(plan, REGISTRY))


def test_replan_limit_is_enforced():
    plan = _bill_plan()
    plan["plan_version"] = MAX_REPLANS + 2
    assert any("重规划次数超过上限" in e for e in validate_plan(plan, REGISTRY))


# ------------------------------------------------------------ 拓扑序

def test_schedule_respects_dependencies_not_declaration_order():
    plan = _bill_plan()
    plan = add_step(plan, step_id="s2", skill="plan_birthday", depends_on=["s3"],
                    risk="HIGH", gate="confirm_plan", success="ok")
    plan = add_step(plan, step_id="s3", skill="analyze_wealth",
                    depends_on=["s1"], risk="READ", success="ok")
    assert schedule(plan) == ["s1", "s3", "s2"]


def test_schedule_on_cycle_returns_partial_order():
    plan = _bill_plan()
    plan = add_step(plan, step_id="s2", skill="analyze_wealth",
                    depends_on=["s3"], risk="READ", success="ok")
    plan = add_step(plan, step_id="s3", skill="analyze_wealth",
                    depends_on=["s2"], risk="READ", success="ok")
    assert len(schedule(plan)) < 3


# ------------------------------------------------------------ Observe/Reflect

def test_observe_marks_done_and_records_tools():
    plan = _bill_plan()
    plan = observe(plan, "s1", SkillResult(skill="analyze_bill", ok=True,
                                           data={"month": "2026-09"},
                                           tools_called=["monthly_report"]))
    step = get_step(plan, "s1")
    assert step["status"] == STEP_DONE
    assert step["observation"] == {"month": "2026-09"}
    assert step["tools_called"] == ["monthly_report"]


def test_observe_tool_ok_but_criteria_unmet_is_failure():
    """工具没报错 ≠ 成功：成功条件由 ok 字段显式表达。"""
    plan = _bill_plan()
    plan = observe(plan, "s1", SkillResult(skill="analyze_bill", ok=False,
                                           reason="月报期间不一致"))
    assert get_step(plan, "s1")["status"] == STEP_FAILED
    reflection = reflect(plan, REGISTRY)
    assert reflection.status == "recoverable"
    assert "s1" in reflection.failed_steps
    assert any("检查成功条件" in f or "返回月报" in f for f in reflection.fixes)


def test_reflect_all_done_is_success():
    plan = _bill_plan()
    plan = observe(plan, "s1", SkillResult(skill="analyze_bill", ok=True))
    assert reflect(plan, REGISTRY).status == "success"


def test_reflect_delegated_money_step_counts_as_success():
    plan = _birthday_step(_bill_plan())
    plan = observe(plan, "s1", SkillResult(skill="analyze_bill", ok=True))
    plan = set_step(plan, "s3", status=STEP_DELEGATED)
    reflection = reflect(plan, REGISTRY)
    assert reflection.status == "success"
    assert "plan_birthday" in reflection.summary


def test_reflect_pending_step_is_failure_not_silent_success():
    assert reflect(_bill_plan(), REGISTRY).status == "failed"


def test_empty_plan_reflects_failure():
    assert reflect(new_plan(), REGISTRY).status == "failed"


# ------------------------------------------------------------ 重规划与 diff

def test_diff_detects_added_removed_changed_steps():
    old = _wealth_step(_bill_plan())
    new = add_step(old, step_id="s3", skill="plan_birthday", depends_on=["s1"],
                   risk="HIGH", gate="confirm_plan", success="锁定单 executed")
    new = set_step(new, "s2", args={"keyword": "债基"})
    # 删掉 s2 的版本
    trimmed = {**new, "steps": [s for s in new["steps"] if s["id"] != "s2"]}
    d = diff_plans(new, trimmed)
    assert d["removed"] == ["s2"]
    assert d["changed"] == {}

    d2 = diff_plans(old, new)
    assert d2["added"] == ["s3"]
    assert d2["changed"].get("s2") == ["args"]


def test_diff_detects_constraint_and_goal_change():
    old = _bill_plan()
    new = new_plan(plan_id="p1", goal="分析账单并给出方案", constraints=["不动房租"])
    d = diff_plans(old, new)
    assert d["goal_changed"] is True
    assert d["constraints_changed"] is True


def test_replan_keeps_done_steps_and_bumps_version():
    """验收第 4 条：已完成步骤不重跑，只改受影响步骤。"""
    plan = _bill_plan()
    plan = observe(plan, "s1", SkillResult(skill="analyze_bill", ok=True,
                                           data={"month": "2026-09"}))
    assert int(plan["plan_version"]) == 1

    amended = set_step(plan, "s1", args={"month": "2026-08"})
    replanned = bump_version(amended, diff_plans(plan, amended))
    assert int(replanned["plan_version"]) == 2
    # 已完成的观察结果被保留（不重跑）
    assert get_step(replanned, "s1")["observation"] == {"month": "2026-09"}
    assert get_step(replanned, "s1")["args"] == {"month": "2026-08"}
    assert replanned["diff_from_version"]["changed"] == {"s1": ["args"]}
    assert [t["phase"] for t in replanned["trace"]][-1] == "replan"


def test_trace_records_phases_for_replay():
    plan = add_trace(_bill_plan(), "perceive", note="读到上次关注餐饮")
    plan = add_trace(plan, "exec", step="s1", status=STEP_DONE)
    phases = [t["phase"] for t in plan["trace"]]
    assert phases == ["perceive", "exec"]
    assert plan["trace"][0]["plan_version"] == 1


def test_plan_status_transitions_are_explicit():
    plan = {**_bill_plan(), "status": PLAN_DONE}
    assert plan["status"] == "done"


@pytest.mark.parametrize("patch,expect_key", [
    ({"risk": "MEDIUM"}, "风险等级非法"),
    ({"gate": "confirm_unknown"}, "闸门非法"),
    ({"success": "  "}, "成功条件"),
])
def test_common_step_defects_are_caught(patch: dict, expect_key: str):
    plan = _bill_plan()
    for key, value in patch.items():
        plan = set_step(plan, "s1", **{key: value})
    assert any(expect_key in e for e in validate_plan(plan, REGISTRY))
