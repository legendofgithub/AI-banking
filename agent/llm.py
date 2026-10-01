"""LLM 工厂:M1 编排层唯一的模型入口。

设计要点(研发计划 §3/§4 铁律 1):
- LLM 只做"理解/消歧/播报",金额与账务一律走 bank_core 的确定性代码;
- 生产环境用智谱 GLM 的 OpenAI 兼容端点(langchain-openai ChatOpenAI);
- 测试注入 langchain_core.language_models.fake_chat_models 的假模型,
  脚本化 LLM 响应、全程不联网(tests/test_agent_graph.py 即此用法)。

环境变量:
- ZAI_API_KEY   必填(生产);缺失时 get_llm() 抛 RuntimeError 而非静默降级
- ZAI_BASE_URL  选填,默认 https://open.bigmodel.cn/api/paas/v4/
- ZAI_MODEL     选填;不设时 api 启动走 pick_default_model() 按端点清单
                自动选(最新 flash 级,离线回落 glm-4.6)——不写死模型名
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import urllib.request
from typing import Any, Callable

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
DEFAULT_MODEL = "glm-4.6"

# 自动选型结果缓存(每进程一次;端点清单变化靠重启生效)
_auto_model_cache: str | None = None

# 请求级 LLM 覆盖(BYOK:界面填的 Key/选的模型随请求生效;无覆盖走环境变量)
_request_override: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "request_llm_override", default=None)


def set_request_llm(*, api_key: str | None = None,
                    model: str | None = None) -> contextvars.Token:
    return _request_override.set({"api_key": api_key, "model": model})


def reset_request_llm(token: contextvars.Token) -> None:
    _request_override.reset(token)


def fetch_models_json(base_url: str, api_key: str) -> Any:
    """同步拉取端点模型清单(在线程池里跑,避免阻塞事件循环)。"""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


async def pick_default_model(
    *, api_key: str | None = None, base_url: str | None = None,
    fetch_json: Callable[[str, str], Any] | None = None,
) -> str:
    """按端点实际可用的模型自动选编排层默认模型(与前端探测同一清单)。

    选型规则(编排层全是受限任务——分类/抽取/播报,快比深重要):
    1. 取 created 最新的 flash/turbo 级模型(flashx 视同 flash);
    2. 没有快模型时,取 created 最新的任意模型;
    3. 清单为空/接口失败 → DEFAULT_MODEL(glm-4.6,离线兜底)。
    显式设置了 ZAI_MODEL 的调用方应直接用环境变量,不走本函数。

    fetch_json 仅供测试注入(base_url, api_key) -> dict;缺省真实拉取。
    """
    global _auto_model_cache
    if _auto_model_cache:
        return _auto_model_cache
    key = api_key or os.environ.get("ZAI_API_KEY") or ""
    base = base_url or os.environ.get("ZAI_BASE_URL") or DEFAULT_BASE_URL
    try:
        fetch = fetch_json or fetch_models_json
        payload = await asyncio.to_thread(fetch, base, key)
        items = [
            {"id": str(m.get("id") or ""), "created": int(m.get("created") or 0)}
            for m in (payload.get("data") or []) if m.get("id")
        ]
        items.sort(key=lambda m: m["created"], reverse=True)
        picked = (next((m["id"] for m in items
                        if any(t in m["id"] for t in ("flash", "turbo"))), None)
                  or (items[0]["id"] if items else None)
                  or DEFAULT_MODEL)
    except Exception:  # noqa: BLE001 —— 选型失败不是启动失败,回落默认
        picked = DEFAULT_MODEL
    _auto_model_cache = picked
    return picked


def get_llm(model: str | None = None, *, api_key: str | None = None,
            base_url: str | None = None, temperature: float = 0.1,
            **model_kwargs: Any) -> Any:
    """返回一个可调用的聊天模型对象(具备 .invoke/.ainvoke)。

    Args:
        model: 模型名,默认取 ZAI_MODEL 环境变量,再默认 glm-4.6。
        api_key: 显式密钥,默认取 ZAI_API_KEY 环境变量。
        base_url: OpenAI 兼容端点,默认取 ZAI_BASE_URL 环境变量。
        temperature: 低温度——编排/抽取任务要稳定,不要发散。
        **model_kwargs: 透传给 ChatOpenAI 的字段(如 timeout=60、max_retries=1,
            脚本类调用用它防挂死;timeout 是 request_timeout 字段的别名)。

    Raises:
        RuntimeError: 没有配置 ZAI_API_KEY(测试请直接注入假模型,不走本工厂)。
    """
    # 延迟导入:无网络/无依赖的环境(纯单元测试)不必安装 langchain-openai
    from langchain_openai import ChatOpenAI

    key = api_key or os.environ.get("ZAI_API_KEY")
    if not key:
        raise RuntimeError(
            "未配置 ZAI_API_KEY:生产环境请设置后调用 get_llm();"
            "测试环境请注入 langchain_core.language_models.fake_chat_models 假模型。")
    return ChatOpenAI(
        model=model or os.environ.get("ZAI_MODEL", DEFAULT_MODEL),
        api_key=key,
        base_url=base_url or os.environ.get("ZAI_BASE_URL", DEFAULT_BASE_URL),
        temperature=temperature,
        **model_kwargs,
    )


class RequestLLM:
    """按请求动态解析 key/model 的聊天模型门面(BYOK 与前端模型切换的落点)。

    图在启动时构建一次并持有本门面;真正调用时按 contextvar 里的请求级覆盖
    (api_key/model)解析出 ChatOpenAI 实例,实例按 (key, model) 缓存不重复构造。
    无覆盖时回落环境变量/自动选型(与旧行为完全一致)。

    测试注入:factory 可替换(返回带 .ainvoke 的任意对象)。
    """

    def __init__(self, *, fallback_model: str | None = None,
                 factory: Callable[..., Any] | None = None,
                 **base_kwargs: Any) -> None:
        self._fallback_model = fallback_model
        self._factory = factory or get_llm
        self._base_kwargs = base_kwargs
        self._instances: dict[tuple[str, str], Any] = {}

    @property
    def model_name(self) -> str | None:
        # health 展示用:默认解析到的模型名
        return (os.environ.get("ZAI_MODEL") or self._fallback_model
                or DEFAULT_MODEL)

    def _resolve(self) -> Any:
        ov = _request_override.get() or {}
        key = ov.get("api_key") or os.environ.get("ZAI_API_KEY")
        model = (ov.get("model") or os.environ.get("ZAI_MODEL")
                 or self._fallback_model or DEFAULT_MODEL)
        if not key:
            raise RuntimeError(
                "未配置 API Key:请在界面设置里填写,或部署时配置 ZAI_API_KEY 环境变量。")
        ck = (str(key), str(model))
        if ck not in self._instances:
            self._instances[ck] = self._factory(
                model=model, api_key=key, **self._base_kwargs)
        return self._instances[ck]

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        return await self._resolve().ainvoke(messages, **kwargs)

    async def astream(self, messages: Any, **kwargs: Any) -> Any:
        return await self._resolve().astream(messages, **kwargs)
