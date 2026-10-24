"""全项目 pytest 共享夹具。

为什么要有这一条 autouse 夹具
----------------------------
Agent 长期记忆(`agent/memory.py`)默认写在 `data/agent_memory.sqlite`——
那是**运行库**,不入库、可被演示重置。若测试沿用默认路径,会:
1. 往真实库里灌测试用户(1/11)的经验,污染演示现场;
2. 让"第二次计划读到上次记忆"这类断言依赖上一次运行的残留。

因此每个用例都把 `AGENT_MEMORY_DB` 指向 tmp_path,记忆库隔离且跑完即弃。
(`tests/test_memory.py` 自己也会 monkeypatch 同一变量,后设置的生效,不冲突。)
"""

from __future__ import annotations

import sys

import pytest

import agent.bank as _bank_mod
import _mcp_inprocess


@pytest.fixture(autouse=True)
def _isolate_agent_memory_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_DB", str(tmp_path / "agent_memory.sqlite"))
    monkeypatch.setenv("AGENT_PERSONA_DB", str(tmp_path / "agent_persona.sqlite"))


def _make_inprocess_loader():
    # 必须是 async：真实 load_bank_tools 是协程，所有调用点都写的是
    # `await load_bank_tools(...)`，同步返回会直接 TypeError。
    async def load_bank_tools(db_path=None, user_id: int = 1):
        return await _mcp_inprocess.load_bank_tools_inprocess(db_path, user_id)

    return load_bank_tools


@pytest.fixture(autouse=True)
def _use_inprocess_bank_tools(monkeypatch):
    """把 `load_bank_tools` 换成进程内实现，省掉每次工具调用的子进程冷启动。

    为什么按模块扫而不是只改 `agent.bank`：本仓库多处是**模块级按名导入**
    （`from agent.bank import load_bank_tools`），函数名在导入时就已绑定到
    各模块自己的全局命名空间，改 `agent.bank.load_bank_tools` 影响不到它们。
    因此对所有已加载模块做一次身份比对（`is 原函数`）再替换，
    覆盖 tests/ 与 agent/api.py 等所有现存引用点。

    `BANK_TESTS_STDIO=1` 可整条关掉，退回真实 stdio 子进程做对照。
    """
    if not _mcp_inprocess.enabled():
        yield
        return

    original = _bank_mod.load_bank_tools
    loader = _make_inprocess_loader()

    rebound = []
    for module in list(sys.modules.values()):
        if getattr(module, "load_bank_tools", None) is original:
            monkeypatch.setattr(module, "load_bank_tools", loader, raising=False)
            rebound.append(module)

    monkeypatch.setattr(_bank_mod, "load_bank_tools", loader, raising=False)
    try:
        yield
    finally:
        # monkeypatch 会自己还原；这里只补一次兜底，防测试内 `import *` 残留
        for module in rebound:
            if getattr(module, "load_bank_tools", None) is loader:
                setattr(module, "load_bank_tools", original)


@pytest.fixture(scope="session", autouse=True)
def _close_inprocess_bundles():
    """会话结束关掉进程内替身缓存的所有 sqlite 连接。"""
    yield
    _mcp_inprocess.close_all()
