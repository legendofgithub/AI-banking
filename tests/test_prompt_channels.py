"""条目 3 · 提示词通道契约测试(System Prompt 静态 / 动态上下文走 extra)。

为什么单独立一个文件
--------------------
`tests/conftest.py` 的 autouse 夹具把 `AGENT_PERSONA_DB` 与 `AGENT_MEMORY_DB`
隔离到 tmp_path,于是测试里**人设与长期记忆默认都是空的** —— 改通道前拼的是空串、
改通道后拼的还是空串,全量 413 条全绿**证明不了任何事**。必须显式 seed 人设/记忆,
再断言"动态内容确实换了通道"。

钉死三件事:
1. `ask_llm` 第一参数必须是**纯模块常量**(严格相等,不含人设文本)—— 保住前缀缓存;
2. 人设/长期记忆必须出现在**尾部 HumanMessage**(extra 通道),而不是 SystemMessage;
3. 播报链路的人设必须放在 `【事实】` JSON **之后**,不能插进 JSON 中间破坏解析。
"""
from __future__ import annotations

import asyncio
import json
import re

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import agent.graph as graph_mod
import agent.memory as memory_mod
import agent.persona as persona_mod
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel


PERSONA_TEXT = "说话像江湖侠客,称呼用户为『客官』"
PROFILE_NAME = "陈明"


class _RecordingLLM(FakeMessagesListChatModel):
    """记录每一次 astream 的 (messages, kwargs),响应按序返回。

    覆写 `astream` 而不是 `ainvoke`:`ask_llm` 走的是 `astream` 通道,
    覆写错了会一个调用都录不到(项目里 `_CountingLLM` 就踩过这个坑)。
    """

    def __init__(self, *responses: str):
        super().__init__(responses=[AIMessage(content=r) for r in responses])
        # pydantic 模型不允许直接 setattr 未声明字段,走 object.__setattr__
        object.__setattr__(self, "seen", [])

    async def astream(self, messages, config=None, *, stop=None, **kwargs):
        self.seen.append({"messages": list(messages), "kwargs": dict(kwargs)})
        async for chunk in super().astream(messages, config=config, stop=stop,
                                           **kwargs):
            yield chunk


def _run(coro):
    return asyncio.run(coro)


class _DummyTools:
    """哑工具集:播报链路会真的调一次只读工具,兜住它即可(不碰资金)。"""

    async def call(self, name: str, /, **args):
        return {"tool": name, "args": args}


def _build(recording: _RecordingLLM):
    """建一张图(不挂 checkpointer,跑完即弃)。"""
    return graph_mod.build_agent_graph(recording, _DummyTools(), None)


def _seed(monkeypatch, tmp_path, *, persona=True, profile=True):
    """把人设/记忆库指到 tmp_path 并写入非空内容(绕开 conftest 的默认空态)。"""
    monkeypatch.setenv("AGENT_PERSONA_DB", str(tmp_path / "persona.sqlite"))
    monkeypatch.setenv("AGENT_MEMORY_DB", str(tmp_path / "memory.sqlite"))
    if persona:
        assert persona_mod.set_persona(1, PERSONA_TEXT, 0.7), "人设写入失败"
    if profile:
        memory_mod.merge(1, "profile", {"name": PROFILE_NAME})


async def _chat_turn(recording: _RecordingLLM) -> list[dict]:
    """跑一轮闲聊:router 一次 + chat 一次。"""
    graph = _build(recording)
    await graph.ainvoke(
        {"messages": [HumanMessage(content="你好啊")], "auth_user_id": 1},
        config={"configurable": {"thread_id": "t1"}})
    return recording.seen


# ── 通道契约 1:System Prompt 必须是纯常量 ──────────────────────────────

def test_chat_system_prompt_is_pure_constant(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    seen = _run(_chat_turn(_RecordingLLM('{"intent": "chat"}', "客官好。")))
    assert len(seen) >= 2, f"期待 router + chat 两次调用,实际 {len(seen)}"
    chat_call = seen[-1]
    system = chat_call["messages"][0]
    assert isinstance(system, SystemMessage)
    assert system.content == graph_mod.CHAT_SYS, \
        "System Prompt 被拼接了动态内容 —— 前缀缓存会全部失效(条目 3 的核心)"
    assert PERSONA_TEXT not in system.content
    assert PROFILE_NAME not in system.content


def test_dynamic_context_moved_to_trailing_human_message(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    seen = _run(_chat_turn(_RecordingLLM('{"intent": "chat"}', "客官好。")))
    msgs = seen[-1]["messages"]
    tail = [m for m in msgs if isinstance(m, HumanMessage)]
    assert tail, "动态上下文必须至少占一条 HumanMessage"
    joined = "\n".join(m.content for m in tail)
    assert PERSONA_TEXT in joined, "人设没进 extra 通道 —— 会被静默丢弃"
    assert PROFILE_NAME in joined, "长期记忆没进 extra 通道"


def test_persona_and_temperature_still_reach_the_model(monkeypatch, tmp_path):
    """换通道不等于降功能:strong 与 temperature 必须照旧透传(语气控制不能丢)。"""
    _seed(monkeypatch, tmp_path)
    seen = _run(_chat_turn(_RecordingLLM('{"intent": "chat"}', "客官好。")))
    assert seen[-1]["kwargs"].get("strong") is True
    assert seen[-1]["kwargs"].get("temperature") == 0.7


def test_no_extra_message_when_persona_and_memory_are_empty(monkeypatch, tmp_path):
    """两块都空时必须连 extra 都不发 —— 与改动前"拼空串"的行为一致。"""
    _seed(monkeypatch, tmp_path, persona=False, profile=False)
    seen = _run(_chat_turn(_RecordingLLM('{"intent": "chat"}', "你好。")))
    msgs = seen[-1]["messages"]
    assert len([m for m in msgs if isinstance(m, HumanMessage)]) == 1, \
        "空人设/空记忆时不该多出一条空 HumanMessage(只有用户那一轮)"


def test_dynamic_ctx_skips_empty_blocks():
    """`_dynamic_ctx` 不得留下多余换行(会改变播报入参文本)。"""
    assert graph_mod._dynamic_ctx() == ""
    assert graph_mod._dynamic_ctx("", "") == ""
    assert graph_mod._dynamic_ctx("A", "", "B") == "A\nB"
    assert graph_mod._dynamic_ctx("", "B") == "B"


# ── 通道契约 2:所有调用点第一参数都是纯常量 ────────────────────────────

def test_every_ask_llm_call_passes_a_bare_constant():
    """grep 版验收:施工文档条目 3 的验收标准原文。

    只看真实调用点:跳过注释行、docstring 行与 `ask_llm` 自身的定义行
    (文档正文里也提到 `ask_llm(`,不跳过会误报)。
    """
    src = open(graph_mod.__file__, encoding="utf-8").read()
    found: list[str] = []
    for raw in src.splitlines():
        line = raw.strip()
        if line.startswith("#") or "def ask_llm" in line:
            continue
        if line.startswith(("\"\"\"", ">", "`")) or line.endswith(("`", ".py`")):
            continue
        found += [m.group(1).strip()
                  for m in re.finditer(r"\bask_llm\(\s*([^\n,)]+)", raw)]
    assert found, "没解析到任何 ask_llm 调用点"
    assert len(found) >= 13, f"调用点数量异常({len(found)}),正则可能失配"
    for first in found:
        first = first.strip()
        assert "+" not in first and "f\"" not in first and "format(" not in first, \
            f"ask_llm 第一参数不是纯常量: {first!r}"
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", first), \
            f"ask_llm 第一参数不是常量标识符: {first!r}"


# ── 通道契约 3:播报链路的人设必须在【事实】JSON 之后 ──────────────────

def test_announce_keeps_facts_json_parseable_ahead_of_persona(monkeypatch, tmp_path):
    """人设插进 JSON 中间会让模型(和 json 解析)读不出事实段 —— 必须排在后面。"""
    _seed(monkeypatch, tmp_path)
    recording = _RecordingLLM(
        '{"intent": "bill_analysis"}',
        '{"period": "2026-09", "ask": "monthly_report"}',
        "客官,九月支出一万二。")
    graph = _build(recording)
    _run(graph.ainvoke(
        {"messages": [HumanMessage(content="看看九月账单")], "auth_user_id": 1,
         "notice": {"kind": "bill_report", "ask": "monthly_report"}},
        config={"configurable": {"thread_id": "t2"}}))

    announce = [c for c in recording.seen
                if c["messages"][0].content == graph_mod.REPORT_SYS]
    assert announce, "没跑到播报节点"
    extra = [m.content for m in announce[-1]["messages"]
             if isinstance(m, HumanMessage)][-1]
    assert extra.startswith("【事实】")
    if PERSONA_TEXT in extra:
        assert extra.index("【事实】") < extra.index(PERSONA_TEXT), \
            "人设必须排在【事实】之后,否则会插进 JSON 中间"
    # 事实段本身仍要能被解析出来
    body = extra[len("【事实】"):].split("\n【人设")[0]
    assert isinstance(json.loads(body), dict), "【事实】段不再是可解析的 JSON"


# ── 零回归护栏 ────────────────────────────────────────────────────────

def test_prompt_constants_are_module_level_and_stable():
    """条目 3 的前提:常量必须是模块级、跨轮不变,否则前缀缓存无从谈起。"""
    for name in ("CHAT_SYS", "REPORT_SYS", "ROUTER_SYS"):
        const = getattr(graph_mod, name)
        assert isinstance(const, str) and const
        assert not callable(const)


def test_chat_sys_carries_anti_fabrication_redline():
    """2026-10-28 编造联系人名单事件的红线:chat 兜底必须自带禁编造约束。

    chat 是唯一没有【事实】注入纪律的自由生成路径(播报走 REPORT_SYS 的
    "禁止编造",抽取器只出 JSON)——红线一旦被删,模型又会凭空补名单。
    """
    assert "【业务事实红线】" in graph_mod.CHAT_SYS, \
        "CHAT_SYS 缺少业务事实红线 —— chat 兜底会回到编造业务数据的状态"
    assert "【实时" in graph_mod.CHAT_SYS, \
        "红线必须指明事实来源是系统注入的【实时…】上下文块"
    for w in ("编造", "查不到"):
        assert w in graph_mod.CHAT_SYS, f"红线缺少关键约束词: {w}"
