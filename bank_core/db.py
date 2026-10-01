"""SQLite 存储层：建库、建表、公共工具。

金额字段一律以"分"（INTEGER）存储；时间一律存 ISO8601 本地时间字符串。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# 数据库文件位置：默认项目根下 data/bank.db，可用环境变量覆盖
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "bank.db"


def default_db_path() -> Path:
    """数据库路径延迟解析：调用时才读 BANK_CORE_DB 环境变量。

    踩坑：原先 DB_PATH 在 import 时求值环境变量，进程内后设的 BANK_CORE_DB
    不生效（组装根延迟解析原则：路径在使用点解析，不在导入点固化）。
    """
    return Path(os.environ.get("BANK_CORE_DB", _DEFAULT_DB))


# 兼容旧导出：仅指回默认路径、不再读环境变量；要环境变量语义请用 default_db_path()
DB_PATH = _DEFAULT_DB

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,          -- 真实姓名（注册时实名录入）
    phone      TEXT NOT NULL,
    id_card    TEXT NOT NULL DEFAULT '',  -- 身份证号（注册必录；老用户为空串）
    email      TEXT NOT NULL DEFAULT '',  -- 邮箱（注册选填）
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    type          TEXT NOT NULL CHECK (type IN ('checking', 'savings')),
    name          TEXT NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'CNY',
    balance_cents INTEGER NOT NULL DEFAULT 0 CHECK (balance_cents >= 0),
    opened_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    id       INTEGER PRIMARY KEY,
    user_id  INTEGER NOT NULL REFERENCES users(id),
    name     TEXT NOT NULL,
    phone    TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'friend',
    note     TEXT NOT NULL DEFAULT '',
    UNIQUE (user_id, phone)
);

CREATE TABLE IF NOT EXISTS cards (
    id                INTEGER PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users(id),
    account_id        INTEGER REFERENCES accounts(id),
    card_no_masked    TEXT NOT NULL,
    card_type         TEXT NOT NULL CHECK (card_type IN ('debit', 'credit')),
    status            TEXT NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'locked', 'lost', 'frozen')),
    daily_limit_cents INTEGER NOT NULL DEFAULT 5000000,
    per_tx_limit_cents INTEGER NOT NULL DEFAULT 2000000,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id                  INTEGER PRIMARY KEY,
    account_id          INTEGER NOT NULL REFERENCES accounts(id),
    ts                  TEXT NOT NULL,
    direction           TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    amount_cents        INTEGER NOT NULL CHECK (amount_cents > 0),
    balance_after_cents INTEGER NOT NULL,
    tx_type             TEXT NOT NULL CHECK (tx_type IN (
                        'salary', 'transfer_in', 'transfer_out', 'pos', 'online',
                        'fee', 'interest', 'repayment', 'subscribe', 'redeem',
                        'aa_collect', 'split')),
    counterparty        TEXT NOT NULL DEFAULT '',
    counterparty_tail   TEXT NOT NULL DEFAULT '',   -- 对方账号尾号（演示用）
    category            TEXT NOT NULL DEFAULT '其他',
    channel             TEXT NOT NULL DEFAULT 'app',
    memo                TEXT NOT NULL DEFAULT '',
    card_id             INTEGER REFERENCES cards(id),
    external_ref        TEXT UNIQUE                  -- 幂等键（转账防重复执行）
);
CREATE INDEX IF NOT EXISTS idx_tx_account_ts ON transactions(account_id, ts);
CREATE INDEX IF NOT EXISTS idx_tx_counterparty ON transactions(counterparty, amount_cents);

CREATE TABLE IF NOT EXISTS transfer_orders (
    id               INTEGER PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    from_account_id  INTEGER NOT NULL REFERENCES accounts(id),
    to_contact_id    INTEGER REFERENCES contacts(id),
    to_name          TEXT NOT NULL,
    to_account_tail  TEXT NOT NULL DEFAULT '',
    amount_cents     INTEGER NOT NULL CHECK (amount_cents > 0),
    memo             TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending_confirm'
                     CHECK (status IN ('pending_confirm', 'scheduled',
                                       'executed', 'cancelled', 'failed')),
    fail_reason      TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    scheduled_at     TEXT,
    executed_at      TEXT,
    idempotency_key  TEXT UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS split_bills (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    title       TEXT NOT NULL,
    total_cents INTEGER NOT NULL CHECK (total_cents > 0),
    status      TEXT NOT NULL DEFAULT 'collecting'
                CHECK (status IN ('collecting', 'settled', 'cancelled')),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS split_bill_items (
    id          INTEGER PRIMARY KEY,
    bill_id     INTEGER NOT NULL REFERENCES split_bills(id),
    contact_name TEXT NOT NULL,
    share_cents INTEGER NOT NULL CHECK (share_cents > 0),
    paid        INTEGER NOT NULL DEFAULT 0,
    paid_at     TEXT
);

CREATE TABLE IF NOT EXISTS wealth_products (
    id                    INTEGER PRIMARY KEY,
    code                  TEXT UNIQUE NOT NULL,
    name                  TEXT NOT NULL,
    p_type                TEXT NOT NULL CHECK (p_type IN (
                          'money_fund', 'bond', 'mixed', 'gold', 'deposit')),
    risk_level            INTEGER NOT NULL CHECK (risk_level BETWEEN 1 AND 5),
    expected_return_bps   INTEGER NOT NULL,   -- 七日年化/业绩基准，万分比
    min_subscribe_cents   INTEGER NOT NULL,
    lock_days             INTEGER NOT NULL DEFAULT 0,
    subscription_fee_bps  INTEGER NOT NULL DEFAULT 0,
    redemption_fee_bps    INTEGER NOT NULL DEFAULT 0,
    intro                 TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS wealth_holdings (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    product_id      INTEGER NOT NULL REFERENCES wealth_products(id),
    principal_cents INTEGER NOT NULL,
    est_value_cents INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'holding'
                    CHECK (status IN ('holding', 'redeemed')),
    subscribed_at   TEXT NOT NULL,
    redeemed_at     TEXT
);

CREATE TABLE IF NOT EXISTS risk_profiles (
    user_id      INTEGER PRIMARY KEY REFERENCES users(id),
    answers_json TEXT NOT NULL DEFAULT '{}',
    score        INTEGER NOT NULL,
    level        TEXT NOT NULL CHECK (level IN ('C1', 'C2', 'C3', 'C4', 'C5')),
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS merchants (
    id            INTEGER PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,
    category      TEXT NOT NULL,
    is_subscription INTEGER NOT NULL DEFAULT 0,
    period_days   INTEGER
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    merchant_name   TEXT NOT NULL,
    category        TEXT NOT NULL DEFAULT '订阅',
    amount_cents    INTEGER NOT NULL,
    period_days     INTEGER NOT NULL,
    next_charge_date TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'cancelled')),
    last_tx_id      INTEGER REFERENCES transactions(id),
    detected_at     TEXT NOT NULL,
    UNIQUE (user_id, merchant_name)
);

CREATE TABLE IF NOT EXISTS user_events (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    event_type    TEXT NOT NULL CHECK (event_type IN (
                  'birthday', 'anniversary', 'payday', 'bill_day', 'custom')),
    title         TEXT NOT NULL,
    event_date    TEXT NOT NULL,      -- YYYY-MM-DD
    repeat_yearly INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id           INTEGER PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    task_type    TEXT NOT NULL CHECK (task_type IN (
                 'scheduled_transfer', 'reminder')),
    payload_json TEXT NOT NULL,
    run_at       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending', 'done', 'cancelled', 'failed')),
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY,
    ts        TEXT NOT NULL,
    tool      TEXT NOT NULL,
    args_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    risk      TEXT NOT NULL DEFAULT 'READ'
);

-- 跨场景联动计划（比赛剧本：生日 → 锁定预算 → 提前订购鲜花/蛋糕）。
-- CREATE TABLE IF NOT EXISTS 追加：老库直接 executescript 升级，不动存量数据。
CREATE TABLE IF NOT EXISTS linkage_plans (
    id            INTEGER PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    event_id      INTEGER REFERENCES user_events(id),
    title         TEXT NOT NULL,
    budget_cents  INTEGER NOT NULL CHECK (budget_cents > 0),
    lock_order_id INTEGER REFERENCES transfer_orders(id),  -- 预算锁定转账单（两步走，只建单不动钱）
    actions_json  TEXT NOT NULL,   -- [{"type":"order","what":"鲜花","merchant":"花店",
                                   --   "amount_cents":30000,"days_before":2,
                                   --   "done":false,"done_at":null,"task_id":...}, ...]
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'done', 'cancelled')),
    created_at    TEXT NOT NULL
);

-- 账号体系（注册/登录会话/支付密码；口径与 bank_core/auth_core.py 一致。
-- 放进 SCHEMA 是让所有 init_db 路径——mcp 子进程/管理台/测试——自动获得）。
CREATE TABLE IF NOT EXISTS auth_users (
    user_id         INTEGER PRIMARY KEY REFERENCES users(id),
    identifier      TEXT NOT NULL UNIQUE,
    identifier_type TEXT NOT NULL CHECK (identifier_type IN ('phone','email')),
    password_hash   TEXT NOT NULL,
    pay_hash        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_login_at   TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    """本地时间 ISO 字符串（秒级），全库统一时间格式。"""
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def connect(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    """打开连接；默认启用外键与 WAL。
    check_same_thread=False：MCP 服务端会把工具调用放进线程池执行；
    CPython 的 sqlite3 为 serialized 线程模式，单连接跨线程安全（演示单用户场景足够）。
    """
    path = Path(db_path) if db_path else default_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    migrate_users_kyc(conn)
    conn.commit()
    return conn


def migrate_users_kyc(conn: sqlite3.Connection) -> None:
    """老库补列：users 加 id_card/email（CREATE TABLE IF NOT EXISTS 不改已存在的表）。

    供 init_db 之外的直连路径复用（agent 账号库 auth_conn、管理台 connect），
    幂等：列已存在时 PRAGMA 查得即跳过。
    """
    # PRAGMA table_info 第 2 列是列名;用下标取,兼容 row_factory 非 Row 的连接
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if not cols:
        return  # 库还没建 users 表（executescript 尚未跑过），交给 init_db
    for col in ("id_card", "email"):
        if col not in cols:
            conn.execute(
                f"ALTER TABLE users ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
    conn.commit()


def reset_db(db_path: str | os.PathLike | None = None) -> None:
    """删除数据库文件（含 WAL/SHM），供重新播种。"""
    path = Path(db_path) if db_path else default_db_path()
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()


def audit(conn: sqlite3.Connection, tool: str, args: dict, result, risk: str = "READ") -> None:
    """工具调用留痕：参数与结果摘要全部入库。"""
    conn.execute(
        "INSERT INTO audit_log (ts, tool, args_json, result_json, risk) VALUES (?,?,?,?,?)",
        (now_iso(), tool, json.dumps(args, ensure_ascii=False, default=str),
         json.dumps(result, ensure_ascii=False, default=str), risk),
    )
    conn.commit()
