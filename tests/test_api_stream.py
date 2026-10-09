"""API 流式端点测试:TestClient 断言 SSE 协议、确认卡片字段、完整确认/取消 HTTP 往返。

不联网:注入 FakeMessagesListChatModel;银行库/检查点库都在 tmp_path。
临时库与断言助手复用 tests/test_agent_graph.py(同目录,pytest prepend 导入模式可直接 import)。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from agent.api import STREAM_HEADER, STREAM_HEADER_VALUE, create_app
from test_agent_graph import DEFAULT_BALANCE, _balance, _mk_db, _order


def _fake_llm(*responses: str) -> FakeMessagesListChatModel:
    return FakeMessagesListChatModel(responses=[AIMessage(content=r) for r in responses])


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
        assert body["bank_tools"] == 42  # +4 联动 +1 verify_pay_password
        assert "model" in body

        # 没有 user 消息 → 400,绝不进入流式
        r2 = client.post("/api/chat",
                         json={"messages": [{"role": "assistant", "content": "x"}],
                               "thread_id": "bad"})
        assert r2.status_code == 400


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

        _post_chat(client, "888888", "api-gq", token=_tok)
        msgs2 = client.get("/api/history",
                           params={"thread_id": "api-gq"},
                           headers={"x-bank-token": _tok}).json()["messages"]
        texts2 = [m["parts"][0]["text"] for m in msgs2]
        assert any("已向张三" in t for t in texts2)
        # 无连续重复消息(补写的问题在 resume 回流后被去重)
        assert all(texts2[i] != texts2[i - 1] for i in range(1, len(texts2)))


# ----------------------------------------------------------------- BYOK / 模型切换

def test_chat_request_accepts_byok_fields(tmp_path):
    """请求携带 api_key/model 字段:假模型注入路径下被安全忽略,流程不受影响。"""
    client, _db, _tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])
    with client:
        r = client.post("/api/chat", json={
            "messages": [{"role": "user", "content": "你好"}],
            "thread_id": "byok-1",
            "api_key": "x" * 40,
            "model": "glm-5.3-flash",
        })
        assert r.status_code == 200
        assert "hi" in _joined_text(_frames(r.text))


def test_validate_key_endpoint(tmp_path, monkeypatch):
    """验 Key 接口:成功返回 ok+模型数;拉取失败返回 ok=False(不抛 500)。"""
    import agent.llm as llm_mod
    client, _db, _tok = _client(tmp_path, ['{"intent": "chat"}', "hi"])

    def fake_ok(base, key):
        assert key == "k" * 40
        return {"data": [{"id": "glm-5.3"}, {"id": "glm-5.3-flash"}]}

    def bad(base, key):
        raise OSError("401 unauthorized")

    with client:
        monkeypatch.setattr(llm_mod, "fetch_models_json", fake_ok)
        r = client.post("/api/validate-key", json={"api_key": "k" * 40})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "model_count": 2}

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
