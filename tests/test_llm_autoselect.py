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


def test_request_llm_override_and_cache():
    """BYOK 门面:请求级覆盖 key/model;同组合复用实例;缺 key 报错。"""
    import asyncio
    from agent.llm import RequestLLM, reset_request_llm, set_request_llm

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
