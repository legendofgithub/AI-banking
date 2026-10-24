"""Agent 人设提示词：用户给 Agent 设定的性格/说话方式/温度。

是什么
------
一段 ≤300 字的用户自定文本 + 一个 0-1 的 LLM temperature,存在独立 SQLite
(默认 ``data/agent_persona.sqlite``,env ``AGENT_PERSONA_DB`` 覆盖)。
**按用户隔离**——每个登录用户给自己的 Agent 设人设,互不可见。

为什么单独一个库
----------------
它不是"经验"(memory.py),也不是"任务"(tasks.py),是**用户配置**;
独立小库让两边的演化互不牵扯。硬约束沿用 memory.py/tasks.py 同款:
失败软处理、未登录不写、uid 只认真整数。

默认即现网行为
--------------
未设置时 ``text=""``、``temperature=None``:图里注入空块、不覆盖
``get_llm`` 默认 temperature——一切与没有人设时完全一致。
"""

from __future__ import annotations

import logging
import numbers
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

_log = logging.getLogger("agent.persona")

PERSONA_MAX_CHARS = 300
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 1.0

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS agent_persona (
    user_id      INTEGER PRIMARY KEY,
    persona_text TEXT NOT NULL DEFAULT '',
    temperature  REAL,
    updated_at   TEXT NOT NULL
)
"""


def db_path() -> Path:
    """人设库路径(env AGENT_PERSONA_DB 可覆盖,默认 data/agent_persona.sqlite)。"""
    raw = os.environ.get("AGENT_PERSONA_DB", "").strip()
    if raw:
        return Path(raw)
    return _PROJECT_ROOT / "data" / "agent_persona.sqlite"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _valid_user_id(user_id: Any) -> int | None:
    """与 memory.py 同口径:只认真整数/无损整数字符串(防跨用户串档)。"""
    if isinstance(user_id, bool) or user_id is None:
        return None
    if isinstance(user_id, numbers.Integral):
        return int(user_id) if user_id > 0 else None
    if isinstance(user_id, str):
        digits = user_id.strip().lstrip("+-")
        if digits.isdigit():
            uid = int(user_id)
            return uid if uid > 0 else None
    return None


def _clamp_temperature(value: Any) -> float | None:
    """0-1 之外的值一律视为"未设置"(None),绝不拿越界温度去实例化模型。"""
    try:
        t = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if t < TEMPERATURE_MIN or t > TEMPERATURE_MAX:
        return None
    return t


def _connect(create: bool = True) -> sqlite3.Connection | None:
    """打开人设库;WAL/软处理/纯读不建空文件,与 memory.py 同款。"""
    path = db_path()
    try:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.exists():
            return None
        conn = sqlite3.connect(str(path), timeout=5)
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_SQL)
            conn.commit()
        return conn
    except Exception:  # noqa: BLE001 —— 人设不可用不能拖垮主流程
        _log.warning("人设库连接失败 path=%s create=%s", path, create,
                     exc_info=True)
        return None


def get_persona(user_id: Any) -> dict:
    """读人设;未设置/未登录/库异常一律 ``{"text": "", "temperature": None}``。"""
    empty = {"text": "", "temperature": None}
    uid = _valid_user_id(user_id)
    if uid is None:
        return empty
    conn = _connect(create=False)
    if conn is None:
        return empty
    try:
        row = conn.execute(
            "SELECT persona_text, temperature FROM agent_persona WHERE user_id=?",
            (uid,)).fetchone()
        if row is None:
            return empty
        text = str(row[0] or "").strip()[:PERSONA_MAX_CHARS]
        return {"text": text, "temperature": _clamp_temperature(row[1])}
    except Exception:  # noqa: BLE001
        _log.debug("人设读取失败 uid=%s", uid, exc_info=True)
        return empty
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def set_persona(user_id: Any, text: str, temperature: Any = None) -> bool:
    """写入人设;**空文本=清除**(回到默认不注入)。返回是否写成功。"""
    uid = _valid_user_id(user_id)
    if uid is None or not isinstance(text, str):
        return False
    cleaned = text.strip()[:PERSONA_MAX_CHARS]
    temp = _clamp_temperature(temperature)
    conn = _connect(create=True)
    if conn is None:
        return False
    try:
        if not cleaned:
            conn.execute("DELETE FROM agent_persona WHERE user_id=?", (uid,))
        else:
            conn.execute(
                "INSERT INTO agent_persona (user_id, persona_text, temperature,"
                " updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "persona_text=excluded.persona_text, "
                "temperature=excluded.temperature, "
                "updated_at=excluded.updated_at",
                (uid, cleaned, temp, _now_iso()))
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        _log.warning("人设写入失败 uid=%s", uid, exc_info=True)
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "PERSONA_MAX_CHARS",
    "TEMPERATURE_MAX",
    "TEMPERATURE_MIN",
    "db_path",
    "get_persona",
    "set_persona",
]
