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
- ZAI_PROVIDER  选填,部署方默认协议:openai(兼容) | anthropic(Claude)
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import math
import os
import urllib.request
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlparse

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
DEFAULT_MODEL = "glm-4.6"
ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"

# 自动选型结果缓存(每进程一次;端点清单变化靠重启生效)
_auto_model_cache: str | None = None

# 请求级 LLM 覆盖(BYOK:界面填的 Key/模型/端点随请求生效;无覆盖走环境变量)
_request_override: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "request_llm_override", default=None)

_log = logging.getLogger(__name__)


def _is_connection_error(exc: BaseException) -> bool:
    """是否连接类异常(openai 的 APIConnectionError,含超时子类)。

    openai 是 langchain-openai 的传递依赖,保持延迟导入:纯单测环境不必安装。
    只认连接类——鉴权/限流/业务异常换新实例也救不了,不该触发自愈。
    """
    try:
        from openai import APIConnectionError
    except ImportError:
        return False
    return isinstance(exc, APIConnectionError)


def provider_family(provider: str | None) -> str:
    """供应商 → 协议族。Claude/Anthropic 走原生 messages,其余走 OpenAI 兼容。"""
    return ("anthropic" if str(provider or "").lower() in ("anthropic", "claude")
            else "openai")


# 估算口径(施工文档条目 2):CJK ≈ 1 token/字符,其余 ≈ 0.25 token/字符(4 字符 ≈ 1 token)。
# 只覆盖中日韩统一表意文字 + 兼容表意文字 + CJK 标点/全角区,够用即可。
_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
               (0x3000, 0x303F), (0xFF00, 0xFFEF))

# 每条消息的角色/分隔开销(tokenizer 实测经验值)
_MESSAGE_OVERHEAD = 4


def estimate_tokens(text: Any) -> int:
    """字符数 → token 数的**估算**值(端点拿不到 usage 时的回落口径)。

    这是纯函数:无 I/O、无日志、无随机、不读全局状态,相同输入必然相同输出。
    为什么必须这么干净:全量测试里**所有假模型路径都会走这条分支**
    (FakeMessagesListChatModel 的 chunk 不带 usage_metadata),任何副作用都会被
    放大到 413 条用例。

    精度只求量级正确——它只用于"是否超预算"的阈值判断(条目 4 的 L2),
    不用于计费,真实计量一律以端点回传的 usage 为准。
    """
    if text is None:
        return 0
    if not isinstance(text, str):
        text = str(text)
    if not text:
        return 0
    cjk = sum(1 for ch in text
              if any(lo <= ord(ch) <= hi for lo, hi in _CJK_RANGES))
    return math.ceil(cjk + (len(text) - cjk) * 0.25)


def estimate_messages_tokens(messages: Any) -> int:
    """估算消息列表的 token 数(逐条 content 累加 + 每条角色开销)。

    同样纯函数:只依赖对象的 `.content` 属性,不 import langchain,
    因此条目 4 的窗口预算判断可以脱离图做单测。
    """
    total = 0
    for m in messages or ():
        content = getattr(m, "content", m)
        total += estimate_tokens(content) + _MESSAGE_OVERHEAD
    return total


# 推理模型识别:命中这些 hint 的模型默认开启原生深度思考(reasoning_content)
REASONING_MODEL_HINTS = ("reasoner", "r1", "thinking",
                         "deepseek-reasoner", "deepseek-r1")


def _default_thinking(model: str | None, provider: str | None) -> bool:
    """是否默认对给定模型开启原生推理。

    - 环境变量 REASONING_MODEL 若等于该模型名 → 强制开启;
    - 否则按模型名 hint 自动判定(如 deepseek-r1 / glm-thinking)。
    """
    env = os.environ.get("REASONING_MODEL")
    if env and env == (model or ""):
        return True
    mid = (model or "").lower()
    return any(h in mid for h in REASONING_MODEL_HINTS)


# 可见答案(播报/闲聊)默认换用的"会思考"映射:同一供应商下的推理模型。
# 深度思考的效果靠它——路由/抽取仍用快模型,只有最终回答走这里。
_THINKING_MODEL_MAP: dict[str, str] = {
    "deepseek-chat": "deepseek-reasoner",
}


def thinking_model_for(model: str | None, provider: str | None) -> str:
    """给定编排模型 → 可见答案应使用的推理模型。

    - 环境变量 THINKING_MODEL 显式指定时优先;
    - 已经是推理模型(r1/reasoner/thinking)→ 原样返回;
    - DeepSeek 官方双模型 → deepseek-chat 换 deepseek-reasoner;
    - 其他(智谱 GLM/OpenAI 兼容网关)→ 同名模型,靠 do_think 打开思考
      (GLM 支持 thinking 字段;不支持的原样应答,优雅降级)。
    """
    env = os.environ.get("THINKING_MODEL")
    if env:
        return env
    model = str(model or "")
    if _default_thinking(model, provider):
        return model
    return _THINKING_MODEL_MAP.get(model.lower(), model)


def normalize_base_url(value: str | None) -> str | None:
    """校验并规范化用户填的 Base URL(只允许 http/https,去掉账号密码与尾斜杠)。"""
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Base URL 必须是 http(s) 开头的完整地址")
    if parsed.username or parsed.password:
        raise ValueError("Base URL 不能包含账号密码")
    return raw.rstrip("/")


def set_request_llm(*, api_key: str | None = None,
                    model: str | None = None,
                    base_url: str | None = None,
                    provider: str | None = None,
                    enable_thinking: bool | None = None) -> contextvars.Token:
    return _request_override.set({
        "api_key": api_key, "model": model,
        "base_url": base_url, "provider": provider,
        "enable_thinking": enable_thinking})


def reset_request_llm(token: contextvars.Token) -> None:
    _request_override.reset(token)


def _created_epoch(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return 0


def fetch_models_json(base_url: str, api_key: str,
                      provider: str = "openai") -> Any:
    """同步拉取端点模型清单(在线程池里跑,避免阻塞事件循环)。

    OpenAI 兼容(DeepSeek/OpenAI/自定义网关):GET /models + Bearer。
    Claude:GET /v1/models + x-api-key / anthropic-version;把响应归一成
    {"data":[{"id","created"}]} 供调用方统一取用。
    """
    base = normalize_base_url(base_url) or DEFAULT_BASE_URL
    if provider_family(provider) == "anthropic":
        headers = {"x-api-key": api_key,
                   "anthropic-version": ANTHROPIC_VERSION}
    else:
        headers = {"Authorization": f"Bearer {api_key}"}
    req = urllib.request.Request(base + "/models", headers=headers)
    with urllib.request.urlopen(req, timeout=10) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if provider_family(provider) == "anthropic":
        items = []
        for item in payload.get("data") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            items.append({"id": str(item["id"]),
                          "created": _created_epoch(item.get("created_at"))})
        return {"data": items}
    return payload


async def pick_default_model(
    *, api_key: str | None = None, base_url: str | None = None,
    provider: str | None = None,
    fetch_json: Callable[[str, str], Any] | None = None,
) -> str:
    """按端点实际可用的模型自动选编排层默认模型(与前端探测同一清单)。

    选型规则(编排层全是受限任务——分类/抽取/播报,快比深重要):
    1. 取 created 最新的 flash/turbo 级模型(flashx 视同 flash);
    2. 没有快模型时,取 created 最新的任意模型;
    3. 清单为空/接口失败 → DEFAULT_MODEL(glm-4.6,离线兜底)。
    显式设置了 ZAI_MODEL 的调用方应直接用环境变量,不走本函数。

    fetch_json 仅供测试注入(base_url, api_key) -> dict;缺省按 provider 真实拉取。
    """
    global _auto_model_cache
    if _auto_model_cache:
        return _auto_model_cache
    key = api_key or os.environ.get("ZAI_API_KEY") or ""
    base = base_url or os.environ.get("ZAI_BASE_URL") or DEFAULT_BASE_URL
    try:
        if fetch_json is not None:
            payload = await asyncio.to_thread(fetch_json, base, key)
        else:
            payload = await asyncio.to_thread(
                fetch_models_json, base, key, provider or "openai")
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


class AnthropicChatModel:
    """Claude 原生 /v1/messages 适配器(仅实现图需要的 ainvoke/astream)。

    编排图只把 System/Human/AI 文本消息交给模型,不依赖 OpenAI 的 function
    calling;因此这里做最小协议转换,不额外引入 anthropic SDK。
    """

    def __init__(self, *, model: str, api_key: str,
                 base_url: str | None = None, temperature: float = 0.1,
                 timeout: float | None = None, **_kwargs: Any) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = (base_url or ANTHROPIC_BASE_URL).rstrip("/")
        self.temperature = temperature
        self.timeout = timeout or 60

    async def ainvoke(self, messages: Any, **_kwargs: Any) -> Any:
        import httpx
        from langchain_core.messages import (AIMessage, HumanMessage,
                                             SystemMessage)

        system_parts: list[str] = []
        chat: list[dict[str, str]] = []
        for message in messages or []:
            content = getattr(message, "content", "")
            text = content if isinstance(content, str) else str(content)
            if isinstance(message, SystemMessage):
                system_parts.append(text)
            elif isinstance(message, HumanMessage):
                chat.append({"role": "user", "content": text})
            elif isinstance(message, AIMessage):
                chat.append({"role": "assistant", "content": text})
        # Anthropic 要求 user/assistant 交替;连续同角色合并
        merged: list[dict[str, str]] = []
        for item in chat:
            if merged and merged[-1]["role"] == item["role"]:
                merged[-1]["content"] += "\n\n" + item["content"]
            else:
                merged.append(dict(item))
        if not merged:
            merged = [{"role": "user", "content": ""}]
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "temperature": self.temperature,
            "messages": merged,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        headers = {"x-api-key": self.api_key,
                   "anthropic-version": ANTHROPIC_VERSION,
                   "content-type": "application/json"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}/messages",
                                     json=payload, headers=headers)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Anthropic API {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = "".join(
            block.get("text", "")
            for block in (data.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text")
        return AIMessage(content=text)

    async def astream(self, messages: Any, **kwargs: Any):
        # 图目前只用 ainvoke;保留逐块接口供将来流式播报直接复用
        yield await self.ainvoke(messages, **kwargs)


def get_llm(model: str | None = None, *, api_key: str | None = None,
            base_url: str | None = None, provider: str | None = None,
            temperature: float = 0.1, enable_thinking: bool | None = None,
            **model_kwargs: Any) -> Any:
    """返回一个可调用的聊天模型对象(具备 .invoke/.ainvoke)。

    Args:
        model: 模型名,默认取 ZAI_MODEL 环境变量,再默认 glm-4.6。
        api_key: 显式密钥,默认取 ZAI_API_KEY 环境变量。
        base_url: 端点地址;Claude 默认 Anthropic,其余取 ZAI_BASE_URL。
        provider: deepseek/openai/anthropic/custom;Claude 走原生协议。
        temperature: 低温度——编排/抽取任务要稳定,不要发散。
        **model_kwargs: 透传字段(如 timeout=60、max_retries=1)。

    Raises:
        RuntimeError: 没有配置 API Key(测试请直接注入假模型,不走本工厂)。
    """
    family = provider_family(provider or os.environ.get("ZAI_PROVIDER"))
    key = api_key or os.environ.get("ZAI_API_KEY")
    if not key:
        raise RuntimeError(
            "未配置 API Key:请在界面「大模型管理」里接入供应商,"
            "或部署时设置 ZAI_API_KEY 环境变量。")
    if family == "anthropic":
        return AnthropicChatModel(
            model=model or "claude-sonnet-4-5",
            api_key=key,
            base_url=base_url or ANTHROPIC_BASE_URL,
            temperature=temperature,
            **model_kwargs)
    # 延迟导入:无网络/无依赖的环境(纯单元测试)不必安装 langchain-openai
    from langchain_openai import ChatOpenAI

    mk = dict(model_kwargs)
    do_think = (enable_thinking if enable_thinking is not None
                else _default_thinking(model, provider))
    # 智谱 GLM thinking 模式需在请求体带 thinking 字段;DeepSeek 等推理模型
    # 靠 reasoning_content 自动返回,无需额外 body。
    # 注意:必须放进 ChatOpenAI 的 model_kwargs 里——作为顶层构造参数传入时
    # langchain 会打 UserWarning("thinking is not default parameter"),虽仍能
    # 透传但不规范。
    if do_think and (str(provider or "").lower() == "zhipuai"
                     or str(model or "").lower().startswith("glm")):
        inner = mk.get("model_kwargs")
        if not isinstance(inner, dict):
            inner = {}
        inner.setdefault("thinking", {"type": "enabled"})
        mk["model_kwargs"] = inner
    model_name = model or os.environ.get("ZAI_MODEL", DEFAULT_MODEL)
    # DeepSeek 系走官方适配器:langchain-openai 的流式不透出 reasoning_content
    # (1.6.2 源码明确"not extracted"),ChatDeepSeek 才会把它逐 token 塞进
    # additional_kwargs.reasoning_content——前端思考面板的数据来源。
    # 未安装 langchain-deepseek 时降级 ChatOpenAI(能答,但没有思考流)。
    if "deepseek" in model_name.lower():
        try:
            from langchain_deepseek import ChatDeepSeek
            return ChatDeepSeek(
                model=model_name,
                api_key=key,
                api_base=base_url or os.environ.get(
                    "ZAI_BASE_URL", "https://api.deepseek.com"),
                temperature=temperature,
                **mk)
        except ImportError:
            pass
    return ChatOpenAI(
        model=model_name,
        api_key=key,
        base_url=base_url or os.environ.get("ZAI_BASE_URL", DEFAULT_BASE_URL),
        temperature=temperature,
        **mk,
    )


class RequestLLM:
    """按请求动态解析 key/model 的聊天模型门面(BYOK 与前端模型切换的落点)。

    图在启动时构建一次并持有本门面;真正调用时按 contextvar 里的请求级覆盖
    (api_key/model/base_url/provider)解析出模型实例,按连接参数缓存不重复构造。
    无覆盖时回落环境变量/自动选型(与旧行为完全一致)。

    自愈(2026-10-28 宕机复盘):实例内部是 openai/httpx 客户端,连接池一旦
    卡死(实测:手机端流式中途断开泄漏连接)会对同一缓存键**永久**失败,
    其他实例却全然正常,只能重启进程。构造实例是零 I/O 的内存操作——凡连接类
    异常即逐出缓存实例,首响应/首块吐出前换新实例透明重试一次,调用方无感;
    已吐块后失败只逐出不重试(无声重发会重复内容),下一轮调用自愈。

    测试注入:factory 可替换(返回带 .ainvoke 的任意对象)。
    """

    def __init__(self, *, fallback_model: str | None = None,
                 factory: Callable[..., Any] | None = None,
                 **base_kwargs: Any) -> None:
        self._fallback_model = fallback_model
        self._factory = factory or get_llm
        self._base_kwargs = base_kwargs
        self._instances: dict[tuple[str, str, str, str, bool, float], Any] = {}

    @property
    def model_name(self) -> str | None:
        # health 展示用:默认解析到的模型名
        return (os.environ.get("ZAI_MODEL") or self._fallback_model
                or DEFAULT_MODEL)

    def _evict(self, ck: tuple, exc: BaseException) -> None:
        """连接类异常即逐出缓存实例(自愈的落点,配合 _is_connection_error)。"""
        if self._instances.pop(ck, None) is not None:
            _log.warning("LLM 连接类异常,已逐出缓存实例待重建: model=%s err=%s: %s",
                         ck[3], type(exc).__name__, str(exc)[:200])

    def _resolve(self, strong: bool = False,
                 temperature: float | None = None) -> tuple[Any, tuple]:
        """解析本次调用的模型实例,返回 (实例, 缓存键);自愈逐出要用键。"""
        ov = _request_override.get() or {}
        key = ov.get("api_key") or os.environ.get("ZAI_API_KEY")
        model = (ov.get("model") or os.environ.get("ZAI_MODEL")
                 or self._fallback_model or DEFAULT_MODEL)
        provider = (ov.get("provider") or os.environ.get("ZAI_PROVIDER")
                    or "openai")
        enable_thinking = ov.get("enable_thinking")
        # strong=可见答案(播报/闲聊):除非用户显式关闭深度思考
        # (enable_thinking=False),否则换用"会思考"的推理模型——
        # DeepSeek 效果的落点:reasoning_content 逐 token 流进思考面板。
        use_think_model = bool(strong) and enable_thinking is not False
        do_think = (enable_thinking if enable_thinking is not None
                    else _default_thinking(model, provider))
        eff_model = (thinking_model_for(model, provider)
                     if use_think_model else model)
        if use_think_model:
            do_think = True
        family = provider_family(provider)
        if family == "anthropic":
            base = ov.get("base_url") or ANTHROPIC_BASE_URL
        else:
            base = (ov.get("base_url") or os.environ.get("ZAI_BASE_URL")
                    or DEFAULT_BASE_URL)
        if not key:
            raise RuntimeError(
                "未配置 API Key:请在界面「大模型管理」里接入供应商,"
                "或部署时配置 ZAI_API_KEY 环境变量。")
        # temperature(人设功能)进缓存键:不同温度各自实例化,绝不共用
        ck = (family, str(base), str(key), str(eff_model), bool(do_think),
              float(temperature) if temperature is not None else -1.0)
        if ck not in self._instances:
            kwargs = dict(self._base_kwargs)
            if temperature is not None:
                kwargs["temperature"] = temperature
            self._instances[ck] = self._factory(
                model=eff_model, api_key=key, base_url=base,
                provider=provider, enable_thinking=do_think,
                **kwargs)
        return self._instances[ck], ck

    async def ainvoke(self, messages: Any, *, strong: bool = False,
                      temperature: float | None = None,
                      **kwargs: Any) -> Any:
        instance, ck = self._resolve(strong, temperature)
        try:
            return await instance.ainvoke(messages, **kwargs)
        except Exception as exc:
            if not _is_connection_error(exc):
                raise
            self._evict(ck, exc)
        # 自愈重试:首响应还没出炉,换全新实例重发一次,调用方无感
        instance, ck = self._resolve(strong, temperature)
        try:
            return await instance.ainvoke(messages, **kwargs)
        except Exception as exc:
            if _is_connection_error(exc):
                self._evict(ck, exc)
            raise

    async def astream(self, messages: Any, *, strong: bool = False,
                      temperature: float | None = None,
                      **kwargs: Any) -> Any:
        """代理底层模型的 astream(逐 token 异步生成器)。

        注意:langchain 的 astream 返回的是异步生成器,不能 await;
        必须逐块 yield 出来,供上层(agent.graph.ask_llm)累加下发。
        strong=True 时解析为"会思考"的模型(见 thinking_model_for)。
        temperature 是**人设功能的请求级参数**:必须在门面层拦截消费,
        不能透传给底层 astream(langchain 不认这个 kwargs)。
        自愈规则:连接类异常即逐出实例;首块吐出前失败换新实例透明重试
        一次(对消费方无损);已吐块后失败只逐出不重试(无声重发会重复
        内容),下一轮调用自然拿到新实例。finally 里显式 aclose 内层生成器,
        消费方中途弃流时连接确定性归还,不给连接池留悬空流。
        """
        instance, ck = self._resolve(strong, temperature)
        inner = instance.astream(messages, **kwargs)
        yielded = False
        retried = False
        try:
            while True:
                try:
                    chunk = await inner.__anext__()
                except StopAsyncIteration:
                    return
                except Exception as exc:
                    if not _is_connection_error(exc):
                        raise
                    self._evict(ck, exc)
                    if yielded or retried:
                        raise
                    retried = True  # 首块前失败:换新实例补试一次
                    instance, ck = self._resolve(strong, temperature)
                    inner = instance.astream(messages, **kwargs)
                    continue
                yielded = True
                yield chunk
        finally:
            aclose = getattr(inner, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 —— 收尾关闭失败不掩盖原异常
                    pass
