"""统一工具调用咽喉(agent/guarded.py)测试 —— 施工文档条目 5 · B3 批。

本批 `guarded_call` 是**纯转发**,所以这里钉死三件事:
1. 记账结构与迁移前的 `graph.py._jr` **逐字段等价**(契约测试与 `_audited_tools` 都盯着它);
2. 调用转发**零变形**:入参原样传给工具、返回值原样交回调用方;
3. 迁移进度可断言:直接 `tools.call(` 的计数必须等于本批预期的剩余值
   (每迁一批就改一次这个数字,防止有人偷偷绕过咽喉新增直接调用)。

不建库、不联网、不启动图。
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from agent import guarded as guarded_mod

ROOT = Path(__file__).resolve().parents[1]

# 迁移进度基线:每迁一批改一次,改的时候必须同步跑全量
# B3 前实测:graph.py 53 / skills.py 8 / api.py 4 = 65
# B3 已迁 5 处(b_ 1 + s_ 4)→ graph.py 48
# 注意 api.py 走的是 `app.state.bank.call(...)`,所以它的匹配式与另两个文件不同
EXPECTED_DIRECT_CALLS = {
    # graph.py 48→51(2026-10-26 理财中心入口:w_entry 拉等级+全量产品 2 处,
    # w_query 未测评分支列 R1 产品 1 处;与周边 w_* 节点同款直接调用)
    # 51→55(2026-10-27 AA 交款单:sb_create 派发 1 + pr_extract 查收件箱 1
    # + pr_pay 付款/刷新进度 2;与 sb_*/ss_* 同款直接调用)
    "agent/graph.py": (r"tools\.call\(", 55),
    "agent/skills.py": (r"tools\.call\(", 8),
    "agent/api.py": (r"\.call\(", 4),
}


class _FakeTools:
    """记录每一次调用的工具替身:只冒名 `.call(name, **args)`。"""

    def __init__(self, result=None):
        self.calls: list[tuple[str, dict]] = []
        self.result = {} if result is None else result

    async def call(self, name: str, /, **args):
        self.calls.append((name, args))
        return self.result


def _run(coro):
    return asyncio.run(coro)


# ── 记账结构:与迁移前的 _jr 逐字段等价 ────────────────────────────────

def _legacy_jr(name: str, args: dict, result):
    """迁移前 graph.py 的实现原文,作为结构对拍的基准。"""
    err = result.get("error") if isinstance(result, dict) else None
    return {"tool": name, "args": args, "ok": err is None, "error": err}


@pytest.mark.parametrize("result", [
    {},
    {"merchant_name": "房租", "annual_yuan": "81600.00"},
    {"error": "余额不足"},
    {"error": None},
    "非 dict 的返回值",
    None,
])
def test_journal_record_matches_legacy_field_by_field(result):
    for name, args in (("monthly_report", {"month": "2026-09"}),
                       ("cancel_subscription", {"subscription_id": 3}),
                       ("list_subscriptions", {})):
        assert guarded_mod.journal_record(name, args, result) == \
            _legacy_jr(name, args, result)


def test_journal_record_has_exactly_four_keys():
    rec = guarded_mod.journal_record("x", {"a": 1}, {})
    assert set(rec) == {"tool", "args", "ok", "error"}, \
        "记账字段被契约测试与 _audited_tools 冻结,禁止增删"


def test_journal_ok_flag_follows_error_key():
    assert guarded_mod.journal_record("x", {}, {})["ok"] is True
    assert guarded_mod.journal_record("x", {}, {"error": "炸了"})["ok"] is False
    assert guarded_mod.journal_record("x", {}, {"error": None})["ok"] is True


# ── 转发零变形 ────────────────────────────────────────────────────────

def test_guarded_call_forwards_name_and_args_verbatim():
    tools = _FakeTools({"ok": 1})
    res, rec = _run(guarded_mod.guarded_call(
        tools, "monthly_report", {"month": "2026-09"}))
    assert tools.calls == [("monthly_report", {"month": "2026-09"})]
    assert res == {"ok": 1}, "返回值必须原样交回调用方(禁区:不得改工具返回值)"
    assert rec == {"tool": "monthly_report", "args": {"month": "2026-09"},
                   "ok": True, "error": None}


def test_guarded_call_does_not_mutate_caller_args():
    """调用方的 args 字典不许被咽喉改写(schema 校验是 B8 的事,本批不碰)。"""
    tools = _FakeTools({})
    args = {"subscription_id": 3}
    _run(guarded_mod.guarded_call(tools, "cancel_subscription", args))
    assert args == {"subscription_id": 3}
    assert tools.calls[0][1] == {"subscription_id": 3}


def test_journal_args_overrides_only_the_recorded_snapshot():
    """s_exec 的语义:工具收 int(sub_id),记账保留原始 sub_id——两者必须能分开。"""
    tools = _FakeTools({})
    res, rec = _run(guarded_mod.guarded_call(
        tools, "cancel_subscription", {"subscription_id": 3},
        journal_args={"subscription_id": "3"}))
    assert tools.calls == [("cancel_subscription", {"subscription_id": 3})]
    assert rec["args"] == {"subscription_id": "3"}


def test_guarded_call_into_appends_and_returns_result():
    tools = _FakeTools({"rows": []})
    calls: list[dict] = []
    res = _run(guarded_mod.guarded_call_into(calls, tools,
                                             "list_subscriptions"))
    assert res == {"rows": []}
    assert calls == [{"tool": "list_subscriptions", "args": {}, "ok": True,
                      "error": None}], "记账必须 append 进节点的 calls 列表"


def test_error_result_is_passed_through_not_swallowed():
    """工具报错时,结果必须照原样回到节点——由节点自己决定 notice,咽喉不代劳。"""
    tools = _FakeTools({"error": "工具炸了"})
    res, rec = _run(guarded_mod.guarded_call(tools, "monthly_report", {}))
    assert res == {"error": "工具炸了"}
    assert rec["ok"] is False and rec["error"] == "工具炸了"


# ── 迁移进度:防止绕过咽喉新增直接调用 ────────────────────────────────

def _count(pattern: str, rel: str) -> int:
    return len(re.findall(pattern, (ROOT / rel).read_text(encoding="utf-8")))


@pytest.mark.parametrize("rel", sorted(EXPECTED_DIRECT_CALLS))
def test_direct_tool_calls_match_migration_progress(rel):
    """直接工具调用(`tools.call(` / api 侧的 `bank.call(`)计数必须等于本批剩余值。

    这个数字只在**迁移批次**里下降。如果它上升了,说明有人绕过咽喉新增了直接调用;
    如果它比预期低,说明有人多迁了 —— 两种情况都要显式更新本文件的基线并跑全量。
    """
    pattern, expected = EXPECTED_DIRECT_CALLS[rel]
    got = _count(pattern, rel)
    assert got == expected, (
        f"{rel} 直接工具调用 {got} 处,预期 {expected}。"
        "确认是有意迁移后,更新 tests/test_guarded_call.py 的 EXPECTED_DIRECT_CALLS 并重跑全量")


def test_graph_delegates_journal_to_guarded_module():
    """graph.py 的 _jr 必须转发到统一口径,不能另起一套实现(否则两边会漂移)。"""
    src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    assert "guarded_mod.journal_record" in src
    # 旧的内联实现不得残留
    assert 'return {"tool": name, "args": args, "ok": err is None' not in src


def test_pay_password_journal_stays_handwritten():
    """禁区:支付密码必须脱敏为 ***,这处手写记账不得被迁移覆盖。"""
    src = (ROOT / "agent" / "graph.py").read_text(encoding="utf-8")
    assert '"pay_password": "***"' in src


# ── 对抗性探测(AGENTS.md 铁律 6):B3 是"改执行路径",按资金类改动的三条探测走 ──
# 探测 1:同指令重放 → 两次调用必须产生两条记账(审计不可去重,禁区 1「全量审计日志」)
# 探测 2:工具抛异常时咽喉必须原样上抛,不许静默吞掉成"成功"
# 探测 3:入参被加工(如 int(sub_id))时,记账快照与真实入参各自独立且互不污染

def test_probe_1_replayed_calls_produce_two_journal_records():
    """探测 1:咽喉将来可能加缓存/去重,绝不能把同指令重放合并成一条记账。"""
    tools = _FakeTools({"ok": True})
    calls: list[dict] = []
    for _ in range(2):
        _run(guarded_mod.guarded_call_into(calls, tools, "cancel_subscription",
                                           {"subscription_id": 3}))
    assert len(calls) == 2, "审计要求每次调用都留痕,重放不得被去重"
    assert len(tools.calls) == 2, "也不得被缓存成只调一次(两步走语义靠真实调用次数)"


def test_probe_2_tool_exception_propagates_not_swallowed():
    """探测 2:工具内部 raise 时,咽喉必须让它冒上去,不许伪装成 ok=True。"""

    class _Boom:
        async def call(self, name, /, **args):
            raise RuntimeError("连接断了")

    with pytest.raises(RuntimeError, match="连接断了"):
        _run(guarded_mod.guarded_call(_Boom(), "monthly_report", {}))


def test_probe_3_args_snapshot_is_isolated_from_call_args():
    """探测 3:记账快照与真实入参是两个对象,改一个不许影响另一个。"""
    tools = _FakeTools({})
    original = {"subscription_id": "3"}
    _, rec = _run(guarded_mod.guarded_call(
        tools, "cancel_subscription", {"subscription_id": int("3")},
        journal_args=original))
    rec["args"]["subscription_id"] = "被改坏了"
    assert original == {"subscription_id": "3"}
    assert tools.calls[0][1] == {"subscription_id": 3}, "真实入参不得被记账对象污染"
