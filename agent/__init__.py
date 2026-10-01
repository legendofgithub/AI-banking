"""agent 包:LangGraph 编排层(M1)。

模块导航:
- llm.py    LLM 工厂(智谱 GLM OpenAI 兼容端点;测试注入假模型)
- state.py  对话状态定义(AgentState / 槽位)
- bank.py   bank_core MCP 工具装载与统一调用封装
- graph.py  StateGraph 组装:Router + 转账子图(定时/AA/同名消歧)+ interrupt 人工确认
- api.py    FastAPI SSE 端点(AI SDK UI Message Stream 协议,python -m agent.api)

用法:
    from agent.bank import load_bank_tools
    from agent.graph import build_agent_graph, make_sqlite_checkpointer
    from agent.llm import get_llm

    tools = await load_bank_tools("data/bank.db")
    saver, conn = await make_sqlite_checkpointer("data/agent_ckpt.sqlite")
    graph = build_agent_graph(get_llm(), tools, saver)
"""

__version__ = "0.1.0"
