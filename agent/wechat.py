"""微信 IM 渠道适配层 —— 公众号(测试号)回调入口 + 客服消息回发。

方案:docs/微信IM接入方案-20261029.md(P0)。定位是"微信只做壳":
这里只做验签/加解密、消息归一化、openid↔账号绑定、去重与回发,
业务执行全部复用 /api/chat 同款图调用(checkpointer/记忆/审计照旧),
不新增 MCP 工具,不动 tool_meta/EXPECTED_TOOL_COUNT 基线。

平台硬规则(2026-10 已核实):
- 被动回复须 5 秒内响应,超时微信会重试(重试=重复消息)→ 一律
  "立即 ack + asyncio 后台处理 + MsgId 去重";
- 被动回复文本上限 2048 字节 → 结果走客服消息接口主动下发(48h 交互
  窗口内,超窗 errcode 45015),长文本按 1800 字节分片;
- 测试号可跑明文模式;配置 WECHAT_AES_KEY 后自动切加密模式(官方
  AES-CBC 加解密方案)。

安全边界(通道分级,方案 §4.6):
- 未绑定 openid = 观光模式(auth_user_id=None,图内 auth_guard 拦业务);
- 微信通道不在**转账支付密码闸门**上 resume——动钱确认引导回 App,
  支付密码永远不出现在微信会话;
- 业务动作的审计由 ledger/Service 工具层既有 audit_log 覆盖,本层不重复记。

环境键(全部选填;不配则回调路由返回 503 引导,见 webui/.env.example):
  WECHAT_CALLBACK_TOKEN  回调验签 Token(测试号后台填同一个值)
  WECHAT_TEST_APPID      测试号 appID
  WECHAT_TEST_APPSECRET  测试号 appsecret(客服消息接口取 access_token)
  WECHAT_AES_KEY         EncodingAESKey(43 位;不配=明文模式)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import sqlite3
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx
from fastapi import HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

logger = logging.getLogger("agent.wechat")

RECURSION_LIMIT = 50  # 与 api.py 同值(反问循环另有状态级封顶,双保险)
TURN_DEADLINE_S = float(os.environ.get("AGENT_TURN_DEADLINE", "180"))
PUSH_CHUNK_BYTES = 1800  # 客服消息文本按 ≤1800 字节分片(被动回复上限 2048 的保守值)
DEDUP_TTL_S = 600        # MsgId 去重窗口:微信超时重试间隔分钟级,10 分钟足够

# 被动回复模式(个人订阅号):无客服消息接口(个人主体不能认证,调了报 48001),
# 只能"用户发消息 → 5 秒内 HTTP 响应里回 XML"。微信超时会重试(共 3 次机会),
# 每次挂等 PASSIVE_WAIT_S 抢答;3 次都没抢到 → 答案进存稿箱,用户发任意消息收取。
PASSIVE_WAIT_S = float(os.environ.get("WECHAT_PASSIVE_WAIT_S", "4.5"))
PASSIVE_MAX_ATTEMPTS = 3
PASSIVE_PAGE_CHARS = 550  # 被动回复上限 2048 字节,550 汉字(×3B)+XML 包装稳过

BIND_RE = re.compile(r"^绑定\s*(1\d{10})$")
UNBIND_RE = re.compile(r"^(解绑|解除绑定)$")

WELCOME_TEXT = (
    "您好,这里是 BankAgent 智能银行助手。\n"
    "可以直接发消息咨询或办理业务,例如:查一下我的余额、本月账单汇总。\n"
    "首次使用请回复「绑定 你的手机号」绑定银行账户(如:绑定 13800002233)。"
)
ACK_TEXT = "已收到,正在处理,请稍候…"

try:  # cryptography 随既有依赖链已在 .venv;缺失时仅加密模式不可用
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    _HAS_CRYPTO = True
except ImportError:  # pragma: no cover
    _HAS_CRYPTO = False


# ---------------------------------------------------------------- 配置

@dataclass
class WechatConfig:
    callback_token: str = ""
    appid: str = ""
    appsecret: str = ""
    aes_key: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.callback_token)


def load_config() -> WechatConfig:
    return WechatConfig(
        callback_token=os.environ.get("WECHAT_CALLBACK_TOKEN", "").strip(),
        appid=os.environ.get("WECHAT_TEST_APPID", "").strip(),
        appsecret=os.environ.get("WECHAT_TEST_APPSECRET", "").strip(),
        aes_key=os.environ.get("WECHAT_AES_KEY", "").strip(),
    )


# ---------------------------------------------------------------- 验签与加解密(官方方案)

def wx_signature(*parts: str) -> str:
    """微信回调签名:sha1(字典序拼接)。GET 验证与 POST msg_signature 同算法。"""
    return hashlib.sha1("".join(sorted(parts)).encode("utf-8")).hexdigest()


def decrypt_message(aes_key_b64: str, msg_encrypt_b64: str,
                    expect_appid: str = "") -> str:
    """官方加解密方案:Base64 → AES-256-CBC(key=EncodingAESKey+'=', iv=key[:16])。

    明文 = 16 字节随机串 + 4 字节网络序消息长度 + 消息 XML + 接收方 appid。
    """
    if not _HAS_CRYPTO:  # pragma: no cover
        raise RuntimeError("收到加密消息但未安装 cryptography,无法解密")
    key = base64.b64decode(aes_key_b64 + "=")
    data = base64.b64decode(msg_encrypt_b64)
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    dec = cipher.decryptor()
    plain = dec.update(data) + dec.finalize()
    plain = plain[:-plain[-1]]  # 去 PKCS7
    msg_len = struct.unpack(">I", plain[16:20])[0]
    from_appid = plain[20 + msg_len:].decode("utf-8", "replace")
    if expect_appid and from_appid != expect_appid:
        raise ValueError(f"解密报文 appid 不匹配:{from_appid}")
    return plain[20:20 + msg_len].decode("utf-8")


# ---------------------------------------------------------------- 消息归一化

@dataclass
class Inbound:
    msg_type: str = ""          # text / event / voice / ...
    openid: str = ""            # FromUserName(粉丝 openid,假名标识)
    account: str = ""           # ToUserName(公众号自己的微信号)
    content: str = ""           # text 内容
    msg_id: str = ""            # 去重键(event 类无 MsgId)
    event: str = ""             # subscribe / CLICK / ...
    event_key: str = ""


def parse_message(xml_text: str) -> Inbound:
    root = ET.fromstring(xml_text)
    get = lambda tag: (root.findtext(tag) or "").strip()  # noqa: E731
    return Inbound(
        msg_type=get("MsgType"), openid=get("FromUserName"),
        account=get("ToUserName"), content=get("Content"),
        msg_id=get("MsgId"), event=get("Event"), event_key=get("EventKey"))


def ack_text_xml(account: str, openid: str, content: str) -> str:
    """被动回复文本(明文模式专用;加密模式回 'success' 不被动回复)。"""
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{escape(openid)}]]></ToUserName>"
        f"<FromUserName><![CDATA[{escape(account)}]]></FromUserName>"
        f"<CreateTime>{int(time.time())}</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{escape(content)}]]></Content>"
        "</xml>")


class Deduper:
    """MsgId 去重(进程内存):微信 5 秒无响应会重试最多 3 次,重试=同 MsgId。"""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}

    def first_time(self, msg_id: str) -> bool:
        now = time.monotonic()
        for k in [k for k, t in self._seen.items() if now - t > DEDUP_TTL_S]:
            self._seen.pop(k, None)
        if not msg_id:  # event 类无 MsgId,不去重
            return True
        if msg_id in self._seen:
            return False
        self._seen[msg_id] = now
        return True


# ---------------------------------------------------------------- 被动回复(订阅号)

@dataclass
class _Turn:
    """一条消息的在途回合:passive 模式下,微信的每次重试都挂到同一 Turn 抢答。"""

    msg_id: str
    openid: str
    event: asyncio.Event
    pages: list[str] = field(default_factory=list)  # 分页后的完整回答
    done: bool = False
    delivered: bool = False                          # 已被动回复过首页/提示
    attempts: int = 0


def _paginate(text: str, per: int = PASSIVE_PAGE_CHARS) -> list[str]:
    """按字符数分页(被动回复 2048 字节上限,中文按字符稳过)。"""
    text = text.strip()
    if not text:
        return [""]
    return [text[i:i + per] for i in range(0, len(text), per)]


# ---------------------------------------------------------------- openid ↔ 账号绑定

def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS wechat_bind (
             openid   TEXT PRIMARY KEY,
             user_id  INTEGER NOT NULL REFERENCES users(id),
             status   TEXT NOT NULL DEFAULT 'bound',
             channel  TEXT NOT NULL DEFAULT 'gh_test',
             bound_at TEXT NOT NULL)""")
    conn.commit()


def lookup_uid(conn: sqlite3.Connection, openid: str) -> int | None:
    row = conn.execute(
        "SELECT user_id FROM wechat_bind WHERE openid=? AND status='bound'",
        (openid,)).fetchone()
    return int(row[0]) if row else None


def bind_phone(conn: sqlite3.Connection, openid: str, phone: str) -> dict:
    """绑定:手机号必须已注册(users.phone)。返回 {ok, name?} / {ok:False, error}。"""
    row = conn.execute(
        "SELECT id, name FROM users WHERE phone=?", (phone,)).fetchone()
    if not row:
        return {"ok": False, "error": "该手机号尚未注册银行账户,请先在 App 内注册"}
    conn.execute(
        """INSERT INTO wechat_bind (openid, user_id, status, channel, bound_at)
           VALUES (?,?, 'bound', 'gh_test', ?)
           ON CONFLICT(openid) DO UPDATE SET
             user_id=excluded.user_id, status='bound', bound_at=excluded.bound_at""",
        (openid, int(row[0]), datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return {"ok": True, "name": row[1]}


def unbind(conn: sqlite3.Connection, openid: str) -> None:
    conn.execute("UPDATE wechat_bind SET status='disabled' WHERE openid=?", (openid,))
    conn.commit()


# ---------------------------------------------------------------- 回发(客服消息接口)

def split_for_wechat(text: str, max_bytes: int = PUSH_CHUNK_BYTES) -> list[str]:
    """按字节上限分片,切在字符边界;空串至少返回一片。"""
    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for ch in text:
        w = len(ch.encode("utf-8"))
        if buf and size + w > max_bytes:
            chunks.append("".join(buf))
            buf, size = [], 0
        buf.append(ch)
        size += w
    chunks.append("".join(buf))
    return chunks or [""]


_MD_PATTERNS = [
    (re.compile(r"^#{1,6}\s*", re.M), ""),            # 标题井号
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),      # 粗体
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"\1"),   # 斜体(不吞粗体)
    (re.compile(r"`([^`]+)`"), r"\1"),                # 行内代码
    (re.compile(r"^\s*[-*]{3,}\s*$", re.M), ""),      # 分隔线
    (re.compile(r"^\s*\|(.+)\|\s*$", re.M), r"\1"),   # 表格行去围栏
    (re.compile(r"^\s*[\|:\- ]+\|?\s*$", re.M), ""),  # 表格分隔行(|---|---|)
    (re.compile(r"\n{3,}"), "\n\n"),                  # 压缩空行
]


def markdown_to_plain(text: str) -> str:
    """Markdown 降级为纯文本(微信文本消息不渲染 MD;表格降为竖线分隔)。"""
    out = text
    for pat, rep in _MD_PATTERNS:
        out = pat.sub(rep, out)
    return out.strip()


class WxClient:
    """测试号/公众号 HTTP 客户端:access_token 进程内缓存,过期前 120s 刷新。"""

    def __init__(self, cfg: WechatConfig) -> None:
        self.cfg = cfg
        self._token: str = ""
        self._expire_at: float = 0.0

    async def _access_token(self) -> str:
        if self._token and time.monotonic() < self._expire_at:
            return self._token
        url = ("https://api.weixin.qq.com/cgi-bin/token"
               f"?grant_type=client_credential&appid={self.cfg.appid}"
               f"&secret={self.cfg.appsecret}")
        async with httpx.AsyncClient(timeout=10) as cli:
            data = (await cli.get(url)).json()
        if "access_token" not in data:
            raise RuntimeError(f"获取 access_token 失败:{data}")
        self._token = data["access_token"]
        self._expire_at = time.monotonic() + int(data.get("expires_in", 7200)) - 120
        return self._token

    async def push_text(self, openid: str, content: str) -> bool:
        token = await self._access_token()
        url = f"https://api.weixin.qq.com/cgi-bin/message/custom/send?access_token={token}"
        async with httpx.AsyncClient(timeout=10) as cli:
            data = (await cli.post(url, json={
                "touser": openid, "msgtype": "text",
                "text": {"content": content}})).json()
        if data.get("errcode", 0) != 0:
            # 45015=超 48h 交互窗口:静默降级,用户重新发消息即重开窗口
            logger.warning("微信客服消息下发失败 openid=%s errcode=%s",
                           openid, data.get("errcode"))
            return False
        return True


# ---------------------------------------------------------------- 图调用(与 /api/chat 同构)

def _paused_at_pay_gate(snapshot: Any) -> bool:
    """thread 停在**转账支付密码闸门**:微信通道不 resume 动钱确认(方案 §4.6)。"""
    for task in (getattr(snapshot, "tasks", None) or []):
        for intr in (getattr(task, "interrupts", None) or []):
            payload = getattr(intr, "value", None)
            if (isinstance(payload, dict)
                    and payload.get("type") == "confirm_transfer"
                    and payload.get("pay_required")):
                return True
    return False


def _final_text(result: Any) -> str:
    """从 ainvoke 终态取回复:停在 interrupt 取 question,否则取最后一条 AIMessage。"""
    intr = (result or {}).get("__interrupt__") or ()
    for item in intr:
        payload = getattr(item, "value", None)
        if isinstance(payload, dict) and payload.get("question"):
            return str(payload["question"])
    for msg in reversed((result or {}).get("messages") or []):
        if isinstance(msg, AIMessage):
            content = msg.content
            if isinstance(content, list):  # 块形态(少数 provider)
                content = "".join(c.get("text", "") for c in content
                                  if isinstance(c, dict))
            if isinstance(content, str) and content.strip():
                return content
    return ""


async def _push(app: Any, openid: str, text: str) -> None:
    """回发:测试/自测注入 app.state.wechat_pusher;生产走 WxClient 客服接口。"""
    app.state.wechat_pushes.append(text)
    pusher = getattr(app.state, "wechat_pusher", None)
    if pusher is not None:
        await pusher(openid, text)
        return
    client = getattr(app.state, "wechat_client", None)
    if client is None:
        logger.warning("未配置 WECHAT_TEST_APPSECRET,无法回发微信(openid=%s)", openid)
        return
    await client.push_text(openid, text)


async def _record_wechat_thread(app: Any, openid: str, uid: int | None,
                                first_text: str, reply: str) -> None:
    """把 wx:{openid} 会话登记进 threads 目录,让 webui 侧边栏(含手机端)
    的「微信 ClawBot」分区能列出/打开/删除微信对话。

    消息本体早已在检查点库里(每轮 relay/callback 都走同一 thread 的图执行),
    这里只补目录行——列表/历史/删除端点对 wx: 会话零改动复用。

    与 /api/chat 的 _touch_thread 的差异:
    - 不动 active_threads(不抢 webui 当前会话的跨设备跟随焦点);
    - 已存在会话不改归属(防串号),与 webui 同口径;
    - 停在澄清轮时把问题存 pending_question,/api/history 恢复现场时
      最后一条"没理人"的追问才补得上;
    - **标题跟随最新一条用户消息**(webui 是首条消息定标题):微信是
      "一人一会话",标题若钉死在第一句,每句新话都看不出列表变化,
      用户会误以为"没同步"——feed 式标题让每轮对话立即可见。
    """
    thread_id = f"wx:{openid}"
    title = first_text.strip().replace("\n", " ")
    title = title[:30] + ("…" if len(title) > 30 else "")
    paused = False
    try:
        graph = await app.state.get_user_graph(uid or 1)
        snap = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        paused = bool(snap.next)
    except Exception:  # noqa: BLE001 —— 状态读失败按非暂停处理,登记照常
        pass
    now = datetime.now().isoformat(timespec="microseconds")
    try:
        await app.state.threads.execute(
            """INSERT INTO threads (thread_id,title,created_at,updated_at,
                                   pending_question,pending_payload,user_id,
                                   revision,last_client_id)
               VALUES (?,?,?,?, '',?,?,1,'wechat')
               ON CONFLICT(thread_id) DO UPDATE SET
                 updated_at=excluded.updated_at,
                 title=excluded.title,
                 pending_question=excluded.pending_question,
                 pending_payload='',
                 revision=revision+1""",
            (thread_id, title or "微信对话", now, now,
             reply if paused else "", uid or 0))
        await app.state.threads.commit()
    except Exception:  # noqa: BLE001 —— 目录登记失败绝不影响回话本身
        logger.warning("登记微信会话目录失败 openid=%s", openid, exc_info=True)


async def _invoke_turn(app: Any, inbound: Inbound,
                       conn: sqlite3.Connection) -> str:
    """整轮图调用,返回最终回复文本(含闸门引导/错误兜底)。只取答案,不负责投递。"""
    openid = inbound.openid
    uid = lookup_uid(conn, openid)
    graph = await app.state.get_user_graph(uid or 1)
    thread_id = f"wx:{openid}"  # thread_id 为 TEXT(1-128),免建会话

    cfg = {"configurable": {"thread_id": thread_id},
           "recursion_limit": RECURSION_LIMIT}
    snapshot = await graph.aget_state(cfg)
    paused = bool(snapshot.next) and any(t.interrupts for t in snapshot.tasks)
    if paused and _paused_at_pay_gate(snapshot):
        return ("为保障资金安全,本条操作请在 BankAgent App 或网页端登录后完成,"
                "微信端不处理支付确认。")
    # 注:webui 端"闸门上新转账指令→两段式挂起并行"(api.py:1714)此处未复刻,
    # 微信端该场景退化为普通 resume(闸门会再问一次),P1 再对齐。
    graph_input: Any = (Command(resume=inbound.content) if paused else
                        {"auth_user_id": uid,
                         "messages": [HumanMessage(content=inbound.content)]})
    try:
        async with asyncio.timeout(TURN_DEADLINE_S):
            result = await graph.ainvoke(graph_input, cfg)
    except Exception:  # noqa: BLE001
        logger.exception("微信回合失败 openid=%s", openid)
        return "抱歉,刚才处理出错了,请再试一次。"
    reply = _final_text(result)
    if not reply:
        return "这条我没有理解,换个说法试试?"
    return reply


async def _run_turn(app: Any, inbound: Inbound, conn: sqlite3.Connection) -> None:
    """cs 模式投递:图调用收齐全文 → 降级/分片 → 客服接口逐条回发。"""
    openid = inbound.openid
    uid = lookup_uid(conn, openid)
    reply = await _invoke_turn(app, inbound, conn)
    await _record_wechat_thread(app, openid, uid, inbound.content, reply)
    for chunk in split_for_wechat(markdown_to_plain(reply)):
        await _push(app, openid, chunk)
    if uid is None:  # 观光模式:业务意图已被 auth_guard 拦,补一条绑定引导
        await _push(app, openid, "(未绑定账户:回复「绑定 你的手机号」即可绑定后办理业务)")


# ---------------------------------------------------------------- 回调路由

class WechatRelayIn(BaseModel):
    """本地网关(OpenClaw+ClawBot 薄壳)转发入参:openid=微信侧稳定身份。"""

    openid: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=2000)


def mount(app: Any) -> None:
    """在 create_app() 末尾挂载回调路由(注册不依赖 lifespan 状态)。

    未配置 WECHAT_CALLBACK_TOKEN 时路由仍存在但返回 503 引导——
    部署方看到 503 即知道差哪个键,而不是 404 摸不着头脑。

    回信模式 WECHAT_REPLY_MODE(默认 cs):
      cs       客服消息接口异步推送——测试号/认证订阅号/服务号用;
      passive  被动回复——个人订阅号用(无客服接口,个人主体不能认证)。
    """
    app.state.wechat_cfg = load_config()
    app.state.wechat_mode = os.environ.get("WECHAT_REPLY_MODE", "cs").strip() \
        or "cs"
    app.state.wechat_passive_wait_s = float(
        os.environ.get("WECHAT_PASSIVE_WAIT_S", str(PASSIVE_WAIT_S)))
    app.state.wechat_dedup = Deduper()
    app.state.wechat_pushes = []          # 测试/自测观察点(生产无读者)
    app.state.wechat_locks = {}
    app.state.wechat_turns = {}           # passive:msg_id → _Turn(重试挂同一回合)
    app.state.wechat_inbox = {}           # passive:openid → 待领取的回答分页

    def _xml_reply(inbound: Inbound, content: str) -> Response:
        return Response(content=ack_text_xml(inbound.account, inbound.openid,
                                             content),
                        media_type="application/xml")

    async def _passive_text(inbound: Inbound,
                            conn: sqlite3.Connection) -> Response:
        """passive 模式业务消息:3 次机会抢答(微信超时重试),抢不到转存稿箱。"""
        inbox = app.state.wechat_inbox
        turns = app.state.wechat_turns
        # 存稿箱优先:上一轮的答案先领,本条消息不再当新指令(发"1"即领取)
        pending = inbox.get(inbound.openid)
        if pending:
            page = pending.pop(0)
            if pending:
                page += "\n(还有下一条,发送 1 继续)"
            return _xml_reply(inbound, page)

        t = turns.get(inbound.msg_id)
        if t is None:
            t = _Turn(msg_id=inbound.msg_id, openid=inbound.openid,
                      event=asyncio.Event())
            turns[inbound.msg_id] = t

            async def _bg() -> None:
                text = await _invoke_turn(app, inbound, conn)
                t.pages = _paginate(markdown_to_plain(text))
                t.done = True
                # 存稿箱是唯一写入点(先入箱再唤醒,单线程事件循环保证顺序):
                # 抢答成功的请求醒来后把首页从箱里取走直接回给用户,
                # 放弃抢答(已回"正在处理")的答案留在箱里等用户发 1 领取。
                inbox.setdefault(inbound.openid, []).extend(t.pages)
                await _record_wechat_thread(app, inbound.openid,
                                            lookup_uid(conn, inbound.openid),
                                            inbound.content, text)
                t.event.set()

            asyncio.create_task(_bg())
        t.attempts += 1
        try:
            await asyncio.wait_for(t.event.wait(),
                                   timeout=app.state.wechat_passive_wait_s)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        if t.done:
            t.delivered = True
            # 首页由本次响应直接交付:从存稿箱取出,避免用户下一封"1"重复领首页
            q = inbox.get(inbound.openid) or []
            if q and q[0] == t.pages[0]:
                q.pop(0)
            more = len(t.pages) > 1
            body = t.pages[0] + ("\n(内容较长,发送 1 获取剩余)" if more else "")
            return _xml_reply(inbound, body)
        if t.attempts >= PASSIVE_MAX_ATTEMPTS:
            # 3 次都没等到:回提示消费掉本次重试链;后台完成后 _bg 入存稿箱
            t.delivered = True
            return _xml_reply(inbound, "正在处理中,稍后发送 1 获取结果。")
        return PlainTextResponse("success")  # 空 ack → 微信稍后重试(下一次机会)

    @app.get("/api/wechat/callback")
    async def wechat_verify(signature: str = "", timestamp: str = "",
                            nonce: str = "", echostr: str = "") -> Any:
        """URL 有效性验证:sha1(token,timestamp,nonce) 对上才回显 echostr。"""
        c: WechatConfig = app.state.wechat_cfg
        if not c.enabled:
            raise HTTPException(status_code=503, detail=(
                "微信回调未启用:请在 webui/.env.local 配置 WECHAT_CALLBACK_TOKEN"
                " 等键后重启"))
        if signature and signature == wx_signature(c.callback_token, timestamp, nonce):
            return PlainTextResponse(echostr)
        return PlainTextResponse("bad signature", status_code=403)

    @app.post("/api/wechat/callback")
    async def wechat_callback(req: Request) -> Response:
        c: WechatConfig = app.state.wechat_cfg
        if not c.enabled:
            raise HTTPException(status_code=503, detail="微信回调未启用")
        body = (await req.body()).decode("utf-8", "replace")
        root = ET.fromstring(body)
        encrypt_b64 = (root.findtext("Encrypt") or "").strip()
        if encrypt_b64 and c.aes_key:
            # 加密模式:先验 msg_signature 再解密取内层 XML
            msg_signature = req.query_params.get("msg_signature", "")
            expect = wx_signature(c.callback_token,
                                  req.query_params.get("timestamp", ""),
                                  req.query_params.get("nonce", ""), encrypt_b64)
            if msg_signature != expect:
                return PlainTextResponse("bad signature", status_code=403)
            inner = decrypt_message(c.aes_key, encrypt_b64, c.appid)
        else:
            inner = body
        inbound = parse_message(inner)
        if not inbound.openid:
            return PlainTextResponse("success")
        conn: sqlite3.Connection = app.state.auth_conn
        passive = app.state.wechat_mode == "passive"

        # ① 关注事件:passive 被动回欢迎语;cs 走客服接口推送
        if inbound.msg_type == "event":
            if inbound.event == "subscribe":
                if passive:
                    return _xml_reply(inbound, WELCOME_TEXT)
                await _push(app, inbound.openid, WELCOME_TEXT)
            return PlainTextResponse("success")

        # ② 绑定/解绑指令:本层直处理(不过图,确定性),两种模式都当场回
        m = BIND_RE.match(inbound.content)
        if m:
            r = bind_phone(conn, inbound.openid, m.group(1))
            text = (f"绑定成功,您好,{r['name']}。现在可以直接发消息办理业务了。"
                    if r.get("ok") else f"绑定失败:{r['error']}")
            if passive:
                return _xml_reply(inbound, text)
            await _push(app, inbound.openid, text)
            return PlainTextResponse("success")
        if UNBIND_RE.match(inbound.content):
            unbind(conn, inbound.openid)
            text = "已解绑。回复「绑定 你的手机号」可重新绑定。"
            if passive:
                return _xml_reply(inbound, text)
            await _push(app, inbound.openid, text)
            return PlainTextResponse("success")
        if inbound.msg_type != "text":
            text = "目前支持文字消息,语音/图片能力在规划中。"
            if passive:
                return _xml_reply(inbound, text)
            await _push(app, inbound.openid, text)
            return PlainTextResponse("success")

        # ③ 业务消息
        if passive:
            return await _passive_text(inbound, conn)
        # cs 模式:去重 → 立即 ack(5s 约束)→ 后台整轮 → 客服接口回发
        if not app.state.wechat_dedup.first_time(inbound.msg_id):
            return PlainTextResponse("success")
        encrypted = bool(encrypt_b64 and c.aes_key)
        ack = ("success" if encrypted else
               ack_text_xml(inbound.account, inbound.openid, ACK_TEXT))
        lock = app.state.wechat_locks.setdefault(inbound.openid, asyncio.Lock())

        async def _worker() -> None:
            async with lock:  # 同一 openid 的消息串行,避免同 thread 并发写状态
                await _run_turn(app, inbound, conn)

        asyncio.create_task(_worker())
        return Response(content=ack, media_type="application/xml")

    @app.post("/api/wechat/relay")
    async def wechat_relay(payload: WechatRelayIn, request: Request) -> dict:
        """本地网关转发入口(官方 ClawBot 插件 + OpenClaw 薄壳,方案 §3.2.1)。

        与直连 /api/chat 的区别:走 wx:{openid} 会话与微信渠道安全策略——
        openid 绑定、支付密码闸门护栏(动钱引导回 App)、观光模式全在这里生效。
        仅限本机调用:默认 loopback;配置 WECHAT_RELAY_KEY 后改凭
        X-Relay-Key 头(任何来源)。
        """
        host = (request.client.host if request.client else "") or ""
        relay_key = os.environ.get("WECHAT_RELAY_KEY", "").strip()
        if relay_key:
            if request.headers.get("x-relay-key") != relay_key:
                raise HTTPException(status_code=401, detail="bad relay key")
        elif host not in ("127.0.0.1", "::1", "localhost", "testclient"):
            raise HTTPException(status_code=403, detail="relay is loopback only")
        conn: sqlite3.Connection = app.state.auth_conn
        openid = payload.openid.strip()
        content = payload.text.strip()

        m = BIND_RE.match(content)
        if m:  # 绑定指令走渠道层直处理,与回调路由同口径
            r = bind_phone(conn, openid, m.group(1))
            reply = (f"绑定成功,您好,{r['name']}。现在可以直接发消息办理业务了。"
                     if r.get("ok") else f"绑定失败:{r['error']}")
            return {"reply": reply}
        if UNBIND_RE.match(content):
            unbind(conn, openid)
            return {"reply": "已解绑。发送「绑定 你的手机号」可重新绑定。"}

        inbound = Inbound(msg_type="text", openid=openid, content=content)
        lock = app.state.wechat_locks.setdefault(openid, asyncio.Lock())
        async with lock:  # 同一 openid 串行,避免同 thread 并发写状态
            reply = await _invoke_turn(app, inbound, conn)
        await _record_wechat_thread(app, openid, lookup_uid(conn, openid),
                                    content, reply)
        if lookup_uid(conn, openid) is None:  # 观光模式:补绑定引导
            reply += "\n(未绑定账户:发送「绑定 你的手机号」即可绑定后办理业务)"
        return {"reply": markdown_to_plain(reply)}
