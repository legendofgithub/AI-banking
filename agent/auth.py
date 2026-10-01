"""账号体系（8800 侧）：注册/登录/会话编排。

原语（哈希/表结构/支付密码核验/脱敏）在 bank_core/auth_core.py——
auth 表在 bank.db 里，MCP 工具与编排层共用，bank_core 不反向依赖 agent。

设计取舍（比赛周期内的"够用且专业"）：
- 不复活模板的 Auth.js+云库（已断线），账号直接落 bank.db——注册即 users 行，
  管理后台天然同步；
- 会话 = 随机 token 落 sessions 表（可撤销、7 天过期），前端 localStorage 携带；
- 登录密码策略：≥8 位且同时含大写/小写/数字/符号（服务端强校验，前端只做提示）；
- 支付密码：6 位纯数字，只用于动钱/敏感操作闸门，绝不写日志；
- 陈明（演示主用户）预置凭证在 ensure_demo_auth() 里幂等补齐。
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime, timedelta

from bank_core.auth_core import (hash_password, init_auth_tables,
                                 is_valid_id_card, mask_identifier,
                                 verify_password, verify_pay_password)
from bank_core.db import migrate_users_kyc

SESSION_DAYS = 7

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class AuthError(Exception):
    """注册/登录业务拒绝，信息可直接展示给用户。"""


# ---------------------------------------------------------------- 校验

def validate_login_password(password: str) -> None:
    """登录密码策略：≥8 位且含大写/小写/数字/符号。不满足抛 AuthError。"""
    if not isinstance(password, str) or len(password) < 8:
        raise AuthError("登录密码至少 8 位")
    if not re.search(r"[A-Z]", password):
        raise AuthError("登录密码必须包含大写字母")
    if not re.search(r"[a-z]", password):
        raise AuthError("登录密码必须包含小写字母")
    if not re.search(r"\d", password):
        raise AuthError("登录密码必须包含数字")
    if not re.search(r"[^A-Za-z0-9]", password):
        raise AuthError("登录密码必须包含符号（如 !@#$%）")


def validate_pay_password(password: str) -> None:
    if not isinstance(password, str) or not re.fullmatch(r"\d{6}", password):
        raise AuthError("支付密码必须是 6 位纯数字")


def classify_identifier(identifier: str) -> str:
    """返回 'phone' | 'email'，非法抛 AuthError。"""
    identifier = (identifier or "").strip()
    if re.fullmatch(r"1\d{10}", identifier):
        return "phone"
    if _EMAIL_RE.fullmatch(identifier):
        return "email"
    raise AuthError("账号必须是 1 开头的 11 位手机号或合法邮箱")


# ---------------------------------------------------------------- 初始化

def init_auth(conn: sqlite3.Connection) -> None:
    init_auth_tables(conn)
    # 注册现在写入 users.id_card/email——老库在此补列(auth_conn 直连不经过
    # init_db 的建表路径),保证 8800 注册链路在任何库状态下可用
    migrate_users_kyc(conn)


def ensure_demo_auth(conn: sqlite3.Connection, user_id: int = 1,
                     login_password: str = "Demo@12345",
                     pay_password: str = "888888") -> bool:
    """陈明预置凭证（幂等：已有 auth 行则跳过）。返回是否新写入。"""
    if conn.execute("SELECT 1 FROM auth_users WHERE user_id=?",
                    (user_id,)).fetchone():
        return False
    user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        return False
    identifier = user["phone"] if user["phone"] else f"user{user_id}@demo.local"
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute(
        """INSERT INTO auth_users (user_id, identifier, identifier_type,
           password_hash, pay_hash, created_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(user_id) DO NOTHING""",
        (user_id, identifier, "phone" if user["phone"] else "email",
         hash_password(login_password), hash_password(pay_password), now))
    conn.commit()
    return True


# ---------------------------------------------------------------- 注册/登录

def register(conn: sqlite3.Connection, *, real_name: str, id_card: str,
             phone: str, email: str = "", login_password: str,
             pay_password: str) -> dict:
    """注册：建 users 行 + 0 元活期账户 + auth 行（同一事务）。

    实名口径：真实姓名 + 18 位身份证（GB 11643 校验码核验，一张证件一个
    账户）；手机号必填（即登录账号，同步写 users.phone）；邮箱选填仅作
    联系方式，登录一律用手机号（老邮箱账号仍可按原 identifier 登录）。
    """
    real_name = (real_name or "").strip()
    if not (2 <= len(real_name) <= 20):
        raise AuthError("真实姓名需 2-20 个字符")
    id_card = (id_card or "").strip().upper()
    if not is_valid_id_card(id_card):
        raise AuthError("身份证号不合法（需 18 位且校验位正确）")
    if conn.execute("SELECT 1 FROM users WHERE id_card=?", (id_card,)).fetchone():
        raise AuthError("该身份证号已注册，请直接登录")
    phone = (phone or "").strip()
    if classify_identifier(phone) != "phone":
        raise AuthError("手机号必须是 1 开头的 11 位号码")
    email = (email or "").strip()
    if email and not _EMAIL_RE.fullmatch(email):
        raise AuthError("邮箱格式不正确")
    validate_login_password(login_password)
    validate_pay_password(pay_password)
    if conn.execute("SELECT 1 FROM auth_users WHERE identifier=?",
                    (phone,)).fetchone():
        raise AuthError("该账号已注册，请直接登录")
    now = datetime.now().isoformat(timespec="seconds")
    try:
        cur = conn.execute(
            "INSERT INTO users (name, phone, id_card, email, created_at) "
            "VALUES (?,?,?,?,?)",
            (real_name, phone, id_card, email, now))
        uid = cur.lastrowid
        conn.execute(
            """INSERT INTO accounts (user_id, type, name, currency,
               balance_cents, opened_at) VALUES (?,?,?,?,0,?)""",
            (uid, "checking", f"{real_name}的活期", "CNY", now))
        conn.execute(
            """INSERT INTO auth_users (user_id, identifier, identifier_type,
               password_hash, pay_hash, created_at) VALUES (?,?,?,?,?,?)""",
            (uid, phone, "phone",
             hash_password(login_password), hash_password(pay_password), now))
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise AuthError("注册失败：账号已存在") from exc
    return {"user_id": uid, "nickname": real_name, "real_name": real_name,
            "identifier": phone, "identifier_type": "phone",
            "id_card": id_card, "email": email}


def login(conn: sqlite3.Connection, *, identifier: str, password: str) -> dict:
    """登录：校验后发 7 天会话 token。"""
    identifier = (identifier or "").strip()
    row = conn.execute(
        """SELECT a.user_id, a.password_hash, u.name
           FROM auth_users a JOIN users u ON u.id=a.user_id
           WHERE a.identifier=?""", (identifier,)).fetchone()
    if not row or not verify_password(password or "", row["password_hash"]):
        raise AuthError("账号或密码不正确")
    token = secrets.token_hex(32)
    now = datetime.now()
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (token, row["user_id"], now.isoformat(timespec="seconds"),
         (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")))
    conn.execute("UPDATE auth_users SET last_login_at=? WHERE user_id=?",
                 (now.isoformat(timespec="seconds"), row["user_id"]))
    conn.commit()
    return {"token": token, "user_id": row["user_id"], "nickname": row["name"],
            "identifier": identifier}


def resolve_token(conn: sqlite3.Connection, token: str | None) -> dict | None:
    """token → {user_id, nickname}；无效/过期返回 None（观光模式）。"""
    if not token:
        return None
    row = conn.execute(
        """SELECT s.user_id, s.expires_at, u.name FROM sessions s
           JOIN users u ON u.id=s.user_id WHERE s.token=?""",
        (token,)).fetchone()
    if not row:
        return None
    if row["expires_at"] < datetime.now().isoformat(timespec="seconds"):
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        return None
    return {"user_id": row["user_id"], "nickname": row["name"]}


def logout(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()


__all__ = ["AuthError", "classify_identifier", "ensure_demo_auth", "init_auth",
           "login", "logout", "mask_identifier", "register", "resolve_token",
           "validate_login_password", "validate_pay_password",
           "verify_pay_password"]
