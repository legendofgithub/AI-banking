"""管理后台 API（零 LLM）：用户汇总 + 单用户监控维护，双库留痕。

  python -m bank_core.admin_api            # http://127.0.0.1:8789

定位（与顾客端三个端口互补）：
- 3000 顾客对话（带 LLM）｜ 8800 agent 编排 ｜ 8788 网银演示页
- 8789  本管理台：纯确定性 REST + 静态管理页，一个模型调用都没有。

主页 = 用户汇总（GET /api/users，按注册时间筛选），双击用户进入个人管理台；
所有个人接口带可选 user_id 参数（默认 1，兼容单用户时期的调用与测试）。

数据来源：
- data/money_ledger.sqlite  资金流水记录（bank_core/recorder.py 双写钩子落库）
- data/change_log.sqlite    功能修改记录（同上）
- data/bank.db              业务库直读（汇总/订单/计划列表）+ 复用服务层做维护操作

维护操作的铁律：全部走既有 Service（业务校验+audit_log 一分不少），并在
admin_scope() 内执行——recorder 落库时 operator='admin'，管理员自己的每个
写操作与前端 agent 的留痕同库同表、可区分可审计。

工厂模式（create_admin_app）：模块级 app 供 uvicorn；测试注入临时库，
不触碰 data/bank.db（与 agent/api.py 的 create_app 同套路）。
请求模型必须放模块级——工厂内局部 BaseModel 对 FastAPI 注解解析不可见
（get_type_hints 只看全局命名空间，局部类会被当成 query 参数 → 422）。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import recorder
from .auth_core import mask_identifier, mask_id_card, verify_password
from .analysis import AnalysisService
from .db import audit, connect, migrate_cards_bound, migrate_users_kyc, now_iso
from .events import EventService
from .ledger import LedgerError, LedgerService
from .linkage import LinkageError, LinkageService
from .money import cents_to_yuan, yuan_to_cents
from .recorder import admin_scope, record_change, record_money
from .wealth import WealthError, WealthService


def _err(exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


# 演示口令(管理台凭证直显用):库里只存加盐哈希,明文不可找回;此处只把
# "哈希能通过该口令验证"的账号明文回显,其余账号回显空串(前端显示"—")。
# scripts/grant_demo_logins.py 播种的就是这个口令,两边常量必须一致。
DEMO_LOGIN_PASSWORD = "Demo@12345"


# ---------------------------------------------------------------- 请求模型(模块级)
class ContactIn(BaseModel):
    name: str
    phone: str
    note: str = ""


class CardStatusIn(BaseModel):
    status: str


class CardLimitsIn(BaseModel):
    daily_limit_yuan: str | None = None
    per_tx_limit_yuan: str | None = None


class AdjustIn(BaseModel):
    account_id: int = 1
    amount_yuan: str  # 可负（扣）可正（补）
    reason: str


class TransferLimitsIn(BaseModel):
    """账户级转账限额覆盖；空串/0 = 恢复系统默认。"""

    daily_limit_yuan: str = ""
    single_limit_yuan: str = ""


def _parse_limit(value: str, label: str) -> int:
    v = (value or "").strip()
    if v in ("", "0", "0.00"):
        return 0
    try:
        cents = yuan_to_cents(v)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label}: {exc}")
    if cents > 100_000_000:  # 1,000,000 元上限，防手滑输入天文数字
        raise HTTPException(status_code=400,
                            detail=f"{label}不能超过 1,000,000 元")
    return cents


# 注册时间筛选窗口：within 参数 → 天数
_WITHIN_DAYS = {"week": 7, "month": 30, "quarter": 90, "year": 365}


def _day_bound(value: str, *, end: bool) -> str:
    """把 YYYY-MM-DD 转成记录库可比的 ISO 时间边界（date_to 取次日零点左闭右开）。"""
    try:
        day = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式应为 YYYY-MM-DD")
    if end:
        day += timedelta(days=1)
    return day.isoformat(timespec="seconds")


def create_admin_app(db_path: str | Path | None = None) -> FastAPI:
    """构造管理后台应用。db_path=None 走 BANK_CORE_DB/默认库（生产）。"""
    conn = connect(db_path)
    # 验证码记录等跨连接只读要拿库文件路径(connect 不回传,这里另开一次解析)
    from bank_core.db import default_db_path as _ddp
    _db_file = Path(db_path) if db_path else Path(_ddp())
    # users.id_card/email 老库补列(管理台 connect 直连,不经 init_db)
    migrate_users_kyc(conn)
    migrate_cards_bound(conn)  # 演示绑卡列

    def _uid(user_id: int | None) -> int:
        if user_id and not conn.execute(
                "SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise HTTPException(status_code=404, detail=f"用户 {user_id} 不存在")
        return user_id or 1

    def _nickname_of(user_id: int) -> str:
        """记录库按昵称过滤用（与 recorder._nickname 同规则：user1 允许 env 覆盖）。"""
        override = os.environ.get("DEMO_USER_NICKNAME")
        if user_id == 1 and override:
            return override
        row = conn.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
        return row["name"] if row else f"user-{user_id}"

    class _Svcs:
        """按用户构造的服务组——所有既有 Service 本来就吃 user_id。"""

        def __init__(self, uid: int):
            self.ledger = LedgerService(conn, uid)
            self.wealth = WealthService(conn, uid)
            self.analysis = AnalysisService(conn, uid)
            self.events = EventService(conn, uid)
            self.linkage = LinkageService(conn, uid)

    def _rec_rows(path: Path, sql: str, args: tuple) -> list[sqlite3.Row]:
        """读记录库；库或表尚不存在（还没有任何写入过）时返回空——监控页不因此 500。"""
        rc = sqlite3.connect(path, check_same_thread=False)
        rc.row_factory = sqlite3.Row
        try:
            return rc.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            rc.close()

    def _account_map(rows: list[sqlite3.Row]) -> dict[int, sqlite3.Row]:
        """调账记录只存 account_id；回业务库映射到当前用户（改名/重名都能落到正确的人）。"""
        ids = sorted({int(r["account_id"]) for r in rows})
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        return {int(r["id"]): r for r in conn.execute(
            f"""SELECT a.id, a.user_id, a.name AS account_name,
                       a.type AS account_type, u.name AS nickname
                FROM accounts a JOIN users u ON u.id = a.user_id
                WHERE a.id IN ({marks})""", tuple(ids))}

    def _limits_of(uid: int) -> dict:
        """有效限额 + 默认值/是否覆盖，供总览业务参数卡与保存响应共用。"""
        lim = _Svcs(uid).ledger.transfer_limits()
        return {
            "daily_limit_yuan": cents_to_yuan(lim["daily_limit_cents"]),
            "single_limit_yuan": cents_to_yuan(lim["single_limit_cents"]),
            "daily_overridden": lim["daily_overridden"],
            "single_overridden": lim["single_overridden"],
            "default_daily_limit_yuan": cents_to_yuan(
                lim["default_daily_limit_cents"]),
            "default_single_limit_yuan": cents_to_yuan(
                lim["default_single_limit_cents"]),
        }

    app = FastAPI(title="AI Banking 管理后台", version="0.2.0")

    # ------------------------------------------------------------ 健康与用户汇总
    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "service": "admin-api", "llm": "none"}

    @app.get("/api/users")
    def users(within: str = "all") -> dict:
        """主页数据：全部用户汇总。within: week|month|quarter|year|all——按注册时间筛选。"""
        cutoff = ""
        days = _WITHIN_DAYS.get(within)
        if days:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat(
                timespec="seconds")
        sql = """SELECT u.id, u.name, u.phone, u.id_card, u.email, u.created_at,
                        u.transfer_locked, u.pay_fail_count,
                        (SELECT COUNT(*) FROM accounts a WHERE a.user_id=u.id) accounts_n,
                        (SELECT COALESCE(SUM(a.balance_cents),0) FROM accounts a
                          WHERE a.user_id=u.id) balance_cents,
                        (SELECT COUNT(*) FROM contacts c WHERE c.user_id=u.id) contacts_n,
                        (SELECT COUNT(*) FROM transfer_orders o WHERE o.user_id=u.id) orders_n,
                        (SELECT COUNT(*) FROM transactions t JOIN accounts a2
                          ON t.account_id=a2.id WHERE a2.user_id=u.id) tx_n,
                        (SELECT MAX(t.ts) FROM transactions t JOIN accounts a2
                          ON t.account_id=a2.id WHERE a2.user_id=u.id) last_active
                 FROM users u"""
        args: tuple = ()
        if cutoff:
            sql += " WHERE u.created_at >= ?"
            args = (cutoff,)
        sql += " ORDER BY u.created_at ASC"
        rows = conn.execute(sql, args).fetchall()
        # 注册信息(auth 行):对外字段脱敏展示 JR/T 0171 C2 级;老演示用户无
        # auth 为 None。演示凭证(登录账号全量 + 登录密码)仅当哈希能通过
        # DEMO_LOGIN_PASSWORD 验证才明文回显——哈希不可逆,验证不过就只能显示"—",
        # 绝不瞎编;scripts/grant_demo_logins.py 可把全库统一成该演示口令。
        auth_by_uid = {r["user_id"]: r for r in conn.execute(
            "SELECT user_id, identifier, identifier_type, password_hash, "
            "last_login_at FROM auth_users")}
        # 双库以昵称为键，按昵称计数
        money_n: dict[str, int] = {}
        for r in _rec_rows(recorder.money_db_path(conn),
                           "SELECT nickname, COUNT(*) n FROM money_records "
                           "GROUP BY nickname", ()):
            money_n[r["nickname"]] = r["n"]
        change_n: dict[str, int] = {}
        for r in _rec_rows(recorder.change_db_path(conn),
                           "SELECT nickname, COUNT(*) n FROM change_records "
                           "GROUP BY nickname", ()):
            change_n[r["nickname"]] = r["n"]
        def _auth_of(uid: int) -> dict | None:
            a = auth_by_uid.get(uid)
            if not a:
                return None
            demo_ok = verify_password(DEMO_LOGIN_PASSWORD, a["password_hash"])
            return {"identifier_masked": mask_identifier(a["identifier"]),
                    "identifier_type": a["identifier_type"],
                    "identifier": a["identifier"],
                    "login_password": DEMO_LOGIN_PASSWORD if demo_ok else "",
                    "last_login_at": a["last_login_at"]}

        out = [{
            "user_id": r["id"], "nickname": r["name"], "phone": r["phone"],
            "id_card_masked": mask_id_card(r["id_card"]),
            "email_masked": mask_identifier(r["email"]) if r["email"] else "",
            "transfer_locked": bool(r["transfer_locked"]),
            "pay_fail_count": r["pay_fail_count"],
            "auth": _auth_of(r["id"]),
            "created_at": r["created_at"], "accounts": r["accounts_n"],
            "total_balance_yuan": cents_to_yuan(r["balance_cents"]),
            "contacts": r["contacts_n"], "orders": r["orders_n"],
            "transactions": r["tx_n"], "money_records": money_n.get(r["name"], 0),
            "change_records": change_n.get(r["name"], 0),
            "last_active": r["last_active"],
        } for r in rows]
        return {"users": out, "within": within,
                "total_users": conn.execute(
                    "SELECT COUNT(*) n FROM users").fetchone()["n"]}

    # ------------------------------------------------------------ 单用户监控
    @app.get("/api/overview")
    def overview(user_id: int | None = None) -> dict:
        uid = _uid(user_id)
        s = _Svcs(uid)
        accounts = s.ledger.list_accounts()
        rows = conn.execute(
            """SELECT id, to_name, amount_cents, status, created_at, memo FROM transfer_orders
               WHERE user_id=? AND status IN ('pending_confirm','scheduled')
               ORDER BY id DESC LIMIT 20""", (uid,)).fetchall()
        pending_orders = [{"id": r["id"], "to_name": r["to_name"],
                           "amount_yuan": cents_to_yuan(r["amount_cents"]),
                           "status": r["status"], "created_at": r["created_at"],
                           "memo": r["memo"]}
                          for r in rows]
        holdings = s.wealth.get_holdings()
        due = conn.execute(
            """SELECT id, run_at, payload_json FROM scheduled_tasks
               WHERE user_id=? AND status='pending' ORDER BY run_at LIMIT 10""",
            (uid,)).fetchall()
        due_tasks = []
        for r in due:
            try:
                payload = json.loads(r["payload_json"])
            except json.JSONDecodeError:
                payload = {}
            due_tasks.append({"id": r["id"], "run_at": r["run_at"],
                              "payload_title": str(payload.get("title", ""))})
        today = now_iso()[:10]
        nick = _nickname_of(uid)
        tm = _rec_rows(
            recorder.money_db_path(conn),
            "SELECT COUNT(*) n, COALESCE(SUM(delta_cents),0) d FROM money_records "
            "WHERE ts LIKE ? AND nickname=?", (f"{today}%", nick))
        cc = _rec_rows(
            recorder.change_db_path(conn),
            "SELECT COUNT(*) n FROM change_records WHERE ts LIKE ? AND nickname=?",
            (f"{today}%", nick))
        active_plans = conn.execute(
            "SELECT COUNT(*) n FROM linkage_plans WHERE status='active' AND user_id=?",
            (uid,)).fetchone()["n"]
        t0 = tm[0] if tm else {"n": 0, "d": 0}
        c0 = cc[0] if cc else {"n": 0}
        return {
            "nickname": nick,
            "accounts": [{"id": a["id"], "name": a["name"], "type": a["type"],
                          "balance_yuan": a["balance_yuan"]} for a in accounts],
            "pending_orders": pending_orders,
            "holdings": holdings,
            "due_tasks": due_tasks,
            "stats": {"today_money_count": t0["n"],
                      "today_money_delta_yuan": cents_to_yuan(t0["d"]),
                      "change_count_24h": c0["n"], "active_plans": active_plans},
            "limits": _limits_of(uid),
        }

    @app.post("/api/transfer-limits")
    def set_transfer_limits(body: TransferLimitsIn,
                            user_id: int | None = None) -> dict:
        """账户级转账限额（每日/单笔）：空串或 0 恢复系统默认，保存后立即生效。"""
        uid = _uid(user_id)
        daily = _parse_limit(body.daily_limit_yuan, "每日转账限额")
        single = _parse_limit(body.single_limit_yuan, "单笔转账限额")
        old = conn.execute(
            "SELECT daily_transfer_limit_cents d, single_transfer_limit_cents s "
            "FROM users WHERE id=?", (uid,)).fetchone()
        conn.execute(
            "UPDATE users SET daily_transfer_limit_cents=?, "
            "single_transfer_limit_cents=? WHERE id=?",
            (daily, single, uid))
        conn.commit()
        result = {"user_id": uid, "limits": _limits_of(uid)}
        audit(conn, "admin_set_transfer_limits",
              {"user_id": uid, "daily_cents": daily,
               "single_cents": single},
              {"old_daily_cents": old["d"] if old else 0,
               "old_single_cents": old["s"] if old else 0, **result},
              risk="MED")
        with admin_scope():
            record_change(
                conn, uid, category="admin", action="transfer_limits",
                target=_nickname_of(uid),
                detail={"daily_limit_yuan": cents_to_yuan(daily) if daily else "默认",
                        "single_limit_yuan":
                            cents_to_yuan(single) if single else "默认"})
        return result

    @app.get("/api/money-records")
    def money_records(limit: int = 100, user_id: int | None = None) -> dict:
        lim = max(1, min(limit, 500))
        rows = _rec_rows(
            recorder.money_db_path(conn),
            "SELECT * FROM money_records WHERE nickname=? ORDER BY id DESC LIMIT ?",
            (_nickname_of(_uid(user_id)), lim))
        return {"records": [
            {"id": r["id"], "nickname": r["nickname"], "ts": r["ts"],
             "tool": r["tool"], "delta_yuan": cents_to_yuan(r["delta_cents"]),
             "before_yuan": cents_to_yuan(r["before_cents"]),
             "after_yuan": cents_to_yuan(r["after_cents"]),
             "account_id": r["account_id"], "ref": r["ref"], "note": r["note"],
             "operator": r["operator"]} for r in rows]}

    @app.get("/api/change-records")
    def change_records(limit: int = 100, category: str | None = None,
                       user_id: int | None = None) -> dict:
        lim = max(1, min(limit, 500))
        nick = _nickname_of(_uid(user_id))
        if category:
            sql = ("SELECT * FROM change_records WHERE category=? AND nickname=? "
                   "ORDER BY id DESC LIMIT ?")
            args = (category, nick, lim)
        else:
            sql = ("SELECT * FROM change_records WHERE nickname=? "
                   "ORDER BY id DESC LIMIT ?")
            args = (nick, lim)
        rows = _rec_rows(recorder.change_db_path(conn), sql, args)
        return {"records": [
            {"id": r["id"], "nickname": r["nickname"], "ts": r["ts"],
             "category": r["category"], "action": r["action"],
             "target": r["target"], "detail": r["detail"],
             "operator": r["operator"]} for r in rows]}

    # ------------------------------------------------------------ 联系人维护
    @app.get("/api/contacts")
    def contacts(user_id: int | None = None) -> dict:
        return {"contacts": _Svcs(_uid(user_id)).ledger.list_contacts()}

    @app.post("/api/contacts")
    def add_contact(body: ContactIn, user_id: int | None = None):
        try:
            with admin_scope():
                return _Svcs(_uid(user_id)).ledger.add_contact(
                    body.name, body.phone, body.note)
        except LedgerError as e:
            return _err(e)

    # ------------------------------------------------------------ 用户满意度(消息投票)
    @app.get("/api/votes")
    def votes(limit: int = 50) -> dict:
        """最近的消息投票(点赞/点踩):8800 threads 库 message_votes 表。

        跨库只读(表不存在=还没有人投过票,返回空);汇总出满意度供总览。
        """
        n = max(1, min(int(limit or 50), 200))
        thr = Path(os.environ.get("AGENT_THREADS_DB")
                   or (Path(__file__).resolve().parents[1]
                       / "data" / "agent_threads.sqlite"))
        rows = _rec_rows(
            thr,
            """SELECT chat_id, message_id, is_upvoted, updated_at
               FROM message_votes ORDER BY updated_at DESC LIMIT ?""", (n,))
        ups = sum(1 for r in rows if r["is_upvoted"])
        return {"votes": [dict(r) for r in rows], "summary": {
            "total": len(rows), "up": ups, "down": len(rows) - ups,
            "satisfaction": round(ups * 100.0 / len(rows), 1) if rows else None}}

    # 权限边界(2026-10-01 定):管理员对用户联系人=只读+代客录入,不提供
    # 删除——个人数据不容后台销毁,删除权属用户本人(将来经对话工具+闸门)。
    # 原先的 DELETE /api/contacts/{id} 连同 ledger.delete_contact 一并移除。

    # ------------------------------------------------------------ 卡片维护
    @app.get("/api/cards")
    def cards(user_id: int | None = None) -> dict:
        return {"cards": _Svcs(_uid(user_id)).ledger.list_cards()}

    @app.post("/api/cards/{card_id}/status")
    def card_status(card_id: int, body: CardStatusIn, user_id: int | None = None):
        try:
            with admin_scope():
                return _Svcs(_uid(user_id)).ledger.set_card_status(
                    card_id, body.status)
        except LedgerError as e:
            return _err(e)

    @app.post("/api/cards/{card_id}/limits")
    def card_limits(card_id: int, body: CardLimitsIn, user_id: int | None = None):
        try:
            with admin_scope():
                return _Svcs(_uid(user_id)).ledger.set_card_limits(
                    card_id,
                    yuan_to_cents(body.daily_limit_yuan) if body.daily_limit_yuan else None,
                    yuan_to_cents(body.per_tx_limit_yuan) if body.per_tx_limit_yuan else None)
        except (LedgerError, ValueError) as e:
            return _err(e)

    # ------------------------------------------------------------ 理财
    @app.get("/api/products")
    def products() -> dict:
        return {"products": _Svcs(1).wealth.list_products()}  # 产品库全局共享

    @app.get("/api/holdings")
    def holdings(user_id: int | None = None) -> dict:
        return {"holdings": _Svcs(_uid(user_id)).wealth.get_holdings()}

    @app.post("/api/holdings/{holding_id}/redeem")
    def redeem(holding_id: int, user_id: int | None = None):
        """管理员代赎回：管理台是授权方，直接 confirmed=True（留痕 operator=admin）。"""
        try:
            with admin_scope():
                return _Svcs(_uid(user_id)).wealth.redeem_product(
                    holding_id, confirmed=True)
        except WealthError as e:
            return _err(e)

    # ------------------------------------------------------------ 联动计划
    @app.get("/api/linkage-plans")
    def linkage_plans(user_id: int | None = None) -> dict:
        rows = conn.execute(
            "SELECT * FROM linkage_plans WHERE user_id=? ORDER BY id DESC LIMIT 50",
            (_uid(user_id),)).fetchall()
        plans = []
        for r in rows:
            actions = json.loads(r["actions_json"])
            lock_status = None
            if r["lock_order_id"]:
                o = conn.execute("SELECT status FROM transfer_orders WHERE id=?",
                                 (r["lock_order_id"],)).fetchone()
                lock_status = o["status"] if o else None
            done = sum(1 for a in actions if a.get("done"))
            plans.append({"plan_id": r["id"], "title": r["title"],
                          "status": r["status"],
                          "budget_yuan": cents_to_yuan(r["budget_cents"]),
                          "progress": f"{done}/{len(actions)}",
                          "lock_status": lock_status,
                          "created_at": r["created_at"]})
        return {"plans": plans}

    @app.post("/api/linkage-plans/{plan_id}/cancel")
    def cancel_plan(plan_id: int, user_id: int | None = None):
        try:
            with admin_scope():
                return _Svcs(_uid(user_id)).linkage.cancel_linkage_plan(plan_id)
        except LinkageError as e:
            return _err(e)

    # ------------------------------------------------------------ 转账订单
    @app.get("/api/orders")
    def orders(status: str | None = None, user_id: int | None = None) -> dict:
        uid = _uid(user_id)
        # 行查询 LIMIT 100（最新在前），total 同口径计数返回给前端做
        # 截断提示（>100 笔时旧单不在本列表，用状态筛选可看全某一状态）
        where = "WHERE user_id=?"
        args: tuple = (uid,)
        if status:
            where += " AND status=?"
            args = (uid, status)
        rows = conn.execute(
            f"SELECT * FROM transfer_orders {where} "
            "ORDER BY id DESC LIMIT 100", args).fetchall()
        total = conn.execute(
            f"SELECT COUNT(*) n FROM transfer_orders {where}",
            args).fetchone()["n"]
        return {"orders": [{"id": r["id"], "to_name": r["to_name"],
                            "amount_yuan": cents_to_yuan(r["amount_cents"]),
                            "status": r["status"], "created_at": r["created_at"],
                            "memo": r["memo"]} for r in rows],
                "total": total}

    @app.post("/api/orders/{order_id}/cancel")
    def cancel_order(order_id: int, user_id: int | None = None):
        """撤销待确认/孤儿单（如幂等未命中产生的重复单）。已执行单不可撤。"""
        try:
            with admin_scope():
                return _Svcs(_uid(user_id)).ledger.cancel_transfer_order(order_id)
        except LedgerError as e:
            return _err(e)

    @app.post("/api/transfer-unlock")
    def transfer_unlock(user_id: int | None = None):
        """解除转账功能锁(连续 4 次支付密码错误的唯一解除通道,权限边界:
        只属于管理员;用户侧无论对话还是页面都没有解锁入口)。同时清零错误
        计数,双库留痕(operator=admin)。"""
        uid = _uid(user_id)
        row = conn.execute(
            "SELECT transfer_locked, pay_fail_count FROM users WHERE id=?",
            (uid,)).fetchone()
        if not row["transfer_locked"]:
            return {"user_id": uid, "unlocked": False,
                    "note": "该用户转账功能未锁定"}
        conn.execute(
            "UPDATE users SET transfer_locked=0, pay_fail_count=0 WHERE id=?",
            (uid,))
        conn.commit()
        audit(conn, "transfer_unlock", {"user_id": uid},
              {"unlocked": True}, risk="HIGH")
        with admin_scope():
            record_change(conn, uid, category="admin",
                          action="transfer_unlock", target=str(uid),
                          detail={"prev_fail_count": row["pay_fail_count"]})
        return {"user_id": uid, "unlocked": True,
                "note": "转账功能已解锁,错误计数已清零"}

    @app.get("/api/subscriptions")
    def subscriptions(user_id: int | None = None) -> dict:
        return {"subscriptions": _Svcs(_uid(user_id)).analysis.list_subscriptions()}

    # ------------------------------------------------------------ 调账
    @app.post("/api/adjust")
    def adjust(body: AdjustIn, user_id: int | None = None):
        """人工调账：金额带方向、原因必填；业务库真实变动 + 双库留痕（operator=admin）。"""
        uid = _uid(user_id)
        if not body.reason.strip():
            raise HTTPException(status_code=400, detail="调账原因必填")
        s = body.amount_yuan.strip()
        negative = s.startswith("-")
        if negative:
            s = s[1:]
        try:
            cents = yuan_to_cents(s)  # 只验非负部分的合法性(数字/精度/非零)
        except ValueError as e:
            return _err(e)
        delta = -cents if negative else cents
        acct = conn.execute(
            "SELECT * FROM accounts WHERE id=? AND user_id=?",
            (body.account_id, uid)).fetchone()
        if not acct:
            raise HTTPException(status_code=404,
                                detail="账户不存在或不属于该用户")
        new_balance = acct["balance_cents"] + delta
        if new_balance < 0:
            return _err(LedgerError(
                f"调整后余额为负（{cents_to_yuan(new_balance)} 元），拒绝"))
        ts = now_iso()
        # 同一秒内连续调账时，external_ref 必须有唯一后缀，否则撞 transactions 唯一约束
        ref = f"admin:{ts}:{uuid4().hex[:12]}"
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE accounts SET balance_cents=? WHERE id=?",
                         (new_balance, body.account_id))
            conn.execute(
                """INSERT INTO transactions
                   (account_id, ts, direction, amount_cents, balance_after_cents,
                    tx_type, counterparty, category, memo, external_ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (body.account_id, ts, "in" if delta >= 0 else "out", abs(delta),
                 new_balance, "interest" if delta >= 0 else "fee",
                 "管理员调账", "管理调账", body.reason.strip(), ref))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        audit(conn, "admin_adjust",
              {"account_id": body.account_id, "delta_cents": delta,
               "reason": body.reason.strip(), "user_id": uid},
              {"new_balance_cents": new_balance}, risk="HIGH")
        with admin_scope():
            record_money(conn, uid, tool="admin_adjust", delta_cents=delta,
                         before_cents=acct["balance_cents"],
                         after_cents=new_balance,
                         account_id=body.account_id, ref=ref,
                         note=body.reason.strip())
            record_change(conn, uid, category="admin", action="adjust_balance",
                          target=f"账户{body.account_id}",
                          detail={"delta_yuan": cents_to_yuan(delta),
                                  "reason": body.reason.strip()})
        return {"new_balance_yuan": cents_to_yuan(new_balance)}

    @app.get("/api/adjust-records")
    def adjust_records(limit: int = 50, offset: int = 0, q: str = "",
                       direction: str = "all", user_id: int | None = None,
                       date_from: str = "", date_to: str = "") -> dict:
        """跨用户查询管理端调账记录（只读，按时间倒序）。

        数据源 money_records 中 tool='admin_adjust' 且 operator='admin' 的记录：
        原因在 note、余额快照在 before/after、账户在 account_id。账户回业务库
        映射当前用户，避免只靠昵称（改名/重名）认错人。
        筛选：关键词（昵称/原因/账户ID）、加款/扣款、用户、日期区间；分页返回汇总。
        """
        if direction not in ("all", "increase", "decrease"):
            raise HTTPException(
                status_code=400, detail="direction 只支持 all/increase/decrease")
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=400, detail="开始日期不能晚于结束日期")
        start_bound = _day_bound(date_from, end=False) if date_from else ""
        end_bound = _day_bound(date_to, end=True) if date_to else ""

        lim = max(1, min(limit, 200))
        off = max(0, offset)
        empty = {
            "records": [], "total": 0, "limit": lim, "offset": off,
            "has_more": False,
            "summary": {"count": 0, "added_yuan": "0.00",
                        "deducted_yuan": "0.00", "net_yuan": "0.00"},
        }

        where = ["tool='admin_adjust'", "operator='admin'"]
        params: list = []
        if user_id is not None:
            uid = _uid(user_id)
            account_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM accounts WHERE user_id=?", (uid,))]
            if not account_ids:
                return empty
            where.append(
                f"account_id IN ({','.join('?' for _ in account_ids)})")
            params.extend(account_ids)
        needle = (q or "").strip()
        if needle:
            like = f"%{needle}%"
            where.append(
                "(nickname LIKE ? OR note LIKE ? "
                "OR CAST(account_id AS TEXT) LIKE ?)")
            params.extend([like, like, like])
        if direction == "increase":
            where.append("delta_cents > 0")
        elif direction == "decrease":
            where.append("delta_cents < 0")
        if start_bound:
            where.append("ts >= ?")
            params.append(start_bound)
        if end_bound:
            where.append("ts < ?")
            params.append(end_bound)

        where_sql = " AND ".join(where)
        path = recorder.money_db_path(conn)
        agg = _rec_rows(
            path,
            f"""SELECT COUNT(*) n,
                       COALESCE(SUM(CASE WHEN delta_cents > 0
                                         THEN delta_cents ELSE 0 END), 0) added,
                       COALESCE(SUM(CASE WHEN delta_cents < 0
                                         THEN -delta_cents ELSE 0 END), 0) deducted
                FROM money_records WHERE {where_sql}""",
            tuple(params))
        if not agg:
            return empty
        total = int(agg[0]["n"])
        added = int(agg[0]["added"])
        deducted = int(agg[0]["deducted"])

        rows = _rec_rows(
            path,
            f"""SELECT * FROM money_records WHERE {where_sql}
                ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?""",
            tuple([*params, lim, off]))
        accounts = _account_map(rows)
        records = []
        for row in rows:
            acct = accounts.get(int(row["account_id"]))
            records.append({
                "id": row["id"],
                "ts": row["ts"],
                "user_id": acct["user_id"] if acct else None,
                "nickname": acct["nickname"] if acct else row["nickname"],
                "record_nickname": row["nickname"],
                "account_id": row["account_id"],
                "account_name": acct["account_name"] if acct else "",
                "account_type": acct["account_type"] if acct else "",
                "delta_yuan": cents_to_yuan(row["delta_cents"]),
                "before_yuan": cents_to_yuan(row["before_cents"]),
                "after_yuan": cents_to_yuan(row["after_cents"]),
                "reason": row["note"],
                "ref": row["ref"],
                "operator": row["operator"],
            })
        return {
            "records": records,
            "total": total,
            "limit": lim,
            "offset": off,
            "has_more": off + len(records) < total,
            "summary": {
                "count": total,
                "added_yuan": cents_to_yuan(added),
                "deducted_yuan": cents_to_yuan(deducted),
                "net_yuan": cents_to_yuan(added - deducted),
            },
        }

    # ------------------------------------------------------------ 短信记录(登录验证码)
    @app.get("/api/otp-codes")
    def otp_codes(limit: int = 30) -> dict:
        """登录短信验证码发送记录(2026-10-27 登录二次校验)。

        只读;表由 init_db/init_auth 建,老演示库首启前可能还没有——空列表兜底。
        展示脱敏:手机号走 mask_identifier;库中本就无明文码(code_hash)。
        """
        from bank_core.auth_core import mask_identifier as _mask
        from bank_core.db import now_iso as _now

        n = max(1, min(int(limit or 30), 100))
        rows = _rec_rows(
            _db_file,
            """SELECT o.id, o.user_id, o.phone, o.attempts, o.created_at,
                      o.expires_at, o.consumed_at, u.name
               FROM login_otps o LEFT JOIN users u ON u.id=o.user_id
               ORDER BY o.id DESC LIMIT ?""", (n,))
        items = [{
            "id": r["id"], "user_id": r["user_id"], "nickname": r["name"],
            "phone_masked": _mask(r["phone"]), "attempts": r["attempts"],
            "created_at": r["created_at"], "expires_at": r["expires_at"],
            "status": ("已使用" if r["consumed_at"]
                       else ("已过期" if r["expires_at"] < _now()
                             else ("已作废" if r["attempts"] >= 5
                                   else "待使用"))),
        } for r in rows]
        return {"otp_codes": items}

    # ------------------------------------------------------------ 静态管理页
    admin_dir = Path(__file__).resolve().parent.parent / "admin_console"
    if admin_dir.exists():
        app.mount("/", StaticFiles(directory=str(admin_dir), html=True), name="admin")

    return app


app = create_admin_app()  # 供 uvicorn bank_core.admin_api:app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("ADMIN_API_PORT", "8789")))
