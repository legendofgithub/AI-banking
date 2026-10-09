"""真实 LLM 端到端评估:标准化剧本驱动编排图,量化 agent 质量。

与 tests/test_agent_graph.py 的假模型套路互补:这里走真模型(自动选型,
同 agent/api.py 生产路径:ZAI_MODEL 优先,否则 pick_default_model() 取最新
flash 级,当前 glm-5.3-flash),验证「提示词 + 真实抽取 + 确定性编排」整条
链路。真模型有波动,断言只贴业务事实(余额/订单状态/notice kind/DB 终态),
绝不贴播报措辞(见任务约定:抽取偶尔抽偏属正常,不许靠字符串匹配话术过关)。

隔离(全程进程内,不碰 8800 线上服务):
- 每个剧本独立临时库:tempfile 造目录,BANK_CORE_DB 指向它;先调
  bank_core.seed.seed() 确定性播种(12 个月流水,同 tests 惯例),再补插
  一个「张三」联系人——种子 CONTACTS 里没有张三(seed.py:34-41),而多数
  剧本要转给张三;王芳×2 由剧本 10 自己走 contact_add 流程建(顺带验流程);
- 银行工具走 MCP stdio 子进程(load_bank_tools(db),每调用一会话,
  见 agent/bank.py 踩坑记录),检查点器 AsyncSqliteSaver 也落临时文件;
- 驱动约定照抄 agent/api.py:327-331:新消息 ainvoke({'messages':
  [HumanMessage(text)]});停在 interrupt 时 aget_state 看 tasks[*].interrupts,
  有则按剧本的应答函数取回复 ainvoke(Command(resume=reply))。

防挂死(真 LLM 必备):
- 单轮问答上限 8 条用户消息(clarify/gate 自环图内已有 MAX_*_ROUNDS 封顶,
  这里再兜一层防图外死循环);
- 剧本级 asyncio.timeout 超时按失败记并继续跑完其余;
- get_llm(timeout=60, max_retries=1) 单次调用熔断(同 scripts/live_smoke.py)。

用法:
    python scripts/eval_agent.py --set smoke   # 2 个剧本(第1+第5),开发自验
    python scripts/eval_agent.py --set quick   # 前 6 个,编排层闸门用
    python scripts/eval_agent.py --set full    # 全部 25 个(12 基础 + 3 联动 + 8 账单/理财/卡片 + 2 订阅)
    python scripts/eval_agent.py --only linkage_create,linkage_due  # 定向跑(自验)

并发说明:剧本串行执行(并发=1,任务书上界 2 以内);单剧本失败记录后继续。

输出:控制台逐剧本进度 + 末尾一行 'EVAL RESULT: pass=N/M success_rate=0.xx'
(>=0.80 退出码 0,否则 1);scripts/eval_report.md(人可读表格)与
scripts/eval_results.json(机读)。

踩坑记录:
- aiosqlite 连接绑定创建它的事件循环(tests/test_agent_graph.py:159 同款
  结论):整个评估在单个 asyncio.run 里跑完;每剧本用完即关连接,断言
  失败/超时也关(Windows 实测残留工作线程会让进程退不出去)。
- 剧本超时被 cancel 后 conn.close() 可能卡在半途操作上,wait_for 5 秒兜底,
  绝不因清理失败拖死整个评估。
- 临时库用完不删:失败剧本可拿 DB 现场对账(路径打印在控制台);重复评估
  永远播新库,不存在脏数据累积。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))  # scripts/ 下也能 import agent/bank_core

from langchain_core.messages import HumanMessage  # noqa: E402
from langgraph.types import Command  # noqa: E402

from agent.bank import load_bank_tools  # noqa: E402
from agent.graph import build_agent_graph, make_sqlite_checkpointer  # noqa: E402
from agent.llm import get_llm, pick_default_model  # noqa: E402
from bank_core.linkage import REMINDER_HOUR, _next_occurrence  # noqa: E402
from bank_core.money import yuan_to_cents  # noqa: E402

# ----------------------------------------------------------------- 常量

SCENARIO_TIMEOUT_S = 300.0   # 单剧本墙钟上限,超时按失败记并继续
MAX_MSGS_PER_TURN = 8        # 单轮(一条开场消息起的问答循环)用户消息上限
RECURSION_LIMIT = 50         # 同 agent/api.py:88
SUCCESS_THRESHOLD = 0.80     # 编排层闸门阈值
REPORT_MD = Path(__file__).resolve().parent / "eval_report.md"
REPORT_JSON = Path(__file__).resolve().parent / "eval_results.json"


# ----------------------------------------------------------------- 只读查库(断言用,同 tests 惯例:直查临时库)


def _utf8_stdout() -> None:
    """Windows 控制台下强制 UTF-8 输出,避免中文乱码(同 scripts/live_smoke.py)。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _ro(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _balance(db: Path, account_id: int = 1) -> int:
    conn = _ro(db)
    try:
        return conn.execute(
            "SELECT balance_cents FROM accounts WHERE id=?", (account_id,)).fetchone()[0]
    finally:
        conn.close()


def _rows(db: Path, sql: str, args: tuple = ()) -> list[dict]:
    conn = _ro(db)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _orders(db: Path, to_name: str | None = None) -> list[dict]:
    if to_name is None:
        return _rows(db, "SELECT * FROM transfer_orders ORDER BY id")
    return _rows(db, "SELECT * FROM transfer_orders WHERE to_name=? ORDER BY id",
                 (to_name,))


def _txs(db: Path, *, tx_type: str | None = None,
         external_ref: str | None = None) -> list[dict]:
    sql = "SELECT * FROM transactions WHERE 1=1"
    args: list[Any] = []
    if tx_type:
        sql += " AND tx_type=?"
        args.append(tx_type)
    if external_ref:
        sql += " AND external_ref=?"
        args.append(external_ref)
    return _rows(db, sql, tuple(args))


def _contacts(db: Path, name: str | None = None) -> list[dict]:
    if name is None:
        return _rows(db, "SELECT * FROM contacts ORDER BY id")
    return _rows(db, "SELECT * FROM contacts WHERE name=? ORDER BY id", (name,))


def _split_items(db: Path) -> list[dict]:
    return _rows(db, "SELECT * FROM split_bill_items ORDER BY bill_id, id")


# ----------------------------------------------------------------- 剧本数据结构


@dataclass
class Turn:
    """一轮对话:开场消息 + interrupt 应答(函数或按序弹出的列表)。"""

    text: str
    # 应答器:payload(中断载荷 dict) -> 回复文本;None 表示本轮不该再被追问
    reply: Callable[[dict], str | None] | list[str] | None = None


@dataclass
class Ctx:
    """断言上下文:图终态 + 各轮中断记录 + 临时库与余额基线。"""

    db: Path
    base_balance: int                  # 账户1(活期)基线,分
    base_balance2: int = 0             # 账户2(理财专户)基线,分(联动锁定单断言用)
    base_orders: int = 0               # 转账单基线张数(种子自带 1 张「林悦家用」定时单)
    values: dict = field(default_factory=dict)  # 最终 aget_state().values(notice/bank_calls/…)
    turns: list[dict] = field(default_factory=list)  # [{text, interrupts, status}]

    @property
    def notice(self) -> dict:
        return self.values.get("notice") or {}

    @property
    def bank_call_tools(self) -> list[str]:
        return [c.get("tool") for c in self.values.get("bank_calls") or []]

    def interrupts(self, ptype: str | None = None) -> list[dict]:
        out = [p for t in self.turns for p in t["interrupts"]]
        if ptype:
            out = [p for p in out if p.get("type") == ptype]
        return out

    def last_ai_text(self) -> str:
        for m in reversed(self.values.get("messages") or []):
            content = getattr(m, "content", "")
            if type(m).__name__ == "AIMessage" and isinstance(content, str) and content.strip():
                return content
        return ""


@dataclass
class Scenario:
    name: str
    turns: list[Turn]
    assert_fn: Callable[[Ctx], None]
    desc: str = ""


def _fail(msg: str) -> None:
    raise AssertionError(msg)


def _kind_is(ctx: Ctx, kind: str) -> None:
    got = ctx.notice.get("kind")
    if got != kind:
        _fail(f"notice.kind 应为 {kind},实际 {got!r}(notice={ctx.notice})")


def _single_order(ctx: Ctx, to_name: str) -> dict:
    orders = _orders(ctx.db, to_name=to_name)
    if len(orders) != 1:
        _fail(f"应有恰好 1 张 to_name={to_name} 的订单,实际 {len(orders)} 张")
    return orders[0]


# ----------------------------------------------------------------- 常用应答器(防抽取波动:按载荷类型应答,不赌问题顺序)


def _transfer_reply(confirm: str, *, payee: str = "张三", amount: str = "10",
                    scheduled_hint: str = "明天早上9点") -> Callable[[dict], str]:
    """转账类剧本的通用应答:缺啥补啥,闸门给定文案(confirm/取消/犹豫)。"""
    def reply(payload: dict) -> str:
        ptype = payload.get("type")
        if ptype == "ask_slot":
            missing = payload.get("missing") or []
            if "payee" in missing:
                return f"给{payee}"
            if "scheduled_at" in missing:
                return scheduled_hint
            return f"{amount}元"
        return confirm  # confirm_transfer / pick_contact(同名时按尾号4444=张三)
    return reply


# ----------------------------------------------------------------- 剧本断言(1-15 基础/联动)


def _assert_transfer_simple(ctx: Ctx) -> None:
    _kind_is(ctx, "executed")
    o = _single_order(ctx, "张三")
    if o["status"] != "executed" or o["amount_cents"] != 1_000:
        _fail(f"订单应为 executed/1000分,实际 {o['status']}/{o['amount_cents']}")
    bal = _balance(ctx.db)
    if bal != ctx.base_balance - 1_000:
        _fail(f"余额应恰好 -10.00 元({ctx.base_balance - 1_000}),实际 {bal}")
    txs = _txs(ctx.db, external_ref=f"order:{o['id']}")
    if len(txs) != 1 or txs[0]["amount_cents"] != 1_000 or txs[0]["direction"] != "out":
        _fail(f"流水应恰一条 external_ref=order:{o['id']} 的 out/1000分,实际 {txs}")


def _assert_transfer_cancel(ctx: Ctx) -> None:
    _kind_is(ctx, "cancelled")
    o = _single_order(ctx, "张三")
    if o["status"] != "cancelled":
        _fail(f"订单状态应为 cancelled,实际 {o['status']}")
    if _balance(ctx.db) != ctx.base_balance:
        _fail(f"取消后余额应不变({ctx.base_balance}),实际 {_balance(ctx.db)}")


def _assert_transfer_overlimit(ctx: Ctx) -> None:
    _kind_is(ctx, "blocked")
    reasons = "".join(str(r) for r in ctx.notice.get("reasons") or [])
    if "限额" not in reasons:
        _fail(f"拦截理由应含『限额』,实际 {ctx.notice.get('reasons')!r}")
    if ctx.interrupts("confirm_transfer"):
        _fail("超限单不该进确认闸门")
    if _orders(ctx.db, to_name="张三"):
        _fail("超限单不该建单")
    if _balance(ctx.db) != ctx.base_balance:
        _fail("拦截后余额应不变")
    tools = ctx.bank_call_tools
    if "policy_check" not in tools or "create_transfer_order" in tools:
        _fail(f"审计轨迹应含 policy_check 且无 create_transfer_order,实际 {tools}")


def _assert_transfer_clarify(ctx: Ctx) -> None:
    asks = ctx.interrupts("ask_slot")
    ok_ask = any("转" in str(p.get("question") or "")
                 and ("谁" in str(p.get("question") or "")
                      or "多少" in str(p.get("question") or ""))
                 for p in asks)
    if not ok_ask:
        _fail(f"应出现缺槽反问(问题含『转』与『谁/多少』),实际 {asks}")
    _kind_is(ctx, "executed")
    o = _single_order(ctx, "张三")
    if o["status"] != "executed" or o["amount_cents"] != 1_000:
        _fail(f"补槽后应 executed/1000分,实际 {o['status']}/{o['amount_cents']}")
    if _balance(ctx.db) != ctx.base_balance - 1_000:
        _fail(f"余额应恰好 -10.00 元,实际 {_balance(ctx.db)}(基线 {ctx.base_balance})")


def _assert_aa_create(ctx: Ctx) -> None:
    _kind_is(ctx, "split_created")
    bills = _rows(ctx.db, "SELECT * FROM split_bills")
    if len(bills) != 1:
        _fail(f"split_bills 应新增恰 1 单,实际 {len(bills)}")
    if bills[0]["total_cents"] != 30_000:
        _fail(f"账单总额应为 30000 分,实际 {bills[0]['total_cents']}")
    items = _split_items(ctx.db)
    if len(items) != 3:
        _fail(f"应 3 个分摊项(我/张三/老王),实际 {len(items)} 项:{items}")
    shares = sorted(i["share_cents"] for i in items)
    if shares != [10_000, 10_000, 10_000]:
        _fail(f"三人应各 100.00 元(10000分),实际 {shares}")
    if _balance(ctx.db) != ctx.base_balance:
        _fail("发起 AA 不该动自己的余额")


def _assert_contact_add(ctx: Ctx) -> None:
    _kind_is(ctx, "contact_added")
    rows = [c for c in _contacts(ctx.db, name="王芳") if c["phone"] == "13700001111"]
    if len(rows) != 1:
        _fail(f"contacts 应新增 王芳/13700001111 恰一行,实际 {rows}")
    if "测试" not in str(rows[0].get("note") or ""):
        _fail(f"备注应含『测试』,实际 {rows[0].get('note')!r}")


def _assert_chat_fallback(ctx: Ctx) -> None:
    if ctx.interrupts():
        _fail("闲聊不该出现任何 interrupt")
    if not ctx.last_ai_text().strip():
        _fail("回复不应为空")
    calls = ctx.values.get("bank_calls") or []
    if calls:
        _fail(f"闲聊不该碰银行工具,实际 {calls}")


def _assert_gate_hedge(ctx: Ctx) -> None:
    _kind_is(ctx, "cancelled")
    o = _single_order(ctx, "张三")
    if o["status"] != "cancelled":
        _fail(f"两问封顶应自动取消,订单实际 {o['status']}(绝不能 executed)")
    if _balance(ctx.db) != ctx.base_balance:
        _fail("封顶取消后余额应不变")


def _assert_transfer_duplicate(ctx: Ctx) -> None:
    _kind_is(ctx, "duplicate_order")
    orders = _orders(ctx.db, to_name="张三")
    if len(orders) != 1:
        _fail(f"重发同指令不该建新单,实际 {len(orders)} 张")
    if orders[0]["status"] != "executed":
        _fail(f"原单应保持 executed,实际 {orders[0]['status']}")
    if _balance(ctx.db) != ctx.base_balance - 1_000:
        _fail(f"不该二次扣款,余额应 {ctx.base_balance - 1_000},实际 {_balance(ctx.db)}")
    # 种子库自带 12 个月转出流水,扣款断言必须按本单 external_ref 精确圈定
    outs = _txs(ctx.db, external_ref=f"order:{orders[0]['id']}")
    if len(outs) != 1:
        _fail(f"本单扣款流水应恰一条,实际 {len(outs)} 条")


def _assert_transfer_disambiguate(ctx: Ctx) -> None:
    if not ctx.interrupts("pick_contact"):
        _fail("两个同名王芳应触发选人反问(pick_contact)")
    _kind_is(ctx, "executed")
    wangfang = _contacts(ctx.db, name="王芳")
    by_phone = {c["phone"]: c for c in wangfang}
    if "13700002222" not in by_phone or "13700003333" not in by_phone:
        _fail(f"应先建出两个同名王芳(2222/3333),实际 {wangfang}")
    o = _single_order(ctx, "王芳")
    want_id = by_phone.get("13700002222", {}).get("id")
    if o["to_contact_id"] != want_id:
        _fail(f"应转给尾号2222的王芳(contact_id={want_id}),实际 {o['to_contact_id']}")
    if o["status"] != "executed" or o["amount_cents"] != 500:
        _fail(f"订单应 executed/500分,实际 {o['status']}/{o['amount_cents']}")
    if _balance(ctx.db) != ctx.base_balance - 500:
        _fail(f"余额应恰好 -5.00 元,实际 {_balance(ctx.db)}(基线 {ctx.base_balance})")


def _assert_scheduled_transfer(ctx: Ctx) -> None:
    _kind_is(ctx, "scheduled_created")
    o = _single_order(ctx, "张三")
    if o["status"] != "scheduled":
        _fail(f"定时单状态应为 scheduled,实际 {o['status']}")
    if o["amount_cents"] != 5_000 or not o["scheduled_at"]:
        _fail(f"订单应 5000分且带 scheduled_at,实际 {o['amount_cents']}/{o['scheduled_at']!r}")
    if _balance(ctx.db) != ctx.base_balance:
        _fail(f"到期前不该扣款,余额应 {ctx.base_balance},实际 {_balance(ctx.db)}")
    if _txs(ctx.db, external_ref=f"order:{o['id']}"):
        _fail("定时单确认后绝不该立刻产生扣款流水")
    if "confirm_transfer_order" in ctx.bank_call_tools:
        _fail("定时单确认不该调用 confirm_transfer_order")


def _assert_settle_flow(ctx: Ctx) -> None:
    _kind_is(ctx, "split_progress")
    bills = _rows(ctx.db, "SELECT * FROM split_bills")
    if len(bills) != 1:
        _fail(f"split_bills 应恰 1 单,实际 {len(bills)}")
    items = [i for i in _split_items(ctx.db) if i["contact_name"] == "张三"]
    if len(items) != 1 or items[0]["paid"] != 1:
        _fail(f"张三的分摊项应 paid=1,实际 {items}")


# ------------------------------------------------------------ 联动剧本(13/14/15)断言
# 口径说明:任务书断言"账户2余额恰 +1000.00",但 bank_core 的
# confirm_transfer_order 只记转出侧(transfer_out 落账、to_account_tail 仅展示,
# ledger.py:278-292),理财专户不入账——这是已有实现的既定语义,单测有明确注释
# (tests/test_agent_graph.py:635-640「理财专户余额不变」)。评估按真实业务事实
# 断言:锁定单 executed + 活期恰好 -1000.00 + 账户2 零变化;绝不为凑指标断言假事实。


def _linkage_plans(ctx: Ctx) -> list[dict]:
    return _rows(ctx.db, "SELECT * FROM linkage_plans ORDER BY id")


def _linkage_single_plan(ctx: Ctx, want_status: str) -> tuple[dict, dict]:
    """取唯一联动计划行 + 解析后的动作列表,并校验计划状态。"""
    plans = _linkage_plans(ctx)
    if len(plans) != 1:
        _fail(f"linkage_plans 应恰 1 条,实际 {len(plans)} 条:"
              f"{[(p['id'], p['status']) for p in plans]}")
    plan = plans[0]
    if plan["status"] != want_status:
        _fail(f"计划状态应为 {want_status},实际 {plan['status']}")
    return plan, json.loads(plan["actions_json"])


def _assert_linkage_create(ctx: Ctx) -> None:
    if not ctx.interrupts("confirm_linkage"):
        _fail("建计划应出现 confirm_linkage 确认闸门")
    _kind_is(ctx, "linkage_locked")
    plan, actions = _linkage_single_plan(ctx, "active")
    if plan["budget_cents"] != 100_000:
        _fail(f"预算应为 1000.00 元(100000分),实际 {plan['budget_cents']}")
    # 动作名是 LLM 自由抽取(实测波动:'鲜花'/'订鲜花'都出现),按包含式判;
    # 金额是业务铁律,必须严格 300+200(与 linkage_due 的包含式口径一致)。
    whats = [str(a["what"]) for a in actions]
    flower = next((a for a in actions if "鲜花" in str(a["what"])), None)
    cake = next((a for a in actions if "蛋糕" in str(a["what"])), None)
    if flower is None or cake is None or len(actions) != 2:
        _fail(f"动作应为 鲜花+蛋糕 各一样,实际 {whats}")
    if flower["amount_cents"] != 30_000 or cake["amount_cents"] != 20_000:
        _fail(f"鲜花/蛋糕应 30000/20000 分,实际 "
              f"{flower['amount_cents']}/{cake['amount_cents']}")
    # 锁定转账单 executed:1000 元、备注'生日预留'、收款人自己(陈明)
    lock = _rows(ctx.db, "SELECT * FROM transfer_orders WHERE id=?",
                 (plan["lock_order_id"],))
    if len(lock) != 1 or lock[0]["status"] != "executed":
        _fail(f"锁定单应 executed,实际 {lock and lock[0]['status']}")
    if lock[0]["amount_cents"] != 100_000 or lock[0]["memo"] != "生日预留":
        _fail(f"锁定单应 100000分/备注'生日预留',实际 "
              f"{lock[0]['amount_cents']}/{lock[0]['memo']!r}")
    if lock[0]["to_name"] != "陈明":
        _fail(f"锁定单收款人应为用户本人'陈明',实际 {lock[0]['to_name']!r}")
    outs = _txs(ctx.db, external_ref=f"order:{lock[0]['id']}")
    if len(outs) != 1 or outs[0]["amount_cents"] != 100_000 \
            or outs[0]["direction"] != "out":
        _fail(f"锁定划转流水应恰一条 out/100000分,实际 {outs}")
    # 余额:活期恰好 -1000.00;理财专户零变化(bank_core 只记转出侧,见函数区口径说明)
    if _balance(ctx.db, 1) != ctx.base_balance - 100_000:
        _fail(f"活期余额应恰好 -1000.00 元({ctx.base_balance - 100_000}),"
              f"实际 {_balance(ctx.db, 1)}(基线 {ctx.base_balance})")
    if _balance(ctx.db, 2) != ctx.base_balance2:
        _fail(f"理财专户余额应不变({ctx.base_balance2}),实际 {_balance(ctx.db, 2)}")
    # scheduled_tasks:恰 2 条 reminder 提醒(种子库自带 1 条 scheduled_transfer 不算),
    # 都指向本计划。期望时刻按「计划实际绑定的事件」动态推导:
    #   提醒日 = _next_occurrence(事件日, 年重复) - 该动作 days_before,固定 09:00
    #   (bank_core/linkage.py:_next_occurrence/REMINDER_HOUR,直接复用勿重造)。
    # 踩坑:原断言硬编码 2026-09-24,只在编写日(2026-09-22)成立——那时台词
    # "9月26日"恰与种子生日(播种日+4)同日;日期漂移后图侧可能绑定种子事件
    # (播种日+4),也可能按台词新建事件(抽取器对已过的"9月26日"甚至给过去
    # 年份,bank_core 会把年度生日顺延到明年),三种合法绑定各有提醒日,
    # 硬编码必误报(2026-09-27 实测:绑定 2025-09-26→提醒 2027-09-24T09:00,
    # 资金侧全对却按失败记)。动态推导后断言强度不降反升:提醒条数、指向本计划、
    # 提前天数、触发时刻(09:00)四项全部精确相等。
    ev_rows = _rows(ctx.db, "SELECT * FROM user_events WHERE id=?", (plan["event_id"],))
    if len(ev_rows) != 1:
        _fail(f"计划应绑定恰 1 条 user_events(event_id={plan['event_id']}),"
              f"实际 {len(ev_rows)} 条")
    ev = ev_rows[0]
    next_day = _next_occurrence(str(ev["event_date"]), int(ev["repeat_yearly"] or 0))
    tasks = _rows(ctx.db,
                  "SELECT * FROM scheduled_tasks WHERE task_type='reminder'")
    if len(tasks) != 2:
        _fail(f"提醒任务应恰 2 条(鲜花/蛋糕),实际 {len(tasks)} 条")
    for t in tasks:
        payload = json.loads(t["payload_json"])
        if payload.get("plan_id") != plan["id"]:
            _fail(f"提醒任务应指向计划 {plan['id']},实际 {payload}")
        idx = int(payload.get("action_idx", -1))
        if not 0 <= idx < len(actions):
            _fail(f"提醒任务 action_idx={idx} 越界(actions 共 {len(actions)} 条)")
        days_before = int(actions[idx].get("days_before") or 2)
        want = f"{(next_day - timedelta(days=days_before)).isoformat()}" \
               f"T{REMINDER_HOUR:02d}:00:00"
        if str(t["run_at"]) != want:
            _fail(f"提醒应排在事件下次发生({next_day})前 {days_before} 天"
                  f"({want}),实际 {t['run_at']}")
    # 到期前绝不该有购买流水(铁律:锁定 ≠ 购买)
    if _rows(ctx.db, "SELECT * FROM transactions WHERE external_ref LIKE 'linkage:%'"):
        _fail("建计划+锁预算阶段绝不该产生 linkage 购买流水")


def _assert_linkage_due(ctx: Ctx) -> None:
    _kind_is(ctx, "linkage_progress")
    plan, actions = _linkage_single_plan(ctx, "active")  # 一买一跳,计划未完成
    flower = next((a for a in actions if "鲜花" in str(a["what"])), None)
    cake = next((a for a in actions if "蛋糕" in str(a["what"])), None)
    if flower is None or cake is None:
        _fail(f"动作应含 鲜花/蛋糕,实际 {[a['what'] for a in actions]}")
    if not flower["done"]:
        _fail(f"鲜花动作应 done=1,实际 {flower}")
    if cake["done"]:
        _fail(f"蛋糕动作不该被处理,实际 {cake}")
    fidx, cidx = actions.index(flower), actions.index(cake)
    # 鲜花:真实扣款流水(online,商户=计划动作里的商户,幂等键 linkage:{plan}:{idx})
    ftx = _txs(ctx.db, external_ref=f"linkage:{plan['id']}:{fidx}")
    if len(ftx) != 1 or ftx[0]["amount_cents"] != 30_000 \
            or ftx[0]["direction"] != "out" or ftx[0]["tx_type"] != "online":
        _fail(f"鲜花购买流水应恰一条 out/online/30000分,实际 {ftx}")
    if ftx[0]["counterparty"] != flower["merchant"] or not ftx[0]["counterparty"]:
        _fail(f"流水商户应与计划动作商户一致({flower['merchant']!r}),"
              f"实际 {ftx[0]['counterparty']!r}")
    if _txs(ctx.db, external_ref=f"linkage:{plan['id']}:{cidx}"):
        _fail("蛋糕被跳过,不该有任何购买流水")
    # 余额:活期 = 基线 - 锁定1000 - 鲜花300;理财专户不变
    want = ctx.base_balance - 100_000 - 30_000
    if _balance(ctx.db, 1) != want:
        _fail(f"活期余额应为 基线-1000-300={want},实际 {_balance(ctx.db, 1)}")
    if _balance(ctx.db, 2) != ctx.base_balance2:
        _fail(f"理财专户余额应不变({ctx.base_balance2}),实际 {_balance(ctx.db, 2)}")
    if ctx.bank_call_tools.count("execute_linkage_action") != 1:
        _fail(f"到期处理应恰一次 execute_linkage_action,实际 {ctx.bank_call_tools}")
    if not ctx.interrupts("confirm_linkage_action"):
        _fail("到期处理应出现逐动作闸门 confirm_linkage_action")


def _assert_linkage_cancel(ctx: Ctx) -> None:
    _kind_is(ctx, "linkage_cancelled")
    plan, _ = _linkage_single_plan(ctx, "cancelled")
    lock = _rows(ctx.db, "SELECT * FROM transfer_orders WHERE id=?",
                 (plan["lock_order_id"],))
    if len(lock) != 1 or lock[0]["status"] != "cancelled":
        _fail(f"锁定单应随计划一并 cancelled(未执行),实际 {lock and lock[0]['status']}")
    # 余额零变化(两账户都不动)
    if _balance(ctx.db, 1) != ctx.base_balance or _balance(ctx.db, 2) != ctx.base_balance2:
        _fail(f"取消后余额应零变化,实际 活期{_balance(ctx.db, 1)}(基线{ctx.base_balance})"
              f"/专户{_balance(ctx.db, 2)}(基线{ctx.base_balance2})")
    # 不该有任何划转/购买流水
    if _txs(ctx.db, external_ref=f"order:{plan['lock_order_id']}"):
        _fail("锁定单未执行,不该有划转流水")
    if _rows(ctx.db, "SELECT * FROM transactions WHERE external_ref LIKE 'linkage:%'"):
        _fail("取消的计划不该有任何购买流水")
    # 提醒任务全部撤销
    sts = _rows(ctx.db,
                "SELECT status FROM scheduled_tasks WHERE task_type='reminder'")
    if len(sts) != 2 or any(s["status"] != "cancelled" for s in sts):
        _fail(f"2 条提醒任务应全部 cancelled,实际 {[s['status'] for s in sts]}")
    tools = ctx.bank_call_tools
    if "cancel_linkage_plan" not in tools:
        _fail(f"审计轨迹应含 cancel_linkage_plan,实际 {tools}")
    if "confirm_transfer_order" in tools or "execute_linkage_action" in tools:
        _fail(f"取消路径绝不该动钱,实际 {tools}")


# ------------------------------------------------------------ 新管线剧本(16-23)断言
# 账单/理财/卡片三管线收官剧本(2026-09-27 扩展,六场景全覆盖收官):
# - 只读场景(16/17/18/21)贴「工具被调 + 入参对 + 零动钱」;
# - 写场景(19/20/22/23)贴「闸门载荷 + DB 终态 + bank_calls 两步语义」,
#   申购的扣费额按工具返回(fee_yuan)核对,不猜费率;
# - 申购产品选种子的「安享定期90天」(seed.py PRODUCTS DP002:R1 低风险、
#   起购恰 1000 元、申赎费率 0):种子库没有它的持仓 → 剧本 20 赎回时
#   关键词唯一命中,不会撞上种子自带的余额+货币基金/稳健纯债基金两笔持仓。


def _calls(ctx: Ctx, name: str) -> list[dict]:
    """bank_calls 里指定工具的调用记录(带 args,核对入参用)。"""
    return [c for c in (ctx.values.get("bank_calls") or [])
            if c.get("tool") == name]


def _cents_or_zero(yuan: Any) -> int:
    """元字符串→分;0/非法按 0(费率 0 的产品工具返回 '0.00',yuan_to_cents 会拒 0)。"""
    try:
        return yuan_to_cents(str(yuan))
    except ValueError:
        return 0


def _assert_balances_frozen(ctx: Ctx) -> None:
    """零动钱铁律:两账户余额都回基线。"""
    if _balance(ctx.db, 1) != ctx.base_balance or _balance(ctx.db, 2) != ctx.base_balance2:
        _fail(f"两账户余额应零变化,实际 活期{_balance(ctx.db, 1)}"
              f"(基线{ctx.base_balance})/专户{_balance(ctx.db, 2)}"
              f"(基线{ctx.base_balance2})")


def _prev_month(today: date | None = None) -> str:
    """上个月 'YYYY-MM'(与 graph.parse_period 的 _shift_month(-1) 同口径)。"""
    t = today or date.today()
    last_prev = t.replace(day=1) - timedelta(days=1)
    return f"{last_prev.year:04d}-{last_prev.month:02d}"


def _assert_bill_monthly(ctx: Ctx) -> None:
    _kind_is(ctx, "bill_report")
    if ctx.notice.get("ask") != "monthly_report":
        _fail(f"notice.ask 应为 monthly_report,实际 {ctx.notice.get('ask')!r}")
    calls = _calls(ctx, "monthly_report")
    if len(calls) != 1:
        _fail(f"monthly_report 应恰被调 1 次,实际 {len(calls)} 次:{ctx.bank_call_tools}")
    want = _prev_month()
    got = calls[0].get("args", {}).get("month")
    if got != want:
        _fail(f"月报月份应为上月 {want},实际 {got!r}(args={calls[0].get('args')})")
    if ctx.interrupts():
        _fail(f"只读账单分析不该有任何 interrupt,实际 {ctx.interrupts()}")
    _assert_balances_frozen(ctx)


def _assert_bill_anomaly(ctx: Ctx) -> None:
    _kind_is(ctx, "bill_report")
    if ctx.notice.get("ask") != "anomaly_scan":
        _fail(f"notice.ask 应为 anomaly_scan,实际 {ctx.notice.get('ask')!r}")
    calls = _calls(ctx, "detect_anomalies")
    if len(calls) != 1:
        _fail(f"detect_anomalies 应恰被调 1 次,实际 {len(calls)} 次:{ctx.bank_call_tools}")
    # 「最近三个月」按 parse_period 日历口径折算成 days(月末差 60-93 天,
    # 任务书 ≈90 的宽松带;绝不赌具体值,只圈住量级防「最近7天」之类误窗)
    days = calls[0].get("args", {}).get("days")
    if not isinstance(days, int) or not 60 <= days <= 92:
        _fail(f"异常检测窗口应 ≈90 天(60-92),实际 {days!r}(args={calls[0].get('args')})")
    _assert_balances_frozen(ctx)


def _assert_wealth_recommend(ctx: Ctx) -> None:
    _kind_is(ctx, "wealth_products")
    calls = _calls(ctx, "list_wealth_products")
    if not calls:
        _fail(f"推荐应调用 list_wealth_products,实际 {ctx.bank_call_tools}")
    # 种子库自带 C3 测评(seed.py:129-132),w_query 按等级确定性加 max_risk_level=3
    # (graph.py w_query 的 args 组装),断言带上它:不带=没按风险等级过滤(降级断言会放水)
    got_levels = [c.get("args", {}).get("max_risk_level") for c in calls]
    if 3 not in got_levels:
        _fail(f"推荐应带 max_risk_level=3(种子测评 C3),实际参数 {got_levels}")
    if not ctx.last_ai_text().strip():
        _fail("推荐播报不应为空")
    _assert_balances_frozen(ctx)


def _assert_wealth_subscribe(ctx: Ctx) -> None:
    _kind_is(ctx, "wealth_subscribed")
    gate_kinds = [str((p.get("order") or {}).get("kind"))
                  for p in ctx.interrupts("confirm_wealth")]
    if "subscribe" not in gate_kinds:
        _fail(f"申购应出现 confirm_wealth 闸门(kind=subscribe),实际 {gate_kinds}")
    # 持仓:安享定期90天(product_id=2)新增恰一行;本金=申购额-费用,费用按工具返回核对
    rows = _rows(ctx.db,
                 "SELECT * FROM wealth_holdings WHERE product_id=2 AND status='holding'")
    if len(rows) != 1:
        _fail(f"wealth_holdings 应新增安享定期90天恰一行,实际 {len(rows)} 行")
    result = ctx.notice.get("result") or {}
    fee_cents = _cents_or_zero(result.get("fee_yuan"))
    if rows[0]["principal_cents"] != 100_000 - fee_cents:
        _fail(f"持仓本金应为 1000元-费用({100_000 - fee_cents}分),"
              f"实际 {rows[0]['principal_cents']}(工具返回 fee_yuan={result.get('fee_yuan')!r})")
    if _balance(ctx.db, 1) != ctx.base_balance - 100_000:
        _fail(f"活期余额应恰减申购额 1000.00 元({ctx.base_balance - 100_000}),"
              f"实际 {_balance(ctx.db, 1)}(基线 {ctx.base_balance})")
    if _balance(ctx.db, 2) != ctx.base_balance2:
        _fail(f"申购不该动理财专户({ctx.base_balance2}),实际 {_balance(ctx.db, 2)}")
    # 两步铁律:bank_calls 恰两次 subscribe_product,先 confirmed=False 建单、
    # 后同参 confirmed=True 扣款(bank_core 两步语义,幂等靠此约定)
    subs = _calls(ctx, "subscribe_product")
    if len(subs) != 2:
        _fail(f"subscribe_product 应恰被调 2 次(建单+扣款),实际 {len(subs)} 次:"
              f"{ctx.bank_call_tools}")
    flags = [c.get("args", {}).get("confirmed") for c in subs]
    if flags != [False, True]:
        _fail(f"两步语义应为 confirmed False→True,实际 {flags}")
    for c in subs:
        if c.get("args", {}).get("product_id") != 2 \
                or _cents_or_zero(c.get("args", {}).get("amount_yuan")) != 100_000:
            _fail(f"两次调用都应 product_id=2/金额1000元,实际 {c.get('args')}")
    # 幂等键口径(2026-10-04 起):external_ref = f"wealth:sub:{幂等键}",键由编排层
    # 生成(agent-wealth- 前缀);旧口径 wealth:sub:{product_id}:{ts} 已废除。
    # 两次调用必须携带同一个键(建单时生成、确认时原样复用),否则重放拦不住。
    keys = {c.get("args", {}).get("idempotency_key") for c in subs}
    if len(keys) != 1 or None in keys:
        _fail(f"两次 subscribe_product 必须携带同一个非空幂等键,实际 {keys}")
    only_key = str(keys.pop())
    if not only_key.startswith("agent-wealth-"):
        _fail(f"幂等键应为编排层生成的 agent-wealth- 前缀,实际 {only_key!r}")
    txs = _rows(ctx.db,
                "SELECT * FROM transactions WHERE external_ref=?",
                (f"wealth:sub:{only_key}",))
    if len(txs) != 1 or txs[0]["amount_cents"] != 100_000 or txs[0]["direction"] != "out":
        _fail(f"申购扣款流水应恰一条 out/100000分 且 external_ref 等于幂等键,实际 {txs}")


def _assert_wealth_redeem_cancel(ctx: Ctx) -> None:
    _kind_is(ctx, "wealth_cancelled")
    gate_kinds = [str((p.get("order") or {}).get("kind"))
                  for p in ctx.interrupts("confirm_wealth")]
    if not gate_kinds or gate_kinds[-1] != "redeem":
        _fail(f"赎回应走到 confirm_wealth 闸门(kind=redeem),实际 {gate_kinds}")
    rows = _rows(ctx.db,
                 "SELECT * FROM wealth_holdings WHERE product_id=2 AND status='holding'")
    if len(rows) != 1:
        _fail(f"取消赎回后持仓应仍在(恰 1 行 holding),实际 {len(rows)} 行")
    # 余额保持申购后水平(本剧本第 1 轮已扣 1000):取消赎回零变动
    if _balance(ctx.db, 1) != ctx.base_balance - 100_000:
        _fail(f"取消赎回余额应保持申购后水平({ctx.base_balance - 100_000}),"
              f"实际 {_balance(ctx.db, 1)}(基线 {ctx.base_balance})")
    reds = _calls(ctx, "redeem_product")
    if len(reds) != 1 or reds[0].get("args", {}).get("confirmed") is not False:
        _fail(f"赎回只该建单一次 confirmed=False,实际 {[c.get('args') for c in reds]}")
    if _rows(ctx.db, "SELECT * FROM transactions WHERE external_ref LIKE 'wealth:red:%'"):
        _fail("取消赎回绝不该有到账流水")


def _assert_card_list(ctx: Ctx) -> None:
    _kind_is(ctx, "card_listed")
    if not _calls(ctx, "list_cards"):
        _fail(f"应调用 list_cards,实际 {ctx.bank_call_tools}")
    cards = ctx.notice.get("cards") or []
    if len(cards) != 3:
        _fail(f"种子库 3 张卡应全列出(seed.py:112-116),实际 {len(cards)} 张")
    _assert_balances_frozen(ctx)


def _assert_card_limit(ctx: Ctx) -> None:
    _kind_is(ctx, "card_limits_set")
    views = [p.get("card") or {} for p in ctx.interrupts("confirm_card")]
    lim = [v for v in views if v.get("kind") == "limits"]
    if not lim:
        _fail(f"调限额应出现 confirm_card 闸门(kind=limits),实际 {views}")
    if _cents_or_zero(lim[0].get("new_daily_limit_yuan")) != 800_000:
        _fail(f"闸门复述的新日限额应为 8000 元,实际 {lim[0]!r}")
    rows = _rows(ctx.db, "SELECT * FROM cards WHERE id=1")
    if rows[0]["daily_limit_cents"] != 800_000:
        _fail(f"cards.daily_limit_cents 应为 800000(8000元),"
              f"实际 {rows[0]['daily_limit_cents']}")
    calls = _calls(ctx, "set_card_limits")
    if len(calls) != 1 or calls[0].get("args", {}).get("card_id") != 1 \
            or _cents_or_zero(calls[0].get("args", {}).get("daily_limit_yuan")) != 800_000:
        _fail(f"set_card_limits 应恰一次(card_id=1,日限额8000元),"
              f"实际 {[c.get('args') for c in calls]}")
    if rows[0]["per_tx_limit_cents"] != 200_000:
        _fail(f"只调日限额不该动单笔限额(200000分),实际 {rows[0]['per_tx_limit_cents']}")
    _assert_balances_frozen(ctx)


def _assert_card_lock(ctx: Ctx) -> None:
    _kind_is(ctx, "card_status_set")
    views = [p.get("card") or {} for p in ctx.interrupts("confirm_card")]
    st = [v for v in views if v.get("kind") == "status"]
    if not st or st[0].get("target") != "locked":
        _fail(f"锁卡应出现 confirm_card 闸门(kind=status,target=locked),实际 {views}")
    rows = _rows(ctx.db, "SELECT * FROM cards WHERE id=1")
    if rows[0]["status"] != "locked":
        _fail(f"卡1状态应为 locked,实际 {rows[0]['status']!r}")
    calls = _calls(ctx, "set_card_status")
    if len(calls) != 1 or calls[0].get("args") != {"card_id": 1, "status": "locked"}:
        _fail(f"set_card_status 应恰一次 card_id=1/locked,"
              f"实际 {[c.get('args') for c in calls]}")
    others = {r["id"]: r["status"] for r in _rows(
        ctx.db, "SELECT id,status FROM cards WHERE id IN (2,3)")}
    if others != {2: "locked", 3: "active"}:
        _fail(f"不该误动其他卡(2=locked/3=active 是种子态),实际 {others}")
    _assert_balances_frozen(ctx)


# ------------------------------------------------------------ 订阅/代扣剧本(24-25)断言
# 2026-10-09 扩展:六场景满贯后补上订阅评估覆盖(s_ 管线,commit d6be42b)。
# 种子库由 seed.py 的 detect_subscriptions() 挖掘出 8 条 active 订阅,
# 断言一律与 subscriptions 表逐项对账,不硬编码商户清单与金额。

SUB_CANCEL_MERCHANT = "腾讯视频VIP"   # 种子真实的月度周期扣费(README 涨价戏眼)


def _change_rows(db: Path, category: str) -> list[dict]:
    """管理台修改记录库(与银行库同目录的 change_log.sqlite);不存在按空。"""
    path = db.parent / "change_log.sqlite"
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM change_records WHERE category=?", (category,)).fetchall()]
    finally:
        conn.close()


def _assert_subscription_list(ctx: Ctx) -> None:
    """查订阅清单:只读——list_subscriptions 恰一次、零闸门、零动钱;

    卡片逐项与 subscriptions 表对账,月/年合计按同一公式复算(不贴播报措辞)。
    """
    _kind_is(ctx, "sub_list")
    calls = _calls(ctx, "list_subscriptions")
    if len(calls) != 1:
        _fail(f"list_subscriptions 应恰被调 1 次,实际 {len(calls)} 次:{ctx.bank_call_tools}")
    if ctx.interrupts():
        _fail(f"只读查订阅不该有任何 interrupt,实际 {ctx.interrupts()}")
    rows = _rows(ctx.db, "SELECT * FROM subscriptions WHERE status='active' ORDER BY id")
    if not rows:
        _fail("种子库应有 active 订阅(seed.py 会跑 detect_subscriptions)")
    view = ctx.values.get("sub_view") or {}
    items = view.get("items") or []
    # 顺序不约束(list_subscriptions 按扣费日排),但集合必须一一对应:
    # 用排序后列表比对,既查漏/查多,也查重复项。
    got_names = sorted(str(i.get("merchant_name")) for i in items)
    want_names = sorted(r["merchant_name"] for r in rows)
    if got_names != want_names:
        _fail(f"卡片商户应与 active 订阅一一对应,卡片={got_names},库={want_names}")
    if ctx.notice.get("n") != len(rows):
        _fail(f"notice.n 应为 {len(rows)},实际 {ctx.notice.get('n')!r}")
    by_name = {i["merchant_name"]: i for i in items}
    annual_cents = 0
    for r in rows:
        item = by_name[r["merchant_name"]]
        if _cents_or_zero(item.get("amount_yuan")) != r["amount_cents"]:
            _fail(f"{r['merchant_name']} 卡片金额应为 {r['amount_cents']} 分,"
                  f"实际 {item.get('amount_yuan')!r}")
        want_annual = round(365 / r["period_days"] * r["amount_cents"])
        annual_cents += want_annual
        if _cents_or_zero(item.get("annual_yuan")) != want_annual:
            _fail(f"{r['merchant_name']} 年化应为 {want_annual} 分,"
                  f"实际 {item.get('annual_yuan')!r}")
    if _cents_or_zero(view.get("annual_total_yuan")) != annual_cents:
        _fail(f"年合计应为 {annual_cents} 分,实际 {view.get('annual_total_yuan')!r}")
    if _cents_or_zero(view.get("monthly_total_yuan")) != round(annual_cents / 12):
        _fail(f"月合计应为 {round(annual_cents / 12)} 分,"
              f"实际 {view.get('monthly_total_yuan')!r}")
    if ctx.notice.get("annual_total_yuan") != view.get("annual_total_yuan"):
        _fail("notice 与卡片的年合计必须一致(播报与卡片不许两套数)")
    _assert_balances_frozen(ctx)


def _assert_subscription_cancel(ctx: Ctx) -> None:
    """取消代扣:confirm_sub_cancel 支付密码闸门 → status=cancelled。

    资金安全侧断言:取消代扣不动钱(余额冻结、零新转账单),且不误伤其余订阅。
    """
    _kind_is(ctx, "sub_cancelled")
    gates = ctx.interrupts("confirm_sub_cancel")
    if len(gates) != 1:
        _fail(f"应恰好出现 1 次 confirm_sub_cancel 闸门,实际 {len(gates)} 次:"
              f"{[p.get('type') for p in ctx.interrupts()]}")
    gate = gates[0]
    if not gate.get("pay_required"):
        _fail(f"取消代扣属敏感操作,闸门应要求支付密码,实际 {gate!r}")
    rows = _rows(ctx.db, "SELECT * FROM subscriptions WHERE merchant_name=?",
                 (SUB_CANCEL_MERCHANT,))
    if len(rows) != 1:
        _fail(f"种子库应有恰好 1 条 {SUB_CANCEL_MERCHANT},实际 {len(rows)} 条")
    target = rows[0]
    if target["status"] != "cancelled":
        _fail(f"{SUB_CANCEL_MERCHANT} 终态应为 cancelled,实际 {target['status']!r}")
    sub_view = gate.get("sub") or {}
    if sub_view.get("merchant_name") != SUB_CANCEL_MERCHANT:
        _fail(f"闸门复述商户应为 {SUB_CANCEL_MERCHANT},"
              f"实际 {sub_view.get('merchant_name')!r}")
    # 闸门复述的金额/年化必须来自库内事实,不许拍脑袋
    if _cents_or_zero(sub_view.get("amount_yuan")) != target["amount_cents"]:
        _fail(f"闸门复述金额应为 {target['amount_cents']} 分,"
              f"实际 {sub_view.get('amount_yuan')!r}")
    want_annual = round(365 / target["period_days"] * target["amount_cents"])
    if _cents_or_zero(sub_view.get("annual_yuan")) != want_annual:
        _fail(f"闸门复述年化应为 {want_annual} 分,实际 {sub_view.get('annual_yuan')!r}")
    if sub_view.get("sub_id") != target["id"]:
        _fail(f"闸门载荷 sub_id 应为 {target['id']},实际 {sub_view.get('sub_id')!r}")
    calls = _calls(ctx, "cancel_subscription")
    if len(calls) != 1 or calls[0].get("args", {}).get("subscription_id") != target["id"]:
        _fail(f"cancel_subscription 应恰一次且 subscription_id={target['id']},"
              f"实际 {[c.get('args') for c in calls]}")
    # 不误伤:其余订阅保持 active
    alive = _rows(ctx.db, "SELECT merchant_name FROM subscriptions WHERE status='active'")
    total = _rows(ctx.db, "SELECT COUNT(*) n FROM subscriptions")[0]["n"]
    if len(alive) != total - 1:
        _fail(f"取消 1 条后应剩 {total - 1} 条 active,实际 {len(alive)} 条:"
              f"{[a['merchant_name'] for a in alive]}")
    # 资金安全:取消代扣不动钱。注意种子自带 1 张「林悦 500 元家用」定时单,
    # 故比基线张数,而不是断言"零转账单"。
    orders = _orders(ctx.db)
    if len(orders) != ctx.base_orders:
        _fail(f"取消代扣不该新建转账单:基线 {ctx.base_orders} 张,实际 {len(orders)} 张")
    _assert_balances_frozen(ctx)
    # 管理台修改记录留痕(2026-10-07 补的双库留痕)
    recs = [r for r in _change_rows(ctx.db, "subscription")
            if r.get("action") == "cancel" and r.get("target") == SUB_CANCEL_MERCHANT]
    if len(recs) != 1:
        _fail(f"change_log 应有恰 1 条 subscription/cancel/{SUB_CANCEL_MERCHANT},"
              f"实际 {len(recs)} 条")


# ----------------------------------------------------------------- 剧本清单(顺序即 full 集;quick=前6,smoke=第1+第5)

# 联动剧本共用开场(任务书原文)。踩坑(2026-09-27):注释原写"9月26日=种子生日
# 同日、计划标题确定性《林悦的生日联动》"——那只在当时(2026-09-22,种子生日=
# 播种日+4)成立;日期漂移后 l_plan 可能绑定种子事件(播种日+4),也可能按台词
# 新建事件(年度生日已过会被 bank_core._next_occurrence 顺延到明年),标题不
# 固定。linkage_due 的到期消息虽按《林悦的生日联动》点名,但 l_due 有"唯一
# active 计划"兜底(graph.py l_due),标题对不上也能命中,故不作硬依赖;
# linkage_create 的提醒日断言已改为按计划实际绑定的事件动态推导(见断言注释)。
LINKAGE_OPEN_TEXT = "我爱人生日在9月26日，帮我留1000元，生日前2天订300元鲜花和200元蛋糕"


def _linkage_open_reply(confirm: str) -> Callable[[dict], str]:
    """联动建计划应答:缺槽(ask_slot)整句重说补全;闸门(confirm_linkage)给文案。"""
    def reply(payload: dict) -> str:
        if payload.get("type") == "ask_slot":
            return ("预算1000元；我爱人的生日是2026年9月26日；"
                    "生日前2天订300元的鲜花和200元的蛋糕")
        return confirm
    return reply


def _linkage_due_reply(payload: dict) -> str:
    """到期逐动作闸门应答:只买鲜花,蛋糕按取消跳过。

    动作顺序由抽取决定(鲜花未必 idx=0),按载荷 action.what 内容判,
    不赌顺序;到期闸门一次只问一项(graph l_due_gate 队列驱动)。
    """
    what = str((payload.get("action") or {}).get("what") or "")
    return "888888" if "鲜花" in what else "取消"


# ---- 新管线应答器(16-23):只读场景整句重说补槽;写场景闸门给明确确认/取消 ----

# 申购目标:种子真实低风险产品(seed.py PRODUCTS DP002「安享定期90天」,
# R1/起购1000元/申赎费率0/锁90天)。选它的关键:种子库没有它的持仓,
# 剧本 20 赎回时关键词唯一命中,不会撞种子自带的另两笔持仓。
WEALTH_SUB_PRODUCT = "安享定期90天"


def _clarify_only(fallback: str) -> Callable[[dict], str | None]:
    """只读场景应答器:ask_slot 时整句重说;出现任何闸门=图行为异常,
    返回 None 让驱动按 no_reply 失败(只读场景零闸门是业务事实)。"""
    def reply(payload: dict) -> str | None:
        if payload.get("type") == "ask_slot":
            return fallback
        return None
    return reply


def _wealth_sub_reply(payload: dict) -> str:
    """申购应答:缺产品/金额时补全;闸门(confirm_wealth)给确认。"""
    if payload.get("type") == "ask_slot":
        missing = payload.get("missing") or []
        if "amount_yuan" in missing:
            return "申购1000元"
        return f"申购{WEALTH_SUB_PRODUCT}"
    return "888888"


def _wealth_redeem_reply(payload: dict) -> str:
    """赎回应答:多持仓命中反问时点名产品;闸门给取消。"""
    if payload.get("type") == "ask_slot":
        return f"赎回{WEALTH_SUB_PRODUCT}那笔"
    return "取消"


def _card_limit_reply(payload: dict) -> str:
    if payload.get("type") == "ask_slot":
        missing = payload.get("missing") or []
        if "card" in missing:
            return "第一张卡"
        if "limits" in missing:
            return "每日限额调到8000元"
        return "把我第一张卡的每日限额调到8000元"
    return "888888"


def _card_lock_reply(payload: dict) -> str:
    if payload.get("type") == "ask_slot":
        missing = payload.get("missing") or []
        if "card" in missing:
            return "第一张卡"
        if "status_target" in missing:
            return "锁定"
        return "锁定我的第一张卡"
    return "888888"


def _sub_cancel_reply(payload: dict) -> str:
    """取消代扣应答:缺槽补商户名;confirm_sub_cancel 闸门给支付密码。"""
    ptype = payload.get("type")
    if ptype == "ask_slot":
        missing = payload.get("missing") or []
        if "merchant" in missing:
            return f"取消{SUB_CANCEL_MERCHANT}的自动扣费"
        return "取消代扣"
    if ptype == "confirm_sub_cancel":
        return "888888"          # 支付密码闸门(与挂失/限额同级)
    return "取消"


def _build_scenarios() -> list[Scenario]:
    def aa_create_reply(payload: dict) -> str:
        if payload.get("type") == "ask_slot":  # sb 缺槽兜底:整句重说
            return "火锅300元，我和张三、老王均摊"
        return "确认"  # confirm_split

    def contact_reply(phone: str, note: str) -> Callable[[dict], str]:
        """录入反问一次性补全(姓名/手机/备注都给,缺啥都能补上)。"""
        def reply(payload: dict) -> str:
            if payload.get("type") == "ask_slot":
                return f"王芳，手机{phone}，备注{note}"
            return "确认"  # confirm_contact
        return reply

    def disambiguate_turn3(payload: dict) -> str:
        ptype = payload.get("type")
        if ptype == "pick_contact":
            return "尾号2222"
        if ptype == "ask_slot":
            missing = payload.get("missing") or []
            if "payee" in missing:
                return "给王芳"
            return "5元"
        return "确认"  # confirm_transfer

    return [
        Scenario("transfer_simple", [
            Turn("给张三转10元", _transfer_reply("888888")),
        ], _assert_transfer_simple, "立即转账:闸门确认→扣款10元"),
        Scenario("transfer_cancel", [
            Turn("给张三转10元", _transfer_reply("取消")),
        ], _assert_transfer_cancel, "闸门取消:订单cancelled、余额不动"),
        Scenario("transfer_overlimit", [
            Turn("给张三转60000元", _transfer_reply("取消", amount="60000")),
        ], _assert_transfer_overlimit, "超单笔限额:风控拦截,不进闸门"),
        Scenario("transfer_clarify", [
            Turn("转账", _transfer_reply("888888")),  # 缺收款人与金额,逐步补齐
        ], _assert_transfer_clarify, "缺槽反问→补齐→确认→executed"),
        Scenario("aa_create", [
            Turn("发起AA，火锅300元，我和张三、老王均摊", aa_create_reply),
        ], _assert_aa_create, "AA发起:3人各100元"),
        Scenario("contact_add", [
            Turn("添加收款人", contact_reply("13700001111", "测试")),
        ], _assert_contact_add, "收款人录入:反问后一次补全并保存"),
        Scenario("chat_fallback", [
            Turn("你好，介绍下你自己", None),
        ], _assert_chat_fallback, "闲聊兜底:回复非空且不碰银行"),
        Scenario("gate_hedge", [
            # 两次犹豫答复 → 图内 MAX_GATE_ASKS=2 封顶自动取消(确定性代码路径)
            Turn("给张三转10元", ["等等，我想改金额", "再想想"]),
        ], _assert_gate_hedge, "闸门两问封顶:自动取消,绝不executed"),
        Scenario("transfer_duplicate", [
            Turn("给张三转10元", _transfer_reply("888888")),
            Turn("给张三转10元", None),  # 原样重发:幂等命中,不该再有闸门
        ], _assert_transfer_duplicate, "同指令重发:duplicate_order,不二次扣款"),
        Scenario("transfer_disambiguate", [
            Turn("添加收款人", contact_reply("13700002222", "尾号2222")),
            # 台词 v2(2026-09-27):两处续轮开场都被 router 长历史波动兜底过 chat
            # (v1'对了，还要再录一位收款人…'/v2'再添加一位收款人…'各翻车一次,
            # 翻车轮零工具调用,DB 对账零订单零扣款,资金无险但指标失真)。
            # 改为与剧本 6 完全同款的强触发开头"添加收款人"(该开场历次全过),
            # 且一次报全姓名+手机+备注——防沿用上一轮旧手机号直进闸门(踩坑:
            # contact_draft 跨轮保留,只说"再添加一位"不触发反问);第 3 轮用
            # "转账"逐字命中路由规则。残余波动如实记:router 偶发不输出 JSON
            # 是已知问题,台词只能压概率,压不住就按失败如实呈现。
            Turn("添加收款人：王芳，手机号13700003333，备注尾号3333",
                 contact_reply("13700003333", "尾号3333")),
            Turn("给王芳转账5元", disambiguate_turn3),        # 选人→确认→executed
        ], _assert_transfer_disambiguate, "同名消歧:尾号2222"),
        Scenario("scheduled_transfer", [
            Turn("明天早上9点给张三转50元", _transfer_reply("888888", amount="50")),
        ], _assert_scheduled_transfer, "定时转账:scheduled、到期才扣"),
        Scenario("settle_flow", [
            Turn("发起AA，火锅300元，我和张三、老王均摊", aa_create_reply),
            # 台词 v2(2026-09-27):v1'张三把火锅的钱付了'在收官全量里被 router
            # 兜底成 chat(已知 glm-5.3-flash 长历史偶发不输出 JSON),结算没触发、
            # paid 全 0。按路由规则"某人已把 AA 的钱付了"补齐强触发词(AA+已经
            # 付了),语义不变,仍只点名张三一个人。
            Turn("AA火锅那单，张三已经把钱付给我了", lambda p: "确认"),  # confirm_settle
        ], _assert_settle_flow, "AA结算:张三项 paid=1"),
        # ---- 跨场景联动(13/14/15):生日剧本全链路,承接 MCP 联动工具与 l_* 管线 ----
        Scenario("linkage_create", [
            Turn(LINKAGE_OPEN_TEXT, _linkage_open_reply("888888")),
        ], _assert_linkage_create,
            "联动建计划:闸门确认→锁定单executed、活期-1000、2条提醒"),
        Scenario("linkage_due", [
            # 承接剧本 13 的 plan(同 thread):第 1 轮先按同句开场建计划并确认锁定,
            # 第 2 轮发系统到期提醒,逐动作闸门只确认鲜花、蛋糕按取消跳过。
            Turn(LINKAGE_OPEN_TEXT, _linkage_open_reply("888888")),
            Turn("(到期提醒) 计划《林悦的生日联动》的【鲜花】今天到期，请帮我处理",
                 _linkage_due_reply),
        ], _assert_linkage_due,
            "联动到期:鲜花确认购买(300元流水),蛋糕跳过不动"),
        Scenario("linkage_cancel", [
            Turn(LINKAGE_OPEN_TEXT, _linkage_open_reply("取消")),
        ], _assert_linkage_cancel,
            "联动取消:计划cancelled、锁定单未执行、余额零变化"),
        # ---- 账单/理财/卡片(16-23):六场景收官,复用 b_/w_/k_ 管线 ----
        # 申购产品名取自种子真实产品库(见 WEALTH_SUB_PRODUCT 注释)
        Scenario("bill_monthly", [
            Turn("看看我上个月的账单报告", _clarify_only("看上个月的月度账单报告")),
        ], _assert_bill_monthly, "账单月报:monthly_report(恰为上月),只读零闸门零动钱"),
        Scenario("bill_anomaly", [
            Turn("查一下我最近三个月有没有异常交易",
                 _clarify_only("查最近三个月的异常交易")),
        ], _assert_bill_anomaly, "异常检测:detect_anomalies(≈90天),零动钱"),
        Scenario("wealth_recommend", [
            Turn("帮我推荐一些稳健的理财产品", _clarify_only("推荐稳健的理财产品")),
        ], _assert_wealth_recommend, "理财推荐:按C3风险等级过滤,回复非空,零动钱"),
        Scenario("wealth_subscribe", [
            Turn(f"我要申购1000元的{WEALTH_SUB_PRODUCT}", _wealth_sub_reply),
        ], _assert_wealth_subscribe,
            "理财申购:闸门确认→持仓新增、活期恰减1000、两步调用False→True"),
        Scenario("wealth_redeem_cancel", [
            # 承接 19(同 thread):先申购确认,再发赎回指令、闸门给「取消」
            Turn(f"我要申购1000元的{WEALTH_SUB_PRODUCT}", _wealth_sub_reply),
            Turn("把刚才买的理财赎回了", _wealth_redeem_reply),
        ], _assert_wealth_redeem_cancel,
            "赎回取消:闸门取消→持仓仍在、余额保持申购后水平"),
        Scenario("card_list", [
            Turn("看看我名下的银行卡", _clarify_only("看看我名下的银行卡清单")),
        ], _assert_card_list, "卡片查询:list_cards,零动钱"),
        Scenario("card_limit", [
            Turn("把我第一张卡的每日限额调到8000元", _card_limit_reply),
        ], _assert_card_limit, "调限额:闸门确认→daily_limit_cents=800000"),
        Scenario("card_lock", [
            Turn("暂时锁定我的第一张卡", _card_lock_reply),
        ], _assert_card_lock, "锁卡:闸门确认(单闸,非挂失)→status=locked"),
        # ---- 订阅/代扣(24-25):六场景满贯后补评估覆盖(2026-10-09) ----
        Scenario("subscription_list", [
            Turn("帮我看看我都有哪些自动扣费，一年要花多少钱",
                 _clarify_only("查一下我的订阅清单和年化成本")),
        ], _assert_subscription_list,
            "订阅清单:list_subscriptions,卡片与库逐项对账,只读零闸门零动钱"),
        Scenario("subscription_cancel", [
            Turn(f"取消{SUB_CANCEL_MERCHANT}的自动扣费", _sub_cancel_reply),
        ], _assert_subscription_cancel,
            "取消代扣:支付密码闸门→status=cancelled,不误伤其余订阅、零动钱"),
    ]


# ----------------------------------------------------------------- 驱动与执行


def _seed_db(root: Path) -> Path:
    """播种剧本专属临时库:bank_core.seed 确定性播种 + 补插张三。

    张三必须补:种子 CONTACTS(seed.py)只有林悦/陈朵朵/张阿姨/老王/小刘/王秀兰,
    而本评估多数剧本向张三转账/收款。
    """
    from bank_core.seed import seed

    db = root / "bank.db"
    seed(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO contacts (user_id,name,phone,relation,note) "
            "VALUES (1,'张三','13633334444','colleague','同事·评估')")
        # 登录态:user1 预置 auth 行(支付密码 888888)——动钱/敏感闸
        # 走 verify_pay_password 真实核验,评估以登录用户身份驱动。
        from bank_core.auth_core import hash_password
        conn.execute(
            "INSERT INTO auth_users (user_id, identifier, identifier_type, "
            "password_hash, pay_hash, created_at) VALUES "
            "(1,'13800000000','phone',?,?,'2026-01-01T00:00:00')",
            (hash_password("Demo@12345"), hash_password("888888")))
        conn.commit()
    finally:
        conn.close()
    return db


def _paused_payload(snap: Any) -> dict | None:
    """图停在 interrupt 时取载荷;否则 None(照 api.py:329 的判定)。"""
    if not bool(getattr(snap, "next", None)):
        return None
    for t in snap.tasks:
        if t.interrupts:
            v = t.interrupts[0].value
            return v if isinstance(v, dict) else {"type": "raw", "value": v}
    return None


async def _run_turn(graph: Any, cfg: dict, turn: Turn) -> dict:
    """跑一轮:发开场消息,按剧本应答 interrupt 直到图到 END。

    Returns:
        {"text", "interrupts": [载荷…], "msgs": 用户消息数, "status":
        end|paused|no_reply|max_rounds}
    """
    interrupts: list[dict] = []
    await graph.ainvoke({"auth_user_id": 1, "messages": [HumanMessage(content=turn.text)]}, cfg)
    msgs = 1
    queue = list(turn.reply) if isinstance(turn.reply, (list, tuple)) else None
    while msgs < MAX_MSGS_PER_TURN:
        snap = await graph.aget_state(cfg)
        payload = _paused_payload(snap)
        if payload is None:
            return {"text": turn.text, "interrupts": interrupts, "msgs": msgs,
                    "status": "end"}
        interrupts.append(payload)
        if queue is not None:
            if not queue:
                return {"text": turn.text, "interrupts": interrupts, "msgs": msgs,
                        "status": "no_reply"}  # 剧本没料到这个追问
            answer: str | None = queue.pop(0)
        else:
            answer = turn.reply(payload) if turn.reply else None
        if not answer:
            return {"text": turn.text, "interrupts": interrupts, "msgs": msgs,
                    "status": "no_reply"}
        await graph.ainvoke(Command(resume=answer), cfg)
        msgs += 1
    return {"text": turn.text, "interrupts": interrupts, "msgs": msgs,
            "status": "max_rounds"}


async def _run_scenario(scn: Scenario, llm: Any, tmp_root: Path) -> dict:
    """单剧本:播种→建图→驱动→断言;任何异常都转成失败记录,绝不中断整场。"""
    t0 = time.perf_counter()
    root = tmp_root / scn.name
    root.mkdir(parents=True, exist_ok=True)
    record: dict = {"name": scn.name, "desc": scn.desc, "ok": False, "rounds": 0,
                    "bank_calls": 0, "seconds": 0.0, "reason": "", "db": str(root / "bank.db"),
                    "turns": []}
    conn = None
    try:
        db = _seed_db(root)
        os.environ["BANK_CORE_DB"] = str(db)  # 隔离声明;工具装载实际走显式路径
        base_balance = _balance(db)
        base_balance2 = _balance(db, 2)
        tools = await load_bank_tools(db)
        saver, conn = await make_sqlite_checkpointer(root / "ckpt.sqlite")
        graph = build_agent_graph(llm, tools, saver)
        cfg = {"configurable": {"thread_id": f"eval-{scn.name}"},
               "recursion_limit": RECURSION_LIMIT}
        async with asyncio.timeout(SCENARIO_TIMEOUT_S):
            turns: list[dict] = []
            for turn in scn.turns:
                log = await _run_turn(graph, cfg, turn)
                turns.append(log)
                record["rounds"] += log["msgs"]
                if log["status"] != "end":
                    _fail(f"第 {len(turns)} 轮({log['text']!r})未正常收束:"
                          f"status={log['status']},interrupts={log['interrupts']}")
            snap = await graph.aget_state(cfg)
            values = dict(snap.values or {})
            record["bank_calls"] = len(values.get("bank_calls") or [])
            record["turns"] = [
                {"text": t["text"], "status": t["status"],
                 "interrupts": [str(p.get("type")) for p in t["interrupts"]]}
                for t in turns]
            scn.assert_fn(Ctx(db=db, base_balance=base_balance,
                              base_balance2=base_balance2,
                              base_orders=len(_orders(db)), values=values,
                              turns=turns))
        record["ok"] = True
    except TimeoutError:
        record["reason"] = f"剧本超时(>{SCENARIO_TIMEOUT_S:.0f}s),按失败记"
    except AssertionError as exc:
        record["reason"] = str(exc) or repr(exc)
    except Exception as exc:  # noqa: BLE001 —— 网络/模型/图崩溃一律按失败记录
        record["reason"] = f"{type(exc).__name__}: {exc}"
    finally:
        if conn is not None:  # 超时取消后 close 可能卡,5 秒兜底
            try:
                await asyncio.wait_for(conn.close(), timeout=5)
            except Exception:  # noqa: BLE001
                pass
    record["seconds"] = round(time.perf_counter() - t0, 1)
    return record


# ----------------------------------------------------------------- 报告

# 场景分组(报告按组呈现;漏配的剧本落"其他",报告里可见不会被吞)
GROUPS: dict[str, str] = {
    "transfer_simple": "转账", "transfer_cancel": "转账",
    "transfer_overlimit": "转账", "transfer_clarify": "转账",
    "gate_hedge": "转账", "transfer_duplicate": "转账",
    "transfer_disambiguate": "转账", "scheduled_transfer": "转账",
    "aa_create": "AA收款", "settle_flow": "AA收款",
    "contact_add": "联系人",
    "chat_fallback": "闲聊",
    "linkage_create": "联动", "linkage_due": "联动", "linkage_cancel": "联动",
    "bill_monthly": "账单", "bill_anomaly": "账单",
    "wealth_recommend": "理财", "wealth_subscribe": "理财",
    "wealth_redeem_cancel": "理财",
    "card_list": "卡片", "card_limit": "卡片", "card_lock": "卡片",
    "subscription_list": "订阅", "subscription_cancel": "订阅",
}
GROUP_ORDER = ["转账", "AA收款", "联系人", "闲聊", "联动", "账单", "理财", "卡片", "订阅"]


def _failure_tag(rec: dict) -> str:
    """失败归因(粗分类,附原始 reason 一起呈现,不替模型说话)。

    口径:_fail() 抛的业务断言消息都含「应…」;驱动收束检查以「未正常收束」
    开头;异常类型(TimeoutError 等)走 Python 异常名前缀,勿按子串猜——
    实测 disambiguate 的「应触发选人反问」曾被误标成运行异常,这里修正。
    """
    r = str(rec.get("reason") or "")
    if "超时" in r:
        return "超时"
    if "未正常收束" in r:
        return "驱动未收束(闸门/反问超出预案)"
    if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*(Error|Exception)\b", r) \
            or "timeout" in r.lower() or "connection" in r.lower():
        return "模型/网络异常"
    if "应" in r:
        return "业务断言不符(DB/notice 终态)"
    return "运行异常(图/工具崩溃)"


def _group_metrics(records: list[dict]) -> dict:
    n = len(records)
    return {
        "total": n,
        "pass": sum(1 for r in records if r["ok"]),
        "success_rate": round(sum(1 for r in records if r["ok"]) / n, 4) if n else 0.0,
        "avg_rounds": round(sum(r["rounds"] for r in records) / n, 2) if n else 0.0,
        "avg_bank_calls": round(sum(r["bank_calls"] for r in records) / n, 2) if n else 0.0,
        "avg_seconds": round(sum(r["seconds"] for r in records) / n, 1) if n else 0.0,
    }


def _write_reports(records: list[dict], model_desc: str, set_name: str,
                   tmp_root: Path) -> dict:
    for r in records:
        r["group"] = GROUPS.get(r["name"], "其他")
        r["failure_tag"] = "" if r["ok"] else _failure_tag(r)
    overall = _group_metrics(records)
    grouped: list[dict] = []
    names = set(r["group"] for r in records)
    for g in GROUP_ORDER + sorted(names - set(GROUP_ORDER)):
        rs = [r for r in records if r["group"] == g]
        if rs:
            grouped.append({"group": g, **_group_metrics(rs),
                            "scenarios": [r["name"] for r in rs]})
    failures = [r for r in records if not r["ok"]]
    fail_tags: dict[str, int] = {}
    for r in failures:
        fail_tags[r["failure_tag"]] = fail_tags.get(r["failure_tag"], 0) + 1

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "model": model_desc,
        "set": set_name,
        "threshold": SUCCESS_THRESHOLD,
        "pass": overall["pass"], "total": overall["total"],
        "success_rate": overall["success_rate"],
        "metrics": {k: overall[k] for k in
                    ("avg_rounds", "avg_bank_calls", "avg_seconds")},
        "total_seconds": round(sum(r["seconds"] for r in records), 1),
        "groups": grouped,
        "failure_tags": fail_tags,
        "scenarios": records,
    }
    REPORT_JSON.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Agent 真实 LLM 评估报告",
        "",
        f"- 时间:{summary['generated_at']}  模型:`{model_desc}`  剧本集:`{set_name}`",
        f"- 阈值:success_rate >= {SUCCESS_THRESHOLD:.2f}",
        f"- 临时库根目录(失败可对账):`{tmp_root}`",
        "",
        "## 汇总",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 通过 / 总数 | {overall['pass']} / {overall['total']} |",
        f"| 成功率 | {overall['success_rate']:.2%} |",
        f"| 平均轮次(用户消息/剧本) | {overall['avg_rounds']} |",
        f"| 平均工具调用 | {overall['avg_bank_calls']} |",
        f"| 平均耗时 | {overall['avg_seconds']}s |",
        f"| 总耗时 | {summary['total_seconds']}s |",
        "",
        "## 按场景分组",
        "",
    ]
    for g in grouped:
        lines.append(f"### {g['group']}({g['total']} 个:"
                     f"通过 {g['pass']},成功率 {g['success_rate']:.0%},"
                     f"平均轮次 {g['avg_rounds']},平均工具 {g['avg_bank_calls']},"
                     f"平均 {g['avg_seconds']}s)")
        lines += ["", "| 剧本 | 结果 | 轮次 | 工具调用 | 秒 | 失败原因 |",
                  "|---|---|---|---|---|---|"]
        for r in records:
            if r["group"] != g["group"]:
                continue
            lines.append(
                f"| {r['name']} | {'PASS' if r['ok'] else 'FAIL'} | {r['rounds']} "
                f"| {r['bank_calls']} | {r['seconds']} | {r['reason'] or ''} |")
        lines.append("")

    lines += ["## 失败归因", ""]
    if not failures:
        lines.append("全部通过,无失败需要归因。")
    else:
        lines.append("| 归因 | 次数 |")
        lines.append("|---|---|")
        for tag, n in sorted(fail_tags.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {tag} | {n} |")
        lines += ["", "逐条明细:", ""]
        for r in failures:
            lines.append(f"- **{r['name']}**[{r['failure_tag']}]:{r['reason']}")
    lines += ["", f"**EVAL RESULT: pass={overall['pass']}/{overall['total']} "
              f"success_rate={overall['success_rate']:.2f}**", ""]
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")
    return summary


# ----------------------------------------------------------------- 主流程


async def _amain(set_name: str, only: str = "") -> int:
    scenarios = _build_scenarios()
    if only:
        names = [n.strip() for n in only.split(",") if n.strip()]
        known = {s.name for s in scenarios}
        unknown = [n for n in names if n not in known]
        if unknown:
            print(f"未知剧本名: {unknown}(可选: {sorted(known)})")
            return 2
        chosen = [s for s in scenarios if s.name in names]
        set_name = f"only({len(chosen)}):{','.join(names)}"
    elif set_name == "smoke":
        chosen = [scenarios[0], scenarios[4]]          # 第1 + 第5
    elif set_name == "quick":
        chosen = scenarios[:6]                          # 前 6 个
    else:
        chosen = scenarios                              # 全部

    # 模型选择照抄 agent/api.py:202 生产路径:显式 ZAI_MODEL 优先,否则自动选型
    model_name = os.environ.get("ZAI_MODEL") or await pick_default_model()
    llm = get_llm(model=model_name, timeout=60, max_retries=1)  # 温度默认 0.1

    tmp_root = Path(tempfile.mkdtemp(prefix="eval-agent-"))
    print(f"== 真实 LLM 评估 set={set_name} 模型={model_name} "
          f"剧本数={len(chosen)} 超时={SCENARIO_TIMEOUT_S:.0f}s ==")
    print(f"临时库根目录:{tmp_root}")

    records: list[dict] = []
    for i, scn in enumerate(chosen, 1):
        print(f"[{i}/{len(chosen)}] {scn.name} …", flush=True)
        rec = await _run_scenario(scn, llm, tmp_root)
        records.append(rec)
        verdict = "PASS" if rec["ok"] else f"FAIL({rec['reason']})"
        print(f"    -> {verdict}  轮次={rec['rounds']} 工具={rec['bank_calls']} "
              f"秒={rec['seconds']}", flush=True)

    summary = _write_reports(records, model_name, set_name, tmp_root)
    print(f"报告:{REPORT_MD}")
    print(f"数据:{REPORT_JSON}")
    print(f"EVAL RESULT: pass={summary['pass']}/{summary['total']} "
          f"success_rate={summary['success_rate']:.2f}")
    return 0 if summary["success_rate"] >= SUCCESS_THRESHOLD else 1


def main() -> int:
    _utf8_stdout()
    # 踩坑(2026-09-27 收官全量被编排层 world.run 判异常):外层检查器给 stderr
    # 设了 262144 字节上限,而每个银行工具调用各自新起一个 MCP stdio 会话
    # (agent/bank.py 注 3),FastMCP 子进程每次启动都向 stderr 打 banner +
    # 更新提示 + INFO 行——实测 23 剧本约 102 个会话 ≈268KB,直接超帽。
    # 子进程 env 经 bank.py 的 {**os.environ} 继承,这里从源头静音
    # (fastmcp/settings.py:FASTMCP_ 前缀,show_server_banner/log_level);
    # setdefault 不覆盖外部显式配置。信噪不受影响:失败信息全在 stdout 进度
    # 与报告文件里,banner 从来没有诊断价值。
    os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")
    os.environ.setdefault("FASTMCP_LOG_LEVEL", "CRITICAL")
    parser = argparse.ArgumentParser(
        description="真实 LLM 端到端评估:标准化剧本跑编排图,量化 agent 质量")
    parser.add_argument("--set", choices=("smoke", "quick", "full"), default="full",
                        help="smoke=2 个(开发自验) quick=前6(闸门) full=全部25")
    parser.add_argument("--only", default="",
                        help="逗号分隔剧本名,定向跑(自验用;与 --set 二选一,前者优先)")
    args = parser.parse_args()
    return asyncio.run(_amain(args.set, args.only))


if __name__ == "__main__":
    sys.exit(main())
