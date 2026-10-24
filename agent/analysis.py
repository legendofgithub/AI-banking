"""只读分析视图层：把"技能执行结果"压成一张可验收的分析计划卡。

这一层全是**纯函数**：不调工具、不调 LLM、不读全局状态，同一份输入永远得到
同一张卡。三条铁律在这里的落点：

- **金额全部走 ``bank_core.money`` 的整数分运算**，本模块只负责格式化展示，
  绝不自己算钱、更不让 LLM 算；
- **零写操作**：分析技能只读，卡片里所有"可省订阅/候选产品"都是建议，
  取消订阅与申购都还要走各自的支付密码闸门；
- **空数据不装成功**：月报/持仓为空时事实行写明"无数据"，而不是编一个数。

对外两件事：
1. ``bill_facts`` / ``wealth_facts``：原始工具返回 → 事实行（label/value/hint）；
2. ``build_analysis_card``：计划 + 观察结果 → 前端 ``data-analysis-plan`` 载荷。
"""

from __future__ import annotations

from typing import Any

from agent.core import Reflection, STEP_FAILED, plan_summary, trace_view
from bank_core.money import cents_to_yuan, yuan_to_cents

# 只读分析卡片的固定提示（对外承诺"不动钱"，与 Claw评议口径一致）
ANALYSIS_HINT = (
    "本次分析为只读：不扣款、不取消订阅、不改风险等级；"
    "要办理请另行发起，动钱步骤仍需支付密码。"
)

# 技能名 → 卡片上的中文短句（与前端 AnalysisStep.title 冻结）
SKILL_TITLES: dict[str, str] = {
    "analyze_bill": "读取账单/分类/商户/异常/订阅",
    "analyze_wealth": "读取风险等级/持仓/候选产品",
    "plan_birthday": "生成生日预算计划（需确认 + 支付密码）",
    "execute_transfer": "转账（既受闸门子图执行）",
    "execute_card": "卡片操作（既受闸门子图执行）",
    "execute_subscription": "订阅操作（既受闸门子图执行）",
}

# router 的 LLM 意图分类 → 思维链里给用户的口语化标签
INTENT_LABELS: dict[str, str] = {
    "bill_analysis": "分析账单收支",
    "wealth": "查看理财持仓与配置建议",
    "linkage": "做一笔生日 / 事件预算规划",
    "transfer": "转账 / 调拨资金",
    "card": "卡片相关操作",
    "subscription": "订阅管理",
}

_EMPTY_RESULT_KEYS = frozenset({"报告", "分类", "商户", "异常", "订阅"})


def _cents0(value: Any) -> int:
    """零容忍元→分：None/""/"0.00"/非法一律 0（与 agent/plan.py 同口径）。"""
    if value is None or isinstance(value, bool):
        return 0
    try:
        return yuan_to_cents(value)
    except (TypeError, ValueError, ArithmeticError):
        return 0


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _max_item(items: list[dict], key: str) -> dict | None:
    """按金额列取最大项（同额时按名称升序，保证确定性）。"""
    rows = [r for r in items if isinstance(r, dict)]
    if not rows:
        return None
    return max(rows, key=lambda r: (_cents0(r.get(key)),
                                    str(r.get("category") or r.get("merchant") or "")))


def _pct_text(value: Any) -> str:
    """环比/占比文案：None 表示上月为 0，不能编成 0%。"""
    if value is None:
        return "—"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{v:+.1f}%"


# ------------------------------------------------------------- 账单事实行

def bill_facts(*, report: dict | None = None, categories: dict | None = None,
               merchants: list[dict] | None = None,
               anomalies: list[dict] | None = None,
               subscriptions: list[dict] | None = None) -> list[dict]:
    """账单分析的确定性摘要行（只描述工具真实返回值）。"""
    report = report if isinstance(report, dict) else {}
    categories = categories if isinstance(categories, dict) else {}
    items = _as_list(categories.get("items"))
    top_cat = _max_item(items, "amount_yuan")
    top_mer = _max_item(_as_list(merchants), "amount_yuan")
    anomaly_list = _as_list(anomalies)
    sub_list = _as_list(subscriptions)

    month = str(report.get("month") or "")
    income = report.get("income_yuan")
    expense = report.get("expense_yuan")
    net = report.get("net_yuan")

    rows: list[dict] = [
        {"label": "统计期间", "value": month or "—",
         "hint": "月报口径，内部划转不计入收支"},
        {"label": "收入", "value": f"¥{income}" if income else "¥0.00", "hint": None},
        {"label": "支出", "value": f"¥{expense}" if expense else "¥0.00", "hint": None},
        {"label": "结余", "value": f"¥{net}" if net else "¥0.00", "hint": None},
        {"label": "支出环比", "value": _pct_text(report.get("mom_expense_pct")),
         "hint": "上月为 0 时不计算环比"},
        {"label": "最大支出分类",
         "value": (f"{top_cat['category']} ¥{top_cat['amount_yuan']}"
                   f"（{top_cat.get('pct', 0)}%）" if top_cat else "无数据"),
         "hint": None},
        {"label": "Top 商户",
         "value": (f"{top_mer['merchant']} ¥{top_mer['amount_yuan']}"
                   if top_mer else "无数据"),
         "hint": None},
        {"label": "异常交易", "value": f"{len(anomaly_list)} 笔",
         "hint": "；".join(str(a.get("rule") or "") for a in anomaly_list[:2]) or None},
        {"label": "订阅支出", "value": f"{len(sub_list)} 项 • 建议↓",
         "hint": "仅建议，取消需支付密码"},
    ]
    if not report:
        rows[0]["value"] = "月报不可用"
        rows[0]["hint"] = "该月无数据或查询失败"
    return rows


# ------------------------------------------------------------- 理财事实行

def wealth_facts(*, profile: dict | None = None, holdings: list[dict] | None = None,
                 products: list[dict] | None = None,
                 max_risk_level: int | None = None) -> list[dict]:
    """理财持仓分析的确定性摘要行。

    风险等级 sk_check：**候选产品必须全部 R ≤ 用户等级**（迭代计划 §7.2 复盘条件），
    这里只负责展示；真正的过滤在 skills.py 出参前完成。
    """
    profile = profile if isinstance(profile, dict) else {}
    holding_rows = _as_list(holdings)
    product_rows = _as_list(products)

    total_value = sum(_cents0(h.get("est_value_yuan")) for h in holding_rows)
    total_profit = sum(_cents0(h.get("profit_yuan")) for h in holding_rows)
    total_principal = sum(_cents0(h.get("principal_yuan")) for h in holding_rows)
    top_holding = _max_item(holding_rows, "est_value_yuan")
    concentration = 0.0
    if top_holding and total_value > 0:
        concentration = round(_cents0(top_holding.get("est_value_yuan"))
                              * 100 / total_value, 1)

    level = str(profile.get("level") or "")
    return [
        {"label": "风险等级",
         "value": level or "未测评",
         "hint": (f"最高可购 R{max_risk_level}" if max_risk_level else "未测评限购 R1")},
        {"label": "持仓市值", "value": f"¥{cents_to_yuan(total_value)}",
         "hint": f"{len(holding_rows)} 只在持"},
        {"label": "累计盈亏", "value": f"¥{cents_to_yuan(total_profit)}",
         "hint": (f"本金 ¥{cents_to_yuan(total_principal)}" if holding_rows else None)},
        {"label": "最大持仓占比",
         "value": (f"{concentration}%（{top_holding['product']}）"
                   if top_holding else "无数据"),
         "hint": "占比过高提示集中度风险"},
        {"label": "候选产品", "value": f"{len(product_rows)} 只",
         "hint": "全部 R ≤ 你的风险等级，仅供参考"},
    ]


# ------------------------------------------------------------- 卡片视图

def _step_view(step: dict) -> dict:
    return {
        "id": str(step.get("id") or ""),
        "skill": str(step.get("skill") or ""),
        "title": SKILL_TITLES.get(str(step.get("skill") or ""), "执行技能"),
        "risk": str(step.get("risk") or "READ"),
        "gate": step.get("gate"),
        "status": str(step.get("status") or "pending"),
    }


def build_analysis_card(plan: dict, *, intent: str = "",
                        bill: dict | None = None,
                        wealth: dict | None = None,
                        reflection: Reflection | None = None,
                        persona: str = "",
                        hint: str = ANALYSIS_HINT) -> dict:
    """计划 + 观察结果 → ``data-analysis-plan`` 卡片载荷（纯函数）。

    Args:
        plan:       Core 的 Plan（含 steps/status/trace/memory_used）。
        intent:     ``bill`` | ``wealth``，决定事实行走哪套。
        bill:       bill 观察结果：report/categories/merchants/anomalies/subscriptions。
        wealth:     wealth 观察结果：profile/holdings/products/max_risk_level。
        reflection: Reflect 阶段结论（复盘摘要）。
        persona:    当前用户人设（摘录，卡片头部🎭行展示；空=不展示）。
    """
    plan = plan if isinstance(plan, dict) else {}
    if intent == "wealth":
        payload = dict(wealth or {})
        facts = wealth_facts(
            profile=payload.get("profile"), holdings=payload.get("holdings"),
            products=payload.get("products"),
            max_risk_level=payload.get("max_risk_level"))
    else:
        payload = dict(bill or {})
        facts = bill_facts(
            report=payload.get("report"), categories=payload.get("categories"),
            merchants=payload.get("merchants"), anomalies=payload.get("anomalies"),
            subscriptions=payload.get("subscriptions"))

    summary = plan_summary(plan)
    refl = reflection if isinstance(reflection, Reflection) else None
    return {
        "plan_id": plan.get("plan_id"),
        "plan_version": int(plan.get("plan_version") or 1),
        "goal": plan.get("goal") or "",
        "intent": str(intent or ""),
        "hint": hint,
        "facts": facts,
        "steps": [_step_view(s) for s in (plan.get("steps") or [])
                  if isinstance(s, dict)],
        "reflection": {"status": refl.status if refl else summary.get("status"),
                       "summary": (refl.summary if refl
                                   else "尚未复盘")},
        "memory_used": list(plan.get("memory_used") or []),
        # 当前人设摘录(前端卡片头部🎭行;没有人设时为空串不展示)
        "persona": str(persona or "").strip()[:60],
        "trace": trace_view(plan),
        # 重规划 diff(新增/删除/修改了哪些步骤):验收要求"改了什么必须可见"
        "diff": plan.get("diff_from_version"),
    }


def build_thinking(plan: dict, *, memory_used: list | None = None,
                   persona: str = "") -> list[dict]:
    """把 Core 的 trace + 意图 + 记忆 + 人设拼成"人话思维链"（纯函数，确定性）。

    前端 ``data-thinking`` 据此渲染可折叠的 🧠 思维链面板，让用户看到
    Agent 是怎么想的：人设 → 识别意图 → 调取记忆 → 制定计划 → 安全守卫
    → 执行 → 复盘。不引入任何新的 LLM 调用，全部来自既有的确定性 trace。
    每个图节点重建思维链都会带上 persona(有人设时置顶),不会被后续节点覆盖掉。
    """
    plan = plan if isinstance(plan, dict) else {}
    mem = list(memory_used if memory_used is not None
               else (plan.get("memory_used") or []))
    steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
    title_of = lambda sk: SKILL_TITLES.get(str(sk or ""), str(sk or "技能"))

    thoughts: list[dict] = []

    # 0) 人设(用户设定,置顶:用户能看到"我按哪套人设办事";空=不出现)
    persona_text = str(persona or "").strip()
    if persona_text:
        thoughts.append({
            "phase": "人设",
            "text": persona_text,
            "status": "info",
        })

    # 1) 识别意图（a_perceive 写入的 perceive trace；没有则跳过）
    perceive = next((t for t in (plan.get("trace") or [])
                     if str(t.get("phase") or "") == "perceive"), None)
    raw = str((perceive or {}).get("note") or "")
    if raw:
        thoughts.append({
            "phase": "识别意图",
            "text": f"你这句话，我理解为：{INTENT_LABELS.get(raw, raw)}",
            "status": "info",
        })

    # 2) 长期记忆（给本次的建议）
    if mem:
        thoughts.append({
            "phase": "调取记忆",
            "text": "翻了下你的长期记忆，本次参考：" +
                    "；".join(str(m) for m in mem),
            "status": "info",
        })

    # 3) 制定计划（从 steps 拼，比 trace 的"技能: …"更口语）
    if steps:
        chain = " → ".join(title_of(s.get("skill")) for s in steps)
        thoughts.append({
            "phase": "制定计划",
            "text": f"我打算这样办：{chain}。这些技能都是只读的，不会动你的钱。",
            "status": "info",
        })

    # 4) 其余 trace 阶段逐条转人话
    for t in (plan.get("trace") or []):
        phase = str(t.get("phase") or "")
        if phase == "perceive":
            continue
        note = str(t.get("note") or "")
        st = str(t.get("status") or "")
        if phase == "guard":
            if st == "blocked":
                thoughts.append({
                    "phase": "安全守卫",
                    "text": f"静态守卫拦下了这一步：{note}",
                    "status": "blocked",
                })
            else:
                thoughts.append({
                    "phase": "安全守卫",
                    "text": "静态守卫检查通过：没有越权工具，也没有需要你输支付密码的"
                            "高风险步骤。",
                    "status": "passed",
                })
        elif phase == "act":
            sid = str(t.get("step") or "")
            step = next((s for s in steps if str(s.get("id")) == sid), None)
            head = title_of(step.get("skill") if step else sid)
            if st == "delegated":
                thoughts.append({
                    "phase": head,
                    "text": f"{note}（资金操作交给既有的、带支付密码闸门的链路，"
                            f"我不会替你确认）",
                    "status": "delegated",
                })
            elif st == "done":
                thoughts.append({
                    "phase": head,
                    "text": note or "成功条件满足，结果已取到。",
                    "status": "done",
                })
            elif st == "failed":
                thoughts.append({
                    "phase": head,
                    "text": f"这一步没满足成功条件：{note}",
                    "status": "failed",
                })
            else:
                thoughts.append({
                    "phase": head,
                    "text": note or "已处理。",
                    "status": "info",
                })
        elif phase == "replan":
            thoughts.append({
                "phase": "重新规划",
                "text": note or "局部重规划：只改受影响的步骤，已完成的不再重跑。",
                "status": "info",
            })
        elif phase == "reflect":
            thoughts.append({
                "phase": "复盘",
                "text": note or "对照成功条件复盘完毕。",
                "status": st or "info",
            })
    return {"thoughts": thoughts}


def diff_text(diff: dict | None) -> str:
    """plan diff → 一句人话(确定性拼接,不给 LLM 转述的机会)。"""
    if not isinstance(diff, dict):
        return ""
    parts: list[str] = []
    if diff.get("added"):
        parts.append("新增步骤 " + "、".join(diff["added"]))
    if diff.get("removed"):
        parts.append("删除步骤 " + "、".join(diff["removed"]))
    if diff.get("changed"):
        parts.append("修改步骤 " + "、".join(sorted(diff["changed"])))
    return "；".join(parts)


def failed_note(plan: dict) -> str:
    """计划里有步骤失败时的确定性说明（给播报事实用，不编数字）。"""
    steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
    bad = [str(s.get("id")) for s in steps if s.get("status") == STEP_FAILED]
    return f"步骤 {', '.join(bad)} 未通过成功条件" if bad else ""


__all__ = [
    "ANALYSIS_HINT",
    "INTENT_LABELS",
    "SKILL_TITLES",
    "bill_facts",
    "build_analysis_card",
    "build_thinking",
    "diff_text",
    "failed_note",
    "wealth_facts",
]
