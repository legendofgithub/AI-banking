"""测试专用：进程内 MCP 工具传输层（不启子进程）。

为什么需要
----------
`agent/bank.py` 的 stdio 传输在**每次工具调用**都新起一个
`python -m bank_core.mcp_server` 子进程（见 agent/bank.py 模块注释第 3 条）。
实测单个子进程的固定开销约 2.27s（import fastmcp 1.08s + import bank_core 1.19s），
再加解释器启动与 MCP 握手，一次调用 2-4s。测试里动辄几十次工具调用，
这就是全量 15-25 分钟的主因——**不是用例多，是每次调用都在冷启动一个 Python 进程**。

这里把同一批工具函数在当前进程内直接调用，绕开 stdio 与 MCP 协议，
保留的契约与真实链路逐条对齐：

1. **单个工具返回值的解包语义不变**：本模块返回 `[{"type":"text","text": <json>}]`，
   与 fastmcp 序列化后的内容块同形，`agent.bank.unwrap()` 原样可解
   （含空列表、`{"result": ...}` 包装两种历史踩坑形态）。
2. **工具级异常不抛出**：fastmcp 把业务异常包成
   `Error calling tool '<name>': <msg>` 文本返回，这里逐字复刻，
   保证 `BankTools.call()` 仍得到 `{"error": ...}` 而不是异常。
3. **(库, 用户) 隔离语义不变**：真实链路靠子进程 env（`BANK_CORE_DB` /
   `BANK_USER_ID`）隔离；这里按同一键缓存一套 service 单例，
   等价于"一个子进程服务一个库+一个用户"。

开关
----
默认启用（仅为测试提速）。设 `BANK_TESTS_STDIO=1` 可强制走真实 stdio 子进程，
用于怀疑"进程内替身与真实链路不一致"时对照验证。

边界
----
本模块**只改测试侧**，不碰 agent/ 与 bank_core/ 任何业务代码。
MCP 协议本身（工具发现、参数 schema 校验、内容块编解码）在这条路径上不被覆盖，
那些由 `tests/test_contract.py` 与 `scripts/mcp_smoke.py` 负责。
"""

from __future__ import annotations

import json
import os
from typing import Any

# 与 agent.bank.REQUIRED_TOOLS 同源：装载后校验工具齐全
from agent.bank import REQUIRED_TOOLS, BankTools

_ENV_SWITCH = "BANK_TESTS_STDIO"

# (db_path, user_id) -> {"conn": Connection, "globals": {名字: 新值}}
_bundles: dict[tuple[str, int], dict[str, Any]] = {}


def _build_bundle(db_path: str, user_id: int) -> dict[str, Any]:
    """按真实 mcp_server 的模块级装配方式，为一个 (库,用户) 建一套 service。"""
    from bank_core.analysis import AnalysisService
    from bank_core.db import init_db
    from bank_core.events import EventService
    from bank_core.ledger import LedgerService
    from bank_core.linkage import LinkageService
    from bank_core.wealth import WealthService

    conn = init_db(db_path)
    return {
        "conn": conn,
        # 键名与 bank_core.mcp_server 的模块级全局一一对应：
        # 工具函数体内对这些名字的解析发生在调用时，故替换这些全局即可整体换库/换用户。
        "globals": {
            "_conn": conn,
            "_ledger": LedgerService(conn, user_id),
            "_analysis": AnalysisService(conn, user_id),
            "_wealth": WealthService(conn, user_id),
            "_events": EventService(conn, user_id),
            "_linkage": LinkageService(conn, user_id),
        },
    }


def _get_bundle(db_path: str, user_id: int) -> dict[str, Any]:
    key = (db_path, user_id)
    bundle = _bundles.get(key)
    if bundle is None:
        bundle = _build_bundle(db_path, user_id)
        _bundles[key] = bundle
    return bundle


def _activate(db_path: str, user_id: int) -> None:
    """把目标 bundle 的单例装回 mcp_server 模块全局（等价于切子进程）。"""
    from bank_core import mcp_server

    # 保证 bank_core 的延迟路径解析在直连读库时也指向同一库
    os.environ["BANK_CORE_DB"] = db_path
    os.environ["BANK_USER_ID"] = str(user_id)
    for name, value in _get_bundle(db_path, user_id)["globals"].items():
        setattr(mcp_server, name, value)


class _InProcessTool:
    """一个工具函数的进程内替身，`ainvoke` 形态与适配器 StructuredTool 一致。

    只实现被 `BankTools.call()` 用到的接口（`ainvoke`），
    返回形如 fastmcp 序列化后的内容块列表。
    """

    __slots__ = ("name", "_fn", "_db_path", "_user_id")

    def __init__(self, name: str, fn: Any, db_path: str, user_id: int):
        self.name = name
        self._fn = fn
        self._db_path = db_path
        self._user_id = user_id

    async def ainvoke(self, args: dict[str, Any] | None = None) -> list[dict[str, str]]:
        _activate(self._db_path, self._user_id)
        params = dict(args or {})
        try:
            # tool 装饰器返回的是原始函数，直接调用等价于服务端执行
            result = self._fn(**params)
        except Exception as exc:  # noqa: BLE001 —— 复刻 fastmcp 的业务异常包装
            return [{"type": "text", "text": f"Error calling tool '{self.name}': {exc}"}]
        # fastmcp 对 None 返回值产出**零个内容块**，与空列表同一条路径；
        # agent.bank.unwrap() 把它短路成业务空列表 []。
        # 若不特判，json.dumps(None) 会得到字符串 "null"，
        # 解包后变成 {"error": "null"}——把"用户没做过风险测评"误报成工具故障，
        # 编排层会走进错误分支。实测 get_risk_profile() 就是这个形态。
        if result is None:
            return []
        return [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}]


async def load_bank_tools_inprocess(db_path: Any = None, user_id: int = 1) -> BankTools:
    """`agent.bank.load_bank_tools` 的进程内替代，签名与返回类型完全一致。

    工具清单必须从 FastMCP 注册表取（`mcp.list_tools()`），**不能**扫模块的
    public callable：`bank_core/mcp_server.py` 里还有非工具的公有函数（如 `main`），
    扫 `dir()` 会多认出一个，`/health` 报 48 个工具、`test_health_and_validation`
    随即失败（踩过一次）。注册表还自带真实工具名，比函数名更准。
    """
    from bank_core import mcp_server

    if db_path is None:
        db_path = os.environ.get("BANK_CORE_DB") or (os.path.join(os.getcwd(), "data", "bank.db"))
    db_str = os.fspath(db_path)

    mapping: dict[str, Any] = {}
    for tool in await mcp_server.mcp.list_tools():
        fn = getattr(tool, "fn", None) or getattr(mcp_server, tool.name, None)
        if fn is None:  # pragma: no cover - 注册表与模块不同步时的显式失败
            raise RuntimeError(f"工具 {tool.name} 在 bank_core.mcp_server 里找不到实现")
        mapping[tool.name] = _InProcessTool(tool.name, fn, db_str, user_id)

    missing = [n for n in REQUIRED_TOOLS if n not in mapping]
    if missing:
        raise RuntimeError(f"bank_core MCP 缺少必需工具: {missing}")
    return BankTools(mapping, db_path=db_str)


def enabled() -> bool:
    """进程内替身是否启用（默认启用；BANK_TESTS_STDIO=1 时退回真实 stdio）。"""
    return os.environ.get(_ENV_SWITCH, "") not in {"1", "true", "yes"}


def close_all() -> None:
    """关闭并清空所有缓存的连接（会话结束时调用，避免句柄泄漏）。"""
    for bundle in _bundles.values():
        try:
            bundle["conn"].close()
        except Exception:  # noqa: BLE001 —— 已关闭/已删库都无害
            pass
    _bundles.clear()
