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
    data-wealth-confirmation    理财确认卡片(kind=subscribe|redeem;order=产品/金额/费率/锁定期/风险/账户)
    data-card-confirmation      卡片确认卡片(kind=apply|limits|status;card=card_id/尾号/类型/当前值/目标值)
    data-contact-choices        同名联系人选项(卡片式消歧,而非开放反问)
    data-ask-slot               缺槽反问(附缺失字段清单)
    data-gate-pending           本轮结束时图停在人工闸门(前端可切换输入态为"确认/取消")
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
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from agent import auth as auth_mod
from agent.bank import BankTools, load_bank_tools
from agent.graph import build_agent_graph, make_sqlite_checkpointer
from bank_core.ledger import LedgerError, LedgerService
from bank_core.money import cents_to_yuan
from agent.llm import (RequestLLM, fetch_models_json, get_llm,
                       pick_default_model, reset_request_llm, set_request_llm)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

STREAM_HEADER = "x-vercel-ai-ui-message-stream"
STREAM_HEADER_VALUE = "v1"  # 依据:ai-sdk.dev stream-protocol(自定义后端必须设 v1)

# 播报型节点:这些节点的 AIMessage 是给用户看的最终文本。
# 不播报其他节点的消息(如 t_gate 复放的问题文本),避免与闸门卡片重复。
ANNOUNCE_NODES = frozenset({"t_report", "sb_report", "ss_report", "c_report",
                            "l_report", "b_report", "w_report", "k_report",
                            "chat"})

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
    # BYOK(可选):界面设置里填的 Key 随请求生效;不填走部署方环境变量
    api_key: str | None = Field(default=None, max_length=200)
    # 模型切换(可选):前端选择器选中的模型;不填走自动选型
    model: str | None = Field(default=None, max_length=64)
    # 登录会话(可选):注册/登录下发的 token;不带=观光模式(闲聊可用,
    # 业务意图被 auth_guard 引导登录)
    token: str | None = Field(default=None, max_length=128)


class ValidateKeyRequest(BaseModel):
    api_key: str = Field(min_length=10, max_length=200)


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


# ----------------------------------------------------------------- 卡片(自定义 data 部件)

async def _transfer_card(order_view: dict, tools: BankTools,
                         policy_cache: dict, pay_required: bool = False) -> dict:
    """转账确认卡片字段(本服务自有契约;金额/收款人来自建单回执,限额来自 get_policy)。"""
    policy = policy_cache.get("value")
    if policy is None:
        policy = await tools.call("get_policy")  # [READ] 只读,失败也不拦截发卡
        policy_cache["value"] = policy
    if isinstance(policy, dict) and "error" not in policy:
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
                 pending_question  TEXT NOT NULL DEFAULT '')""")
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
        await tconn.commit()
        # 账号库:auth 表建在 bank.db(注册即 users 行,管理后台天然同步);
        # 演示主用户陈明预置凭证(登录 Demo@12345 / 支付 888888,幂等)。
        import sqlite3 as _s
        auth_conn = _s.connect(db, check_same_thread=False)
        auth_conn.row_factory = _s.Row  # login 等按列名取值
        auth_mod.init_auth(auth_conn)
        auth_mod.ensure_demo_auth(auth_conn)
        # 评审修复:生产模型必须带超时/重试上限(测试注入的假模型原样使用)
        # 模型选择:显式 ZAI_MODEL 优先;否则按端点清单自动选(最新 flash 级,
        # 与前端模型探测同源,端点上新模型后重启即自动跟进)。
        # 生产用 RequestLLM 门面:请求可携带用户自填 Key(BYOK)与所选模型。
        model_name = os.environ.get("ZAI_MODEL") or await pick_default_model()
        model = llm if llm is not None else RequestLLM(
            fallback_model=model_name,
            timeout=LLM_TIMEOUT_S, max_retries=LLM_MAX_RETRIES)
        app.state.bank = tools
        app.state.conn = conn
        app.state.threads = tconn
        app.state.auth_conn = auth_conn
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
    async def history(thread_id: str, token: str | None = None) -> dict:
        """单个会话的完整消息(前端刷新后恢复现场用)。

        数据来源 = LangGraph 检查点里该 thread 的 messages(服务端 SQLite 持久化,
        与 Open WebUI 等成熟方案同思路:历史在服务端,任何浏览器打开都能恢复)。
        """
        # 归属校验:登录用户可看观光(0)与自己的会话;未登录只能看观光会话
        me = auth_mod.resolve_token(app.state.auth_conn, token)
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
        # 本 thread 停在闸门时,把待答问题补成最后一条助手消息
        # (存在会话目录里;刷新恢复的现场不再是"没理人")
        prow = await app.state.threads.execute(
            "SELECT pending_question FROM threads WHERE thread_id=?", (thread_id,))
        prow_row = await prow.fetchone()
        pending = (prow_row[0] if prow_row else "") or ""
        if pending and not (msgs and msgs[-1]["role"] == "assistant"
                            and msgs[-1]["parts"][0]["text"] == pending):
            msgs.append({"id": "pending", "role": "assistant",
                         "parts": [{"type": "text", "text": pending}]})
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
    async def auth_me(token: str) -> dict:
        me = auth_mod.resolve_token(app.state.auth_conn, token)
        if not me:
            raise HTTPException(status_code=401, detail="未登录或会话过期")
        return me

    # ------------------------------------------------ 悬空转账提醒(弹窗+落地页)
    @app.get("/api/pending-orders")
    async def pending_orders(token: str) -> dict:
        """当前登录用户名下待确认/已排程的转账单(右下角强制提醒弹窗数据源)。

        pending_confirm=建单后悬空待确认;scheduled=定时未到期(到期由
        run_due_tasks 转成待确认)。观光/未登录返回空列表。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, token)
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

    @app.post("/api/orders/confirm")
    async def order_confirm(req: OrderConfirmIn):
        """页面直连确认(弹窗"去确认"落地页):token 定用户 → 支付密码核验
        → confirm_transfer_order 收尾。与对话闸门同一套安全规则(两步走+
        支付密码),只是入口从聊天卡换成网页;operator 默认记 'agent'。"""
        me = auth_mod.resolve_token(app.state.auth_conn, req.token)
        if not me:
            return JSONResponse(status_code=401, content={"error": "未登录或会话过期"})
        order = app.state.auth_conn.execute(
            "SELECT status FROM transfer_orders WHERE id=? AND user_id=?",
            (req.order_id, me["user_id"])).fetchone()
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
                                            me["user_id"], req.pay_password):
            return JSONResponse(status_code=400, content={"error": "支付密码不正确"})
        try:
            svc = LedgerService(app.state.auth_conn, user_id=me["user_id"])
            done = svc.confirm_transfer_order(req.order_id)
        except LedgerError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        return {"ok": True, "order_id": req.order_id, "status": done["status"]}

    @app.get("/api/threads")
    async def threads(limit: int = 100, token: str | None = None) -> dict:
        """会话目录(侧边栏):按最后活跃倒序。

        带登录 token 只看自己的会话;观光(无 token)看观光会话(user_id=0)。
        """
        me = auth_mod.resolve_token(app.state.auth_conn, token)
        owner = me["user_id"] if me else 0
        limit = max(1, min(limit, 200))
        cur = await app.state.threads.execute(
            """SELECT thread_id, title, updated_at FROM threads
               WHERE user_id=? ORDER BY updated_at DESC LIMIT ?""",
            (owner, limit))
        rows = await cur.fetchall()
        chats = [{"id": r[0], "title": r[1], "createdAt": r[2],
                  "userId": None, "visibility": "private"} for r in rows]
        return {"chats": chats, "hasMore": False}

    @app.post("/api/validate-key")
    async def validate_key(req: ValidateKeyRequest) -> dict:
        """校验用户自填的 API Key(拉一次模型清单即可,不耗推理 token)。"""
        from agent import llm as llm_mod  # 动态引用,便于测试 monkeypatch
        base = (os.environ.get("ZAI_BASE_URL")
                or "https://open.bigmodel.cn/api/paas/v4/")
        try:
            payload = await asyncio.to_thread(
                llm_mod.fetch_models_json, base, req.api_key)
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
        return {"reminders": out}

    def _now_iso() -> str:
        from datetime import datetime
        # 微秒精度:同秒内多次会话也能按最后活跃正确排序
        return datetime.now().isoformat(timespec="microseconds")

    async def _touch_thread(thread_id: str, first_user_text: str,
                            owner: int = 1) -> None:
        """登记/刷新会话目录:标题取首条用户消息(截断),时间取当下。

        owner=会话归属(user_id;观光=0)。已存在的会话不改归属——
        防止别人拿到 thread_id 就把会话挂到自己名下。
        """
        title = first_user_text.strip().replace("\n", " ")
        title = title[:30] + ("…" if len(title) > 30 else "")
        now = _now_iso()
        await app.state.threads.execute(
            """INSERT INTO threads (thread_id,title,created_at,updated_at,
                                   pending_question,user_id)
               VALUES (?,?,?,?, '',?)
               ON CONFLICT(thread_id) DO UPDATE SET
                 updated_at=excluded.updated_at,
                 pending_question=''""",
            (thread_id, title or "新会话", now, now, owner))
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
        await _touch_thread(req.thread_id, str(last_user), owner=uid or 0)

        cfg = {"configurable": {"thread_id": req.thread_id},
               "recursion_limit": RECURSION_LIMIT}
        # 该 thread 是否停在 interrupt:是则本条消息作为 resume 值恢复执行
        snapshot = await graph.aget_state(cfg)
        paused = bool(snapshot.next) and any(t.interrupts for t in snapshot.tasks)
        graph_input: Any = (Command(resume=last_user) if paused else
                            {"auth_user_id": uid,
                             "messages": [HumanMessage(content=last_user)]})

        async def event_source() -> AsyncIterator[str]:
            yield _frame({"type": "start", "messageId": uuid4().hex})
            gate: dict | None = None
            # BYOK/模型切换:本请求携带的 Key/模型覆盖图内所有 LLM 调用
            token = set_request_llm(api_key=req.api_key, model=req.model)
            try:
                # 评审修复:单轮整体 deadline(模型/工具挂起时前端 guaranteed 收到
                # error 帧 + finish,不再无限等待;asyncio.timeout 取消底层流)
                async with asyncio.timeout(TURN_DEADLINE_S):
                    async for chunk in graph.astream(graph_input, cfg,
                                                     stream_mode="updates"):
                        for node, update in chunk.items():
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
                            elif node in ANNOUNCE_NODES and isinstance(update, dict):
                                for m in update.get("messages") or []:
                                    if isinstance(m, AIMessage) and m.content:
                                        for f in _text_frames(str(m.content)):
                                            yield f
            except TimeoutError:
                yield _frame({"type": "error",
                              "errorText": f"本轮处理超时(>{TURN_DEADLINE_S:.0f}秒),"
                                           "已中止;如需继续请重新发起。"})
                yield _frame({"type": "finish", "finishReason": "error"})
                return
            except Exception as exc:  # noqa: BLE001 —— 流式响应里必须转成 error 帧
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
                    await app.state.threads.execute(
                        "UPDATE threads SET pending_question=? WHERE thread_id=?",
                        (gate.get("question") or "", req.thread_id))
                    await app.state.threads.commit()
                except Exception:  # noqa: BLE001 —— 补记失败不影响本轮收尾
                    pass
            yield _frame({"type": "finish", "finishReason": "stop"})

        return StreamingResponse(
            event_source(),
            media_type="text/event-stream",
            headers={STREAM_HEADER: STREAM_HEADER_VALUE,
                     "cache-control": "no-cache",
                     "x-accel-buffering": "no"},  # 防代理缓冲 SSE
        )

    async def _interrupt_frames(payload: dict, app: FastAPI) -> list[str]:
        """把图的 interrupt 载荷映射为 DATA-* 部件(问题文本由调用方另发 text 帧)。"""
        ptype = payload.get("type")
        tools: BankTools = app.state.bank
        if ptype == "confirm_transfer":
            card = await _transfer_card(payload.get("order") or {}, tools,
                                        app.state.policy_cache,
                                        pay_required=bool(payload.get("pay_required")))
            return [_frame({"type": "data-transfer-confirmation", "data": card})]
        if ptype == "confirm_split":
            return [_frame({"type": "data-split-confirmation",
                            "data": {**(payload.get("bill") or {}), "pay_required": bool(payload.get("pay_required"))}})]
        if ptype == "confirm_settle":
            return [_frame({"type": "data-settle-confirmation",
                            "data": {**(payload.get("settle") or {}), "pay_required": bool(payload.get("pay_required"))}})]
        if ptype == "pick_contact":
            return [_frame({"type": "data-contact-choices",
                            "data": {"candidates": payload.get("candidates") or []}})]
        if ptype == "confirm_contact":
            return [_frame({"type": "data-contact-confirmation",
                            "data": {**(payload.get("contact") or {}), "pay_required": bool(payload.get("pay_required"))}})]
        if ptype == "confirm_linkage":
            # 联动计划闸门:整卡数据照载荷的 plan 视图(标题/日期/预算/各动作/锁定单)
            return [_frame({"type": "data-linkage-plan",
                            "data": {**(payload.get("plan") or {}), "pay_required": bool(payload.get("pay_required"))}})]
        if ptype == "confirm_linkage_action":
            # 到期动作闸门:what/merchant/amount + 计划定位字段
            return [_frame({"type": "data-linkage-action",
                            "data": {**(payload.get("action") or {}), "pay_required": bool(payload.get("pay_required"))}})]
        if ptype == "confirm_wealth":
            # 理财闸门(申购/赎回共用,kind 区分):kind 提升到顶层,
            # order 视图含 产品/金额/费率/锁定期/风险/账户(或持仓/估值/费用/到手)
            view = payload.get("order") or {}
            return [_frame({"type": "data-wealth-confirmation",
                            "data": {"kind": view.get("kind"), "order": view,
                                     "pay_required": bool(payload.get("pay_required"))}})]
        if ptype == "confirm_card":
            # 卡片闸门(办卡/限额/状态共用,kind 区分):kind 提升到顶层,
            # card 视图含 card_id/尾号/类型/当前限额或状态/目标值
            view = payload.get("card") or {}
            return [_frame({"type": "data-card-confirmation",
                            "data": {"kind": view.get("kind"), "card": view,
                                     "pay_required": bool(payload.get("pay_required"))}})]
        if ptype == "ask_slot":
            return [_frame({"type": "data-ask-slot",
                            "data": {"missing": payload.get("missing") or []}})]
        return []  # 未知类型只发问题文本,不臆造部件

    return app


app = create_app()  # 供 uvicorn agent.api:app;导入无副作用,初始化在 lifespan


def main() -> None:
    import uvicorn

    port = int(os.environ.get("AGENT_API_PORT", "8800"))
    # 绑 0.0.0.0:前端用 localhost/127.0.0.1/局域网 IP 打开都能打到同一后端
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
