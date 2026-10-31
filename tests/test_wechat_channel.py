"""微信 IM 渠道测试:验签/加解密/绑定/去重/整轮回发/闸门护栏/降级分片。

不联网:LLM 注入 FakeMessagesListChatModel(与 test_api_stream 同模式),
微信回发注入 app.state.wechat_pusher 假推送器,银行库/检查点库都在 tmp_path。
"""

from __future__ import annotations

import base64
import os
import struct
import time
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from agent import wechat as wx
from agent.api import create_app
from test_agent_graph import _mk_db

TOKEN = "tok-1234567890"
AES_KEY_43 = base64.b64encode(bytes(range(32))).decode().rstrip("=")  # 43 位形状
APPID = "wx1234567890abcdef"


def _fake_llm(*messages) -> FakeMessagesListChatModel:
    built = [m if isinstance(m, AIMessage) else AIMessage(content=m)
             for m in messages]
    return FakeMessagesListChatModel(responses=built)


def _client(tmp_path, monkeypatch, responses: list[str]):
    """建注入假模型 + 微信测试配置的 app;返回 (TestClient, db, app)。"""
    monkeypatch.setenv("WECHAT_CALLBACK_TOKEN", TOKEN)
    monkeypatch.setenv("WECHAT_TEST_APPID", APPID)
    db = _mk_db(tmp_path)
    import sqlite3 as _s
    from agent import auth as _auth
    ac = _s.connect(db)
    _auth.init_auth(ac)
    _auth.ensure_demo_auth(ac)
    ac.close()
    app = create_app(llm=_fake_llm(*responses), db_path=db,
                     checkpoint_path=tmp_path / "wx_ckpt.sqlite",
                     threads_path=tmp_path / "wx_threads.sqlite")
    sent: list[tuple[str, str]] = []

    async def fake_pusher(openid: str, text: str) -> None:
        sent.append((openid, text))

    app.state.wechat_pusher = fake_pusher
    return TestClient(app), db, app, sent


def _sign(token: str, *parts: str) -> str:
    return wx.wx_signature(token, *parts)


def _text_xml(openid: str, content: str, msg_id: str = "100001",
              account: str = "gh_demo") -> str:
    return (f"<xml><ToUserName><![CDATA[{account}]]></ToUserName>"
            f"<FromUserName><![CDATA[{openid}]]></FromUserName>"
            "<CreateTime>1760000000</CreateTime>"
            "<MsgType><![CDATA[text]]></MsgType>"
            f"<Content><![CDATA[{content}]]></Content>"
            f"<MsgId>{msg_id}</MsgId></xml>")


def _wait(pred, timeout: float = 8.0) -> bool:
    """后台回合跑在 portal 事件循环里,测试线程轮询等它完成。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _post_xml(client: TestClient, xml: str, **params):
    return client.post("/api/wechat/callback", content=xml.encode("utf-8"),
                       headers={"Content-Type": "application/xml"}, params=params)


# ----------------------------------------------------------------- 验签与启用开关

def test_get_verify_roundtrip(tmp_path, monkeypatch):
    client, _db, _app, _sent = _client(tmp_path, monkeypatch,
                                       ['{"intent": "chat"}', "hi"])
    with client:
        qs = {"timestamp": "1760000000", "nonce": "abc", "echostr": "ECHO-42"}
        good = dict(qs, signature=_sign(TOKEN, qs["timestamp"], qs["nonce"]))
        r = client.get("/api/wechat/callback", params=good)
        assert r.status_code == 200 and r.text == "ECHO-42"

        bad = dict(qs, signature=_sign("wrong-token", qs["timestamp"], qs["nonce"]))
        assert client.get("/api/wechat/callback", params=bad).status_code == 403


def test_disabled_without_token_returns_503(tmp_path, monkeypatch):
    monkeypatch.delenv("WECHAT_CALLBACK_TOKEN", raising=False)
    monkeypatch.setenv("WECHAT_TEST_APPID", APPID)
    db = _mk_db(tmp_path)
    app = create_app(llm=_fake_llm("x"), db_path=db,
                     checkpoint_path=tmp_path / "c.sqlite",
                     threads_path=tmp_path / "t.sqlite")
    with TestClient(app) as client:
        assert client.get("/api/wechat/callback").status_code == 503
        assert _post_xml(client, _text_xml("oA1", "hi")).status_code == 503


# ----------------------------------------------------------------- 绑定

def test_bind_unbind_flow(tmp_path, monkeypatch):
    client, db, _app, sent = _client(tmp_path, monkeypatch,
                                     ['{"intent": "chat"}', "hi"])
    with client:
        # 未注册手机号 → 拒绝,零落库
        r = _post_xml(client, _text_xml("oA1", "绑定 19999999999"))
        assert r.status_code == 200
        assert _wait(lambda: sent and "绑定失败" in sent[0][1])

        # 已注册手机号(测试库 user1)→ 绑定成功
        sent.clear()
        _post_xml(client, _text_xml("oA1", "绑定 13800000000"))
        assert _wait(lambda: sent and "绑定成功" in sent[0][1])
        import sqlite3 as _s
        row = _s.connect(db).execute(
            "SELECT user_id, status FROM wechat_bind WHERE openid='oA1'").fetchone()
        assert row == (1, "bound")

        # 解绑 → status=disabled,查询为观光
        sent.clear()
        _post_xml(client, _text_xml("oA1", "解绑"))
        assert _wait(lambda: sent and "已解绑" in sent[0][1])
        assert wx.lookup_uid(_s.connect(db), "oA1") is None


def test_subscribe_sends_welcome(tmp_path, monkeypatch):
    client, _db, _app, sent = _client(tmp_path, monkeypatch,
                                      ['{"intent": "chat"}', "hi"])
    with client:
        xml = ("<xml><ToUserName><![CDATA[gh_demo]]></ToUserName>"
               "<FromUserName><![CDATA[oA2]]></FromUserName>"
               "<CreateTime>1760000000</CreateTime>"
               "<MsgType><![CDATA[event]]></MsgType>"
               "<Event><![CDATA[subscribe]]></Event></xml>")
        assert _post_xml(client, xml).status_code == 200
        assert _wait(lambda: sent and "BankAgent" in sent[0][1])


def test_unsupported_msg_type(tmp_path, monkeypatch):
    client, _db, _app, sent = _client(tmp_path, monkeypatch,
                                      ['{"intent": "chat"}', "hi"])
    with client:
        xml = _text_xml("oA3", "").replace(
            "<MsgType><![CDATA[text]]></MsgType>",
            "<MsgType><![CDATA[image]]></MsgType>").replace(
            "<Content><![CDATA[]]></Content>", "<PicUrl><![CDATA[x]]></PicUrl>")
        assert _post_xml(client, xml).status_code == 200
        assert _wait(lambda: sent and "文字" in sent[0][1])


# ----------------------------------------------------------------- 整轮:ack/去重/回发

def test_text_message_full_turn_unbound(tmp_path, monkeypatch):
    client, _db, _app, sent = _client(tmp_path, monkeypatch,
                                      ['{"intent": "chat"}', "你好呀"])
    with client:
        r = _post_xml(client, _text_xml("oA4", "你好呀"))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/xml")
        assert "正在处理" in r.text  # 被动回复 ack(5s 约束)

        # 后台整轮:答案 + 未绑定提示
        assert _wait(lambda: len(sent) >= 2), f"sent={sent}"
        assert "你好呀" in sent[0][1]
        assert "绑定" in sent[-1][1]


def test_msgid_dedup_single_turn(tmp_path, monkeypatch):
    client, _db, _app, sent = _client(tmp_path, monkeypatch,
                                      ['{"intent": "chat"}', "在吗"])
    with client:
        _post_xml(client, _text_xml("oA5", "在吗", msg_id="777"))
        _post_xml(client, _text_xml("oA5", "在吗", msg_id="777"))  # 微信重试同 MsgId
        assert _wait(lambda: len(sent) >= 1)
        time.sleep(0.3)  # 给重试报文"如果没被去重"留窗口
        # 未绑定一轮=答案+绑定提示两条;答案只许出现一次(重试被去重的证据)
        answers = [t for _, t in sent].count("在吗")
        assert answers == 1, f"重试未被去重:sent={sent}"


# ----------------------------------------------------------------- 加密模式

def _encrypt_xml(aes_key_43: str, inner_xml: str, appid: str) -> str:
    """测试侧实现官方加密(与 agent.wechat.decrypt_message 互为逆运算)。"""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    key = base64.b64decode(aes_key_43 + "=")
    raw = inner_xml.encode("utf-8")
    msg = (os.urandom(16) + struct.pack(">I", len(raw)) + raw
           + appid.encode("utf-8"))
    pad = 32 - len(msg) % 32
    msg += bytes([pad]) * pad
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    enc = cipher.encryptor()
    payload = base64.b64encode(enc.update(msg) + enc.finalize()).decode()
    return (f"<xml><ToUserName><![CDATA[gh_demo]]></ToUserName>"
            f"<Encrypt><![CDATA[{payload}]]></Encrypt></xml>")


def test_encrypted_message_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("WECHAT_AES_KEY", AES_KEY_43)
    client, _db, _app, sent = _client(tmp_path, monkeypatch,
                                      ['{"intent": "chat"}', "加密你好"])
    with client:
        xml = _encrypt_xml(AES_KEY_43, _text_xml("oA6", "加密你好"), APPID)
        qs = {"timestamp": "1760000000", "nonce": "n1"}
        # 错签名 → 403
        bad = _post_xml(client, xml,
                        **dict(qs, msg_signature=_sign("wrong", "x", "y", "z")))
        assert bad.status_code == 403
        # 对签名 → 解密处理
        good = _post_xml(client, xml, **dict(
            qs, msg_signature=_sign(TOKEN, qs["timestamp"], qs["nonce"],
                                    ET.fromstring(xml).findtext("Encrypt"))))
        assert good.status_code == 200 and good.text == "success"  # 加密模式不被动回复
        assert _wait(lambda: sent and "加密你好" in sent[0][1]), f"sent={sent}"


# ----------------------------------------------------------------- 纯函数:闸门护栏/分片/降级

def test_paused_at_pay_gate_detector():
    def snap(payload: dict) -> SimpleNamespace:
        return SimpleNamespace(next=("n",), tasks=[SimpleNamespace(
            interrupts=[SimpleNamespace(value=payload)])])

    assert wx._paused_at_pay_gate(snap({"type": "confirm_transfer",
                                        "pay_required": True}))
    assert not wx._paused_at_pay_gate(snap({"type": "confirm_transfer",
                                            "pay_required": False}))  # 普通确认闸
    assert not wx._paused_at_pay_gate(snap({"type": "other"}))


def test_split_for_wechat_respects_bytes():
    text = "a" * 500 + "汉" * 1000  # 混合中英,汉=3 字节
    chunks = wx.split_for_wechat(text, max_bytes=1800)
    assert all(len(c.encode("utf-8")) <= 1800 for c in chunks)
    assert "".join(chunks) == text
    assert wx.split_for_wechat("") == [""]


def test_markdown_to_plain():
    md = "## 余额\n**工资卡**:1,000.00 元\n| 日期 | 金额 |\n|---|---|\n| 10-01 | 100 |"
    out = wx.markdown_to_plain(md)
    assert "#" not in out and "**" not in out and "|---|" not in out
    assert "工资卡" in out and "10-01" in out


# ----------------------------------------------------------------- passive 模式(个人订阅号)
# 个人订阅号无客服消息接口(个人主体不能认证,48001),只能被动回复:
# 5 秒内 HTTP 响应回 XML,微信超时重试共 3 次机会 → 挂等抢答;抢不到转存稿箱。

class _SlowFake(FakeMessagesListChatModel):
    """带人为延迟的假模型:模拟 LLM 慢于 5 秒(真实 DeepSeek 常态)。"""

    delay: float = 0.0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        time.sleep(self.delay)
        return super()._generate(messages, stop=stop, run_manager=run_manager,
                                 **kwargs)


def _client_passive(tmp_path, monkeypatch, responses: list[str],
                    delay: float = 0.0, wait: float = 0.3):
    """passive 模式 app:WECHAT_REPLY_MODE=passive + 可调抢答窗口。"""
    monkeypatch.setenv("WECHAT_CALLBACK_TOKEN", TOKEN)
    monkeypatch.setenv("WECHAT_TEST_APPID", APPID)
    monkeypatch.setenv("WECHAT_REPLY_MODE", "passive")
    monkeypatch.setenv("WECHAT_PASSIVE_WAIT_S", str(wait))
    db = _mk_db(tmp_path)
    import sqlite3 as _s
    from agent import auth as _auth
    ac = _s.connect(db)
    _auth.init_auth(ac)
    _auth.ensure_demo_auth(ac)
    ac.close()
    app = create_app(llm=_SlowFake(responses=[
        m if isinstance(m, AIMessage) else AIMessage(content=m)
        for m in responses], delay=delay), db_path=db,
        checkpoint_path=tmp_path / "wxp_ckpt.sqlite",
        threads_path=tmp_path / "wxp_threads.sqlite")
    return TestClient(app), db, app


def test_passive_fast_answer_within_window(tmp_path, monkeypatch):
    """答案在抢答窗口内出来 → 首个请求直接被动回复 XML(微信不重试)。"""
    client, _db, _app = _client_passive(
        tmp_path, monkeypatch, ['{"intent": "chat"}', "余额是1万元"], delay=0.02)
    with client:
        r = _post_xml(client, _text_xml("oP1", "查余额", msg_id="P1"))
        assert r.status_code == 200
        assert "余额是1万元" in r.text and "<MsgType><![CDATA[text]]>" in r.text


def test_passive_slow_answer_gives_up_then_fetch(tmp_path, monkeypatch):
    """答案慢于 3 次抢答 → 第 3 次『正在处理』,完成后发 1 从存稿箱领取。"""
    client, _db, app = _client_passive(
        tmp_path, monkeypatch, ['{"intent": "chat"}', "慢答案来了"],
        delay=0.6, wait=0.25)
    with client:
        # 3 次重试链(同 MsgId):前两次空 ack(触发微信重试),第 3 次放弃
        r1 = _post_xml(client, _text_xml("oP2", "查账单", msg_id="P2"))
        assert r1.text == "success"
        r2 = _post_xml(client, _text_xml("oP2", "查账单", msg_id="P2"))
        assert r2.text == "success"
        r3 = _post_xml(client, _text_xml("oP2", "查账单", msg_id="P2"))
        assert "正在处理" in r3.text
        # 后台完成后答案进存稿箱 → 新消息(如"1")领取
        assert _wait(lambda: app.state.wechat_inbox.get("oP2"))
        r4 = _post_xml(client, _text_xml("oP2", "1", msg_id="P3"))
        assert "慢答案来了" in r4.text


def test_passive_long_answer_paginated(tmp_path, monkeypatch):
    """超长回答分页(550 字/页):首页带提示,发 1 领下一页。"""
    long_answer = "账" * 1200
    client, _db, _app = _client_passive(
        tmp_path, monkeypatch, ['{"intent": "chat"}', long_answer], delay=0.02)
    with client:
        r1 = _post_xml(client, _text_xml("oP4", "年报", msg_id="P4"))
        assert "发送 1 获取剩余" in r1.text
        r2 = _post_xml(client, _text_xml("oP4", "1", msg_id="P5"))
        assert "还有下一条" in r2.text
        r3 = _post_xml(client, _text_xml("oP4", "1", msg_id="P6"))
        assert "账" in r3.text and "还有下一条" not in r3.text  # 最后一页


def test_passive_subscribe_and_bind_sync_reply(tmp_path, monkeypatch):
    """passive 模式:关注欢迎语与绑定指令都在 HTTP 响应里直接回(不过图)。"""
    client, db, _app = _client_passive(
        tmp_path, monkeypatch, ['{"intent": "chat"}', "x"], delay=0.0)
    with client:
        sub = ("<xml><ToUserName><![CDATA[gh_demo]]></ToUserName>"
               "<FromUserName><![CDATA[oP5]]></FromUserName>"
               "<CreateTime>1760000000</CreateTime>"
               "<MsgType><![CDATA[event]]></MsgType>"
               "<Event><![CDATA[subscribe]]></Event></xml>")
        r = _post_xml(client, sub)
        assert "BankAgent" in r.text  # 被动回复 XML,非 success
        r2 = _post_xml(client, _text_xml("oP5", "绑定 13800000000", msg_id="P7"))
        assert "绑定成功" in r2.text
        import sqlite3 as _s
        row = _s.connect(db).execute(
            "SELECT user_id, status FROM wechat_bind WHERE openid='oP5'").fetchone()
        assert row == (1, "bound")


def test_passive_mode_env_default_is_cs(tmp_path, monkeypatch):
    """不配 WECHAT_REPLY_MODE → 默认 cs(现有行为不变)。"""
    monkeypatch.delenv("WECHAT_REPLY_MODE", raising=False)
    monkeypatch.setenv("WECHAT_CALLBACK_TOKEN", TOKEN)
    from agent.api import create_app as _ca
    from agent import wechat as _wx
    app = _ca(llm=_fake_llm("x"), db_path=_mk_db(tmp_path),
              checkpoint_path=tmp_path / "m.sqlite",
              threads_path=tmp_path / "t.sqlite")
    assert app.state.wechat_mode == "cs"
    assert _wx.PASSIVE_MAX_ATTEMPTS == 3


# ----------------------------------------------------------------- relay(OpenClaw/ClawBot 薄壳)

def test_relay_bind_then_chat(tmp_path, monkeypatch):
    """本地转发入口:绑定指令直处理;业务消息复用渠道整轮(同 fake 模型)。"""
    client, _db, _app, _sent = _client(tmp_path, monkeypatch,
                                       ['{"intent": "chat"}', "relay回复"])
    with client:
        r = client.post("/api/wechat/relay",
                        json={"openid": "oR1", "text": "绑定 13800000000"})
        assert r.status_code == 200 and r.json()["reply"].startswith("绑定成功")
        r2 = client.post("/api/wechat/relay",
                         json={"openid": "oR1", "text": "你好"})
        assert "relay回复" in r2.json()["reply"]
        assert "未绑定账户" not in r2.json()["reply"]  # 已绑定不附引导


def test_relay_unbound_appends_hint(tmp_path, monkeypatch):
    client, _db, _app, _sent = _client(tmp_path, monkeypatch,
                                       ['{"intent": "chat"}', "观光回复"])
    with client:
        r = client.post("/api/wechat/relay",
                        json={"openid": "oR2", "text": "你好"})
        assert "观光回复" in r.json()["reply"]
        assert "未绑定账户" in r.json()["reply"]


def test_relay_key_guard(tmp_path, monkeypatch):
    """配置 WECHAT_RELAY_KEY 后凭 X-Relay-Key 访问;错钥 401。"""
    monkeypatch.setenv("WECHAT_RELAY_KEY", "sekret-1")
    client, _db, _app, _sent = _client(tmp_path, monkeypatch,
                                       ['{"intent": "chat"}', "x"])
    with client:
        r = client.post("/api/wechat/relay",
                        json={"openid": "oR3", "text": "hi"})
        assert r.status_code == 401
        r2 = client.post("/api/wechat/relay",
                         json={"openid": "oR3", "text": "绑定 13800000000"},
                         headers={"X-Relay-Key": "sekret-1"})
        assert r2.status_code == 200


# ----------------------------------------------------------------- 会话目录(wx: 线程进侧边栏)

def test_relay_registers_thread_with_owner(tmp_path, monkeypatch):
    """relay 回合后:wx:{openid} 进 threads 目录,归属=绑定用户,标题取首条消息。

    这是「侧边栏微信 ClawBot 分区」的后端契约:列表/历史/删除端点对 wx:
    会话零改动复用,靠的就是这行目录登记。
    """
    # 绑定指令在渠道层直处理不过图,只消耗一次图调用(路由+回复)
    client, _db, app, _sent = _client(tmp_path, monkeypatch,
                                      ['{"intent": "chat"}', "余额是1万元"])
    with client:
        client.post("/api/wechat/relay",
                    json={"openid": "oR9", "text": "绑定 13800000000"})
        r = client.post("/api/wechat/relay",
                        json={"openid": "oR9", "text": "查一下我的余额"})
        assert "余额" in r.json()["reply"]
        import sqlite3 as _s
        row = _s.connect(tmp_path / "wx_threads.sqlite").execute(
            "SELECT title, user_id, last_client_id FROM threads "
            "WHERE thread_id='wx:oR9'").fetchone()
        assert row[0].startswith("查一下我的余额")  # 标题=首条消息(同 webui 口径)
        assert row[1] == 1                          # 归属=绑定的演示用户
        assert row[2] == "wechat"
        # 归属校验:未登录(history 只许看观光 0)看不到别人的微信会话
        h = client.get("/api/history", params={"thread_id": "wx:oR9"})
        assert h.json()["messages"] == []


def test_cs_turn_registers_thread_unbound_owner_zero(tmp_path, monkeypatch):
    """未绑定(cs 回调)回合:目录行归属 0(观光),无 token 的 history 可见。"""
    client, _db, _app, sent = _client(tmp_path, monkeypatch,
                                      ['{"intent": "chat"}', "观光回复内容"])
    with client:
        _post_xml(client, _text_xml("oA9", "你好呀", msg_id="A9"))
        assert _wait(lambda: sent and "观光回复内容" in sent[0][1])
        import sqlite3 as _s
        row = _s.connect(tmp_path / "wx_threads.sqlite").execute(
            "SELECT user_id FROM threads WHERE thread_id='wx:oA9'").fetchone()
        assert row[0] == 0  # 未绑定 → 观光归属,侧边栏未登录态可见
        h = client.get("/api/history", params={"thread_id": "wx:oA9"})
        texts = [p["text"] for m in h.json()["messages"]
                 for p in m["parts"] if p.get("type") == "text"]
        assert any("你好呀" in t for t in texts)
