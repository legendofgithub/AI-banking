"""pick_default_model 自动选型单测(注入假清单,不联网;每例先清进程缓存)。"""
from __future__ import annotations

import asyncio

import agent.llm as llm_mod
from agent.llm import DEFAULT_MODEL, pick_default_model


def _run(payload=None, *, raises=False):
    calls = []

    def fetch(base, key):
        calls.append(1)
        if raises:
            raise OSError("network down")
        return payload

    async def go():
        return await pick_default_model(fetch_json=fetch)
    return asyncio.run(go()), calls


def setup_function(_):
    llm_mod._auto_model_cache = None


def test_prefers_newest_flash_over_newer_flagship():
    payload = {"data": [
        {"id": "glm-4.6", "created": 100},
        {"id": "glm-5.3", "created": 300},          # 更新的旗舰
        {"id": "glm-5.3-flash", "created": 300},    # 同代的 flash
    ]}
    got, _ = _run(payload)
    assert got == "glm-5.3-flash"


def test_falls_back_to_newest_any_when_no_flash():
    payload = {"data": [
        {"id": "glm-4.5", "created": 100},
        {"id": "glm-6", "created": 900},
    ]}
    got, _ = _run(payload)
    assert got == "glm-6"


def test_empty_or_failure_falls_back_to_default():
    got, _ = _run({"data": []})
    assert got == DEFAULT_MODEL
    got2, _ = _run(raises=True)
    assert got2 == DEFAULT_MODEL


def test_result_cached_per_process():
    payload = {"data": [{"id": "glm-5.3-flash", "created": 1}]}
    _, calls1 = _run(payload)
    assert len(calls1) == 1
    # 第二次调用不再发请求(缓存命中)
    async def second():
        return await pick_default_model(fetch_json=lambda b, k: (_ for _ in ()).throw(AssertionError("不应再拉取")))
    assert asyncio.run(second()) == "glm-5.3-flash"


def test_request_llm_override_and_cache(monkeypatch):
    """BYOK 门面:请求级覆盖 key/model;同组合复用实例;缺 key 报错。

    注意隔离真实环境:本机用户环境变量可能长期存有 ZAI_MODEL(2026-10-09 切
    DeepSeek 时 setx 写入过)。RequestLLM 的模型优先级是 覆盖 > env > fallback,
    不清掉 env 的话 fallback 断言会被真实环境变量盖掉而假失败。
    """
    import asyncio
    from agent.llm import RequestLLM, reset_request_llm, set_request_llm

    monkeypatch.delenv("ZAI_MODEL", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)

    made = []

    class FakeInst:
        def __init__(self, **kw):
            self.kw = kw
            made.append(self)

        async def ainvoke(self, msgs, **_):
            return "ok"

    llm = RequestLLM(
        fallback_model="glm-fast",
        factory=lambda **kw: FakeInst(**kw),
        timeout=1)

    async def go():
        tok = set_request_llm(api_key="k1", model="m1")
        try:
            await llm.ainvoke([{"role": "user", "content": "hi"}])
            await llm.ainvoke([{"role": "user", "content": "hi"}])  # 缓存:不再新建
        finally:
            reset_request_llm(tok)
        assert made[0].kw["api_key"] == "k1"
        assert made[0].kw["model"] == "m1"
        assert len(made) == 1
        # 无覆盖:回落 fallback_model;key 缺失 → 明确报错
        tok2 = set_request_llm(api_key="k2")
        try:
            await llm.ainvoke([{"role": "user", "content": "hi"}])
            assert made[1].kw["model"] == "glm-fast"
        finally:
            reset_request_llm(tok2)

    asyncio.run(go())


def test_normalize_base_url_and_provider_family():
    """供应商 Base URL 只允许 http(s),Claude 归入 Anthropic 协议族。"""
    import pytest
    from agent.llm import normalize_base_url, provider_family

    assert normalize_base_url(" https://api.deepseek.com/v1/ ") == (
        "https://api.deepseek.com/v1")
    assert normalize_base_url("") is None
    for bad in ("file:///etc/passwd", "ftp://example.com", "not-a-url"):
        with pytest.raises(ValueError):
            normalize_base_url(bad)
    assert provider_family("anthropic") == "anthropic"
    assert provider_family("claude") == "anthropic"
    assert provider_family("deepseek") == "openai"
    assert provider_family(None) == "openai"


def test_anthropic_adapter_converts_messages(monkeypatch):
    """Claude 适配器:系统提示单独传,messages 交替合并,解析 content 文本。"""
    import httpx
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

    from agent.llm import AnthropicChatModel

    seen: dict = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"content": [{"type": "text", "text": "pong"}]}

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None, headers=None):
            seen.update({"url": url, "json": json, "headers": headers})
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    model = AnthropicChatModel(model="claude-sonnet-4-5", api_key="k",
                               base_url="https://api.anthropic.com/v1/")
    out = asyncio.run(model.ainvoke([
        SystemMessage(content="sys"),
        HumanMessage(content="hi"),
        AIMessage(content="hello"),
        HumanMessage(content="again"),
    ]))
    assert out.content == "pong"
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "k"
    assert seen["json"]["system"] == "sys"
    assert seen["json"]["messages"] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "again"},
    ]


def test_request_llm_passes_connection(monkeypatch):
    """请求级连接参数(base_url/provider)原样传给工厂,并参与实例缓存键。"""
    from agent.llm import RequestLLM, reset_request_llm, set_request_llm

    monkeypatch.delenv("ZAI_BASE_URL", raising=False)
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    made: list[dict] = []

    class FakeInst:
        def __init__(self, **kw):
            made.append(kw)

        async def ainvoke(self, _msgs, **_):
            return "ok"

    llm = RequestLLM(factory=lambda **kw: FakeInst(**kw))

    async def go():
        tok = set_request_llm(
            api_key="k1", model="m1",
            base_url="https://api.deepseek.com/v1", provider="deepseek")
        try:
            await llm.ainvoke([])
        finally:
            reset_request_llm(tok)
        tok2 = set_request_llm(
            api_key="k1", model="m1",
            base_url="https://api.anthropic.com/v1", provider="anthropic")
        try:
            await llm.ainvoke([])   # 端点/协议不同 → 新实例
        finally:
            reset_request_llm(tok2)

    asyncio.run(go())
    assert made[0]["base_url"] == "https://api.deepseek.com/v1"
    assert made[0]["provider"] == "deepseek"
    assert made[1]["base_url"] == "https://api.anthropic.com/v1"
    assert made[1]["provider"] == "anthropic"
    assert len(made) == 2
