"""真实 GLM 连通性冒烟(只读,恰好一次真实模型调用)。

验证 agent/llm.get_llm() 配置的智谱 GLM OpenAI 兼容端点可用,并验证
agent/graph.ROUTER_SYS 意图分类路由提示词能被真实模型正确消费。

安全约定(铁律):
- 只做一次聊天补全(意图分类),绝不装载 bank_core 工具、绝不调用任何
  动钱/写库工具——本脚本连 load_bank_tools 都不 import;
- 可重复执行、无交互;不读写 data/bank.db。

用法:
  .venv/Scripts/python scripts/live_smoke.py

环境变量:
  ZAI_API_KEY   必填;ZAI_MODEL / ZAI_BASE_URL 选填(默认 glm-4.6 /
                https://open.bigmodel.cn/api/paas/v4/,见 agent/llm.py)

退出码:0 成功;2 缺 ZAI_API_KEY;1 调用/解析失败(网络、鉴权、模型名、输出不合规范)。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))  # 让 scripts/ 下也能 import agent


def _utf8_stdout() -> None:
    """Windows 控制台下强制 UTF-8 输出,避免中文乱码/编解码异常(同 mcp_smoke.py)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main() -> int:
    _utf8_stdout()
    print("== agent get_llm() 真实 GLM 冒烟(意图分类,单次调用,不碰银行工具)==")

    api_key = os.environ.get("ZAI_API_KEY")
    if not api_key:
        print("FAIL: 未设置环境变量 ZAI_API_KEY。")
        print("提示:生产/联调环境请 export ZAI_API_KEY=<智谱 API Key>;"
              "纯单元测试无需本脚本(测试用假模型)。")
        return 2

    from langchain_core.messages import HumanMessage, SystemMessage

    from agent.graph import ROUTER_SYS, json_from_content  # 真实路由提示词,单一事实源
    from agent.llm import DEFAULT_MODEL, get_llm

    model_name = os.environ.get("ZAI_MODEL", DEFAULT_MODEL)
    print(f"[0] 模型: {model_name}  端点: {os.environ.get('ZAI_BASE_URL', '(默认 open.bigmodel.cn/api/paas/v4)')}")

    try:
        llm = get_llm(timeout=60, max_retries=1)  # 脚本类调用防挂死
    except Exception as exc:  # noqa: BLE001 —— 构造失败也要落清晰错误
        print(f"FAIL: 模型构造失败: {type(exc).__name__}: {exc}")
        return 1

    try:
        ai = llm.invoke([
            SystemMessage(content=ROUTER_SYS),
            HumanMessage(content="帮我把五百块转给张三"),
        ])
    except Exception as exc:  # noqa: BLE001 —— 网络/鉴权/模型名等一切调用失败
        print(f"FAIL: 调用 {model_name} 失败: {type(exc).__name__}: {exc}")
        print("提示:检查 ZAI_API_KEY 是否有效、网络是否可达、"
              f"ZAI_MODEL 是否为可用模型名(当前 {model_name})、"
              "ZAI_BASE_URL 是否指向正确的 OpenAI 兼容端点。")
        return 1

    content = ai.content if isinstance(ai.content, str) else str(ai.content)
    print(f"[1] 原始返回: {content[:300]}")
    parsed = json_from_content(content)
    if not parsed or "intent" not in parsed:
        print("FAIL: 返回内容无法解析为含 intent 的 JSON(路由提示词要求只输出 JSON)。")
        return 1
    intent = parsed.get("intent")
    if intent != "transfer":
        print(f"FAIL: 预期意图 transfer(话术是转账),实际 {intent!r}。")
        return 1

    usage = getattr(ai, "usage_metadata", None)
    print(f"[2] 意图分类: {intent} ✓(话术「帮我把五百块转给张三」)")
    if usage:
        print(f"    token 用量: {usage}")
    print("== 冒烟通过(exit=0);全程未调用任何银行工具 ==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
