"""账号体系测试：密码策略 / 实名注册登录会话 / 支付密码核验 / 展示脱敏。

编排层的登录态(login_required 拦截)与支付密码闸(错密码重问、封顶取消)
由 tests/test_agent_graph.py 覆盖(应答已改为支付密码)；这里测 auth 原语本身。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agent import auth
from bank_core.auth_core import is_valid_id_card, mask_id_card
from bank_core.db import init_db


@pytest.fixture()
def bank(tmp_path: Path) -> sqlite3.Connection:
    conn = init_db(tmp_path / "bank.db")
    conn.execute(
        "INSERT INTO users (id,name,phone,created_at) "
        "VALUES (1,'陈明','13800000000','2026-01-01T00:00:00')")
    conn.commit()
    auth.init_auth(conn)
    yield conn
    conn.close()


# ---------------------------------------------------------------- 密码策略

@pytest.mark.parametrize("bad,reason_part", [
    ("Ab1!", "至少 8 位"),
    ("abcdefgh1!", "大写字母"),
    ("ABCDEFG1!", "小写字母"),
    ("Abcdefgh!", "数字"),
    ("Abcdefg1", "符号"),
])
def test_login_password_policy_rejects(bad, reason_part):
    with pytest.raises(auth.AuthError) as e:
        auth.validate_login_password(bad)
    assert reason_part in str(e.value)


def test_login_password_policy_accepts():
    auth.validate_login_password("Demo@12345")


@pytest.mark.parametrize("bad", ["12345", "1234567", "12a456", "12 456"])
def test_pay_password_policy_rejects(bad):
    with pytest.raises(auth.AuthError):
        auth.validate_pay_password(bad)


def test_pay_password_policy_accepts():
    auth.validate_pay_password("888888")


def test_classify_identifier():
    assert auth.classify_identifier("13900001111") == "phone"
    assert auth.classify_identifier("a.b@test.com") == "email"
    for bad in ("23900001111", "12900", "a@b", "abc@def"):
        with pytest.raises(auth.AuthError):
            auth.classify_identifier(bad)


# ---------------------------------------------------------------- 身份证

def test_is_valid_id_card():
    assert is_valid_id_card("110101199003077774") is True
    assert is_valid_id_card("11010119900307774x") is True   # 小写 x 也接受
    for bad in ("110101199003077775",        # 校验位错
                "11010119900307777",         # 17 位
                "1101011990030777774",       # 19 位
                "1101011990030777a4",        # 非法字符
                ""):
        assert is_valid_id_card(bad) is False


def test_mask_id_card():
    assert mask_id_card("110101199003077774") == "110***********7774"
    assert mask_id_card("") == "—"


# ---------------------------------------------------------------- 注册/登录/会话

def test_register_creates_user_account_auth_row(bank):
    info = auth.register(bank, real_name="王五",
                         id_card="110101199003077774",
                         phone="13900002222", email="wangwu@test.com",
                         login_password="Passw0rd!",
                         pay_password="123456")
    assert info["user_id"] > 1
    uid = info["user_id"]
    # 实名信息落 users 行(管理台同步可见)
    row = bank.execute(
        "SELECT name, phone, id_card, email FROM users WHERE id=?", (uid,)
    ).fetchone()
    assert row["name"] == "王五" and row["phone"] == "13900002222"
    assert row["id_card"] == "110101199003077774"
    assert row["email"] == "wangwu@test.com"
    acct = bank.execute(
        "SELECT type, balance_cents FROM accounts WHERE user_id=?",
        (uid,)).fetchone()
    assert acct["type"] == "checking" and acct["balance_cents"] == 0
    arow = bank.execute("SELECT identifier, identifier_type FROM auth_users "
                        "WHERE user_id=?", (uid,)).fetchone()
    assert arow["identifier"] == "13900002222" and arow["identifier_type"] == "phone"


def test_register_email_optional(bank):
    """邮箱选填:不填可注册;填了格式错则拒绝。"""
    info = auth.register(bank, real_name="王五",
                         id_card="110101199003077774", phone="13900002222",
                         login_password="Passw0rd!", pay_password="123456")
    row = bank.execute("SELECT email FROM users WHERE id=?",
                       (info["user_id"],)).fetchone()
    assert row["email"] == ""
    with pytest.raises(auth.AuthError, match="邮箱"):
        auth.register(bank, real_name="李四",
                      id_card="110101198509128888", phone="13900005555",
                      email="not-an-email",
                      login_password="Passw0rd!", pay_password="123456")


@pytest.mark.parametrize("bad_id,reason_part", [
    ("110101199003077775", "身份证"),   # 校验位错
    ("11010119900307777", "身份证"),    # 位数错
    ("", "身份证"),
])
def test_register_rejects_bad_id_card(bank, bad_id, reason_part):
    with pytest.raises(auth.AuthError, match=reason_part):
        auth.register(bank, real_name="王五", id_card=bad_id,
                      phone="13900002222",
                      login_password="Passw0rd!", pay_password="123456")
    assert bank.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_register_rejects_duplicate_id_card(bank):
    """一张证件一个账户:同身份证不同手机号也拒绝。"""
    auth.register(bank, real_name="王五", id_card="110101199003077774",
                  phone="13900002222",
                  login_password="Passw0rd!", pay_password="123456")
    with pytest.raises(auth.AuthError, match="身份证"):
        auth.register(bank, real_name="王五", id_card="110101199003077774",
                      phone="13900003333",
                      login_password="Passw0rd!", pay_password="123456")


def test_register_rejects_duplicate_identifier(bank):
    kw = dict(real_name="王五", id_card="110101199003077774",
              phone="13900002222",
              login_password="Passw0rd!", pay_password="123456")
    auth.register(bank, **kw)
    with pytest.raises(auth.AuthError, match="已注册"):
        auth.register(bank, **kw)


def test_register_requires_phone_not_email(bank):
    """手机号必填:拿邮箱当 phone 传要被拒。"""
    with pytest.raises(auth.AuthError, match="手机号"):
        auth.register(bank, real_name="王五", id_card="110101199003077774",
                      phone="a.b@test.com",
                      login_password="Passw0rd!", pay_password="123456")


def test_register_rejects_weak_password_before_any_write(bank):
    with pytest.raises(auth.AuthError):
        auth.register(bank, real_name="李四", id_card="110101198509128888",
                      phone="13900003333",
                      login_password="weakpass", pay_password="123456")
    assert bank.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1  # 只有陈明


def test_login_flow_and_session(bank):
    auth.register(bank, real_name="王五", id_card="110101198509128888",
                  phone="13900003333",
                  login_password="Passw0rd!", pay_password="123456")
    with pytest.raises(auth.AuthError):
        auth.login(bank, identifier="13900003333", password="Wrong@123")
    sess = auth.login(bank, identifier="13900003333", password="Passw0rd!")
    me = auth.resolve_token(bank, sess["token"])
    assert me == {"user_id": sess["user_id"], "nickname": "王五"}
    # 登出后会话失效
    auth.logout(bank, sess["token"])
    assert auth.resolve_token(bank, sess["token"]) is None
    assert auth.resolve_token(bank, None) is None  # 观光


def test_expired_session_rejected(bank):
    from datetime import datetime, timedelta
    auth.register(bank, real_name="王五", id_card="330106199512066662",
                  phone="13900004444",
                  login_password="Passw0rd!", pay_password="123456")
    sess = auth.login(bank, identifier="13900004444", password="Passw0rd!")
    # 人为把过期时间拨到过去
    bank.execute(
        "UPDATE sessions SET expires_at=? WHERE token=?",
        ((datetime.now() - timedelta(days=1)).isoformat(timespec="seconds"),
         sess["token"]))
    bank.commit()
    assert auth.resolve_token(bank, sess["token"]) is None


# ---------------------------------------------------------------- 支付密码与脱敏

def test_verify_pay_password(bank):
    auth.ensure_demo_auth(bank)  # 陈明:支付 888888
    assert auth.verify_pay_password(bank, 1, "888888") is True
    assert auth.verify_pay_password(bank, 1, "888887") is False
    assert auth.verify_pay_password(bank, 999, "888888") is False  # 无 auth 行
    assert auth.ensure_demo_auth(bank) is False  # 幂等


def test_mask_identifier():
    assert auth.mask_identifier("13900001111") == "139****1111"
    assert auth.mask_identifier("a.b@test.com") == "a***@test.com"
    assert auth.mask_identifier("") == "—"
