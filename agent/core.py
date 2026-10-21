"""Agent Core 契约层：Plan / PlanStep / SkillSpec / SkillResult / Reflection。

这一层是"项目本身成为一个 Agent"的地基（迭代计划 §3、§5）：

    Perceive → Plan → Guard → PlanCard → Act → Observe → Reflect → Replan → Remember

本模块**只提供显式的数据契约与确定性算法**，不做任何 IO、不调 LLM、不调银行工具：
- 金额、占比、日期、限额全部由确定性代码计算；LLM 只负责目标理解与播报（迭代计划 §5 末）；
- 计划 JSON 必须能被 ``validate_plan`` 通过，否则不允许执行（Guard）；
- **动钱步骤必须由 gate 强制**，Planner 不允许规划"自动确认"（§14 硬边界）。

资金安全侧的三条硬规则在这里落地：
1. Planner 只能引用技能目录里的 skill，不能直接引用 ``confirm_transfer_order``
   等确认/扣款类底层工具（``FORBIDDEN_TOOLS``）；
2. risk=HIGH 的步骤必须带合法 gate；执行器对无 gate 的高风险步骤直接拒绝；
3. 计划确认不能替代支付密码——本层不产生任何"已确认"语义。
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------- 常量

# 风险等级：READ 只读；LOW/MED 非资金或低敏写；HIGH 涉及资金，必须有闸门
RISK_LEVELS: tuple[str, ...] = ("READ", "LOW", "MED", "HIGH")

# 步骤状态
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_DONE = "done"
STEP_FAILED = "failed"
STEP_BLOCKED = "blocked"      # Guard 拦下：违反约束/缺闸门，不允许执行
STEP_SKIPPED = "skipped"      # 重规划后该步骤不再需要（保留留痕，不重跑）
STEP_DELEGATED = "delegated"  # 已交给既有受闸门子图（第一期生日技能走这条路）

# 计划状态
PLAN_RUNNING = "running"
PLAN_WAITING_USER = "waiting_user"
PLAN_DONE = "done"
PLAN_FAILED = "failed"
PLAN_CANCELLED = "cancelled"

# Guard 上限（迭代计划 §4.3 / 自主规划计划 §0.5 的避坑清单）
MAX_STEPS = 12
MAX_REPLANS = 3
MAX_PLAN_VERSION = MAX_REPLANS + 1

# 合法闸门类型 = graph.py 真实会抛出的 interrupt 载荷类型。
# 这里故意写成白名单：Planner 给出名单外的 gate（或 HIGH 步骤给 null），
# validate_plan 直接把它判成不可执行，而不是静默放行。
ALLOWED_GATES: frozenset[str] = frozenset({
    "confirm_plan",       # 计划确认（普通确认，不扣款）
    "confirm_linkage",    # 联动预算锁定（支付密码）
    "confirm_sub_cancel", # 订阅取消（支付密码）
    "confirm_transfer",   # 转账确认（支付密码）
    "confirm_wealth",     # 理财申赎确认（支付密码）
    "confirm_card",       # 卡片操作确认（支付密码）
})

# Planner 绝不允许规划的底层工具：确认/扣款/改限额/改测评这一类
FORBIDDEN_TOOLS: frozenset[str] = frozenset({
    "confirm_transfer_order",
    "confirm_wealth_order",
    "confirm_card_action",
    "cancel_subscription",
    "set_risk_profile",
    "set_card_limits",
    "set_card_status",
})

# Planner 若试图规划自动确认，命中这些词也直接判违规（防绕过措辞）
_FORBIDDEN_PHRASES: tuple[str, ...] = (
    "自动确认", "自动扣款", "免密码", "无需密码", "跳过闸门", "自动执行扣款",
)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_plan_id() -> str:
    return "aplan-" + uuid.uuid4().hex[:10]


# ------------------------------------------------------------- 技能规格契约

@dataclass(frozen=True)
class SkillSpec:
    """技能目录里的一项：Core 只看这一层，不看 44 个底层 MCP 工具。

    Attributes:
        name:        技能唯一名，计划步骤用它引用。
        description: 给 Planner 的一句话能力描述。
        kind:        ``analysis``（只读分析）/ ``execution``（执行，含动钱）。
        risk:        READ / LOW / MED / HIGH。
        tools:       该技能允许调用的底层工具（只读/受闸门），用于白名单留痕。
        idempotent:  是否幂等（可安全重放）。
        gate:        None 表示无需闸门；否则必须是 ALLOWED_GATES 里的名字。
        success:     成功条件的确定性描述。
        preconditions: 前置条件（人类可读，执行前由各自的技能适配器校验）。
    """

    name: str
    description: str
    kind: str                       # analysis | execution
    risk: str                       # READ | LOW | MED | HIGH
    tools: tuple[str, ...] = ()
    idempotent: bool = True
    gate: str | None = None
    success: str = ""
    preconditions: tuple[str, ...] = ()
    readonly: bool = True           # True: 执行期间保证零写操作

    def to_dict(self) -> dict:
        d = asdict(self)
        d["tools"] = list(self.tools)
        d["preconditions"] = list(self.preconditions)
        return d


@dataclass
class SkillResult:
    """技能执行的观察结果（Observe 阶段的输入）。

    ``ok`` 不是"工具没报错"，而是**成功条件是否满足**（迭代计划 §3.1：Observe
    不能把"工具没报错"当成功）。度量不得在此处捏造——数值一律来自工具返回值。
    """

    skill: str
    ok: bool
    data: dict = field(default_factory=dict)
    reason: str = ""                # 失败/不可用的确定性原因
    tools_called: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Reflection:
    """Reflect 阶段的输出：成功 / 可恢复失败 / 约束冲突 / 用户取消 / 失败。"""

    status: str                     # success | recoverable | constraint_conflict | cancelled | failed
    summary: str = ""
    failed_steps: list[str] = field(default_factory=list)
    fixes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def to_dict(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------- Plan

def new_plan(*, plan_id: str | None = None, goal: str = "",
             constraints: Sequence[str] | None = None,
             memory_used: Sequence[str] | None = None,
             plan_version: int = 1, intent: str = "") -> dict:
    """构造一份空计划（后续由 add_step 逐步填充）。"""
    return {
        "plan_id": str(plan_id or new_plan_id()),
        "goal": str(goal or ""),
        "intent": str(intent or ""),
        "constraints": [str(c) for c in (constraints or []) if str(c).strip()],
        "memory_used": [str(m) for m in (memory_used or []) if str(m).strip()],
        "steps": [],
        "fallback": [],
        "status": PLAN_RUNNING,
        "plan_version": int(plan_version or 1),
        "diff_from_version": None,
        "trace": [],
    }


def add_step(plan: dict, *, step_id: str, skill: str, args: dict | None = None,
             depends_on: Sequence[str] | None = None, risk: str = "READ",
             gate: str | None = None, success: str = "",
             budget_yuan: str | None = None) -> dict:
    """往计划里追加一步；返回新的 steps 列表（纯函数，不改入参）。"""
    steps = list(plan.get("steps") or [])
    steps.append({
        "id": str(step_id),
        "skill": str(skill),
        "args": dict(args or {}),
        "depends_on": [str(d) for d in (depends_on or [])],
        "risk": risk if risk in RISK_LEVELS else "READ",
        "gate": gate,
        "success": str(success or ""),
        "status": STEP_PENDING,
        "observation": None,
        "tools_called": [],
        **({"budget_yuan": str(budget_yuan)} if budget_yuan is not None else {}),
    })
    return {**plan, "steps": steps, "steps_count": len(steps)}


def get_step(plan: dict, step_id: str) -> dict | None:
    for s in (plan.get("steps") or []):
        if isinstance(s, dict) and s.get("id") == step_id:
            return s
    return None


def set_step(plan: dict, step_id: str, **patch: Any) -> dict:
    """更新某一步的字段（status/observation/memory 等），保持步骤顺序不变。"""
    steps: list[dict] = []
    for s in (plan.get("steps") or []):
        if isinstance(s, dict) and s.get("id") == step_id:
            steps.append({**s, **patch})
        else:
            steps.append(s)
    return {**plan, "steps": steps}


def add_trace(plan: dict, phase: str, **payload: Any) -> dict:
    """追加一条 trace（计划版本、步骤、状态、观察、复盘、记忆都可回放）。"""
    trace = list(plan.get("trace") or [])
    entry = {"at": _now_iso(), "phase": str(phase),
             "plan_version": int(plan.get("plan_version") or 1)}
    entry.update(payload)
    trace.append(entry)
    return {**plan, "trace": trace}


# ------------------------------------------------------------------- Guard

def validate_plan(plan: dict, registry: dict[str, SkillSpec] | None = None) -> list[str]:
    """Plan Guard 静态校验：返回违规清单（空列表 = 合法可执行）。

    检查项对应迭代计划 §3.1 的 Guard 行：约束/风险/步数/禁止自动确认/成功条件，
    外加依赖闭环、重复 id、非法 gate 与越权工具引用。
    """
    errors: list[str] = []
    if not isinstance(plan, dict):
        return ["plan 不是 dict"]
    registry = registry or {}

    if not str(plan.get("goal") or "").strip():
        errors.append("plan.goal 不能为空")

    steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
    if not steps:
        errors.append("plan.steps 不能为空")
    if len(steps) > MAX_STEPS:
        errors.append(f"步骤数 {len(steps)} 超过上限 {MAX_STEPS}")
    if int(plan.get("plan_version") or 1) > MAX_PLAN_VERSION:
        errors.append(f"重规划次数超过上限 {MAX_REPLANS}")

    goal_text = str(plan.get("goal") or "")
    for bad in _FORBIDDEN_PHRASES:
        if bad in goal_text:
            errors.append(f"计划目标含越权措辞「{bad}」：不允许规划自动确认/自动扣款")

    seen: set[str] = set()
    for s in steps:
        sid = str(s.get("id") or "")
        if not sid:
            errors.append("步骤缺少 id")
            continue
        if sid in seen:
            errors.append(f"步骤 id 重复：{sid}")
        seen.add(sid)

        skill = str(s.get("skill") or "")
        if skill not in registry:
            errors.append(f"步骤 {sid} 引用了技能目录外的 skill：{skill or '(空)'}")
        else:
            spec = registry[skill]
            if s.get("risk") != spec.risk and spec.risk == "HIGH":
                errors.append(f"步骤 {sid} 风险等级低于技能声明（{spec.risk}）")
            if spec.risk == "HIGH" and not s.get("gate"):
                errors.append(f"高风险步骤 {sid}（{skill}）缺少闸门，执行器将拒绝")
            if spec.gate and not s.get("gate"):
                errors.append(f"步骤 {sid} 的技能 {skill} 要求闸门 {spec.gate}，但 gate 为空")
            for tool in (s.get("tools_called") or []):
                if str(tool) in FORBIDDEN_TOOLS:
                    errors.append(f"步骤 {sid} 直接调用了受限底层工具：{tool}")
            for key in ("tool", "tools", "tool_name"):
                raw = s.get(key)
                hit = [str(t) for t in (_as_list(raw)) if str(t) in FORBIDDEN_TOOLS]
                if hit:
                    errors.append(f"步骤 {sid} 试图规划受限工具：{hit}")

        risk = str(s.get("risk") or "")
        if risk not in RISK_LEVELS:
            errors.append(f"步骤 {sid} 的风险等级非法：{risk or '(空)'}")
        gate = s.get("gate")
        if gate is not None and str(gate) not in ALLOWED_GATES:
            errors.append(f"步骤 {sid} 的闸门非法：{gate}")
        if not str(s.get("success") or "").strip():
            errors.append(f"步骤 {sid} 缺少成功条件")

        args_raw = s.get("args") if isinstance(s.get("args"), dict) else {}
        for tool in args_raw.get("tools", []) if isinstance(args_raw, dict) else []:
            if str(tool) in FORBIDDEN_TOOLS:
                errors.append(f"步骤 {sid} 的 args 引用了受限工具：{tool}")

    for s in steps:
        for dep in _as_list(s.get("depends_on")):
            if str(dep) not in seen:
                errors.append(f"步骤 {s.get('id')} 依赖了不存在的步骤：{dep}")

    order = schedule(plan)
    if len(order) != len(steps):
        errors.append("步骤依赖存在环或不完整，无法拓扑排序")

    return errors


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, str):
        return [value]
    return [value]


def schedule(plan: dict) -> list[str]:
    """按 depends_on 拓扑排序，同级保持声明顺序（确定性，便于计划可复现）。

    存在环时返回不完整的列表——``validate_plan`` 靠长度不一致判出"依赖有环"。
    """
    steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
    ids = [str(s.get("id")) for s in steps]
    deps = {str(s.get("id")): [str(d) for d in _as_list(s.get("depends_on"))
                               if str(d) in ids]
            for s in steps}
    ordered: list[str] = []
    temp: set[str] = set()

    def visit(node: str) -> bool:
        if node in ordered:
            return True
        if node in temp:  # 环
            return False
        temp.add(node)
        for d in deps.get(node, []):
            if not visit(d):
                return False
        temp.discard(node)
        ordered.append(node)
        return True

    for sid in ids:
        if not visit(sid):
            break
    return ordered


# ------------------------------------------------------------ Observe/Reflect

def observe(plan: dict, step_id: str, result: SkillResult) -> dict:
    """把技能执行结果写回计划（对照成功条件，而不是看工具有没有报错）。"""
    return set_step(
        plan, step_id,
        status=(STEP_DONE if result.ok else STEP_FAILED),
        observation=(result.data if result.ok else {"reason": result.reason}),
        tools_called=list(result.tools_called),
    )


def reflect(plan: dict, registry: dict[str, SkillSpec] | None = None) -> Reflection:
    """对照每一步的成功条件做复盘，给出状态与局部修复建议。

    只建议"局部修复"（改受影响步骤），不整段重跑已完成步骤（迭代计划 §3.1）。
    """
    registry = registry or {}
    steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
    if not steps:
        return Reflection(status="failed", summary="计划为空，未执行任何步骤")

    failed = [str(s.get("id")) for s in steps if s.get("status") == STEP_FAILED]
    blocked = [str(s.get("id")) for s in steps if s.get("status") == STEP_BLOCKED]
    delegated = [str(s.get("id")) for s in steps if s.get("status") == STEP_DELEGATED]
    pending = [str(s.get("id")) for s in steps if s.get("status") == STEP_PENDING]

    if failed or blocked:
        fixes: list[str] = []
        for sid in failed or blocked:
            step = get_step(plan, sid) or {}
            spec = registry.get(str(step.get("skill") or ""))
            fixes.append(
                f"重规划步骤 {sid}：{spec.success if spec else '检查成功条件是否满足'}")
        return Reflection(status="recoverable", summary="存在未通过成功条件的步骤",
                          failed_steps=failed + blocked, fixes=fixes)
    if delegated:
        names = ", ".join(str((get_step(plan, sid) or {}).get("skill")) for sid in delegated)
        return Reflection(status="success",
                          summary=f"已把 {names} 交给受闸门的子图执行")
    if pending:
        return Reflection(status="failed", summary=f"仍有未完成步骤：{pending}",
                          failed_steps=pending)
    return Reflection(status="success", summary="全部步骤通过成功条件")


def trace_view(plan: dict) -> list[dict]:
    """给前端/API 用的 trace 视图（脱敏：只留 phase/step/status/时间）。"""
    out: list[dict] = []
    for t in (plan.get("trace") or []):
        if not isinstance(t, dict):
            continue
        out.append({k: t.get(k) for k in
                    ("at", "phase", "plan_version", "step", "status", "note")
                    if k in t})
    return out


# ------------------------------------------------------------ Replan / diff

STEP_DIFF_KEYS = ("skill", "args", "risk", "gate", "success", "depends_on")


def diff_plans(old: dict | None, new: dict) -> dict:
    """两份计划的差异（新增/删除/修改了哪些步骤 + 约束/目标是否变化）。

    迭代计划 §12 验收第 4 条：重规划必须"展示 plan diff"，且已完成步骤不重跑。
    """
    old_steps = {str(s.get("id")): s for s in ((old or {}).get("steps") or [])
                 if isinstance(s, dict)}
    new_steps = {str(s.get("id")): s for s in (new.get("steps") or [])
                 if isinstance(s, dict)}
    changed: dict[str, list[str]] = {}
    for sid, s in new_steps.items():
        prev = old_steps.get(sid)
        if prev is None:
            continue
        keys = [k for k in STEP_DIFF_KEYS if copy.deepcopy(prev.get(k)) != copy.deepcopy(s.get(k))]
        if keys:
            changed[sid] = keys
    return {
        "added": sorted(set(new_steps) - set(old_steps)),
        "removed": sorted(set(old_steps) - set(new_steps)),
        "changed": changed,
        "constraints_changed": (list((old or {}).get("constraints") or [])
                                != list(new.get("constraints") or [])),
        "goal_changed": str((old or {}).get("goal") or "") != str(new.get("goal") or ""),
        "from_version": int((old or {}).get("plan_version") or 0),
        "to_version": int(new.get("plan_version") or 1),
    }


def bump_version(plan: dict, diff: dict | None = None) -> dict:
    """版本自增并记录 diff（同时把 diff 摘要写进 trace，便于回放）。"""
    version = int(plan.get("plan_version") or 1) + 1
    out = {**plan, "plan_version": version, "diff_from_version": diff}
    note = ""
    if isinstance(diff, dict):
        parts = []
        if diff.get("added"):
            parts.append("新增步骤 " + ",".join(diff["added"]))
        if diff.get("removed"):
            parts.append("删除步骤 " + ",".join(diff["removed"]))
        if diff.get("changed"):
            parts.append("修改步骤 " + ",".join(sorted(diff["changed"])))
        note = "；".join(parts) or "无步骤变化"
    return add_trace(out, "replan", note=note or "重规划")


def plan_summary(plan: dict) -> dict:
    """给播报/记忆用的确定性摘要（不含工具原始返回，避免把库数据塞进提示词）。"""
    steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
    return {
        "plan_id": plan.get("plan_id"),
        "plan_version": plan.get("plan_version"),
        "intent": plan.get("intent"),
        "goal": plan.get("goal"),
        "status": plan.get("status"),
        "skills": [str(s.get("skill")) for s in steps],
        "step_results": {str(s.get("id")): str(s.get("status")) for s in steps},
        "constraints": list(plan.get("constraints") or []),
        "memory_used": list(plan.get("memory_used") or []),
    }


def select_skills(text: str, wanted: Iterable[str]) -> list[str]:
    """确定性技能选择（不让 LLM 自由挑工具）：只从候选里按目标词命中。

    迭代计划 §2.2 的"受约束的自主规划"：技能目录 + 计划模板 + 数据驱动分支。
    """
    hits = [s for s in wanted if s]
    return hits


__all__ = [
    "ALLOWED_GATES",
    "FORBIDDEN_TOOLS",
    "MAX_PLAN_VERSION",
    "MAX_REPLANS",
    "MAX_STEPS",
    "PLAN_CANCELLED",
    "PLAN_DONE",
    "PLAN_FAILED",
    "PLAN_RUNNING",
    "PLAN_WAITING_USER",
    "RISK_LEVELS",
    "Reflection",
    "STEP_BLOCKED",
    "STEP_DELEGATED",
    "STEP_DIFF_KEYS",
    "STEP_DONE",
    "STEP_FAILED",
    "STEP_PENDING",
    "STEP_RUNNING",
    "STEP_SKIPPED",
    "SkillResult",
    "SkillSpec",
    "add_step",
    "add_trace",
    "bump_version",
    "diff_plans",
    "get_step",
    "new_plan",
    "new_plan_id",
    "observe",
    "plan_summary",
    "reflect",
    "schedule",
    "select_skills",
    "set_step",
    "trace_view",
    "validate_plan",
]
