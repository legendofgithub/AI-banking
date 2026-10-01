"""管理后台双库记录器：资金流水库 + 功能修改库（用户昵称为键）。

需求背景（管理员后台，零 LLM）：
- 前端 agent 每次动钱（转账确认/理财申赎/联动购买/管理端调账）→ money_ledger.sqlite
  落一条含"变动前后余额快照"的记录，后端一眼核对"前端取 3000 → 账号少 3000"；
- 前端每次改数据不动钱（联系人/卡片/风险测评/联动计划/AA）→ change_log.sqlite
  落一条功能修改记录；
- 两库以用户昵称为主键维度：昵称取 users.name，可用环境变量 DEMO_USER_NICKNAME 覆盖
  （多用户账号体系是明确的 P3 债，先把键位立好，将来直接沿用）。

路径规则（测试隔离的关键）：记录库跟着银行库走——从 bank_conn 反查其 db 文件
所在目录，在其旁边落 money_ledger.sqlite / change_log.sqlite。临时测试库的记录
自然落在临时目录，绝不污染 data/；环境变量 ADMIN_MONEY_DB / ADMIN_CHANGE_DB
可显式钉死位置。连接按路径缓存。

写入安全铁律：记录器绝不能弄坏业务主流程——所有写库包在 try/except 里，
失败只打印告警。调用点都在业务事务 commit 之后（顺序：业务提交 → audit → record_*）。

操作方区分（operator 字段）：默认 "agent"（前端大模型链路）；
管理端请求用 admin_scope() 上下文把 contextvar 切成 "admin"（与 agent/llm.py
RequestLLM 的请求级覆盖同一套路），管理员自己的每个写操作同样留痕不打折。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .db import now_iso

_MONEY_DB_NAME = "money_ledger.sqlite"
_CHANGE_DB_NAME = "change_log.sqlite"

# 请求级操作方：agent(默认) / admin(管理端请求内)
_operator: ContextVar[str] = ContextVar("bank_recorder_operator", default="agent")


@contextmanager
def admin_scope():
    """管理端调用范围：期间产生的记录 operator='admin'。"""
    token = _operator.set("admin")
    try:
        yield
    finally:
        _operator.reset(token)


def current_operator() -> str:
    return _operator.get()


def _paths_for(bank_conn: sqlite3.Connection) -> tuple[Path, Path]:
    """记录库路径 = 银行库同目录；env 钉死优先；内存库落到系统临时目录。"""
    row = bank_conn.execute("PRAGMA database_list").fetchone()
    db_file = row[2] if row else None
    if db_file:
        base = Path(db_file).parent
    else:
        base = Path(tempfile.gettempdir()) / "bank_records_memory"
    env_m = os.environ.get("ADMIN_MONEY_DB")
    env_c = os.environ.get("ADMIN_CHANGE_DB")
    return (Path(env_m) if env_m else base / _MONEY_DB_NAME,
            Path(env_c) if env_c else base / _CHANGE_DB_NAME)


def money_db_path(bank_conn: sqlite3.Connection) -> Path:
    return _paths_for(bank_conn)[0]


def change_db_path(bank_conn: sqlite3.Connection) -> Path:
    return _paths_for(bank_conn)[1]


def _nickname(conn: sqlite3.Connection, user_id: int) -> str:
    """昵称解析：环境变量覆盖 > users.name。查不到用 'user-{id}' 兜底（记录不等人）。"""
    override = os.environ.get("DEMO_USER_NICKNAME")
    if override:
        return override
    row = conn.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
    return row["name"] if row else f"user-{user_id}"


_CONNS: dict[str, sqlite3.Connection] = {}


def _conn_for(path: Path, kind: str) -> sqlite3.Connection:
    key = f"{kind}:{path}"
    conn = _CONNS.get(key)
    if conn is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        if kind == "money":
            conn.execute(
                """CREATE TABLE IF NOT EXISTS money_records (
                     id          INTEGER PRIMARY KEY,
                     nickname    TEXT NOT NULL,
                     ts          TEXT NOT NULL,
                     tool        TEXT NOT NULL,
                     delta_cents INTEGER NOT NULL,
                     before_cents INTEGER NOT NULL,
                     after_cents  INTEGER NOT NULL,
                     account_id  INTEGER NOT NULL,
                     ref         TEXT NOT NULL DEFAULT '',
                     note        TEXT NOT NULL DEFAULT '',
                     operator    TEXT NOT NULL DEFAULT 'agent'
                   )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_money_nick_ts "
                "ON money_records(nickname, ts)")
        else:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS change_records (
                     id       INTEGER PRIMARY KEY,
                     nickname TEXT NOT NULL,
                     ts       TEXT NOT NULL,
                     category TEXT NOT NULL,
                     action   TEXT NOT NULL,
                     target   TEXT NOT NULL DEFAULT '',
                     detail   TEXT NOT NULL DEFAULT '',
                     operator TEXT NOT NULL DEFAULT 'agent'
                   )""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_change_nick_ts "
                "ON change_records(nickname, ts)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_change_cat ON change_records(category)")
        conn.commit()
        _CONNS[key] = conn
    return conn


def record_money(bank_conn: sqlite3.Connection, user_id: int, *, tool: str,
                 delta_cents: int, before_cents: int, after_cents: int,
                 account_id: int, ref: str = "", note: str = "") -> None:
    """动钱记录：delta 带方向（支出为负/入账为正），before/after 为余额快照。"""
    try:
        conn = _conn_for(money_db_path(bank_conn), "money")
        conn.execute(
            """INSERT INTO money_records
               (nickname, ts, tool, delta_cents, before_cents, after_cents,
                account_id, ref, note, operator)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (_nickname(bank_conn, user_id), now_iso(), tool,
             delta_cents, before_cents, after_cents,
             account_id, ref, note, current_operator()),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 —— 记录失败不拖垮业务
        print(f"[recorder] money 记录失败: {exc}", file=sys.stderr)


def record_change(bank_conn: sqlite3.Connection, user_id: int, *, category: str,
                  action: str, target: str = "", detail: dict | None = None) -> None:
    """功能修改记录：category=contact/card/wealth/linkage/aa/transfer/admin。"""
    try:
        conn = _conn_for(change_db_path(bank_conn), "change")
        conn.execute(
            """INSERT INTO change_records
               (nickname, ts, category, action, target, detail, operator)
               VALUES (?,?,?,?,?,?,?)""",
            (_nickname(bank_conn, user_id), now_iso(), category, action,
             target, json.dumps(detail or {}, ensure_ascii=False), current_operator()),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001 —— 记录失败不拖垮业务
        print(f"[recorder] change 记录失败: {exc}", file=sys.stderr)
