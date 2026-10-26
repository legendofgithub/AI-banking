"""长程任务(轨道通用+剧本注册)图级闭环测试。

套路与 test_agent_graph.py 一致:假 LLM 脚本化 + 真实 MCP(stdio) + 临时库。
覆盖三拍:
  1. 建任务:触发词 → a_task_new 建档 + 排复盘提醒(零动钱);
  2. 快进唤醒:run_due_tasks 到期 → 横幅文本发回 → router 分流 a_task_review
     → 查月报(只读) → 进度落库 + 调整建议;
  3. 收尾:任务终结后可再建新任务(轨道复用)。
任务库隔离:AGENT_TASKS_DB 指向 tmp;银行库与登录态照抄 _mk_db 模式。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import HumanMessage
from langgraph.types import Command

from agent.bank import load_bank_tools
from agent.graph import build_agent_graph, make_sqlite_checkpointer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BALANCE = 5_000_000  # 5 万元(分)


def _mk_db(tmp_path: Path) -> Path:
    """临时库:1 用户 + checking/savings 两个账户 + 登录态。

    进程内 `init_db(path)` 直写,不启子进程——`bank_core/db.py` 的库路径
    已是调用点延迟解析,与进程环境无关,子进程只剩约 2.5s 的固定开销。
    """
    from bank_core.db import init_db

    db = tmp_path / "bank.db"
    c = init_db(db)
    c.execute("INSERT INTO users (id,name,phone,created_at) "
              "VALUES (1,'测试用户','13800000000','2026-01-01T00:00:00')")
    c.execute("INSERT INTO accounts (id,user_id,type,name,balance_cents,opened_at) "
              "VALUES (1,1,'checking','工资卡',?,'2026-01-01T00:00:00')",
              (DEFAULT_BALANCE,))
    c.execute("INSERT INTO accounts (id,user_id,type,name,balance_cents,opened_at) "
              "VALUES (2,1,'savings','理财专户',0,'2026-01-01T00:00:00')")
    c.commit()
    c.close()
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


class _AgentSession:
    """建图 + 关闭(checkpointer 绑定事件循环,不能跨 loop 复用)。"""

    def __init__(self, db: Path, tmp_path: Path, responses: list[str]):
        self.db, self.tmp = db, tmp_path
        self.llm = FakeMessagesListChatModel(
            responses=[__import__("langchain_core.messages", fromlist=["AIMessage"])
                       .AIMessage(content=r) for r in responses])

    async def __aenter__(self):
        self.tools = await load_bank_tools(self.db, 1)
        saver, _conn = await make_sqlite_checkpointer(
            self.tmp / "ckpt.sqlite")
        self._saver = saver
        self.graph = build_agent_graph(self.llm, self.tools,
                                       checkpointer=saver)
        return self.graph

    async def __aexit__(self, *exc):
        await self._saver.conn.close()


def _ai_text(result: dict) -> str:
    return " ".join(str(m.content) for m in result.get("messages", [])
                    if type(m).__name__ == "AIMessage")


def _tasks_db(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "agent_tasks.sqlite"
    monkeypatch.setenv("AGENT_TASKS_DB", str(db))
    return db


def test_longtask_create_wake_review(tmp_path, monkeypatch):
    """创建 → 快进唤醒 → 复盘:任务档案/提醒/进度全链路,零动钱。"""
    bank_db = _mk_db(tmp_path)
    tasks_db = _tasks_db(tmp_path, monkeypatch)

    async def scenario():
        # ---- 第 1 拍:建任务(router → a_task_new → a_report = 2 次 LLM) ----
        async with _AgentSession(bank_db, tmp_path, [
            '{"intent": "chat"}',
            "任务已建档,复盘提醒已排好。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "lt1"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage(
                     "下个月25号我老婆生日，帮我规划未来30天存出3000块惊喜基金,"
                     "每天省一点,做不到自己调整")]}, cfg)
            assert not any(t.interrupts for t in
                           (await graph.aget_state(cfg)).tasks), "建任务不该有闸门"
            # 任务落库(隔离库)
            from agent import tasks as tasks_mod
            task = tasks_mod.get_active_task(1)
            assert task is not None, "任务应已建档"
            assert task["target_yuan"] == "3000"
            assert task["daily_target_yuan"] == "100"  # 3000/30 天向上取整
            assert len(task["progress"]) == 0
            # 复盘提醒已排(银行库 scheduled_tasks,payload 带 task_id)
            conn = sqlite3.connect(bank_db)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT payload_json FROM scheduled_tasks "
                "WHERE task_type='reminder'").fetchall()
            conn.close()
            assert len(rows) >= 1, "复盘提醒应已排程"
            payloads = [json.loads(r["payload_json"]) for r in rows]
            assert all(p.get("kind") == "task_review" for p in payloads)
            assert all(p.get("task_id") == task["task_id"] for p in payloads)
            assert any(p["title"].startswith("(任务提醒)") for p in payloads)
            # 零动钱:余额不变、无订单
            conn = sqlite3.connect(bank_db)
            assert conn.execute(
                "SELECT balance_cents FROM accounts WHERE id=1"
            ).fetchone()[0] == DEFAULT_BALANCE
            assert conn.execute(
                "SELECT COUNT(*) FROM transfer_orders").fetchone()[0] == 0
            conn.close()

        # ---- 第 2 拍:快进唤醒(横幅文本原样发回 → a_task_review) ----
        async with _AgentSession(bank_db, tmp_path, [
            '{"intent": "chat"}',
            "复盘完成,进度与建议如下。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "lt2"}}
            wake = ("(任务提醒) 存钱任务该复盘了:检查进度并调整计划")
            r2 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage(wake)]}, cfg)
            snap = await graph.aget_state(cfg)
            assert not any(t.interrupts for t in snap.tasks)
            from agent import tasks as tasks_mod
            task = tasks_mod.get_active_task(1)
            assert task is not None
            # 复盘已写进度:当月净结余(临时库无流水 → 0),每日目标按剩余重算
            assert len(task["progress"]) == 1
            assert "复盘" in task["progress"][-1]["note"]
            # 月报确实被查过(审计里有 monthly_report + list_subscriptions)
            conn = sqlite3.connect(bank_db)
            tools_called = {r[0] for r in conn.execute(
                "SELECT tool FROM audit_log").fetchall()}
            conn.close()
            assert "monthly_report" in tools_called
            assert "list_subscriptions" in tools_called
            assert "cancel_subscription" not in tools_called  # 只报不砍

    run(scenario())


def test_longtask_review_requires_active_task(tmp_path, monkeypatch):
    """没有进行中任务时,"(任务提醒)"唤醒不该改道(no_task 兜底)。"""
    bank_db = _mk_db(tmp_path)
    _tasks_db(tmp_path, monkeypatch)

    async def scenario():
        async with _AgentSession(bank_db, tmp_path, [
            '{"intent": "chat"}',
            "当前没有进行中的任务。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "lt3"}}
            r = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage(
                     "(任务提醒) 存钱任务该复盘了:检查进度并调整计划")]}, cfg)
            assert not any(t.interrupts for t in
                           (await graph.aget_state(cfg)).tasks)
            from agent import tasks as tasks_mod
            assert tasks_mod.get_active_task(1) is None

    run(scenario())


def run(coro):
    asyncio.run(coro)


# ----------------------------------------------- 任务库健壮性(2026-10-23)

def test_second_active_task_is_rejected(tmp_path, monkeypatch):
    """每用户一个 active:第二个建档返回 None(并发竞态由唯一索引兜底)。"""
    _tasks_db(tmp_path, monkeypatch)
    from agent import tasks as tasks_mod
    first = tasks_mod.create_task(1, goal="生日存钱", target_yuan="600",
                                  deadline="2026-11-20")
    assert first is not None
    assert tasks_mod.create_task(1, goal="再存一笔", target_yuan="100",
                                 deadline="2026-11-21") is None
    assert tasks_mod.get_active_task(1)["task_id"] == first["task_id"]


def test_duplicate_active_rows_are_collapsed(tmp_path, monkeypatch):
    """老库漂移(索引建立前的竞态年代留下两个 active):下一次建库连接时
    每用户保留最新一条,其余置 cancelled,并把唯一索引建起来。"""
    db = _tasks_db(tmp_path, monkeypatch)
    from agent import tasks as tasks_mod
    tasks_mod.create_task(7, goal="原任务", target_yuan="100",
                          deadline="2026-12-01")
    conn = sqlite3.connect(db)
    try:  # 绕过唯一索引手工塞一个更晚的 active 行,模拟旧版本竞态产物
        conn.execute("DROP INDEX IF EXISTS ux_agent_tasks_one_active")
        conn.execute(
            "INSERT INTO agent_tasks (task_id,user_id,goal,target_yuan,"
            "deadline,status,created_at) VALUES "
            "('dup-1',7,'漂移任务','50','2026-12-01','active','2099-01-01')")
        conn.commit()
    finally:
        conn.close()
    tasks_mod._connect(create=True)  # 触发去重迁移 + 重建索引
    active = tasks_mod.get_active_task(7)
    assert active["goal"] == "漂移任务"  # rowid 更大的新行保留
    conn = sqlite3.connect(db)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE user_id=7 "
            "AND status='active'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_plan_word_does_not_steal_longtask(tmp_path, monkeypatch):
    """硬闸②(2026-10-26 "计划"关键词路由):"存钱计划"里的"计划"不得把它
    劫去联动——存/攒字族的任务轨道改写在关键词改写之前,仍进 task_new。"""
    bank_db = _mk_db(tmp_path)
    tasks_db = _tasks_db(tmp_path, monkeypatch)

    async def scenario():
        async with _AgentSession(bank_db, tmp_path, [
            '{"intent": "chat"}',   # 路由落 chat;存钱字族应改写为 task_new
            "任务已建档,复盘提醒已排好。",
        ]) as graph:
            cfg = {"configurable": {"thread_id": "kw-lt"}}
            r1 = await graph.ainvoke(
                {"auth_user_id": 1,
                 "messages": [HumanMessage("帮我做个存钱计划,30天存3000")]}, cfg)
            # 任务建档成功 = 走的是任务轨道,不是联动(联动会 ask_slot 反问)
            from agent import tasks as tasks_mod
            task = tasks_mod.get_active_task(1)
            assert task is not None, "存钱计划应进长程任务轨道建档"
            assert task["target_yuan"] == "3000"
            assert not any(t.interrupts for t in
                           (await graph.aget_state(cfg)).tasks)

    run(scenario())
