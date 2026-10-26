"""对话提炼记忆测试(2026-10-26 录入规则升级配套)。

覆盖三层:
1. ``graph.extract_turn_extracts`` —— 纯函数提炼:偏好/事实/目标/反目标/误报闸;
2. ``graph.remember_turn_extracts`` —— 落库编排:四档写入、时间线、去重、未登录;
3. ``memory.append_items`` —— 条目型存储:新在前、key 去重、截顶、软失败。

全套走临时库(AGENT_MEMORY_DB → tmp_path),不碰运行库;
与 test_memory.py 的三条硬约束同口径:软处理 / 未登录不写 / 隔离。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import memory
from agent.graph import (extract_self_intro_name, extract_turn_extracts,
                         remember_turn_extracts)


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_DB", str(tmp_path / "mem.sqlite"))


def _texts(module: str, items: list[dict]) -> list[str]:
    return [i.get("text") for i in items if i.get("module") == module]


# --------------------------------------------------------- 提炼(纯函数)

def test_extract_preferences_like_and_dislike():
    got = extract_turn_extracts("我喜欢喝咖啡,但我不喜欢香菜")
    kinds_texts = {(i["kind"], i["text"]) for i in got["preferences"]}
    assert ("like", "喝咖啡") in kinds_texts
    assert ("dislike", "香菜") in kinds_texts


def test_extract_preference_no_more():
    got = extract_turn_extracts("不想再订爱奇艺会员了")
    assert got["preferences"] == [
        {"kind": "dislike", "text": "不想再订爱奇艺会员",
         "key": "nomore:订爱奇艺会员"}]


def test_extract_risk_attitude():
    got = extract_turn_extracts("我比较保守,不想亏本金")
    kinds = {(i["kind"], i["text"]) for i in got["preferences"]}
    assert ("attitude", "偏保守") in kinds
    assert ("attitude", "不想亏本金") in kinds


def test_extract_risk_attitude_with_infix():
    """插字说法也要命中:"我是偏激进派"(2026-10-26 现场漏网:timeline 记了、
    preferences 却没记——态度正则原先不容"是/偏"之后再插一个"偏/更")。"""
    got = extract_turn_extracts("我是偏激进派,能接受大起大落")
    kinds = {(i["kind"], i["text"]) for i in got["preferences"]}
    assert ("attitude", "偏激进") in kinds


def test_extract_facts_rich_sentence():
    got = extract_turn_extracts("我叫faker,在杭州工作,我是一名程序员,今年28岁")
    facts = {(i["kind"], i["text"]) for i in got["facts"]}
    assert ("城市", "杭州") in facts
    assert ("职业", "程序员") in facts
    assert ("年龄", "28岁") in facts


def test_extract_income_family_pet_birthday():
    got = extract_turn_extracts("月薪2万,已婚,有个女儿,我养了一只猫,生日12月8号")
    facts = {(i["kind"], i["text"]) for i in got["facts"]}
    assert ("收入", "月薪2万") in facts
    assert ("家庭", "已婚") in facts
    assert ("家庭", "有孩子") in facts
    assert ("宠物", "养猫") in facts
    assert ("生日", "12月8日") in facts


def test_extract_negated_city_is_skipped():
    """否定语境("不住在北京")不落北京;肯定表述("老家在杭州")照常落。"""
    got = extract_turn_extracts("我不住在北京,我老家在杭州")
    assert {i["text"] for i in got.get("facts", [])} == {"杭州"}


def test_extract_workplace_stoplist():
    """无信息量工作地("在公司上班")不记。"""
    got = extract_turn_extracts("我在公司上班,很忙")
    texts = {i["text"] for i in got.get("facts", [])}
    assert "在公司上班" not in texts


def test_extract_goals_and_query_guard():
    got = extract_turn_extracts("我想买iPhone 17,还想年底去日本旅游")
    assert {i["text"] for i in got["goals"]} == {"买iPhone 17", "年底去日本旅游"}
    # 查询类请求不是目标;反目标也不是目标
    assert "goals" not in extract_turn_extracts("帮我看看这个月的账单,我想看消费分类")
    assert "goals" not in extract_turn_extracts("不想再订爱奇艺会员了")


def test_extract_age_out_of_range_skipped():
    got = extract_turn_extracts("我今年8岁")
    assert "facts" not in got


def test_extract_empty_and_greeting():
    assert extract_turn_extracts("") == {}
    assert extract_turn_extracts("你好") == {}
    assert extract_turn_extracts("   ") == {}


def test_self_intro_name_still_works():
    assert extract_self_intro_name("我叫faker") == "faker"


# --------------------------------------------------------- 落库编排

def test_remember_turn_writes_all_modules_and_timeline():
    counts = remember_turn_extracts(
        11, "我喜欢喝咖啡,我想买iPhone 17,我今年28岁,在杭州工作", "chat")
    # facts 三条:城市杭州 + 职业在杭州上班 + 年龄28岁
    assert counts == {"preferences": 1, "goals": 1, "facts": 3}
    assert [i["text"] for i in
            (memory.recall(11, "preferences") or {}).get("items", [])] == ["喝咖啡"]
    assert [i["text"] for i in
            (memory.recall(11, "goals") or {}).get("items", [])] == ["买iPhone 17"]
    timeline = (memory.recall(11, "timeline") or {}).get("items", [])
    assert len(timeline) == 1
    assert timeline[0]["kind"] == "闲聊"
    assert timeline[0]["text"].startswith("我喜欢喝咖啡")


def test_remember_turn_timeline_ignores_short_text():
    remember_turn_extracts(11, "好", "chat")
    assert memory.recall(11, "timeline") is None


def test_remember_turn_dedupes_repeated_input():
    remember_turn_extracts(11, "我喜欢喝咖啡", "chat")
    remember_turn_extracts(11, "我喜欢喝咖啡", "chat")
    items = (memory.recall(11, "preferences") or {}).get("items", [])
    assert [i["text"] for i in items] == ["喝咖啡"]
    timeline = (memory.recall(11, "timeline") or {}).get("items", [])
    assert len(timeline) == 1


def test_remember_turn_intent_label_in_timeline():
    remember_turn_extracts(11, "帮我分析一下这个月的账单", "bill_analysis")
    item = (memory.recall(11, "timeline") or {})["items"][0]
    assert item["kind"] == "账单"


def test_remember_turn_anonymous_writes_nothing():
    for uid in (None, 0, -1, "abc", 3.5):
        assert remember_turn_extracts(uid, "我喜欢喝咖啡", "chat") == {"preferences": 1}
        assert memory.recall(uid, "preferences") is None
        assert memory.recall(uid, "timeline") is None


# --------------------------------------------------------- append_items

def test_append_items_newest_first_and_key_dedupe():
    memory.append_items(11, "goals", [{"text": "买房", "key": "goal:买房"}])
    memory.append_items(11, "goals", [{"text": "买车", "key": "goal:买车"},
                                      {"text": "买房", "key": "goal:买房"}])
    items = (memory.recall(11, "goals") or {})["items"]
    assert [i["text"] for i in items] == ["买车", "买房"]  # 重复"买房"没再进


def test_append_items_respects_limit():
    for i in range(memory.ITEM_LIMITS["goals"] + 4):
        memory.append_items(11, "goals", [{"text": f"目标{i}",
                                           "key": f"goal:{i}"}])
    items = (memory.recall(11, "goals") or {})["items"]
    assert len(items) == memory.ITEM_LIMITS["goals"]
    assert items[0]["text"] == f"目标{memory.ITEM_LIMITS['goals'] + 3}"


def test_append_items_rejects_unknown_module_and_bad_input():
    assert memory.append_items(11, "随便写", [{"text": "x"}]) == []
    assert memory.append_items(11, "goals", ["不是dict", {"text": ""}]) == []
    assert memory.append_items(None, "goals", [{"text": "x"}]) == []


def test_append_items_soft_fail_on_broken_db(tmp_path: Path, monkeypatch):
    blocker = tmp_path / "not-a-file"
    blocker.mkdir()
    monkeypatch.setenv("AGENT_MEMORY_DB", str(blocker))
    # 软失败:返回本轮入参,绝不抛异常
    assert memory.append_items(11, "goals", [{"text": "x"}]) == [{"text": "x"}]


def test_append_items_keeps_dedupe_key_and_stamps_time():
    """key 留在存储里(跨轮去重要靠它复现),时间戳由存储层补。"""
    memory.append_items(11, "goals", [{"text": "买房", "key": "goal:买房"}])
    stored = (memory.recall(11, "goals") or {})["items"][0]
    assert stored["key"] == "goal:买房"
    assert stored["at"]


# --------------------------------------------------------- 渲染与检阅

def test_render_new_modules():
    assert memory.render_module("facts", {"items": [
        {"kind": "城市", "text": "杭州"},
        {"kind": "宠物", "text": "养猫"},
    ]}) == ["城市：杭州", "宠物：养猫"]
    assert memory.render_module("preferences", {"items": [
        {"kind": "like", "text": "喝咖啡"},
        {"kind": "dislike", "text": "香菜"},
        {"kind": "attitude", "text": "偏保守"},
    ]}) == ["喜欢：喝咖啡", "不喜欢：香菜", "风险态度：偏保守"]
    assert memory.render_module("goals", {"items": [
        {"text": "买iPhone 17"}]}) == ["买iPhone 17"]
    assert memory.render_module("timeline", {"items": [
        {"kind": "账单", "text": "分析本月账单", "at": "2026-10-26T10:00:00"},
    ]}) == ["[账单] 分析本月账单（10-26）"]


def test_overview_full_order_profile_first():
    remember_turn_extracts(11, "我喜欢喝咖啡,在杭州工作,我想买手机", "chat")
    memory.merge(11, "bill", {"top_category": "餐饮"})
    modules = [m["module"] for m in memory.memory_overview(11)]
    assert modules[0] == "profile" or modules[0] in ("facts", "preferences",
                                                     "goals")
    # 既有对话提炼四档,又有技能经验一档;profile 无内容时(本例未写过)不出现
    assert {"facts", "preferences", "goals", "timeline", "bill"} <= set(modules)


def test_summarize_for_plan_includes_latest_goal():
    remember_turn_extracts(11, "我想年底去日本旅游", "chat")
    hints = memory.summarize_for_plan(11, ("bill",))
    assert hints == ["用户提过想「年底去日本旅游」"]


def test_summarize_without_goals_unchanged():
    """没有目标记忆时,计划卡提示不出现目标句(旧行为零变化)。"""
    memory.remember(11, "bill", {"top_category": "餐饮"})
    assert memory.summarize_for_plan(11, ("bill",)) == ["上次账单关注「餐饮」"]
