"""API 流式端点测试:TestClient 断言 SSE 协议、确认卡片字段、完整确认/取消 HTTP 往返。

不联网:注入 FakeMessagesListChatModel;银行库/检查点库都在 tmp_path。
临时库与断言助手复用 tests/test_agent_graph.py(同目录,pytest prepend 导入模式可直接 import)。
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from agent.api import STREAM_HEADER, STREAM_HEADER_VALUE, create_app
from bank_core.money import cents_to_yuan
from test_agent_graph import DEFAULT_BALANCE, _balance, _mk_db, _order


def _fake_llm(*messages) -> FakeMessagesListChatModel:
    """构造注入用假模型。

    每个参数可以是 str(自动包成 AIMessage)或现成的 AIMessage
    (可带 additional_kwargs 以测试 reasoning_content 等扩展字段)。
    """
    from langchain_core.messages import AIMessage as _AIMessage
    built = [m if isinstance(m, _AIMessage) else _AIMessage(content=m) for m in messages]
    return FakeMessagesListChatModel(responses=built)


def _client(tmp_path: Path, responses: list[str]) -> tuple[TestClient, Path]:
    """建一个注入假模型的 app + TestClient(须用 with 触发 lifespan 装载图)。

    同时返回临时银行库路径——每个测试只初始化一次库,重复 init 会撞 users.id 唯一约束。
    """
    db = _mk_db(tmp_path)
    # 登录态:注册测试用户拿 token(业务意图须登录;动钱闸需支付密码 888888)
    import sqlite3 as _s
    from agent import auth as _auth
    ac = _s.connect(db)
    _auth.init_auth(ac)
    _auth.ensure_demo_auth(ac)  # 陈明(登录 Demo@12345 / 支付 888888)
    ac.close()
    app = create_app(llm=_fake_llm(*responses), db_path=db,
                     checkpoint_path=tmp_path / "api_ckpt.sqlite",
                     threads_path=tmp_path / "api_threads.sqlite")
    with TestClient(app) as probe:  # lifespan 起来才能登录(auth_conn)
        tok = probe.post("/api/auth/login",
                         json={"identifier": "13800000000",
                               "password": "Demo@12345"}).json()["token"]
    return TestClient(app), db, tok


def _frames(body: str) -> list[dict]:
    out: list[dict] = []
    for block in body.strip().split("\n\n"):
        if block.startswith("data: "):
            out.append(json.loads(block[len("data: "):]))
    return out


def _post_chat(client: TestClient, text: str, thread: str,
               token: str | None = None):
    return client.post("/api/chat",
                       json={"messages": [{"role": "user", "content": text}],
                             "thread_id": thread, "token": token})


def _joined_text(frames: list[dict]) -> str:
    return "".join(f.get("delta", "") for f in frames if f["type"] == "text-delta")


def _find_data(frames: list[dict], part_type: str) -> dict | None:
    return next((f for f in frames if f["type"] == part_type), None)


# ----------------------------------------------------------------- 健康检查与入参校验

def test_health_and_validation(tmp_path):
    client, _db, _tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])
    with client:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["bank_tools"] == 45  # +1 annual_report(2026-10-26 年度账单报告)
        assert "model" in body

        # 没有 user 消息 → 400,绝不进入流式
        r2 = client.post("/api/chat",
                         json={"messages": [{"role": "assistant", "content": "x"}],
                               "thread_id": "bad"})
        assert r2.status_code == 400


def test_accounts_and_deposit_endpoint(tmp_path):
    """新用户自助入金:登录后可查账户;入金产生余额,幂等键防重复。"""
    client, _db, tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])
    headers = {"x-bank-token": tok}
    with client:
        accounts = client.get("/api/accounts", headers=headers)
        assert accounts.status_code == 200
        account = accounts.json()["accounts"][0]
        before = account["balance_cents"]

        payload = {
            "account_id": account["id"],
            "amount_yuan": "100",
            "note": "测试入金",
            "idempotency_key": "api-dep-1",
        }
        r = client.post("/api/deposit", headers=headers, json=payload)
        assert r.status_code == 200
        result = r.json()
        assert result["duplicate"] is False
        assert result["balance_after_yuan"] == cents_to_yuan(before + 10000)

        # 同一个幂等键重放:余额不再增加
        again = client.post("/api/deposit", headers=headers, json=payload)
        assert again.status_code == 200
        assert again.json()["duplicate"] is True
        now = client.get("/api/accounts", headers=headers).json()["accounts"][0]
        assert now["balance_cents"] == before + 10000

        # 未登录不可入金;单笔上限
        assert client.post("/api/deposit", json=payload).status_code == 401
        over = {**payload, "amount_yuan": "100001",
                "idempotency_key": "api-dep-over"}
        assert client.post("/api/deposit", headers=headers,
                           json=over).status_code == 400


def test_bound_card_endpoints(tmp_path):
    """演示绑卡 → 从虚拟卡余额充钱 → 解绑;不接真实银行卡。"""
    client, _db, tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])
    headers = {"x-bank-token": tok}
    with client:
        created = client.post("/api/bound-cards", headers=headers, json={
            "bank_name": "招商银行",
            "card_no": "6225 8800 1234 5678",
            "holder_name": "陈明",
        })
        assert created.status_code == 200
        card = created.json()
        assert card["card_no_masked"] == "**** **** **** 5678"
        assert card["balance_yuan"] == "50000.00"

        listed = client.get("/api/bound-cards", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["cards"][0]["card_id"] == card["card_id"]

        account = client.get("/api/accounts", headers=headers
                             ).json()["accounts"][0]
        before = account["balance_cents"]
        r = client.post("/api/deposit", headers=headers, json={
            "account_id": account["id"],
            "amount_yuan": "100",
            "card_id": card["card_id"],
            "note": "从卡转入",
            "idempotency_key": "api-card-dep-1",
        })
        assert r.status_code == 200
        result = r.json()
        assert result["source"] == "card"
        assert result["balance_after_yuan"] == cents_to_yuan(before + 10000)
        assert result["card_balance_after_yuan"] == "49900.00"

        again = client.post("/api/deposit", headers=headers, json={
            "account_id": account["id"],
            "amount_yuan": "100",
            "card_id": card["card_id"],
            "idempotency_key": "api-card-dep-1",
        })
        assert again.json()["duplicate"] is True
        after = client.get("/api/accounts", headers=headers
                           ).json()["accounts"][0]
        assert after["balance_cents"] == before + 10000

        assert client.delete(
            f"/api/bound-cards/{card['card_id']}",
            headers=headers).status_code == 200
        assert client.get("/api/bound-cards", headers=headers
                          ).json()["cards"] == []


# ----------------------------------------------------------------- SSE 基本协议

def test_chat_stream_sse_protocol(tmp_path):
    client, _db, _tok = _client(tmp_path, [
        '{"intent": "chat"}',
        "你好,我是练功假银行助手,目前支持转账和 AA 收款。",
    ])
    with client:
        r = _post_chat(client, "你好呀", "api-chat")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.headers[STREAM_HEADER] == STREAM_HEADER_VALUE  # 官方自定义后端要求的头

        frames = _frames(r.text)
        assert frames[0]["type"] == "start"
        assert frames[-1] == {"type": "finish", "finishReason": "stop"}

        # 文本帧三段式:id 一致、增量可拼回原文
        starts = [f for f in frames if f["type"] == "text-start"]
        deltas = [f for f in frames if f["type"] == "text-delta"]
        ends = [f for f in frames if f["type"] == "text-end"]
        assert len(starts) == len(ends) == 1
        assert len(deltas) >= 1
        assert starts[0]["id"] == deltas[0]["id"] == ends[0]["id"]
        assert "转账" in _joined_text(frames)

        # 闲聊兜底不该有任何确认卡片
        assert _find_data(frames, "data-transfer-confirmation") is None
        # 非推理假模型:不应出现 reasoning 帧(优雅降级,默认折叠面板不会出现)
        assert not any(f["type"].startswith("reasoning-") for f in frames)


# ----------------------------------------------------------------- 深度思考(reasoning_content)流式
def test_reasoning_model_emits_reasoning_frames(tmp_path):
    from langchain_core.messages import AIMessage

    # 第二个响应带 reasoning_content:模拟 DeepSeek / 智谱 GLM thinking 的原生推理
    reasoning_ai = AIMessage(
        content="好的,我来帮您查询余额。",
        additional_kwargs={"reasoning_content": "先判断意图:用户问余额,属查询类,直接作答即可。"},
    )
    client, _db, _tok = _client(tmp_path, [
        '{"intent": "chat"}',
        reasoning_ai,
    ])
    with client:
        r = _post_chat(client, "我的余额多少", "api-chat-reason")
        assert r.status_code == 200
        frames = _frames(r.text)
        starts = [f for f in frames if f["type"] == "reasoning-start"]
        deltas = [f for f in frames if f["type"] == "reasoning-delta"]
        ends = [f for f in frames if f["type"] == "reasoning-end"]
        # 成对的 reasoning 帧
        assert len(starts) == len(ends) >= 1, frames
        assert len(deltas) >= 1
        rid = starts[0]["id"]
        assert all(d["id"] == rid for d in deltas), "reasoning delta 的 id 应一致"
        assert all(e["id"] == rid for e in ends), "reasoning end 的 id 应一致"
        joined = "".join(d.get("delta", "") for d in deltas)
        assert "查询类" in joined, "模型原生推理文本应被捕获并下发"
        # 正文照常流式
        assert any(f["type"] == "text-delta" for f in frames)


def test_non_reasoning_model_has_no_reasoning_frames(tmp_path):
    client, _db, _tok = _client(tmp_path, [
        '{"intent": "chat"}',
        "你好,我是练功假银行助手。",
    ])
    with client:
        r = _post_chat(client, "你好呀", "api-chat-noreason")
        assert r.status_code == 200
        frames = _frames(r.text)
        # 普通模型:无推理帧,仅正文流式(优雅降级)
        assert not any(f["type"].startswith("reasoning-") for f in frames), \
            f"非推理模型不应出现 reasoning 帧: {[f['type'] for f in frames]}"
        assert any(f["type"] == "text-delta" for f in frames)


# ----------------------------------------------------------------- 建单 → 中断帧 → 确认 → 执行完成

def test_transfer_card_then_confirm_over_http(tmp_path):
    client, db, _tok = _client(tmp_path, [
        '{"intent": "transfer"}',
        '{"payee": "张三", "amount_yuan": "500", "when": "now"}',
        "已向张三转账 500.00 元。",
    ])
    with client:
        # 第 1 条:建单 → 中断在人工闸门,下发确认卡片
        r1 = _post_chat(client, "给张三转 500 元", "api-t1", token=_tok)
        assert r1.status_code == 200
        frames1 = _frames(r1.text)
        card = _find_data(frames1, "data-transfer-confirmation")
        assert card is not None, f"缺少确认卡片,帧类型: {[f['type'] for f in frames1]}"
        data = card["data"]
        # 任务书要求的字段:order_id / 金额 / 收款人 / 限额说明
        assert isinstance(data["order_id"], int)
        assert data["amount_yuan"] == "500.00"
        assert data["to_name"] == "张三"
        assert "限额" in data["policy_note"]
        assert data["policy"]["single_tx_limit_cents"] == "50000元"  # bank_core get_policy 原文
        assert "支付密码" in data["confirm_hint"]  # 动钱闸改支付密码确认
        assert data["pay_required"] is True

        order_id = data["order_id"]
        pending = _find_data(frames1, "data-gate-pending")
        assert pending is not None and pending["data"]["gate_type"] == "confirm_transfer"
        assert frames1[-1] == {"type": "finish", "finishReason": "stop"}
        # 中断时:订单已建但未动钱(整数分断言)
        assert _order(db, order_id)["status"] == "pending_confirm"
        assert _balance(db) == DEFAULT_BALANCE

        # 第 2 条:同 thread 携带「确认」→ 恢复 interrupt 继续执行
        r2 = _post_chat(client, "888888", "api-t1", token=_tok)
        assert r2.status_code == 200
        frames2 = _frames(r2.text)
        assert _find_data(frames2, "data-transfer-confirmation") is None  # 不再发卡
        assert _find_data(frames2, "data-gate-pending") is None
        assert "已向张三" in _joined_text(frames2)
        assert frames2[-1] == {"type": "finish", "finishReason": "stop"}

        assert _balance(db) == DEFAULT_BALANCE - 50_000  # 恰好扣 500 元
        assert _order(db, order_id)["status"] == "executed"


# ----------------------------------------------------------------- 建单 → 中断帧 → 取消

def test_transfer_cancel_over_http(tmp_path):
    client, db, _tok = _client(tmp_path, [
        '{"intent": "transfer"}',
        '{"payee": "张三", "amount_yuan": "88.5", "when": "now"}',
        "好的,已取消这笔转账。",
    ])
    with client:
        r1 = _post_chat(client, "转 88.5 元给张三", "api-t2", token=_tok)
        card = _find_data(_frames(r1.text), "data-transfer-confirmation")
        assert card is not None
        order_id = card["data"]["order_id"]

        r2 = _post_chat(client, "取消", "api-t2", token=_tok)
        assert "取消" in _joined_text(_frames(r2.text))
        assert _order(db, order_id)["status"] == "cancelled"
        assert _balance(db) == DEFAULT_BALANCE  # 一分没动


# ----------------------------------------------------------------- 会话持久化(刷新恢复)

def test_history_and_threads(tmp_path):
    """聊天两轮 → /api/history 恢复完整消息;/api/threads 出目录(标题+倒序)。"""
    client, _db, _tok = _client(
        tmp_path,
        ['{"intent": "chat"}', "你好呀",
         '{"intent": "chat"}', "第二句回复"])
    with client:
        _post_chat(client, "你好", "t-hist", token=_tok)
        _post_chat(client, "再说点什么", "t-hist", token=_tok)
        _post_chat(client, "另一个会话", "t-other", token=_tok)

        r = client.get("/api/history", params={"thread_id": "t-hist"},
                        headers={"x-bank-token": _tok})
        assert r.status_code == 200
        msgs = r.json()["messages"]
        roles = [m["role"] for m in msgs]
        texts = [m["parts"][0]["text"] for m in msgs]
        assert roles == ["user", "assistant", "user", "assistant"]
        assert texts[0] == "你好" and texts[2] == "再说点什么"
        assert texts[1] == "你好呀" and texts[3] == "第二句回复"

        # 未知 thread → 空历史(不报错,前端按新会话处理)
        r2 = client.get("/api/history", params={"thread_id": "no-such"},
                         headers={"x-bank-token": _tok})
        assert r2.status_code == 200 and r2.json()["messages"] == []

        r3 = client.get("/api/threads", headers={"x-bank-token": _tok})
        assert r3.status_code == 200
        body = r3.json()
        assert body["hasMore"] is False
        titles = [c["title"] for c in body["chats"]]
        ids = [c["id"] for c in body["chats"]]
        assert set(ids) == {"t-hist", "t-other"}
        assert "你好" in titles and "另一个会话" in titles
        # 倒序:最后活跃的 t-other 在最前
        assert ids[0] == "t-other"

        # offset 分页(手机端会话总览滚动加载):limit=1 时 hasMore=True,
        # 第二页取到另一条且 hasMore=False,两页合起来覆盖全部会话。
        r4 = client.get("/api/threads", params={"limit": 1},
                        headers={"x-bank-token": _tok}).json()
        assert len(r4["chats"]) == 1 and r4["hasMore"] is True
        r5 = client.get("/api/threads", params={"limit": 1, "offset": 1},
                        headers={"x-bank-token": _tok}).json()
        assert len(r5["chats"]) == 1 and r5["hasMore"] is False
        assert {r4["chats"][0]["id"], r5["chats"][0]["id"]} == {
            "t-hist", "t-other"}


def test_thread_title_truncation_and_touch(tmp_path):
    """标题取首条用户消息并截断;后续消息只刷新时间不改标题。"""
    client, _db, _tok = _client(tmp_path, ['{"intent": "chat"}', "ok",
                                     '{"intent": "chat"}', "ok"])
    with client:
        long_text = "这是一条特别长的开场白" * 5  # 55 字
        _post_chat(client, long_text, "t-title", token=_tok)
        r = client.get("/api/threads", headers={"x-bank-token": _tok})
        title = r.json()["chats"][0]["title"]
        assert title.startswith("这是一条特别长的开场白") and title.endswith("…")
        assert len(title) <= 31
        _post_chat(client, "第二条消息", "t-title", token=_tok)
        r2 = client.get("/api/threads", headers={"x-bank-token": _tok})
        assert r2.json()["chats"][0]["title"] == title  # 标题不变


def test_history_shows_pending_gate_question(tmp_path):
    """停在闸门时刷新:恢复的历史里能看到待答问题;resume 后问题不重复。"""
    client, db, _tok = _client(tmp_path, [
        '{"intent": "transfer"}',
        '{"payee": "张三", "amount_yuan": "500", "when": "now"}',
        "已向张三转账 500.00 元。",
    ])
    with client:
        _post_chat(client, "给张三转 500 元", "api-gq", token=_tok)
        msgs = client.get("/api/history",
                          params={"thread_id": "api-gq"},
                           headers={"x-bank-token": _tok}).json()["messages"]
        texts = [m["parts"][0]["text"] for m in msgs]
        assert texts[0] == "给张三转 500 元"
        assert texts[-1].startswith("请确认转账")  # 闸门问题可见,不再是"没理人"
        # 闸门卡片随历史恢复(跨设备/刷新都能看到转账确认卡,不只是文本)
        pending_parts = [p for m in msgs for p in m["parts"]]
        card = next((p for p in pending_parts
                     if p.get("type") == "data-transfer-confirmation"), None)
        assert card is not None
        assert card["data"]["amount_yuan"] == "500.00"
        assert card["data"]["pay_required"] is True

        _post_chat(client, "888888", "api-gq", token=_tok)
        msgs2 = client.get("/api/history",
                           params={"thread_id": "api-gq"},
                           headers={"x-bank-token": _tok}).json()["messages"]
        texts2 = [m["parts"][0]["text"] for m in msgs2]
        assert any("已向张三" in t for t in texts2)
        # 闸门已处理:历史里不再残留旧确认卡
        assert not any(p.get("type") == "data-transfer-confirmation"
                       for m in msgs2 for p in m["parts"])
        # 无连续重复消息(补写的问题在 resume 回流后被去重)
        assert all(texts2[i] != texts2[i - 1] for i in range(1, len(texts2)))


def test_history_restores_analysis_card(tmp_path):
    """分析轮后刷新/换设备(手机加入电脑端会话):/api/history 挂回
    data-analysis-plan 报告卡,不再只剩文字播报;下一轮非分析对话后,
    旧卡不残留(router 每轮入口清空 analysis_view)。"""
    from test_agent_graph import (_prev_month_ym, _seed_bill_wealth_card,
                                  _seed_tx_month)
    client, db, _tok = _client(tmp_path, [
        '{"intent": "bill_analysis"}',
        "上个月账单分析完成。",
        '{"intent": "chat"}',
        "好的,有需要再找我。",
    ])
    _seed_bill_wealth_card(db)
    y, m = _prev_month_ym()
    _seed_tx_month(db, y, m)
    with client:
        _post_chat(client, "帮我分析上个月的账单", "api-ac", token=_tok)
        msgs = client.get("/api/history",
                          params={"thread_id": "api-ac"},
                          headers={"x-bank-token": _tok}).json()["messages"]
        last = msgs[-1]
        assert last["role"] == "assistant"
        types = [p["type"] for p in last["parts"]]
        assert "data-analysis-plan" in types
        # 卡片在播报文本之前(与直播时的卡片位置一致)
        assert types.index("data-analysis-plan") < types.index("text")
        card = next(p for p in last["parts"]
                    if p["type"] == "data-analysis-plan")["data"]
        assert card["intent"] == "bill"
        assert card["reflection"]["status"] == "success"

        # 下一轮闲聊:旧分析卡不残留(最后一轮已不是分析轮)
        _post_chat(client, "谢谢", "api-ac", token=_tok)
        msgs2 = client.get("/api/history",
                           params={"thread_id": "api-ac"},
                           headers={"x-bank-token": _tok}).json()["messages"]
        assert not any(p.get("type") == "data-analysis-plan"
                       for m in msgs2 for p in m["parts"])


# ----------------------------------------------------------------- 常用收款人选择器

def test_contacts_api_list_create_and_validation(tmp_path):
    """转账选择器数据源:登录后列本人收款人;新建走 Ledger 校验并立即可见。"""
    client, _db, tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])
    with client:
        # 未登录:401(前端据此给登录引导,而不是空列表)
        r = client.get("/api/contacts")
        assert r.status_code == 401
        assert "登录" in r.json()["error"]

        headers = {"x-bank-token": tok}
        r = client.get("/api/contacts", headers=headers)
        assert r.status_code == 200
        contacts = r.json()["contacts"]
        assert [c["name"] for c in contacts] == ["林悦", "林悦", "张三"]
        # 每个收款人对应手机号(同名林悦靠手机号区分)
        assert sorted(c["phone"] for c in contacts) == [
            "13633334444", "13711112222", "13900008821"]

        r = client.post("/api/contacts", headers=headers, json={
            "name": "王小明", "phone": "13512345678", "note": "测试收款人"})
        assert r.status_code == 200
        created = r.json()["contact"]
        assert created["name"] == "王小明"
        assert created["phone"] == "13512345678"
        assert created["note"] == "测试收款人"

        names = [c["name"] for c in
                 client.get("/api/contacts", headers=headers).json()["contacts"]]
        assert "王小明" in names

        # 手机号唯一:同号换个名字也拒绝(不静默覆盖)
        r = client.post("/api/contacts", headers=headers, json={
            "name": "王小明明", "phone": "13512345678"})
        assert r.status_code == 400
        assert "不能重复" in r.json()["error"]

        # 手机号格式与姓名由服务层确定性校验
        r = client.post("/api/contacts", headers=headers, json={
            "name": "赵六", "phone": "1351234"})
        assert r.status_code == 400
        assert "11 位" in r.json()["error"]
        r = client.post("/api/contacts", headers=headers, json={
            "name": " ", "phone": "13512340000"})
        assert r.status_code == 400
        assert "姓名不能为空" in r.json()["error"]

        # 未登录不可新建
        r = client.post("/api/contacts", json={
            "name": "游客", "phone": "13512340001"})
        assert r.status_code == 401


# ----------------------------------------------------------------- 手机扫码登录

def test_qr_login_roundtrip_and_one_time(tmp_path):
    """扫码登录主链路:电脑端签票据 → 手机端换正式会话;票据一次性。"""
    client, _db, tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])
    with client:
        headers = {"x-bank-token": tok}
        # 未登录不可签发票据(码只能来自登录者本人的屏幕)
        r = client.post("/api/auth/qr-ticket")
        assert r.status_code == 401
        assert "电脑端登录" in r.json()["error"]

        r = client.post("/api/auth/qr-ticket", headers=headers)
        assert r.status_code == 200
        body = r.json()
        assert len(body["ticket"]) >= 16
        assert body["expires_in"] == 120

        # 手机端:票据换正式 token,与密码登录同权(/api/auth/me 可用)
        r = client.post("/api/auth/qr-login",
                        json={"ticket": body["ticket"]})
        assert r.status_code == 200
        sess = r.json()
        assert sess["nickname"] == "测试用户"
        r = client.get("/api/auth/me",
                       headers={"x-bank-token": sess["token"]})
        assert r.status_code == 200
        assert r.json()["user_id"] == sess["user_id"]

        # 同一票据第二次:已用过即焚
        r = client.post("/api/auth/qr-login",
                        json={"ticket": body["ticket"]})
        assert r.status_code == 400
        assert "已过期或已被使用" in r.json()["error"]

        # 乱码票据:同样 400
        r = client.post("/api/auth/qr-login",
                        json={"ticket": "x" * 32})
        assert r.status_code == 400


def test_qr_ticket_expiry(tmp_path, monkeypatch):
    """票据过期路径:TTL=0 时签出的票据即刻失效,手机端兑换被拒。"""
    monkeypatch.setenv("AGENT_QR_TICKET_TTL", "0")
    client, _db, tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])
    with client:
        r = client.post("/api/auth/qr-ticket",
                        headers={"x-bank-token": tok})
        assert r.status_code == 200
        r = client.post("/api/auth/qr-login",
                        json={"ticket": r.json()["ticket"]})
        assert r.status_code == 400
        assert "已过期" in r.json()["error"]


def test_qr_render_and_lan_info(tmp_path):
    """二维码本地渲染出 PNG;lan-info 返回本机 IP 口径。"""
    import re as _re
    client, _db, _tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])
    with client:
        r = client.get("/api/qr", params={
            "data": "http://192.168.1.5:3000/qr-login?ticket=abc123"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/png")
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

        # 缺 data 参数拒绝
        r = client.get("/api/qr")
        assert r.status_code == 400

        r = client.get("/api/lan-info")
        assert r.status_code == 200
        assert _re.fullmatch(r"(\d{1,3}\.){3}\d{1,3}",
                             r.json()["host"]), r.json()


def test_thread_create_named_and_search(tmp_path):
    """手机端「+」先命名建会话;搜索覆盖标题与消息正文(含命中片段)。"""
    client, _db, tok = _client(
        tmp_path,
        ['{"intent": "chat"}', "好的",
         '{"intent": "chat"}', "理财建议"])
    headers = {"x-bank-token": tok}
    with client:
        # 命名规则:空名/超 10 字拒绝;10 字整可建
        assert client.post("/api/threads", json={"title": "  "},
                           headers=headers).status_code == 400
        r = client.post("/api/threads", json={"title": "一二三四五六七八九十甲"},
                        headers=headers)
        assert r.status_code == 400
        assert "10 个字" in r.json()["error"]
        r = client.post("/api/threads", json={"title": "午餐记录"},
                        headers=headers)
        assert r.status_code == 200
        created = r.json()
        assert created["title"] == "午餐记录"
        tid = created["id"]

        # 进入会话发首条消息:标题保持用户命名,不被首句覆盖
        _post_chat(client, "今天中午吃了牛肉面", tid, token=tok)
        r = client.get("/api/threads", headers=headers)
        named = next(c for c in r.json()["chats"] if c["id"] == tid)
        assert named["title"] == "午餐记录"

        _post_chat(client, "帮我看看理财", "t-finance", token=tok)

        # 标题命中
        r = client.get("/api/threads", params={"q": "午餐"}, headers=headers)
        body = r.json()
        assert body["total"] == 1
        assert body["chats"][0]["id"] == tid
        # 正文命中(标题里没有「牛肉」):结果带片段
        r = client.get("/api/threads", params={"q": "牛肉"}, headers=headers)
        body = r.json()
        assert body["total"] == 1
        assert body["chats"][0]["id"] == tid
        assert "牛肉面" in body["chats"][0]["snippet"]
        # 另一个会话的正文也能命中
        r = client.get("/api/threads", params={"q": "理财"},
                       headers=headers)
        ids = [c["id"] for c in r.json()["chats"]]
        assert "t-finance" in ids
        # 无结果
        r = client.get("/api/threads", params={"q": "完全不存在的词"},
                       headers=headers)
        assert r.json()["total"] == 0
        assert r.json()["chats"] == []


# ----------------------------------------------------------------- 跨设备同步

def test_device_sync_state_revision_and_active(tmp_path):
    """跨设备同步:当前会话跟随、revision 每轮 +2、最后写入 client_id 可识别。"""
    client, _db, tok = _client(tmp_path, ['{"intent": "chat"}', "同步回复"])
    headers = {"x-bank-token": tok}
    with client:
        # 未登录不参与同步
        assert client.get("/api/sync/state").status_code == 401
        assert client.post("/api/sync/active", json={
            "thread_id": "t-sync"}).status_code == 401

        r = client.get("/api/sync/state", headers=headers)
        assert r.json()["thread_id"] is None

        # 不存在/不属于自己的会话不能设为 active(同步提示尽力而为,返回 ok=false)
        r = client.post("/api/sync/active", headers=headers, json={
            "thread_id": "no-such"})
        assert r.status_code == 200 and r.json()["ok"] is False

        # 发一轮对话:开始与结束各 +1 revision,并记录写入设备
        r = client.post("/api/chat", json={
            "messages": [{"role": "user", "content": "你好"}],
            "thread_id": "t-sync",
            "token": tok,
            "client_id": "desktop-tab",
        })
        assert r.status_code == 200
        r = client.get("/api/sync/state", headers=headers)
        state = r.json()
        assert state["thread_id"] == "t-sync"
        assert state["revision"] >= 2
        assert state["last_client_id"] == "desktop-tab"

        # 新命名会话创建后即成为当前会话(另一台设备应跟过去)
        created = client.post("/api/threads", headers=headers,
                              json={"title": "同步新会话"}).json()
        r = client.get("/api/sync/state", headers=headers)
        state = r.json()
        assert state["thread_id"] == created["id"]
        assert state["title"] == "同步新会话"
        assert state["revision"] == 0

        # 手动切回旧会话
        r = client.post("/api/sync/active", headers=headers,
                        json={"thread_id": "t-sync"})
        assert r.status_code == 200
        r = client.get("/api/sync/state", headers=headers)
        assert r.json()["thread_id"] == "t-sync"


# ----------------------------------------------------------------- BYOK / 模型切换

def test_chat_request_accepts_byok_fields(tmp_path):
    """请求携带供应商连接字段:假模型注入路径下被安全忽略,流程不受影响。"""
    client, _db, _tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])
    with client:
        r = client.post("/api/chat", json={
            "messages": [{"role": "user", "content": "你好"}],
            "thread_id": "byok-1",
            "api_key": "x" * 40,
            "base_url": "https://api.deepseek.com/v1",
            "provider": "deepseek",
            "model": "glm-5.3-flash",
        })
        assert r.status_code == 200
        assert "hi" in _joined_text(_frames(r.text))


def test_validate_key_endpoint(tmp_path, monkeypatch):
    """验 Key 接口:成功返回 ok+模型数;拉取失败返回 ok=False(不抛 500)。"""
    import agent.llm as llm_mod
    client, _db, _tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])

    def fake_ok(base, key, provider="openai"):
        assert key == "k" * 40
        assert base == "https://api.deepseek.com/v1"
        assert provider == "deepseek"
        return {"data": [{"id": "glm-5.3"}, {"id": "glm-5.3-flash"}]}

    def bad(base, key, provider="openai"):
        raise OSError("401 unauthorized")

    with client:
        monkeypatch.setattr(llm_mod, "fetch_models_json", fake_ok)
        r = client.post("/api/validate-key", json={
            "api_key": "k" * 40,
            "base_url": "https://api.deepseek.com/v1/",
            "provider": "deepseek",
        })
        assert r.status_code == 200
        assert r.json() == {"ok": True, "model_count": 2}

        # 非法 Base URL 在发起请求前就被拒(不允许 file:// 等协议)
        r_bad_base = client.post("/api/validate-key", json={
            "api_key": "k" * 40,
            "base_url": "file:///etc/passwd",
            "provider": "openai",
        })
        assert r_bad_base.json()["ok"] is False
        assert "http" in r_bad_base.json()["error"]

        monkeypatch.setattr(llm_mod, "fetch_models_json", bad)
        r2 = client.post("/api/validate-key", json={"api_key": "k" * 40})
        assert r2.status_code == 200
        body = r2.json()
        assert body["ok"] is False and "401" in body["error"]


# ----------------------------------------------------------------- 到期提醒入口

def test_reminders_endpoint_formats_linkage(tmp_path):
    """GET /api/reminders:as_of 时间旅行触发联动提醒 → 格式化为可直接发给
    助手的文本(《计划》+【动作】);重复触发不重复弹;无到期返回空列表。"""
    from bank_core.linkage import LinkageService
    from bank_core.money import yuan_to_cents
    from test_agent_graph import (LINK_AS_OF, _mk_linkage_db,
                                  _seed_linkage_plan)

    db = _mk_linkage_db(tmp_path)
    plan_id = _seed_linkage_plan(db)
    app = create_app(llm=_fake_llm('{"intent": "chat"}', "hi"),
                     db_path=db,
                     checkpoint_path=tmp_path / "api_ckpt.sqlite",
                     threads_path=tmp_path / "api_threads.sqlite")
    with TestClient(app) as client:
        # 拨到生日前 1 天:鲜花(前2天)与蛋糕(前1天)提醒都已到期
        r = client.get("/api/reminders", params={"as_of": LINK_AS_OF})
        assert r.status_code == 200
        items = r.json()["reminders"]
        assert len(items) == 2
        texts = {i["text"] for i in items}
        assert texts == {
            "(到期提醒) 计划《林悦的生日联动》的【鲜花】今天到期,请帮我处理",
            "(到期提醒) 计划《林悦的生日联动》的【蛋糕】今天到期,请帮我处理",
        }
        assert all(i["kind"] == "linkage" and i["plan_id"] == plan_id
                   for i in items)

        # run_due_tasks 已把任务置 done:再触发(含不传 as_of)不重复弹
        r2 = client.get("/api/reminders", params={"as_of": LINK_AS_OF})
        assert r2.status_code == 200 and r2.json()["reminders"] == []
        r3 = client.get("/api/reminders")
        assert r3.status_code == 200 and r3.json()["reminders"] == []


def test_reminders_endpoint_no_due_returns_empty(tmp_path):
    """空库(无任何到期任务):返回空列表,绝不 500(提醒入口不拖垮聊天)。"""
    client, _db, _tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])
    with client:
        r = client.get("/api/reminders")
        assert r.status_code == 200
        assert r.json() == {"reminders": []}


# ------------------------------------------------- 悬空转账提醒接口(右下角弹窗+落地页)

def test_pending_orders_and_page_confirm(tmp_path):
    """弹窗数据源 + 页面直连确认:与对话闸门同一套安全规则(两步走+支付密码)。"""
    client, db, tok = _client(tmp_path, [
        '{"intent": "transfer"}',
        '{"payee": "张三", "amount_yuan": "500", "when": "now"}',
        "(不会被走到:建单后停闸门,report 不执行)",
    ])
    with client:
        # 建单 → 悬空在人工闸门
        r1 = _post_chat(client, "给张三转 500 元", "api-pend", token=tok)
        card = _find_data(_frames(r1.text), "data-transfer-confirmation")
        assert card is not None
        order_id = card["data"]["order_id"]
        assert _order(db, order_id)["status"] == "pending_confirm"
        assert _balance(db) == DEFAULT_BALANCE  # 建单不动钱

        # 弹窗数据源:当前用户悬空单可见
        r = client.get("/api/pending-orders", headers={"x-bank-token": tok})
        assert r.status_code == 200
        orders = r.json()["orders"]
        assert any(o["order_id"] == order_id and o["to_name"] == "张三"
                   and o["amount_yuan"] == "500.00"
                   and o["status"] == "pending_confirm" for o in orders)

        # 未登录/无效 token → 空列表(不泄露他人订单)
        assert client.get("/api/pending-orders",
                          headers={"x-bank-token": "x" * 32}).json()["orders"] == []

        # 页面确认:密码错 → 拒绝且订单不动
        r = client.post("/api/orders/confirm",
                        json={"token": tok, "order_id": order_id,
                              "pay_password": "000000"})
        assert r.status_code == 400 and "支付密码" in r.json()["error"]
        assert _order(db, order_id)["status"] == "pending_confirm"
        assert _balance(db) == DEFAULT_BALANCE

        # 密码对 → executed + 恰好扣 500(与对话确认同一条收尾路径)
        r = client.post("/api/orders/confirm",
                        json={"token": tok, "order_id": order_id,
                              "pay_password": "888888"})
        assert r.status_code == 200 and r.json()["status"] == "executed"
        assert _balance(db) == DEFAULT_BALANCE - 50_000

        # 确认后弹窗数据源不再含该单
        orders2 = client.get("/api/pending-orders",
                             headers={"x-bank-token": tok}).json()["orders"]
        assert all(o["order_id"] != order_id for o in orders2)

        # 已执行单重复确认 → 状态不可确认
        r = client.post("/api/orders/confirm",
                        json={"token": tok, "order_id": order_id,
                              "pay_password": "888888"})
        assert r.status_code == 400 and "不可确认" in r.json()["error"]

        # 别人的订单 404(user 隔离)
        r = client.post("/api/orders/confirm",
                        json={"token": tok, "order_id": 99999,
                              "pay_password": "888888"})
        assert r.status_code == 404


# ------------------------------------------------- 订阅场景 SSE 帧(六场景收官)

def _seed_subs(db) -> None:
    import sqlite3 as _s
    from datetime import datetime
    conn = _s.connect(db)
    conn.executemany(
        """INSERT INTO subscriptions
           (user_id, merchant_name, category, amount_cents, period_days,
            next_charge_date, status, detected_at)
           VALUES (1,?,?,?,?,?,?,?)""",
        [("腾讯视频VIP", "订阅", 3000, 31, "2026-10-09", "active",
          "2026-09-01T00:00:00"),
         ("Keep会员", "订阅", 1900, 31, "2026-10-06", "active",
          "2026-09-01T00:00:00")])
    conn.commit()
    conn.close()


def test_subscription_list_and_cancel_frames(tmp_path):
    """查订阅→data-subscription-list 卡片帧;取消→确认卡+密码闸门→执行。"""
    client, db, tok = _client(tmp_path, [
        '{"intent": "subscription"}',
        '{"action": "list", "merchant": null}',
        "订阅清单已列出。",
        '{"intent": "subscription"}',
        '{"action": "cancel", "merchant": "腾讯视频VIP"}',
        "已取消腾讯视频VIP的自动扣费。",
    ])
    _seed_subs(db)
    with client:
        # 1) 查订阅:列表卡帧(active 2 项+月/年合计),无闸门
        r1 = _post_chat(client, "帮我看看订阅都花多少钱", "api-sub", token=tok)
        frames1 = _frames(r1.text)
        card = _find_data(frames1, "data-subscription-list")
        assert card is not None, f"缺订阅列表卡: {[f['type'] for f in frames1]}"
        items = card["data"]["items"]
        assert [i["merchant_name"] for i in items] == ["Keep会员", "腾讯视频VIP"]
        assert card["data"]["annual_total_yuan"] and card["data"]["monthly_total_yuan"]
        assert _find_data(frames1, "data-gate-pending") is None

        # 2) 取消:确认卡帧 + 支付密码闸门
        r2 = _post_chat(client, "取消腾讯视频VIP的自动扣费", "api-sub", token=tok)
        frames2 = _frames(r2.text)
        cancel = _find_data(frames2, "data-subscription-cancel")
        assert cancel is not None
        assert cancel["data"]["merchant_name"] == "腾讯视频VIP"
        assert cancel["data"]["pay_required"] is True
        gate = _find_data(frames2, "data-gate-pending")
        assert gate is not None and gate["data"]["gate_type"] == "confirm_sub_cancel"

        # 3) 支付密码确认 → 取消落库
        r3 = _post_chat(client, "888888", "api-sub", token=tok)
        assert "取消" in _joined_text(_frames(r3.text))
        import sqlite3 as _s2
        conn = _s2.connect(db)
        status = conn.execute(
            "SELECT status FROM subscriptions WHERE merchant_name='腾讯视频VIP'"
        ).fetchone()[0]
        conn.close()
        assert status == "cancelled"


# ------------------------------------- 支付密码 4 次尝试限制(待确认转账页)

def _confirm(client, tok, order_id, pw):
    return client.post("/api/orders/confirm",
                       json={"token": tok, "order_id": order_id,
                             "pay_password": pw})


def test_pay_password_four_strikes_lock(tmp_path):
    """4 错锁定/成功清零/锁后正确密码也拒/管理员解锁恢复。"""
    import sqlite3 as _s
    client, db, tok = _client(tmp_path, [
        '{"intent": "transfer"}',
        '{"payee": "张三", "amount_yuan": "500", "when": "now"}',
        '{"intent": "transfer"}',
        '{"payee": "张三", "amount_yuan": "300", "when": "now"}',
        '{"intent": "transfer"}',
        '{"payee": "张三", "amount_yuan": "200", "when": "now"}',
        '{"intent": "transfer"}',
        '{"payee": "张三", "amount_yuan": "100", "when": "now"}',
    ])
    with client:
        # 建三笔悬空单(用三个 thread 各建一笔)
        oids = []
        for i, amt in enumerate(("500", "300", "200")):
            r = _post_chat(client, f"给张三转 {amt} 元", f"lock-t{i}", token=tok)
            card = _find_data(_frames(r.text), "data-transfer-confirmation")
            assert card is not None
            oids.append(card["data"]["order_id"])

        def user_row():
            conn = _s.connect(db)
            row = conn.execute(
                "SELECT pay_fail_count, transfer_locked FROM users WHERE id=1"
            ).fetchone()
            conn.close()
            return tuple(row)

        # A) 错 1-2 次:提示剩余次数,不锁
        r1 = _confirm(client, tok, oids[0], "000001")
        assert r1.status_code == 400 and "1/4" in r1.json()["error"]
        r2 = _confirm(client, tok, oids[0], "000002")
        assert r2.status_code == 400 and "2/4" in r2.json()["error"]
        assert user_row() == (2, 0)
        # B) 第 3 次输对:计数清零(爆破者无法累积窗口)
        r3 = _confirm(client, tok, oids[0], "888888")
        assert r3.status_code == 200
        assert user_row() == (0, 0)
        # C) 重新连续 4 错:第 4 次触发锁定(403)
        for i in range(3):
            rr = _confirm(client, tok, oids[1], "999999")
            assert rr.status_code == 400
        r4 = _confirm(client, tok, oids[1], "999998")
        assert r4.status_code == 403 and "锁定" in r4.json()["error"]
        assert user_row() == (4, 1)
        # D) 锁定后:正确密码也拒绝(403),订单不动
        r5 = _confirm(client, tok, oids[1], "888888")
        assert r5.status_code == 403
        assert _order(db, oids[1])["status"] == "pending_confirm"
        # E) 锁定波及一切转账入口(ledger 层统一卡点:对话建单/页面确认同源)
        import pytest as _pt
        from bank_core.ledger import LedgerError, LedgerService
        conn2 = _s.connect(db)
        conn2.row_factory = _s.Row
        acct_id = conn2.execute(
            "SELECT id FROM accounts WHERE user_id=1 AND type='checking'"
        ).fetchone()[0]
        svc = LedgerService(conn2, user_id=1)
        with _pt.raises(LedgerError, match="锁定"):
            svc.create_transfer_order(acct_id, 10_000, to_name="张三",
                                      to_account_tail="0001",
                                      idempotency_key="lock-e2e-1")
        with _pt.raises(LedgerError, match="锁定"):
            svc.confirm_transfer_order(oids[2])
        conn2.close()
        # F) 管理员解锁(8789 同库):计数清零,转账恢复
        from bank_core.admin_api import create_admin_app
        from fastapi.testclient import TestClient as _TC
        with _TC(create_admin_app(db_path=db)) as admin:
            ru = admin.post("/api/transfer-unlock", params={"user_id": 1})
            assert ru.status_code == 200 and ru.json()["unlocked"] is True
        assert user_row() == (0, 0)
        r7 = _confirm(client, tok, oids[1], "888888")
        assert r7.status_code == 200
        assert _order(db, oids[1])["status"] == "executed"


def test_subscription_precheck_frame(tmp_path):
    """订阅预审:SSE 应出现 data-subscription-precheck 卡片帧(明天扣费项)。"""
    from datetime import date, timedelta
    import sqlite3 as _s

    client, db, tok = _client(tmp_path, [
        '{"intent": "subscription"}',
        json.dumps({"action": "precheck", "merchant": None}, ensure_ascii=False),
        "即将扣费已列出。",
    ])
    nxt = (date.today() + timedelta(days=1)).isoformat()
    ac = _s.connect(db)
    ac.execute(
        """INSERT INTO subscriptions
           (user_id, merchant_name, category, amount_cents, period_days,
            next_charge_date, status, detected_at)
           VALUES (1,'明日扣会员','订阅',1500,30,?,'active','2026-09-01T00:00:00')""",
        (nxt,))
    ac.commit()
    ac.close()
    with client:
        resp = _post_chat(client, "订阅预审", "sp-frame", tok)
    card = _find_data(_frames(resp.text), "data-subscription-precheck")
    assert card and card["data"]["items"], "预审卡帧未下发"
    assert card["data"]["items"][0]["merchant_name"] == "明日扣会员"
    assert card["data"]["items"][0]["days_left"] == 1


def test_order_receipt_audit_chain(tmp_path):
    """播报即凭证:执行单的三表审计链可查,未登录 401 / 他人单 404。"""
    import sqlite3 as _s

    from bank_core.db import connect as _bconnect
    from bank_core.ledger import LedgerService

    client, db, tok = _client(tmp_path, [])
    conn = _bconnect(db)
    svc = LedgerService(conn, user_id=1)
    acct = svc.list_accounts()[0]["id"]
    order = svc.create_transfer_order(
        from_account_id=acct, amount_cents=1234, to_name="张三",
        memo="凭证测试", idempotency_key="agent-test-receipt-1")
    svc.confirm_transfer_order(order["id"])
    conn.close()
    oid = order["id"]

    with client:
        resp = client.get(f"/api/orders/{oid}/receipt",
                          headers={"x-bank-token": tok})
    assert resp.status_code == 200
    body = resp.json()
    assert body["order"]["order_id"] == oid
    assert body["order"]["amount_yuan"] == "12.34"
    assert body["order"]["status"] == "executed"
    assert body["order"]["executed_at"]
    assert body["order"]["idempotency_key"] == "agent-test-receipt-1"
    assert body["transaction"]["external_ref"] == f"order:{oid}"
    assert body["transaction"]["balance_after_yuan"]
    led = body["ledger"]
    assert led and led["ref"] == f"order:{oid}" and led["operator"] == "agent"
    assert led["before_yuan"] and led["after_yuan"]
    tools_hit = {a["tool"] for a in body["audit_trail"]}
    assert {"create_transfer_order", "confirm_transfer_order"} <= tools_hit

    with client:
        assert client.get(f"/api/orders/{oid}/receipt").status_code == 401
        assert client.get("/api/orders/99999/receipt",
                          headers={"x-bank-token": tok}).status_code == 404


# ----------------------------------------------------------------- 记忆检阅入口(2026-10-23)

def test_memory_view_and_forget_endpoints(tmp_path, monkeypatch):
    """「Agent 记忆」弹窗数据源:GET /api_memory(view)画像置顶+长程任务轨道;
    DELETE(忘记)自作用域;未登录 401、未知模块 404。"""
    # 任务库指到 tmp(conftest 只隔离了记忆库)
    monkeypatch.setenv("AGENT_TASKS_DB", str(tmp_path / "api_tasks.sqlite"))
    from agent import memory as memory_mod, tasks as tasks_mod
    tasks_mod.create_task(1, goal="生日存钱", target_yuan="600",
                          deadline="2026-12-01")
    client, _db, tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])
    headers = {"x-bank-token": tok}
    with client:
        # 未登录 401(记忆是个人数据)
        assert client.get("/api/memory").status_code == 401
        assert client.delete("/api/memory/bill").status_code == 401
        memory_mod.remember(1, "profile", {"name": "丁博文",
                                           "risk_level": "稳健"})
        memory_mod.remember(1, "bill", {"top_category": "餐饮"})
        r = client.get("/api/memory", headers=headers)
        assert r.status_code == 200
        body = r.json()
        mods = body["modules"]
        assert [m["module"] for m in mods] == ["profile", "bill"]  # 画像置顶
        assert mods[0]["label"] == "用户画像"
        assert mods[0]["lines"] == ["姓名：丁博文", "风险偏好：稳健"]
        assert mods[0]["updated_at"]  # 留痕:什么时候记的
        assert body["active_task"]["goal"] == "生日存钱"  # 长程任务轨道
        # 忘记一个模块 → 返回剩余;未知模块 404
        r = client.delete("/api/memory/bill", headers=headers)
        assert [m["module"] for m in r.json()["modules"]] == ["profile"]
        assert memory_mod.recall(1, "bill") is None
        assert client.delete("/api/memory/随便写",
                             headers=headers).status_code == 404
        # 结束长程任务轨道(2026-10-24):cancelled 留痕,视图立即清空
        assert client.delete("/api/memory/active-task").status_code == 401
        r = client.delete("/api/memory/active-task", headers=headers)
        assert r.json()["active_task"] is None
        assert tasks_mod.get_active_task(1) is None
        # 一键清空全部档案(2026-10-24):任务轨道不在档案里,不受影响
        memory_mod.remember(1, "wealth", {"risk_level": "C3"})
        assert client.delete("/api/memory").status_code == 401
        r = client.delete("/api/memory", headers=headers)
        assert r.json()["modules"] == []
        assert memory_mod.recall_all(1) == {}


# ----------------------------------------------------------------- 人设接口(2026-10-24)

def test_persona_get_put_endpoints(tmp_path):
    """GET/PUT /api/persona:未登录 401;roundtrip;空文本恢复默认;
    温度越界/超长文本 422(pydantic 校验)。"""
    from agent import persona as persona_mod
    client, _db, tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])
    headers = {"x-bank-token": tok}
    with client:
        assert client.get("/api/persona").status_code == 401
        assert client.put("/api/persona", json={"persona": "x"}
                          ).status_code == 401
        # 默认态
        r = client.get("/api/persona", headers=headers)
        assert r.status_code == 200
        assert r.json() == {"persona": "", "temperature": 0.1,
                            "is_default": True}
        # 写入 + 回读
        r = client.put("/api/persona", headers=headers,
                       json={"persona": "说话像江湖侠客", "temperature": 0.9})
        assert r.status_code == 200
        assert r.json() == {"persona": "说话像江湖侠客", "temperature": 0.9,
                            "is_default": False}
        assert client.get("/api/persona", headers=headers).json()[
            "persona"] == "说话像江湖侠客"
        # 恢复默认
        r = client.put("/api/persona", headers=headers,
                       json={"persona": "", "temperature": 0.1})
        assert r.json()["is_default"] is True
        assert persona_mod.get_persona(1)["text"] == ""
        # 校验:温度越界 / 超长文本
        assert client.put("/api/persona", headers=headers,
                          json={"persona": "x", "temperature": 1.5}
                          ).status_code == 422
        assert client.put("/api/persona", headers=headers,
                          json={"persona": "长" * 301}
                          ).status_code == 422


# ----------------------------- 多单并行挂起: api 侧闸门新指令判定(2026-10-26)

def test_gate_new_transfer_predicate():
    """_gate_new_transfer:只认 confirm_transfer+pay_required 的中断载荷;
    其他闸门(理财/卡片)或非指令文本一律不分流。"""
    from types import SimpleNamespace as NS
    from agent.api import _gate_new_transfer

    def snap(ptype, pay=True):
        intr = NS(value={"type": ptype, "pay_required": pay, "question": "q"})
        return NS(tasks=[NS(interrupts=[intr])])

    assert _gate_new_transfer(snap("confirm_transfer"), "给老王转500元") is True
    assert _gate_new_transfer(snap("confirm_wealth"), "给老王转500元") is False
    assert _gate_new_transfer(snap("confirm_card"), "给老王转500元") is False
    assert _gate_new_transfer(snap("confirm_transfer", pay=False),
                              "给老王转500元") is False
    assert _gate_new_transfer(snap("confirm_transfer"), "888888") is False
    assert _gate_new_transfer(snap("confirm_transfer"), "取消") is False
    assert _gate_new_transfer(snap("confirm_transfer"), "") is False
    assert _gate_new_transfer(NS(tasks=[NS(interrupts=[])]),
                              "给老王转500元") is False


# --------------------------------------------------------- 理财中心入口卡(2026-10-26)

def test_wealth_entry_card_frame(tmp_path):
    """「理财操作」泛入口 → SSE 下发 data-wealth-entry(等级+在售产品可购标注+
    目的选项),前端 WealthEntryCard 渲染;LLM 误判 linkage 时由 router 确定性改道。"""
    client, db, tok = _client(tmp_path, [
        '{"intent": "linkage"}',  # 模拟实测误判:LLM 把「理财操作」归联动
        json.dumps({"action": None, "keyword": None, "product_id": None,
                    "amount_yuan": None, "holding_id": None, "p_type": None,
                    "assess_answer": None}, ensure_ascii=False),
    ])
    import sqlite3 as _s
    ac = _s.connect(db)
    ac.executemany(
        """INSERT INTO wealth_products
           (id,code,name,p_type,risk_level,expected_return_bps,
            min_subscribe_cents,lock_days,subscription_fee_bps,
            redemption_fee_bps,intro)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [(1, "MF001", "余额+货币基金", "money_fund", 1, 185, 100, 0, 0, 0,
          "随存随取，七日年化约1.85%"),
         (3, "MX006", "科技创新股票基金", "mixed", 4, 850, 10000, 0, 15, 50,
          "高波动，追求长期增值")])
    ac.execute(
        "INSERT INTO risk_profiles (user_id,answers_json,score,level,updated_at) "
        "VALUES (1,'{\"稳健偏好\":3,\"投资经验\":3,\"亏损容忍\":3,"
        "\"投资期限\":3,\"收入稳定\":3}',15,'C3','2026-01-01T00:00:00')")
    ac.commit()
    ac.close()
    with client:
        r = _post_chat(client, "理财操作", "api-w-entry", token=tok)
        assert r.status_code == 200
        frames = _frames(r.text)
        card = _find_data(frames, "data-wealth-entry")
        assert card is not None, f"应下发理财中心入口卡: {[f['type'] for f in frames]}"
        data = card["data"]
        assert data["level"] == "C3"
        assert data["max_risk_level"] == 3
        purch = {p["code"]: p["purchasable"] for p in data["products"]}
        assert purch == {"MF001": True, "MX006": False}  # 可购标注按等级算好
        assert data["options"][-1] == "先做风险测评"
        # 问题文本照常以 text 帧播报(卡片之外还有一句引导语)
        assert "理财" in _joined_text(frames)


# --------------------------------------------------------- 可购产品列表卡(2026-10-26)

def test_wealth_products_card_frame(tmp_path):
    """图一用户口径:「现在有哪些产品」不能只回一段文字——w_query 更新时 SSE
    下发 data-wealth-products(等级+可购产品清单),前端 WealthProductsCard
    渲染成表单卡;帧随节点更新下发,不占 interrupt。"""
    client, db, tok = _client(tmp_path, [
        '{"intent": "wealth"}',
        json.dumps({"action": "query", "keyword": None, "product_id": None,
                    "amount_yuan": None, "holding_id": None, "p_type": None,
                    "assess_answer": None}, ensure_ascii=False),
        "已按你的风险等级筛出可购产品。",
    ])
    import sqlite3 as _s
    ac = _s.connect(db)
    ac.executemany(
        """INSERT INTO wealth_products
           (id,code,name,p_type,risk_level,expected_return_bps,
            min_subscribe_cents,lock_days,subscription_fee_bps,
            redemption_fee_bps,intro)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [(1, "MF001", "余额+货币基金", "money_fund", 1, 185, 100, 0, 0, 0,
          "随存随取，七日年化约1.85%"),
         (2, "BD003", "稳健纯债基金", "bond", 2, 320, 10000, 0, 0, 15,
          "中低风险，历史年化约3.2%"),
         (3, "MX006", "科技创新股票基金", "mixed", 4, 850, 10000, 0, 15, 50,
          "高波动，追求长期增值")])
    ac.execute(
        "INSERT INTO risk_profiles (user_id,answers_json,score,level,updated_at) "
        "VALUES (1,'{\"稳健偏好\":3,\"投资经验\":3,\"亏损容忍\":3,"
        "\"投资期限\":3,\"收入稳定\":3}',15,'C3','2026-01-01T00:00:00')")
    ac.commit()
    ac.close()
    with client:
        r = _post_chat(client, "现在有哪些产品", "api-w-products", token=tok)
        assert r.status_code == 200
        frames = _frames(r.text)
        card = _find_data(frames, "data-wealth-products")
        assert card is not None, \
            f"应下发可购产品列表卡: {[f['type'] for f in frames]}"
        data = card["data"]
        assert data["level"] == "C3"
        assert data["max_risk_level"] == 3
        assert data["need_assess"] is False
        # 适当性过滤后的可购清单(R1/R2),逐行 purchasable 标注
        purch = {p["code"]: p["purchasable"] for p in data["products"]}
        assert purch == {"MF001": True, "BD003": True}  # R4 被 max_risk_level 滤掉
        # 播报文本照常流出(卡片是补充,不是替代)
        assert "产品" in _joined_text(frames)


# --------------------------------------------------------- 年度账单报告卡(2026-10-26)

def test_bill_report_card_frame(tmp_path):
    """「月度/年度账单报告」→ SSE 下发 data-bill-report(年度口径):
    KPI 总览 + 逐月走势 + 分类/商户,前端 BillReportCard 渲染;零动钱。"""
    client, db, tok = _client(tmp_path, [
        '{"intent": "chat"}',  # 模拟 LLM 误判闲聊:router 确定性改道 bill_analysis
        json.dumps({"ask": None, "period": None, "category": None},
                   ensure_ascii=False),
        "年度账单报告已生成。",
    ])
    import sqlite3 as _s
    from datetime import date as _date
    ac = _s.connect(db)
    year = _date.today().year
    ts = f"{year}-01-15T10:00:00"
    ac.executemany(
        """INSERT INTO transactions
           (account_id, ts, direction, amount_cents, balance_after_cents,
            tx_type, counterparty, category, memo)
           VALUES (1, ?, ?, ?, 0, 'pos', ?, ?, '')""",
        [(ts, "in", 800_000, "示例公司", "工资"),
         (ts, "out", 120_000, "示例超市", "生活")])
    ac.commit()
    ac.close()
    with client:
        r = _post_chat(client, "月度/年度账单报告。", "api-bill-report", token=tok)
        assert r.status_code == 200
        frames = _frames(r.text)
        card = _find_data(frames, "data-bill-report")
        assert card is not None, f"应下发账单报告卡: {[f['type'] for f in frames]}"
        data = card["data"]
        assert data["kind"] == "annual"
        assert str(year) in data["title"]
        assert data["income_yuan"] == "8000.00"
        assert data["expense_yuan"] == "1200.00"
        assert len(data["monthly"]) == _date.today().month
        cats = {c["category"] for c in data["categories"]}
        assert cats == {"生活"}
        assert data["merchants"][0]["merchant"] == "示例超市"
        # 只读:没有确认卡 / 闸门
        assert _find_data(frames, "data-gate-pending") is None


def test_home_overview_aggregates_readonly(tmp_path):
    """银行首页聚合端点:登录可读五块数据,未登录 401,纯读不动账。"""
    client, db, tok = _client(tmp_path, ["好的"])
    with client:
        r = client.get("/api/home", headers={"x-bank-token": tok})
        assert r.status_code == 200
        data = r.json()
        assert Decimal(str(data["total_yuan"])) > 0
        assert data["accounts"], "种子库应有账户"
        assert data["recent_transactions"] == []  # 最小夹具库无流水
        # 入金产生一笔流水 -> 首页立刻可见(读的是同一张表)
        rd = client.post("/api/deposit", headers={"x-bank-token": tok},
                         json={"amount_yuan": "1000"})
        assert rd.status_code == 200, rd.text
        data2 = client.get("/api/home",
                           headers={"x-bank-token": tok}).json()
        assert data2["recent_transactions"], "入金后应有最近交易"
        top = data2["recent_transactions"][0]
        assert top["direction"] == "in" and top["amount_yuan"] == "1000.00"
        # 未登录 401
        r401 = client.get("/api/home")
        assert r401.status_code == 401


def test_delete_thread_endpoint(tmp_path):
    """删会话:目录行/active 一并清掉,重复删 404,非本人 403。"""
    client, db, tok = _client(tmp_path, ["好的"])
    with client:
        h = {"x-bank-token": tok}
        tid = client.post("/api/threads", headers=h,
                          json={"title": "待删会话"}).json()["id"]
        listed = client.get("/api/threads?limit=200", headers=h).json()
        assert any(c["id"] == tid for c in listed["chats"])

        # 未登录(owner=0)删别人的会话 → 403
        assert client.delete(f"/api/threads/{tid}").status_code == 403

        d = client.delete(f"/api/threads/{tid}", headers=h)
        assert d.status_code == 200
        assert d.json()["deleted"] == tid
        after = client.get("/api/threads?limit=200", headers=h).json()
        assert all(c["id"] != tid for c in after["chats"])
        # 重复删 → 404
        assert client.delete(f"/api/threads/{tid}", headers=h).status_code == 404


def test_delete_all_threads_endpoint(tmp_path):
    """清空全部会话:只清当前用户的,返回删除条数。"""
    client, db, tok = _client(tmp_path, ["好的"])
    with client:
        h = {"x-bank-token": tok}
        for title in ("会话一", "会话二"):
            assert client.post("/api/threads", headers=h,
                               json={"title": title}).status_code == 200
        before = client.get("/api/threads?limit=200", headers=h).json()
        assert len(before["chats"]) >= 2
        d = client.delete("/api/threads", headers=h)
        assert d.status_code == 200
        assert d.json()["deleted"] >= 2
        after = client.get("/api/threads?limit=200", headers=h).json()
        assert after["chats"] == []
