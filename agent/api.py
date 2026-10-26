"""FastAPI 流式对话端点:把 LangGraph 编排图接到前端(M1 联调地基)。

启动:
    python -m agent.api                      # 127.0.0.1:8800
    AGENT_API_PORT=9000 python -m agent.api  # 自定义端口
    uvicorn agent.api:app --port 8800        # 等价

环境变量:
    BANK_CORE_DB          银行库路径(默认 data/bank.db,与 bank_core 一致)
    AGENT_CHECKPOINT_DB   会话检查点库(默认 data/agent_ckpt.sqlite)
    ZAI_API_KEY / ZAI_MODEL / ZAI_BASE_URL  真实模型(见 agent/llm.py)

────────────────────────────────────────────────────────────────────────────
SSE 协议依据(字段名逐条核对过源码/官方文档,勿臆造、勿凭记忆改):

线格式 = SSE,每帧 `data: {json}\\n\\n`。part 词汇表取自 Vercel AI SDK 5 的
UIMessageChunk(源码 packages/ai/src/ui-message-stream/ui-message-chunks.ts,
2026-09 核对;官方自定义后端文档 ai-sdk.dev/docs/ai-sdk-ui/stream-protocol):

    {"type": "start"}                                    流开始(messageId 可选)
    {"type": "text-start", "id": "..."}                  文本块开始
    {"type": "text-delta", "id": "...", "delta": "..."}  文本增量(注意是 delta 不是 textDelta)
    {"type": "text-end", "id": "..."}                    文本块结束
    {"type": "reasoning-start", "id": "..."}             模型原生推理开始(思维链,可折叠面板)
    {"type": "reasoning-delta", "id": "...", "delta":…"}  推理增量(字段名同为 delta,非 textDelta)
    {"type": "reasoning-end", "id": "..."}               推理结束
    {"type": "data-<name>", "data": {...}}               自定义数据部件(type 必须 data- 前缀;
                                                          id/transient 可选)
    {"type": "error", "errorText": "..."}                错误(字段名就是 errorText)
    {"type": "finish", "finishReason": "stop"}           结束(finishReason 枚举:
                                                          stop|length|content-filter|
                                                          tool-calls|error|other)

响应头 x-vercel-ai-ui-message-stream: v1 —— 官方要求自定义后端必须设置此头
(createUIMessageStreamResponse 的默认行为,值是字符串 "v1"),useChat 靠它识别流类型。

`data-*` 是协议明文扩展点;部件内字段是本服务自有契约(前端按此渲染 generative UI):
    data-transfer-confirmation  转账确认卡片(见 _transfer_card)
    data-split-confirmation     AA 收款确认卡片
    data-settle-confirmation    AA 成员结算确认卡片(评审修复后新增的闸门)
    data-linkage-plan           跨场景联动计划确认卡片(建计划后的预算锁定闸门)
    data-linkage-action         联动到期动作确认卡片(逐项购买闸门)
    data-plan                   生日预算计划确认卡片(普通确认不扣款;取消订阅/锁定预算另过密码闸门)
    data-analysis-plan          Agent Core 只读分析计划卡片(目标/事实行/步骤/复盘/上次记忆,无按钮)
    data-thinking               Agent Core 编排思维链(识别意图→调取记忆→制定计划→安全守卫→
                                执行→复盘,可折叠面板)
                                注意:reasoning-* 与 data-thinking 是两层不同的"思考",二者并存:
                                  reasoning-*     = 模型原生推理文本(reasoning_content),只有
                                                    DeepSeek-R1 / GLM thinking 等推理模型才有;
                                                    非推理模型不产生任何 reasoning 帧(优雅降级)
                                  data-thinking   = 本服务 Agent Core 的编排进度(确定性生成,
                                                    与模型是否推理无关)
    data-wealth-confirmation    理财确认卡片(kind=subscribe|redeem;order=产品/金额/费率/锁定期/风险/账户)
    data-wealth-entry           理财中心入口卡(level/在售产品含 purchasable 标注/目的选项)
    data-bill-report            账单报告卡(年度/月度:KPI 总览 + 逐月走势 + 分类占比 + 商户 Top)
    data-wealth-products        可购产品列表卡(w_query 查询结果;含逐行申购入口,回话走既有闸门链路)
    data-card-confirmation      卡片确认卡片(kind=apply|limits|status;card=card_id/尾号/类型/当前值/目标值)
    data-contact-choices        同名联系人选项(卡片式消歧,而非开放反问)
    data-ask-slot               缺槽反问(附缺失字段清单)
    data-gate-pending           本轮结束时图停在人工闸门(前端可切换输入态为"确认/取消")

常用收款人 REST(非 SSE,「我要转账」选择器用;均需 x-bank-token 头):
    GET  /api/contacts          当前登录用户收款人列表;未登录 401
    POST /api/contacts          新建收款人 {name, phone, note};手机号 1 开头 11 位且唯一

手机扫码登录(电脑端已登录 → 手机免密登录同一账号):
    POST /api/auth/qr-ticket    电脑端签发一次性票据(120s,需 x-bank-token)
    POST /api/auth/qr-login     手机端凭 {ticket} 换正式会话 token(一次性)
    GET  /api/lan-info          本机局域网 IP(拼手机可访问的 URL)
    GET  /api/qr?data=...       本地二维码 PNG 渲染(不出网)
────────────────────────────────────────────────────────────────────────────

会话驱动约定(与 agent/graph.py 的图契约一致):
- 请求只取【最后一条 user 消息】作为新输入;完整历史由 checkpointer 按 thread_id 保持;
- 同一 thread 若停在 interrupt(闸门/反问),下一条消息自动作为 Command(resume=文本)
  恢复执行——前端只需照常把用户回复发上来,不必区分"新指令"还是"闸门答复"。
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from agent import auth as auth_mod
from agent import memory as memory_mod
from agent import persona as persona_mod
from agent import tasks as tasks_mod
from agent.bank import BankTools, load_bank_tools
from agent.graph import (_looks_like_new_transfer, build_agent_graph,
                         make_sqlite_checkpointer)
from bank_core.db import migrate_cards_bound
from bank_core.ledger import LedgerError, LedgerService
from bank_core.money import cents_to_yuan, yuan_to_cents
from agent.llm import (RequestLLM, fetch_models_json, get_llm,
                       normalize_base_url, pick_default_model,
                       reset_request_llm, set_request_llm)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

STREAM_HEADER = "x-vercel-ai-ui-message-stream"
STREAM_HEADER_VALUE = "v1"  # 依据:ai-sdk.dev stream-protocol(自定义后端必须设 v1)

# 播报型节点:这些节点的 AIMessage 是给用户看的最终文本。
# 不播报其他节点的消息(如 t_gate 复放的问题文本),避免与闸门卡片重复。
ANNOUNCE_NODES = frozenset({"t_report", "sb_report", "ss_report", "c_report",
                            "l_report", "b_report", "w_report", "k_report",
                            "s_report", "p_report", "chat",
                            # Agent Core 的收尾播报(分析卡由 a_exec/a_reflect 单独下发)
                            "a_report"})

TEXT_CHUNK_SIZE = 96  # 文本切片粒度(字节近似;假模型无 token 流,切片仅为产生增量帧)

# 评审修复:生产路径必须有超时熔断(计划 §8),否则上游模型挂起时单轮可拖满
# ChatOpenAI 默认 600s × 重试,且前端只收到一条 start 帧后无限等待。
LLM_TIMEOUT_S = float(os.environ.get("AGENT_LLM_TIMEOUT", "60"))    # 单次模型调用
LLM_MAX_RETRIES = int(os.environ.get("AGENT_LLM_MAX_RETRIES", "1"))
TURN_DEADLINE_S = float(os.environ.get("AGENT_TURN_DEADLINE", "180"))  # 单轮 SSE 整体
RECURSION_LIMIT = 50  # 显式递归上限(反问循环另有状态级封顶,双保险)


# ----------------------------------------------------------------- 请求/响应模型

class ChatMessage(BaseModel):
    role: str = "user"
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    thread_id: str = Field(min_length=1, max_length=128)
    # BYOK(可选):界面「大模型管理」里已连接供应商的 Key/端点/协议随请求生效;
    # 不填走部署方环境变量
    api_key: str | None = Field(default=None, max_length=200)
    base_url: str | None = Field(default=None, max_length=300)
    provider: str | None = Field(default=None, max_length=32)
    # 模型切换(可选):前端选择器选中的模型;不填走自动选型
    model: str | None = Field(default=None, max_length=64)
    # 深度思考(可选):是否启用模型原生推理(reasoning_content)。
    # None=后端按模型名自动判定(如 deepseek-r1 / glm-thinking);True/False 强制开/关。
    # 前端无需改动即可生效——选推理模型时后端自动开启。
    enable_thinking: bool | None = None
    # 登录会话(可选):注册/登录下发的 token;不带=观光模式(闲聊可用,
    # 业务意图被 auth_guard 引导登录)
    token: str | None = Field(default=None, max_length=128)
    # 设备/标签页标识:跨设备同步时区分"自己写的"与"对方写的"
    client_id: str | None = Field(default=None, max_length=64)


class SyncActiveIn(BaseModel):
    """当前设备打开的会话(手机/电脑互相跟随)。"""
    thread_id: str = Field(min_length=1, max_length=128)


class VoteIn(BaseModel):
    """消息投票:换票覆盖,同票再点取消(模板语义)。"""
    chatId: str = Field(min_length=1, max_length=128)
    messageId: str = Field(min_length=1, max_length=128)
    type: str = Field(pattern="^(up|down)$")


class ValidateKeyRequest(BaseModel):
    api_key: str = Field(min_length=10, max_length=200)
    base_url: str | None = Field(default=None, max_length=300)
    provider: str | None = Field(default=None, max_length=32)


class RegisterIn(BaseModel):
    real_name: str = Field(min_length=2, max_length=20)   # 真实姓名
    id_card: str = Field(min_length=18, max_length=18)    # 身份证号(18 位含校验位)
    phone: str = Field(min_length=11, max_length=11)      # 手机号(必填,即登录账号)
    email: str = ""                                       # 邮箱(选填)
    login_password: str = Field(min_length=8, max_length=64)
    pay_password: str = Field(min_length=6, max_length=6)


class LoginIn(BaseModel):
    identifier: str = Field(min_length=5, max_length=120)
    password: str = Field(min_length=8, max_length=64)


class TokenIn(BaseModel):
    token: str = Field(min_length=16, max_length=128)


class DepositIn(BaseModel):
    """模拟入金入参：account_id 为空时用本人第一个账户；card_id 为空走模拟外部账户。"""

    account_id: int | None = None
    amount_yuan: str = Field(min_length=1, max_length=20)
    note: str = Field(default="", max_length=200)
    idempotency_key: str = Field(default="", max_length=80)
    card_id: int | None = None


class BoundCardIn(BaseModel):
    bank_name: str = Field(min_length=2, max_length=30)
    card_no: str = Field(min_length=16, max_length=23)
    holder_name: str = Field(min_length=2, max_length=30)


class PersonaIn(BaseModel):
    """人设提示词入参:空文本=清除(回到默认);温度只收 0-1。"""

    persona: str = Field(default="", max_length=300)
    temperature: float = Field(default=0.1, ge=0.0, le=1.0)


class ContactIn(BaseModel):
    """新建收款人(联系人)入参;格式与重复手机号由 LedgerService 统一校验。"""
    name: str = ""
    phone: str = ""
    note: str = ""


class QrLoginIn(BaseModel):
    """手机扫码登录入参:电脑端签发的一次性票据。"""
    ticket: str = Field(min_length=16, max_length=128)


class ThreadCreateIn(BaseModel):
    """新建会话(手机端「+」):先命名,再进入空会话。"""
    title: str = ""


class OrderConfirmIn(BaseModel):
    token: str = Field(min_length=16, max_length=128)
    order_id: int
    pay_password: str = Field(min_length=6, max_length=6)


# ----------------------------------------------------------------- SSE 帧

def _frame(part: dict) -> str:
    return f"data: {json.dumps(part, ensure_ascii=False)}\n\n"


def _text_frames(text: str, chunk_size: int = TEXT_CHUNK_SIZE) -> list[str]:
    """一段文本 → text-start / text-delta×N / text-end 帧序列(id 全程一致)。"""
    part_id = uuid4().hex
    frames = [_frame({"type": "text-start", "id": part_id})]
    if text:
        pieces = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
        for piece in pieces:
            frames.append(_frame({"type": "text-delta", "id": part_id, "delta": piece}))
    frames.append(_frame({"type": "text-end", "id": part_id}))
    return frames


def _chunk_reasoning(chunk: Any) -> str:
    """从流式 chunk 抽取模型原生推理文本(reasoning_content),多 provider 兼容。

    兼容:OpenAI 兼容(DeepSeek / 智谱 GLM thinking)的
    additional_kwargs["reasoning_content"](字符串)或 reasoning_details(list);
    Anthropic 风格 langchain AIMessageChunk.reasoning。
    """
    ak = getattr(chunk, "additional_kwargs", None) or {}
    if isinstance(ak, dict):
        rc = ak.get("reasoning_content")
        if isinstance(rc, str) and rc:
            return rc
        rd = ak.get("reasoning_details")
        if isinstance(rd, list):
            txt = "".join(d.get("text", "") for d in rd if isinstance(d, dict))
            if txt:
                return txt
    r = getattr(chunk, "reasoning", None)
    if isinstance(r, str) and r:
        return r
    if isinstance(r, list):
        return "".join(d.get("text", "") for d in r if isinstance(d, dict))
    return ""


def _gate_new_transfer(snapshot: Any, text: str | None) -> bool:
    """thread 停在**转账支付密码闸门**且新消息像另一笔转账指令(多单并行挂起)。

    只认 confirm_transfer 且 pay_required 的中断:其余闸门(理财/卡片/联动)
    的确认流程不受影响。纯函数,可单测。
    """
    if not text:
        return False
    for task in (getattr(snapshot, "tasks", None) or []):
        for intr in (getattr(task, "interrupts", None) or []):
            payload = getattr(intr, "value", None)
            if (isinstance(payload, dict)
                    and payload.get("type") == "confirm_transfer"
                    and payload.get("pay_required")
                    and _looks_like_new_transfer(text)):
                return True
    return False


def _snippet(text: str, needle: str, radius: int = 18) -> str:
    """命中词上下文片段(会话内容搜索的可解释结果),压成单行。"""
    low = text.lower()
    pos = low.find(needle.lower())
    if pos < 0:
        return ""
    start = max(0, pos - radius)
    end = min(len(text), pos + len(needle) + radius)
    body = text[start:end].replace("\n", " ").strip()
    return ("…" if start > 0 else "") + body + ("…" if end < len(text) else "")


# ----------------------------------------------------------------- 卡片(自定义 data 部件)

async def _transfer_card(order_view: dict, tools: BankTools,
                         policy_cache: dict, pay_required: bool = False) -> dict:
    """转账确认卡片字段(本服务自有契约;金额/收款人来自建单回执,限额来自 get_policy)。"""
    policy = policy_cache.get("value")
    if policy is None:
        policy = await tools.call("get_policy")  # [READ] 只读,失败也不拦截发卡
        policy_cache["value"] = policy
    if isinstance(policy, dict) and "error" not in policy:
        # get_policy 在 MCP 工具层已把 *_cents 键的值转为 "50000元" 形态的
        # 展示串(mcp_server.get_policy),这里直接透传即可,不要再转一次
        policy_note = (f"单笔限额 {policy.get('single_tx_limit_cents')},"
                       f"当日累计转出限额 {policy.get('daily_out_limit_cents')}")
    else:
        policy_note = "限额信息暂不可用,确认前请知悉银行风控仍会二次校验"
    return {
        "order_id": order_view.get("id"),
        "amount_yuan": order_view.get("amount_yuan"),
        "to_name": order_view.get("to_name"),
        "scheduled_at": order_view.get("scheduled_at"),
        "status": order_view.get("status"),
        "memo": order_view.get("memo"),
        "policy_note": policy_note,          # 限额说明
        "policy": policy if isinstance(policy, dict) else None,
        "pay_required": pay_required,
        "confirm_hint": ("输入 6 位支付密码执行转账;回复「取消」撤销本单"
                         if pay_required else
                         "回复「确认」执行转账;回复「取消」撤销本单"),
    }


# ----------------------------------------------------------------- 应用工厂

def create_app(*, llm: Any = None, db_path: str | Path | None = None,
               checkpoint_path: str | Path | None = None,
               threads_path: str | Path | None = None) -> FastAPI:
    """构造 FastAPI 应用。

    Args:
        llm: 注入的聊天模型(测试传假模型);None 则 lifespan 里用 agent.llm.get_llm()。
        db_path: 银行库路径;None 取 BANK_CORE_DB / data/bank.db。
        checkpoint_path: 检查点库;None 取 AGENT_CHECKPOINT_DB / data/agent_ckpt.sqlite。
        threads_path: 会话索引库;None 取 AGENT_THREADS_DB / data/agent_threads.sqlite。
    """
    db = str(db_path or os.environ.get("BANK_CORE_DB")
             or (PROJECT_ROOT / "data" / "bank.db"))
    ckpt = str(checkpoint_path or os.environ.get("AGENT_CHECKPOINT_DB")
               or (PROJECT_ROOT / "data" / "agent_ckpt.sqlite"))
    thr = str(threads_path or os.environ.get("AGENT_THREADS_DB")
              or (PROJECT_ROOT / "data" / "agent_threads.sqlite"))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        import aiosqlite
        tools = await load_bank_tools(db)
        saver, conn = await make_sqlite_checkpointer(ckpt)
        # 会话索引(侧边栏清单用):thread_id → 标题/时间。消息本体在检查点库里,
        # 这里只存目录,不重复存内容——单一事实来源,避免两处数据打架。
        tconn = await aiosqlite.connect(thr)
        await tconn.execute(
            """CREATE TABLE IF NOT EXISTS threads (
                 thread_id         TEXT PRIMARY KEY,
                 title             TEXT NOT NULL,
                 created_at        TEXT NOT NULL,
                 updated_at        TEXT NOT NULL,
                 pending_question  TEXT NOT NULL DEFAULT '',
                 pending_payload   TEXT NOT NULL DEFAULT '',
                 revision          INTEGER NOT NULL DEFAULT 0,
                 last_client_id    TEXT NOT NULL DEFAULT '')""")
        try:  # 旧库补列(列已存在时忽略)
            await tconn.execute(
                "ALTER TABLE threads ADD COLUMN pending_question "
                "TEXT NOT NULL DEFAULT ''")
        except Exception:  # noqa: BLE001
            pass
        try:  # 账号体系:threads 归属用户(老行默认 1=陈明)
            await tconn.execute(
                "ALTER TABLE threads ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        except Exception:  # noqa: BLE001
            pass
        try:  # 跨设备同步:会话内容版本号(每次对话开始/结束递增)
            await tconn.execute(
                "ALTER TABLE threads ADD COLUMN revision "
                "INTEGER NOT NULL DEFAULT 0")
        except Exception:  # noqa: BLE001
            pass
        try:  # 浏览器刷新/另一台设备恢复时重建闸门卡片(interrupt 原始载荷)
            await tconn.execute(
                "ALTER TABLE threads ADD COLUMN pending_payload "
                "TEXT NOT NULL DEFAULT ''")
        except Exception:  # noqa: BLE001
            pass
        try:  # 跨设备同步:最后写入的设备/标签页(自己写的更新不用回拉覆盖本地卡片)
            await tconn.execute(
                "ALTER TABLE threads ADD COLUMN last_client_id "
                "TEXT NOT NULL DEFAULT ''")
        except Exception:  # noqa: BLE001
            pass
        try:  # 深度思考:最近一条助手消息的原生推理文本(刷新后思考面板不丢)
            await tconn.execute(
                "ALTER TABLE threads ADD COLUMN last_reasoning "
                "TEXT NOT NULL DEFAULT ''")
        except Exception:  # noqa: BLE001
            pass
        # 每个用户当前正在看的会话:手机扫码登录后自动跟随电脑端所在的会话
        await tconn.execute(
            """CREATE TABLE IF NOT EXISTS active_threads (
                 user_id    INTEGER PRIMARY KEY,
                 thread_id  TEXT NOT NULL,
                 updated_at TEXT NOT NULL)""")
        # 消息投票(点赞/点踩):一行一条投票,换票覆盖、同票再点取消。
        # 放 threads 库(会话目录同源),管理台跨库读这一份做满意度面板。
        await tconn.execute(
            """CREATE TABLE IF NOT EXISTS message_votes (
                 chat_id     TEXT NOT NULL,
                 message_id  TEXT NOT NULL,
                 is_upvoted  INTEGER NOT NULL,
                 updated_at  TEXT NOT NULL,
                 PRIMARY KEY (chat_id, message_id))""")
        await tconn.commit()
        # 账号库:auth 表建在 bank.db(注册即 users 行,管理后台天然同步);
        # 演示主用户陈明预置凭证(登录 Demo@12345 / 支付 888888,幂等)。
        import sqlite3 as _s
        auth_conn = _s.connect(db, check_same_thread=False)
        auth_conn.row_factory = _s.Row  # login 等按列名取值
        auth_mod.init_auth(auth_conn)
        auth_mod.ensure_demo_auth(auth_conn)
        migrate_cards_bound(auth_conn)  # 演示绑卡列(老库补列)
        # 评审修复:生产模型必须带超时/重试上限(测试注入的假模型原样使用)
        # 模型选择:显式 ZAI_MODEL 优先;否则按端点清单自动选(最新 flash 级,
        # 与前端模型探测同源,端点上新模型后重启即自动跟进)。
        # 生产用 RequestLLM 门面:请求可携带用户自填 Key(BYOK)与所选模型。
        model_name = os.environ.get("ZAI_MODEL") or await pick_default_model(
            provider=os.environ.get("ZAI_PROVIDER"))
        model = llm if llm is not None else RequestLLM(
            fallback_model=model_name,
            timeout=LLM_TIMEOUT_S, max_retries=LLM_MAX_RETRIES)
        app.state.bank = tools
        app.state.conn = conn
        app.state.threads = tconn
        app.state.auth_conn = auth_conn
        # 手机扫码登录的一次性票据(进程内存即可:短时效+一次性,
        # 重启丢票据的代价只是电脑端重新点一次"手机端登录")
        app.state.qr_tickets = {}
        app.state.qr_ttl = float(os.environ.get("AGENT_QR_TICKET_TTL", "120"))
        app.state.graph = build_agent_graph(model, tools, saver)
        app.state.model_desc = getattr(model, "model_name", None) \
            or getattr(model, "model", None) or type(model).__name__
        app.state.policy_cache = {}
        # 多用户:工具与图按登录用户缓存(BankTools 自持连接配置,
        # BANK_USER_ID 在装载时烧进 env → 每用户各一份;图对象轻量)。
        app.state.tools_by_user = {1: tools}
        app.state.graph_by_user = {1: app.state.graph}
        app.state.model = model
        app.state.saver = saver

        async def get_user_graph(uid: int):
            g = app.state.graph_by_user.get(uid)
            if g is None:
                t = app.state.tools_by_user.get(uid)
                if t is None:
                    t = await load_bank_tools(db, uid)
                    app.state.tools_by_user[uid] = t
                g = build_agent_graph(app.state.model, t, app.state.saver)
                app.state.graph_by_user[uid] = g
            return g

        app.state.get_user_graph = get_user_graph
        yield
        await conn.close()
        await tconn.close()
        auth_conn.close()

    app = FastAPI(title="AI Banking Agent API", version="0.1.0", lifespan=lifespan)
    # 前端(Next.js dev server :3000)联调用;演示项目放开来源
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                       allow_headers=["*"])

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok", "service": "agent-api",
                "bank_tools": len(app.state.bank.names),
                "db": db, "model": app.state.model_desc}

    @app.get("/api/history")
    async def history(thread_id: str,
                      x_bank_token: str | None = Header(default=None)) -> dict:
        """单个会话的完整消息(前端刷新后恢复现场用)。

        数据来源 = LangGraph 检查点里该 thread 的 messages(服务端 SQLite 持久化,
        与 Open WebUI 等成熟方案同思路:历史在服务端,任何浏览器打开都能恢复)。
        会话 token 走 x-bank-token 请求头(2026-10-07 前是 query 参数,会明文落
        access log,已全链路改头)。
        """
        # 归属校验:登录用户可看观光(0)与自己的会话;未登录只能看观光会话
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        allowed = {0, me["user_id"]} if me else {0}
        cur = await app.state.threads.execute(
            "SELECT user_id FROM threads WHERE thread_id=?", (thread_id,))
        row = await cur.fetchone()
        if row and row[0] not in allowed:
            return {"isReadonly": False, "visibility": "private",
                    "userId": None, "messages": []}
        snap = await app.state.graph.aget_state(
            {"configurable": {"thread_id": thread_id}})
        msgs: list[dict] = []
        values = getattr(snap, "values", None) if snap else None
        for i, m in enumerate((values or {}).get("messages") or []):
            content = m.content if isinstance(getattr(m, "content", None), str) else ""
            if not content.strip():
                continue
            if isinstance(m, HumanMessage):
                role = "user"
            elif isinstance(m, AIMessage):
                role = "assistant"
            else:
                continue
            # 去重:闸门问题由 api 补写一次、resume 后节点回流再带一次,
            # 连续相同的助手消息只保留一条(刷新恢复的现场才不啰嗦)。
            if (msgs and role == "assistant"
                    and msgs[-1]["role"] == "assistant"
                    and msgs[-1]["parts"][0]["text"] == content):
                continue
            msgs.append({"id": f"m{i}", "role": role,
                         "parts": [{"type": "text", "text": content}]})
        # 深度思考:该 thread 最近一条助手消息的原生推理文本落库在
        # threads.last_reasoning,刷新后作为 reasoning 部件挂回最后一条
        # 助手消息(前端 mergedReasoning 渲染思考面板)。
        rrow = await app.state.threads.execute(
            "SELECT last_reasoning FROM threads WHERE thread_id=?", (thread_id,))
        rrow_row = await rrow.fetchone()
        last_reasoning = (rrow_row[0] if rrow_row else "") or ""
        if last_reasoning and msgs and msgs[-1]["role"] == "assistant":
            msgs[-1]["parts"].insert(0, {"type": "reasoning",
                                         "text": last_reasoning})
        # 本 thread 停在闸门时,把待答问题补成最后一条助手消息
        # (存在会话目录里;刷新/另一台设备恢复时不再是"没理人")
        prow = await app.state.threads.execute(
            "SELECT pending_question,pending_payload FROM threads "
            "WHERE thread_id=?", (thread_id,))
        prow_row = await prow.fetchone()
        pending = (prow_row[0] if prow_row else "") or ""
        pending_payload_raw = (prow_row[1] if prow_row else "") or ""
        if pending:
            parts: list[dict] = [{"type": "text", "text": pending}]
            if pending_payload_raw:
                try:
                    payload = json.loads(pending_payload_raw)
                    if isinstance(payload, dict):
                        # 恢复闸门卡片(转账/理财/卡片/联系人等),不只恢复文本
                        parts.extend(await _interrupt_parts(payload, app))
                except Exception:  # noqa: BLE001 —— 坏载荷只丢卡片,不丢文本
                    pass
            if (msgs and msgs[-1]["role"] == "assistant"
                    and msgs[-1]["parts"][0]["text"] == pending):
                msgs[-1]["parts"] = parts
            else:
                msgs.append({"id": "pending", "role": "assistant",
                             "parts": parts})
        # 只读分析卡(Agent Core):analysis_view 由 router 每轮入口置 None,
        # 检查点里非 None 就代表"最后一轮是分析轮"。这里挂回最后一条助手消息
        # (插在播报文本之前,与直播时的卡片位置一致),否则刷新/换设备(手机
        # 加入电脑端会话)后只剩文字播报,分析报告卡凭空消失。
        acard = (values or {}).get("analysis_view")
        if isinstance(acard, dict) and msgs and msgs[-1]["role"] == "assistant":
            pos = next((i for i, p in enumerate(msgs[-1]["parts"])
                        if p.get("type") == "text"), 0)
            msgs[-1]["parts"].insert(pos, {"type": "data-analysis-plan",
                                           "data": acard})
        # 账单报告卡(年度/月度,2026-10-26):bill_view 同样由 router 每轮置 None,
        # 非 None 即"最后一轮是报告轮"。刷新后把卡挂回(与 analysis_view 同款),
        # 否则演示时刷新一下就只剩一句播报、报告卡凭空消失。
        bview = (values or {}).get("bill_view")
        if isinstance(bview, dict) and msgs and msgs[-1]["role"] == "assistant":
            pos = next((i for i, p in enumerate(msgs[-1]["parts"])
                        if p.get("type") == "text"), 0)
            msgs[-1]["parts"].insert(pos, {"type": "data-bill-report",
                                           "data": bview})
        return {"isReadonly": False, "visibility": "private",
                "userId": None, "messages": msgs}

    # ------------------------------------------------------------ 账号
    @app.post("/api/auth/register")
    async def auth_register(req: RegisterIn):
        """注册:真实姓名+身份证号+手机号(必填)+邮箱(选填)+登录密码+支付密码
        → users 行 + 0 元活期 + auth 行(管理后台立即可见);返回登录态 token
        (注册即登录,账号=手机号)。"""
        try:
            info = auth_mod.register(
                app.state.auth_conn, real_name=req.real_name,
                id_card=req.id_card, phone=req.phone, email=req.email,
                login_password=req.login_password,
                pay_password=req.pay_password)
        except auth_mod.AuthError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        sess = auth_mod.login(app.state.auth_conn, identifier=req.phone,
                              password=req.login_password)
        return {"user_id": info["user_id"], "nickname": info["nickname"],
                "identifier": auth_mod.mask_identifier(info["identifier"]),
                "token": sess["token"]}

    @app.post("/api/auth/login")
    async def auth_login(req: LoginIn):
        try:
            sess = auth_mod.login(app.state.auth_conn,
                                  identifier=req.identifier,
                                  password=req.password)
        except auth_mod.AuthError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        return {"token": sess["token"], "user_id": sess["user_id"],
                "nickname": sess["nickname"],
                "identifier": auth_mod.mask_identifier(sess["identifier"])}

    @app.post("/api/auth/logout")
    async def auth_logout(req: TokenIn) -> dict:
        auth_mod.logout(app.state.auth_conn, req.token)
        return {"ok": True}

    @app.get("/api/auth/me")
    async def auth_me(x_bank_token: str | None = Header(default=None)) -> dict:
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            raise HTTPException(status_code=401, detail="未登录或会话过期")
        return me

    # ------------------------------------------------ 账户 / 模拟入金(新用户自己产生余额)
    @app.get("/api/accounts")
    async def accounts(x_bank_token: str | None = Header(default=None)) -> dict:
        """当前登录用户的账户与实时余额(入金弹窗/余额展示用)。"""
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "请先登录后查看账户"})
        svc = LedgerService(app.state.auth_conn, user_id=me["user_id"])
        return {"accounts": svc.list_accounts()}

    @app.get("/api/bound-cards")
    async def bound_cards(x_bank_token: str | None = Header(default=None)) -> dict:
        """当前用户已绑定的演示银行卡(掩码 + 虚拟余额)。"""
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "请先登录后查看银行卡"})
        svc = LedgerService(app.state.auth_conn, user_id=me["user_id"])
        return {"cards": svc.list_bound_cards()}

    @app.post("/api/bound-cards")
    async def bind_bound_card(req: BoundCardIn,
                              x_bank_token: str | None = Header(default=None)):
        """演示绑卡:只记录银行/掩码/开户人并分配虚拟余额,不校验真实卡号。"""
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "请先登录后再绑定银行卡"})
        try:
            card = LedgerService(
                app.state.auth_conn, user_id=me["user_id"]).bind_bound_card(
                    req.bank_name, req.card_no, req.holder_name)
        except LedgerError as exc:
            return JSONResponse(status_code=400,
                                content={"error": str(exc)})
        return card

    @app.delete("/api/bound-cards/{card_id}")
    async def unbind_bound_card(card_id: int,
                                x_bank_token: str | None = Header(default=None)):
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "请先登录后再解绑银行卡"})
        try:
            return LedgerService(
                app.state.auth_conn, user_id=me["user_id"]).unbind_bound_card(
                    card_id)
        except LedgerError as exc:
            return JSONResponse(status_code=400,
                                content={"error": str(exc)})

    @app.post("/api/deposit")
    async def deposit(req: DepositIn,
                      x_bank_token: str | None = Header(default=None)):
        """模拟入金:演示外部账户 → 本人账户，入账即产生余额。

        刻意不接真实银行卡:入账不是动用户现有资金，因此不需要支付密码;
        但仍要求登录、单笔有上限、幂等键防重复入账。生产接入必须换成
        银行/持牌机构入金回调与对账，不能允许用户自助凭空加余额。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "请先登录后再入金"})
        try:
            amount_cents = yuan_to_cents(req.amount_yuan.strip())
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        if amount_cents > 10_000_000:  # 100,000 元/笔（演示上限）
            return JSONResponse(
                status_code=400,
                content={"error": "单笔模拟入金上限 100,000 元"})
        uid = me["user_id"]
        account_id = req.account_id
        if account_id is None:
            row = app.state.auth_conn.execute(
                "SELECT id FROM accounts WHERE user_id=? ORDER BY id LIMIT 1",
                (uid,)).fetchone()
            if not row:
                return JSONResponse(
                    status_code=400,
                    content={"error": "当前账号没有可用账户"})
            account_id = row["id"]
        key = req.idempotency_key.strip() or uuid4().hex
        try:
            svc = LedgerService(app.state.auth_conn, user_id=uid)
            if req.card_id is not None:
                result = svc.deposit_from_card(
                    account_id, req.card_id, amount_cents,
                    note=req.note.strip(), idempotency_key=key)
            else:
                result = svc.deposit(
                    account_id, amount_cents,
                    note=req.note.strip(), idempotency_key=key)
        except LedgerError as exc:
            return JSONResponse(status_code=400,
                                content={"error": str(exc)})
        return result

    # ------------------------------------------------ 手机扫码登录(电脑端已登录 → 手机免密登录)
    @app.post("/api/auth/qr-ticket")
    async def auth_qr_ticket(
            x_bank_token: str | None = Header(default=None)):
        """电脑端(已登录)签发一次性扫码票据,嵌入二维码 URL。

        票据 = 随机 24 字节 urlsafe,默认 120 秒有效、用一次即焚;
        只发给持有有效会话的请求(手机扫的是登录者本人屏幕上的码)。
        存进程内存:重启最多让电脑端重新点一次按钮,不引入新表。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "请先在电脑端登录,再生成手机登录二维码"})
        now = time.time()
        tickets = app.state.qr_tickets
        for key in [k for k, v in tickets.items() if v["expires"] <= now]:
            tickets.pop(key, None)
        ticket = secrets.token_urlsafe(24)
        tickets[ticket] = {"user_id": me["user_id"],
                           "expires": now + app.state.qr_ttl}
        return {"ticket": ticket, "expires_in": int(app.state.qr_ttl)}

    @app.post("/api/auth/qr-login")
    async def auth_qr_login(req: QrLoginIn):
        """手机端扫二维码后的免密登录:票据换正式 7 天会话 token。

        票据一次性(pop 即焚):同一张码扫第二次直接拒绝;过期同样拒绝。
        换到的会话与密码登录完全同权(动钱仍要支付密码闸门)。
        """
        entry = app.state.qr_tickets.pop(req.ticket, None)
        if not entry or entry["expires"] <= time.time():
            return JSONResponse(
                status_code=400,
                content={"error": "二维码已过期或已被使用,请在电脑端刷新后重扫"})
        try:
            sess = auth_mod.issue_session(app.state.auth_conn,
                                          entry["user_id"])
        except auth_mod.AuthError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        return {"token": sess["token"], "user_id": sess["user_id"],
                "nickname": sess["nickname"],
                "identifier": auth_mod.mask_identifier(sess["identifier"])}

    @app.get("/api/lan-info")
    async def lan_info() -> dict:
        """本机局域网 IP(前端拼手机可访问的扫码 URL 用)。

        UDP connect 只让路由层选出口网卡,不真正发包;取不到就退回
        127.0.0.1(此时二维码只能在同一台机器上打开,演示现场应保证有局域网)。
        """
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            host = s.getsockname()[0]
        except OSError:
            host = "127.0.0.1"
        finally:
            s.close()
        return {"host": host}

    @app.get("/api/qr")
    async def qr_png(data: str = ""):
        """本地二维码 PNG 渲染(前端 <img> 直接引用;离线可用,不出网)。

        data 由调用方(电脑端页面)拼好,通常是 /qr-login?ticket=... 的完整 URL。
        """
        data = (data or "").strip()[:512]
        if not data:
            return JSONResponse(status_code=400, content={"error": "缺少 data 参数"})
        import io

        import qrcode
        img = qrcode.make(data)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")

    # ------------------------------------------------ 常用收款人(转账选择器)
    @app.get("/api/contacts")
    async def contacts_list(
            x_bank_token: str | None = Header(default=None)) -> dict:
        """当前登录用户的收款人全量列表(聊天「我要转账」选择器数据源)。

        只返回本人 contacts;未登录返回 401(前端给出登录引导)。
        token 走 x-bank-token 头,不进 access log。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401, content={"error": "请先登录后查看收款人"})
        svc = LedgerService(app.state.auth_conn, user_id=me["user_id"])
        return {"contacts": svc.list_contacts()}

    @app.post("/api/contacts")
    async def contacts_add(
            req: ContactIn,
            x_bank_token: str | None = Header(default=None)):
        """新建收款人(姓名+手机号,手机号唯一)。

        校验复用 LedgerService.add_contact:姓名非空、手机号 1 开头 11 位、
        同手机号不可重复;写入 audit_log + change_log(动钱账户资料留痕)。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401, content={"error": "请先登录后新建收款人"})
        svc = LedgerService(app.state.auth_conn, user_id=me["user_id"])
        try:
            contact = svc.add_contact(req.name, req.phone, req.note)
        except LedgerError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        return {"contact": contact}

    # ------------------------------------------------ 悬空转账提醒(弹窗+落地页)
    @app.get("/api/pending-orders")
    async def pending_orders(
            x_bank_token: str | None = Header(default=None)) -> dict:
        """当前登录用户名下待确认/已排程的转账单(右下角强制提醒弹窗数据源)。

        pending_confirm=建单后悬空待确认;scheduled=定时未到期(到期由
        run_due_tasks 转成待确认)。观光/未登录返回空列表。
        token 走 x-bank-token 头,不进 access log。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return {"orders": []}
        rows = app.state.auth_conn.execute(
            """SELECT id, to_name, amount_cents, memo, status, created_at, scheduled_at
               FROM transfer_orders
               WHERE user_id=? AND status IN ('pending_confirm','scheduled')
               ORDER BY id DESC LIMIT 20""", (me["user_id"],)).fetchall()
        return {"orders": [{
            "order_id": r["id"], "to_name": r["to_name"],
            "amount_yuan": cents_to_yuan(r["amount_cents"]),
            "memo": r["memo"] or "", "status": r["status"],
            "created_at": r["created_at"],
            "scheduled_at": r["scheduled_at"] or "",
        } for r in rows]}

    # ------------------------------------------------ Agent 记忆检阅(view/delete)
    # 参考 Anthropic memory tool:按主题"一档一文件"、人类可读、可看可删。
    # 记忆是用户个人数据,只作用于本人(自作用域),未登录 401。
    @app.get("/api/memory")
    async def agent_memory(
            x_bank_token: str | None = Header(default=None)) -> dict:
        """当前用户全部长期记忆(画像置顶,含渲染行与更新时间)+长程任务轨道。

        「Agent 记忆」弹窗的数据源;库异常软处理返回空列表,绝不 500。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "未登录或会话过期"})
        return {"modules": memory_mod.memory_overview(me["user_id"]),
                "active_task": tasks_mod.get_active_task(me["user_id"])}

    @app.delete("/api/memory/active-task")
    async def forget_active_task(
            x_bank_token: str | None = Header(default=None)) -> dict:
        """结束当前长程任务轨道(用户对"任何一块"记忆都有控制权)。

        status=cancelled 落库留痕、不物理删;轨道视图立即清空。
        字面路由必须先于 /api/memory/{module} 注册,否则会被白名单 404 拦下。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "未登录或会话过期"})
        task = tasks_mod.get_active_task(me["user_id"])
        if task:
            tasks_mod.finish_task(me["user_id"], task.get("task_id"),
                                  status="cancelled")
        return {"modules": memory_mod.memory_overview(me["user_id"]),
                "active_task": tasks_mod.get_active_task(me["user_id"])}

    @app.delete("/api/memory")
    async def forget_all_memory(
            x_bank_token: str | None = Header(default=None)) -> dict:
        """一键清空全部记忆档案(module=None 全删);长程任务轨道不受影响。"""
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "未登录或会话过期"})
        memory_mod.forget(me["user_id"], None)
        return {"modules": memory_mod.memory_overview(me["user_id"]),
                "active_task": tasks_mod.get_active_task(me["user_id"])}

    @app.delete("/api/memory/{module}")
    async def forget_memory(module: str,
                            x_bank_token: str | None = Header(default=None)) -> dict:
        """忘记一个模块的记忆(用户对记忆有完全控制权)。

        白名单校验;返回剩余模块与任务轨道,前端原地刷新。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "未登录或会话过期"})
        if module not in memory_mod.MEMORY_MODULES:
            return JSONResponse(status_code=404, content={"error": "未知记忆模块"})
        memory_mod.forget(me["user_id"], module)
        return {"modules": memory_mod.memory_overview(me["user_id"]),
                "active_task": tasks_mod.get_active_task(me["user_id"])}

    # ------------------------------------------------ 消息投票(满意度反馈)
    # 模板裁剪版的空壳接口转真实:落 threads 库 message_votes 表。
    # 投票属于会话数据而非个人配置,访客也允许投(会话本身免登录可建);
    # PATCH 恒 200(模板前端 toast 依赖此语义),GET 返回该会话全部投票。
    @app.get("/api/vote")
    async def get_votes(chatId: str) -> list:
        """某会话的全部消息投票(刷新后恢复点赞/点踩高亮)。"""
        cur = await app.state.threads.execute(
            "SELECT chat_id, message_id, is_upvoted FROM message_votes "
            "WHERE chat_id=?", (chatId,))
        rows = await cur.fetchall()
        return [{"chatId": r[0], "messageId": r[1],
                 "isUpvoted": bool(r[2])} for r in rows]

    @app.patch("/api/vote")
    async def patch_vote(req: VoteIn):
        """投/换/取消一票:同票再点=取消(行删除),换票=覆盖,新票=插入。"""
        cur = await app.state.threads.execute(
            "SELECT is_upvoted FROM message_votes "
            "WHERE chat_id=? AND message_id=?",
            (req.chatId, req.messageId))
        row = await cur.fetchone()
        want = 1 if req.type == "up" else 0
        if row is not None and bool(row[0]) == bool(want):
            await app.state.threads.execute(
                "DELETE FROM message_votes WHERE chat_id=? AND message_id=?",
                (req.chatId, req.messageId))
        else:
            await app.state.threads.execute(
                """INSERT INTO message_votes (chat_id,message_id,is_upvoted,
                                              updated_at)
                   VALUES (?,?,?,?)
                   ON CONFLICT(chat_id, message_id) DO UPDATE SET
                     is_upvoted=excluded.is_upvoted,
                     updated_at=excluded.updated_at""",
                (req.chatId, req.messageId, want, _now_iso()))
        await app.state.threads.commit()
        return Response(status_code=200)

    # ------------------------------------------------ Agent 人设(persona)
    # 用户给 Agent 设定的性格/说话方式/温度;只作用于本人。未设置=完全现网行为。
    @app.get("/api/persona")
    async def get_persona(
            x_bank_token: str | None = Header(default=None)) -> dict:
        """读当前用户的人设(prompt + temperature)。"""
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "未登录或会话过期"})
        stored = persona_mod.get_persona(me["user_id"])
        return {"persona": stored["text"],
                "temperature": stored["temperature"]
                if stored["temperature"] is not None else 0.1,
                "is_default": not stored["text"]}

    @app.put("/api/persona")
    async def put_persona(req: PersonaIn,
                          x_bank_token: str | None = Header(default=None)) -> dict:
        """保存人设;persona 为空=恢复默认(不再注入)。"""
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "未登录或会话过期"})
        if not persona_mod.set_persona(me["user_id"], req.persona,
                                       req.temperature):
            return JSONResponse(status_code=500, content={"error": "人设保存失败"})
        stored = persona_mod.get_persona(me["user_id"])
        return {"persona": stored["text"],
                "temperature": stored["temperature"]
                if stored["temperature"] is not None else req.temperature,
                "is_default": not stored["text"]}

    @app.get("/api/orders/{order_id}/receipt")
    async def order_receipt(order_id: int,
                            x_bank_token: str | None = Header(default=None)):
        """播报即凭证:单笔转账订单的完整审计链(三表拼装,只读)。

        transfer_orders(状态机)→ transactions(external_ref=order:{id})
        → money_records(ref=order:{id},前后余额快照+operator)
        + audit_log 调用轨迹(工具/风险级/时间)。覆盖转账全家(立即/定时/
        联动锁定与到期购买,均为 transfer_orders);理财申赎不在本表范围。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401, content={"error": "未登录或会话过期"})
        uid = me["user_id"]
        o = app.state.auth_conn.execute(
            "SELECT * FROM transfer_orders WHERE id=? AND user_id=?",
            (order_id, uid)).fetchone()
        if not o:
            return JSONResponse(status_code=404, content={"error": "订单不存在"})
        order = {
            "order_id": o["id"], "to_name": o["to_name"],
            "amount_yuan": cents_to_yuan(o["amount_cents"]),
            "memo": o["memo"] or "", "status": o["status"],
            "created_at": o["created_at"],
            "scheduled_at": o["scheduled_at"] or "",
            "executed_at": o["executed_at"] or "",
            "idempotency_key": o["idempotency_key"],
        }
        tx = app.state.auth_conn.execute(
            "SELECT ts, direction, amount_cents, balance_after_cents,"
            " external_ref, tx_type FROM transactions WHERE external_ref=?",
            (f"order:{order_id}",)).fetchone()
        transaction = None
        if tx:
            transaction = {
                "ts": tx["ts"], "direction": tx["direction"],
                "amount_yuan": cents_to_yuan(tx["amount_cents"]),
                "balance_after_yuan": cents_to_yuan(tx["balance_after_cents"]),
                "external_ref": tx["external_ref"], "tx_type": tx["tx_type"],
            }
        # 资金流水库(与银行库同目录派生):按 ref 反查前后余额与操作方
        ledger_row = None
        try:
            from bank_core.recorder import money_db_path

            mpath = money_db_path(app.state.auth_conn)
            if mpath.is_file():
                mconn = sqlite3.connect(f"file:{mpath}?mode=ro", uri=True)
                mconn.row_factory = sqlite3.Row
                mrow = mconn.execute(
                    "SELECT ts, tool, delta_cents, before_cents, after_cents,"
                    " ref, note, operator FROM money_records WHERE ref=?"
                    " ORDER BY id DESC LIMIT 1", (f"order:{order_id}",)).fetchone()
                if mrow:
                    ledger_row = {
                        "ts": mrow["ts"], "tool": mrow["tool"],
                        "delta_yuan": cents_to_yuan(mrow["delta_cents"]),
                        "before_yuan": cents_to_yuan(mrow["before_cents"]),
                        "after_yuan": cents_to_yuan(mrow["after_cents"]),
                        "ref": mrow["ref"], "note": mrow["note"],
                        "operator": mrow["operator"],
                    }
                mconn.close()
        except Exception:  # noqa: BLE001 —— 流水库缺失不拦凭证主链路
            ledger_row = None
        # 审计轨迹:建单/确认/取消的工具调用记录(按单号 JSON 字段模糊匹配)
        pats = (f'"order_id": {order_id}', f'"id": {order_id}')
        like_pats = tuple(f"%{p}%" for p in pats)
        audit_rows = app.state.auth_conn.execute(
            "SELECT ts, tool, risk FROM audit_log WHERE "
            "args_json LIKE ? OR result_json LIKE ? "
            "OR args_json LIKE ? OR result_json LIKE ? ORDER BY id",
            (*like_pats, *like_pats)).fetchall()
        return {"order": order, "transaction": transaction,
                "ledger": ledger_row,
                "audit_trail": [{"ts": r["ts"], "tool": r["tool"],
                                 "risk": r["risk"]} for r in audit_rows]}

    @app.post("/api/orders/confirm")
    async def order_confirm(req: OrderConfirmIn):
        """页面直连确认(弹窗"去确认"落地页):token 定用户 → 支付密码核验
        → confirm_transfer_order 收尾。与对话闸门同一套安全规则(两步走+
        支付密码),只是入口从聊天卡换成网页;operator 默认记 'agent'。

        防暴力破解(2026-10-09):支付密码仅 4 次尝试机会,连续 4 错即锁定
        该账户的转账功能(建单/确认全线拒绝,ledger 层统一卡点),成功一次
        即清零计数;解锁仅管理员在管理台操作。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, req.token)
        if not me:
            return JSONResponse(status_code=401, content={"error": "未登录或会话过期"})
        uid = me["user_id"]
        row = app.state.auth_conn.execute(
            "SELECT transfer_locked, pay_fail_count FROM users WHERE id=?",
            (uid,)).fetchone()
        if row and row["transfer_locked"]:
            return JSONResponse(status_code=403, content={
                "error": "转账功能已锁定（连续支付密码错误），请联系管理员解除"})
        order = app.state.auth_conn.execute(
            "SELECT status FROM transfer_orders WHERE id=? AND user_id=?",
            (req.order_id, uid)).fetchone()
        if not order:
            return JSONResponse(status_code=404, content={"error": "订单不存在"})
        if order["status"] == "scheduled":
            return JSONResponse(status_code=400,
                                content={"error": "定时转账未到期,到期后才可确认"})
        if order["status"] != "pending_confirm":
            return JSONResponse(
                status_code=400,
                content={"error": f"订单状态为 {order['status']},不可确认"})
        if not auth_mod.verify_pay_password(app.state.auth_conn,
                                            uid, req.pay_password):
            fails = int(row["pay_fail_count"] or 0) + 1
            if fails >= 4:
                app.state.auth_conn.execute(
                    "UPDATE users SET pay_fail_count=?, transfer_locked=1 "
                    "WHERE id=?", (fails, uid))
                app.state.auth_conn.commit()
                return JSONResponse(status_code=403, content={
                    "error": "已连续输错 4 次支付密码,转账功能已锁定,请联系管理员解除"})
            app.state.auth_conn.execute(
                "UPDATE users SET pay_fail_count=? WHERE id=?", (fails, uid))
            app.state.auth_conn.commit()
            return JSONResponse(status_code=400, content={
                "error": f"支付密码不正确（已错 {fails}/4 次,连续 4 次将锁定转账功能）"})
        # 密码正确:计数清零(证明持有人在场,不给爆破者累积窗口)
        if row and row["pay_fail_count"]:
            app.state.auth_conn.execute(
                "UPDATE users SET pay_fail_count=0 WHERE id=?", (uid,))
            app.state.auth_conn.commit()
        try:
            svc = LedgerService(app.state.auth_conn, user_id=uid)
            done = svc.confirm_transfer_order(req.order_id)
        except LedgerError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        return {"ok": True, "order_id": req.order_id, "status": done["status"]}

    @app.post("/api/threads")
    async def create_thread(req: ThreadCreateIn,
                            x_bank_token: str | None = Header(default=None)):
        """新建会话(手机端「+」):必须先命名,名称 ≤10 个字。

        只写会话目录一行,不动图状态;进会话后的首条消息只刷新时间,
        不会用首句覆盖这里的命名(_touch_thread 的 upsert 不更新 title)。
        """
        title = " ".join(req.title.split()).strip()
        if not title:
            return JSONResponse(status_code=400,
                                content={"error": "请先给会话起个名字"})
        if len(title) > 10:
            return JSONResponse(status_code=400,
                                content={"error": "会话名称最多 10 个字"})
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        owner = me["user_id"] if me else 0
        thread_id = str(uuid4())
        now = _now_iso()
        await app.state.threads.execute(
            """INSERT INTO threads (thread_id,title,created_at,updated_at,
                                   pending_question,user_id,revision,last_client_id)
               VALUES (?,?,?,?, '',?,0,'')""",
            (thread_id, title, now, now, owner))
        await app.state.threads.execute(
            """INSERT INTO active_threads (user_id,thread_id,updated_at)
               VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 thread_id=excluded.thread_id,
                 updated_at=excluded.updated_at""",
            (owner, thread_id, now))
        await app.state.threads.commit()
        return {"id": thread_id, "title": title, "createdAt": now}

    @app.get("/api/threads")
    async def threads(limit: int = 100, offset: int = 0, q: str = "",
                      x_bank_token: str | None = Header(default=None)) -> dict:
        """会话目录(侧边栏/手机端会话总览):按最后活跃倒序,offset 分页。

        带登录 token(x-bank-token 头)只看自己的会话;观光(无 token)看
        观光会话(user_id=0)。hasMore 表示还有下一页,前端滚动到底继续取。

        q 非空时按关键词检索:先匹配标题,再读该会话的 checkpoint 消息全文
        (含用户与助手所有文本、待答问题),命中返回 snippet 供前端展示。
        搜索是一次性过滤后分页,数据量=个人会话数(本机演示规模)。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        owner = me["user_id"] if me else 0
        limit = max(1, min(limit, 200))
        offset = max(0, offset)
        needle = (q or "").strip()
        if needle:
            cur = await app.state.threads.execute(
                """SELECT thread_id,title,updated_at,pending_question FROM threads
                   WHERE user_id=? ORDER BY updated_at DESC""",
                (owner,))
            rows = await cur.fetchall()
            low = needle.lower()
            matched: list[tuple[str, str, str, str]] = []
            for r in rows:
                tid, title, updated, pending = r[0], r[1] or "", r[2], r[3] or ""
                snippet = ""
                if low not in title.lower():
                    # 标题未命中才读图状态:命中词可能在任何一轮用户/助手消息里
                    try:
                        snap = await app.state.graph.aget_state(
                            {"configurable": {"thread_id": tid}})
                        msgs = ((getattr(snap, "values", None) or {})
                                .get("messages") or [])
                        for m in msgs:
                            content = getattr(m, "content", "")
                            if (isinstance(content, str)
                                    and low in content.lower()):
                                snippet = _snippet(content, needle)
                                break
                    except Exception:  # noqa: BLE001 —— 单个会话读失败不拖垮搜索
                        pass
                    if not snippet and low in pending.lower():
                        snippet = _snippet(pending, needle)
                    if not snippet:
                        continue
                matched.append((tid, title, updated, snippet))
            total = len(matched)
            page = matched[offset:offset + limit]
            chats = [{"id": tid, "title": title, "createdAt": updated,
                      "userId": None, "visibility": "private",
                      "snippet": snippet}
                     for tid, title, updated, snippet in page]
            return {"chats": chats, "hasMore": offset + limit < total,
                    "total": total, "query": needle}
        # 多取一条判断 hasMore,避免额外 COUNT 查询
        cur = await app.state.threads.execute(
            """SELECT thread_id, title, updated_at FROM threads
               WHERE user_id=? ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
            (owner, limit + 1, offset))
        rows = await cur.fetchall()
        has_more = len(rows) > limit
        rows = rows[:limit]
        chats = [{"id": r[0], "title": r[1], "createdAt": r[2],
                  "userId": None, "visibility": "private"} for r in rows]
        return {"chats": chats, "hasMore": has_more}

    # ------------------------------------------------ 跨设备会话同步(手机 ↔ 电脑)
    @app.get("/api/sync/state")
    async def sync_state(x_bank_token: str | None = Header(default=None)):
        """当前用户"正在看的会话"+内容版本号(前端轮询,约 1.5s 一次)。

        active_threads 由打开会话/发消息时写入;缺失时回落到最近活跃会话。
        revision 在同一轮对话开始/结束时各 +1,另一台设备据此拉取历史。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "登录后才能同步会话"})
        uid = me["user_id"]
        cur = await app.state.threads.execute(
            """SELECT a.thread_id, t.title, t.updated_at, t.revision,
                      t.last_client_id
               FROM active_threads a
               JOIN threads t
                 ON t.thread_id=a.thread_id AND t.user_id=?
               WHERE a.user_id=?""",
            (uid, uid))
        row = await cur.fetchone()
        if not row:  # 没有 active 行/会话已被删除:回落到最近活跃会话
            cur = await app.state.threads.execute(
                """SELECT thread_id, title, updated_at, revision,
                          last_client_id
                   FROM threads WHERE user_id=?
                   ORDER BY updated_at DESC LIMIT 1""",
                (uid,))
            row = await cur.fetchone()
        if not row:
            return {"thread_id": None, "title": "", "updated_at": "",
                    "revision": 0, "last_client_id": ""}
        return {"thread_id": row[0], "title": row[1], "updated_at": row[2],
                "revision": int(row[3] or 0), "last_client_id": row[4] or ""}

    @app.post("/api/sync/active")
    async def sync_active(req: SyncActiveIn,
                          x_bank_token: str | None = Header(default=None)):
        """登记当前设备打开的会话;另一台设备轮询到后会跟随切换。"""
        me = auth_mod.resolve_token(app.state.auth_conn, x_bank_token)
        if not me:
            return JSONResponse(status_code=401,
                                content={"error": "登录后才能同步会话"})
        uid = me["user_id"]
        row = await app.state.threads.execute(
            "SELECT 1 FROM threads WHERE thread_id=? AND user_id=?",
            (req.thread_id, uid))
        if not await row.fetchone():
            # 同步是"尽力而为"的提示:新会话刚 pushState、thread 行尚未落库时
            # 会短暂查不到;返回 200+ok=false,避免浏览器控制台刷 404。
            return {"ok": False, "error": "会话尚未登记"}
        now = _now_iso()
        await app.state.threads.execute(
            """INSERT INTO active_threads (user_id,thread_id,updated_at)
               VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 thread_id=excluded.thread_id,
                 updated_at=excluded.updated_at""",
            (uid, req.thread_id, now))
        await app.state.threads.commit()
        return {"ok": True, "thread_id": req.thread_id}

    @app.post("/api/validate-key")
    async def validate_key(req: ValidateKeyRequest) -> dict:
        """校验用户接入的供应商(拉一次模型清单,不耗推理 token)。

        支持 DeepSeek/OpenAI/自定义(OpenAI 兼容)与 Claude(Anthropic 原生);
        base_url 为空时用部署方环境变量。
        """
        from agent import llm as llm_mod  # 动态引用,便于测试 monkeypatch
        try:
            base = normalize_base_url(req.base_url) or (
                os.environ.get("ZAI_BASE_URL")
                or "https://open.bigmodel.cn/api/paas/v4/")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        provider = req.provider or "openai"
        try:
            payload = await asyncio.to_thread(
                llm_mod.fetch_models_json, base, req.api_key, provider)
            return {"ok": True,
                    "model_count": len(payload.get("data") or [])}
        except Exception as exc:  # noqa: BLE001 —— 校验失败返回原因,不抛 500
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}

    @app.get("/api/reminders")
    async def reminders(as_of: str | None = None) -> dict:
        """到期提醒(前端"到期提醒"横幅入口)。

        调 run_due_tasks 触发到期任务(演示同步触发;as_of 为比赛演示的时间旅行,
        'YYYY-MM-DDTHH:MM:SS',把"今天"拨到指定时刻),把联动类到期项格式化成
        一条可直接发给助手的提醒文本(前端点「发给助手处理」即作为普通消息发送,
        图按 '(到期提醒)' 前缀分流进 l_due 到期处理);非联动提醒原样透传。
        任何失败返回空列表,绝不 500 —— 提醒入口挂了不能影响聊天主链路。
        """
        try:
            fired = await app.state.bank.call("run_due_tasks", as_of=as_of)
        except Exception:  # noqa: BLE001 —— 工具/会话层异常:空列表收尾
            return {"reminders": []}
        if not isinstance(fired, list):  # 含 {"error": ...} 形态
            return {"reminders": []}
        out: list[dict] = []
        title_cache: dict[int, str] = {}  # 同一计划的标题只查一次
        for item in fired:
            if not isinstance(item, dict):
                continue
            payload = item.get("payload") or {}
            if payload.get("plan_id") is None or not payload.get("what"):
                # 非联动到期项(普通提醒/定时转账到期):文本原样透传
                out.append({"kind": "plain",
                            "text": str(item.get("message") or "")})
                continue
            plan_id = int(payload["plan_id"])
            if plan_id not in title_cache:
                # 提醒 payload 只有任务标题,计划名取 get_linkage_plan(READ)
                d = await app.state.bank.call("get_linkage_plan", plan_id=plan_id)
                title_cache[plan_id] = (str(d.get("title") or "")
                                        if isinstance(d, dict) and not d.get("error")
                                        else "")
            out.append({
                "kind": "linkage",
                "text": (f"(到期提醒) 计划《{title_cache[plan_id]}》的"
                         f"【{payload.get('what')}】今天到期,请帮我处理"),
                "plan_id": plan_id,
                "action_idx": payload.get("action_idx"),
            })
        # ---- 代扣预审(事前知情):未来 N 天将扣费的订阅主动浮出 ----
        # 只读不动钱;已放行/跳过的周期由服务端 ack 抑制;失败静默不影响上面
        try:
            upcoming = await app.state.bank.call(
                "list_upcoming_precharges", as_of=as_of, within_days=3)
            if isinstance(upcoming, list):
                for r in upcoming:
                    if not isinstance(r, dict) or r.get("error"):
                        continue
                    left = int(r.get("days_left") or 0)
                    when = "今天" if left == 0 else ("明天" if left == 1
                                                    else f"{left} 天后")
                    note = f"({r.get('price_note')})" if r.get("price_note") else ""
                    out.append({
                        "kind": "precharge",
                        "text": (f"(订阅预审) {r.get('merchant')} {when}将扣 "
                                 f"{r.get('amount_yuan')} 元{note},"
                                 f"发送「订阅预审」放行 / 跳过 / 解约"),
                        "sub_id": r.get("sub_id"),
                        "merchant": r.get("merchant"),
                        "next_charge_date": r.get("next_charge_date"),
                    })
        except Exception:  # noqa: BLE001 —— 预审段失败不影响到期提醒主链路
            pass
        return {"reminders": out}

    def _now_iso() -> str:
        from datetime import datetime
        # 微秒精度:同秒内多次会话也能按最后活跃正确排序
        return datetime.now().isoformat(timespec="microseconds")

    async def _touch_thread(thread_id: str, first_user_text: str,
                            owner: int = 1, client_id: str = "") -> None:
        """登记/刷新会话目录:标题取首条用户消息(截断),时间取当下。

        owner=会话归属(user_id;观光=0)。已存在的会话不改归属——
        防止别人拿到 thread_id 就把会话挂到自己名下。

        revision 每次对话开始/结束各 +1,跨设备同步靠它判断内容是否有更新;
        last_client_id 记录最后写入的设备,自己写入的更新不回拉覆盖本地卡片。
        同时把该用户"当前会话"指向这里,另一台设备会跟随。
        """
        title = first_user_text.strip().replace("\n", " ")
        title = title[:30] + ("…" if len(title) > 30 else "")
        now = _now_iso()
        await app.state.threads.execute(
            """INSERT INTO threads (thread_id,title,created_at,updated_at,
                                   pending_question,user_id,revision,last_client_id)
               VALUES (?,?,?,?, '',?,1,?)
               ON CONFLICT(thread_id) DO UPDATE SET
                 updated_at=excluded.updated_at,
                 pending_question='',
                 pending_payload='',
                 last_reasoning='',
                 revision=revision+1,
                 last_client_id=excluded.last_client_id""",
            (thread_id, title or "新会话", now, now, owner, client_id))
        await app.state.threads.execute(
            """INSERT INTO active_threads (user_id,thread_id,updated_at)
               VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 thread_id=excluded.thread_id,
                 updated_at=excluded.updated_at""",
            (owner, thread_id, now))
        await app.state.threads.commit()

    async def _bump_thread(thread_id: str, owner: int,
                           client_id: str = "") -> None:
        """一轮对话结束:revision +1,通知另一台设备拉取最终助手文本。"""
        now = _now_iso()
        await app.state.threads.execute(
            """UPDATE threads SET revision=revision+1, updated_at=?,
                                  last_client_id=?
               WHERE thread_id=?""",
            (now, client_id, thread_id))
        # 只有该会话仍是用户当前会话时才刷新活跃时间;不抢回被另一台设备切走的焦点
        await app.state.threads.execute(
            """UPDATE active_threads SET updated_at=?
               WHERE user_id=? AND thread_id=?""",
            (now, owner, thread_id))
        await app.state.threads.commit()

    @app.post("/api/chat")
    async def chat(req: ChatRequest) -> StreamingResponse:
        # 登录态:token → user_id(观光=None,图内 auth_guard 拦业务意图);
        # 图与银行工具按用户缓存(BANK_USER_ID 烧进 MCP 子进程 env)。
        me = auth_mod.resolve_token(app.state.auth_conn, req.token)
        uid = me["user_id"] if me else None
        graph = await app.state.get_user_graph(uid or 1)
        last_user = next((m.content for m in reversed(req.messages)
                          if m.role == "user" and m.content.strip()), None)
        if not last_user:
            raise HTTPException(status_code=400, detail="messages 里没有有效的 user 消息")
        try:
            base_url = normalize_base_url(req.base_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await _touch_thread(req.thread_id, str(last_user), owner=uid or 0,
                            client_id=req.client_id or "")

        cfg = {"configurable": {"thread_id": req.thread_id},
               "recursion_limit": RECURSION_LIMIT}
        # 该 thread 是否停在 interrupt:是则本条消息作为 resume 值恢复执行
        snapshot = await graph.aget_state(cfg)
        paused = bool(snapshot.next) and any(t.interrupts for t in snapshot.tasks)
        graph_input: Any = (Command(resume=last_user) if paused else
                            {"auth_user_id": uid,
                             "messages": [HumanMessage(content=last_user)]})
        # 多单并行挂起(2026-10-26):停在**转账支付密码闸门**且新消息其实是
        # 另一笔转账指令时,不再"必须先处理上一单"——第一段 resume 让闸门
        # 分流把当前单挂起(不确认不取消,不动钱),第二段把指令作为新输入
        # 正常建单。两段串在同一响应里,用户无需重说一遍。
        turn_inputs: list[Any] = []
        if paused and _gate_new_transfer(snapshot, last_user):
            turn_inputs = [Command(resume=last_user),
                           {"auth_user_id": uid,
                            "messages": [HumanMessage(content=last_user)]}]
            graph_input = turn_inputs[0]

        async def event_source() -> AsyncIterator[str]:
            yield _frame({"type": "start", "messageId": uuid4().hex})
            gate: dict | None = None
            gate_payload: dict | None = None
            # BYOK/模型切换:本请求携带的 Key/端点/供应商覆盖图内所有 LLM 调用
            token = set_request_llm(api_key=req.api_key, model=req.model,
                                    base_url=base_url,
                                    provider=req.provider,
                                    enable_thinking=req.enable_thinking)
            open_text: dict[str, str] = {}
            open_reason: dict[str, str] = {}
            cur_text: str | None = None
            cur_reason: str | None = None
            streamed_nodes: set[str] = set()
            # messages 通道去重:LLM 消息会以两种形态出现——
            #   ① 流式模型的 AIMessageChunk(逐 token);
            #   ② 节点经 add_messages 落状态的完整 AIMessage(回显,必现:
            #      ask_llm 改 astream 后"token chunks + 状态回显"都走本通道)。
            # 回显再发就是同 id 巨型 delta → 回复整体重复。非流式模型的 LLM
            # 结果也是普通 AIMessage(带 reasoning_content,必须发),它先于
            # 回显出现。规则:chunk 恒发;普通 AIMessage 仅当该节点"既没流过
            # token、也没发过普通消息"时才发(= 非流式模型的 LLM 结果),
            # 其余一律视为回显跳过。
            streamed_any: dict[str, bool] = {}
            plain_seen: set[str] = set()
            # 深度思考持久化:本轮全部 reasoning 文本(刷新后 /api/history 挂回)
            reason_saved: list[str] = []
            # reasoning 合帧:推理模型每秒可产出几十个增量,逐帧下发会把前端
            # 打卡(整棵消息树反复重渲染)。攒够 REASON_FLUSH_CHARS 字符才发一帧;
            # 节点切换/推理结束由 _close 兜底清空缓冲。
            reason_buf: dict[str, list[str]] = {}
            REASON_FLUSH_CHARS = 60

            def _flush_reason(node: str) -> list[str]:
                buf = reason_buf.pop(node, None)
                rid = open_reason.get(node)
                if not buf or rid is None:
                    return []
                piece = "".join(buf)
                reason_saved.append(piece)
                return [_frame({"type": "reasoning-delta", "id": rid,
                                "delta": piece})]

            def _close(node: str) -> list[str]:
                nonlocal cur_text, cur_reason
                fr = _flush_reason(node)
                t = open_text.pop(node, None)
                if t is not None:
                    fr.append(_frame({"type": "text-end", "id": t}))
                r = open_reason.pop(node, None)
                if r is not None:
                    fr.append(_frame({"type": "reasoning-end", "id": r}))
                if cur_text == node:
                    cur_text = None
                if cur_reason == node:
                    cur_reason = None
                return fr

            try:
                # 评审修复:单轮整体 deadline(模型/工具挂起时前端 guaranteed 收到
                # error 帧 + finish,不再无限等待;asyncio.timeout 取消底层流)
                async with asyncio.timeout(TURN_DEADLINE_S):
                  # 多段输入(turn_inputs):闸门上新转账指令的两段式——
                  # 段1 resume(分流挂起旧单)+ 段2 新输入(办理新单)。
                  # 缩进两格是刻意的:不动内部 400 行帧处理逻辑,只包一层循环。
                  for one_input in (turn_inputs or [graph_input]):
                    # 段间防御:上一段收尾后若仍停在闸门(分流未生效),
                    # 后续段不再作为新输入硬闯(interrupt 节点会把新输入当
                    # resume 值),退化为已完成的段(旧行为等价)。
                    if one_input is not graph_input:
                        snap2 = await graph.aget_state(cfg)
                        if bool(snap2.next) and any(
                                t.interrupts for t in snap2.tasks):
                            break
                    async for mode, data in graph.astream(
                            one_input, cfg,
                            stream_mode=["messages", "updates"]):
                        if mode == "messages":
                            chunk, meta = data
                            node = (meta or {}).get("langgraph_node")
                            if node not in ANNOUNCE_NODES:
                                continue
                            if isinstance(chunk, AIMessageChunk):
                                streamed_any[node] = True
                            else:
                                # 普通 AIMessage:LLM 结果或状态回显,二选一
                                if node in plain_seen or streamed_any.get(node):
                                    continue  # 状态回显,已发过
                                plain_seen.add(node)
                            # 推理先流:捕获模型原生 reasoning_content
                            rtext = _chunk_reasoning(chunk)
                            if rtext:
                                if cur_reason is not None and cur_reason != node:
                                    for f in _close(cur_reason):
                                        yield f
                                rid = open_reason.get(node)
                                if rid is None:
                                    rid = uuid4().hex
                                    open_reason[node] = rid
                                    yield _frame({"type": "reasoning-start", "id": rid})
                                # 合帧下发(见 reason_buf 注释),不再逐 token 轰炸
                                buf = reason_buf.setdefault(node, [])
                                buf.append(rtext)
                                if sum(len(p) for p in buf) >= REASON_FLUSH_CHARS:
                                    for f in _flush_reason(node):
                                        yield f
                                cur_reason = node
                            # 文本再流:真实逐 token
                            ctext = (chunk.content if isinstance(chunk.content, str)
                                     else "")
                            if ctext:
                                if cur_text is not None and cur_text != node:
                                    for f in _close(cur_text):
                                        yield f
                                tid = open_text.get(node)
                                if tid is None:
                                    tid = uuid4().hex
                                    open_text[node] = tid
                                    yield _frame({"type": "text-start", "id": tid})
                                    # 同节点答案开始即闭合其推理(推理先于答案)
                                    if node in open_reason:
                                        for f in _flush_reason(node):
                                            yield f
                                        yield _frame({"type": "reasoning-end",
                                                      "id": open_reason.pop(node)})
                                yield _frame({"type": "text-delta", "id": tid,
                                              "delta": ctext})
                                cur_text = node
                            # 只有真正下发过内容才标记"已流式"。避免两种情况误伤:
                            # ① OpenAI 流式首个 chunk 常只有 role(content=""),
                            #    若因此被标记,updates 兜底会跳过 → 答案丢失;
                            # ② content 非字符串(多模态 list)时 messages 侧发不出文本,
                            #    必须留给 updates 兜底。
                            if rtext or ctext:
                                streamed_nodes.add(node)
                        else:  # updates 模式:保留原 data-* / data-thinking / interrupt 逻辑
                            for node, update in data.items():
                                if node == "__interrupt__":
                                    for intr in update:
                                        payload = intr.value if isinstance(intr.value, dict) else {}
                                        question = str(payload.get("question") or "")
                                        if question:
                                            for f in _text_frames(question):
                                                yield f
                                        for f in await _interrupt_frames(payload, app):
                                            yield f
                                        gate = {"gate_type": payload.get("type"),
                                                "question": question}
                                        gate_payload = payload
                                elif node in ANNOUNCE_NODES and isinstance(update, dict):
                                    if node in streamed_nodes:
                                        continue  # 已通过 messages 逐字下发,避免重复
                                    for m in update.get("messages") or []:
                                        if isinstance(m, AIMessage):
                                            rtext = _chunk_reasoning(m)
                                            if rtext:
                                                reason_saved.append(rtext)
                                                rid = uuid4().hex
                                                yield _frame({"type": "reasoning-start",
                                                              "id": rid})
                                                yield _frame({"type": "reasoning-delta",
                                                              "id": rid, "delta": rtext})
                                                yield _frame({"type": "reasoning-end",
                                                              "id": rid})
                                            if m.content:
                                                for f in _text_frames(str(m.content)):
                                                    yield f
                                elif node == "s_list" and isinstance(update, dict) \
                                        and update.get("sub_view"):
                                    # 订阅清单只读无闸门:列表卡随节点更新直接下发
                                    # (与闸门卡不同,它不占 interrupt,刷新恢复时由
                                    # /api/history 的文本播报兜底)
                                    yield _frame({"type": "data-subscription-list",
                                                  "data": update["sub_view"]})
                                elif node == "s_precheck" and isinstance(update, dict) \
                                        and update.get("precheck_view"):
                                    # 代扣预审卡(事前知情):与订阅清单同款下发模式,
                                    # 决策按钮以普通消息回话,不占 interrupt
                                    yield _frame({"type": "data-subscription-precheck",
                                                  "data": update["precheck_view"]})
                                elif node == "b_run" and isinstance(update, dict) \
                                        and update.get("bill_view"):
                                    # 年度/月度账单报告卡:报告数据随 b_run 更新
                                    # 直接下发(与订阅清单同款,不占 interrupt;
                                    # 只读,无确认按钮)
                                    yield _frame({"type": "data-bill-report",
                                                  "data": update["bill_view"]})
                                elif node == "w_query" and isinstance(update, dict) \
                                        and update.get("wealth_view"):
                                    # 可购产品列表卡:问"现在有哪些产品"时随 w_query
                                    # 更新直接下发(与订阅清单同款,不占 interrupt;
                                    # 申购按钮以普通消息回话走既有受闸门链路)
                                    yield _frame({"type": "data-wealth-products",
                                                  "data": update["wealth_view"]})
                                elif node in ("a_exec", "a_reflect") \
                                        and isinstance(update, dict) \
                                        and update.get("analysis_view"):
                                    # Agent Core 只读分析卡:与订阅清单同款下发模式
                                    # (不占 interrupt)。a_exec 只在"要转交既有受闸门
                                    # 链路"时发卡,a_reflect 在只读闭环收尾时发卡,
                                    # 两条路互斥,不会重复渲染两张卡。
                                    yield _frame({"type": "data-analysis-plan",
                                                  "data": update["analysis_view"]})
                                elif node in ("a_perceive", "a_plan", "a_exec",
                                              "a_reflect") \
                                        and isinstance(update, dict) \
                                        and update.get("thinking_view"):
                                    # 思维链:把 Agent Core 的"识别意图→计划→守卫→执行→
                                    # 复盘"按节点真实进度流式暴露给用户。a_perceive 最早
                                    # 发"识别意图",a_plan 补全规划与守卫,a_exec 追加执行,
                                    # a_reflect 收尾复盘——每帧都是累计全貌,前端合并成
                                    # 一个实时生长的面板。
                                    yield _frame({"type": "data-thinking",
                                                  "data": update["thinking_view"]})
                    # 收尾:闭合所有仍开着的 part,避免前端 part 永远处于 streaming
                    for node in list(open_text) + list(open_reason):
                        for f in _close(node):
                            yield f
            except TimeoutError:
                # 先闭合仍开着的 part,避免前端永远停在"生成中"
                for node in list(open_text) + list(open_reason):
                    for f in _close(node):
                        yield f
                yield _frame({"type": "error",
                              "errorText": f"本轮处理超时(>{TURN_DEADLINE_S:.0f}秒),"
                                           "已中止;如需继续请重新发起。"})
                yield _frame({"type": "finish", "finishReason": "error"})
                return
            except Exception as exc:  # noqa: BLE001 —— 流式响应里必须转成 error 帧
                for node in list(open_text) + list(open_reason):
                    for f in _close(node):
                        yield f
                yield _frame({"type": "error",
                              "errorText": f"{type(exc).__name__}: {exc}"})
                yield _frame({"type": "finish", "finishReason": "error"})
                return
            finally:
                reset_request_llm(token)
            if gate is not None:  # 本轮停在闸门:先给前端一个显式状态部件再收尾
                yield _frame({"type": "data-gate-pending", "data": gate})
                # 把待答问题记到会话目录(不碰图状态——aupdate_state 会破坏
                # interrupt 恢复语义,实测翻车):刷新恢复现场时 /api/history
                # 把它补成最后一条助手消息,resume 后自然被真实消息取代。
                try:
                    now = _now_iso()
                    await app.state.threads.execute(
                        """UPDATE threads SET pending_question=?,
                               pending_payload=?,
                               revision=revision+1, updated_at=?,
                               last_client_id=?
                           WHERE thread_id=?""",
                        (gate.get("question") or "",
                         json.dumps(gate_payload or {}, ensure_ascii=False,
                                    default=str),
                         now, req.client_id or "", req.thread_id))
                    await app.state.threads.execute(
                        """UPDATE active_threads SET updated_at=?
                           WHERE user_id=? AND thread_id=?""",
                        (now, uid or 0, req.thread_id))
                    await app.state.threads.commit()
                except Exception:  # noqa: BLE001 —— 补记失败不影响本轮收尾
                    pass
            # 本轮结束再推一次版本:另一台设备据此拉取完整助手文本/待答问题
            # 深度思考落库:思考文本存 threads.last_reasoning,刷新后
            # /api/history 把它作为 reasoning 部件挂回最后一条助手消息
            # (新回合开始时 _touch_thread 会清空,只保留最近一轮)。
            if reason_saved:
                try:
                    await app.state.threads.execute(
                        "UPDATE threads SET last_reasoning=? WHERE thread_id=?",
                        ("".join(reason_saved)[-8000:], req.thread_id))
                    await app.state.threads.commit()
                except Exception:  # noqa: BLE001 —— 落库失败不影响回复
                    pass
            try:
                await _bump_thread(req.thread_id, uid or 0,
                                   req.client_id or "")
            except Exception:  # noqa: BLE001 —— 同步标记失败不影响回复
                pass
            yield _frame({"type": "finish", "finishReason": "stop"})

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={STREAM_HEADER: STREAM_HEADER_VALUE,
                     "cache-control": "no-cache",
                     "x-accel-buffering": "no"},  # 防代理缓冲 SSE
        )

    async def _interrupt_parts(payload: dict, app: FastAPI) -> list[dict]:
        """把图的 interrupt 载荷映射为 DATA-* 部件(问题文本由调用方另发 text 帧)。"""
        ptype = payload.get("type")
        tools: BankTools = app.state.bank
        if ptype == "confirm_transfer":
            card = await _transfer_card(payload.get("order") or {}, tools,
                                        app.state.policy_cache,
                                        pay_required=bool(payload.get("pay_required")))
            return [{"type": "data-transfer-confirmation", "data": card}]
        if ptype == "confirm_split":
            return [{"type": "data-split-confirmation",
                            "data": {**(payload.get("bill") or {}), "pay_required": bool(payload.get("pay_required"))}}]
        if ptype == "confirm_settle":
            return [{"type": "data-settle-confirmation",
                            "data": {**(payload.get("settle") or {}), "pay_required": bool(payload.get("pay_required"))}}]
        if ptype == "pick_contact":
            return [{"type": "data-contact-choices",
                            "data": {"candidates": payload.get("candidates") or []}}]
        if ptype == "confirm_contact":
            return [{"type": "data-contact-confirmation",
                            "data": {**(payload.get("contact") or {}), "pay_required": bool(payload.get("pay_required"))}}]
        if ptype == "confirm_linkage":
            # 联动计划闸门:整卡数据照载荷的 plan 视图(标题/日期/预算/各动作/锁定单)
            return [{"type": "data-linkage-plan",
                            "data": {**(payload.get("plan") or {}), "pay_required": bool(payload.get("pay_required"))}}]
        if ptype == "confirm_linkage_action":
            # 到期动作闸门:what/merchant/amount + 计划定位字段
            return [{"type": "data-linkage-action",
                            "data": {**(payload.get("action") or {}), "pay_required": bool(payload.get("pay_required"))}}]
        if ptype == "confirm_wealth":
            # 理财闸门(申购/赎回共用,kind 区分):kind 提升到顶层,
            # order 视图含 产品/金额/费率/锁定期/风险/账户(或持仓/估值/费用/到手)
            view = payload.get("order") or {}
            return [{"type": "data-wealth-confirmation",
                            "data": {"kind": view.get("kind"), "order": view,
                                     "pay_required": bool(payload.get("pay_required"))}}]
        if ptype == "confirm_card":
            # 卡片闸门(办卡/限额/状态共用,kind 区分):kind 提升到顶层,
            # card 视图含 card_id/尾号/类型/当前限额或状态/目标值
            view = payload.get("card") or {}
            return [{"type": "data-card-confirmation",
                            "data": {"kind": view.get("kind"), "card": view,
                                     "pay_required": bool(payload.get("pay_required"))}}]
        if ptype == "confirm_sub_cancel":
            # 订阅取消闸门:data-subscription-cancel 金色卡(商户/每期金额/
            # 下次扣费/年省);pay_required 时前端渲染支付密码框
            sub = payload.get("sub") or {}
            return [{"type": "data-subscription-cancel",
                            "data": {**sub, "pay_required": bool(payload.get("pay_required"))}}]
        if ptype == "confirm_plan":
            # 生日预算计划闸门(普通确认,确认本身不扣款):plan 视图由
            # plan.plan_card_view 冻结;pay_required 恒为 False,取消订阅与
            # 锁定预算各自还有一道支付密码闸门(p_cut→s_gate / p_lock→l_gate)
            return [{"type": "data-plan",
                            "data": {**(payload.get("plan") or {}), "pay_required": False}}]
        if ptype == "wealth_entry":
            # 理财中心入口卡:风险等级 + 在售产品(标注可购/受限)+ 目的选项
            # (question 由调用方另发 text 帧,这里只下卡片数据)
            return [{"type": "data-wealth-entry",
                            "data": {k: v for k, v in payload.items()
                                     if k not in ("type", "question")}}]
        if ptype == "ask_slot":
            # options:枚举型反问的可点选项(graph 各 *_options 提供,可缺省);
            # 前端 AskSlot 渲染选项按钮 + 「其他」自由输入区
            return [{"type": "data-ask-slot",
                            "data": {"missing": payload.get("missing") or [],
                                     "options": payload.get("options") or []}}]
        return []  # 未知类型只发问题文本,不臆造部件

    async def _interrupt_frames(payload: dict, app: FastAPI) -> list[str]:
        """SSE 帧封装；历史恢复复用同一套 data-* 映射（_interrupt_parts）。"""
        return [_frame(part) for part in await _interrupt_parts(payload, app)]

    return app


app = create_app()  # 供 uvicorn agent.api:app;导入无副作用,初始化在 lifespan


def main() -> None:
    import uvicorn

    port = int(os.environ.get("AGENT_API_PORT", "8800"))
    # 绑 0.0.0.0:前端用 localhost/127.0.0.1/局域网 IP 打开都能打到同一后端
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
