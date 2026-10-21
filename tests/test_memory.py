"""agent.memory 长期记忆测试：UPSERT / 用户隔离 / 失败软处理 / 计划历史队列。

三条硬约束在这里被钉死（迭代计划 §6.3）：
1. 每个 module 只保留最新一条（UPSERT，不是追加）；
2. 用户之间互不干扰，且 user_id 为空（未登录）时一律不落库；
3. **任何异常都不能冒到调用方**——记忆不可用必须静默降级，绝不阻断主流程。

全部用 AGENT_MEMORY_DB 指向 tmp_path，不碰运行库。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent import memory


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    """每个用例一份临时记忆库（并清掉模块级的隐式状态依赖）。"""
    monkeypatch.setenv("AGENT_MEMORY_DB", str(tmp_path / "mem.sqlite"))


# --------------------------------------------------------------- 读写与 UPSERT

def test_remember_then_recall_roundtrip():
    assert memory.remember(11, "bill", {"top_category": "餐饮"})
    assert memory.recall(11, "bill") == {"top_category": "餐饮"}


def test_remember_is_upsert_not_append():
    memory.remember(11, "bill", {"top_category": "餐饮", "lasthit": "a"})
    memory.remember(11, "bill", {"top_category": "交通"})
    assert memory.recall(11, "bill") == {"top_category": "交通"}
    # 底层确实只有一行（不是追加）
    conn = memory._connect(create=False)
    try:
        rows = conn.execute(
            "SELECT COUNT(*) FROM agent_memory WHERE user_id=11 AND module='bill'"
        ).fetchone()
        assert rows[0] == 1
    finally:
        conn.close()


def test_recall_missing_module_returns_none():
    assert memory.recall(11, "bill") is None


def test_merge_patches_existing_content():
    memory.remember(11, "profile", {"budget_pref": "保守"})
    merged = memory.merge(11, "profile", {"risk_level": "C3"})
    assert merged == {"budget_pref": "保守", "risk_level": "C3"}
    assert memory.recall(11, "profile")["risk_level"] == "C3"


def test_merge_on_empty_module_creates_it():
    assert memory.merge(11, "wealth", {"preferred_type": "债基"}) == {
        "preferred_type": "债基"}


# ----------------------------------------------------------------- 用户隔离

def test_memory_is_isolated_between_users():
    memory.remember(11, "bill", {"top_category": "餐饮"})
    memory.remember(12, "bill", {"top_category": "数码"})
    assert memory.recall(11, "bill")["top_category"] == "餐饮"
    assert memory.recall(12, "bill")["top_category"] == "数码"


def test_anonymous_user_is_never_persisted():
    for uid in (None, 0, -1, "abc", "", True, False, 3.5):
        assert memory.remember(uid, "bill", {"x": 1}) is False, uid
        assert memory.recall(uid, "bill") is None, uid


def test_unknown_module_is_rejected():
    assert memory.remember(11, "随便写", {"x": 1}) is False
    assert memory.recall(11, "随便写") is None


def test_recall_all_groups_by_module():
    memory.remember(11, "bill", {"a": 1})
    memory.remember(11, "wealth", {"b": 2})
    got = memory.recall_all(11)
    assert set(got) == {"bill", "wealth"}
    assert got["bill"] == {"a": 1}


# --------------------------------------------------------------- 失败软处理

def test_broken_db_path_fails_soft(tmp_path: Path, monkeypatch):
    """库不可写（路径是目录）时，读写都返回空值且不抛异常。"""
    blocker = tmp_path / "not-a-file"
    blocker.mkdir()
    monkeypatch.setenv("AGENT_MEMORY_DB", str(blocker))
    assert memory.remember(11, "bill", {"x": 1}) is False
    assert memory.recall(11, "bill") is None
    assert memory.recall_all(11) == {}


def test_db_path_env_override(tmp_path: Path, monkeypatch):
    target = tmp_path / "custom.sqlite"
    monkeypatch.setenv("AGENT_MEMORY_DB", str(target))
    assert memory.db_path() == target
    memory.remember(11, "bill", {"x": 1})
    assert target.is_file()


def test_corrupted_json_row_does_not_raise(tmp_path: Path, monkeypatch):
    """既有行被写坏（非法 JSON）时，recall 返回 None 而不是炸掉主流程。"""
    monkeypatch.setenv("AGENT_MEMORY_DB", str(tmp_path / "bad.sqlite"))
    memory.remember(11, "bill", {"ok": 1})
    conn = memory._connect(create=False)
    try:
        conn.execute("UPDATE agent_memory SET content_json='{不是 json' "
                     "WHERE user_id=11 AND module='bill'")
        conn.commit()
    finally:
        conn.close()
    assert memory.recall(11, "bill") is None
    assert memory.recall_all(11) == {}


def test_remember_coerces_exotic_values_instead_of_raising():
    """不可 JSON 序列化的值被 str 降级而非抛异常（宁可写歪，不阻断主流程）。"""
    assert memory.remember(11, "bill", {"fn": object()}) is True
    assert isinstance(memory.recall(11, "bill")["fn"], str)


# --------------------------------------------------------------- 计划历史

def test_push_plan_history_keeps_latest_n_and_dedups():
    for i in range(3):
        memory.push_plan_history(11, {"plan_id": f"p{i}", "goal": f"目标{i}"})
    items = (memory.recall(11, "plan_history") or {}).get("items") or []
    assert [i["plan_id"] for i in items] == ["p2", "p1", "p0"]

    # 同 plan_id 重推（跨轮预算编辑）不产生重复行
    memory.push_plan_history(11, {"plan_id": "p1", "goal": "目标1'", "result": "cancelled"})
    items = (memory.recall(11, "plan_history") or {}).get("items") or []
    assert [i["plan_id"] for i in items] == ["p1", "p2", "p0"]
    assert items[0]["result"] == "cancelled"


def test_plan_history_respects_limit():
    for i in range(memory.PLAN_HISTORY_LIMIT + 3):
        memory.push_plan_history(11, {"plan_id": f"p{i}"})
    items = (memory.recall(11, "plan_history") or {}).get("items") or []
    assert len(items) == memory.PLAN_HISTORY_LIMIT
    assert items[0]["plan_id"] == f"p{memory.PLAN_HISTORY_LIMIT + 2}"


def test_push_plan_history_anonymous_is_noop():
    assert memory.push_plan_history(None, {"plan_id": "p1"}) == []


# --------------------------------------------------------------- 摘要与清理

def test_summarize_for_plan_is_empty_without_memory():
    assert memory.summarize_for_plan(11, ("bill", "wealth", "birthday")) == []


def test_summarize_for_plan_renders_hints():
    memory.remember(11, "bill", {"top_category": "餐饮"})
    memory.remember(11, "wealth", {"preferred_type": "债基", "risk_level": "C3"})
    memory.remember(11, "birthday", {"last_budget_yuan": "800",
                                     "rejected_cut": "true"})
    hints = memory.summarize_for_plan(11, ("bill", "wealth", "birthday"))
    assert "上次账单关注「餐饮」" in hints
    assert "上次偏好「债基」理财" in hints
    assert "上次生日预算 ¥800" in hints
    assert "上次保留订阅未取消" in hints


def test_summarize_prefers_accepted_cut_over_rejected():
    memory.remember(11, "birthday", {"accepted_cut_merchant": "腾讯视频"})
    hints = memory.summarize_for_plan(11, ("birthday",))
    assert any("腾讯视频" in h for h in hints)


def test_forget_module_and_user():
    memory.remember(11, "bill", {"a": 1})
    memory.remember(11, "wealth", {"b": 2})
    assert memory.forget(11, "bill") is True
    assert memory.recall(11, "bill") is None
    assert memory.recall(11, "wealth") == {"b": 2}
    assert memory.forget(11) is True
    assert memory.recall_all(11) == {}


def test_stored_payload_is_json_column():
    """库列是 TEXT JSON：可读性优先（便于排障），由 json 库保证往返无损。"""
    memory.remember(11, "birthday", {"last_budget_yuan": "800.00"})
    conn = memory._connect(create=False)
    try:
        row = conn.execute(
            "SELECT module, content_json FROM agent_memory WHERE user_id=11"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "birthday"
    assert json.loads(row[1])["last_budget_yuan"] == "800.00"
