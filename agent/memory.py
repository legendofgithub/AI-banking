"""Agent 长期记忆：跨会话的用户偏好、上次计划结果与失败原因。

为什么单独一个库
----------------
现有 ``data/bank.db`` 是银行事实库（账户/流水/订阅/持仓），
``data/agent_threads.sqlite`` 是会话库（消息/pending 卡片），两者都不适合放
"下次规划要用的经验"。本模块按迭代计划 §6 落在独立 SQLite
（默认 ``data/agent_memory.sqlite``，可用环境变量 ``AGENT_MEMORY_DB`` 覆盖），
**不改 bank.db 表结构、不新增 MCP 工具、不改工具计数**。

记住的不是聊天记录，是三类"下次有用"的短摘要：
- ``profile``      用户画像：预算偏好、风险等级、偏好的产品类型、不能动的固定支出；
- ``bill``/``wealth``/``birthday``  技能经验：上次关注什么、给了什么方案、
  结果是成功还是失败、**被用户拒绝的方案**（不要再默认推荐）；
- ``plan_history`` 最近 N 条计划：plan_id/目标/用了哪些技能/结果/失败原因/计划 diff。

三条硬约束（迭代计划 §6.3、§14）
--------------------------------
1. **失败软处理**：读写异常一律吞掉返回空值，绝不阻断主流程；
2. **未登录不写**：``user_id`` 为空/非正整数时读写都不落库；
3. **只能做建议**：记忆不替代实时余额/限额/持仓校验，执行前仍读实时事实。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

_log = logging.getLogger("agent.memory")

# 记忆模块枚举：写库前做白名单校验，防止把任意字符串当成 module 污染库
MEMORY_MODULES: frozenset[str] = frozenset({
    "profile",        # 用户画像
    "bill",           # 账单分析经验
    "wealth",         # 理财分析经验
    "birthday",       # 生日预算计划经验
    "plan_history",   # 最近计划历史（短摘要队列）
})

# plan_history 只留最近几条计划，避免库无限增长
PLAN_HISTORY_LIMIT = 5

# 检阅视图(参考 Anthropic memory tool 的文件式记忆):每个模块 = 一个
# "记忆文件",label 是它的展示名,渲染顺序 profile 画像置顶。
_MODULE_LABELS: dict[str, str] = {
    "profile": "用户画像",
    "bill": "账单经验",
    "wealth": "理财经验",
    "birthday": "生日计划经验",
    "plan_history": "计划历史",
}
_MODULE_ORDER: tuple[str, ...] = (
    "profile", "bill", "wealth", "birthday", "plan_history")

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS agent_memory (
    user_id      INTEGER NOT NULL,
    module       TEXT NOT NULL,
    content_json TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, module)
)
"""


def db_path() -> Path:
    """记忆库路径（环境变量 AGENT_MEMORY_DB 可覆盖，默认 data/agent_memory.sqlite）。"""
    raw = os.environ.get("AGENT_MEMORY_DB", "").strip()
    if raw:
        return Path(raw)
    return _PROJECT_ROOT / "data" / "agent_memory.sqlite"


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _valid_user_id(user_id: Any) -> int | None:
    """规范化 user_id：未登录/非法一律 None（调用方据此跳过读写）。

    只认整数或纯数字字符串：**浮点数一律拒绝**（``int(3.5)`` 会静默截断成 3,
    把 11 号用户的经验写到 3 号头上就是跨用户记忆污染）。
    """
    if isinstance(user_id, bool) or user_id is None:
        return None
    if isinstance(user_id, float):
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    return uid if uid > 0 else None


def _connect(create: bool = True) -> sqlite3.Connection | None:
    """打开记忆库；任何异常都返回 None（调用方按"记忆不可用"处理）。

    - WAL：默认 journal 模式下并发写会锁库 5 秒后被"软处理"吞掉，等于记忆
      **静默丢失**；WAL 让读写不互斥（*.sqlite-wal/-shm 已被 .gitignore 拦截）。
    - 纯读（create=False）先判文件存在：sqlite3.connect 对不存在的路径也会
      建出空库文件，读路径不该留这种副作用。
    """
    path = db_path()
    try:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.exists():
            return None
        conn = sqlite3.connect(str(path), timeout=5)
        if create:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute(_CREATE_SQL)
            conn.commit()
        return conn
    except Exception:  # noqa: BLE001 —— 记忆不可用不能连带炸掉主流程
        _log.warning("记忆库连接失败 path=%s create=%s", path, create, exc_info=True)
        return None


# ------------------------------------------------------------------ 读

def recall(user_id: Any, module: str) -> dict | None:
    """读某个用户某模块的记忆；无记录/未登录/库异常一律 None。"""
    uid = _valid_user_id(user_id)
    if uid is None or module not in MEMORY_MODULES:
        return None
    conn = _connect(create=False)
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT content_json FROM agent_memory WHERE user_id=? AND module=?",
            (uid, module)).fetchone()
        if row is None:
            return None
        data = json.loads(row[0])
        return dict(data) if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def recall_all(user_id: Any) -> dict[str, dict]:
    """一次读出该用户的全部模块记忆（缺失的模块不出现在结果里）。"""
    uid = _valid_user_id(user_id)
    if uid is None:
        return {}
    conn = _connect(create=False)
    if conn is None:
        return {}
    try:
        rows = conn.execute(
            "SELECT module, content_json FROM agent_memory WHERE user_id=?",
            (uid,)).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    out: dict[str, dict] = {}
    for module, raw in rows:
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            out[str(module)] = data
    return out


def remember(user_id: Any, module: str, content: dict) -> bool:
    """UPSERT 一条记忆（每个模块只保留最新一条）。返回是否写成功。"""
    uid = _valid_user_id(user_id)
    if uid is None or module not in MEMORY_MODULES or not isinstance(content, dict):
        return False
    try:
        payload = json.dumps(content, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        _log.warning("记忆序列化失败 uid=%s module=%s", uid, module, exc_info=True)
        return False
    conn = _connect(create=True)
    if conn is None:
        return False
    try:
        conn.execute(
            "INSERT INTO agent_memory (user_id, module, content_json, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, module) DO UPDATE SET "
            "content_json=excluded.content_json, updated_at=excluded.updated_at",
            (uid, module, payload, _now_iso()))
        conn.commit()
        return True
    except Exception:  # noqa: BLE001 —— 软处理但必须留痕，否则现场静默丢记忆无法排查
        _log.warning("记忆写入失败 uid=%s module=%s", uid, module, exc_info=True)
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def merge(user_id: Any, module: str, patch: dict) -> dict:
    """在既有记忆上打补丁（浅合并一层），返回合并后的内容（写失败也返回合并值）。

    - 读 + 合并 + 写在**同一连接同一事务**（BEGIN IMMEDIATE）里完成：并发回合
      同时 merge 不再是"各自读到旧值、后写覆盖先写"的丢更新竞态。
    - 删键语义（参考 Anthropic memory tool 的 str_replace/delete）：patch 里值
      为 ``None`` 的键表示**删除该键**（合并结果里不保留），用于清理过期字段。
    """
    uid = _valid_user_id(user_id)
    if not isinstance(patch, dict):
        patch = {}
    if uid is None or module not in MEMORY_MODULES:
        return dict(patch)
    conn = _connect(create=True)
    if conn is None:
        return dict(patch)
    base: dict = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT content_json FROM agent_memory WHERE user_id=? AND module=?",
            (uid, module)).fetchone()
        if row is not None:
            try:
                data = json.loads(row[0])
                if isinstance(data, dict):
                    base = data
            except Exception:  # noqa: BLE001 —— 旧数据损坏按空底处理
                base = {}
        merged = {k: v for k, v in {**base, **patch}.items() if v is not None}
        conn.execute(
            "INSERT INTO agent_memory (user_id, module, content_json, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, module) DO UPDATE SET "
            "content_json=excluded.content_json, updated_at=excluded.updated_at",
            (uid, module, json.dumps(merged, ensure_ascii=False, default=str),
             _now_iso()))
        conn.commit()
        return merged
    except Exception:  # noqa: BLE001
        _log.warning("记忆合并写入失败 uid=%s module=%s", uid, module, exc_info=True)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {k: v for k, v in {**base, **patch}.items() if v is not None}
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def push_plan_history(user_id: Any, entry: dict) -> list[dict]:
    """追加一条计划历史（保留最近 PLAN_HISTORY_LIMIT 条，最新在前）。

    去重 + 写入在同一事务里完成（并发追加不再互相顶掉）。

    entry 建议字段：plan_id/goal/skills/result/reason/plan_version/updated_at。
    """
    uid = _valid_user_id(user_id)
    if uid is None or not isinstance(entry, dict):
        return []
    item = dict(entry)
    item.setdefault("at", _now_iso())
    conn = _connect(create=True)
    if conn is None:
        return [item]
    items: list[dict] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT content_json FROM agent_memory "
            "WHERE user_id=? AND module='plan_history'", (uid,)).fetchone()
        if row is not None:
            try:
                prev = json.loads(row[0])
                if isinstance(prev, dict):
                    items = [i for i in (prev.get("items") or [])
                             if isinstance(i, dict)]
            except Exception:  # noqa: BLE001
                items = []
        items = [i for i in items
                 if str(i.get("plan_id") or "") != str(item.get("plan_id") or "")]
        items.insert(0, item)
        items = items[:PLAN_HISTORY_LIMIT]
        conn.execute(
            "INSERT INTO agent_memory (user_id, module, content_json, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(user_id, module) DO UPDATE SET "
            "content_json=excluded.content_json, updated_at=excluded.updated_at",
            (uid, "plan_history",
             json.dumps({"items": items}, ensure_ascii=False, default=str),
             _now_iso()))
        conn.commit()
        return items
    except Exception:  # noqa: BLE001
        _log.warning("计划历史写入失败 uid=%s", uid, exc_info=True)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return [item]
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def forget(user_id: Any, module: str | None = None) -> bool:
    """删除某用户某个模块的记忆；module 为空则清空该用户全部记忆（管理用）。"""
    uid = _valid_user_id(user_id)
    if uid is None:
        return False
    conn = _connect(create=False)
    if conn is None:
        return False
    try:
        if module:
            conn.execute("DELETE FROM agent_memory WHERE user_id=? AND module=?",
                         (uid, module))
        else:
            conn.execute("DELETE FROM agent_memory WHERE user_id=?", (uid,))
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        _log.warning("记忆删除失败 uid=%s module=%s", uid, module, exc_info=True)
        return False
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _day_of(iso: Any) -> str:
    """ISO 时间戳 → 'MM-DD'（解析不出返回空串）。

    时间是记忆的一等公民（Zep 的核心主张）："上次你保留了订阅"远不如
    "上次(10-15)你保留了订阅"可信，也给用户核对面。
    """
    s = str(iso or "")
    return s[5:10] if len(s) >= 10 else ""


def summarize_for_plan(user_id: Any, modules: tuple[str, ...]) -> list[str]:
    """把若干模块的记忆压成一两句"人话"，供计划卡的 memory_used 展示。

    纯确定性字符串拼接，不调 LLM；没有任何记忆时返回空列表。
    profile 画像只补**当前技能没覆盖到**的信息（如只做账单分析时补一句
    "你是稳健型投资者"），避免与 bill/wealth 模块提示重复。
    """
    out: list[str] = []
    memory = recall_all(user_id)
    if "bill" in modules and "bill" in memory:
        cat = str((memory["bill"].get("top_category") or "") or "")
        if cat:
            when = _day_of(memory["bill"].get("at"))
            out.append(f"上次账单关注「{cat}」" + (f"({when})" if when else ""))
    if "wealth" in modules and "wealth" in memory:
        pref = str((memory["wealth"].get("preferred_type") or "") or "")
        level = str((memory["wealth"].get("risk_level") or "") or "")
        if pref:
            out.append(f"上次偏好「{pref}」理财")
        if level:
            out.append(f"上次风险等级 {level}")
    if "birthday" in modules and "birthday" in memory:
        b = memory["birthday"]
        budget = str(b.get("last_budget_yuan") or "")
        if budget:
            when = _day_of(b.get("at"))
            out.append(f"上次生日预算 ¥{budget}" + (f"({when})" if when else ""))
        if str(b.get("rejected_cut") or "") == "true" or b.get("rejected_cut") is True:
            out.append("上次保留订阅未取消")
        elif str(b.get("accepted_cut_merchant") or ""):
            out.append(f"上次接受取消「{b['accepted_cut_merchant']}」")
    prof = memory.get("profile") or {}
    if prof:
        if "wealth" not in modules:
            level = str(prof.get("risk_level") or "")
            if level:
                out.append(f"用户是{level}型投资者")
        if "bill" not in modules:
            cat = str(prof.get("top_category") or "")
            if cat:
                out.append(f"常在「{cat}」上消费")
    return out


def module_label(module: str) -> str:
    """模块展示名（检阅入口用；未知模块原样返回）。"""
    return _MODULE_LABELS.get(module, module)


def render_module(module: str, content: dict) -> list[str]:
    """把一个模块的记忆渲染成人类可读的中文行。

    Anthropic memory tool 的 ``view`` 命令等价物：记忆不是黑盒 JSON，
    而是随时可翻阅的"文件内容"。纯确定性拼接、不调 LLM；空值一律跳过，
    未知模块（未来新增白名单时）逐键列出非空项兜底。
    """
    if not isinstance(content, dict) or not content:
        return []
    out: list[str] = []
    if module == "profile":
        pairs = (("risk_level", "风险偏好"), ("preferred_type", "偏好产品"),
                 ("top_category", "消费大头"), ("holding_count", "持仓数"),
                 ("subscription_count", "订阅数"))
        for key, label in pairs:
            v = content.get(key)
            if v is not None and str(v) != "":
                out.append(f"{label}：{v}")
    elif module == "bill":
        cat = str(content.get("top_category") or "")
        if cat:
            period = str(content.get("period") or "")
            out.append(f"上次账单关注「{cat}」" + (f"（{period}）" if period else ""))
        if content.get("anomaly_count"):
            out.append(f"发现 {content['anomaly_count']} 笔异常")
        if content.get("subscription_count"):
            out.append(f"{content['subscription_count']} 笔订阅")
    elif module == "wealth":
        level = str(content.get("risk_level") or "")
        if level:
            out.append(f"风险等级 {level}")
        pref = str(content.get("preferred_type") or "")
        if pref:
            out.append(f"最大持仓「{pref}」")
        if content.get("holding_count"):
            out.append(f"持仓 {content['holding_count']} 笔")
        if content.get("needs_assessment") is True:
            out.append("待做风险测评")
    elif module == "birthday":
        budget = str(content.get("last_budget_yuan") or "")
        if budget:
            out.append(f"上次生日预算 ¥{budget}")
        goal = str(content.get("last_goal") or "")
        if goal:
            out.append(f"目标：{goal}")
        if content.get("rejected_cut") is True:
            m = str(content.get("rejected_cut_merchant") or "")
            out.append("上次保留了订阅" + (f"「{m}」" if m else "") + "（不再默认推荐取消）")
        elif str(content.get("accepted_cut_merchant") or ""):
            out.append(f"上次接受取消「{content['accepted_cut_merchant']}」")
    elif module == "plan_history":
        for i, it in enumerate(content.get("items") or [], 1):
            if not isinstance(it, dict):
                continue
            goal = str(it.get("goal") or "")[:30]
            result = str(it.get("result") or "")
            when = _day_of(it.get("at"))
            line = f"{i}. {goal}"
            if result:
                line += f" → {result}"
            if when:
                line += f"（{when}）"
            out.append(line)
    if not out and module not in _MODULE_LABELS:
        for k, v in content.items():
            if v not in (None, "", [], {}):
                out.append(f"{k}：{v}")
    return out


def memory_overview(user_id: Any) -> list[dict]:
    """记忆检阅视图：每模块一条（label/updated_at/内容/渲染行），画像置顶。

    供 8800 ``GET /api/memory`` 与前端「Agent 记忆」弹窗使用；任何异常
    软处理返回空列表（检阅入口绝不能拖垮主流程）。
    """
    uid = _valid_user_id(user_id)
    if uid is None:
        return []
    conn = _connect(create=False)
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT module, content_json, updated_at FROM agent_memory "
            "WHERE user_id=?", (uid,)).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    by_module: dict[str, tuple[dict, str]] = {}
    for module, raw, updated in rows:
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, dict):
            by_module[str(module)] = (data, str(updated or ""))
    out: list[dict] = []
    for module in _MODULE_ORDER:
        if module not in by_module:
            continue
        content, updated = by_module[module]
        out.append({
            "module": module,
            "label": module_label(module),
            "updated_at": updated,
            "lines": render_module(module, content),
            "content": content,
        })
    return out


__all__ = [
    "MEMORY_MODULES",
    "PLAN_HISTORY_LIMIT",
    "db_path",
    "forget",
    "merge",
    "memory_overview",
    "module_label",
    "push_plan_history",
    "recall",
    "recall_all",
    "remember",
    "render_module",
    "summarize_for_plan",
]
