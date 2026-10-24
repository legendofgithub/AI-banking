"""消息投票接口测试:投/换/取消往返、GET 恢复、参数校验、管理台汇总。

threads 库隔离在 tmp_path(create_app 传参);投票是会话数据,访客也可投。
管理台 /api/votes 跨库读同一张表,汇总满意度与明细。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agent.api import create_app
from test_agent_graph import _mk_db
from test_api_stream import _fake_llm


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    """注入假模型的 app + TestClient(lifespan 里建 message_votes 表)。"""
    db = _mk_db(tmp_path)
    app = create_app(llm=_fake_llm("ok"), db_path=db,
                     checkpoint_path=tmp_path / "v_ckpt.sqlite",
                     threads_path=tmp_path / "v_threads.sqlite")
    with TestClient(app) as probe:
        yield probe


def _patch(client: TestClient, chat="c1", msg="m1", typ="up"):
    return client.patch("/api/vote", json={
        "chatId": chat, "messageId": msg, "type": typ})


# ---------------------------------------------------------------- 投/换/取消

def test_upvote_then_get_roundtrip(client: TestClient):
    assert _patch(client).status_code == 200
    votes = client.get("/api/vote", params={"chatId": "c1"}).json()
    assert votes == [{"chatId": "c1", "messageId": "m1", "isUpvoted": True}]


def test_switch_vote_overwrites(client: TestClient):
    _patch(client, typ="up")
    assert _patch(client, typ="down").status_code == 200
    votes = client.get("/api/vote", params={"chatId": "c1"}).json()
    assert len(votes) == 1
    assert votes[0]["isUpvoted"] is False


def test_same_vote_again_cancels(client: TestClient):
    _patch(client, typ="up")
    _patch(client, typ="up")  # 同票再点=取消
    assert client.get("/api/vote", params={"chatId": "c1"}).json() == []


def test_votes_scoped_by_chat(client: TestClient):
    _patch(client, chat="c1", msg="m1", typ="up")
    _patch(client, chat="c2", msg="m9", typ="down")
    c1 = client.get("/api/vote", params={"chatId": "c1"}).json()
    assert [v["messageId"] for v in c1] == ["m1"]


def test_guest_can_vote_without_token(client: TestClient):
    """投票属会话数据:无登录头也放行(会话本身免登录可建)。"""
    r = _patch(client)  # fixture 未带任何 token 头
    assert r.status_code == 200


def test_invalid_type_rejected(client: TestClient):
    assert client.patch("/api/vote", json={
        "chatId": "c1", "messageId": "m1", "type": "mid"}).status_code == 422
    assert client.patch("/api/vote", json={
        "chatId": "", "messageId": "m1", "type": "up"}).status_code == 422


# ---------------------------------------------------------------- 管理台汇总

def test_admin_votes_summary(tmp_path: Path, monkeypatch):
    """管理台 /api/votes 跨库读 8800 的 message_votes 并汇总。"""
    from bank_core.admin_api import create_admin_app

    thr = tmp_path / "admin_threads.sqlite"
    monkeypatch.setenv("AGENT_THREADS_DB", str(thr))
    db = _mk_db(tmp_path)
    app = create_app(llm=_fake_llm("ok"), db_path=db,
                     checkpoint_path=tmp_path / "av_ckpt.sqlite",
                     threads_path=thr)
    with TestClient(app) as probe:
        probe.patch("/api/vote", json={
            "chatId": "c1", "messageId": "m1", "type": "up"})
        probe.patch("/api/vote", json={
            "chatId": "c1", "messageId": "m2", "type": "up"})
        probe.patch("/api/vote", json={
            "chatId": "c2", "messageId": "m3", "type": "down"})

    admin = create_admin_app(db)
    with TestClient(admin) as probe:
        data = probe.get("/api/votes").json()
    assert data["summary"]["total"] == 3
    assert data["summary"]["up"] == 2
    assert data["summary"]["down"] == 1
    assert data["summary"]["satisfaction"] == 66.7
    assert len(data["votes"]) == 3


def test_admin_votes_empty_when_no_table(tmp_path: Path, monkeypatch):
    """表还没建(没人投过票)时返回空+None 满意度,绝不 500。"""
    from bank_core.admin_api import create_admin_app

    monkeypatch.setenv("AGENT_THREADS_DB",
                       str(tmp_path / "no_such_threads.sqlite"))
    admin = create_admin_app(_mk_db(tmp_path))
    with TestClient(admin) as probe:
        data = probe.get("/api/votes").json()
    assert data == {"votes": [], "summary": {
        "total": 0, "up": 0, "down": 0, "satisfaction": None}}
