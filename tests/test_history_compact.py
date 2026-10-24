"""条目 4 · 历史窗口压缩测试(L1 确定性清理 / L2 阈值摘要 / 熔断)。

设计约束(施工文档 §1 禁区 4):
- 压缩产物**只是参考**,闸门/订单/余额/限额一律以 DB 实时读取为准;
- 因此占位行与摘要都**不得含"已确认/可执行"语义**,也不得硬编码日期;
- 产物**只进 LLM 上下文**,绝不写回 `state["messages"]`(那是 checkpointer 持久化的
  真实历史,改了会污染库并影响 `/api/history` 刷新恢复)。

本文件全部同步/纯函数级用例 + 少量 asyncio.run,不建库、不联网、不启动图
——L2 逻辑已抽成模块级 `compact_l2`,摘要模型可注入,不需要真图。
"""
from __future__ import annotations

import asyncio
import inspect
import re

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import agent.graph as graph_mod


def _run(coro):
    return asyncio.run(coro)


def _long_ai(text: str = "账单分析结果", n: int = 100) -> AIMessage:
    """造一条超过 500 字符阈值的历史 assistant 消息(模拟大 JSON 工具结果)。"""
    return AIMessage(content=(text * n))


def _history(rounds: int = 30, long_every: int = 1) -> list:
    """造 n 轮对话:交替 Human / AI,AI 里混进超长工具结果。"""
    msgs = []
    for i in range(rounds):
        msgs.append(HumanMessage(content=f"第{i}轮用户提问"))
        msgs.append(_long_ai() if i % long_every == 0
                    else AIMessage(content=f"第{i}轮简短回复"))
    return msgs


# ── L1:compact_history ────────────────────────────────────────────────

def test_short_history_is_returned_untouched():
    msgs = [HumanMessage(content="你好"), AIMessage(content="在的")]
    assert graph_mod.compact_history(msgs) == msgs


def test_recent_messages_stay_verbatim():
    """最近 4 条必须原文保留 —— 压缩不能动"正在说的这一轮"。"""
    h = _history(rounds=20)
    out = graph_mod.compact_history(h)
    assert out[-graph_mod._HISTORY_KEEP_RECENT:] == h[-graph_mod._HISTORY_KEEP_RECENT:]


def test_old_long_ai_messages_are_replaced_by_placeholder():
    h = _history(rounds=30)
    out = graph_mod.compact_history(h)
    older = out[:-graph_mod._HISTORY_KEEP_RECENT]
    assert any(m.content == graph_mod._HISTORY_PLACEHOLDER for m in older), \
        "旧的长工具结果必须被占位替换"
    assert len(out) == len(h), "占位替换不改变条数(主线全保留)"


def test_short_old_messages_are_kept():
    """短消息原样保留:压缩只对"大段工具结果"下手,不删对话主线。"""
    h = [AIMessage(content="短回复")] * 10
    out = graph_mod.compact_history(h)
    assert out == h


def test_human_messages_are_never_replaced():
    """用户说的话一条都不许动 —— 用户意图不能被压缩掉。"""
    h = [HumanMessage(content="苹" * 800) for _ in range(10)]
    out = graph_mod.compact_history(h)
    assert all(m.content == "苹" * 800 for m in out)


def test_input_list_is_not_mutated():
    """纯函数:返回新列表,绝不改动入参(防误写回 state["messages"])。"""
    h = _history(rounds=10)
    snapshot = list(h)
    out = graph_mod.compact_history(h)
    assert h == snapshot, "入参被改了 —— 会污染 checkpointer 里的真实历史"
    assert out is not h


def test_placeholder_has_no_executable_or_date_semantics():
    """禁区 4:占位行不得含可执行/已确认语义,也不得硬编码日期。"""
    ph = graph_mod._HISTORY_PLACEHOLDER
    for forbidden in ("已确认", "可以执行", "可执行", "已完成", "扣款", "确认转账"):
        assert forbidden not in ph, f"占位行含越权语义: {forbidden}"
    assert not re.search(r"20\d{2}-\d{2}-\d{2}", ph), "占位行不得硬编码日期"


def test_threshold_is_configurable():
    h = [AIMessage(content="x" * 600)] * 6
    assert all(m.content == graph_mod._HISTORY_PLACEHOLDER
               for m in graph_mod.compact_history(h)[:2])
    # 阈值调高后不该再替换
    out = graph_mod.compact_history(h, threshold=10_000)
    assert all(m.content == "x" * 600 for m in out)


# ── L2:needs_compaction ───────────────────────────────────────────────

def test_needs_compaction_false_for_short_or_small_history():
    assert graph_mod.needs_compaction([]) is False
    assert graph_mod.needs_compaction(_history(rounds=3)) is False      # 条数不够
    small = [HumanMessage(content="你好"), AIMessage(content="在的")] * 3
    assert graph_mod.needs_compaction(small) is False                   # 条数不够


def test_needs_compaction_true_when_over_budget():
    big = [AIMessage(content="苹" * 3_000) for _ in range(5)]   # 远超 6000
    assert graph_mod.needs_compaction(big) is True


def test_needs_compaction_respects_custom_budget():
    big = [AIMessage(content="苹" * 3_000) for _ in range(5)]
    assert graph_mod.needs_compaction(big, budget=10**9) is False


# ── L2:compact_l2 ─────────────────────────────────────────────────────

def test_l2_replaces_oldest_with_summary_keeps_tail():
    big = [AIMessage(content="苹" * 3_000) for _ in range(5)]

    async def summarize(oldest):
        return "用户此前问过账单与理财,结论见实时查询。"

    out = _run(graph_mod.compact_l2(summarize, big))
    assert len(out) == graph_mod._HISTORY_KEEP_RECENT + 1
    assert isinstance(out[0], HumanMessage)
    assert out[0].content.startswith(graph_mod._L2_SUMMARY_PREFIX)
    assert out[1:] == big[-graph_mod._HISTORY_KEEP_RECENT:]


def test_l2_does_nothing_when_under_budget():
    small = [HumanMessage(content="你好"), AIMessage(content="在的")] * 3
    calls = {"n": 0}

    async def summarize(oldest):
        calls["n"] += 1
        return "不该被调用"

    assert _run(graph_mod.compact_l2(summarize, small)) == small
    assert calls["n"] == 0, "没超预算就不许白调一次模型"


def test_l2_circuit_breaks_after_three_failures():
    """熔断:连续 3 次失败后,同一图实例降级为只做 L1(不再尝试摘要)。"""
    big = [AIMessage(content="苹" * 3_000) for _ in range(5)]
    circuit: dict = {}
    calls = {"n": 0}

    async def boom(oldest):
        calls["n"] += 1
        raise RuntimeError("摘要模型挂了")

    for _ in range(graph_mod._L2_FAIL_LIMIT):
        assert _run(graph_mod.compact_l2(boom, big, circuit)) == big
    assert circuit["n"] == graph_mod._L2_FAIL_LIMIT

    # 第 4 次:已熔断,连调用都不该发生(原窗口直接返回)
    assert _run(graph_mod.compact_l2(boom, big, circuit)) == big
    assert calls["n"] == graph_mod._L2_FAIL_LIMIT, "熔断后不得再调摘要模型"


def test_l2_circuit_resets_on_success():
    big = [AIMessage(content="苹" * 3_000) for _ in range(5)]
    circuit = {"n": 2}
    out = _run(graph_mod.compact_l2(lambda oldest: _ok(oldest), big, circuit))
    assert circuit["n"] == 0, "成功一次必须清零熔断计数"
    assert len(out) == graph_mod._HISTORY_KEEP_RECENT + 1


async def _ok(oldest):
    return "摘要"


def test_l2_empty_summary_counts_as_failure():
    """摘要模型返回空串(被内容过滤)也计一次失败,不能当成"压完了"。"""
    big = [AIMessage(content="苹" * 3_000) for _ in range(5)]
    circuit: dict = {}

    async def empty(oldest):
        return "   "

    assert _run(graph_mod.compact_l2(empty, big, circuit)) == big
    assert circuit["n"] == 1


def test_l2_summary_prefix_marks_it_as_reference_only():
    """禁区 4:摘要必须自带"仅供参考"标记,且不出现可执行语义。"""
    prefix = graph_mod._L2_SUMMARY_PREFIX
    assert "参考" in prefix
    for forbidden in ("已确认", "可以执行", "可执行"):
        assert forbidden not in prefix
    # COMPACT_SYS 里钉死了:禁止推算补全、禁止可执行/确认语义
    sys_prompt = graph_mod.COMPACT_SYS
    assert "禁止" in sys_prompt and "未确认" in sys_prompt
    assert "编造" in sys_prompt


# ── 零回归:L2 默认关闭 ────────────────────────────────────────────────

def test_l2_is_off_by_default():
    """最关键的一条:现有调用点全是位置参数传 3 个,默认必须关闭。

    test_agent_graph.py 靠脚本化响应序列驱动,L2 多消耗一次调用会让后续节点全部错位
    (响应列表会循环,不会 IndexError,只会静默错乱)。
    """
    sig = inspect.signature(graph_mod.build_agent_graph)
    assert sig.parameters["compact_enabled"].default is False
    assert sig.parameters["compact_llm"].default is None
    # 两个新参数必须是 keyword-only,且排在原有三个之后 —— 保证老调用点不受影响
    kinds = [p.kind for p in sig.parameters.values()]
    assert kinds[:3] == [inspect.Parameter.POSITIONAL_OR_KEYWORD] * 3
    assert sig.parameters["compact_enabled"].kind is inspect.Parameter.KEYWORD_ONLY


def test_ask_llm_uses_the_unified_window_entry():
    """窗口组装必须走统一入口(compact_history),不能再用裸 `history[-n:]`。"""
    src = inspect.getsource(graph_mod.build_agent_graph)
    assert "compact_history(list(history[-n:]))" in src
    assert "*history[-n:]" not in src.replace(
        "compact_history(list(history[-n:]))", ""), "残留裸滑窗"


@pytest.mark.parametrize("name", ["compact_history", "compact_l2",
                                  "needs_compaction"])
def test_helpers_are_module_level(name):
    """L1/L2 必须可脱离图单测 —— 否则只能靠跑全量脚本化用例来兜,反馈太慢。"""
    assert callable(getattr(graph_mod, name))
    assert f"def {name}(" in inspect.getsource(graph_mod)


# ── 对抗性探测(AGENTS.md 铁律 6)──────────────────────────────────────
# 本批改的是"进 LLM 上下文的内容",按提示词/上下文类的三条探测走:
# 探测 1:超长历史(1000 条)不炸、条数守恒、仍在 3 秒内(每条用例都会走)
# 探测 2:最近一轮用户提问必须原样留在窗口末尾(模型不能把"正在问的问题"压掉)
# 探测 3:极端脏历史(None / 空 content / 非消息对象)全部原样放行,不许抛异常

def test_probe_1_thousand_message_history_is_cheap_and_lossless():
    import time

    h = _history(rounds=500)          # 1000 条
    t0 = time.perf_counter()
    out = graph_mod.compact_history(h)
    elapsed = time.perf_counter() - t0
    assert len(out) == len(h), "占位替换是等价替换,条数必须守恒(主线全保留)"
    assert elapsed < 3.0, f"太慢({elapsed:.2f}s),会拖垮全量测试"


def test_probe_2_latest_user_question_survives_compaction():
    h = _history(rounds=30)
    h.append(HumanMessage(content="那我这个月到底超支了没?"))
    out = graph_mod.compact_history(h)
    assert out[-1].content == "那我这个月到底超支了没?", \
        "当前这一轮的提问被压掉了 —— 模型会答非所问"
    assert isinstance(out[-1], HumanMessage)


def test_probe_3_dirty_history_is_passed_through_safely():
    class _Weird:
        content = None

    # 注意:AIMessage(content=None) 在 langchain 里根本构造不出来(pydantic 校验),
    # 所以"content 为 None"这条只能用鸭子类型替身来模拟。
    dirty = [_Weird(), AIMessage(content=""), HumanMessage(content=""),
             _Weird(), None]
    out = graph_mod.compact_history(dirty)
    assert len(out) == len(dirty), "认不出的消息一律原样放行,不得静默丢弃"
    assert graph_mod.needs_compaction(dirty) in (True, False)   # 不抛异常即可
