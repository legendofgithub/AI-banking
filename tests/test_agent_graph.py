"""M1 编排图测试:LangGraph 转账闭环 + interrupt 人工闸门 + AA 收款。

套路(全程不联网):
- LLM 用 langchain_core.language_models.fake_chat_models.FakeMessagesListChatModel
  脚本化:按图的节点调用顺序给出 router→抽取→播报 的固定响应;
- 银行工具走真实 MCP(stdio 子进程,照 scripts/mcp_smoke.py 的写法),
  BANK_CORE_DB 指向 tmp_path 临时库;
- 检查点器 AsyncSqliteSaver(同包 langgraph-checkpoint-sqlite;同步 SqliteSaver
  不支持异步方法、MCP 工具又仅支持异步,见 agent/graph.make_sqlite_checkpointer 注释);
- 金额断言一律整数"分",直接查临时库,与 bank_core 单测惯例一致;
- 每个测试在单个 asyncio.run 中跑完整会话(checkpointer 的 aiosqlite 连接
  绑定其事件循环,不能跨 loop 复用);_AgentSession 保证断言失败也关连接。

LLM 调用次序(不同分支节点数不同,脚本按此排布):
- 立即转账(无消歧): router → t_extract → t_report = 3 次
- 同名消歧: router → t_extract → t_report = 3 次(消歧解析是确定性代码,不调 LLM)
- 缺槽补问: router → t_extract → t_extract(合并答复) → t_report = 4 次
- AA 发起: router → sb_extract → sb_report = 3 次
- AA 结算: router → ss_extract → ss_report = 3 次
- 联动建计划: router → l_extract → l_report = 3 次(闸门/l_plan 不调 LLM)
- 联动到期处理: router → l_report = 2 次(l_due/逐动作闸门/execute 均不调 LLM)
- 账单月报: router → b_extract → b_report = 3 次(b_run 不调 LLM)
- 理财推荐: router → w_extract → w_report = 3 次(w_query 不调 LLM)
- 理财申购: router → w_extract → (闸门确认后) w_report = 3 次(定位/建单/扣款均不调 LLM)
- 理财赎回取消: router → w_extract → w_report = 3 次
- 卡片限额/挂失: router → k_extract → (闸门确认后) k_report = 3 次(挂失第二道闸也不调 LLM)
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from agent.bank import load_bank_tools
from agent.graph import build_agent_graph, make_sqlite_checkpointer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BALANCE = 1_000_000  # 1 万元(整数分)


# ----------------------------------------------------------------- 基础设施

def _mk_db(tmp_path: Path, balance_cents: int = DEFAULT_BALANCE) -> Path:
    """建临时银行库:1 用户 + 1 checking 账户 + 3 联系人(林悦同名 ×2)。

    以子进程 + BANK_CORE_DB 环境变量初始化 bank_core(它导入时读该变量),
    不触碰默认 data/bank.db。
    """
    import os
    import subprocess
    import sys

    db = tmp_path / "bank.db"
    env = {**os.environ, "BANK_CORE_DB": str(db)}
    script = (
        "from bank_core.db import init_db\n"
        "import sys\n"
        "c = init_db(sys.argv[1])\n"
        "c.execute(\"INSERT INTO users (id,name,phone,created_at) "
        "VALUES (1,'测试用户','13800000000','2026-01-01T00:00:00')\")\n"
        f"c.execute(\"INSERT INTO accounts (id,user_id,type,name,balance_cents,opened_at) "
        f"VALUES (1,1,'checking','工资卡',{balance_cents},'2026-01-01T00:00:00')\")\n"
        "c.execute(\"INSERT INTO contacts (id,user_id,name,phone,relation,note) "
        "VALUES (1,1,'林悦','13900008821','spouse','招商银行 尾号8821')\")\n"
        "c.execute(\"INSERT INTO contacts (id,user_id,name,phone,relation,note) "
        "VALUES (2,1,'林悦','13711112222','friend','建设银行 尾号2222')\")\n"
        "c.execute(\"INSERT INTO contacts (id,user_id,name,phone,relation,note) "
        "VALUES (3,1,'张三','13633334444','colleague','')\")\n"
        "c.commit(); c.close()\n"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(db)],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, f"临时库初始化失败:\n{proc.stdout}\n{proc.stderr}"
    # 登录态预置:user1 的 auth 行(登录密码 Demo@12345 / 支付密码 888888)。
    # 动钱/敏感闸门走 verify_pay_password 真实核验,测试应答用 "888888"。
    from bank_core.auth_core import hash_password as _hp
    ac = sqlite3.connect(db)
    ac.execute(
        "INSERT INTO auth_users (user_id, identifier, identifier_type, "
        "password_hash, pay_hash, created_at) VALUES "
        "(1,'13800000000','phone',?,?,'2026-01-01T00:00:00')",
        (_hp("Demo@12345"), _hp("888888")))
    ac.commit()
    ac.close()
    return db


def _ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _balance(db: Path, account_id: int = 1) -> int:
    conn = _ro(db)
    try:
        return conn.execute(
            "SELECT balance_cents FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
    finally:
        conn.close()


def _order(db: Path, order_id: int) -> dict:
    conn = _ro(db)
    try:
        row = conn.execute(
            "SELECT * FROM transfer_orders WHERE id=?", (order_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _audited_tools(db: Path) -> list[str]:
    conn = _ro(db)
    try:
        return [r[0] for r in conn.execute("SELECT tool FROM audit_log").fetchall()]
    finally:
        conn.close()


def _split_bill(db: Path, bill_id: int) -> dict:
    conn = _ro(db)
    try:
        bill = dict(conn.execute(
            "SELECT * FROM split_bills WHERE id=?", (bill_id,)).fetchone())
        bill["items"] = [dict(r) for r in conn.execute(
            "SELECT * FROM split_bill_items WHERE bill_id=? ORDER BY id",
            (bill_id,)).fetchall()]
        return bill
    finally:
        conn.close()


def _count(db: Path, table: str) -> int:
    conn = _ro(db)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _interrupt_payload(result: dict) -> dict:
    """从 ainvoke 结果取出 interrupt 载荷(图被暂停时才有)。"""
    ints = result.get("__interrupt__")
    assert ints, f"预期图被 interrupt 暂停,实际直接结束: keys={list(result)}"
    return ints[0].value


def _ai_text(result: dict) -> str:
    msgs = result.get("messages") or []
    assert msgs, "结果里没有消息"
    last = msgs[-1]
    return last.content if isinstance(last.content, str) else str(last.content)


def _fake_llm(*responses: str) -> FakeMessagesListChatModel:
    return FakeMessagesListChatModel(responses=[AIMessage(content=r) for r in responses])


def _slots_json(**kv) -> str:
    return json.dumps(kv, ensure_ascii=False)


class _AgentSession:
    """测试脚手架:装载工具 + 建检查点器 + 编译图;退出时确保关闭连接。

    checkpointer 的 aiosqlite 连接绑定创建它的事件循环,断言失败也必须关掉,
    否则残留的工作线程可能让 pytest 进程退不出去(Windows 实测)。
    """

    def __init__(self, db: Path, tmp_path: Path, responses: list[str]):
        self._db, self._tmp, self._resp = db, tmp_path, responses
        self.graph = None
        self._conn = None

    async def __aenter__(self):
        tools = await load_bank_tools(self._db)
        saver, self._conn = await make_sqlite_checkpointer(
            self._tmp / "agent_ckpt.sqlite")
        self.graph = build_agent_graph(_fake_llm(*self._resp), tools, saver)
        return self.graph

    async def __aexit__(self, *exc):
        if self._conn is not None:
            await self._conn.close()
        return False


def run(coro):
    return asyncio.run(coro)


# ----------------------------------------------------------------- 立即转账:闸门与扣款

def test_transfer_gate_then_confirm(tmp_path):
    """立即转账:先 interrupt 确认(余额未动),确认后余额恰好减少且订单 executed。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(payee="张三", amount_yuan="500", when="now"),
            "已向张三转账 500.00 元。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t1"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("给张三转 500 元")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_transfer"
            order_id = payload["order"]["id"]
            assert payload["order"]["amount_yuan"] == "500.00"
            # 建单后、确认前:订单待确认,余额分文未动(铁律:两步走)
            assert _order(db, order_id)["status"] == "pending_confirm"
            assert _balance(db) == DEFAULT_BALANCE

            r2 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert "已向张三" in _ai_text(r2)
            # 恰好扣 500 元 = 50000 分
            assert _balance(db) == DEFAULT_BALANCE - 50_000
            order = _order(db, order_id)
            assert order["status"] == "executed"
            assert order["amount_cents"] == 50_000
            assert order["to_contact_id"] == 3
            # 审计链:建单→确认都留痕
            tools_called = _audited_tools(db)
            assert "create_transfer_order" in tools_called
            assert "confirm_transfer_order" in tools_called

    run(scenario())


def test_transfer_cancel_keeps_balance(tmp_path):
    """闸门处取消:订单 cancelled,余额不变,cancel 也留审计、绝不 confirm。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(payee="张三", amount_yuan="88.5", when="now"),
            "好的,已取消这笔转账。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t2"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("转 88.5 元给张三")]}, cfg)
            payload = _interrupt_payload(r1)
            order_id = payload["order"]["id"]

            r2 = await graph.ainvoke(Command(resume="取消"), cfg)
            assert "取消" in _ai_text(r2)
            assert _balance(db) == DEFAULT_BALANCE  # 一分没动
            assert _order(db, order_id)["status"] == "cancelled"
            tools_called = _audited_tools(db)
            assert "cancel_transfer_order" in tools_called
            assert "confirm_transfer_order" not in tools_called

    run(scenario())


def test_transfer_missing_amount_clarify(tmp_path):
    """缺金额:先反问,答复后继续走完建单→确认→扣款。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(payee="张三"),                        # 首轮:没给金额
            _slots_json(payee="张三", amount_yuan="120", when="now"),  # 合并反问答复
            "已向张三转账 120.00 元。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t3"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("给张三转点钱")]}, cfg)
            ask = _interrupt_payload(r1)
            assert ask["type"] == "ask_slot"
            assert "amount_yuan" in ask["missing"]

            r2 = await graph.ainvoke(Command(resume="转 120 块"), cfg)
            payload = _interrupt_payload(r2)  # 补齐后进入确认闸门
            assert payload["type"] == "confirm_transfer"
            r3 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert _balance(db) == DEFAULT_BALANCE - 12_000
            assert _order(db, payload["order"]["id"])["status"] == "executed"

    run(scenario())


# ----------------------------------------------------------------- 同名消歧

def test_transfer_ambiguous_contact_disambiguation(tmp_path):
    """同名联系人 ≥2:反问 → 用户答"尾号2222" → 选定后继续,转账落到选中的 contact。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(payee="林悦", amount_yuan="66.6", when="now"),
            "已向林悦(尾号2222)转账 66.60 元。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t4"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("给林悦转66.6")]}, cfg)
            pick = _interrupt_payload(r1)
            assert pick["type"] == "pick_contact"
            assert len(pick["candidates"]) == 2

            r2 = await graph.ainvoke(Command(resume="尾号2222那个"), cfg)
            payload = _interrupt_payload(r2)  # 选完人进入确认闸门
            assert payload["type"] == "confirm_transfer"
            r3 = await graph.ainvoke(Command(resume="888888"), cfg)
            order = _order(db, payload["order"]["id"])
            assert order["status"] == "executed"
            assert order["to_contact_id"] == 2  # 建行尾号2222 的林悦(id=2)
            assert _balance(db) == DEFAULT_BALANCE - 6_660

    run(scenario())


# ----------------------------------------------------------------- 风控拦截

def test_transfer_over_limit_blocked_by_policy(tmp_path):
    """超单笔限额:policy_check 不通过,建单前即被拦截并播报原因,余额/订单零变化。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(payee="张三", amount_yuan="60000", when="now"),  # 6 万 > 单笔 5 万
            "这笔转账超过单笔限额,已拦截。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t5"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("给张三转 60000")]}, cfg)
            assert not r1.get("__interrupt__")  # 不该进确认闸门
            assert "拦截" in _ai_text(r1)
            assert _balance(db) == DEFAULT_BALANCE
            assert _count(db, "transfer_orders") == 0  # 建单都没发生
            tools_called = _audited_tools(db)
            assert "policy_check" in tools_called
            assert "create_transfer_order" not in tools_called

    run(scenario())


# ----------------------------------------------------------------- 定时转账

def test_scheduled_transfer_no_immediate_debit(tmp_path):
    """定时转账:确认后订单 scheduled、不立即扣款,也不调用 confirm。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(payee="张三", amount_yuan="300", when="scheduled",
                        scheduled_at="2026-10-01T09:00:00"),
            "已建立定时转账单,到期会提醒你确认后才扣款。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t6"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("10月1号早上9点给张三转300")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_transfer"
            assert payload["order"]["scheduled_at"] == "2026-10-01T09:00:00"
            order_id = payload["order"]["id"]
            assert _order(db, order_id)["status"] == "scheduled"  # 建的就是定时单

            r2 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert _balance(db) == DEFAULT_BALANCE  # 确认≠扣款
            assert _order(db, order_id)["status"] == "scheduled"
            tools_called = _audited_tools(db)
            assert "create_transfer_order" in tools_called
            assert "confirm_transfer_order" not in tools_called  # 绝不提前执行

    run(scenario())


# ----------------------------------------------------------------- AA 收款

def test_split_bill_create_then_settle(tmp_path):
    """AA:发起(闸门确认后建单、均摊 100/100/100)→ 老王付款结算 → 进度 collecting。"""
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
            "老王已付,还差 2 人。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t7"}}
            # 发起 → 闸门
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("发起AA,昨晚火锅一共300,我、老王、小刘均摊")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_split"
            parts = {p["name"]: p["share_yuan"] for p in payload["bill"]["participants"]}
            assert parts == {"我": "100.00", "老王": "100.00", "小刘": "100.00"}
            # 确认发起
            r2 = await graph.ainvoke(Command(resume="确认"), cfg)
            assert "AA" in _ai_text(r2)
            bill = _split_bill(db, 1)
            assert bill["total_cents"] == 30_000
            assert bill["status"] == "collecting"
            assert [i["share_cents"] for i in bill["items"]] == [10_000, 10_000, 10_000]
            # AA 不动自己的余额(收别人的钱)
            assert _balance(db) == DEFAULT_BALANCE

            # 结算:老王付了(同一 thread,图记得最近账单);
            # 评审修复:settle 现在也要过人工确认闸门(confirm_settle)
            r3 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("老王把钱转给我了")]}, cfg)
            settle_gate = _interrupt_payload(r3)
            assert settle_gate["type"] == "confirm_settle"
            assert settle_gate["settle"]["contact_name"] == "老王"
            assert settle_gate["settle"]["share_yuan"] == "100.00"
            r4 = await graph.ainvoke(Command(resume="确认"), cfg)
            assert "老王" in _ai_text(r4)
            bill = _split_bill(db, 1)
            item = next(i for i in bill["items"] if i["contact_name"] == "老王")
            assert item["paid"] == 1
            others = [i for i in bill["items"] if i["contact_name"] != "老王"]
            assert all(i["paid"] == 0 for i in others)
            assert bill["status"] == "collecting"  # 还差 2 人
            # agent 侧轨迹含 settle 调用(评审修复后同节点多次调用不再互相覆盖)
            snap = await graph.aget_state(cfg)
            traced = [c["tool"] for c in snap.values.get("bank_calls", [])]
            assert "settle_split_bill_item" in traced
            assert "get_split_bill" in traced
            # 注:bank_core 的 settle_split_bill_item 本身不写 audit_log(ledger.py 现状),
            # 结算事实由上面 split_bill_items 的 paid 位与 get_split_bill 审计共同佐证。
            tools_called = _audited_tools(db)
            assert "create_split_bill" in tools_called
            assert "get_split_bill" in tools_called

    run(scenario())


def test_split_bill_gate_cancel(tmp_path):
    """AA 闸门取消:不建单、零副作用。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "split_bill"}',
            json.dumps({"title": "奶茶", "total_yuan": "90", "even": True,
                        "participants": [{"name": "我"}, {"name": "小美"}]},
                       ensure_ascii=False),
            "好的,这次先不发起。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t8"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("帮我发起AA收奶茶钱90,我和小美平摊")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_split"
            r2 = await graph.ainvoke(Command(resume="取消"), cfg)
            assert "不" in _ai_text(r2)
            assert _count(db, "split_bills") == 0
            assert _balance(db) == DEFAULT_BALANCE

    run(scenario())


# ----------------------------------------------------------------- 会话恢复与兜底

def test_thread_resume_across_invokes(tmp_path):
    """checkpointer:同一 thread 中断/恢复,暂停点与订单都在检查点里,可续跑。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(payee="张三", amount_yuan="10", when="now"),
            "已转账 10.00 元。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t9"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("给张三转10块")]}, cfg)
            order_id = _interrupt_payload(r1)["order"]["id"]
            # 检查点视角:暂停在 t_gate,订单已在状态里
            snap = await graph.aget_state(cfg)
            assert snap.next == ("t_gate",)
            assert snap.values["order"]["id"] == order_id
            r2 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert _balance(db) == DEFAULT_BALANCE - 1_000

    run(scenario())


def test_chat_fallback_no_bank_calls(tmp_path):
    """非转账意图:闲聊兜底,不碰任何银行工具、无 interrupt。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "chat"}',
            "你好,我是练功假银行助手,目前支持转账和 AA 收款。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t10"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage("你好呀")]}, cfg)
            assert not r1.get("__interrupt__")
            assert "转账" in _ai_text(r1)
            assert _audited_tools(db) == []

    run(scenario())


# ----------------------------------------------------------------- 跨场景联动(linkage)
# 剧本日期相对今天推算(与 test_linkage.py 同思路,测试永不因日期流逝翻车):
# 生日 = 今天+4;前 2 天(今天+2)鲜花到期、前 1 天蛋糕到期。

_LINK_TODAY = date.today()
LINK_BIRTHDAY = (_LINK_TODAY + timedelta(days=4)).isoformat()
LINK_AS_OF = (_LINK_TODAY + timedelta(days=3)).isoformat() + "T10:00:00"

_LINK_ACTIONS_JSON = json.dumps(
    {"event_title": "林悦的生日", "event_date": LINK_BIRTHDAY,
     "budget_yuan": "1000",
     "actions": [
         {"what": "鲜花", "merchant": "花店", "days_before": 2, "amount_yuan": "300"},
         {"what": "蛋糕", "merchant": "蛋糕店", "days_before": 1, "amount_yuan": "200"}]},
    ensure_ascii=False)


def _mk_linkage_db(tmp_path: Path) -> Path:
    """联动测试库:1 用户 + checking/savings 两账户(linkage 锁定单要求有理财专户)。

    照 _mk_db 的子进程初始化写法(避免库结构漂移),只是多插一行 savings 账户。
    """
    import os
    import subprocess
    import sys

    db = tmp_path / "bank_linkage.db"
    env = {**os.environ, "BANK_CORE_DB": str(db)}
    script = (
        "from bank_core.db import init_db\n"
        "import sys\n"
        "c = init_db(sys.argv[1])\n"
        "c.execute(\"INSERT INTO users (id,name,phone,created_at) "
        "VALUES (1,'测试用户','13800000000','2026-01-01T00:00:00')\")\n"
        f"c.execute(\"INSERT INTO accounts (id,user_id,type,name,balance_cents,opened_at) "
        f"VALUES (1,1,'checking','工资卡',{DEFAULT_BALANCE},'2026-01-01T00:00:00')\")\n"
        f"c.execute(\"INSERT INTO accounts (id,user_id,type,name,balance_cents,opened_at) "
        f"VALUES (2,1,'savings','理财专户',0,'2026-01-01T00:00:00')\")\n"
        "c.commit(); c.close()\n"
    )
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script, str(db)],
        cwd=str(PROJECT_ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=120)
    assert proc.returncode == 0, f"临时库初始化失败:\n{proc.stdout}\n{proc.stderr}"
    # 登录态预置:user1 的 auth 行(登录密码 Demo@12345 / 支付密码 888888)。
    # 动钱/敏感闸门走 verify_pay_password 真实核验,测试应答用 "888888"。
    from bank_core.auth_core import hash_password as _hp
    ac = sqlite3.connect(db)
    ac.execute(
        "INSERT INTO auth_users (user_id, identifier, identifier_type, "
        "password_hash, pay_hash, created_at) VALUES "
        "(1,'13800000000','phone',?,?,'2026-01-01T00:00:00')",
        (_hp("Demo@12345"), _hp("888888")))
    ac.commit()
    ac.close()
    return db


def _seed_linkage_plan(db: Path) -> int:
    """直接在库里建好联动计划(绕过图,聚焦到期处理链路),返回 plan_id。

    图侧 l_due 的计划发现走 list_scheduled_tasks(payload.plan_id)——
    这里建计划产生的 2 条 pending 提醒任务正是它的发现来源。
    """
    from bank_core.linkage import LinkageService
    from bank_core.money import yuan_to_cents

    # row_factory 必须显式设:bank_core 的服务层按列名取值(init_db 才会自带)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute(
            "INSERT INTO user_events (id,user_id,event_type,title,event_date,"
            "repeat_yearly,note) VALUES (1,1,'birthday','林悦的生日',?,1,'')",
            (LINK_BIRTHDAY,))
        conn.commit()
        plan = LinkageService(conn, 1).create_linkage_plan(
            event_id=1, title="林悦的生日联动",
            budget_cents=yuan_to_cents("1000"),
            actions=[
                {"what": "鲜花", "merchant": "花店",
                 "amount_cents": yuan_to_cents("300"), "days_before": 2},
                {"what": "蛋糕", "merchant": "蛋糕店",
                 "amount_cents": yuan_to_cents("200"), "days_before": 1},
            ])
        return int(plan["plan_id"])
    finally:
        conn.close()


def _linkage_row(db: Path, plan_id: int) -> dict:
    conn = _ro(db)
    try:
        row = conn.execute(
            "SELECT * FROM linkage_plans WHERE id=?", (plan_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _linkage_tx(db: Path, plan_id: int, action_idx: int) -> dict:
    conn = _ro(db)
    try:
        row = conn.execute(
            "SELECT * FROM transactions WHERE external_ref=?",
            (f"linkage:{plan_id}:{action_idx}",)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def test_linkage_plan_gate_then_confirm(tmp_path):
    """联动建计划:建计划只建锁定单(不动钱)→ 闸门 → 确认后锁定单 executed、
    活期余额恰好减少预算金额,计划 active、提醒任务已排上。"""
    db = _mk_linkage_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "linkage"}',
            _LINK_ACTIONS_JSON,
            "预算已预留,联动计划生效。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "l1"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(
                "帮我准备林悦的生日,预算1000,提前2天订300元的鲜花,提前1天订200元的蛋糕")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_linkage"
            plan = payload["plan"]
            assert plan["budget_yuan"] == "1000.00"
            assert plan["event"]["date"] == LINK_BIRTHDAY
            assert [a["what"] for a in plan["actions"]] == ["鲜花", "蛋糕"]
            assert plan["actions"][0]["amount_yuan"] == "300.00"
            assert plan["actions"][0]["days_before"] == 2
            assert plan["lock_order"]["status"] == "pending_confirm"
            plan_id = plan["plan_id"]
            order_id = plan["lock_order"]["order_id"]
            # 建计划后、确认前:锁定单待确认,活期分文未动(两步走铁律)
            assert _balance(db) == DEFAULT_BALANCE
            assert _order(db, order_id)["status"] == "pending_confirm"
            assert _count(db, "scheduled_tasks") == 2  # 每动作一条提醒

            r2 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert "预算" in _ai_text(r2)
            order = _order(db, order_id)
            assert order["status"] == "executed"           # 锁定单真正划转
            assert order["amount_cents"] == 100_000        # 恰好 1000 元
            assert order["memo"] == "生日预留"
            # 活期余额恰好减少预算;理财专户余额不变——bank_core 的
            # confirm_transfer_order 只记转出侧(transfer_out 落一笔 out 流水,
            # to_account_tail 仅展示用,见 ledger.confirm_transfer_order),
            # 所以"预留"的可观察效果是活期减少,而非 savings 增加。
            assert _balance(db, 1) == DEFAULT_BALANCE - 100_000
            assert _balance(db, 2) == 0
            assert _linkage_row(db, plan_id)["status"] == "active"
            tools_called = _audited_tools(db)
            assert "create_linkage_plan" in tools_called
            assert "confirm_transfer_order" in tools_called
            assert "execute_linkage_action" not in tools_called  # 到期前绝不购买

    run(scenario())


def test_linkage_due_actions_gated_then_done(tmp_path):
    """到期处理:提醒文本进图 → 逐动作闸门(每项单独确认)→ 真实扣款流水、计划 done。"""
    db = _mk_linkage_db(tmp_path)
    plan_id = _seed_linkage_plan(db)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "linkage"}',
            "两样都买好了,联动计划完成。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "l2"}}
            msg = f"(到期提醒) 计划《林悦的生日联动》的【鲜花】今天到期,请帮我处理"
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(msg)]}, cfg)
            g1 = _interrupt_payload(r1)  # 第 1 个动作闸门:鲜花
            assert g1["type"] == "confirm_linkage_action"
            assert g1["action"]["what"] == "鲜花"
            assert g1["action"]["merchant"] == "花店"
            assert g1["action"]["amount_yuan"] == "300.00"
            assert g1["action"]["plan_id"] == plan_id
            assert g1["action"]["plan_title"] == "林悦的生日联动"
            assert _balance(db) == DEFAULT_BALANCE  # 闸门前一分未动

            r2 = await graph.ainvoke(Command(resume="888888"), cfg)
            g2 = _interrupt_payload(r2)  # 第 2 个动作闸门:蛋糕
            assert g2["type"] == "confirm_linkage_action"
            assert g2["action"]["what"] == "蛋糕"
            assert g2["action"]["amount_yuan"] == "200.00"
            tx0 = _linkage_tx(db, plan_id, 0)  # 鲜花已购买:online 流水+真实扣款
            assert tx0["amount_cents"] == 30_000
            assert tx0["tx_type"] == "online" and tx0["counterparty"] == "花店"
            assert _balance(db) == DEFAULT_BALANCE - 30_000

            r3 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert not r3.get("__interrupt__")  # 全部处理完,不再停闸门
            assert "完成" in _ai_text(r3)
            assert _balance(db) == DEFAULT_BALANCE - 30_000 - 20_000
            tx1 = _linkage_tx(db, plan_id, 1)
            assert tx1["amount_cents"] == 20_000
            assert _linkage_row(db, plan_id)["status"] == "done"
            tools_called = _audited_tools(db)
            assert tools_called.count("execute_linkage_action") == 2

    run(scenario())


def test_linkage_gate_cancel_cancels_plan(tmp_path):
    """联动闸门取消:计划 cancelled,锁定单与提醒任务一并撤销,余额零变化。"""
    db = _mk_linkage_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "linkage"}',
            _LINK_ACTIONS_JSON,
            "已取消联动计划。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "l3"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(
                "帮我准备林悦的生日,预算1000,提前2天订300元的鲜花,提前1天订200元的蛋糕")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_linkage"
            plan_id = payload["plan"]["plan_id"]
            order_id = payload["plan"]["lock_order"]["order_id"]

            r2 = await graph.ainvoke(Command(resume="取消"), cfg)
            assert "取消" in _ai_text(r2)
            assert _balance(db) == DEFAULT_BALANCE  # 一分没动
            assert _order(db, order_id)["status"] == "cancelled"
            row = _linkage_row(db, plan_id)
            assert row["status"] == "cancelled"
            # 提醒任务全部撤销(不再弹"该订鲜花了")
            conn = _ro(db)
            try:
                sts = [r[0] for r in conn.execute(
                    "SELECT status FROM scheduled_tasks").fetchall()]
            finally:
                conn.close()
            assert sts and all(s == "cancelled" for s in sts)
            tools_called = _audited_tools(db)
            assert "cancel_linkage_plan" in tools_called
            assert "confirm_transfer_order" not in tools_called
            assert "execute_linkage_action" not in tools_called

    run(scenario())


# ----------------------------------------------------------------- 账单分析 / 理财 / 卡片
# 种子行逐字段抄自 bank_core/seed.py(PRODUCTS 与 cards/risk_profiles/wealth_holdings),
# 断言口径与演示库一致;测试永不依赖 data/bank.db。

# (id,code,name,p_type,risk,bps,min分,lock,sub_bps,red_bps,intro) ← seed.PRODUCTS
_W_SEED_PRODUCTS = [
    (1, "MF001", "余额+货币基金", "money_fund", 1, 185, 100, 0, 0, 0,
     "随存随取，七日年化约1.85%"),
    (2, "BD003", "稳健纯债基金", "bond", 2, 320, 10000, 0, 0, 15,
     "中低风险，历史年化约3.2%"),
    (3, "MX006", "科技创新股票基金", "mixed", 4, 850, 10000, 0, 15, 50,
     "高波动，追求长期增值"),
]


def _seed_bill_wealth_card(db: Path) -> None:
    """在 _mk_db 的库上补 理财产品/风险测评/持仓/卡片(行数据来自 bank_core.seed)。

    - 风险测评:五题各 3 分 → 15 分 → C3(seed.py 同款演示用户);
    - 持仓:稳健纯债基金 10000 元,估值 10180 元(赎回费 15bp → 费 15.27/到手 10164.73);
    - 卡片:1 张活跃借记卡(6222****8821,日限 5000/单笔 2000)——单卡场景下
      k_extract 的确定性自动选卡直接命中,不触发多卡反问。
    """
    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            """INSERT INTO wealth_products
               (id,code,name,p_type,risk_level,expected_return_bps,
                min_subscribe_cents,lock_days,subscription_fee_bps,
                redemption_fee_bps,intro)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""", _W_SEED_PRODUCTS)
        conn.execute(
            """INSERT INTO risk_profiles
               (user_id,answers_json,score,level,updated_at)
               VALUES (1,'{"稳健偏好":3,"投资经验":3,"亏损容忍":3,
                         "投资期限":3,"收入稳定":3}',15,'C3',
                       '2026-01-01T00:00:00')""")
        conn.execute(
            """INSERT INTO wealth_holdings
               (user_id,product_id,principal_cents,est_value_cents,status,
                subscribed_at)
               VALUES (1,2,1000000,1018000,'holding','2026-01-01T00:00:00')""")
        conn.execute(
            """INSERT INTO cards (id,user_id,account_id,card_no_masked,card_type,
               status,daily_limit_cents,per_tx_limit_cents,created_at)
               VALUES (1,1,1,'6222 **** **** 8821','debit','active',500000,200000,
                       '2026-01-01T00:00:00')""")
        conn.commit()
    finally:
        conn.close()


def _prev_month(today: date) -> str:
    first = today.replace(day=1)
    prev = first - timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"


def _card_row(db: Path, card_id: int) -> dict:
    conn = _ro(db)
    try:
        row = conn.execute("SELECT * FROM cards WHERE id=?",
                           (card_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def _holding_rows(db: Path) -> list[dict]:
    conn = _ro(db)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM wealth_holdings ORDER BY id").fetchall()]
    finally:
        conn.close()


def _traced(snap_values: dict, tool: str) -> list[dict]:
    """agent 侧轨迹里指定工具的全部调用记录。"""
    return [c for c in snap_values.get("bank_calls", []) if c["tool"] == tool]


def test_bill_monthly_report_readonly(tmp_path):
    """账单月报:bill_analysis 意图 → monthly_report(上个月,确定性换算)被调,
    只读场景无 interrupt,余额零变化。"""
    db = _mk_db(tmp_path)
    prev = _prev_month(date.today())

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "bill_analysis"}',
            json.dumps({"ask": "monthly_report", "period": "上个月",
                        "category": None}, ensure_ascii=False),
            f"{prev} 的月报已生成,收支数字来自银行返回。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "b1"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("看下上个月的账单月报")]}, cfg)
            assert not r1.get("__interrupt__")  # 只读场景没有闸门
            assert "月报" in _ai_text(r1)
            snap = await graph.aget_state(cfg)
            calls = _traced(snap.values, "monthly_report")
            # 月份由确定性代码从"上个月"换算(不是 LLM 算的)
            assert calls and calls[0]["args"] == {"month": prev}
            assert "monthly_report" in _audited_tools(db)
            assert _balance(db) == DEFAULT_BALANCE  # 只读零变化
            assert _count(db, "transfer_orders") == 0

    run(scenario())


def test_wealth_query_filters_by_risk_level(tmp_path):
    """理财推荐:有测评(C3)→ list_wealth_products 带 max_risk_level=3,
    R4 产品被排除,全程零动钱。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "wealth"}',
            json.dumps({"action": "query", "keyword": None, "product_id": None,
                        "amount_yuan": None, "holding_id": None, "p_type": None,
                        "assess_answer": None}, ensure_ascii=False),
            "已按你的风险等级筛出可购产品。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "w1"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("帮我推荐几款理财产品")]}, cfg)
            assert not r1.get("__interrupt__")
            snap = await graph.aget_state(cfg)
            calls = _traced(snap.values, "list_wealth_products")
            assert calls, "理财推荐必须调 list_wealth_products"
            assert calls[0]["args"].get("max_risk_level") == 3  # C3 → R3 上限
            notice = snap.values.get("notice") or {}
            assert notice.get("kind") == "wealth_products"
            names = {p.get("name") for p in notice.get("products") or []}
            assert names == {"余额+货币基金", "稳健纯债基金"}  # R4 的科技股基被排除
            # 零动钱:没有任何申购/赎回轨迹,余额不变
            tools_traced = {c["tool"] for c in snap.values.get("bank_calls", [])}
            assert "subscribe_product" not in tools_traced
            assert "redeem_product" not in tools_traced
            assert _balance(db) == DEFAULT_BALANCE

    run(scenario())


def test_wealth_subscribe_gate_then_execute(tmp_path):
    """申购全流程:关键词定位 → confirmed=False 建单(不动钱)→ confirm_wealth 闸门
    → 确认后同参数 confirmed=True 真扣款:持仓新增、余额恰好扣减、两段轨迹留痕。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "wealth"}',
            json.dumps({"action": "subscribe", "keyword": "余额+货币基金",
                        "product_id": None, "amount_yuan": "5000",
                        "holding_id": None, "p_type": None,
                        "assess_answer": None}, ensure_ascii=False),
            "申购已完成,资金已扣减。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "w2"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("我要申购5000元的余额+货币基金")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_wealth"
            order = payload["order"]
            assert order["kind"] == "subscribe"
            assert order["product"] == "余额+货币基金"
            assert order["amount_yuan"] == "5000.00"
            assert order["fee_yuan"] == "0.00"      # 货基申购费 0,原样引用产品库
            assert order["risk_level"] == "R1"
            assert order["from_account_name"] == "工资卡"
            # 建单后、确认前:钱一分没动,持仓没变(两步走铁律)
            snap = await graph.aget_state(cfg)
            pend = _traced(snap.values, "subscribe_product")
            assert pend and pend[0]["args"]["confirmed"] is False
            assert _balance(db) == DEFAULT_BALANCE
            assert len(_holding_rows(db)) == 1

            r2 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert "申购" in _ai_text(r2)
            # 确认 = 同参数 confirmed=True 二次调用(不是订单号)
            snap = await graph.aget_state(cfg)
            subs = _traced(snap.values, "subscribe_product")
            assert len(subs) == 2
            assert subs[1]["args"] == {**subs[0]["args"], "confirmed": True}
            assert _balance(db) == DEFAULT_BALANCE - 500_000  # 恰好扣 5000 元
            rows = _holding_rows(db)
            assert len(rows) == 2  # 播种 1 笔 + 新申购 1 笔
            new = next(r for r in rows if r["product_id"] == 1)
            assert new["principal_cents"] == 500_000  # 费率 0 → 本金=申购额
            assert new["status"] == "holding"
            assert "subscribe_product" in _audited_tools(db)  # HIGH 动钱留审计

    run(scenario())


def test_wealth_redeem_gate_cancel_keeps_money(tmp_path):
    """赎回取消:confirmed=False 建单 → 闸门取消 → 绝不 confirmed=True,
    持仓与余额分文不变。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "wealth"}',
            json.dumps({"action": "redeem", "keyword": "稳健纯债基金",
                        "product_id": None, "amount_yuan": None,
                        "holding_id": None, "p_type": None,
                        "assess_answer": None}, ensure_ascii=False),
            "好的,本次赎回已取消,资金未变动。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "w3"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("把稳健纯债基金赎回了")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_wealth"
            order = payload["order"]
            assert order["kind"] == "redeem"
            assert order["product"] == "稳健纯债基金"
            assert order["est_value_yuan"] == "10180.00"
            assert order["fee_yuan"] == "15.27"          # 15bp 赎回费,整数分算
            assert order["redeem_net_yuan"] == "10164.73"
            r2 = await graph.ainvoke(Command(resume="取消"), cfg)
            assert "取消" in _ai_text(r2)
            # 取消后:持仓未动、余额未动、没有任何 confirmed=True 调用
            snap = await graph.aget_state(cfg)
            reds = _traced(snap.values, "redeem_product")
            assert len(reds) == 1 and reds[0]["args"]["confirmed"] is False
            rows = _holding_rows(db)
            assert len(rows) == 1 and rows[0]["status"] == "holding"
            assert _balance(db) == DEFAULT_BALANCE
            assert "redeem_product" not in _audited_tools(db)  # bank 侧未落执行审计

    run(scenario())


def test_card_limits_gate_then_set(tmp_path):
    """卡片限额:闸门复述当前/新限额对照,确认后 set_card_limits 参数正确(规范化元)。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "card"}',
            json.dumps({"action": "limits", "card_id": None, "card_hint": None,
                        "card_type": None, "daily_limit_yuan": "3000",
                        "per_tx_limit_yuan": "1000",
                        "status_target": None}, ensure_ascii=False),
            "卡片限额已调整。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "k1"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("把卡日限额改成3000,单笔改成1000")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_card"
            card = payload["card"]
            assert card["kind"] == "limits"
            assert card["card_id"] == 1
            assert card["tail"] == "8821"
            assert card["current_daily_limit_yuan"] == "5000.00"
            assert card["new_daily_limit_yuan"] == "3000.00"
            assert card["new_per_tx_limit_yuan"] == "1000.00"
            # 确认前限额未动
            assert _card_row(db, 1)["daily_limit_cents"] == 500000

            r2 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert "限额" in _ai_text(r2)
            row = _card_row(db, 1)
            assert row["daily_limit_cents"] == 300_000   # 3000 元
            assert row["per_tx_limit_cents"] == 100_000  # 1000 元
            snap = await graph.aget_state(cfg)
            calls = _traced(snap.values, "set_card_limits")
            assert calls and calls[0]["args"] == {
                "card_id": 1, "daily_limit_yuan": "3000.00",
                "per_tx_limit_yuan": "1000.00"}
            # 踩坑:bank_core 的 set_card_limits(ledger.py)不写 audit_log(与
            # apply_card/set_card_status 不一致),本次不动 bank_core——执行事实
            # 由上面 cards 行的限额值 + agent 侧 bank_calls 轨迹共同佐证。
            assert _balance(db) == DEFAULT_BALANCE  # 调限额不动钱

    run(scenario())


def test_card_lost_requires_double_gate(tmp_path):
    """挂失双闸:第一道 yes、第二道 no → 不执行;两道都 yes → 才真正 lost。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)

    # A) 第一道确认、第二道取消:绝不 set_card_status
    async def scenario_cancel():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "card"}',
            json.dumps({"action": "status", "card_id": None, "card_hint": None,
                        "card_type": None, "daily_limit_yuan": None,
                        "per_tx_limit_yuan": None,
                        "status_target": "lost"}, ensure_ascii=False),
            "好的,没有挂失。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "k2"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("帮我把银行卡挂失了")]}, cfg)
            g1 = _interrupt_payload(r1)  # 第一道闸
            assert g1["type"] == "confirm_card"
            assert g1["card"]["kind"] == "status"
            assert g1["card"]["target"] == "lost"
            assert g1["card"]["current_status"] == "active"
            assert "挂失" in g1["question"]

            r2 = await graph.ainvoke(Command(resume="888888"), cfg)
            g2 = _interrupt_payload(r2)  # 第二道闸:必须明示不可逆
            assert g2["type"] == "confirm_card"
            assert "不可逆" in g2["question"]

            r3 = await graph.ainvoke(Command(resume="取消"), cfg)
            assert "没" in _ai_text(r3)
            assert _card_row(db, 1)["status"] == "active"  # 没挂失
            assert "set_card_status" not in _audited_tools(db)

    run(scenario_cancel())

    # B) 两道都明确 yes:才真正挂失
    async def scenario_confirm():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "card"}',
            json.dumps({"action": "status", "card_id": None, "card_hint": None,
                        "card_type": None, "daily_limit_yuan": None,
                        "per_tx_limit_yuan": None,
                        "status_target": "lost"}, ensure_ascii=False),
            "挂失已完成。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "k3"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("帮我把银行卡挂失了")]}, cfg)
            assert _interrupt_payload(r1)["type"] == "confirm_card"
            r2 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert _interrupt_payload(r2)["type"] == "confirm_card"
            r3 = await graph.ainvoke(Command(resume="确认"), cfg)
            assert not r3.get("__interrupt__")  # 双闸通过,不再暂停
            assert "挂失" in _ai_text(r3)
            assert _card_row(db, 1)["status"] == "lost"
            snap = await graph.aget_state(cfg)
            calls = _traced(snap.values, "set_card_status")
            assert calls and calls[0]["args"] == {"card_id": 1, "status": "lost"}
            assert "set_card_status" in _audited_tools(db)

    run(scenario_confirm())


# ------------------------------------------------- 评审修复(2026-10-03):理财三缺口
# 1) compare_products 传参名错(ids vs product_ids)→ 静默空对比却照播"已生成";
# 2) 申购 external_ref 带秒级 ts → 同指令重放会二次扣款;
# 3) 风险闸门写成 `if profile and ...` → 无测评用户(新注册)可裸购 R5。

_W_QUERY_JSON = json.dumps(
    {"action": "query", "keyword": None, "product_id": None, "amount_yuan": None,
     "holding_id": None, "p_type": None, "assess_answer": None}, ensure_ascii=False)


def _w_subscribe_json(keyword: str, amount: str) -> str:
    return json.dumps(
        {"action": "subscribe", "keyword": keyword, "product_id": None,
         "amount_yuan": amount, "holding_id": None, "p_type": None,
         "assess_answer": None}, ensure_ascii=False)


def test_wealth_compare_passes_product_ids(tmp_path):
    """对比理财产品:compare_products 必须收到 product_ids 且返回真实产品。

    这是"对比理财产品"原本坏掉却查不出来的根因——传的是 {"ids": ...},pydantic
    直接报 missing/unexpected,异常被兜成 {"error": ...} 又被静默吞成空列表,
    于是照样播报"对比结果已生成"。本用例把参数名钉死,顺带断言结果非空。
    """
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "wealth"}',
            _W_QUERY_JSON,
            "两款产品的对比结果已生成。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "w4"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("帮我对比一下理财产品")]}, cfg)
            assert not r1.get("__interrupt__")  # 只读场景无闸门
            snap = await graph.aget_state(cfg)
            calls = _traced(snap.values, "compare_products")
            assert calls, "对比意图必须调用 compare_products"
            assert "product_ids" in calls[0]["args"], calls[0]["args"]
            assert "ids" not in calls[0]["args"], "参数名必须叫 product_ids"
            assert calls[0]["ok"] is True, calls[0]["error"]

            notice = snap.values.get("notice") or {}
            assert notice.get("kind") == "wealth_compare"
            products = notice.get("products") or []
            assert len(products) >= 2, f"对比结果不能为空: {notice}"
            assert {p.get("name") for p in products} == {"余额+货币基金", "稳健纯债基金"}
            assert _balance(db) == DEFAULT_BALANCE  # 只读零动钱
            assert _count(db, "transfer_orders") == 0

    run(scenario())


def test_wealth_compare_failure_is_visible(tmp_path, monkeypatch):
    """对比工具失败时必须走可见的 bank_error,不再降级成"空对比 + 照播成功"。"""
    from agent.bank import BankTools

    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)
    real_call = BankTools.call

    async def flaky(self, name, /, **args):
        if name == "compare_products":
            return {"error": "2 validation errors for call[compare_products]"}
        return await real_call(self, name, **args)

    monkeypatch.setattr(BankTools, "call", flaky)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "wealth"}',
            _W_QUERY_JSON,
            "产品对比没能取到数据。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "w5"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("帮我对比一下理财产品")]}, cfg)
            assert not r1.get("__interrupt__")
            snap = await graph.aget_state(cfg)
            notice = snap.values.get("notice") or {}
            assert notice.get("kind") == "bank_error", notice
            assert notice.get("where") == "compare_products"
            assert "validation errors" in str(notice.get("error"))
            # 轨迹里如实记下失败(ok=False),不是"成功但空"
            calls = _traced(snap.values, "compare_products")
            assert calls and calls[0]["ok"] is False
            assert _balance(db) == DEFAULT_BALANCE

    run(scenario())


def test_wealth_subscribe_blocked_without_assessment(tmp_path):
    """无测评用户申购 R2:bank 侧适当性闸门直接拒——不进闸门、零扣款、
    notice 定性为 wealth_need_assess(不是泛化 bank_error)。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("DELETE FROM risk_profiles WHERE user_id=1")
        conn.commit()
    finally:
        conn.close()

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "wealth"}',
            _w_subscribe_json("稳健纯债基金", "5000"),
            "你还没有风险测评记录。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "w6"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("我要申购5000元的稳健纯债基金")]}, cfg)
            # 关键:建单阶段就被拒,用户看不到确认卡(不会误以为"只差一步确认")
            assert not r1.get("__interrupt__"), "被适当性拒绝的申购不该弹确认卡"
            snap = await graph.aget_state(cfg)
            notice = snap.values.get("notice") or {}
            assert notice.get("kind") == "wealth_need_assess", notice
            assert "风险测评" in str(notice.get("error"))
            tools_traced = {c["tool"] for c in snap.values.get("bank_calls", [])}
            assert "confirm_transfer_order" not in tools_traced
            assert _balance(db) == DEFAULT_BALANCE        # 零动钱
            assert len(_holding_rows(db)) == 1            # 只有播种那一笔

    run(scenario())


def test_wealth_subscribe_replay_not_double_charged(tmp_path):
    """同一会话原样重发同一条申购指令:闸门照走,但 bank 侧幂等命中 → 不二次扣款。

    修复前 external_ref 带秒级时间戳,UNIQUE 约束形同虚设,重放会再扣一次。
    """
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)
    text = "我要申购1000元的余额+货币基金"
    sub_json = _w_subscribe_json("余额+货币基金", "1000")

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            # 第 1 轮:路由 → 抽取 → 播报
            '{"intent": "wealth"}', sub_json, "申购已完成。",
            # 第 2 轮(原样重放):路由 → 抽取 → 播报
            '{"intent": "wealth"}', sub_json, "这笔申购此前已经执行过了。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "w7"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage(text)]}, cfg)
            key1 = _interrupt_payload(r1)["order"]
            assert "idempotency_key" not in key1, "内部幂等键不得泄漏进前端卡片载荷"
            r2 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert "申购" in _ai_text(r2)
            after_first = _balance(db)
            assert after_first == DEFAULT_BALANCE - 100_000  # 恰好扣 1000 元
            holdings_after_first = len(_holding_rows(db))

            # 原样重放同一条指令(同 thread + 同台词 → 同幂等键)
            r3 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage(text)]}, cfg)
            assert _interrupt_payload(r3)["type"] == "confirm_wealth"
            r4 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert not r4.get("__interrupt__")
            assert _balance(db) == after_first, "同指令重放不得二次扣款"
            assert len(_holding_rows(db)) == holdings_after_first
            snap = await graph.aget_state(cfg)
            assert (snap.values.get("notice") or {}).get("kind") == "wealth_duplicate"
            subs = _traced(snap.values, "subscribe_product")
            keys = {c["args"].get("idempotency_key") for c in subs}
            assert len(keys) == 1 and None not in keys, "两次必须是同一个幂等键"

    run(scenario())


def test_wealth_idempotency_key_changes_with_wording(tmp_path):
    """用户换一种说法再买一笔 → 指令原文不同 → 新键,必须真的能买成。"""
    from agent.graph import _wealth_idempotency_key

    base = dict(kind="sub", thread_id="t", target_id=1, amount_cents=100_000,
                from_account_id=1)
    assert (_wealth_idempotency_key(turn_text="买1000元货基", **base)
            == _wealth_idempotency_key(turn_text="买1000元货基", **base))
    assert (_wealth_idempotency_key(turn_text="买1000元货基", **base)
            != _wealth_idempotency_key(turn_text="再买1000元货基", **base))
    assert _wealth_idempotency_key(
        turn_text="买1000元货基", **base).startswith("agent-wealth-")


# ---------------------------------------------------------------- 订阅/代扣(s_ 管线)

_SUB_SEED = [
    # (id, merchant, category, amount_cents, period_days, next_charge, status)
    (1, "腾讯视频VIP", "订阅", 3000, 31, "2026-10-09", "active"),
    (2, "Keep会员", "订阅", 1900, 31, "2026-10-06", "active"),
    (3, "中国移动", "通讯", 12800, 31, "2026-10-04", "active"),
    (4, "旧会员", "订阅", 990, 31, "2026-09-01", "cancelled"),
]


def _seed_subscriptions(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executemany(
        """INSERT INTO subscriptions
           (id, user_id, merchant_name, category, amount_cents, period_days,
            next_charge_date, status, detected_at)
           VALUES (?,1,?,?,?,?,?,?,?)""",
        [(i, m, c, a, p, n, s, "2026-09-01T00:00:00")
         for (i, m, c, a, p, n, s) in _SUB_SEED])
    conn.commit()
    conn.close()


def test_subscription_list_shows_card_view(tmp_path):
    """查订阅:只读无闸门,发列表卡视图(active 3 项,已取消不出现),含月/年合计。"""
    db = _mk_db(tmp_path)
    _seed_subscriptions(db)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "subscription"}',
            json.dumps({"action": "list", "merchant": None}, ensure_ascii=False),
            "订阅清单已列出。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "s1"}}
            r = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("帮我看看订阅都花多少钱")]},
                cfg)
            assert not r.get("__interrupt__")  # 只读场景不停闸门
            view = r["sub_view"]
            names = [i["merchant_name"] for i in view["items"]]
            assert names == ["中国移动", "Keep会员", "腾讯视频VIP"]  # 按扣费日排序
            assert all(i["period_text"] == "月" for i in view["items"])
            tencent = next(i for i in view["items"]
                           if i["merchant_name"] == "腾讯视频VIP")
            assert tencent["amount_yuan"] == "30.00"
            assert tencent["annual_yuan"] == "353.23"  # 365/31*3000
            # 年合计 = 各项年化之和;月合计 = 年/12
            annual_cents = sum(round(365 / 31 * a)
                               for a in (3000, 1900, 12800))
            assert view["annual_total_yuan"] == f"{annual_cents / 100:.2f}"
            assert view["monthly_total_yuan"] == f"{round(annual_cents / 12) / 100:.2f}"

    run(scenario())


def test_subscription_cancel_gate_then_execute(tmp_path):
    """取消代扣:定位商户→支付密码闸门→取消落库;错密码不改状态。"""
    db = _mk_db(tmp_path)
    _seed_subscriptions(db)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "subscription"}',
            json.dumps({"action": "cancel", "merchant": "腾讯视频"}, ensure_ascii=False),
            "已取消。",
            '{"intent": "subscription"}',
            json.dumps({"action": "cancel", "merchant": "腾讯视频"}, ensure_ascii=False),
            "没有找到该商户的自动扣费。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "s2"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("取消腾讯视频的自动扣费")]},
                cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_sub_cancel"
            assert payload["pay_required"] is True
            sub = payload["sub"]
            assert sub["merchant_name"] == "腾讯视频VIP"   # 模糊匹配命中全名
            assert sub["amount_yuan"] == "30.00"
            assert sub["annual_yuan"] == "353.23"
            assert sub["sub_id"] == 1

            # 错密码:闸门自环重问,不取消
            r2 = await graph.ainvoke(Command(resume="000000"), cfg)
            p2 = _interrupt_payload(r2)
            assert p2["type"] == "confirm_sub_cancel"
            assert _sub_status(db, 1) == "active"

            # 对密码:取消落库 + 播报
            r3 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert "取消" in _ai_text(r3)
            assert _sub_status(db, 1) == "cancelled"
            # 已取消的不再被匹配:再取消同名 → not_found 收尾
            r4 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("再取消腾讯视频的自动扣费")]}, cfg)
            assert not r4.get("__interrupt__")
            assert "没有找到" in _ai_text(r4)

    run(scenario())


def _sub_status(db: Path, sub_id: int) -> str:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT status FROM subscriptions WHERE id=?", (sub_id,)
        ).fetchone()[0]
    finally:
        conn.close()
