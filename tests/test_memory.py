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


# --------------------------------------------------------------- 健壮性批次(2026-10-23)

def test_merge_none_value_deletes_key():
    """merge 删键语义(参考 Anthropic memory tool 的 str_replace/delete):
    patch 里值为 None 的键表示删除——清理上次留下的过期字段。"""
    memory.remember(11, "birthday",
                    {"rejected_cut": True, "rejected_cut_merchant": "爱奇艺"})
    merged = memory.merge(11, "birthday",
                          {"rejected_cut": False, "rejected_cut_merchant": None})
    assert "rejected_cut_merchant" not in merged
    stored = memory.recall(11, "birthday")
    assert stored["rejected_cut"] is False
    assert "rejected_cut_merchant" not in stored


def test_merge_invalid_uid_returns_patch_only():
    assert memory.merge(None, "bill", {"a": 1}) == {"a": 1}
    assert memory.merge(3.5, "bill", {"a": 1}) == {"a": 1}


def test_read_does_not_create_db_file(tmp_path: Path, monkeypatch):
    """纯读(库不存在)不建空库文件——读路径不能有写副作用。"""
    target = tmp_path / "nope.sqlite"
    monkeypatch.setenv("AGENT_MEMORY_DB", str(target))
    assert memory.recall(11, "bill") is None
    assert memory.recall_all(11) == {}
    assert not target.exists()


def test_db_runs_in_wal_mode():
    """WAL:并发写下不再 5 秒锁死后被软处理吞掉(记忆静默丢失)。"""
    memory.remember(11, "bill", {"a": 1})
    memory.merge(11, "bill", {"b": 2})
    memory.push_plan_history(11, {"plan_id": "p1"})
    conn = memory._connect(create=False)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert str(mode).lower() == "wal"


def test_push_plan_history_on_broken_db_returns_new_item(
        tmp_path: Path, monkeypatch):
    blocker = tmp_path / "not-a-file"
    blocker.mkdir()
    monkeypatch.setenv("AGENT_MEMORY_DB", str(blocker))
    items = memory.push_plan_history(11, {"plan_id": "p1"})
    assert len(items) == 1
    assert items[0]["plan_id"] == "p1"


# ----------------------------------------------- 渲染层与检阅视图(2026-10-23)

def test_render_module_each_kind():
    assert memory.render_module("profile", {
        "risk_level": "稳健", "top_category": "餐饮",
        "holding_count": 3, "preferred_type": "",
    }) == ["风险偏好：稳健", "消费大头：餐饮", "持仓数：3"]
    assert memory.render_module("bill", {
        "top_category": "餐饮", "period": "2026-09",
        "anomaly_count": 2, "subscription_count": 5,
    }) == ["上次账单关注「餐饮」（2026-09）", "发现 2 笔异常", "5 笔订阅"]
    assert memory.render_module("wealth", {
        "risk_level": "C3", "preferred_type": "债基", "needs_assessment": True,
    }) == ["风险等级 C3", "最大持仓「债基」", "待做风险测评"]
    assert memory.render_module("birthday", {
        "last_budget_yuan": "800", "rejected_cut": True,
        "rejected_cut_merchant": "爱奇艺视频",
    }) == ["上次生日预算 ¥800", "上次保留了订阅「爱奇艺视频」（不再默认推荐取消）"]
    assert memory.render_module("plan_history", {"items": [
        {"goal": "生日存钱", "result": "done", "at": "2026-10-15T09:00:00"},
        {"goal": "账单分析"},
    ]}) == ["1. 生日存钱 → done（10-15）", "2. 账单分析"]
    # 空值不渲染、未知模块逐键兜底
    assert memory.render_module("bill", {}) == []
    assert memory.render_module("future_mod", {"x": 1, "y": ""}) == ["x：1"]


def test_memory_overview_profiles_first_and_carries_meta():
    memory.remember(11, "bill", {"top_category": "餐饮"})
    memory.remember(11, "profile", {"risk_level": "稳健"})
    overview = memory.memory_overview(11)
    assert [m["module"] for m in overview] == ["profile", "bill"]
    assert overview[0]["label"] == "用户画像"
    assert overview[0]["lines"] == ["风险偏好：稳健"]
    assert overview[0]["updated_at"]  # 留痕:什么时候记的
    assert overview[0]["content"] == {"risk_level": "稳健"}
    # 用户隔离:别的用户看不到
    assert memory.memory_overview(12) == []


def test_summarize_hints_carry_remembered_date():
    """时间是一等公民:提示带"上次是什么时候记的"。"""
    memory.remember(11, "bill", {"top_category": "餐饮",
                                 "at": "2026-10-15T09:00:00"})
    hints = memory.summarize_for_plan(11, ("bill",))
    assert hints == ["上次账单关注「餐饮」(10-15)"]


def test_summarize_profile_hints_only_for_uncovered_skills():
    """画像只补当前技能没覆盖到的信息,不与 bill/wealth 模块提示重复。"""
    memory.remember(11, "profile", {"risk_level": "稳健", "top_category": "餐饮"})
    # 只做账单分析:补风险偏好,不再重复消费大类(bill 模块自己会说)
    hints = memory.summarize_for_plan(11, ("bill",))
    assert hints == ["用户是稳健型投资者"]
    # 只做理财分析:补消费大头
    hints = memory.summarize_for_plan(11, ("wealth",))
    assert hints == ["常在「餐饮」上消费"]
    # 两技能都做:画像提示全让位,不出现
    hints = memory.summarize_for_plan(11, ("bill", "wealth"))
    assert hints == []


def test_module_label_unknown_passthrough():
    assert memory.module_label("bill") == "账单经验"
    assert memory.module_label("mystery") == "mystery"


# ----------------------------------------------- 2026-10-24 评审修复

def test_push_plan_history_without_plan_id_keeps_history():
    """空 plan_id 不去重:新条目不得顶掉既有的无 id 历史。"""
    memory.push_plan_history(11, {"goal": "无id计划A"})
    memory.push_plan_history(11, {"goal": "无id计划B"})
    items = (memory.recall(11, "plan_history") or {}).get("items") or []
    assert [i["goal"] for i in items] == ["无id计划B", "无id计划A"]


def test_valid_user_id_rejects_lossy_numeric_types():
    """Decimal/Fraction 同 float 一样会被 int() 静默截断,一律拒绝;
    字符串只收无损整数(带下划线的 "1_000" 拒)。"""
    from decimal import Decimal
    from fractions import Fraction

    for bad in (Decimal("3.5"), Fraction(7, 2), 3.5, "1_000", "abc", "",
                0, -2):
        assert memory._valid_user_id(bad) is None, bad
    assert memory._valid_user_id(11) == 11
    assert memory._valid_user_id("12") == 12
    assert memory._valid_user_id("+12") == 12


def test_render_module_birthday_accepts_legacy_string_true():
    """rejected_cut 的字符串 "true" 旧数据:render 与 summarize 口径一致。"""
    lines = memory.render_module("birthday", {"rejected_cut": "true"})
    assert lines == ["上次保留了订阅（不再默认推荐取消）"]
    hints = memory.summarize_for_plan(11, ())  # 模块集合一致性由 import 断言守卫
    assert hints == []


def test_module_definitions_stay_in_sync():
    """MEMORY_MODULES 与检阅顺序 _MODULE_ORDER 必须一致(双份定义防漂移)。"""
    assert set(memory._MODULE_ORDER) == memory.MEMORY_MODULES
    assert set(memory._MODULE_LABELS) == memory.MEMORY_MODULES


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


def test_render_module_profile_includes_name():
    """画像渲染行含姓名(2026-10-24 接线后新增字段),排第一;空值跳过。"""
    assert memory.render_module("profile", {"name": "丁博文"}) == ["姓名：丁博文"]
    assert memory.render_module(
        "profile", {"name": "丁博文", "risk_level": "稳健"}
    ) == ["姓名：丁博文", "风险偏好：稳健"]
    assert memory.render_module("profile", {"risk_level": "稳健"}) == [
        "风险偏好：稳健"]


# --------------------------------------------------------------- 测评写记忆(2026-10-26)

def test_remember_assessment_writes_wealth_and_profile():
    """测评完成 → wealth 刷新等级/待测评清零、profile 同步等级;未登录软跳过。

    (w_assess 原本只落银行事实库 risk_profiles,记忆里毫无痕迹——
    检阅弹窗与下次理财建议只能等下一次完整理财分析才看到新等级。)
    """
    from agent.graph import _remember_assessment

    _remember_assessment(11, "C4")
    wealth = memory.recall(11, "wealth")
    assert wealth["risk_level"] == "C4"
    assert wealth["max_risk_level"] == 4
    assert wealth["needs_assessment"] is False
    assert memory.recall(11, "profile")["risk_level"] == "C4"

    # 未登录(user_id 非法):不写不抛(三条硬约束之二)
    _remember_assessment(None, "C3")
    assert memory.recall(None, "wealth") is None


def test_remember_assessment_merges_without_wiping_analysis_snapshot():
    """浅合并语义:已有分析快照(偏好/持仓)不被测评覆盖,只更新测评确定的键。"""
    from agent.graph import _remember_assessment

    memory.remember(11, "wealth", {"preferred_type": "纯债基金",
                                   "holding_count": 7,
                                   "needs_assessment": True})
    _remember_assessment(11, "C3")
    w = memory.recall(11, "wealth")
    assert w["preferred_type"] == "纯债基金"   # 分析快照保留
    assert w["holding_count"] == 7
    assert w["needs_assessment"] is False      # 待测评清零
    assert w["risk_level"] == "C3"
