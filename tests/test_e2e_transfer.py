"""M1 验收标准 E2E 测试:五条转账剧本逐条断言(含审计链完整性)。

对应研发计划 §6 M1 验收:转账场景 E2E 跑通(含同名消歧、定时、AA)。
与 tests/test_agent_graph.py 的分工:那套验证图机制,本套钉死验收标准——
每条剧本都断言:
  1) 资金安全不变量(整数分):中断时不动钱、确认后恰好扣该金额;
  2) 订单/账单状态机(pending_confirm/scheduled → executed/cancelled/collecting);
  3) 审计链完整:银行侧每个写操作在 audit_log 里有痕,且写操作序列与剧本预期一致。

不联网:注入 FakeMessagesListChatModel;库/检查点都在 tmp_path。
助手复用 tests/test_agent_graph.py(同目录,pytest prepend 导入模式可直接 import)。

bank_core P1 修复后:settle_split_bill_item 已补 audit 留痕(risk=LOW)——
AA 剧本的结算环节现在既看 split_bill_items.paid 位,也断言审计链里有
settle_split_bill_item 写痕(此前缺痕问题曾在交付 notes 中上报,本轮闭环)。
"""

from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from test_agent_graph import (DEFAULT_BALANCE, _AgentSession, _balance, _count,
                              _mk_db, _order, _ro, _slots_json, _split_bill)

# bank_core 会写库/动钱的工具(用于审计链断言;READ 类不算)
WRITE_TOOLS = frozenset({
    "create_transfer_order", "confirm_transfer_order", "cancel_transfer_order",
    "create_split_bill", "settle_split_bill_item", "cancel_subscription",
    "subscribe_product", "redeem_product", "apply_card", "set_card_limits",
    "set_card_status", "set_risk_profile", "add_event", "schedule_reminder",
    "run_due_tasks",
})


def _audit_rows(db: Path) -> list[dict]:
    """audit_log 全量,按 id 升序;args/result 解析为 dict。"""
    conn = _ro(db)
    try:
        rows = conn.execute(
            "SELECT id, tool, risk, args_json, result_json FROM audit_log ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({"id": r["id"], "tool": r["tool"], "risk": r["risk"],
                    "args": json.loads(r["args_json"]),
                    "result": json.loads(r["result_json"])})
    return out


def _write_sequence(db: Path) -> list[str]:
    """审计链里的写操作序列(按发生顺序)——审计完整性的核心断言面。"""
    return [r["tool"] for r in _audit_rows(db) if r["tool"] in WRITE_TOOLS]


def _tool_rows(db: Path, tool: str) -> list[dict]:
    return [r for r in _audit_rows(db) if r["tool"] == tool]


# ----------------------------------------------------------------- 剧本 a:立即转账

def test_e2e_a_immediate_transfer_full_gate(tmp_path):
    """a. 立即转账:interrupt 时订单 pending_confirm、余额未动、审计已有痕;
    恢复确认后余额恰好减少、订单 executed、审计补痕(confirm 留 HIGH 风险痕)。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(payee="张三", amount_yuan="500", when="now"),
            "已向张三转账 500.00 元。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "e2e-a"}}
            # 第一段:走到人工闸门
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("给张三转 500 元")]}, cfg)
            ints = r1["__interrupt__"]
            assert ints[0].value["type"] == "confirm_transfer"
            order_id = ints[0].value["order"]["id"]

            # 中断现场:订单已建未动钱
            assert _order(db, order_id)["status"] == "pending_confirm"
            assert _balance(db) == DEFAULT_BALANCE
            # 审计已有痕:查询/预检/建单全留痕;建单是 HIGH 风险、金额以整数分入审计
            tools_at_gate = [r["tool"] for r in _audit_rows(db)]
            assert "resolve_contact" in tools_at_gate
            assert "policy_check" in tools_at_gate
            create_rows = _tool_rows(db, "create_transfer_order")
            assert len(create_rows) == 1
            assert create_rows[0]["risk"] == "HIGH"
            assert create_rows[0]["args"]["amount_cents"] == 50_000  # 500 元 = 50000 分
            assert _tool_rows(db, "confirm_transfer_order") == []    # 尚未确认
            assert _write_sequence(db) == ["create_transfer_order"]

            # 第二段:恢复确认 → 真正扣款
            await graph.ainvoke(Command(resume="888888"), cfg)
            assert _balance(db) == DEFAULT_BALANCE - 50_000          # 恰好该金额
            assert _order(db, order_id)["status"] == "executed"
            confirm_rows = _tool_rows(db, "confirm_transfer_order")
            assert len(confirm_rows) == 1
            assert confirm_rows[0]["risk"] == "HIGH"
            assert confirm_rows[0]["result"]["status"] == "executed"
            # 写操作序列与剧本一致:建单 → 确认,无其他写操作
            assert _write_sequence(db) == ["create_transfer_order",
                                           "confirm_transfer_order"]

    import asyncio
    asyncio.run(scenario())


# ----------------------------------------------------------------- 剧本 b:同名消歧

def test_e2e_b_ambiguous_contact_full_flow(tmp_path):
    """b. 同名消歧:两个林悦触发反问(审计 matches=2),答「尾号2222」后走完全程。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(payee="林悦", amount_yuan="66.6", when="now"),
            "已向林悦(尾号2222)转账 66.60 元。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "e2e-b"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("给林悦转66.6")]}, cfg)
            pick = r1["__interrupt__"][0].value
            assert pick["type"] == "pick_contact"
            assert len(pick["candidates"]) == 2
            # 审计佐证反问依据:同名命中 2 人
            resolve_rows = _tool_rows(db, "resolve_contact")
            assert resolve_rows[-1]["result"]["matches"] == 2
            assert _write_sequence(db) == []  # 反问阶段零写操作

            r2 = await graph.ainvoke(Command(resume="尾号2222那个"), cfg)
            gate = r2["__interrupt__"][0].value
            assert gate["type"] == "confirm_transfer"
            r3 = await graph.ainvoke(Command(resume="888888"), cfg)
            order = _order(db, gate["order"]["id"])
            assert order["status"] == "executed"
            assert order["to_contact_id"] == 2  # 选中的是建行尾号2222 的林悦
            assert _balance(db) == DEFAULT_BALANCE - 6_660
            assert _write_sequence(db) == ["create_transfer_order",
                                           "confirm_transfer_order"]

    import asyncio
    asyncio.run(scenario())


# ----------------------------------------------------------------- 剧本 c:超限拦截

def test_e2e_c_over_limit_blocked(tmp_path):
    """c. 超限拦截:policy_check 拒绝(审计 approved=false),不建单、余额不动、有风险提示。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(payee="张三", amount_yuan="60000", when="now"),  # 6 万 > 单笔 5 万
            "这笔转账超过单笔限额,已被风控拦截。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "e2e-c"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("给张三转 60000")]}, cfg)
            assert not r1.get("__interrupt__")            # 根本不该到闸门
            text = r1["messages"][-1].content
            assert "拦截" in text or "限额" in text        # 风险提示话术

            assert _balance(db) == DEFAULT_BALANCE
            assert _count(db, "transfer_orders") == 0
            # 审计:预检留痕且结论是拒绝;零写操作
            pc = _tool_rows(db, "policy_check")
            assert len(pc) == 1
            assert pc[0]["result"]["approved"] is False
            assert any("单笔限额" in reason for reason in pc[0]["result"]["reasons"])
            assert _write_sequence(db) == []

    import asyncio
    asyncio.run(scenario())


# ----------------------------------------------------------------- 剧本 d:定时转账

def test_e2e_d_scheduled_transfer_no_debit(tmp_path):
    """d. 定时转账:未来时间建单成功(status=scheduled、审计含 scheduled_at),不立即扣款。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(payee="张三", amount_yuan="300", when="scheduled",
                        scheduled_at="2026-10-01T09:00:00"),
            "已建立定时转账单,到期会提醒你确认后才扣款。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "e2e-d"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("10月1号早上9点给张三转300")]}, cfg)
            gate = r1["__interrupt__"][0].value
            assert gate["type"] == "confirm_transfer"
            assert gate["order"]["scheduled_at"] == "2026-10-01T09:00:00"
            order_id = gate["order"]["id"]
            assert _order(db, order_id)["status"] == "scheduled"

            # 确认(=保留定时单)后仍不扣款
            await graph.ainvoke(Command(resume="888888"), cfg)
            order = _order(db, order_id)
            assert order["status"] == "scheduled"
            assert order["scheduled_at"] == "2026-10-01T09:00:00"
            assert _balance(db) == DEFAULT_BALANCE
            # 审计:建单留痕(含定时时刻),全程无 confirm
            create_rows = _tool_rows(db, "create_transfer_order")
            assert create_rows[0]["args"]["scheduled_at"] == "2026-10-01T09:00:00"
            assert _tool_rows(db, "confirm_transfer_order") == []
            assert _write_sequence(db) == ["create_transfer_order"]

    import asyncio
    asyncio.run(scenario())


# ----------------------------------------------------------------- 剧本 e:AA 收款

def test_e2e_e_split_bill_create_and_settle(tmp_path):
    """e. AA 收款:发起(均摊 100/100/100、审计 MED 痕)→ 结算两名成员 → 汇总状态正确。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            # 第 1 轮:发起
            '{"intent": "split_bill"}',
            json.dumps({"title": "火锅", "total_yuan": "300", "even": True,
                        "participants": [{"name": "我"}, {"name": "老王"},
                                         {"name": "小刘"}]}, ensure_ascii=False),
            "AA 已发起,三人各 100 元。",
            # 第 2 轮:老王付款
            '{"intent": "split_settle"}',
            json.dumps({"contact_name": "老王", "bill_id": None}, ensure_ascii=False),
            "老王已付。",
            # 第 3 轮:小刘付款
            '{"intent": "split_settle"}',
            json.dumps({"contact_name": "小刘", "bill_id": None}, ensure_ascii=False),
            "小刘也付了,还差我这一份。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "e2e-e"}}
            # 发起 → 闸门 → 确认
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("发起AA,火锅一共300,我、老王、小刘均摊")]}, cfg)
            gate = r1["__interrupt__"][0].value
            assert gate["type"] == "confirm_split"
            await graph.ainvoke(Command(resume="确认"), cfg)

            bill = _split_bill(db, 1)
            assert bill["total_cents"] == 30_000
            assert [i["share_cents"] for i in bill["items"]] == [10_000, 10_000, 10_000]
            assert bill["status"] == "collecting"
            assert _balance(db) == DEFAULT_BALANCE          # AA 不动自己余额
            sb_rows = _tool_rows(db, "create_split_bill")
            assert len(sb_rows) == 1 and sb_rows[0]["risk"] == "MED"
            assert sb_rows[0]["result"]["total_yuan"] == "300.00"

            # 结算老王(第 2 轮;评审修复:settle 也要过 confirm_settle 闸门)
            r2 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("老王把钱转给我了")]}, cfg)
            settle_gate = r2["__interrupt__"][0].value
            assert settle_gate["type"] == "confirm_settle"
            assert settle_gate["settle"]["contact_name"] == "老王"
            await graph.ainvoke(Command(resume="确认"), cfg)
            bill = _split_bill(db, 1)
            assert [i["paid"] for i in bill["items"]] == [0, 1, 0]
            assert bill["status"] == "collecting"
            # 结算小刘(第 3 轮)→ 汇总:2/3 已付,仍未结清(我自己那份)
            r3 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("小刘也付了")]}, cfg)
            assert r3["__interrupt__"][0].value["type"] == "confirm_settle"
            r4 = await graph.ainvoke(Command(resume="确认"), cfg)
            assert "还差" in r4["messages"][-1].content
            bill = _split_bill(db, 1)
            assert [i["paid"] for i in bill["items"]] == [0, 1, 1]
            assert bill["status"] == "collecting"           # 还差"我"自己
            assert _balance(db) == DEFAULT_BALANCE

            # 审计链:AA 写操作 = 发起一笔 + 两次结算(老王、小刘)各留一笔;
            # settle_split_bill_item 的 audit 痕(P1 修复补上,risk=LOW)
            # 与上面的 paid 位互相印证。
            assert _write_sequence(db) == ["create_split_bill",
                                           "settle_split_bill_item",
                                           "settle_split_bill_item"]
            assert len(_tool_rows(db, "get_split_bill")) >= 2  # 每次结算后都查了进度

    import asyncio
    asyncio.run(scenario())
