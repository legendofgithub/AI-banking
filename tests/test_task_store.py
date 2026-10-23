"""长程任务存储层单测(纯 sqlite,不碰 MCP/图)。"""

from __future__ import annotations

import os

from agent import tasks


def _isolated_db(tmp_path, monkeypatch):
    db = tmp_path / "agent_tasks.sqlite"
    monkeypatch.setenv("AGENT_TASKS_DB", str(db))
    # db_path() 每次读环境变量,无需清缓存
    return db


def test_create_and_get_task(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    t = tasks.create_task(11, goal="30 天存出 3000 生日基金",
                          target_yuan="3000", deadline="2026-11-20",
                          daily_target_yuan="100",
                          review_dates=["2026-10-28", "2026-11-04"])
    assert t is not None and t["task_id"].startswith("task-")
    assert t["status"] == "active" and t["saved_yuan"] == "0"
    assert tasks.get_task(t["task_id"])["goal"] == t["goal"]


def test_only_one_active_task_per_user(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    first = tasks.create_task(11, goal="a", target_yuan="100",
                              deadline="2026-11-01")
    second = tasks.create_task(11, goal="b", target_yuan="200",
                               deadline="2026-11-02")
    assert first is not None and second is None  # 已有 active → 拒绝
    tasks.finish_task(11, first["task_id"], status="done")
    again = tasks.create_task(11, goal="c", target_yuan="300",
                              deadline="2026-11-03")
    assert again is not None  # 终结后可再建


def test_user_isolation(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    t = tasks.create_task(11, goal="私有", target_yuan="100",
                          deadline="2026-11-01")
    assert tasks.get_active_task(12) is None          # 别的用户看不到
    assert tasks.update_progress(12, t["task_id"], saved_yuan="1",
                                 note="越权") is None  # 不能改别人的任务
    assert tasks.get_active_task(11)["task_id"] == t["task_id"]


def test_update_progress_appends_history(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    t = tasks.create_task(11, goal="存钱", target_yuan="3000",
                          deadline="2026-11-20", daily_target_yuan="100",
                          review_dates=["2026-10-28"])
    updated = tasks.update_progress(
        11, t["task_id"], saved_yuan="420", note="第 1 周达标",
        next_review_at="2026-11-04", daily_target_yuan="80")
    assert updated["saved_yuan"] == "420"
    assert updated["daily_target_yuan"] == "80"
    assert updated["next_review_at"] == "2026-11-04"
    assert updated["progress"][-1]["note"] == "第 1 周达标"
    again = tasks.update_progress(11, t["task_id"], saved_yuan="900",
                                  note="第 2 周达标")
    assert len(again["progress"]) == 2


def test_finish_task_status_whitelist(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    t = tasks.create_task(11, goal="x", target_yuan="1", deadline="2026-11-01")
    assert tasks.finish_task(11, t["task_id"], status="exploded") is None
    assert tasks.get_task(t["task_id"])["status"] == "active"
    done = tasks.finish_task(11, t["task_id"], status="cancelled")
    assert done["status"] == "cancelled"
    assert tasks.get_active_task(11) is None


def test_invalid_user_and_missing_task(tmp_path, monkeypatch):
    _isolated_db(tmp_path, monkeypatch)
    assert tasks.create_task(None, goal="g", target_yuan="1",
                             deadline="2026-11-01") is None
    assert tasks.create_task(3.5, goal="g", target_yuan="1",
                             deadline="2026-11-01") is None  # 浮点拒绝
    assert tasks.get_active_task(None) is None
    assert tasks.update_progress(11, "task-not-exist", saved_yuan="1",
                                 note="n") is None


def test_review_date_arithmetic():
    from datetime import date
    assert tasks.next_review_date(date(2026, 10, 22), 7) == "2026-10-29"
    assert tasks.next_review_date(days=30) > tasks.next_review_date(days=7)
