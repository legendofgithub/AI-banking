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

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import recorder
from .auth_core import mask_identifier, mask_id_card
from .analysis import AnalysisService
from .db import audit, connect, migrate_users_kyc, now_iso
from .events import EventService
from .ledger import LedgerError, LedgerService
from .linkage import LinkageError, LinkageService
from .money import cents_to_yuan, yuan_to_cents
from .recorder import admin_scope, record_change, record_money
from .wealth import WealthError, WealthService


def _err(exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


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


# 注册时间筛选窗口：within 参数 → 天数
_WITHIN_DAYS = {"week": 7, "month": 30, "quarter": 90, "year": 365}


def create_admin_app(db_path: str | Path | None = None) -> FastAPI:
    """构造管理后台应用。db_path=None 走 BANK_CORE_DB/默认库（生产）。"""
    conn = connect(db_path)
    # users.id_card/email 老库补列(管理台 connect 直连,不经 init_db)
    migrate_users_kyc(conn)

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
        # 注册信息(auth 行):脱敏展示 JR/T 0171 C2 级;老演示用户无 auth 为 None
        auth_by_uid = {r["user_id"]: r for r in conn.execute(
            "SELECT user_id, identifier, identifier_type, last_login_at "
            "FROM auth_users")}
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
            return {"identifier_masked": mask_identifier(a["identifier"]),
                    "identifier_type": a["identifier_type"],
                    "last_login_at": a["last_login_at"]}

        out = [{
            "user_id": r["id"], "nickname": r["name"], "phone": r["phone"],
            "id_card_masked": mask_id_card(r["id_card"]),
            "email_masked": mask_identifier(r["email"]) if r["email"] else "",
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
            """SELECT id, to_name, amount_cents, status, created_at FROM transfer_orders
               WHERE user_id=? AND status IN ('pending_confirm','scheduled')
               ORDER BY id DESC LIMIT 20""", (uid,)).fetchall()
        pending_orders = [{"id": r["id"], "to_name": r["to_name"],
                           "amount_yuan": cents_to_yuan(r["amount_cents"]),
                           "status": r["status"], "created_at": r["created_at"]}
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
        }

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
        if status:
            rows = conn.execute(
                """SELECT * FROM transfer_orders WHERE user_id=? AND status=?
                   ORDER BY id DESC LIMIT 100""", (uid, status)).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM transfer_orders WHERE user_id=?
                   ORDER BY id DESC LIMIT 100""", (uid,)).fetchall()
        return {"orders": [{"id": r["id"], "to_name": r["to_name"],
                            "amount_yuan": cents_to_yuan(r["amount_cents"]),
                            "status": r["status"], "created_at": r["created_at"],
                            "memo": r["memo"]} for r in rows]}

    @app.post("/api/orders/{order_id}/cancel")
    def cancel_order(order_id: int, user_id: int | None = None):
        """撤销待确认/孤儿单（如幂等未命中产生的重复单）。已执行单不可撤。"""
        try:
            with admin_scope():
                return _Svcs(_uid(user_id)).ledger.cancel_transfer_order(order_id)
        except LedgerError as e:
            return _err(e)

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
                 "管理员调账", "管理调账", body.reason.strip(), f"admin:{ts}"))
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
                         account_id=body.account_id, ref=f"admin:{ts}",
                         note=body.reason.strip())
            record_change(conn, uid, category="admin", action="adjust_balance",
                          target=f"账户{body.account_id}",
                          detail={"delta_yuan": cents_to_yuan(delta),
                                  "reason": body.reason.strip()})
        return {"new_balance_yuan": cents_to_yuan(new_balance)}

    # ------------------------------------------------------------ 静态管理页
    admin_dir = Path(__file__).resolve().parent.parent / "admin_console"
    if admin_dir.exists():
        app.mount("/", StaticFiles(directory=str(admin_dir), html=True), name="admin")

    return app


app = create_admin_app()  # 供 uvicorn bank_core.admin_api:app


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("ADMIN_API_PORT", "8789")))
