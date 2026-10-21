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

import pytest


@pytest.fixture(autouse=True)
def _isolate_agent_memory_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_MEMORY_DB", str(tmp_path / "agent_memory.sqlite"))
