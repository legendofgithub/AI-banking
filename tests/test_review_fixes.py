"""评审修复回归测试:资金安全与代码质量评审确认的 9 条问题逐条钉死。

对应关系(编号即评审报告序号):
#1  test_replay_same_instruction_idempotent / test_replay_orphan_recovers_once
#2+#6  test_parse_confirmation_strict / test_gate_hedged_answer_never_executes
#3  test_settle_gate_confirm_and_reject(+ test_agent_graph 的 split 测试)
#4  test_clarify_rounds_capped
#9  test_agent_bank_calls_full_trace
(#5/#7 api 超时熔断在 agent/api.py,由 test_api_stream.py 守护回归;
 #8 依赖上下界见 requirements.txt + requirements.lock.txt)

套路同 test_agent_graph:FakeMessagesListChatModel 脚本化、真实 MCP、临时库、
金额断言一律整数分。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from agent.graph import parse_confirmation
from test_agent_graph import (DEFAULT_BALANCE, _AgentSession, _balance, _count,
                              _interrupt_payload, _mk_db, _order, _split_bill)


# ----------------------------------------------------------------- #2/#6 确认语义解析

def test_parse_confirmation_strict():
    """评审判例:带保留/疑问/无关回复一律 None,绝不判 yes;明确肯定才 yes。"""
    # 评审报告里的误判样例,修复后必须全部不可识别
    assert parse_confirmation("好的,但稍等,我要改金额") is None   # 曾被误判 yes
    assert parse_confirmation("怎么确认?") is None                # 曾被误判 yes
    assert parse_confirmation("是500吗") is None                  # 曾被误判 yes
    assert parse_confirmation("May I see the details") is None    # 曾因 'y' 子串误判
    assert parse_confirmation("先别转,我看看") == "no"            # 否定词命中
    assert parse_confirmation("我想改成一千") is None             # hedge「改」
    assert parse_confirmation("") is None
    # 明确肯定
    for yes_text in ("确认", "确认执行", "确认转账", "好的", "好", "嗯", "行",
                     "转吧", "确认一下", "yes", "Y", "OK", "true"):
        assert parse_confirmation(yes_text) == "yes", yes_text
    # 明确否定
    for no_text in ("取消", "算了", "不转了", "别转", "no", "false"):
        assert parse_confirmation(no_text) == "no", no_text
    # 布尔直传(结构化前端)
    assert parse_confirmation(True) == "yes"
    assert parse_confirmation(False) == "no"


def test_gate_hedged_answer_never_executes(tmp_path):
    """闸门处带犹豫的回答绝不扣款:先触发再问,两次不可识别自动按取消。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            '{"payee": "张三", "amount_yuan": "500", "when": "now"}',
            "已向张三转账 500.00 元。",
        ]) as graph:
            cfg_a = {"configurable": {"thread_id": "fix-hedge-a"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("给张三转 500 元")]}, cfg_a)
            order_id = _interrupt_payload(r1)["order"]["id"]
            # 带保留意见的"好的,但…" → 不可识别 → 触发再问,而不是执行
            r2 = await graph.ainvoke(Command(resume="好的,但稍等,我要改金额"), cfg_a)
            retry = _interrupt_payload(r2)
            assert retry["type"] == "confirm_transfer"
            assert _balance(db) == DEFAULT_BALANCE  # 全程未动钱
            # 想清楚了,明确确认 → 执行
            r3 = await graph.ainvoke(Command(resume="888888"), cfg_a)
            assert _order(db, order_id)["status"] == "executed"
            assert _balance(db) == DEFAULT_BALANCE - 50_000

            # 另一条 thread:两次都是疑问/含糊 → 自动按取消,绝不执行
            cfg_b = {"configurable": {"thread_id": "fix-hedge-b"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("再给张三转500")]}, cfg_b)
            order_b = _interrupt_payload(r1)["order"]["id"]
            r2 = await graph.ainvoke(Command(resume="怎么确认?"), cfg_b)
            assert _interrupt_payload(r2)["type"] == "confirm_transfer"
            r3 = await graph.ainvoke(Command(resume="是500吗"), cfg_b)
            assert not r3.get("__interrupt__")            # 以取消收尾
            assert _order(db, order_b)["status"] == "cancelled"
            assert _balance(db) == DEFAULT_BALANCE - 50_000  # 只有第一笔被扣

    asyncio.run(scenario())


# ----------------------------------------------------------------- #1 幂等防重放

def test_replay_same_instruction_idempotent(tmp_path):
    """同 thread 重发同一句转账指令:幂等命中已执行订单,不二次建单、不二次扣款。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            '{"payee": "张三", "amount_yuan": "500", "when": "now"}',
            "已向张三转账 500.00 元。",
            '{"intent": "transfer"}',                          # 重放轮
            '{"payee": "张三", "amount_yuan": "500", "when": "now"}',
            "这笔已经转过啦,没有重复扣款。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "fix-replay"}}
            msg = "给张三转 500 元"
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(msg)]}, cfg)
            await graph.ainvoke(Command(resume="888888"), cfg)
            assert _count(db, "transfer_orders") == 1
            assert _balance(db) == DEFAULT_BALANCE - 50_000

            # 重放同一条指令(如 HTTP 重试/用户重复发送)
            r2 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(msg)]}, cfg)
            assert not r2.get("__interrupt__")                 # 不再进闸门
            snap = await graph.aget_state(cfg)
            assert snap.values["notice"]["kind"] == "duplicate_order"
            assert _count(db, "transfer_orders") == 1          # 没有第二张单
            assert _balance(db) == DEFAULT_BALANCE - 50_000    # 没有第二次扣款

    asyncio.run(scenario())


def test_replay_orphan_recovers_once(tmp_path):
    """建单后未确认(模拟中断/崩溃留下的孤儿单):重放同指令幂等命中原单,
    重新走闸门,确认后恰好执行一次。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            '{"payee": "张三", "amount_yuan": "300", "when": "now"}',
            '{"intent": "transfer"}',                          # 重放轮
            '{"payee": "张三", "amount_yuan": "300", "when": "now"}',
            "已向张三转账 300.00 元。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "fix-orphan"}}
            msg = "给张三转 300 元"
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(msg)]}, cfg)
            order_id = _interrupt_payload(r1)["order"]["id"]   # 停在闸门,未答复

            # 不答复,直接重放同一条指令
            r2 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(msg)]}, cfg)
            gate = _interrupt_payload(r2)
            assert gate["type"] == "confirm_transfer"
            assert gate["order"]["id"] == order_id             # 命中同一张孤儿单
            assert _count(db, "transfer_orders") == 1          # 没有叠加新单

            await graph.ainvoke(Command(resume="888888"), cfg)
            assert _order(db, order_id)["status"] == "executed"
            assert _count(db, "transfer_orders") == 1
            assert _balance(db) == DEFAULT_BALANCE - 30_000    # 恰好扣一次

    asyncio.run(scenario())


# ----------------------------------------------------------------- #4 反问封顶

def test_clarify_rounds_capped(tmp_path):
    """缺槽反问最多 MAX_CLARIFY_ROUNDS 轮,超限兜底收尾,绝不无限循环。"""
    from agent.graph import MAX_CLARIFY_ROUNDS

    db = _mk_db(tmp_path)

    async def scenario():
        # LLM 永远抽不出金额;响应表耗尽后自动循环,无需更多脚本
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            '{"payee": "张三"}',
        ]) as graph:
            cfg = {"configurable": {"thread_id": "fix-cap"}}
            r = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("给张三转点钱")]}, cfg)
            rounds = 0
            while r.get("__interrupt__"):
                rounds += 1
                assert rounds <= MAX_CLARIFY_ROUNDS + 1, "反问超过上限仍未停止"
                r = await graph.ainvoke(Command(resume="不知道"), cfg)
            assert rounds == MAX_CLARIFY_ROUNDS                # 恰好问了 N 轮
            snap = await graph.aget_state(cfg)
            assert snap.values["notice"]["kind"] == "clarify_gaveup"
            assert snap.next == ()                             # 本轮已收尾
            assert _count(db, "transfer_orders") == 0          # 零建单
            assert _balance(db) == DEFAULT_BALANCE

    asyncio.run(scenario())


# ----------------------------------------------------------------- #3 结算闸门

def test_settle_gate_confirm_and_reject(tmp_path):
    """AA 结算过 confirm_settle 闸门:确认才标记已付;取消则分摊项保持未付。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "split_bill"}',
            json.dumps({"title": "火锅", "total_yuan": "300", "even": True,
                        "participants": [{"name": "我"}, {"name": "老王"},
                                         {"name": "小刘"}]}, ensure_ascii=False),
            "AA 已发起,三人各 100 元。",
            '{"intent": "split_settle"}',
            json.dumps({"contact_name": "老王", "bill_id": None}, ensure_ascii=False),
            "老王已付。",
            '{"intent": "split_settle"}',
            json.dumps({"contact_name": "小刘", "bill_id": None}, ensure_ascii=False),
            "好的,没有标记小刘已付。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "fix-settle"}}
            # 发起 AA(均摊 100/100/100)
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("发起AA,火锅300,我、老王、小刘均摊")]}, cfg)
            assert _interrupt_payload(r1)["type"] == "confirm_split"
            await graph.ainvoke(Command(resume="确认"), cfg)
            assert _split_bill(db, 1)["status"] == "collecting"

            # 老王付款:先停在结算闸门
            r2 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("老王把钱转给我了")]}, cfg)
            gate = _interrupt_payload(r2)
            assert gate["type"] == "confirm_settle"
            assert gate["settle"]["contact_name"] == "老王"
            assert gate["settle"]["share_yuan"] == "100.00"
            await graph.ainvoke(Command(resume="确认"), cfg)
            bill = _split_bill(db, 1)
            assert [i["paid"] for i in bill["items"]] == [0, 1, 0]

            # 小刘"付款"但闸门处取消:分摊项保持未付
            r3 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("小刘也付了")]}, cfg)
            assert _interrupt_payload(r3)["type"] == "confirm_settle"
            r4 = await graph.ainvoke(Command(resume="取消"), cfg)
            assert not r4.get("__interrupt__")
            bill = _split_bill(db, 1)
            assert [i["paid"] for i in bill["items"]] == [0, 1, 0]  # 小刘未标记
            assert bill["status"] == "collecting"

    asyncio.run(scenario())


# ----------------------------------------------------------------- #5/#7 SSE 超时熔断

def test_api_turn_deadline_emits_error_frame(tmp_path, monkeypatch):
    """单轮 SSE 整体 deadline:模型挂死时前端必然收到 error 帧 + finish(error),
不再无限等待(评审 #5/#7)。"""
    import agent.api as agent_api
    from fastapi.testclient import TestClient
    from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
    from langchain_core.messages import AIMessage

    db = _mk_db(tmp_path)
    # sleep 是 FakeMessagesListChatModel 自带字段:让"模型"睡 5 秒模拟挂死
    hanging = FakeMessagesListChatModel(
        responses=[AIMessage(content='{"intent": "chat"}'), AIMessage(content="hi")],
        sleep=5)
    monkeypatch.setattr(agent_api, "TURN_DEADLINE_S", 1.0)  # 1 秒熔断
    app = agent_api.create_app(llm=hanging, db_path=db,
                               checkpoint_path=tmp_path / "api_ckpt.sqlite")
    with TestClient(app) as client:
        import time
        t0 = time.monotonic()
        r = client.post("/api/chat",
                        json={"messages": [{"role": "user", "content": "你好"}],
                              "thread_id": "fix-deadline"})
        elapsed = time.monotonic() - t0
        assert r.status_code == 200
        assert elapsed < 4.5, f"SSE 未按时熔断,耗时 {elapsed:.1f}s"
        frames = [json.loads(b[len("data: "):])
                  for b in r.text.strip().split("\n\n") if b.startswith("data: ")]
        kinds = [f["type"] for f in frames]
        assert kinds[0] == "start"
        assert "error" in kinds and "超时" in frames[kinds.index("error")]["errorText"]
        assert frames[-1] == {"type": "finish", "finishReason": "error"}


# ----------------------------------------------------------------- #9 agent 侧轨迹

def test_agent_bank_calls_full_trace(tmp_path):
    """agent 侧 bank_calls 完整:同节点多次调用不再互相覆盖(get_accounts 不丢),
且建单调用带幂等键。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            '{"payee": "张三", "amount_yuan": "500", "when": "now"}',
            "已向张三转账 500.00 元。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "fix-trace"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("给张三转 500 元")]}, cfg)
            await graph.ainvoke(Command(resume="888888"), cfg)
            snap = await graph.aget_state(cfg)
            traced = [c["tool"] for c in snap.values["bank_calls"]]
            # 修复点:get_accounts 在 policy_check 之前且不再被覆盖
            # verify_pay_password:支付密码闸的核验调用(入参脱敏 ***)在
            # create 之后、confirm 之前——动钱双因子的新轨迹点
            assert traced == ["resolve_contact", "get_accounts", "policy_check",
                              "create_transfer_order", "verify_pay_password",
                              "confirm_transfer_order"]
            create_call = snap.values["bank_calls"][3]
            assert create_call["args"]["idempotency_key"].startswith("agent-")
            assert create_call["ok"] is True

    asyncio.run(scenario())
