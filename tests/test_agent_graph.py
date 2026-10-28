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

from agent import memory
from agent import graph as graph_mod
from agent.bank import load_bank_tools
from agent.graph import ROUTER_SYS, build_agent_graph, make_sqlite_checkpointer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BALANCE = 1_000_000  # 1 万元(整数分)


# ----------------------------------------------------------------- 基础设施

def _mk_db(tmp_path: Path, balance_cents: int = DEFAULT_BALANCE,
           name: str = "bank.db") -> Path:
    """建临时银行库:1 用户 + 1 checking 账户 + 3 联系人(林悦同名 ×2)。

    进程内直接 `init_db(path)` 写库,不触碰默认 data/bank.db。

    历史写法是用 `subprocess.run([python, "-c", ...])` 起子进程建库,
    理由是"bank_core 导入时读 BANK_CORE_DB 环境变量"——该理由已失效:
    `bank_core/db.py::default_db_path()` 改成**调用点延迟解析**,
    且 `init_db()` 本就收显式路径参数,与进程环境无关。
    每次子进程建库固定花掉约 2.5s(解释器 + import fastmcp/bank_core),
    本文件 30 处调用累计约 75s,是本文件耗时第二名。改进程内后降为毫秒级。
    """
    from bank_core.db import init_db

    db = tmp_path / name
    c = init_db(db)
    c.execute("INSERT INTO users (id,name,phone,created_at) "
              "VALUES (1,'测试用户','13800000000','2026-01-01T00:00:00')")
    c.execute("INSERT INTO accounts (id,user_id,type,name,balance_cents,opened_at) "
              "VALUES (1,1,'checking','工资卡',?,'2026-01-01T00:00:00')",
              (balance_cents,))
    c.execute("INSERT INTO contacts (id,user_id,name,phone,relation,note) "
              "VALUES (1,1,'林悦','13900008821','spouse','招商银行 尾号8821')")
    c.execute("INSERT INTO contacts (id,user_id,name,phone,relation,note) "
              "VALUES (2,1,'林悦','13711112222','friend','建设银行 尾号2222')")
    c.execute("INSERT INTO contacts (id,user_id,name,phone,relation,note) "
              "VALUES (3,1,'张三','13633334444','colleague','')")
    # AA 收款对象必须是注册用户(2026-10-28 产品口径):预置两个可解析的注册
    # 用户,id 用 91/92 避开 _mk_multi_user_db 显式占用的 id=2(李四)
    c.executemany(
        "INSERT INTO users (id,name,phone,created_at) VALUES (?,?,?,?)",
        [(91, "老王", "13600001234", "2026-01-01T00:00:00"),
         (92, "小刘", "13700005678", "2026-01-01T00:00:00")])
    c.commit()
    c.close()
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


class _RecordingLLM(FakeMessagesListChatModel):
    """捕获每次调用收到的完整消息列表(断言抽取上下文携带【今天】锚点)。

    基类是 pydantic 模型,属性必须声明为字段(_CountingLLM.calls 同理)。"""

    calls: list = []

    def __init__(self, responses: list[str]):
        super().__init__(responses=[AIMessage(content=r) for r in responses])

    async def astream(self, input, **kwargs):  # noqa: A002 —— 与基类形参同名
        self.calls.append(list(input))
        async for chunk in super().astream(input, **kwargs):
            yield chunk


def _calls_with_sys(llm: _RecordingLLM, sys_prefix: str) -> list[list]:
    """筛出系统提示以指定前缀开头的那些调用(一条调用 = 一组消息)。"""
    return [msgs for msgs in llm.calls
            if msgs and str(getattr(msgs[0], "content", "")).startswith(sys_prefix)]


def _slots_json(**kv) -> str:
    return json.dumps(kv, ensure_ascii=False)


class _AgentSession:
    """测试脚手架:装载工具 + 建检查点器 + 编译图;退出时确保关闭连接。

    checkpointer 的 aiosqlite 连接绑定创建它的事件循环,断言失败也必须关掉,
    否则残留的工作线程可能让 pytest 进程退不出去(Windows 实测)。
    """

    def __init__(self, db: Path, tmp_path: Path, responses: list[str],
                 llm=None, user_id: int = 1):
        self._db, self._tmp, self._resp = db, tmp_path, responses
        self._llm = llm
        self._user_id = user_id
        self.graph = None
        self._conn = None

    async def __aenter__(self):
        tools = await load_bank_tools(self._db, user_id=self._user_id)
        saver, self._conn = await make_sqlite_checkpointer(
            self._tmp / "agent_ckpt.sqlite")
        self.graph = build_agent_graph(
            self._llm if self._llm is not None else _fake_llm(*self._resp),
            tools, saver)
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
            # 办结追问:模型漏问时由确定性兜底补上
            assert "还需要给其他人转账吗?" in _ai_text(r2)
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

def test_transfer_four_slot_clarify_not_cut_off(tmp_path):
    """转账逐项补槽不应被反问封顶误杀(2026-10-27 同类问题排查)。

    收款人 → 金额 → 时机 → 定时时刻 是四个**不同**的追问;旧实现共用同一个
    clarify_round 3 次额度,第 4 问会被 clarify_gaveup 直接截断。现在缺项集合
    一变就清零,四个问题都能问到,最终正常进闸门。
    """
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(),                                  # 1) 问收款人
            _slots_json(payee="张三"),                      # 2) 问金额
            _slots_json(payee="张三", amount_yuan="500"),   # 3) 问时机
            _slots_json(payee="张三", amount_yuan="500",
                        when="scheduled"),                  # 4) 问定时时刻
            _slots_json(payee="张三", amount_yuan="500",
                        when="scheduled",
                        scheduled_at="2026-11-01T09:00"),   # 齐 → 闸门
            "定时转账单已创建。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t-4slot"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("我要转账")]}, cfg)
            p1 = _interrupt_payload(r1)
            assert "payee" in p1["missing"]
            r2 = await graph.ainvoke(Command(resume="张三"), cfg)
            p2 = _interrupt_payload(r2)
            assert "amount_yuan" in p2["missing"]
            r3 = await graph.ainvoke(Command(resume="500"), cfg)
            p3 = _interrupt_payload(r3)
            assert "when" in p3["missing"] and p3["options"] == ["立即转", "定时转"]
            r4 = await graph.ainvoke(Command(resume="定时转"), cfg)
            # 第 4 问:旧实现到这里已经是 clarify_gaveup
            p4 = _interrupt_payload(r4)
            assert p4["type"] == "ask_slot"
            assert p4["missing"] == ["scheduled_at"]
            assert not p4.get("notice")
            # 补齐时刻 → 正常进确认闸门(定时单,不动钱)
            r5 = await graph.ainvoke(Command(resume="2026-11-01 09:00"), cfg)
            p5 = _interrupt_payload(r5)
            assert p5["type"] == "confirm_transfer"
            assert p5["order"]["scheduled_at"]
            assert _balance(db) == DEFAULT_BALANCE  # 确认前不动钱

    run(scenario())


def test_clarify_reset_semantics():
    """_clarify_reset:同一问句才累计;缺项集合变化/有新信息即清零(纯函数)。"""
    from agent.graph import _clarify_reset as r
    # 基准是"上一轮问过什么"(clarify_last),不是会被 clarify 清空的 missing
    assert r({"clarify_last": ["when"]}, "m", ["when"]) == {}
    assert r({"clarify_last": ["when"]}, "m", ["scheduled_at"]) ==         {"clarify_round": 0}
    assert r({"clarify_last": ["a", "b"]}, "m", ["a"]) == {"clarify_round": 0}
    assert r({"clarify_last": ["a"]}, "m", ["a"], progressed=True) ==         {"clarify_round": 0}
    assert r({}, "m", ["a"]) == {"clarify_round": 0}  # 首轮不累计


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


def test_merge_split_participants_upserts_by_name():
    """纯函数:澄清轮按姓名 upsert(2026-10-28 复盘——"我自己也要出100"在
    整字段覆盖下既没进新人也没冲掉旧人,纠偏文案原样复读)。"""
    old = [{"name": "老王", "share_yuan": "100", "phone": "13600001234"},
           {"name": "小刘", "share_yuan": "100"},
           {"name": "丁博文", "share_yuan": "100"}]
    # 补充增量:老名单保留,"我自己"置换成真名后追加
    merged = graph_mod.merge_split_participants(
        old, [{"name": "我自己", "share_yuan": "100"}], self_name="陈明")
    assert [p["name"] for p in merged] == ["老王", "小刘", "丁博文", "陈明"]
    assert merged[0]["phone"] == "13600001234"  # 未提到的人字段原样沿用
    # 同名更新:老王改口出 50,其余不动
    merged2 = graph_mod.merge_split_participants(
        old, [{"name": "老王", "share_yuan": "50"}])
    assert merged2[0]["share_yuan"] == "50" and len(merged2) == 3
    # 全量剔除:"丁博文不参加"(全是老名单的人且确实少了 → 以 parsed 为准)
    merged3 = graph_mod.merge_split_participants(
        old, [{"name": "老王", "share_yuan": "200"},
              {"name": "小刘", "share_yuan": "200"}])
    assert [p["name"] for p in merged3] == ["老王", "小刘"]
    # 单人剔除不收缩(parsed 只有一人属最常见补充,绝不触发剔除通道)
    assert len(graph_mod.merge_split_participants(
        old, [{"name": "老王", "share_yuan": "400"}])) == 3
    # 模型没抽出人 → 沿用旧名单;无真名时"我"保留字面
    assert graph_mod.merge_split_participants(old, None) == old
    assert [p["name"] for p in graph_mod.merge_split_participants(
        [], [{"name": "我", "share_yuan": "100"}])] == ["我"]
    # 去重:模型既输出"我"又输出真名,按同一人合并(后者份额覆盖)
    dedup = graph_mod.merge_split_participants(
        [], [{"name": "我", "share_yuan": "100"},
             {"name": "陈明", "share_yuan": "120"}], self_name="陈明")
    assert [p["name"] for p in dedup] == ["陈明"]
    assert dedup[0]["share_yuan"] == "120"


def test_sb_state_question_echoes_shares_and_diff():
    """shares 纠偏问句回显当前分摊明细与差额(替代静态复读);信息不全回落。"""
    st = {"split": {"total_yuan": "400", "participants": [
        {"name": "老王", "share_yuan": "100"}, {"name": "小刘", "share_yuan": "100"},
        {"name": "丁博文", "share_yuan": "100"}]}}
    q = graph_mod._sb_state_question(st, ["shares"])
    assert "老王 100.00元" in q and "还差 100.00 元" in q and "均摊" in q
    # 非 shares 缺项 / 槽位还没到能回显的程度 → 空串,回落 _sb_slot_question
    assert graph_mod._sb_state_question(st, ["title"]) == ""
    assert graph_mod._sb_state_question({}, ["shares"]) == ""
    bad = {"split": {"total_yuan": "400", "participants": [{"name": "老王"}]}}
    assert graph_mod._sb_state_question(bad, ["shares"]) == ""


def test_split_bill_clarify_echoes_then_merges_self_share(tmp_path):
    """2026-10-28 手机端实测复盘:首抽漏了发起人本人("和我一起…他们仨各出
    100"只抽到 3 人,合计 300≠400)→ 纠偏问句回显差额;用户补"我自己也要
    出100"要并进名单直达闸门,而不是原样复读同一句纠偏。"""
    db = _mk_db(tmp_path)
    memory.merge(1, "profile", {"name": "陈明"})

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "split_bill"}',
            # 首抽(复刻实测:只抽到点名的三人,发起人本人没进名单)
            json.dumps({"title": "AA群收款", "total_yuan": "400", "even": False,
                        "participants": [
                            {"name": "老王", "phone": "13600001234",
                             "share_yuan": "100"},
                            {"name": "小刘", "share_yuan": "100"},
                            {"name": "丁博文", "share_yuan": "100"}]},
                       ensure_ascii=False),
            # 澄清轮(最坏情况:模型只回增量,确定性合并兜底)
            json.dumps({"participants": [{"name": "我自己", "share_yuan": "100"}]},
                       ensure_ascii=False),
            "AA 收款已发起。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t9"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage(
                     "老王,小刘和丁博文需要和我一起aa掉400块钱,"
                     "他们仨个人各出100,发起群收款")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "ask_slot"
            # 反问回显当前分摊与差额,不再让用户盲猜
            assert "老王" in payload["question"] and "还差" in payload["question"]
            r2 = await graph.ainvoke(
                Command(resume="因为我自己也要出100呀"), cfg)
            gate = _interrupt_payload(r2)
            assert gate["type"] == "confirm_split"
            parts = {p["name"]: p["share_yuan"]
                     for p in gate["bill"]["participants"]}
            assert parts == {"老王": "100.00", "小刘": "100.00",
                             "丁博文": "100.00", "陈明": "100.00"}

    run(scenario())


def test_split_bill_unregistered_participant_rejected(tmp_path):
    """2026-10-28 产品口径:指定的联系人还没在平台创建账号 → 点名提示、
    整单不予发起(此前未注册参与人静默降级为纯记账,发起人根本不知道
    对方永远收不到交款单)。"""
    db = _mk_db(tmp_path)  # 老王/小刘 已注册;王五 没有

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "split_bill"}',
            json.dumps({"title": "聚餐", "total_yuan": "300", "even": True,
                        "participants": [{"name": "我"}, {"name": "老王"},
                                         {"name": "王五"}]},
                       ensure_ascii=False),
            "王五还没有在平台创建账号,本次未发起群收款。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t-unreg"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(
                "我和老王、王五 aa 300 块,发起群收款")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_split"  # 闸门照常走(还没碰账)
            r2 = await graph.ainvoke(Command(resume="确认"), cfg)
            notice = r2.get("notice") or {}
            assert notice.get("kind") == "bank_error"
            assert "王五" in str(notice.get("error", ""))
            assert "还没有在平台创建账号" in str(notice.get("error", ""))
            # 整单不予发起:账单与分摊项零落库
            assert _count(db, "split_bills") == 0
            assert _count(db, "split_bill_items") == 0
            assert _count(db, "payment_requests") == 0

    run(scenario())


def test_split_bill_inquiry_carries_dispatch_reasons(tmp_path):
    """2026-10-28 实测复盘:发起 AA(含发起人本人)后追问"陈明未发起的原因
    是?",chat 必须拿到【实时AA账单】事实块(逐人投递结果与原因),讲清
    "发起人本人无需交款单",而不是拒答"查不到具体原因"。"""
    db = _mk_db(tmp_path)

    async def scenario():
        llm = _RecordingLLM([
            '{"intent": "split_bill"}',
            json.dumps({"title": "火锅", "total_yuan": "400", "even": True,
                        "participants": [{"name": "我"}, {"name": "老王"},
                                         {"name": "小刘"}]},
                       ensure_ascii=False),
            "AA 已发起,老王小刘的交款单已发送。",
            '{"intent": "chat"}',
            "陈明是发起人本人,交款单不会发给自己;老王和小刘的已发送。",
        ])
        async with _AgentSession(db, tmp_path, ["-"], llm=llm) as graph:
            cfg = {"configurable": {"thread_id": "aa-why"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(
                "发起AA,火锅400,我、老王、小刘均摊")]}, cfg)
            assert _interrupt_payload(r1)["type"] == "confirm_split"
            await graph.ainvoke(Command(resume="确认"), cfg)
            await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(
                "陈明未发起的原因是?")]}, cfg)
        calls = _calls_with_sys(llm, str(graph_mod.CHAT_SYS)[:24])
        assert calls, "追问未被送进 chat"
        extra = str(calls[-1][-1].content)
        assert "【实时AA账单】" in extra
        assert "发起人本人" in extra          # 原因讲清了
        assert "老王" in extra                # 逐人明细在场

    run(scenario())


# ----------------------------------------------------------------- AA 交款单(收件人视角)

def _mk_multi_user_db(tmp_path: Path) -> Path:
    """在标准夹具上追加用户 2(李四,可登录),用于跨用户交款单剧本。"""
    db = _mk_db(tmp_path)
    from bank_core.auth_core import hash_password
    c = sqlite3.connect(db)
    c.execute("INSERT INTO users (id,name,phone,created_at) "
              "VALUES (2,'李四','13900001111','2026-01-01T00:00:00')")
    c.execute("INSERT INTO accounts (id,user_id,type,name,balance_cents,opened_at) "
              "VALUES (2,2,'checking','工资卡',?,'2026-01-01T00:00:00')",
              (DEFAULT_BALANCE,))
    c.execute("INSERT INTO contacts (id,user_id,name,phone,relation,note) "
              "VALUES (4,1,'李四','13900001111','friend','')")
    c.execute(
        "INSERT INTO auth_users (user_id, identifier, identifier_type, "
        "password_hash, pay_hash, created_at) VALUES "
        "(2,'13900001111','phone',?,?,'2026-01-01T00:00:00')",
        (hash_password("Demo@12345"), hash_password("888888")))
    c.commit()
    c.close()
    return db


def _payment_requests(db: Path) -> list[dict]:
    conn = _ro(db)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM payment_requests ORDER BY id").fetchall()]
    finally:
        conn.close()


def _dispatch_bill(db: Path, title: str, total_cents: int,
                   participants: list[dict]) -> int:
    """直接走账本建单+派发(收件人剧本的布景),避开发起人侧的 LLM 闸门。"""
    from bank_core.ledger import LedgerService
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        bill = LedgerService(conn, 1).create_split_bill(title, total_cents, participants)
        LedgerService(conn, 1).send_split_requests(bill["bill_id"])
        return int(bill["bill_id"])
    finally:
        conn.close()


def test_split_bill_dispatch_creates_request_for_registered_user(tmp_path):
    """发起 AA 后,能解析到注册用户的参与人立刻收到交款单(Agent 收件箱)。"""
    db = _mk_multi_user_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "split_bill"}',
            json.dumps({"title": "火锅", "total_yuan": "200", "even": True,
                        "participants": [{"name": "我"}, {"name": "李四"}]},
                       ensure_ascii=False),
            "AA 已发起,已发给李四。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "pr1"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("发起AA,火锅200,我和李四均摊")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_split"
            r2 = await graph.ainvoke(Command(resume="确认"), cfg)
            assert "李四" in _ai_text(r2)

            reqs = _payment_requests(db)
            assert len(reqs) == 1
            assert reqs[0]["to_user_id"] == 2
            assert reqs[0]["from_user_id"] == 1
            assert reqs[0]["amount_cents"] == 10_000
            assert reqs[0]["status"] == "pending"
            assert "send_split_requests" in _audited_tools(db)

    run(scenario())


def test_split_created_inquiry_followup_goes_chat_no_duplicate(tmp_path):
    """建单成功后的疑问性追问(2026-10-28 实测)不得重复走发起流程。

    场景回放:确认建单后用户问「张三老王都会收到你发出的收款账单是吗」,
    路由 LLM 误判回 split_bill,旧实现 sb_extract 拿历史名单再过一遍闸门,
    重复弹确认卡。守卫:已有 bill 且本轮是问句 → 改写 chat;建单即清 split 槽位。
    """
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            # 第 1 轮:发起 → 闸门 → 播报
            '{"intent": "split_bill"}',
            json.dumps({"title": "火锅", "total_yuan": "300", "even": True,
                        "participants": [{"name": "我"}, {"name": "小刘"},
                                         {"name": "老王"}]}, ensure_ascii=False),
            "AA 已发起,三人各 100 元。",
            # 第 2 轮:路由 LLM 照实测误判 split_bill(守卫应改写为 chat)
            '{"intent": "split_bill"}',
            "会的,小刘和老王的 Agent 都会收到交款单。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "dup1"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("发起AA,火锅300,我、小刘、老王均摊")]}, cfg)
            assert _interrupt_payload(r1)["type"] == "confirm_split"
            r2 = await graph.ainvoke(Command(resume="确认"), cfg)
            assert "AA" in _ai_text(r2)

            # 建单即终态:split 槽位被清空(防下一轮带旧名单重走闸门)
            snap = await graph.aget_state(cfg)
            assert not (snap.values.get("split") or {}).get("participants")

            # 追问轮:不再弹确认卡,落在 chat 答疑;库里仍然只有一张单
            r3 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("小刘老王都会收到你发出的收款账单是吗")]}, cfg)
            assert not r3.get("__interrupt__"), "追问被再次拉进发起流程,重复弹确认卡"
            snap3 = await graph.aget_state(cfg)
            assert snap3.values.get("intent") == "chat"
            assert "收到" in _ai_text(r3)
            conn = _ro(db)
            try:
                n = conn.execute("SELECT COUNT(*) FROM split_bills").fetchone()[0]
            finally:
                conn.close()
            assert n == 1
            assert _audited_tools(db).count("create_split_bill") == 1

    run(scenario())


def test_split_statement_after_create_still_starts_new_bill(tmp_path):
    """守卫不误伤陈述句:建单后「再来一单…」没有问句记号,照常走新一轮发起。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "split_bill"}',
            json.dumps({"title": "火锅", "total_yuan": "300", "even": True,
                        "participants": [{"name": "我"}, {"name": "小刘"},
                                         {"name": "老王"}]}, ensure_ascii=False),
            "AA 已发起。",
            '{"intent": "split_bill"}',
            json.dumps({"title": "下午茶", "total_yuan": "90", "even": True,
                        "participants": [{"name": "我"}, {"name": "小刘"},
                                         {"name": "老王"}]}, ensure_ascii=False),
            "AA 已发起。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "dup2"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("发起AA,火锅300,三人均摊")]}, cfg)
            assert _interrupt_payload(r1)["type"] == "confirm_split"
            await graph.ainvoke(Command(resume="确认"), cfg)
            # 陈述句新单:应该再次出现确认闸门(这是合法的第二单)
            r3 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("再发一单下午茶90元,还是三人均摊")]}, cfg)
            assert _interrupt_payload(r3)["type"] == "confirm_split"
            await graph.ainvoke(Command(resume="确认"), cfg)
            conn = _ro(db)
            try:
                n = conn.execute("SELECT COUNT(*) FROM split_bills").fetchone()[0]
            finally:
                conn.close()
            assert n == 2

    run(scenario())


def test_is_inquiry_followup_pure_function():
    """问句记号判定:追问 True,陈述句/空串 False。"""
    assert graph_mod._is_inquiry_followup("张三老王都会收到收款账单是吗")
    assert graph_mod._is_inquiry_followup("会自动扣款?")
    assert graph_mod._is_inquiry_followup("他们是不是已经收到了")
    assert not graph_mod._is_inquiry_followup("再发一单下午茶90元,三人均摊")
    assert not graph_mod._is_inquiry_followup("")


def test_payment_request_list_and_pay_flow(tmp_path):
    """收件人 Agent:查看待付交款单 → 确认支付(支付密码闸门)→ 账单推进。"""
    db = _mk_multi_user_db(tmp_path)
    bill_id = _dispatch_bill(db, "火锅", 20_000, [
        {"name": "我", "share_cents": 10_000},
        {"name": "李四", "share_cents": 10_000},
    ])

    async def scenario():
        # 1) 查看:只读,不弹闸门
        async with _AgentSession(db, tmp_path, [
            '{"intent": "split_payment"}',
            json.dumps({"action": "list"}, ensure_ascii=False),
            "你有 1 张待付交款单:发起人的火锅 100 元。",
        ], user_id=2) as graph:
            cfg = {"configurable": {"thread_id": "pr-list"}}
            r = await graph.ainvoke(
                {"auth_user_id": 2,
                 "messages": [HumanMessage("我有没有交款单")]}, cfg)
            assert "__interrupt__" not in r
            assert "交款单" in _ai_text(r)
            snap = await graph.aget_state(cfg)
            assert [c["tool"] for c in snap.values.get("bank_calls", [])] == [
                "list_payment_requests"]

        # 2) 支付:闸门复述金额 → 支付密码确认 → 账本置已付
        (tmp_path / "pay").mkdir()
        async with _AgentSession(db, tmp_path / "pay", [
            '{"intent": "split_payment"}',
            json.dumps({"action": "pay", "from_name": "测试用户",
                        "title": "火锅"}, ensure_ascii=False),
            "已支付火锅交款单 100 元。",
        ], user_id=2) as graph:
            cfg = {"configurable": {"thread_id": "pr-pay"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 2,
                 "messages": [HumanMessage("把火锅那单交款单付掉")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_pay_request"
            assert payload["pay_required"] is True
            assert payload["request"]["amount_yuan"] == "100.00"
            assert payload["request"]["from_name"] == "测试用户"
            # 闸门前:分文未动
            assert _payment_requests(db)[0]["status"] == "pending"

            r2 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert "已支付" in _ai_text(r2)
            req = _payment_requests(db)[0]
            assert req["status"] == "paid" and req["paid_at"]
            bill = _split_bill(db, bill_id)
            assert bill["status"] == "collecting"
            assert [i["paid"] for i in bill["items"]] == [0, 1]  # 李四是第 2 项
            assert "pay_payment_request" in _audited_tools(db)

    run(scenario())


def test_payment_request_multiple_pending_clarify_then_pay(tmp_path):
    """多张待付:先反问列候选,用户指认后再走支付闸门。"""
    db = _mk_multi_user_db(tmp_path)
    _dispatch_bill(db, "火锅", 20_000, [
        {"name": "李四", "share_cents": 10_000}, {"name": "我", "share_cents": 10_000}])
    _dispatch_bill(db, "奶茶", 20_000, [
        {"name": "李四", "share_cents": 10_000}, {"name": "我", "share_cents": 10_000}])

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "split_payment"}',
            json.dumps({"action": "pay"}, ensure_ascii=False),      # 未指认
            json.dumps({"action": "pay", "title": "奶茶"}, ensure_ascii=False),
            "已支付奶茶交款单 100 元。",
        ], user_id=2) as graph:
            cfg = {"configurable": {"thread_id": "pr-multi"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 2,
                 "messages": [HumanMessage("把我的交款单付掉")]}, cfg)
            ask = _interrupt_payload(r1)
            assert ask["type"] == "ask_slot"
            assert "火锅" in ask["question"] and "奶茶" in ask["question"]

            r2 = await graph.ainvoke(Command(resume="奶茶那单"), cfg)
            gate = _interrupt_payload(r2)
            assert gate["type"] == "confirm_pay_request"
            assert gate["request"]["title"] == "奶茶"

            r3 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert "已支付" in _ai_text(r3)
            paid = [r for r in _payment_requests(db) if r["status"] == "paid"]
            assert len(paid) == 1 and paid[0]["title"] == "奶茶"

    run(scenario())


def test_pick_payment_request_matching_rules():
    """纯函数:单张直选/id/发起人/账单名/序号/歧义。"""
    reqs = [
        {"request_id": 1, "from_name": "张三", "title": "火锅"},
        {"request_id": 2, "from_name": "王五", "title": "奶茶"},
    ]
    assert graph_mod.pick_payment_request(reqs[:1], {"action": "pay"}, "")[0]["request_id"] == 1
    assert graph_mod.pick_payment_request(
        reqs, {"request_id": 2}, "")[0]["request_id"] == 2
    assert graph_mod.pick_payment_request(
        reqs, {"title": "火锅"}, "")[0]["request_id"] == 1
    assert graph_mod.pick_payment_request(
        reqs, {"action": "pay"}, "王五那单")[0]["request_id"] == 2
    assert graph_mod.pick_payment_request(
        reqs, {"action": "pay"}, "第二张")[0]["request_id"] == 2
    chosen, candidates = graph_mod.pick_payment_request(
        reqs, {"action": "pay"}, "随便")
    assert chosen is None and len(candidates) == 2


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


def test_router_prompt_self_intro_goes_chat_not_contact_add():
    """路由提示词守卫:自我介绍必须导向 chat,contact_add 必须要求显式动作词。

    2026-10-24 实测「我叫丁博文」被误判成录入收款人:旧规则在 contact_add
    里裸写「报名字」,LLM 十选一看到人名就往业务桶塞。守卫三条措辞,
    防止回归。
    """
    assert "报名字" not in ROUTER_SYS
    assert "显式动作词" in ROUTER_SYS
    assert "自我介绍" in ROUTER_SYS


# ----------------------------------------------------------------- 长期记忆:自我介绍捕获

def test_self_intro_name_extraction():
    """「我叫XX」抽取纯函数:正例命中,动宾误报(叫车/叫外卖)不收。"""
    ex = graph_mod.extract_self_intro_name
    assert ex("我叫丁博文") == "丁博文"
    assert ex("你好,我叫林悦,想看看这个月账单") == "林悦"
    assert ex("我的名字是阿依古丽·买买提") == "阿依古丽·买买提"
    assert ex("叫我小王就行") == "小王"
    assert ex("我叫Tony") == "Tony"
    assert ex("我叫车就不用了") == ""      # 动宾:叫车
    assert ex("我叫外卖送过来") == ""      # 动宾:叫外卖
    assert ex("你还记得我叫什么名字吗?") == ""  # 疑问句:曾被抓成"什么名字吗"
    assert ex("你知道我叫什么吗") == ""    # 疑问句
    assert ex("我叫丁博文,你知道吗") == "丁博文"  # 正常介绍+疑问尾,首个命中优先
    assert ex("今天天气不错") == ""


def test_profile_ctx_reads_memory():
    """闲聊注入块:profile 有姓名才注入;无记忆返回空串(conftest 隔离记忆库)。"""
    assert graph_mod._profile_ctx({"auth_user_id": 11}) == ""
    memory.remember(11, "profile", {"name": "丁博文"})
    ctx = graph_mod._profile_ctx({"auth_user_id": 11})
    assert "丁博文" in ctx
    assert graph_mod._profile_ctx({"auth_user_id": None}) == ""


def test_router_captures_self_intro_to_profile_memory(tmp_path):
    """说「我叫丁博文」(chat 兜底)后,长期记忆 profile.name 落库。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "chat"}',
            "你好,丁博文!",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "t-self-intro"}}
            await graph.ainvoke(
                {"auth_user_id": 11, "messages": [HumanMessage("我叫丁博文")]},
                cfg)
            assert (memory.recall(11, "profile") or {}).get("name") == "丁博文"

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

    复用 `_mk_db` 的标准装配(用户/账户/联系人/登录态),只额外补一行 savings,
    避免两份脚本各写一套 SQL 造成库结构漂移。
    """
    db = _mk_db(tmp_path, name="bank_linkage.db")
    c = sqlite3.connect(db)
    c.execute("INSERT INTO accounts (id,user_id,type,name,balance_cents,opened_at) "
              "VALUES (2,1,'savings','理财专户',0,'2026-01-01T00:00:00')")
    c.commit()
    c.close()
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
            # 活期余额恰好减少预算;理财专户恰好增加预算——confirm_transfer_order
            # 对内部划转(收款尾号命中自己账户)双侧记账(2026-10-28 起;此前只记
            # 转出侧,预留的钱在账本上凭空消失,理财专户永远 0 余额)。
            assert _balance(db, 1) == DEFAULT_BALANCE - 100_000
            assert _balance(db, 2) == 100_000
            assert _linkage_row(db, plan_id)["status"] == "active"
            tools_called = _audited_tools(db)
            assert "create_linkage_plan" in tools_called
            assert "confirm_transfer_order" in tools_called
            assert "execute_linkage_action" not in tools_called  # 到期前绝不购买

    run(scenario())


def test_is_linkage_cancel_message():
    """撤回/取消已有计划的话术判定:动词+名词同现才认,单说"取消"不认
    (那是闸门否决与反问放弃的领地),到期提醒文本不误分。"""
    assert graph_mod._is_linkage_cancel_message("预留太多了,我要撤回这个单子")
    assert graph_mod._is_linkage_cancel_message("取消联动计划")
    assert graph_mod._is_linkage_cancel_message("把林悦生日计划撤销掉")
    assert not graph_mod._is_linkage_cancel_message("(到期提醒)该订购鲜花了")
    assert not graph_mod._is_linkage_cancel_message("帮我准备生日")
    assert not graph_mod._is_linkage_cancel_message("取消")


def test_linkage_cancel_message_releases_budget(tmp_path):
    """2026-10-28 实测回归:计划执行后说"撤回预留单"必须走 l_cancel 原路
    退回预算,而不是被 l_extract 当成新计划再划一笔(被自己吃满的日限额
    拦死,"撤回失败被风控拦截")。"""
    db = _mk_linkage_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            # 轮 1:建计划 → 闸门 → 支付密码确认执行
            '{"intent": "linkage"}',
            _LINK_ACTIONS_JSON,
            "预算已预留,联动计划生效。",
            # 轮 2:撤回话术(router → l_cancel → l_report 播报;l_cancel 不调 LLM)
            '{"intent": "linkage"}',
            "好的,这笔预留已撤回,1000 元已退回你的活期账户。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "l-cancel"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(
                "帮我准备林悦的生日,预算1000,提前2天订300元的鲜花,提前1天订200元的蛋糕")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_linkage"
            plan_id = payload["plan"]["plan_id"]
            order_id = payload["plan"]["lock_order"]["order_id"]
            await graph.ainvoke(Command(resume="888888"), cfg)
            assert _balance(db, 1) == DEFAULT_BALANCE - 100_000
            assert _balance(db, 2) == 100_000

            # 撤回话术(动词"撤回"+名词"预留/单子")→ l_cancel,plan_id 缺省取最新
            r2 = await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(
                "预留太多了,我要撤回这个单子")]}, cfg)
            notice = r2.get("notice") or {}
            assert notice.get("kind") == "linkage_cancelled"
            released = (notice.get("result") or {}).get("released") or {}
            assert released.get("amount_yuan") == "1000.00"
            assert _linkage_row(db, plan_id)["status"] == "cancelled"
            assert _order(db, order_id)["status"] == "executed"  # 已执行事实不改写
            assert _balance(db, 1) == DEFAULT_BALANCE            # 预算退回活期
            assert _balance(db, 2) == 0                           # 理财专户清空
            tools_called = _audited_tools(db)
            assert "cancel_linkage_plan" in tools_called
            assert "internal_transfer" in tools_called
            # 撤回轮没有再建计划,也没有新的风控预检(= 没把它当新划转)
            assert tools_called.count("create_linkage_plan") == 1
            assert tools_called.count("policy_check") == 2  # 建单+确认各一次

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


# ----------------------------------------------------------------- 年度账单报告(2026-10-26)

def _seed_year_flow(db: Path, year: int) -> None:
    """往年 3 个月各一笔工资/消费(固定金额),供年报汇总与逐月走势断言。"""
    conn = sqlite3.connect(db)
    try:
        rows = [
            (f"{year}-01-10T09:00:00", "in", 800_000, "示例公司", "工资"),
            (f"{year}-01-20T12:00:00", "out", 100_000, "示例超市", "生活"),
            (f"{year}-02-10T09:00:00", "in", 800_000, "示例公司", "工资"),
            (f"{year}-02-20T12:00:00", "out", 200_000, "示例超市", "生活"),
            (f"{year}-03-10T09:00:00", "in", 800_000, "示例公司", "工资"),
            (f"{year}-03-20T12:00:00", "out", 300_000, "示例餐厅", "餐饮"),
        ]
        for ts, direction, cents, cp, cat in rows:
            conn.execute(
                """INSERT INTO transactions
                   (account_id, ts, direction, amount_cents, balance_after_cents,
                    tx_type, counterparty, category, memo)
                   VALUES (1, ?, ?, ?, 0, 'pos', ?, ?, '')""",
                (ts, direction, cents, cp, cat))
        conn.commit()
    finally:
        conn.close()


def test_bill_annual_report_flow(tmp_path):
    """年度账单报告:年度词 → annual_report(往年整年)被调,产出逐月走势/
    分类/商户的 bill_view(api 据此下发 data-bill-report),只读零动钱。"""
    db = _mk_db(tmp_path)
    year = date.today().year - 1
    _seed_year_flow(db, year)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "bill_analysis"}',
            json.dumps({"ask": "annual_report", "period": f"{year}年",
                        "category": None}, ensure_ascii=False),
            f"{year} 年度账单报告已生成。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "b-annual"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage(f"{year}年度账单报告")]}, cfg)
            assert not r1.get("__interrupt__")  # 只读报告,无闸门
            snap = await graph.aget_state(cfg)
            calls = _traced(snap.values, "annual_report")
            assert calls and calls[0]["args"]["year"] == year
            view = snap.values.get("bill_view") or {}
            assert view.get("kind") == "annual"
            assert view.get("title") == f"年度账单报告 {year}"
            assert view.get("income_yuan") == "24000.00"   # 3 × 8000
            assert view.get("expense_yuan") == "6000.00"   # 1000+2000+3000
            assert view.get("net_yuan") == "18000.00"
            assert view.get("savings_rate_pct") == 75.0
            # 往年整年:12 个月刻度齐全,有数据的月份按月份落位
            months = {m["month"]: m for m in view.get("monthly") or []}
            assert len(months) == 12
            assert months[f"{year}-02"]["expense_yuan"] == "2000.00"
            assert months[f"{year}-12"]["expense_yuan"] == "0.00"
            # 分类/商户形状即卡片契约(前端零算术)
            cats = {c["category"] for c in view.get("categories") or []}
            assert cats == {"生活", "餐饮"}
            merchants = {m["merchant"] for m in view.get("merchants") or []}
            assert merchants == {"示例超市", "示例餐厅"}
            assert any("支出最高月" in h for h in view.get("highlights") or [])
            assert "annual_report" in _audited_tools(db)
            assert _balance(db) == DEFAULT_BALANCE  # 只读零变化

    run(scenario())


def test_bill_report_one_shot_from_casual_phrase(tmp_path):
    """用户口径(2026-10-26):输入「月度/年度账单报告。」时——即便 LLM 连意图
    都抽不准(误判 chat)、也抽不出时间范围(NULL)——也必须一次出报告卡:
    router 确定性改道 bill_analysis,b_extract 定 annual_report + 今年缺省,
    不反问、零动钱。这是演示主路径,任何一环依赖 LLM 都会现场翻车。"""
    db = _mk_db(tmp_path)
    cur = date.today().year

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "chat"}',  # 模拟实测:LLM 误判成闲聊
            json.dumps({"ask": None, "period": None, "category": None},
                       ensure_ascii=False),
            "年度账单报告已生成。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "b-oneshot"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("月度/年度账单报告。")]}, cfg)
            assert not r1.get("__interrupt__"), "报告类请求不应反问时间范围"
            snap = await graph.aget_state(cfg)
            assert snap.values.get("intent") == "bill_analysis"  # 路由改道生效
            calls = _traced(snap.values, "annual_report")
            assert calls and calls[0]["args"]["year"] == cur
            view = snap.values.get("bill_view") or {}
            assert view.get("kind") == "annual"
            assert str(cur) in (view.get("title") or "")
            assert _balance(db) == DEFAULT_BALANCE

    run(scenario())


def test_parse_period_year_forms():
    """年度口径的确定性时间解析(纯函数):今年/去年/前年/显式年份;
    月度与区间语义不受影响。"""
    from agent.graph import parse_period
    ref = date(2026, 10, 26)
    this_year = parse_period("今年", ref)
    assert (this_year["start"], this_year["end"], this_year["year"]) == \
        ("2026-01-01", "2026-10-26", 2026)
    last = parse_period("去年", ref)
    assert (last["start"], last["end"], last["year"]) == \
        ("2025-01-01", "2025-12-31", 2025)
    assert parse_period("前年", ref)["year"] == 2024
    assert parse_period("2024年", ref)["start"] == "2024-01-01"
    assert parse_period("2024年度报告", ref)["end"] == "2024-12-31"
    assert parse_period("年度", ref)["start"] == "2026-01-01"
    # 月度语义不变:显式月份仍回到该月(年报解析不能抢走月报)
    mon = parse_period("2025-08", ref)
    assert (mon["month"], mon["start"], mon["end"]) == \
        ("2025-08", "2025-08-01", "2025-08-31")


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
            # 产品列表卡(2026-10-26 图一用户口径):问"有哪些产品"必须随播报
            # 弹出表单卡,不能只回一段文字——api 在本节点下发 data-wealth-products
            view = snap.values.get("wealth_view") or {}
            assert view.get("level") == "C3"
            assert view.get("need_assess") is False
            view_products = {p["name"]: p for p in view.get("products") or []}
            assert set(view_products) == {"余额+货币基金", "稳健纯债基金"}
            assert all(p["purchasable"] is True for p in view_products.values())
            assert view_products["余额+货币基金"]["min_subscribe_yuan"] == "1.00"
            # 零动钱:没有任何申购/赎回轨迹,余额不变
            tools_traced = {c["tool"] for c in snap.values.get("bank_calls", [])}
            assert "subscribe_product" not in tools_traced
            assert "redeem_product" not in tools_traced
            assert _balance(db) == DEFAULT_BALANCE

    run(scenario())


def test_wealth_clarify_cancel_stops_asking(tmp_path):
    """补槽反问中用户明确说"不办了" → 立即按"本次不办理"收尾,
    不再回环重问同一问题;全程零资金变动(2026-10-22 评审修复)。
    2026-10-26 起,泛入口(action 缺失)的"反问"是理财中心入口卡
    (wealth_entry:产品清单+目的选项),取消语义与普通缺槽反问一致。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "wealth"}',
            json.dumps({"action": None, "keyword": None, "product_id": None,
                        "amount_yuan": None, "holding_id": None, "p_type": None,
                        "assess_answer": None}, ensure_ascii=False),
            "",  # 播报 LLM 返回空 → 走确定性兜底文案
        ]) as graph:
            cfg = {"configurable": {"thread_id": "w-cancel"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("帮我弄下理财")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "wealth_entry"
            r2 = await graph.ainvoke(Command(resume="我不办了"), cfg)
            # 关键断言:不再反问(没有新 interrupt),直接兜底收尾
            assert not r2.get("__interrupt__")
            assert "先不办理" in _ai_text(r2)
            snap = await graph.aget_state(cfg)
            notice = snap.values.get("notice") or {}
            assert notice.get("kind") == "clarify_cancel"
            assert _balance(db) == DEFAULT_BALANCE
            assert _count(db, "transfer_orders") == 0

    run(scenario())


def test_wealth_entry_card_misroute_fixed_and_purpose_flow(tmp_path):
    """理财中心入口全流程(2026-10-26 用户口径):「理财操作」被 LLM 误判成
    linkage(实测截图:进"事件日期"反问循环)→ 确定性改道 wealth;泛入口
    直接弹理财中心卡(等级+在售产品+目的选项);答目的后按目的+等级
    个性化推荐(确定性 purpose 捕获,不依赖 LLM 二次理解)。零动钱。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "linkage"}',  # 模拟实测误判:LLM 把"理财操作"归联动
            json.dumps({"action": None, "keyword": None, "product_id": None,
                        "amount_yuan": None, "holding_id": None, "p_type": None,
                        "assess_answer": None}, ensure_ascii=False),
            json.dumps({"action": None, "keyword": None, "product_id": None,
                        "amount_yuan": None, "holding_id": None, "p_type": None,
                        "assess_answer": None}, ensure_ascii=False),
            "已按你的理财目的筛出可购产品。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "w-entry"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("理财操作")]}, cfg)
            # 误判被确定性改写:这一轮走的是理财,不是联动
            assert r1.get("intent") == "wealth", "强理财词必须改道 wealth"
            entry = _interrupt_payload(r1)
            assert entry["type"] == "wealth_entry"
            assert entry["level"] == "C3"
            assert entry["max_risk_level"] == 3
            assert entry["assess_required"] is False
            by_code = {p["code"]: p for p in entry["products"]}
            # 全量在售清单,可购标注按等级算好(C3:R1/R2 可购,R4 受限)
            assert set(by_code) == {"MF001", "BD003", "MX006"}
            assert by_code["MF001"]["purchasable"] is True
            assert by_code["MX006"]["purchasable"] is False
            # 目的选项:四类目的 + 测评入口
            assert entry["options"][-1] == "先做风险测评"
            assert any("灵活" in o for o in entry["options"])

            # 答"灵活闲钱" → 确定性映射 purpose=flexible → query 推荐
            r2 = await graph.ainvoke(
                Command(resume="闲钱要灵活,随用随取"), cfg)
            assert not r2.get("__interrupt__")
            snap = await graph.aget_state(cfg)
            notice = snap.values.get("notice") or {}
            assert notice.get("kind") == "wealth_products"
            assert notice.get("purpose") == "flexible"
            # 目的排序:货基(money_fund)排到债基前面(产品库默认按基准收益
            # 降序是债基在前,排序生效才可能货基第一)
            names = [p["name"] for p in notice.get("products") or []]
            assert names and names[0] == "余额+货币基金"
            # 推荐仍受适当性约束:R4 科技股基不在结果里
            assert "科技创新股票基金" not in names
            # 零动钱
            tools_traced = {c["tool"] for c in snap.values.get("bank_calls", [])}
            assert "subscribe_product" not in tools_traced
            assert "redeem_product" not in tools_traced
            assert _balance(db) == DEFAULT_BALANCE

    run(scenario())


def test_wealth_query_without_profile_lists_r1(tmp_path):
    """未测评用户的推荐:不再空手劝退(no_risk_profile),照常列出可购 R1
    产品并带 need_assess 提示(2026-10-26 口径:让用户先看到能买什么)。"""
    db = _mk_db(tmp_path)  # 不调 _seed_bill_wealth_card → 无风险测评
    conn = sqlite3.connect(db)
    conn.executemany(
        """INSERT INTO wealth_products
           (id,code,name,p_type,risk_level,expected_return_bps,
            min_subscribe_cents,lock_days,subscription_fee_bps,
            redemption_fee_bps,intro)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""", _W_SEED_PRODUCTS)
    conn.commit()
    conn.close()

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "wealth"}',
            json.dumps({"action": "query", "keyword": None, "product_id": None,
                        "amount_yuan": None, "holding_id": None, "p_type": None,
                        "assess_answer": None}, ensure_ascii=False),
            "",  # 播报空 → 兜底文案(need_assess 专用)
        ]) as graph:
            cfg = {"configurable": {"thread_id": "w-r1"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("推荐几款理财产品")]}, cfg)
            assert not r1.get("__interrupt__")
            snap = await graph.aget_state(cfg)
            notice = snap.values.get("notice") or {}
            assert notice.get("kind") == "wealth_products"
            assert notice.get("need_assess") is True
            names = {p["name"] for p in notice.get("products") or []}
            assert names == {"余额+货币基金"}  # 只有 R1 可购
            assert "R1" in _ai_text(r1)  # 兜底文案提到低风险与测评入口
            # 产品列表卡:未测评也要弹(need_assess=True 由前端渲染成
            # "仅可购 R1"的提示条,而不是把用户拦在门外)
            view = snap.values.get("wealth_view") or {}
            assert view.get("need_assess") is True
            assert view.get("level") is None
            assert view.get("max_risk_level") == 1
            assert {p["name"] for p in view.get("products") or []} == \
                {"余额+货币基金"}

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


# ------------------------------------------- 卡号线索解析(2026-10-26 图二用户口径)

_DEMO_CARDS = [
    {"id": 1, "card_no_masked": "6222 **** **** 8821", "card_type": "debit"},
    {"id": 2, "card_no_masked": "6222 **** **** 6673", "card_type": "debit"},
    {"id": 3, "card_no_masked": "6225 **** **** 0231", "card_type": "credit"},
]


def test_resolve_card_matches_visible_digits():
    """_resolve_card 纯函数:用户照卡片念出可见两段"62228821"必须唯一命中,
    不能因为不是 4 位尾号就退回反问(实测截图:说了卡号还被问"哪一张")。
    既有口径(4 位尾号/类型/序号)零回归;跨掩码边界的拼接不猜。"""
    r = graph_mod._resolve_card
    assert r("62228821", _DEMO_CARDS) == 1     # 可见两段连打(头6222+尾8821)
    assert r("挂失借记卡 62228821", _DEMO_CARDS) == 1  # 线索混在原话里
    assert r("尾号8821", _DEMO_CARDS) == 1     # 4 位尾号(既有口径)
    assert r("8821", _DEMO_CARDS) == 1
    assert r("62228822", _DEMO_CARDS) is None  # 连打对不上任何卡 → 反问
    assert r("62226673", _DEMO_CARDS) == 2     # 第二张借记卡同样可定位
    assert r("借记卡", _DEMO_CARDS) is None    # 两张借记卡,类型无法消歧 → 反问
    assert r("信用卡", _DEMO_CARDS) == 3       # 唯一信用卡仍按类型命中
    assert r("第2张", _DEMO_CARDS) == 2        # 序号口径零回归
    assert r("6222021500058821", _DEMO_CARDS) == 1  # 完整卡号形态(头尾可见段对上)
    assert r("28821", _DEMO_CARDS) is None     # 跨掩码边界(第5位起是掩码)不猜
    assert r("", _DEMO_CARDS) is None
    assert r("8821", []) is None               # 无卡 → None(调用层判 no_card)


def test_card_lost_with_visible_digits_skips_clarify(tmp_path):
    """图二实测场景:名下两张借记卡,用户说"挂失借记卡62228821"(可见两段连打)
    → 不再反问"哪一张",直接定位尾号8821那张过双闸;另一张借记卡(6673)
    状态零变化。抽取器即使没把数字摘进 card_hint(脚本回 None),也用
    本轮原话回退解析。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)
    conn = sqlite3.connect(db)  # 补第二张借记卡(与 bank_core.seed 演示库同款)
    conn.execute(
        """INSERT INTO cards (id,user_id,account_id,card_no_masked,card_type,
           status,daily_limit_cents,per_tx_limit_cents,created_at)
           VALUES (2,1,1,'6222 **** **** 6673','debit','locked',500000,100000,
                   '2026-01-01T00:00:00')""")
    conn.commit()
    conn.close()

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "card"}',
            # card_hint=None:故意让抽取器"漏摘"数字,验证原话回退解析
            json.dumps({"action": "status", "card_id": None, "card_hint": None,
                        "card_type": None, "daily_limit_yuan": None,
                        "per_tx_limit_yuan": None,
                        "status_target": "lost"}, ensure_ascii=False),
            "挂失已完成。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "k-visible"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("挂失借记卡 62228821")]}, cfg)
            g1 = _interrupt_payload(r1)
            # 关键断言:直接进确认闸,而不是 ask_slot 反问"哪一张"
            assert g1["type"] == "confirm_card", \
                f"说了卡号还被反问:{g1.get('type')}"
            assert g1["card"]["kind"] == "status"
            assert g1["card"]["card_id"] == 1  # 8821 那张,不是 6673
            assert g1["card"]["tail"] == "8821"
            assert g1["card"]["target"] == "lost"
            r2 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert "不可逆" in _interrupt_payload(r2)["question"]  # 第二道闸
            r3 = await graph.ainvoke(Command(resume="确认"), cfg)
            assert not r3.get("__interrupt__")
            assert _card_row(db, 1)["status"] == "lost"
            assert _card_row(db, 2)["status"] == "locked"  # 另一张卡零变化
            snap = await graph.aget_state(cfg)
            calls = _traced(snap.values, "set_card_status")
            assert calls and calls[0]["args"] == {"card_id": 1, "status": "lost"}

    run(scenario())


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
            assert "还需要取消其他订阅吗?" in _ai_text(r3)
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


# ---------------------------------------------------------------- 订阅代扣预审(2026-10-20)

def _seed_precheck_sub(db: Path, merchant: str = "明日扣会员",
                        amount_cents: int = 1500,
                        next_date: str | None = None) -> int:
    """种一条 next=明天 的 active 订阅(日期运行时算,不对真实时钟敏感)。"""
    from datetime import date, timedelta
    nxt = next_date or (date.today() + timedelta(days=1)).isoformat()
    conn = sqlite3.connect(db)
    cur = conn.execute(
        """INSERT INTO subscriptions
           (user_id, merchant_name, category, amount_cents, period_days,
            next_charge_date, status, detected_at)
           VALUES (1,?, '订阅', ?, 30, ?, 'active', '2026-09-01T00:00:00')""",
        (merchant, amount_cents, nxt))
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid


def test_subscription_precheck_shows_view(tmp_path):
    """订阅预审:过期种子滚动后在窗口外,明天扣费的在窗口内,无闸门直发卡。"""
    from datetime import date, timedelta

    db = _mk_db(tmp_path)
    _seed_subscriptions(db)          # 老种子(2026-10 初,滚动到月底,窗口外)
    _seed_precheck_sub(db)           # 明天扣费
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "subscription"}',
            json.dumps({"action": "precheck", "merchant": None}, ensure_ascii=False),
            "已列出即将扣费。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "sp1"}}
            r = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("订阅预审,看看即将扣费")]}, cfg)
            assert not r.get("__interrupt__")  # 只读,不停闸门
            view = r["precheck_view"]
            merchants = [i["merchant_name"] for i in view["items"]]
            assert merchants == ["明日扣会员"]
            item = view["items"][0]
            assert item["days_left"] == 1
            assert item["next_charge_date"] == tomorrow
            assert item["amount_yuan"] == "15.00"
            assert r["notice"]["kind"] == "sub_precheck"

    run(scenario())


def test_subscription_precheck_allow_executes(tmp_path):
    """放行本期:LOW 决策无闸门直接落库,ack 记录本期、订阅仍 active。"""
    from datetime import date, timedelta

    db = _mk_db(tmp_path)
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    sid = _seed_precheck_sub(db, next_date=tomorrow)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "subscription"}',
            json.dumps({"action": "allow", "merchant": "明日扣会员"},
                       ensure_ascii=False),
            "已放行本期。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "sp2"}}
            r = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("放行明日扣会员的本期代扣")]}, cfg)
            assert not r.get("__interrupt__")  # allow/skip 不设闸门(不动钱)
            assert r["notice"]["kind"] == "sub_precheck_done"
            assert r["notice"]["decision"] == "allow"
            assert r["notice"]["charge_date"] == tomorrow
            conn = sqlite3.connect(db)
            row = conn.execute(
                "SELECT precheck_ack_date, status FROM subscriptions WHERE id=?",
                (sid,)).fetchone()
            conn.close()
            assert row[0] == tomorrow
            assert row[1] == "active"

    run(scenario())


# ---------------------------------------------------------------- 生日预算计划(p_ 管线)
# 新增的只读规划管线:router(linkage + 规划触发词 → plan)→ p_extract
# → p_snapshot(5 个只读工具)→ p_build(纯计算)→ p_gate(confirm_plan,无密码)
# → 确认后按需 s_*(取消订阅)/l_*(锁定预算),两段动钱各自过支付密码闸门。
# 本段全部是追加用例,上面的旧用例一行未改。剧本日期相对今天推算(生日 = 今天+4),
# 断言不随日期流逝翻车。

_PLAN_BIRTHDAY = (date.today() + timedelta(days=4)).isoformat()


def _seed_month_flow(db: Path, income_yuan: str = "5000",
                     expense_yuan: str = "1000") -> None:
    """本月一笔工资收入 + 一笔消费:月报口径 → 可支配 3500.00。

    monthly_report 会排除 subscribe/redeem 与"收款方是本人账户"的内部划转,
    这里用普通商户名,确保计入收支。
    """
    from bank_core.money import yuan_to_cents

    ts = f"{date.today().isoformat()}T10:00:00"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """INSERT INTO transactions
               (account_id, ts, direction, amount_cents, balance_after_cents,
                tx_type, counterparty, category, memo)
               VALUES (1, ?, 'in', ?, 0, 'salary', '示例公司', '工资', '')""",
            (ts, yuan_to_cents(income_yuan)))
        conn.execute(
            """INSERT INTO transactions
               (account_id, ts, direction, amount_cents, balance_after_cents,
                tx_type, counterparty, category, memo)
               VALUES (1, ?, 'out', ?, 0, 'pos', '示例超市', '生活', '')""",
            (ts, yuan_to_cents(expense_yuan)))
        conn.commit()
    finally:
        conn.close()


# (id, merchant, category, amount_cents, period_days, status)
_PLAN_SUB_SEED = [
    (1, "腾讯视频VIP", "订阅", 3000, 31, "active"),
    (2, "Keep会员", "订阅", 1900, 31, "active"),
    (3, "张阿姨", "房租", 680000, 30, "active"),
    (4, "理财专户", "转账", 200000, 30, "active"),
    (5, "余额+货币基金", "理财", 200000, 30, "active"),
    (6, "信用卡还款(0231)", "还款", 300000, 30, "active"),
    (7, "中国移动", "通讯", 12800, 31, "active"),
    (8, "旧会员", "订阅", 990, 31, "cancelled"),
]


def _seed_plan_subs(db: Path) -> None:
    """订阅候选种子(行数据来自 bank_core.seed 的真实商户)。

    next_charge_date 统一设成"今天 + 20 天",远于"生日 + 4 天"的预扣窗口,
    保证 facts.upcoming_yuan = 0.00、可支配稳定为 3500.00。
    """
    nxt = (date.today() + timedelta(days=20)).isoformat()
    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            """INSERT INTO subscriptions
               (id, user_id, merchant_name, category, amount_cents, period_days,
                next_charge_date, status, detected_at)
               VALUES (?,1,?,?,?,?,?,?, '2026-09-01T00:00:00')""",
            [(i, m, c, a, p, nxt, s)
             for (i, m, c, a, p, s) in _PLAN_SUB_SEED])
        conn.commit()
    finally:
        conn.close()


def _plan_slots_json(budget: str = "3000", title: str = "林悦的生日") -> str:
    """p_extract 的 FakeLLM 抽取结果(日期运行时推算)。"""
    return json.dumps(
        {"event_title": title, "event_date": _PLAN_BIRTHDAY,
         "budget_yuan": budget,
         "actions": [
             {"what": "鲜花", "merchant": "花店",
              "days_before": 2, "amount_yuan": "300"},
             {"what": "蛋糕", "merchant": "蛋糕店",
              "days_before": 1, "amount_yuan": "200"}]},
        ensure_ascii=False)


def _plan_steps_gates(card: dict) -> list:
    return [step.get("gate") for step in card.get("steps") or []]


class _CountingLLM(FakeMessagesListChatModel):
    """FakeLLM + 调用计数:FakeMessagesListChatModel 的 responses 会**循环**,
    不能用 i 当计数;计划路径要钉死"router+p_extract+播报 = 3 次"。"""

    calls: int = 0

    async def ainvoke(self, *args, **kwargs):
        self.calls += 1
        return await super().ainvoke(*args, **kwargs)


class _PlanSession:
    """计划支线测试脚手架:与 _AgentSession 同构,额外暴露 LLM 调用计数。"""

    def __init__(self, db: Path, tmp_path: Path, responses: list[str]):
        self._db, self._tmp, self._resp = db, tmp_path, responses
        self.llm: _CountingLLM | None = None
        self.graph = None
        self._conn = None

    async def __aenter__(self):
        tools = await load_bank_tools(self._db)
        saver, self._conn = await make_sqlite_checkpointer(
            self._tmp / "plan_agent_ckpt.sqlite")
        self.llm = _CountingLLM(
            responses=[AIMessage(content=r) for r in self._resp])
        self.graph = build_agent_graph(self.llm, tools, saver)
        return self

    async def __aexit__(self, *exc):
        if self._conn is not None:
            await self._conn.close()
        return False


def _assert_plan_zero_writes(db: Path) -> None:
    """confirm_plan 前的"零写"口径:只看业务表(audit_log 留痕不计)。"""
    assert _count(db, "transfer_orders") == 0
    assert _count(db, "linkage_plans") == 0
    assert _count(db, "scheduled_tasks") == 0
    assert _count(db, "user_events") == 0
    assert _balance(db) == DEFAULT_BALANCE


def test_plan_path_budget_fits_gate_then_lock(tmp_path):
    """预算在可支配内:confirm_plan(无密码)前零写 → 确认后出现支付密码闸门
    → 888888 后锁定单 executed、余额减预算、提醒任务落库;全程 3 次 LLM 调用。"""
    db = _mk_linkage_db(tmp_path)
    _seed_month_flow(db)
    _seed_plan_subs(db)

    async def scenario():
        async with _PlanSession(db, tmp_path, [
            '{"intent": "linkage"}',   # LLM 仍判 linkage;router 确定性改写成 plan
            _plan_slots_json("3000"),
            "生日预算已锁定。",
        ]) as session:
            graph = session.graph
            cfg = {"configurable": {"thread_id": "pp1"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("帮我规划林悦的生日,预算3000")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_plan"
            assert payload["pay_required"] is False  # 计划确认不是支付密码
            card = payload["plan"]
            assert card["requested_budget_yuan"] == "3000.00"
            assert card["recommended_budget_yuan"] == "3000.00"
            assert card["shortfall_yuan"] == "0.00"
            assert card["facts"]["event_date"] == _PLAN_BIRTHDAY
            assert card["facts"]["disposable_yuan"] == "3500.00"
            assert card["selected_cut"] is None
            assert "confirm_sub_cancel" not in _plan_steps_gates(card)
            # 计划确认前:零业务写、无锁定单、余额分文未动
            _assert_plan_zero_writes(db)

            r2 = await graph.ainvoke(Command(resume="确认"), cfg)
            lock_gate = _interrupt_payload(r2)
            assert lock_gate["type"] == "confirm_linkage"  # 锁定预算的支付密码闸门
            assert lock_gate["pay_required"] is True
            plan = lock_gate["plan"]
            assert plan["budget_yuan"] == "3000.00"
            order_id = plan["lock_order"]["order_id"]
            # 建锁定单后、确认前:仍待确认、未扣款(两步走铁律)
            assert _order(db, order_id)["status"] == "pending_confirm"
            assert _balance(db) == DEFAULT_BALANCE

            r3 = await graph.ainvoke(Command(resume="000000"), cfg)
            assert _interrupt_payload(r3)["type"] == "confirm_linkage"
            assert _balance(db) == DEFAULT_BALANCE

            r4 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert not r4.get("__interrupt__")
            order = _order(db, order_id)
            assert order["status"] == "executed"
            assert order["amount_cents"] == 300_000          # 恰好 3000 元
            assert _balance(db) == DEFAULT_BALANCE - 300_000
            assert _count(db, "linkage_plans") == 1
            assert _count(db, "scheduled_tasks") == 2        # 每动作一条提醒
            # router + p_extract + 最终播报 = 3 次;闸门/建计划/锁单不调 LLM
            assert session.llm.calls == 3

    run(scenario())


def test_plan_path_budget_exceeds_lowers_recommended(tmp_path):
    """预算 9000 > 可支配 3500 且没说砍订阅:建议预算下调为可支配,卡片体现差额,
    确认前零业务写;取消后 plan_cancelled。"""
    db = _mk_linkage_db(tmp_path)
    _seed_month_flow(db)
    _seed_plan_subs(db)

    async def scenario():
        async with _PlanSession(db, tmp_path, [
            '{"intent": "linkage"}',
            _plan_slots_json("9000"),
            "好的,已取消这次生日计划。",
        ]) as session:
            graph = session.graph
            cfg = {"configurable": {"thread_id": "pp2"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("帮我规划林悦的生日,预算9000")]}, cfg)
            card = _interrupt_payload(r1)["plan"]
            assert card["requested_budget_yuan"] == "9000.00"
            assert card["recommended_budget_yuan"] == "3500.00"   # 下调到可支配
            assert card["shortfall_yuan"] == "5500.00"            # 差额可见
            assert card["selected_cut"] is None                   # 没说要砍就不砍
            gates = _plan_steps_gates(card)
            assert "confirm_sub_cancel" not in gates
            assert "confirm_linkage" in gates
            _assert_plan_zero_writes(db)

            r2 = await graph.ainvoke(Command(resume="取消"), cfg)
            assert not r2.get("__interrupt__")
            snap = await graph.aget_state(cfg)
            assert (snap.values.get("notice") or {}).get("kind") == "plan_cancelled"
            _assert_plan_zero_writes(db)   # 取消后也没有任何业务写
            assert session.llm.calls == 3

    run(scenario())


def test_plan_path_cut_subscription_then_lock(tmp_path):
    """预算 3600 超出可支配 3500 + 钱不够就砍订阅:
    confirm_plan → confirm_sub_cancel(密码)→ s_exec → confirm_linkage(再要一次密码)
    → 锁定单 executed;房租/理财/话费类不可成为候选。"""
    db = _mk_linkage_db(tmp_path)
    _seed_month_flow(db)
    _seed_plan_subs(db)

    async def scenario():
        async with _PlanSession(db, tmp_path, [
            '{"intent": "linkage"}',
            _plan_slots_json("3600"),
            "生日预算已锁定。",
        ]) as session:
            graph = session.graph
            cfg = {"configurable": {"thread_id": "pp3"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage(
                     "帮我规划林悦的生日,预算3600,钱不够就砍订阅")]}, cfg)
            card = _interrupt_payload(r1)["plan"]
            # 候选只列真实会员:房租(张阿姨)/理财专户/货基/信用卡还款/中国移动全部排除
            assert [c["merchant"] for c in card["cut_candidates"]] == [
                "腾讯视频VIP", "Keep会员"]
            assert card["selected_cut"]["merchant"] == "腾讯视频VIP"
            assert card["selected_cut"]["amount_yuan"] == "30.00"
            assert card["selected_cut"]["annual_yuan"] == "353.23"
            assert card["recommended_budget_yuan"] == "3530.00"  # min(3600, 3500+30)
            assert card["shortfall_yuan"] == "70.00"
            assert _plan_steps_gates(card) == [
                None, "confirm_sub_cancel", "confirm_linkage", None, None]
            _assert_plan_zero_writes(db)

            r2 = await graph.ainvoke(Command(resume="确认"), cfg)
            sub_gate = _interrupt_payload(r2)
            assert sub_gate["type"] == "confirm_sub_cancel"
            assert sub_gate["pay_required"] is True
            assert sub_gate["sub"]["merchant_name"] == "腾讯视频VIP"
            assert sub_gate["sub"]["sub_id"] == 1
            assert _sub_status(db, 1) == "active"   # 密码闸门前不取消
            assert _balance(db) == DEFAULT_BALANCE

            r3 = await graph.ainvoke(Command(resume="888888"), cfg)
            lock_gate = _interrupt_payload(r3)
            # 取消订阅后进入锁定预算:再要一次支付密码(两个动钱动作各自过闸)
            assert lock_gate["type"] == "confirm_linkage"
            assert lock_gate["pay_required"] is True
            assert lock_gate["plan"]["budget_yuan"] == "3530.00"
            assert _sub_status(db, 1) == "cancelled"
            assert _balance(db) == DEFAULT_BALANCE
            order_id = lock_gate["plan"]["lock_order"]["order_id"]
            assert _order(db, order_id)["status"] == "pending_confirm"

            r4 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert not r4.get("__interrupt__")
            assert _order(db, order_id)["status"] == "executed"
            assert _order(db, order_id)["amount_cents"] == 353_000
            assert _balance(db) == DEFAULT_BALANCE - 353_000
            assert _count(db, "scheduled_tasks") == 2
            # 最后一步是 l_report(锁定预算),确定性收尾必须让用户看到订阅已取消
            text = _ai_text(r4)
            assert "腾讯视频VIP" in text and "取消" in text
            assert session.llm.calls == 3

    run(scenario())


def test_plan_path_budget_edit_rebuilds(tmp_path):
    """计划卡上回"预算改成800":p_build 重算、计划卡变化,仍停在 confirm_plan,
    零业务写;再取消 → plan_cancelled。"""
    db = _mk_linkage_db(tmp_path)
    _seed_month_flow(db)
    _seed_plan_subs(db)

    async def scenario():
        async with _PlanSession(db, tmp_path, [
            '{"intent": "linkage"}',
            _plan_slots_json("3000"),
            "好的,已取消这次生日计划。",
        ]) as session:
            graph = session.graph
            cfg = {"configurable": {"thread_id": "pp4"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("帮我规划林悦的生日,预算3000")]}, cfg)
            first = _interrupt_payload(r1)
            assert first["type"] == "confirm_plan"
            assert first["plan"]["plan_version"] == 1
            assert first["plan"]["requested_budget_yuan"] == "3000.00"

            r2 = await graph.ainvoke(Command(resume="预算改成800"), cfg)
            second = _interrupt_payload(r2)   # 重算后重新出卡,仍停在计划闸门
            assert second["type"] == "confirm_plan"
            card = second["plan"]
            assert card["plan_version"] == 2
            assert card["requested_budget_yuan"] == "800.00"
            assert card["recommended_budget_yuan"] == "800.00"
            assert card["shortfall_yuan"] == "0.00"
            assert card["facts"]["disposable_yuan"] == "3500.00"  # 事实快照不重读
            _assert_plan_zero_writes(db)      # 预算编辑不产生任何业务写

            r3 = await graph.ainvoke(Command(resume="取消"), cfg)
            assert not r3.get("__interrupt__")
            snap = await graph.aget_state(cfg)
            assert (snap.values.get("notice") or {}).get("kind") == "plan_cancelled"
            _assert_plan_zero_writes(db)
            assert session.llm.calls == 3     # 重算/重问都不调 LLM

    run(scenario())


def test_plan_cut_decline_reconfirms_without_cut(tmp_path):
    """取消订阅的支付密码闸门回「取消」→ 不静默收尾(复核修复):
    p_degrade(强制不砍)→ p_build(按可支配 3500 重算)→ p_gate 重新确认,
    用户再次确认后才进 confirm_linkage 锁定预算。"""
    db = _mk_linkage_db(tmp_path)
    _seed_month_flow(db)
    _seed_plan_subs(db)

    async def scenario():
        async with _PlanSession(db, tmp_path, [
            '{"intent": "linkage"}',
            _plan_slots_json("3600"),
            "生日预算已锁定。",
        ]) as session:
            graph = session.graph
            cfg = {"configurable": {"thread_id": "pp6"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage(
                     "帮我规划林悦的生日,预算3600,钱不够就砍订阅")]}, cfg)
            card = _interrupt_payload(r1)["plan"]
            assert card["selected_cut"]["merchant"] == "腾讯视频VIP"

            r2 = await graph.ainvoke(Command(resume="确认"), cfg)
            assert _interrupt_payload(r2)["type"] == "confirm_sub_cancel"

            r3 = await graph.ainvoke(Command(resume="取消"), cfg)
            again = _interrupt_payload(r3)      # 降级后重新出计划卡
            assert again["type"] == "confirm_plan"
            card2 = again["plan"]
            assert card2["plan_version"] == 2
            assert card2["selected_cut"] is None
            assert card2["recommended_budget_yuan"] == "3500.00"
            assert "订阅保留" in card2["note"]
            assert _sub_status(db, 1) == "active"
            assert _count(db, "linkage_plans") == 0   # 重新确认前不建锁定计划
            assert _balance(db) == DEFAULT_BALANCE

            r4 = await graph.ainvoke(Command(resume="确认"), cfg)
            gate = _interrupt_payload(r4)
            assert gate["type"] == "confirm_linkage"
            assert gate["plan"]["budget_yuan"] == "3500.00"

            r5 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert not r5.get("__interrupt__")
            assert _balance(db) == DEFAULT_BALANCE - 350_000
            assert _count(db, "linkage_plans") == 1
            assert _count(db, "scheduled_tasks") == 2
            assert session.llm.calls == 3

    run(scenario())


def test_plan_cut_subscription_gone_reconfirms_without_cut(tmp_path):
    """计划选中的订阅在确认后已不存在(s_pick not_found)→ 同样降级重确认:
    p_degrade → p_build(不砍)→ p_gate,而不是既没锁预算也没再确认。"""
    db = _mk_linkage_db(tmp_path)
    _seed_month_flow(db)
    _seed_plan_subs(db)

    async def scenario():
        async with _PlanSession(db, tmp_path, [
            '{"intent": "linkage"}',
            _plan_slots_json("3600"),
            "生日预算已锁定。",
        ]) as session:
            graph = session.graph
            cfg = {"configurable": {"thread_id": "pp7"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage(
                     "帮我规划林悦的生日,预算3600,钱不够就砍订阅")]}, cfg)
            assert _interrupt_payload(r1)["plan"]["selected_cut"]["merchant"] == "腾讯视频VIP"

            # 计划确认后、执行前,该订阅已在别处被取消 → s_pick 定位不到
            conn = sqlite3.connect(db)
            try:
                conn.execute("UPDATE subscriptions SET status='cancelled' WHERE id=1")
                conn.commit()
            finally:
                conn.close()

            r2 = await graph.ainvoke(Command(resume="确认"), cfg)
            again = _interrupt_payload(r2)
            assert again["type"] == "confirm_plan"
            card2 = again["plan"]
            assert card2["selected_cut"] is None
            assert card2["recommended_budget_yuan"] == "3500.00"
            assert "订阅保留" in card2["note"]
            assert _count(db, "linkage_plans") == 0
            assert _balance(db) == DEFAULT_BALANCE

            r3 = await graph.ainvoke(Command(resume="确认"), cfg)
            gate = _interrupt_payload(r3)
            assert gate["type"] == "confirm_linkage"
            assert gate["plan"]["budget_yuan"] == "3500.00"

            r4 = await graph.ainvoke(Command(resume="888888"), cfg)
            assert not r4.get("__interrupt__")
            assert _balance(db) == DEFAULT_BALANCE - 350_000
            assert _count(db, "linkage_plans") == 1

    run(scenario())


def test_plan_lock_creates_same_day_event_when_title_exists_other_day(tmp_path):
    """计划卡日期=用户指定日:库里只有同名但不同日的事件时,p_lock 先建同日事件,
    锁定预算/提醒都落在用户指定的那一天(复核修复:按标题绑回旧事件是错的)。"""
    db = _mk_linkage_db(tmp_path)
    _seed_month_flow(db)
    _seed_plan_subs(db)
    old_date = (date.today() + timedelta(days=10)).isoformat()
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            """INSERT INTO user_events
               (id, user_id, event_type, title, event_date, repeat_yearly, note)
               VALUES (1, 1, 'birthday', '林悦的生日', ?, 1, '')""",
            (old_date,))
        conn.commit()
    finally:
        conn.close()

    async def scenario():
        async with _PlanSession(db, tmp_path, [
            '{"intent": "linkage"}',
            _plan_slots_json("3000"),
            "生日预算已锁定。",
        ]) as session:
            graph = session.graph
            cfg = {"configurable": {"thread_id": "pp8"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("帮我规划林悦的生日,预算3000")]}, cfg)
            card = _interrupt_payload(r1)["plan"]
            assert card["facts"]["event_date"] == _PLAN_BIRTHDAY
            snap = await graph.aget_state(cfg)
            assert snap.values["plan_facts"]["event_found"] is False   # 同日事件不存在

            r2 = await graph.ainvoke(Command(resume="确认"), cfg)
            gate = _interrupt_payload(r2)
            assert gate["type"] == "confirm_linkage"
            assert gate["plan"]["event"]["date"] == _PLAN_BIRTHDAY
            assert gate["plan"]["title"] == "林悦的生日联动"
            # 提醒按用户指定的生日排:提前 2 天 / 提前 1 天
            assert [a["run_at"][:10] for a in gate["plan"]["actions"]] == [
                (date.today() + timedelta(days=2)).isoformat(),
                (date.today() + timedelta(days=3)).isoformat()]

            conn = _ro(db)
            try:
                rows = [tuple(r) for r in conn.execute(
                    "SELECT title, event_date FROM user_events ORDER BY id").fetchall()]
            finally:
                conn.close()
            assert rows == [("林悦的生日", old_date),
                            ("林悦的生日", _PLAN_BIRTHDAY)]

    run(scenario())


def test_due_reminder_with_plan_trigger_word_stays_due(tmp_path):
    """系统到期提醒(api.py 生成的文本)里计划标题自带"省钱"等强触发词时,
    router 不得改写成 p_ 规划管线——旧 l_due 到期处理必须照常进动作闸门。"""
    db = _mk_linkage_db(tmp_path)
    plan_id = _seed_linkage_plan(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("UPDATE linkage_plans SET title='省钱生日联动' WHERE id=?",
                     (plan_id,))
        conn.commit()
    finally:
        conn.close()

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "linkage"}',
            "两样都买好了,联动计划完成。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "pp9"}}
            msg = "(到期提醒) 计划《省钱生日联动》的【鲜花】今天到期,请帮我处理"
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage(msg)]}, cfg)
            g1 = _interrupt_payload(r1)
            assert g1["type"] == "confirm_linkage_action"
            assert g1["action"]["plan_id"] == plan_id
            assert g1["action"]["plan_title"] == "省钱生日联动"
            assert g1["action"]["what"] == "鲜花"
            assert _balance(db) == DEFAULT_BALANCE

    run(scenario())


# =================================================================== Agent Core
# 迭代计划 §13.2 新增:Core 闭环 / 技能组合 / 局部重规划 / 长期记忆。
# 红线:分析路径零动钱;旧账单/理财查询路径一字不变。

def _seed_tx_month(db: Path, year: int, month: int) -> None:
    """给指定月份补 6 笔餐饮支出 + 1 笔工资(Core 的 analyze_bill 要有事实可读)。"""
    conn = sqlite3.connect(db)
    try:
        for i in range(6):
            conn.execute(
                "INSERT INTO transactions (account_id,ts,direction,amount_cents,"
                "balance_after_cents,tx_type,counterparty,category,memo) "
                "VALUES (1,?,'out',?,900000,'pos',?,?,?)",
                (f"{year:04d}-{month:02d}-0{i + 1}T12:00:00",
                 120_000 + i * 10_000, "山姆", "餐饮", ""))
        conn.execute(
            "INSERT INTO transactions (account_id,ts,direction,amount_cents,"
            "balance_after_cents,tx_type,counterparty,category,memo) "
            "VALUES (1,?,'in',1850000,1000000,'salary','公司','工资','')",
            (f"{year:04d}-{month:02d}-05T09:00:00",))
        conn.commit()
    finally:
        conn.close()


def _prev_month_ym() -> tuple[int, int]:
    first = date.today().replace(day=1)
    prev = first - timedelta(days=1)
    return prev.year, prev.month


def test_core_bill_analysis_read_only_and_remembered(tmp_path):
    """Core 账单分析闭环:计划→只读工具→观察→复盘→写记忆;全程零动钱。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)
    y, m = _prev_month_ym()
    _seed_tx_month(db, y, m)

    async def scenario():
        before = _balance(db)
        async with _AgentSession(db, tmp_path, [
            '{"intent": "bill_analysis"}',
            "上个月餐饮花得最多,建议关注。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "core-bill"}}
            r = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("帮我分析上个月的账单")]}, cfg)
            plan = r.get("agent_plan") or {}
            assert [s["skill"] for s in plan["steps"]] == ["analyze_bill"]
            assert all(s["status"] == "done" for s in plan["steps"])
            assert plan["status"] == "done"
            # 零动钱:分析技能只读
            assert _balance(db) == before
            assert _count(db, "transfer_orders") == 0
            # Plan Trace 可回放(perceive=识别意图,由 a_perceive 写入)
            assert [t["phase"] for t in plan["trace"]] == ["perceive", "plan",
                                                           "guard", "act", "reflect"]
            # 卡片:事实行 + 复盘
            view = r.get("analysis_view") or {}
            assert view["intent"] == "bill"
            assert view["reflection"]["status"] == "success"
            labels = [row["label"] for row in view["facts"]]
            assert "最大支出分类" in labels and "Top 商户" in labels
            # 记忆:只存短摘要
            bill_mem = memory.recall(1, "bill") or {}
            assert bill_mem.get("top_category") == "餐饮"
            assert memory.recall(1, "plan_history")
            # 画像(profile):跨技能聚合摘要接线——本次只跑了账单,
            # 所以画像里有消费大头、没有风险等级(浅合并只更新覆盖到的键)
            prof_mem = memory.recall(1, "profile") or {}
            assert prof_mem.get("top_category") == "餐饮"
            assert "risk_level" not in prof_mem
            # 思维链:data-thinking 由 a_reflect 发射,暴露在最终状态里
            think = r.get("thinking_view") or {}
            thoughts = think.get("thoughts") or []
            phases = [t.get("phase") for t in thoughts]
            assert "识别意图" in phases and "制定计划" in phases \
                and "安全守卫" in phases and "复盘" in phases
            # 守卫通过态必须可见(让用户知道"为什么安全")
            assert any(t.get("status") == "passed" for t in thoughts)
            # 识别意图那一步,必须把 LLM 分类说成人话(不是 raw intent 字符串)
            intent_thought = next((t for t in thoughts
                                   if t.get("phase") == "识别意图"), {})
            assert "分析账单收支" in (intent_thought.get("text") or "")
            # 全程只有 router + 播报两次 LLM:计划/技能选择/复盘/记忆都是确定性代码

    run(scenario())


def test_old_bill_query_path_is_never_rerouted_to_core(tmp_path):
    """旧账单查询不命中分析触发词 → 仍走 b_* 路径,Core 状态为空。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "bill_analysis"}',
            json.dumps({"ask": "monthly_report", "period": "上个月",
                        "category": None}, ensure_ascii=False),
            "已读出上个月月报。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "core-old"}}
            r = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("看下上个月的账单月报")]}, cfg)
            assert (r.get("notice") or {}).get("kind") == "bill_report"
            assert r.get("agent_plan") is None
            assert r.get("analysis_view") is None

    run(scenario())


def test_core_plan_combines_two_skills(tmp_path):
    """验收第 3 条:一个计划能组合两个技能(账单 + 理财),且理财按风险过滤。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)
    y, m = _prev_month_ym()
    _seed_tx_month(db, y, m)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "bill_analysis"}',
            "账单与持仓都看了。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "core-two"}}
            r = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("帮我分析一下账单和理财持仓")]}, cfg)
            plan = r.get("agent_plan") or {}
            assert [s["skill"] for s in plan["steps"]] == [
                "analyze_bill", "analyze_wealth"]
            assert all(s["status"] == "done" for s in plan["steps"])
            # 两个技能的经验都写进记忆
            assert memory.recall(1, "bill")
            assert memory.recall(1, "wealth")
            # 画像同时聚合两个技能的要素
            prof_mem = memory.recall(1, "profile") or {}
            assert prof_mem.get("top_category")
            assert prof_mem.get("risk_level")

    run(scenario())


def test_birthday_cut_cancel_cleans_stale_merchant_key(tmp_path):
    """订阅从"保留"变"取消"后,上次的 rejected_cut_merchant 不再残留
    (merge 删键语义;检阅入口直接展示这些字段,过期数据不能留)。"""
    from agent.graph import _birthday_patch, _remember_birthday
    memory.remember(1, "birthday",
                    {"rejected_cut": True, "rejected_cut_merchant": "爱奇艺视频"})
    state = {"plan_draft": {"recommended_budget_yuan": "500", "goal": "生日存钱"},
             "plan_cut": {"status": "cancelled", "merchant": "腾讯视频"}}
    _remember_birthday(1, _birthday_patch(state, "done"))
    stored = memory.recall(1, "birthday") or {}
    assert stored["rejected_cut"] is False
    assert "rejected_cut_merchant" not in stored
    assert stored["accepted_cut_merchant"] == "腾讯视频"


def test_core_replans_only_the_failed_step(tmp_path):
    """验收第 4 条:一次可恢复失败 → 只改受影响步骤、已完成步骤不重跑、有 diff。"""
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)
    # 只在上上个月有数据:用户点名的月份必然"没有可播报事实"→ 触发局部重规划
    y, m = _prev_month_ym()
    prev2 = (date(y, m, 1) - timedelta(days=1)).replace(day=1)
    _seed_tx_month(db, prev2.year, prev2.month)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "bill_analysis"}',
            "已回退到有数据的那个月。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "core-replan"}}
            r = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("帮我分析2020-01的账单和理财持仓")]},
                cfg)
            plan = r.get("agent_plan") or {}
            assert int(plan["plan_version"]) >= 2      # 至少重规划过一次
            diff = plan.get("diff_from_version") or {}
            assert diff.get("changed") == {"s1": ["args"]}
            # 已完成步骤不重跑:理财步骤的 act 轨迹只出现一次
            acts = [t for t in plan["trace"]
                    if t.get("phase") == "act" and t.get("step") == "s2"]
            assert len(acts) == 1
            # diff 在卡片上可见
            view = r.get("analysis_view") or {}
            assert (view["diff"] or {}).get("changed") == {"s1": ["args"]}

    run(scenario())


def test_birthday_success_is_remembered(tmp_path):
    """生日预算锁定成功 → 写 birthday 记忆(结果 + 预算),供下次规划读。"""
    db = _mk_linkage_db(tmp_path)
    _seed_month_flow(db)
    _seed_plan_subs(db)

    async def scenario():
        async with _PlanSession(db, tmp_path, [
            '{"intent": "linkage"}',
            _plan_slots_json("3000"),
            "生日预算已锁定。",
        ]) as session:
            graph = session.graph
            cfg = {"configurable": {"thread_id": "bp-mem"}}
            await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("帮我规划林悦的生日,预算3000")]}, cfg)
            await graph.ainvoke(Command(resume="确认"), cfg)
            await graph.ainvoke(Command(resume="888888"), cfg)
            mem = memory.recall(1, "birthday") or {}
            assert mem.get("last_result") == "locked"
            assert mem.get("last_budget_yuan") == "3000.00"
            assert memory.recall(1, "plan_history")

    run(scenario())


def test_birthday_refusal_at_gate_is_remembered(tmp_path):
    """用户在取消订阅的支付密码闸门前退却 → 记下"下次不再默认推荐砍它"。"""
    db = _mk_linkage_db(tmp_path)
    _seed_month_flow(db)
    _seed_plan_subs(db)

    async def scenario():
        async with _PlanSession(db, tmp_path, [
            '{"intent": "linkage"}',
            _plan_slots_json("3600"),
            "好的,订阅保留。",
        ]) as session:
            graph = session.graph
            cfg = {"configurable": {"thread_id": "bp-refuse"}}
            await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage(
                     "帮我规划林悦的生日,预算3600,钱不够就砍订阅")]}, cfg)
            r2 = await graph.ainvoke(Command(resume="确认"), cfg)
            assert _interrupt_payload(r2)["type"] == "confirm_sub_cancel"
            r3 = await graph.ainvoke(Command(resume="取消"), cfg)   # 闸门拒绝
            assert _interrupt_payload(r3)["type"] == "confirm_plan"  # 降级后重确认
            mem = memory.recall(1, "birthday") or {}
            assert mem.get("rejected_cut") is True
            assert mem.get("rejected_cut_merchant") == "腾讯视频VIP"

    run(scenario())


def test_second_birthday_plan_reads_memory_and_drops_cut(tmp_path):
    """验收第 5 条:第二次同类目标读到上次记忆,默认方案发生变化(不再推荐砍单)。"""
    db = _mk_linkage_db(tmp_path)
    _seed_month_flow(db)
    _seed_plan_subs(db)
    # 预置记忆:上次用户保留了订阅
    memory.remember(1, "birthday", {"rejected_cut": True,
                                    "rejected_cut_merchant": "腾讯视频VIP"})

    async def scenario():
        async with _PlanSession(db, tmp_path, [
            '{"intent": "linkage"}',
            _plan_slots_json("3600"),
            "按你上次的偏好,不再取消订阅。",
        ]) as session:
            graph = session.graph
            cfg = {"configurable": {"thread_id": "bp-2nd"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage(
                     "帮我规划林悦的生日,预算3600,钱不够就砍订阅")]}, cfg)
            card = _interrupt_payload(r1)["plan"]
            # 默认方案变了:没有选中的砍单,也就不出现取消订阅的支付密码闸门
            assert card["selected_cut"] is None
            assert "confirm_sub_cancel" not in _plan_steps_gates(card)
            assert "订阅保留" in (card.get("note") or "")
            # 预算按可支配下调(不是砍订阅补差额)
            assert card["recommended_budget_yuan"] == "3500.00"
            _assert_plan_zero_writes(db)

    run(scenario())


def test_core_persona_loaded_into_state_thinking_and_card(tmp_path):
    """人设闭环:a_perceive 每轮读取 → 图状态 + 思维链置顶"人设"条目 +
    计划卡🎭行;规划/抽取零 LLM 扰动(确定性代码不变)。"""
    from agent import persona as persona_mod
    persona_mod.set_persona(1, "说话像江湖侠客,先结论后细节", 0.9)
    db = _mk_db(tmp_path)
    _seed_bill_wealth_card(db)
    y, m = _prev_month_ym()
    _seed_tx_month(db, y, m)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "bill_analysis"}',
            "上个月餐饮花得最多,建议关注。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "core-persona"}}
            r = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("帮我分析上个月的账单")]}, cfg)
            assert r.get("persona") == "说话像江湖侠客,先结论后细节"
            thoughts = (r.get("thinking_view") or {}).get("thoughts") or []
            assert thoughts[0]["phase"] == "人设"
            assert "江湖侠客" in thoughts[0]["text"]
            assert thoughts[1]["phase"] == "识别意图"  # 置顶但不取代既有链
            view = r.get("analysis_view") or {}
            assert view.get("persona") == "说话像江湖侠客,先结论后细节"

    run(scenario())


# ----------------------------------------------------------------- 抽取上下文"今天"锚点(2026-10-25)
# 现场缺陷:"我后天要过生日"被反问"生日是哪一天"——LINKAGE_EXTRACT_SYS 要求相对日期
# "以今天为基准"换算,t/l/p 三处抽取上下文却从未注入今天,模型无从换算只能按
# "确实未知填 null"退化为反问。回归口径:凡带日期槽的抽取调用,发给模型的上下文
# 必须携带【今天】锚点(graph._today_anchor);换算正确性属真实模型行为,由脚本化
# 假 LLM 无法覆盖,靠锚点注入 + 提示词规则咬合来钉死接线。

def test_today_anchor_format():
    """锚点格式:'【今天】YYYY-MM-DD(星期X;…)'——与提示词规则引用的名字一致。"""
    a = graph_mod._today_anchor()
    assert a.startswith(f"【今天】{date.today().isoformat()}")
    assert "星期" in a and "相对日期" in a


def test_transfer_extract_context_carries_today_anchor(tmp_path):
    """t_extract:scheduled_at 是相对日期槽,上下文必须带【今天】。"""
    db = _mk_db(tmp_path)

    async def scenario():
        llm = _RecordingLLM([
            '{"intent": "transfer"}',
            _slots_json(payee="张三", amount_yuan="500", when="now"),
            "已向张三转账 500.00 元。",
        ])
        async with _AgentSession(db, tmp_path, ["-"], llm=llm) as graph:
            cfg = {"configurable": {"thread_id": "anchor-t"}}
            await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("给张三转 500 元")]}, cfg)
        calls = _calls_with_sys(llm, "你是转账信息抽取器")
        assert calls, "转账抽取器未被调用"
        extra = str(calls[0][-1].content)  # ask_llm 把 extra 作为最后一条 HumanMessage
        assert f"【今天】{date.today().isoformat()}" in extra
        assert extra.index("【今天】") < extra.index("【已有槽位】")

    run(scenario())


def test_linkage_extract_context_carries_today_anchor(tmp_path):
    """l_extract:event_date/days_before 是相对日期槽,上下文必须带【今天】。"""
    db = _mk_linkage_db(tmp_path)

    async def scenario():
        llm = _RecordingLLM([
            '{"intent": "linkage"}',
            _LINK_ACTIONS_JSON,
            "预算已预留,联动计划生效。",
        ])
        async with _AgentSession(db, tmp_path, ["-"], llm=llm) as graph:
            cfg = {"configurable": {"thread_id": "anchor-l"}}
            await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("我后天要过生日,给我一个生日的计划")]}, cfg)
        calls = _calls_with_sys(llm, "你是跨场景联动计划抽取器")
        assert calls, "联动抽取器未被调用"
        extra = str(calls[0][-1].content)
        assert f"【今天】{date.today().isoformat()}" in extra
        assert extra.index("【今天】") < extra.index("【已有槽位】")

    run(scenario())


# ------------------------------------------- "计划"关键词路由(2026-10-26 用户口径)

def test_plan_word_rescues_chat_into_linkage(tmp_path):
    """用户口径:消息出现"计划"即进联动。LLM 把"帮我做个计划"落进 chat 兜底时,
    确定性改写送进 linkage,l_extract 抽槽不全 → ask_slot 追问;零动钱、零订单。"""
    db = _mk_linkage_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "chat"}',   # 路由 LLM 没认出"计划" → chat 兜底
            '{}',                    # 联动抽取空槽 → 缺 event_date/budget/actions
        ]) as graph:
            cfg = {"configurable": {"thread_id": "kw-plan"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [
                HumanMessage("帮我做个计划")]}, cfg)
            payload = _interrupt_payload(r1)
            # 不是闲聊播报,而是联动补槽反问(改写生效的可观察证据)
            assert payload["type"] == "ask_slot"
            assert "event_date" in payload["missing"]
            assert _balance(db) == DEFAULT_BALANCE
            assert _count(db, "transfer_orders") == 0

    run(scenario())


def test_plan_word_never_hijacks_transfer(tmp_path):
    """硬闸①:LLM 已判转账的,"计划"二字不改写——"计划明天转账"仍走转账闸门。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',
            _slots_json(payee="张三", amount_yuan="500", when="now"),
            "已向张三转账 500.00 元。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "kw-tr"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [
                HumanMessage("计划先给张三转500")]}, cfg)
            payload = _interrupt_payload(r1)
            assert payload["type"] == "confirm_transfer"
            assert payload["order"]["amount_yuan"] == "500.00"

    run(scenario())


# ----------------------------- 联动动作容错规整(2026-10-26 迪士尼实测反馈)

def test_normalize_linkage_actions_shapes():
    """规整器覆盖三种实测歪形;救不回返回 None(交缺槽反问,不硬造)。"""
    from agent.graph import _normalize_linkage_actions as norm
    # ① 字符串条目
    got = norm(["门票 600", "快速通道:1000元"])
    assert [(a["what"], a["amount_yuan"]) for a in got] ==         [("门票", "600"), ("快速通道", "1000")]
    # ② dict 金额带单位
    got = norm([{"what": "门票", "amount_yuan": "600元", "days_before": 1}])
    assert got[0]["amount_yuan"] == "600" and got[0]["days_before"] == 1
    # ③ 金额混进 what
    got = norm([{"what": "门票600"}])
    assert got[0]["what"] == "门票" and got[0]["amount_yuan"] == "600"
    # 救不回:非列表 / 空列表 / 全垃圾
    assert norm("门票600") is None
    assert norm([]) is None
    assert norm(["到时候再定"]) is None
    # 规整后的形状能过 l_extract 的确定性校验(金额可用 _cents 换算)
    for a in norm(["门票 600"]):
        assert a["merchant"] is None and a["days_before"] == 2


def test_linkage_malformed_actions_no_reask(tmp_path):
    """端到端:抽取 LLM 输出歪形动作(金额混 what/带单位)时,规整器救回,
    一步到闸门(不再重复追问"想准备些什么")。"""
    db = _mk_linkage_db(tmp_path)
    event_day = (date.today() + timedelta(days=10)).isoformat()

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "chat"}',   # "计划"关键词改写兜底进联动
            json.dumps({"event_title": "上海迪士尼之行", "event_date": event_day,
                        "budget_yuan": "10000",
                        "actions": [{"what": "门票600"},
                                    {"what": "快速通道",
                                     "amount_yuan": "1000元"}]},
                       ensure_ascii=False),
            "预算已预留,联动计划生效。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "kw-disney"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [
                HumanMessage("帮我做个计划,10天后去上海迪士尼,"
                             "门票600,快速通道1000")]}, cfg)
            payload = _interrupt_payload(r1)
            # 直接到闸门:没有 ask_slot 反问
            assert payload["type"] == "confirm_linkage"
            plan = payload["plan"]
            whts = [a["what"] for a in plan["actions"]]
            assert "门票" in whts and "快速通道" in whts
            amounts = {a["what"]: a["amount_yuan"] for a in plan["actions"]}
            assert amounts["门票"] == "600.00"
            assert amounts["快速通道"] == "1000.00"
            assert plan["budget_yuan"] == "10000.00"

    run(scenario())


# ----------------------------- 缺槽反问选项化:选择题式补槽(2026-10-26)

def test_linkage_clarify_offers_clickable_options(tmp_path):
    """缺槽反问带可点选项(AskSlot 卡片):预算→日期→动作逐项出选择题,
    选项文本点击回话后由抽取器解析回槽位,全程零打字可走通到闸门。"""
    db = _mk_linkage_db(tmp_path)
    next_sat = _LINK_TODAY + timedelta(days=(5 - _LINK_TODAY.weekday()) % 7 or 7)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "chat"}',   # "计划"关键词改写 → linkage
            '{}',                   # 抽取空 → 问预算(带档位选项)
            '{"budget_yuan": "1000"}',           # 点了「预留1000元」
            json.dumps({"event_date": next_sat.isoformat()},   # 点了「这周六」
                       ensure_ascii=False),
            json.dumps({"actions": [{"what": "门票", "merchant": None,
                                     "days_before": 2, "amount_yuan": "600"},
                                    {"what": "快速通行", "merchant": None,
                                     "days_before": 2,
                                     "amount_yuan": "1000"}]},  # 自由输入物料清单
                       ensure_ascii=False),
            "预算已预留,联动计划生效。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "opt-l"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [
                HumanMessage("帮我做个计划")]}, cfg)
            p1 = _interrupt_payload(r1)
            assert p1["type"] == "ask_slot"
            assert p1["options"] == ["预留1000元", "预留2000元",
                                     "预留5000元", "预留10000元"]

            # 第 2 轮:点击「预留1000元」= 以该文本回话
            r2 = await graph.ainvoke(Command(resume="预留1000元"), cfg)
            p2 = _interrupt_payload(r2)
            assert p2["options"] == ["明天", "后天", "这周六", "下周六"]
            assert "event_date" in p2["missing"]

            # 第 3 轮:点击「这周六」→ 动作槽**无选项**(物料千人千面,
            # 硬编码模板是死选项,2026-10-26 用户反馈),卡片只有自由输入
            r3 = await graph.ainvoke(Command(resume="这周六"), cfg)
            p3 = _interrupt_payload(r3)
            assert "options" not in p3
            assert "actions" in p3["missing"]

            # 第 4 轮:自由输入物料清单 → 槽齐,直出闸门确认卡
            r4 = await graph.ainvoke(Command(resume="门票600元,快速通行1000元"), cfg)
            p4 = _interrupt_payload(r4)
            assert p4["type"] == "confirm_linkage"
            plan = p4["plan"]
            assert plan["budget_yuan"] == "1000.00"
            assert [a["what"] for a in plan["actions"]] == ["门票", "快速通行"]
            assert plan["event"]["date"] == next_sat.isoformat()

    run(scenario())


def test_linkage_unpriced_items_get_common_sense_reply(tmp_path):
    """用户只报清单、不报金额(实测「两辆凯迪拉克、一辆奔驰 + 预算 5000」):
    不再重复三轮"请列出每样东西和金额"然后机械放弃,而是让模型用常识回应一次
    (接住清单 → 指出预算明显不够 → 给务实方向),且反问仍停在原地可继续。
    全程零建单零动钱。

    按真实四轮复现:预算 → 日期 → 清单 是三次不同追问,第 4 轮必须先出
    常识回应,不能被 MAX_CLARIFY_ROUNDS 提前截断(2026-10-27 实测 bug)。"""
    db = _mk_linkage_db(tmp_path)
    check_remember = "记下了:两辆凯迪拉克、一辆奔驰。"
    check_gap = "这类车常见价位在百万元级,5000 元预算远远不够。"
    check_ask = "要不要调整预算或换成可实现的清单?"
    check_text = f"{check_remember} {check_gap} {check_ask}"

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "linkage"}',
            '{}',                                              # 第 1 轮:问预算
            '{"budget_yuan": "5000"}',                          # 点/答预算
            json.dumps({"event_date": LINK_BIRTHDAY},           # 第 3 轮:问日期
                       ensure_ascii=False),
            json.dumps({"actions": [{"what": "凯迪拉克", "amount_yuan": None},
                                    {"what": "奔驰", "amount_yuan": None}]},
                       ensure_ascii=False),                    # 第 4 轮:报车没金额
            json.dumps({"remember": check_remember, "required_yuan": 1_000_000,
                        "gap_hint": check_gap, "ask_short": check_ask,
                        "ask_ok": "每样大概想花多少?"}, ensure_ascii=False),
        ]) as graph:
            cfg = {"configurable": {"thread_id": "l-budget"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1, "messages": [HumanMessage("帮我准备生日")]}, cfg)
            p1 = _interrupt_payload(r1)                     # 问预算
            assert p1["type"] == "ask_slot" and "budget_yuan" in p1["missing"]

            r2 = await graph.ainvoke(Command(resume="5000元"), cfg)
            p2 = _interrupt_payload(r2)                     # 问日期
            assert "event_date" in p2["missing"]

            r3 = await graph.ainvoke(Command(resume="后天"), cfg)
            p3 = _interrupt_payload(r3)                     # 问清单(默认问句)
            assert p3["missing"] == ["actions"]
            assert "每样东西和对应金额" in p3["question"]

            # 第 4 轮:这里正是原来被 clarify_gaveup 截断的地方
            r4 = await graph.ainvoke(
                Command(resume="两辆凯迪拉克,一辆奔驰"), cfg)
            p4 = _interrupt_payload(r4)
            assert p4["type"] == "ask_slot"                 # 仍在可继续的反问态
            assert p4["question"] == check_text
            assert "凯迪拉克" in p4["question"] and check_gap in p4["question"]
            assert p4["missing"] == ["actions"]
            # 清单留档:即便条目没金额,也要记住用户说了什么
            snap = await graph.aget_state(cfg)
            assert (snap.values.get("linkage") or {}).get("items_listed") == \
                ["凯迪拉克", "奔驰"]
            # 零动钱:没有建计划、没有锁定单、余额不变
            assert _count(db, "linkage_plans") == 0
            assert _balance(db) == DEFAULT_BALANCE

    run(scenario())


def test_linkage_listed_items_and_state_question():
    """无金额条目提取 + 问句覆盖条件(纯函数,不依赖 LLM)。"""
    from agent.graph import (_linkage_listed_items, _linkage_state_question)
    # 各种歪形都能捞出"东西本身";重复项去重;最多 6 条
    assert _linkage_listed_items([
        {"what": "凯迪拉克", "amount_yuan": None},
        {"what": "奔驰"}, "两辆凯迪拉克", "鲜花 300",
    ]) == ["凯迪拉克", "奔驰", "两辆凯迪拉克", "鲜花"]
    assert _linkage_listed_items(None) == []
    assert _linkage_listed_items([{"amount_yuan": "300"}]) == []
    # 问句覆盖只在"只差动作金额"时生效(预算/日期还缺时用默认问句)
    st = {"linkage": {"ask_override": "记下了,5000 元很难买这些"}}
    assert _linkage_state_question(st, ["actions"]) == "记下了,5000 元很难买这些"
    assert _linkage_state_question(st, ["actions", "budget_yuan"]) == ""
    assert _linkage_state_question({"linkage": {}}, ["actions"]) == ""


# ----------------------------- 多单并行挂起(2026-10-26 陈朵朵/老王实测反馈)

def test_looks_like_new_transfer_patterns():
    """闸门"新转账指令"判定:命中口语转账指令;密码/确认/取消/闲聊不命中。"""
    from agent.graph import _looks_like_new_transfer as f
    assert f("给老王转500元")
    assert f("给老王转 500")
    assert f("转账2000元")
    assert f("转给陈朵朵1000块")
    assert f("500元给老王")
    assert not f("888888")          # 纯数字 = 密码尝试
    assert not f("12345678")
    assert not f("确认")            # 确认语义
    assert not f("取消")
    assert not f("先不转了")        # 取消类口语(无金额信号)
    assert not f("这单先挂着")
    assert not f("")                # 空输入


def test_transfer_gate_suspends_on_new_transfer(tmp_path):
    """闸门上说出新转账:当前单挂起(不确认不取消、不动钱),可再建第二单——
    两单并行 pending_confirm。"""
    db = _mk_db(tmp_path)

    async def scenario():
        async with _AgentSession(db, tmp_path, [
            '{"intent": "transfer"}',                       # 第一笔路由
            _slots_json(payee="张三", amount_yuan="1000", when="now"),
            "好的,这笔先挂着,为你办新的。",                  # 段1:挂起播报(LLM)
            '{"intent": "transfer"}',                       # 第二笔路由(新输入)
            _slots_json(payee="张三", amount_yuan="500", when="now"),
        ]) as graph:
            cfg = {"configurable": {"thread_id": "multi-pend"}}
            r1 = await graph.ainvoke({"auth_user_id": 1, "messages": [
                HumanMessage("给张三转1000元")]}, cfg)
            p1 = _interrupt_payload(r1)
            assert p1["type"] == "confirm_transfer"

            # 闸门上直接说出第二笔转账指令
            r2 = await graph.ainvoke(Command(resume="给张三转500元"), cfg)
            # 关键一:没有再停在闸门(分流收尾,不是把指令当密码重问)
            assert not r2.get("__interrupt__")
            # 关键二:第一单仍是 pending_confirm(挂起,未确认未取消)
            o1 = p1["order"]["id"]
            assert _order(db, o1)["status"] == "pending_confirm"
            assert _balance(db) == DEFAULT_BALANCE          # 分文未动

            # 模拟 api 层两段式:同一条指令再作为新输入进图
            r3 = await graph.ainvoke({"auth_user_id": 1, "messages": [
                HumanMessage("给张三转500元")]}, cfg)
            p3 = _interrupt_payload(r3)
            assert p3["type"] == "confirm_transfer"
            o2 = p3["order"]["id"]
            assert o2 != o1
            # 两单并行挂起:都 pending_confirm,余额仍分文未动
            assert _order(db, o1)["status"] == "pending_confirm"
            assert _order(db, o2)["status"] == "pending_confirm"
            assert _balance(db) == DEFAULT_BALANCE

    run(scenario())
