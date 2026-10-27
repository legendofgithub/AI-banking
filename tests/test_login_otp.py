"""登录短信验证码(2026-10-27 登录二次校验)测试。

口径:登录需要验证码,转账不加(支付密码单因子不变)。演示环境无真短信——
AGENT_OTP_ECHO(默认开)时第一步响应带 demo_code,前端展示"模拟短信"。

铁律对齐支付密码:库中只存 PBKDF2 哈希、审计/日志一律 ***、错 5 次作废挑战。
环境开关在测试里用 monkeypatch.setenv 控制(服务层调用点读环境,非 import 冻结)。
注意:发码有 60s 冷却,同一测试内不能连发两次第一步——helper 都复用同一挑战。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from agent.api import create_app
from test_agent_graph import _fake_llm, _mk_db


IDENT = "13800000000"
PASSWORD = "Demo@12345"


def _otp_client(tmp_path) -> tuple[TestClient, str]:
    """建 app(假模型)+ 演示账号(登录 Demo@12345)。OTP 默认开启。"""
    from agent import auth as _auth

    db = _mk_db(tmp_path)
    ac = sqlite3.connect(db)
    _auth.init_auth(ac)
    _auth.ensure_demo_auth(ac)
    ac.close()
    app = create_app(llm=_fake_llm("好的"), db_path=db,
                     checkpoint_path=tmp_path / "otp_ckpt.sqlite",
                     threads_path=tmp_path / "otp_threads.sqlite")
    return TestClient(app), str(db)


def _step1(client: TestClient) -> dict:
    r = client.post("/api/auth/login",
                    json={"identifier": IDENT, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()


def _verify(client: TestClient, c1: dict, code: str | None = None):
    """第二步:用第一步拿到的挑战提交验证码(默认用回显码)。"""
    return client.post("/api/auth/login",
                       json={"identifier": IDENT, "password": PASSWORD,
                             "challenge_id": c1["challenge_id"],
                             "code": code or c1["demo_code"]})


# ----------------------------------------------------------------- 两步主链路

def test_login_two_step_flow(tmp_path):
    """第一步:密码对 → 只发挑战不发 token;第二步:对码 → token 可用。"""
    client, _ = _otp_client(tmp_path)
    with client:
        c1 = _step1(client)
        assert c1["otp_required"] is True
        assert "token" not in c1
        assert c1["phone_masked"].startswith("138****")
        assert len(c1["demo_code"]) == 6
        r = _verify(client, c1)
        assert r.status_code == 200, r.text
        final = r.json()
        assert final["token"] and final["user_id"] == 1
        me = client.get("/api/auth/me",
                        headers={"x-bank-token": final["token"]})
        assert me.status_code == 200 and me.json()["user_id"] == 1


def test_wrong_password_no_challenge(tmp_path):
    """密码错 → 400 且不发挑战(验证码不暴露)。"""
    client, _ = _otp_client(tmp_path)
    with client:
        r = client.post("/api/auth/login",
                        json={"identifier": IDENT, "password": "Wrong@12345"})
        assert r.status_code == 400
        assert "不正确" in r.json()["error"]


def test_wrong_code_counts_then_exhausts(tmp_path):
    """错码计数(剩余次数提示)→ 5 次作废,之后对码也不放行。"""
    client, _ = _otp_client(tmp_path)
    with client:
        c1 = _step1(client)
        for i in range(4):
            r = _verify(client, c1, code="000001")
            assert r.status_code == 400
            assert f"还可尝试 {4 - i}" in r.json()["error"]
        r5 = _verify(client, c1, code="000001")
        assert "重新获取" in r5.json()["error"]
        r6 = _verify(client, c1)  # 作废后对码也拒
        assert r6.status_code == 400


def test_used_challenge_rejected(tmp_path):
    """验证码即焚:同一挑战二次使用 → 400。"""
    client, _ = _otp_client(tmp_path)
    with client:
        c1 = _step1(client)
        assert _verify(client, c1).status_code == 200
        r = _verify(client, c1)
        assert r.status_code == 400 and "已使用" in r.json()["error"]


def test_expired_code_rejected(tmp_path):
    """过期挑战:手工把 expires_at 回拨 → 400 要求重新获取。"""
    client, db = _otp_client(tmp_path)
    with client:
        c1 = _step1(client)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE login_otps SET expires_at='2000-01-01T00:00:00' "
                     "WHERE id=?", (c1["challenge_id"],))
        conn.commit()
        conn.close()
        r = _verify(client, c1)
        assert r.status_code == 400 and "过期" in r.json()["error"]


def test_resend_cooldown_429(tmp_path):
    """60s 冷却:连续第一步两次 → 429(前端据此驱动倒计时)。"""
    client, _ = _otp_client(tmp_path)
    with client:
        _step1(client)
        r = client.post("/api/auth/login",
                        json={"identifier": IDENT, "password": PASSWORD})
        assert r.status_code == 429 and "太频繁" in r.json()["error"]


# ----------------------------------------------------------------- 开关与安全

def test_otp_disabled_single_step(tmp_path, monkeypatch):
    """AGENT_LOGIN_OTP=0 → 单步登录照旧(回退开关),响应即 token。"""
    monkeypatch.setenv("AGENT_LOGIN_OTP", "0")
    client, _ = _otp_client(tmp_path)
    with client:
        r = client.post("/api/auth/login",
                        json={"identifier": IDENT, "password": PASSWORD})
        assert r.status_code == 200
        assert r.json().get("token")


def test_echo_off_hides_demo_code(tmp_path, monkeypatch):
    """AGENT_OTP_ECHO=0 → 响应不带 demo_code(演示"模拟短信"关闭)。"""
    monkeypatch.setenv("AGENT_OTP_ECHO", "0")
    client, _ = _otp_client(tmp_path)
    with client:
        c1 = _step1(client)
        assert "demo_code" not in c1
        assert c1["challenge_id"]  # 挑战仍在,只是不再回显码


def test_no_plaintext_code_in_db_or_audit(tmp_path):
    """库与审计都无明文码:code_hash 是 pbkdf2;审计入参 code=***。"""
    client, db = _otp_client(tmp_path)
    with client:
        c1 = _step1(client)
        assert _verify(client, c1).status_code == 200
    conn = sqlite3.connect(db)
    rows = conn.execute("SELECT code_hash, consumed_at FROM login_otps"
                        ).fetchall()
    assert rows and rows[0][0].startswith("pbkdf2_sha256")
    assert rows[0][1]  # 已即焚
    try:
        audits = conn.execute(
            "SELECT args_json FROM audit_log WHERE tool LIKE 'otp_%'"
        ).fetchall()
        for (args,) in audits:
            assert c1["demo_code"] not in args  # 该码明文绝不出现
    except sqlite3.OperationalError:
        pass  # 测试库未建 audit_log(软审计不阻断)
    conn.close()


def test_cross_user_challenge_rejected(tmp_path):
    """挑战与账号绑定:B 的账号拿 A 的挑战 → 无效。"""
    client, _ = _otp_client(tmp_path)
    with client:
        c1 = _step1(client)  # A 的挑战
        r = client.post("/api/auth/register",
                        json={"real_name": "用户乙",
                              "id_card": "110101199003078515",
                              "phone": "13800001111",
                              "login_password": "Passw0rd!",
                              "pay_password": "123456"})
        assert r.status_code == 200, r.text
        rr = client.post("/api/auth/login",
                         json={"identifier": "13800001111",
                               "password": "Passw0rd!",
                               "challenge_id": c1["challenge_id"],
                               "code": c1["demo_code"]})
        assert rr.status_code == 400 and "无效" in rr.json()["error"]
