"""跨场景联动测试：生日剧本后端（结构化计划 + 预算锁定两步走 + 时间旅行 + 审计）。

覆盖比赛剧本全链路：
建计划(锁定单不动钱) → get 全景 → 时间旅行触发提醒 → execute 真实扣款 →
计划完成/取消语义 → audit_log 留痕。全部跑临时库，不碰演示库。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta

import pytest

from bank_core.db import init_db
from bank_core.events import EventService
from bank_core.ledger import LedgerService
from bank_core.linkage import LinkageError, LinkageService
from bank_core.money import yuan_to_cents

# 剧本日期相对今天推算（测试永不因真实日期流逝而翻车）：
# 生日 = 今天+4 天（如 2026-09-22 跑即 09-26），前 2 天订购鲜花/蛋糕
_TODAY = date.today()
BIRTHDAY = (_TODAY + timedelta(days=4)).isoformat()
BEFORE_2D = (_TODAY + timedelta(days=2)).isoformat()
NOT_YET = (_TODAY + timedelta(days=1)).isoformat() + "T23:59:59"
ALREADY = (_TODAY + timedelta(days=3)).isoformat() + "T12:00:00"
EVENT_DAY = (_TODAY + timedelta(days=5)).isoformat() + "T09:00:00"

ACTIONS = [
    {"what": "鲜花", "merchant": "花店", "amount_cents": yuan_to_cents("300"),
     "days_before": 2},
    {"what": "蛋糕", "merchant": "蛋糕店", "amount_cents": yuan_to_cents("200"),
     "days_before": 2},
]


@pytest.fixture()
def db(tmp_path):
    """最小演示库：用户陈明 + 账户1活期/账户2理财专户 + 林悦生日事件。"""
    conn = init_db(tmp_path / "linkage.db")
    conn.execute("INSERT INTO users (id,name,phone,created_at) "
                 "VALUES (1,'陈明','13800002233','2026-01-01T00:00:00')")
    conn.executemany(
        "INSERT INTO accounts (id,user_id,type,name,balance_cents,opened_at) "
        "VALUES (?,?,?, ?,?, '2026-01-01T00:00:00')",
        [(1, 1, "checking", "生活主账户", yuan_to_cents("5000")),
         (2, 1, "savings", "理财专户", 0)])
    conn.execute(
        "INSERT INTO user_events (id,user_id,event_type,title,event_date,repeat_yearly,note) "
        "VALUES (1,1,'birthday','林悦的生日',?,0,'老婆，记得提前准备礼物')", (BIRTHDAY,))
    conn.commit()
    yield conn
    conn.close()


def _bal(conn: sqlite3.Connection, acct: int = 1) -> int:
    return conn.execute("SELECT balance_cents FROM accounts WHERE id=?",
                        (acct,)).fetchone()["balance_cents"]


def _mk_plan(conn: sqlite3.Connection) -> dict:
    return LinkageService(conn, 1).create_linkage_plan(
        event_id=1, title="林悦生日联动计划", budget_cents=yuan_to_cents("1000"),
        actions=[dict(a) for a in ACTIONS])


# ------------------------------------------------------------- 建计划：锁定单两步语义

def test_create_plan_lock_order_two_step(db):
    """建计划=只建锁定单不动钱：活期余额不变，单为 pending_confirm、收款人自己。"""
    plan = _mk_plan(db)
    assert plan["status"] == "active" and plan["budget_yuan"] == "1000.00"
    # 两步走铁律：锁定单建了，但活期一分没动
    assert _bal(db) == yuan_to_cents("5000")
    o = db.execute("SELECT * FROM transfer_orders WHERE id=?",
                   (plan["lock_order"]["order_id"],)).fetchone()
    assert o["status"] == "pending_confirm"
    assert o["from_account_id"] == 1
    assert o["to_name"] == "陈明" and o["memo"] == "生日预留"
    assert o["amount_cents"] == yuan_to_cents("1000")
    # 每个动作一条提醒任务，run_at = 生日 - 2 天
    tasks = EventService(db, 1).list_scheduled_tasks()
    assert len(tasks) == 2
    assert all(t["type"] == "reminder" and t["run_at"].startswith(BEFORE_2D)
               for t in tasks)
    assert {t["payload"]["what"] for t in tasks} == {"鲜花", "蛋糕"}
    assert all(t["payload"]["plan_id"] == plan["plan_id"] for t in tasks)


def test_create_plan_rejects_bad_input(db):
    """事件不存在 / 空动作 / 非法金额，确定性拒绝且不留半截数据。"""
    lk = LinkageService(db, 1)
    with pytest.raises(LinkageError, match="事件不存在"):
        lk.create_linkage_plan(999, "x", 1000, [dict(ACTIONS[0])])
    with pytest.raises(LinkageError, match="动作"):
        lk.create_linkage_plan(1, "x", 1000, [])
    with pytest.raises(LinkageError, match="金额"):
        lk.create_linkage_plan(1, "x", 1000,
                               [{"what": "w", "merchant": "m", "amount_cents": 0}])
    assert db.execute("SELECT COUNT(*) c FROM linkage_plans").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) c FROM transfer_orders").fetchone()["c"] == 0


# ------------------------------------------------------------- get 全景

def test_get_plan_reflects_progress(db):
    lk = LinkageService(db, 1)
    plan_id = _mk_plan(db)["plan_id"]
    d = lk.get_linkage_plan(plan_id)
    assert d["status"] == "active" and d["progress"] == "0/2"
    assert d["event"]["date"] == BIRTHDAY and "林悦" in d["event"]["title"]
    assert d["lock_order"]["status"] == "pending_confirm"
    assert d["lock_order"]["amount_yuan"] == "1000.00"
    assert [a["what"] for a in d["actions"]] == ["鲜花", "蛋糕"]
    assert all(a["task"]["status"] == "pending" and not a["done"]
               for a in d["actions"])


# ------------------------------------------------------------- 时间旅行触发提醒

def test_run_due_tasks_as_of_time_travel(db):
    """as_of 时间旅行：拨到生日前 1 天提醒触发且携带 plan_id/action_idx；
    拨到之前不触发；不传 as_of(=now) 也不触发（日期在未来）。"""
    plan_id = _mk_plan(db)["plan_id"]
    ev = EventService(db, 1)
    assert ev.run_due_tasks(as_of=NOT_YET) == []                 # 未到期
    fired = ev.run_due_tasks(as_of=ALREADY)                      # 已过生日前 2 天
    reminders = [f for f in fired if f["kind"] == "reminder"]
    assert len(reminders) == 2
    for f in reminders:
        p = f["payload"]
        assert p["plan_id"] == plan_id and p["action_idx"] in (0, 1)
        assert p["what"] in ("鲜花", "蛋糕") and "该订购" in f["message"]
    # 触发后任务置 done，重复触发不重复弹
    assert ev.run_due_tasks(as_of=EVENT_DAY) == []
    # as_of=None（真实当前时间早于 run_at=今天+2）也不触发——默认行为不受影响
    _mk_plan(db)  # 再建一个计划（其任务 pending）
    assert ev.run_due_tasks() == []


# ------------------------------------------------------------- execute：动钱与幂等

def test_execute_action_spends_and_completes_plan(db):
    """execute 真实扣款：online 购买流水 + 余额快照；全部完成计划置 done。"""
    lk = LinkageService(db, 1)
    plan_id = _mk_plan(db)["plan_id"]
    r0 = lk.execute_linkage_action(plan_id, 0)
    assert r0["what"] == "鲜花" and r0["amount_yuan"] == "300.00"
    assert r0["category"] == "礼品" and r0["plan_status"] == "active"
    assert _bal(db) == yuan_to_cents("4700")
    tx = db.execute("SELECT * FROM transactions WHERE external_ref=?",
                    (f"linkage:{plan_id}:0",)).fetchone()
    assert tx["tx_type"] == "online" and tx["counterparty"] == "花店"
    assert tx["balance_after_cents"] == yuan_to_cents("4700")
    # 已执行动作拒绝重放；对应提醒任务不再 pending
    with pytest.raises(LinkageError, match="已执行"):
        lk.execute_linkage_action(plan_id, 0)
    task0 = db.execute(
        "SELECT status FROM scheduled_tasks WHERE payload_json LIKE ?",
        ('%"action_idx": 0%',)).fetchone()
    assert task0["status"] in ("done", "cancelled")
    # 第二个动作完成 → 计划 done
    r1 = lk.execute_linkage_action(plan_id, 1)
    assert r1["plan_status"] == "done" and _bal(db) == yuan_to_cents("4500")
    assert lk.get_linkage_plan(plan_id)["progress"] == "2/2"
    # done 之后不可再执行/取消
    with pytest.raises(LinkageError, match="done"):
        lk.execute_linkage_action(plan_id, 1)


def test_execute_action_guards(db):
    """余额不足不动钱；下标越界/计划不存在确定性报错。"""
    db.execute("UPDATE accounts SET balance_cents=? WHERE id=1",
               (yuan_to_cents("100"),))
    db.commit()
    lk = LinkageService(db, 1)
    plan_id = _mk_plan(db)["plan_id"]
    with pytest.raises(LinkageError, match="余额不足"):
        lk.execute_linkage_action(plan_id, 0)
    assert _bal(db) == yuan_to_cents("100")
    with pytest.raises(LinkageError, match="越界"):
        lk.execute_linkage_action(plan_id, 9)
    with pytest.raises(LinkageError, match="不存在"):
        lk.execute_linkage_action(999, 0)


# ------------------------------------------------------------- cancel：联动撤销

def test_cancel_plan_cancels_lock_and_tasks(db):
    """取消计划：锁定单一并取消、pending 提醒撤销；重复取消/执行被拒。"""
    lk = LinkageService(db, 1)
    plan_id = _mk_plan(db)["plan_id"]
    r = lk.cancel_linkage_plan(plan_id)
    assert r["status"] == "cancelled"
    assert r["lock_order"]["status"] == "cancelled"
    assert len(r["cancelled_task_ids"]) == 2
    assert db.execute("SELECT status FROM transfer_orders WHERE id=?",
                      (r["lock_order"]["order_id"],)).fetchone()["status"] == "cancelled"
    assert all(t["status"] == "cancelled"
               for t in EventService(db, 1).list_scheduled_tasks("cancelled"))
    d = lk.get_linkage_plan(plan_id)
    assert d["status"] == "cancelled"
    with pytest.raises(LinkageError, match="已取消"):
        lk.cancel_linkage_plan(plan_id)
    with pytest.raises(LinkageError, match="cancelled"):
        lk.execute_linkage_action(plan_id, 0)


def test_cancel_after_lock_executed_keeps_money(db):
    """锁定单已 executed（用户确认过划转）再取消计划：不动已划转的钱。"""
    lk = LinkageService(db, 1)
    led = LedgerService(db, 1)
    plan_id = _mk_plan(db)["plan_id"]
    led.confirm_transfer_order(
        db.execute("SELECT lock_order_id FROM linkage_plans WHERE id=?",
                   (plan_id,)).fetchone()["lock_order_id"])
    r = lk.cancel_linkage_plan(plan_id)
    assert r["lock_order"]["status"] == "executed"   # 已划转不回收
    assert _bal(db, 1) == yuan_to_cents("4000")      # 划走的 1000 不退


# ------------------------------------------------------------- 审计留痕

def test_audit_trail(db):
    """建计划/执行动作 risk=HIGH、取消 risk=MED，全部入 audit_log。"""
    lk = LinkageService(db, 1)
    plan_id = _mk_plan(db)["plan_id"]
    lk.execute_linkage_action(plan_id, 0)
    lk.cancel_linkage_plan(plan_id)
    rows = db.execute(
        "SELECT tool, risk FROM audit_log WHERE tool LIKE 'create_linkage%' "
        "OR tool LIKE 'execute_linkage%' OR tool LIKE 'cancel_linkage%' "
        "OR tool='run_due_tasks'").fetchall()
    by_tool = {(r["tool"], r["risk"]) for r in rows}
    assert ("create_linkage_plan", "HIGH") in by_tool
    assert ("execute_linkage_action", "HIGH") in by_tool
    assert ("cancel_linkage_plan", "MED") in by_tool
    # 审计参数里能还原计划要点（可追溯）
    a = db.execute("SELECT args_json FROM audit_log WHERE tool='create_linkage_plan'"
                   ).fetchone()["args_json"]
    assert json.loads(a)["budget_cents"] == yuan_to_cents("1000")
