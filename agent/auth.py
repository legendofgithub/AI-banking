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
from typing import Any

from bank_core.auth_core import (hash_password, init_auth_tables,
                                 is_id_card_shape, mask_identifier,
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
    # 演示阶段临时放宽：只校验 18 位形状，不校验 GB 11643 校验码。
    # 恢复实名 KYC 时换回 bank_core.auth_core.is_valid_id_card(id_card)。
    if not is_id_card_shape(id_card):
        raise AuthError("身份证号需为 18 位（测试阶段不校验校验码）")
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
        # 唯一约束三处:auth_users.identifier(手机号)、users.id_card(一证一户
        # 部分唯一索引,先查后插的竞态兜底)、accounts 主键——统一转友好提示
        raise AuthError("注册失败:该手机号或身份证号已注册,请直接登录") from exc
    return {"user_id": uid, "nickname": real_name, "real_name": real_name,
            "identifier": phone, "identifier_type": "phone",
            "id_card": id_card, "email": email}


def issue_session(conn: sqlite3.Connection, user_id: int) -> dict:
    """为已验证身份的用户直接签发 7 天会话 token。

    登录(密码校验后)与手机扫码登录(票据校验后)共用;两处只负责
    "证明是本人",会话的生成/过期/last_login 一律走这里,口径不漂移。
    """
    row = conn.execute(
        """SELECT u.name, a.identifier FROM users u
           LEFT JOIN auth_users a ON a.user_id=u.id WHERE u.id=?""",
        (user_id,)).fetchone()
    if not row:
        raise AuthError("用户不存在")
    token = secrets.token_hex(32)
    now = datetime.now()
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?,?,?,?)",
        (token, user_id, now.isoformat(timespec="seconds"),
         (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")))
    conn.execute("UPDATE auth_users SET last_login_at=? WHERE user_id=?",
                 (now.isoformat(timespec="seconds"), user_id))
    conn.commit()
    return {"token": token, "user_id": user_id, "nickname": row["name"],
            "identifier": row["identifier"] or ""}


def login(conn: sqlite3.Connection, *, identifier: str, password: str) -> dict:
    """登录：校验后发 7 天会话 token。"""
    identifier = (identifier or "").strip()
    row = conn.execute(
        """SELECT a.user_id, a.password_hash, u.name
           FROM auth_users a JOIN users u ON u.id=a.user_id
           WHERE a.identifier=?""", (identifier,)).fetchone()
    if not row or not verify_password(password or "", row["password_hash"]):
        raise AuthError("账号或密码不正确")
    sess = issue_session(conn, row["user_id"])
    # identifier 返回用户亲手输入的原样值(保持旧口径,不回查库里账号)
    return {**sess, "identifier": identifier}


# ---------------------------------------------------------------- 登录短信验证码(2026-10-27 二次校验)
#
# 口径(用户拍板):登录需要验证码,转账不加(转账仍是支付密码单因子)。
# 演示环境发不出真短信——验证码通过登录第二步卡片的"模拟短信"回显给用户
# (AGENT_OTP_ECHO=1 时 API 响应带 demo_code),管理台另有"短信记录"页签。
# 铁律沿用支付密码:库中只存 PBKDF2 哈希,审计/日志一律 ***。

OTP_MAX_ATTEMPTS = 5      # 同一挑战码错 5 次作废(与支付密码 4 次锁定同量级)


def _otp_env(name: str, default: str) -> str:
    """调用点读取(非 import 时冻结),测试可 monkeypatch.setenv 生效。"""
    import os
    return os.environ.get(name, default)


def _otp_ttl_seconds() -> int:
    try:
        return max(30, int(_otp_env("AGENT_OTP_TTL", "120")))
    except ValueError:
        return 120


def _otp_echo_enabled() -> bool:
    return _otp_env("AGENT_OTP_ECHO", "1") not in ("0", "false", "False")


def _otp_audit(conn: sqlite3.Connection, action: str, phone: str,
               detail: dict) -> None:
    """OTP 审计(软失败):登录链路绝不被审计缺表/写失败阻断。

    码与手机号都不入明文——码恒为 ***,手机号走 mask_identifier。
    """
    try:
        from bank_core.db import audit
        audit(conn, action, {"phone": mask_identifier(phone), "code": "***"},
              detail, risk="MED")
    except Exception:  # noqa: BLE001 —— 审计是旁路
        pass


def _otp_phone(conn: sqlite3.Connection, user_id: int, identifier: str) -> str:
    """验证码接收手机号:identifier 本身是手机号则用之,否则回退 users.phone。"""
    if re.fullmatch(r"1\d{10}", identifier or ""):
        return identifier
    row = conn.execute("SELECT phone FROM users WHERE id=?",
                       (user_id,)).fetchone()
    if row and row["phone"]:
        return row["phone"]
    raise AuthError("该账号未绑定手机号，无法接收验证码")


def send_login_code(conn: sqlite3.Connection, *, identifier: str,
                    password: str) -> dict:
    """登录第一步：密码通过即签发验证码挑战（尚不发会话）。

    返回 {challenge_id, phone_masked, expires_in, demo_code?};
    demo_code 仅在 AGENT_OTP_ECHO 开启时携带（演示"模拟短信"）。
    重发冷却 60s——冷却内重复请求直接拒绝（前端倒计时只是引导，服务端才是闸）。
    """
    identifier = (identifier or "").strip()
    row = conn.execute(
        """SELECT a.user_id, a.password_hash, u.name
           FROM auth_users a JOIN users u ON u.id=a.user_id
           WHERE a.identifier=?""", (identifier,)).fetchone()
    if not row or not verify_password(password or "", row["password_hash"]):
        raise AuthError("账号或密码不正确")
    phone = _otp_phone(conn, row["user_id"], identifier)
    now = datetime.now()
    # 冷却:该手机号 60s 内已发过且未作废 → 拒绝(防轰炸;演示讲解可等可重登)
    recent = conn.execute(
        """SELECT created_at FROM login_otps
           WHERE phone=? AND consumed_at IS NULL
           ORDER BY id DESC LIMIT 1""", (phone,)).fetchone()
    if recent:
        age = (now - datetime.fromisoformat(recent["created_at"])).total_seconds()
        if age < 60:
            raise AuthError("验证码发送太频繁，请 1 分钟后再试")
    code = f"{secrets.randbelow(1_000_000):06d}"
    ttl = _otp_ttl_seconds()
    expires_at = (now + timedelta(seconds=ttl)).isoformat(timespec="seconds")
    cur = conn.execute(
        """INSERT INTO login_otps (user_id, phone, code_hash, attempts,
           created_at, expires_at) VALUES (?,?,?,0,?,?)""",
        (row["user_id"], phone, hash_password(code),
         now.isoformat(timespec="seconds"), expires_at))
    conn.commit()
    _otp_audit(conn, "otp_send", phone,
               {"user_id": row["user_id"], "challenge_id": cur.lastrowid})
    out = {"otp_required": True, "challenge_id": cur.lastrowid,
           "phone_masked": mask_identifier(phone), "expires_in": ttl}
    if _otp_echo_enabled():
        out["demo_code"] = code
    return out


def verify_login_code(conn: sqlite3.Connection, *, identifier: str,
                      challenge_id: Any, code: str) -> dict:
    """登录第二步：校验验证码，成功返回可签发会话的用户信息（即焚）。

    挑战与账号绑定（跨用户重放无效）；错码累计 attempts，达 5 次作废；
    成功置 consumed_at，同一挑战不可二次使用。
    """
    identifier = (identifier or "").strip()
    arow = conn.execute(
        "SELECT user_id FROM auth_users WHERE identifier=?",
        (identifier,)).fetchone()
    try:
        cid = int(str(challenge_id))
    except (TypeError, ValueError):
        raise AuthError("验证码挑战无效，请重新登录") from None
    if not arow:
        raise AuthError("账号或密码不正确")
    row = conn.execute(
        "SELECT * FROM login_otps WHERE id=? AND user_id=?",
        (cid, arow["user_id"])).fetchone()
    if not row:
        raise AuthError("验证码挑战无效，请重新登录")
    if row["consumed_at"]:
        raise AuthError("该验证码已使用，请重新登录")
    if row["attempts"] >= OTP_MAX_ATTEMPTS:
        raise AuthError("尝试次数过多，请重新获取验证码")
    if row["expires_at"] < datetime.now().isoformat(timespec="seconds"):
        raise AuthError("验证码已过期，请重新获取")
    if not verify_password(str(code or ""), row["code_hash"]):
        used = int(row["attempts"]) + 1
        conn.execute("UPDATE login_otps SET attempts=? WHERE id=?",
                     (used, cid))
        conn.commit()
        _otp_audit(conn, "otp_verify", row["phone"],
                   {"ok": False, "attempts": used})
        if used >= OTP_MAX_ATTEMPTS:
            raise AuthError("尝试次数过多，请重新获取验证码")
        raise AuthError(
            f"验证码不正确，还可尝试 {OTP_MAX_ATTEMPTS - used} 次")
    conn.execute("UPDATE login_otps SET consumed_at=? WHERE id=?",
                 (datetime.now().isoformat(timespec="seconds"), cid))
    conn.commit()
    _otp_audit(conn, "otp_verify", row["phone"], {"ok": True})
    urow = conn.execute("SELECT name FROM users WHERE id=?",
                        (arow["user_id"],)).fetchone()
    return {"user_id": arow["user_id"], "name": urow["name"] if urow else "",
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


__all__ = ["AuthError", "OTP_MAX_ATTEMPTS", "classify_identifier",
           "ensure_demo_auth", "init_auth", "issue_session", "login",
           "logout", "mask_identifier", "register", "resolve_token",
           "send_login_code", "validate_login_password",
           "validate_pay_password", "verify_login_code",
           "verify_pay_password"]
