"""统一工具调用咽喉(施工文档条目 5 的落地)。

为什么需要它
------------
施工前实测:`agent/graph.py` 有 53 处、`agent/skills.py` 8 处、`agent/api.py` 4 处
**直接** `await tools.call(...)`(合计 65 处),记账靠 51 处手写的 `_jr(name, args, res)`。
风险断言分散在三处(`core.py:validate_plan` 计划级、`skills.py` 拒绝资金技能自跑、
`bank.py:ScopedBankTools.call` 白名单),彼此不覆盖同一个时刻。

这个模块把所有银行工具调用收敛到一条可审计路径,后续再逐层加治理能力。

分批施工图(严格按批推进,一批一提交,每批全量测试)
--------------------------------------------------
- **B3(本批)**:纯转发 —— 只做 `tools.call` + 统一记账。**不改入参、不改返回值、
  不校验、不裁剪**。此时替换是纯机械的,diff 可逐行核对,任何红灯只可能是替换手误。
- B5/B6/B7:按子图分批迁移剩余调用点,并让 `_jr` 全部改走本模块。
- B8:入参 schema 校验 + 确定性修复注册表(迁入 `_repair_bill_args`)+ 风险断言
  + 大结果裁剪。**单列一批,绝不与迁移混做**。

不可协商的边界(施工文档 §1 全局禁区)
------------------------------------
1. 资金语义一行不得削弱:`bank.py:ScopedBankTools.call` 的白名单 fail-closed
   本模块**不接管也不绕过**,它仍是唯一执行期拦截;
2. `graph.py` 中 `verify_pay_password` 的手写记账(密码脱敏为 `***`)保留原样,
   不为一处例外给全链路加参数;
3. 记账结构必须逐字段等于 `{"tool","args","ok","error"}`,不得增删键
   (契约测试与 `_audited_tools` 都盯着它);
4. 金额一律整数分:本模块只透传 args,不解析、不换算、不重算;
5. 不新增 MCP 工具、不改 bank.db 表结构。
"""
from __future__ import annotations

from typing import Any


def journal_record(name: str, args: dict, result: Any) -> dict:
    """单条银行调用记录 —— 全项目记账的唯一口径。

    结构与迁移前 `graph.py._jr` 的产出**逐字段一致**:
    `{"tool": name, "args": args, "ok": err is None, "error": err}`。
    新增/删键会同时触发契约测试与 `_audited_tools` 红灯,禁止改动。

    args 做**浅拷贝**再存:记账记录是审计留痕,不能与调用方的字典共享同一个对象
    ——否则下游任何一次改写 `bank_calls[...]["args"]` 都会反向污染节点状态。
    (对拍测试 `test_journal_record_matches_legacy_field_by_field` 锁的是值等价,
    不是对象同一。)
    """
    err = result.get("error") if isinstance(result, dict) else None
    return {"tool": name,
            "args": dict(args) if isinstance(args, dict) else args,
            "ok": err is None,
            "error": err}


async def guarded_call(tools: Any, name: str, args: dict | None = None,
                       *, journal_args: dict | None = None) -> tuple[Any, dict]:
    """经咽喉调用一次银行工具,返回 `(result, 记账记录)`。

    B3 版本是**纯转发**:内部只做 `await tools.call(name, **args)`,随后按统一口径记账。
    下一批(B8)才会在 `tools.call` 前后插入 schema 校验 / 修复 / 风险断言 / 裁剪,
    届时**返回值语义不变**,调用方零改动。

    参数
    ----
    tools:  `BankTools` 或 `ScopedBankTools`(都有 `.call(name, **args)`)。
    name:   工具名。
    args:   传给工具的 kwargs。
    journal_args: 记进 `bank_calls` 的入参快照。默认等于 `args`;
            个别调用点会把入参加工后再传工具(如 `int(sub_id)`),
            但记账要保留原始值 —— 这时显式传 `journal_args` 保持与迁移前一致。
    """
    call_args = dict(args or {})
    result = await tools.call(name, **call_args)
    return result, journal_record(
        name, journal_args if journal_args is not None else call_args, result)


async def guarded_call_into(calls: list, tools: Any, name: str,
                            args: dict | None = None,
                            *, journal_args: dict | None = None) -> Any:
    """`guarded_call` 的便捷变体:记账记录直接 append 进节点的 `calls` 列表,只返回结果。

    用于"先建空 calls、再逐次追加"的写法(如 `_sub_rows`、`s_precheck`)。
    """
    result, record = await guarded_call(tools, name, args,
                                        journal_args=journal_args)
    calls.append(record)
    return result
