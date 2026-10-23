"""长程任务存储：跨天的目标档案与进度(比赛演示:30 天生日存钱计划)。

为什么单独一个库
----------------
任务档案是"跨会话存活的编排层状态"——图状态回合结束即逝,memory.py 每个
模块只留最新一条摘要,都装不下"带里程碑和复盘历史的进行中任务"。落在独立
SQLite(默认 data/agent_tasks.sqlite,环境变量 AGENT_TASKS_DB 覆盖),
不改 bank.db 表结构、不加 MCP 工具、不改工具计数。

任务无关性(迭代计划"轨道 vs 剧本")
--------------------------------
本模块只定义**通用任务容器**(目标/期限/进度/复盘历史),不含任何"生日/存钱"
业务语义——那是剧本层(graph 里组装)的事。加新剧本不改本模块。

三条硬约束(与 memory.py 同款)
------------------------------
1. 失败软处理:任何异常吞掉返回空值,绝不阻断主流程;
2. 未登录不写:user_id 非法(非正整数/浮点)一律拒绝;
3. 每用户同时只允许一个 active 任务(演示口径,查询确定性)。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

_log = logging.getLogger("agent.tasks")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id          TEXT PRIMARY KEY,
    user_id          INTEGER NOT NULL,
    goal             TEXT NOT NULL,
    target_yuan      TEXT NOT NULL,
    daily_target_yuan TEXT NOT NULL DEFAULT '0',
    deadline         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    saved_yuan       TEXT NOT NULL DEFAULT '0',
    next_review_at   TEXT NOT NULL DEFAULT '',
    progress_json    TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL
)
"""

# "每用户同时只允许一个 active 任务"由数据库约束兜底:check-then-insert 在并发
# 请求下有窗口能造出两个 active(破坏演示确定性口径),partial unique index
# 把这个口子彻底封死。
_CREATE_ACTIVE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_tasks_one_active "
    "ON agent_tasks(user_id) WHERE status='active'"
)


def db_path() -> Path:
    raw = os.environ.get("AGENT_TASKS_DB", "").strip()
    if raw:
        return Path(raw)
    return _PROJECT_ROOT / "data" / "agent_tasks.sqlite"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _valid_user_id(user_id: Any) -> int | None:
    """与 memory.py 同口径:未登录/非法一律 None;浮点拒绝(防静默截断串户)。"""
    if isinstance(user_id, bool) or user_id is None or isinstance(user_id, float):
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


def _connect(create: bool = True) -> sqlite3.Connection | None:
    """打开任务库;WAL/写失败日志与 memory.py 同款(纯读不建空库文件)。"""
    try:
        path = db_path()
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.exists():
            return None
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(_CREATE_SQL)
            try:
                conn.execute(_CREATE_ACTIVE_INDEX_SQL)
            except sqlite3.IntegrityError:
                # 老库可能已有重复 active(建索引前的竞态年代留下的):
                # 每用户保留最新一条,其余置 cancelled 后重建索引。
                conn.execute(
                    """UPDATE agent_tasks SET status='cancelled'
                       WHERE status='active' AND rowid NOT IN (
                           SELECT MAX(rowid) FROM agent_tasks
                           WHERE status='active' GROUP BY user_id)""")
                conn.execute(_CREATE_ACTIVE_INDEX_SQL)
            conn.commit()
        return conn
    except Exception:  # noqa: BLE001 —— 任务库不可用不能拖垮主流程
        _log.warning("任务库连接失败 path=%s create=%s", path, create, exc_info=True)
        return None


def _row_to_task(r: sqlite3.Row) -> dict:
    try:
        progress = json.loads(r["progress_json"])
    except Exception:  # noqa: BLE001
        progress = []
    return {
        "task_id": r["task_id"], "user_id": r["user_id"],
        "goal": r["goal"], "target_yuan": r["target_yuan"],
        "daily_target_yuan": r["daily_target_yuan"],
        "deadline": r["deadline"], "status": r["status"],
        "saved_yuan": r["saved_yuan"],
        "next_review_at": r["next_review_at"],
        "progress": progress if isinstance(progress, list) else [],
        "created_at": r["created_at"],
    }


def new_task_id() -> str:
    import uuid
    return "task-" + uuid.uuid4().hex[:10]


def create_task(user_id: Any, *, goal: str, target_yuan: str, deadline: str,
                daily_target_yuan: str = "0",
                review_dates: list[str] | None = None) -> dict | None:
    """创建任务档案(每个用户同时只允许一个 active——先到先得)。

    review_dates 仅供调用方排提醒用,本函数不碰 scheduled_tasks
    (提醒走既有 bank_core.schedule_reminder,分层不越界)。
    返回创建后的任务 dict;已有 active 任务/参数非法/写库失败 → None。
    """
    uid = _valid_user_id(user_id)
    if uid is None or not goal.strip() or not target_yuan.strip() \
            or not deadline.strip():
        return None
    if get_active_task(uid) is not None:
        return None
    conn = _connect(create=True)
    if conn is None:
        return None
    task = {
        "task_id": new_task_id(), "user_id": uid,
        "goal": str(goal).strip()[:200],
        "target_yuan": str(target_yuan), "daily_target_yuan": str(daily_target_yuan),
        "deadline": str(deadline), "status": "active",
        "saved_yuan": "0", "next_review_at": (review_dates or [""])[0],
        "progress": [], "created_at": _now_iso(),
    }
    try:
        conn.execute(
            """INSERT INTO agent_tasks
               (task_id,user_id,goal,target_yuan,daily_target_yuan,deadline,
                status,saved_yuan,next_review_at,progress_json,created_at)
               VALUES (?,?,?,?,?,?,'active','0',?,?,?)""",
            (task["task_id"], uid, task["goal"], task["target_yuan"],
             task["daily_target_yuan"], task["deadline"],
             task["next_review_at"], json.dumps([], ensure_ascii=False),
             task["created_at"]))
        conn.commit()
        return task
    except sqlite3.IntegrityError:
        # 并发建档撞上"每用户一个 active"唯一索引:按"已有 active 任务"处理,
        # 与函数开头的 get_active_task 检查殊途同归(那个检查在并发下有窗口)。
        return None
    except Exception:  # noqa: BLE001
        _log.warning("任务建档失败 uid=%s goal=%s", uid, task["goal"], exc_info=True)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def get_task(task_id: Any) -> dict | None:
    conn = _connect(create=False)
    if conn is None:
        return None
    try:
        r = conn.execute("SELECT * FROM agent_tasks WHERE task_id=?",
                         (str(task_id),)).fetchone()
        return _row_to_task(r) if r else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def get_active_task(user_id: Any) -> dict | None:
    """该用户当前进行中的任务(至多一个;没有/未登录/库异常 → None)。"""
    uid = _valid_user_id(user_id)
    if uid is None:
        return None
    conn = _connect(create=False)
    if conn is None:
        return None
    try:
        r = conn.execute(
            "SELECT * FROM agent_tasks WHERE user_id=? AND status='active' "
            "ORDER BY created_at DESC LIMIT 1", (uid,)).fetchone()
        return _row_to_task(r) if r else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def update_progress(user_id: Any, task_id: Any, *, saved_yuan: str,
                    note: str, next_review_at: str | None = None,
                    daily_target_yuan: str | None = None) -> dict | None:
    """追加一次复盘:更新已省金额/每日目标/下次复盘时间,进度历史 +1 条。

    返回更新后的任务;任务不存在/未登录/写失败 → None。进度历史封顶 30 条。
    """
    uid = _valid_user_id(user_id)
    if uid is None:
        return None
    conn = _connect(create=False)
    if conn is None:
        return None
    try:
        r = conn.execute(
            "SELECT * FROM agent_tasks WHERE task_id=? AND user_id=?",
            (str(task_id), uid)).fetchone()
        if r is None:
            return None
        task = _row_to_task(r)
        progress = task["progress"]
        progress.append({"date": _now_iso()[:10], "saved_yuan": str(saved_yuan),
                         "note": str(note)[:200]})
        task["saved_yuan"] = str(saved_yuan)
        task["progress"] = progress[-30:]
        if next_review_at:
            task["next_review_at"] = str(next_review_at)
        if daily_target_yuan:
            task["daily_target_yuan"] = str(daily_target_yuan)
        conn.execute(
            """UPDATE agent_tasks SET saved_yuan=?, daily_target_yuan=?,
               next_review_at=?, progress_json=? WHERE task_id=?""",
            (task["saved_yuan"], task["daily_target_yuan"],
             task["next_review_at"],
             json.dumps(task["progress"], ensure_ascii=False), str(task_id)))
        conn.commit()
        return task
    except Exception:  # noqa: BLE001
        _log.warning("任务进度更新失败 uid=%s task_id=%s", uid, task_id, exc_info=True)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def finish_task(user_id: Any, task_id: Any,
                status: str = "done") -> dict | None:
    """终结任务(status: done|failed|cancelled)。"""
    uid = _valid_user_id(user_id)
    if uid is None or status not in ("done", "failed", "cancelled"):
        return None
    conn = _connect(create=False)
    if conn is None:
        return None
    try:
        conn.execute(
            "UPDATE agent_tasks SET status=? WHERE task_id=? AND user_id=?",
            (status, str(task_id), uid))
        conn.commit()
        return get_task(task_id)
    except Exception:  # noqa: BLE001
        _log.warning("任务终结失败 uid=%s task_id=%s status=%s",
                     uid, task_id, status, exc_info=True)
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def next_review_date(start: date | None = None, days: int = 7) -> str:
    """复盘日期 = start + days(确定性日期算术,绝不交给 LLM)。"""
    base = start or date.today()
    return (base + timedelta(days=days)).isoformat()


__all__ = [
    "create_task", "db_path", "finish_task", "get_active_task", "get_task",
    "new_task_id", "next_review_date", "update_progress",
]
