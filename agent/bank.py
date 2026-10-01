"""bank_core MCP 工具装载与调用封装(照 scripts/mcp_smoke.py 的验证写法)。

关键事实(踩坑记录,均实测验证):
1. langchain-mcp-adapters 0.3.2:直接实例化 MultiServerMCPClient,
   不用 async with 包裹;await client.get_tools() 拉取工具清单。
2. stdio 连接的 env 若传 None,子进程只拿到 MCP SDK get_default_environment()
   的默认子集,BANK_CORE_DB 不会传入 → 必须显式传 {**os.environ, "BANK_CORE_DB": ...}。
3. 每次工具调用各自新起一个 stdio 会话(新开服务端子进程,用完即关),
   工具对象自持连接配置,图可以长期持有工具引用。
4. 工具返回值:适配器构造 StructuredTool(response_format="content_and_artifact"),
   实测在 langchain-core 1.6.3 下 tool.ainvoke 直接返回内容块列表
   [{"type":"text","text":...}] 而非 (content, artifact) 二元组;
   因此 unwrap 同时兼容 二元组 / 列表 / 字符串 三种形态。
5. 适配器默认 handle_tool_errors=True:bank_core 抛的 LedgerError 等
   以错误文本返回(fastmcp 包成 "Error calling tool '...': ..."),不抛异常;
   transport/会话失败才会真正 raise,故 call() 内再兜一层。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# bank_core 实际注册 41 个 @mcp.tool(新增 create/get/execute/cancel_linkage_plan 跨场景联动)
EXPECTED_TOOL_COUNT = 42


def _stdio_config(db_path: str, user_id: int = 1) -> dict[str, Any]:
    """构造指向 bank_core MCP 服务(stdio)的连接配置。

    user_id 经 BANK_USER_ID 注入子进程:每次工具调用各自新起 stdio 子进程,
    每个子进程天然绑定一个登录用户(默认 1=陈明)。工具对象自持连接配置,
    故按用户各装载一份 BankTools(编排层缓存)。
    """
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-m", "bank_core.mcp_server"],
        "cwd": str(PROJECT_ROOT),
        # 冒烟脚本踩坑:env 必须显式继承,否则子进程拿不到 BANK_CORE_DB/BANK_USER_ID
        "env": {**os.environ, "BANK_CORE_DB": db_path, "BANK_USER_ID": str(user_id)},
    }


async def load_bank_tools(db_path: str | os.PathLike | None = None,
                          user_id: int = 1) -> "BankTools":
    """从 bank_core MCP 服务装载全部银行工具,返回 BankTools 句柄。

    user_id: 登录用户(观光/未传默认 1)。工具按用户隔离装载,编排层负责缓存。
    """
    global _BANK_TOOLS
    from langchain_mcp_adapters.client import MultiServerMCPClient

    if db_path is None:
        db_path = os.environ.get("BANK_CORE_DB") or (PROJECT_ROOT / "data" / "bank.db")
    db_str = str(db_path)

    client = MultiServerMCPClient({"bank-core": _stdio_config(db_str, user_id)})
    tools = await client.get_tools()  # 列完即关(该会话只用于发现工具)
    mapping = {t.name: t for t in tools}
    missing = [n for n in REQUIRED_TOOLS if n not in mapping]
    if missing:
        raise RuntimeError(f"bank_core MCP 缺少必需工具: {missing}")
    return BankTools(mapping, db_path=db_str)


# 转账/AA 闭环必须的工具(M1 硬依赖,装载时即校验)
REQUIRED_TOOLS = (
    "get_accounts", "resolve_contact", "policy_check",
    "create_transfer_order", "confirm_transfer_order", "cancel_transfer_order",
    "create_split_bill", "get_split_bill", "settle_split_bill_item",
)


class BankTools:
    """银行工具集合:按名调用,统一解包返回值。

    call() 永不抛银行侧异常,错误统一成 {"error": <文本>},
    调用方(图节点)据此走解释/播报分支——钱的事情宁可失败可见,不可带病继续。
    """

    def __init__(self, tools: dict[str, Any], *, db_path: str):
        self._tools = tools
        self.db_path = db_path

    @property
    def names(self) -> list[str]:
        return sorted(self._tools)

    async def call(self, name: str, /, **args: Any) -> Any:
        """调用一个银行工具并解包。

        Returns:
            dict | list:正常业务返回(结构化 JSON);
            {"error": str}:工具级失败(MCP isError / 会话异常 / 工具不存在)。
        """
        tool = self._tools.get(name)
        if tool is None:
            return {"error": f"工具 {name} 未加载"}
        try:
            raw = await tool.ainvoke(args)
        except Exception as exc:  # noqa: BLE001 —— 会话/transport 层异常兜底
            return {"error": f"{type(exc).__name__}: {exc}"}
        return unwrap(raw)


def _text_blocks(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"]
    return []


def unwrap(result: Any) -> Any:
    """把适配器工具返回值解成业务 JSON;解不出则 {'error': 原始文本}。

    兼容三种形态(优先级从高到低):
    1. (content, artifact) 二元组 → artifact.structured_content
       (fastmcp 对 list 返回值可能包成 {"result": [...]});
    2. 内容块列表 [{"type":"text","text":...}] → 解析 text 的 JSON;
    3. 纯字符串 → 直接解析。

    踩坑(实测 fastmcp 3.4.7):工具返回空列表(如 list_events 空库)时适配器
    给的是"零个内容块"即 [],走文本解析分支会解不出任何东西,被误报成
    {'error': '工具返回无法解析: []'}——必须显式短路成业务空列表,
    否则编排层把"查无数据"当"银行故障"。
    """
    if isinstance(result, list) and not result:
        return []
    content, artifact = result, None
    if isinstance(result, tuple) and len(result) == 2:
        content, artifact = result

    sc = getattr(artifact, "structured_content", None) if artifact is not None else None
    if isinstance(sc, dict) and isinstance(sc.get("result"), (list, dict)):
        return sc["result"]
    if isinstance(sc, (dict, list)):
        return sc

    texts = _text_blocks(content)
    for text in texts:
        try:
            v = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(v, (dict, list)):
            if isinstance(v, dict) and isinstance(v.get("result"), (list, dict)):
                return v["result"]
            return v
    joined = " ".join(t for t in texts if t).strip()
    return {"error": joined or f"工具返回无法解析: {result!r}"}
