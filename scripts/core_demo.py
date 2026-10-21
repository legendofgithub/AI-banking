"""Agent Core 无界面演示脚本(迭代计划 §11 四幕 / 验收第 8 条)。

目的:证明 **Agent 在项目里,不是 UI 里嵌了一个 agent**——去掉浏览器,
Core 依然能完成"目标 → 计划 → 执行 → 观察 → 复盘 → 记忆 → 下一次改进"的闭环。

用法:
    # 离线跑(不联网,LLM 用脚本化假模型,计划/复盘/记忆仍是真实代码)
    .venv/Scripts/python.exe scripts/core_demo.py

    # 用真实模型跑意图识别与播报(需配 ZAI_* 环境变量)
    .venv/Scripts/python.exe scripts/core_demo.py --real

    # 演示交互:遇到闸门自动"确认",支付密码用 --password
    .venv/Scripts/python.exe scripts/core_demo.py --auto-confirm --password 888888

输出三样东西:**PlanCard(分析卡)、Plan Trace、Memory Used**。

注:输出里会混进几段 FastMCP 子进程的启动横幅(每次银行工具调用新起一个
stdio 子进程),那是 MCP 侧的噪声,不影响结论;只看带「幕/────」标记的行即可。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langchain_core.language_models.fake_chat_models import (  # noqa: E402
    FakeMessagesListChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agent import memory as memory_mod  # noqa: E402
from agent.bank import load_bank_tools  # noqa: E402
from agent.graph import build_agent_graph, make_sqlite_checkpointer  # noqa: E402

def _scenes() -> list[tuple[str, list[str]]]:
    """四幕脚本(迭代计划 §11)。

    每幕的响应顺序:router → (各支线抽取) → 播报。假模型只负责这两处"语言",
    计划/技能选择/复盘/记忆全部是确定性代码——所以离线跑出的结论与联网一致。
    """
    day = (date.today() + timedelta(days=4)).isoformat()
    slots = json.dumps({"event_title": "林悦的生日", "event_date": day,
                        "budget_yuan": "3000", "actions": None},
                       ensure_ascii=False)
    return [
        # 第一幕:一个目标,Core 自己选技能(只读账单分析)
        ("帮我分析上个月账单,看看哪里花多了",
         ['{"intent": "bill_analysis"}', "上个月转账支出最多,可省空间在订阅里。"]),
        # 第二幕:一个计划组合两个技能(账单 + 理财)
        ("帮我分析一下账单和理财持仓,给个稳健建议",
         ['{"intent": "bill_analysis"}',
          "账单与持仓都看了,候选产品不超过你的风险等级。"]),
        # 第三幕:分析 + 行动——Core 把生日技能交给既有受闸门链路(要密码)
        (f"帮我分析上个月账单,看看能不能省出1000给老婆过生日,日期{day}",
         ['{"intent": "bill_analysis"}', slots,
          "账单看完了,生日预算已按可支配额度排好,确认后仍要支付密码。"]),
        # 第四幕:新会话,记忆让默认值变化(第二次规划不再默认砍订阅)
        (f"再帮我规划一次生日,预算3000,日期{day}",
         ['{"intent": "linkage"}', slots,
          "按你上次的偏好给你排了生日预算,确认后才会锁定资金。"]),
    ]


def _print_card(title: str, view: dict | None) -> None:
    print(f"\n──── {title} ────")
    if not view:
        print("(本幕没有下发分析卡)")
        return
    print(f"目标      : {view.get('goal')}")
    print(f"计划版本  : v{view.get('plan_version')}")
    for row in view.get("facts") or []:
        hint = f" ({row['hint']})" if row.get("hint") else ""
        print(f"  · {row['label']:<12}{row['value']}{hint}")
    for step in view.get("steps") or []:
        gate = step.get("gate") or "无闸门"
        print(f"  ▸ [{step['risk']}] {step['title']} —— {step['status']} / {gate}")
    diff = view.get("diff")
    if diff:
        print(f"计划调整  : 修改步骤 {'、'.join((diff.get('changed') or {}).keys())}"
              f"(已完成步骤不重跑)")
    if view.get("memory_used"):
        print(f"上次记忆  : {'；'.join(view['memory_used'])}")
    refl = view.get("reflection") or {}
    if refl:
        print(f"复盘      : {refl.get('status')} —— {refl.get('summary')}")


def _print_trace(plan: dict | None) -> None:
    print("\n──── Plan Trace ────")
    if not plan:
        print("(无计划)")
        return
    for t in plan.get("trace") or []:
        extra = f" step={t.get('step')}" if t.get("step") else ""
        extra += f" status={t.get('status')}" if t.get("status") else ""
        print(f"  v{t.get('plan_version')} {t.get('phase'):<8}{extra}"
              f"  {t.get('note') or ''}")
    print(f"  计划状态: {plan.get('status')}  步骤: "
          f"{[(s['skill'], s['status']) for s in plan.get('steps') or []]}")


def _print_memory(user_id: int) -> None:
    print("\n──── Memory Used / 已写入 ────")
    mem = memory_mod.recall_all(user_id)
    if not mem:
        print("(无记忆)")
        return
    for module in sorted(mem):
        print(f"  {module}: {mem[module]}")


async def _run_scene(graph, text: str, thread: str, user_id: int,
                     auto_confirm: bool, password: str) -> dict:
    cfg = {"configurable": {"thread_id": thread}}
    result = await graph.ainvoke(
        {"auth_user_id": user_id, "messages": [HumanMessage(text)]}, cfg)
    auto_confirm = bool(auto_confirm)
    guard = 0
    while result.get("__interrupt__") and auto_confirm and guard < 6:
        guard += 1
        payload = result["__interrupt__"][0].value
        kind = str(payload.get("type") or "")
        resume = password if payload.get("pay_required") else "确认"
        print(f"  ⏸ 闸门 {kind} → 自动答复「{resume}」")
        result = await graph.ainvoke(Command(resume=resume), cfg)
    return result


async def main() -> None:
    ap = argparse.ArgumentParser(description="Agent Core 无界面演示")
    ap.add_argument("--db", default="", help="银行库路径(默认 data/bank.db)")
    ap.add_argument("--user", type=int, default=1,
                    help="演示用户 id(默认 1=陈明,演示主用户;faker 账户=11)")
    ap.add_argument("--memory-db", default="", help="记忆库路径(默认 data/agent_memory.sqlite)")
    ap.add_argument("--real", action="store_true", help="用真实 LLM(需 ZAI_* 环境变量)")
    ap.add_argument("--auto-confirm", action="store_true", help="遇到闸门自动确认")
    ap.add_argument("--password", default="888888", help="自动确认时的支付密码")
    args = ap.parse_args()

    db = Path(args.db) if args.db else ROOT / "data" / "bank.db"
    if args.memory_db:
        os.environ["AGENT_MEMORY_DB"] = args.memory_db
    if not db.is_file():
        print(f"找不到银行库 {db};先跑 .venv/Scripts/python.exe -m bank_core.seed")
        return

    tools = await load_bank_tools(db, user_id=args.user)
    # 检查点库放临时目录:演示脚本不往 data/ 里落任何会话产物
    saver, conn = await make_sqlite_checkpointer(
        Path(tempfile.mkdtemp(prefix="core-demo-")) / "ckpt.sqlite")

    print(f"银行库: {db}   演示用户: {args.user}")
    print(f"记忆库: {memory_mod.db_path()}")

    for i, (text, responses) in enumerate(_scenes(), start=1):
        if args.real:
            from agent.llm import get_llm
            llm = get_llm()
        else:
            llm = FakeMessagesListChatModel(
                responses=[AIMessage(content=r) for r in responses])
        graph = build_agent_graph(llm, tools, saver)
        print(f"\n=========== 第 {i} 幕:{text} ===========")
        result = await _run_scene(graph, text, f"demo-{i}", args.user,
                                  args.auto_confirm, args.password)
        _print_card(f"第 {i} 幕分析卡", result.get("analysis_view"))
        _print_trace(result.get("agent_plan"))
        if result.get("__interrupt__"):
            payload = result["__interrupt__"][0].value
            print(f"\n停在闸门:{payload.get('type')}(加 --auto-confirm 可自动通过)")

    _print_memory(args.user)
    await conn.close()

    print("\n=========== 说明 ===========")
    print("· 资金技能(转账/卡片/订阅)不在这条链上:它们仍走既有子图 + 支付密码闸门;")
    print("· Core 只是把它们登记为技能,并拒绝自行执行(见 agent/skills.py run_skill);")
    print("· 本脚本不依赖浏览器:API/评估脚本能跑同样的闭环。")


if __name__ == "__main__":
    asyncio.run(main())
