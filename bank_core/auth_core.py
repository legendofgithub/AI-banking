"""账号核心原语（bank_core 层）：哈希/校验/表结构/支付密码核验/展示脱敏。

放这一层的原因：auth_users/sessions 表在 bank.db 里，MCP 工具
（verify_pay_password）与编排层（注册/登录会话）都要用——bank_core 不能
反向依赖 agent 包，所以原语下沉在这里，agent/auth.py 薄封装复用。

安全要点：
- PBKDF2-SHA256 + 每密码随机盐（标准库，零依赖）；
- 支付密码只核验不落日志（调用方审计时入参脱敏）；
- 展示脱敏按 JR/T 0171 C2 级：139****0000 / a***@domain.com。
"""

from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3

PBKDF2_ITERATIONS = 120_000

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")

# GB 11643-1999：前 17 位本体码 × 权重求和 mod 11 → 校验码映射
_ID_CARD_RE = re.compile(r"^\d{17}[\dXx]$")
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CODES = "10X98765432"


# ---------------------------------------------------------------- 哈希

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return secrets.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- 表

def init_auth_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS auth_users (
        user_id         INTEGER PRIMARY KEY REFERENCES users(id),
        identifier      TEXT NOT NULL UNIQUE,
        identifier_type TEXT NOT NULL CHECK (identifier_type IN ('phone','email')),
        password_hash   TEXT NOT NULL,
        pay_hash        TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        last_login_at   TEXT
    );
    CREATE TABLE IF NOT EXISTS sessions (
        token      TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    """)
    conn.commit()


def verify_pay_password(conn: sqlite3.Connection, user_id: int,
                        pay_password: str) -> bool:
    """支付密码核验（MCP 工具与闸门共用；调用方负责日志脱敏，绝不入明文）。"""
    row = conn.execute(
        "SELECT pay_hash FROM auth_users WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return False
    return verify_password(pay_password or "", row["pay_hash"])


# ---------------------------------------------------------------- 展示脱敏

def mask_identifier(identifier: str) -> str:
    """JR/T 0171 C2 级展示脱敏：139****0000 / a***@domain.com。"""
    if not identifier:
        return "—"
    if "@" in identifier:
        local, _, domain = identifier.partition("@")
        return f"{local[:1]}***@{domain}"
    if len(identifier) == 11:
        return f"{identifier[:3]}****{identifier[-4:]}"
    return identifier[:2] + "****" if len(identifier) > 4 else identifier


def mask_id_card(id_card: str) -> str:
    """身份证展示脱敏（与手机号同风格）：前 3 后 4，中间打星。"""
    if not id_card:
        return "—"
    if len(id_card) != 18:
        return id_card[:2] + "****"
    return f"{id_card[:3]}{'*' * 11}{id_card[-4:]}"


# ---------------------------------------------------------------- 身份证

def is_valid_id_card(id_card: str) -> bool:
    """18 位二代身份证格式 + GB 11643 校验码核验（大小写 X 均接受）。"""
    if not isinstance(id_card, str) or not _ID_CARD_RE.fullmatch(id_card):
        return False
    total = sum(int(id_card[i]) * _ID_WEIGHTS[i] for i in range(17))
    return _ID_CHECK_CODES[total % 11] == id_card[17].upper()
