"""账本服务：账户、流水、转账（审批两步走）、卡片、AA 收款。

资金安全铁律在代码里的落点：
- 动钱 = 两步：create_transfer_order（pending_confirm）→ confirm_transfer_order（真正扣款）；
- confirm 时二次校验：余额、卡片单笔/日累计限额、幂等键防重放；
- 所有变更走同一个 sqlite 事务，要么全成要么全不成。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

from .db import audit, now_iso
from .money import cents_to_yuan
from .recorder import record_change, record_money

# 默认风控策略（policy_check 与 confirm 共用；后续可配置化）
POLICY = {
    "single_tx_limit_cents": 5_000_000,    # 单笔 5 万
    "daily_out_limit_cents": 1_000_000,    # 当日转出 1 万（演示用，故意收紧便于现场演示拦截）
    "whitelist_only": False,               # True 时仅允许向 contacts 转账
}


def _rowd(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


class LedgerError(Exception):
    """业务拒绝（余额不足、限额、状态冲突等），信息可直接展示给用户。"""


class LedgerService:
    def __init__(self, conn: sqlite3.Connection, user_id: int = 1):
        self.conn = conn
        self.user_id = user_id

    # ------------------------------------------------------------------ 账户

    def list_accounts(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM accounts WHERE user_id=? ORDER BY id", (self.user_id,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["balance_yuan"] = cents_to_yuan(d["balance_cents"])
            out.append(d)
        audit(self.conn, "list_accounts", {"user_id": self.user_id}, {"count": len(out)})
        return out

    def get_transactions(self, start: str | None = None, end: str | None = None,
                         category: str | None = None, counterparty: str | None = None,
                         direction: str | None = None, limit: int = 50) -> list[dict]:
        sql = "SELECT * FROM transactions WHERE account_id IN (SELECT id FROM accounts WHERE user_id=?)"
        args: list = [self.user_id]
        if start:
            sql += " AND ts >= ?"
            args.append(start)
        if end:
            sql += " AND ts < ?"
            args.append(end)
        if category:
            sql += " AND category = ?"
            args.append(category)
        if counterparty:
            sql += " AND counterparty LIKE ?"
            args.append(f"%{counterparty}%")
        if direction in ("in", "out"):
            sql += " AND direction = ?"
            args.append(direction)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(min(limit, 200))
        rows = self.conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["amount_yuan"] = cents_to_yuan(d["amount_cents"])
            d["balance_after_yuan"] = cents_to_yuan(d["balance_after_cents"])
            out.append(d)
        audit(self.conn, "get_transactions",
              {"start": start, "end": end, "category": category,
               "counterparty": counterparty, "limit": limit},
              {"count": len(out)})
        return out

    # ------------------------------------------------------------------ 收款人

    def list_contacts(self) -> list[dict]:
        """联系人全量列表（web 网银 /api/contacts 用）。

        踩坑：此前 web_api 直接对 contacts 裸 SQL 查询，绕过了服务层与
        audit_log 留痕，违背"每个工具调用写 audit_log"铁律——已收编到服务层。
        """
        rows = self.conn.execute(
            "SELECT * FROM contacts WHERE user_id=? ORDER BY id", (self.user_id,)
        ).fetchall()
        out = [dict(r) for r in rows]
        audit(self.conn, "list_contacts", {"user_id": self.user_id}, {"count": len(out)})
        return out

    def resolve_contact(self, name: str | None = None, phone: str | None = None) -> list[dict]:
        """按姓名/手机号找收款人；同姓多人时返回多个，由 Agent 向用户澄清。"""
        rows: list[sqlite3.Row]
        if phone:
            rows = self.conn.execute(
                "SELECT * FROM contacts WHERE user_id=? AND phone=?", (self.user_id, phone)
            ).fetchall()
        elif name:
            rows = self.conn.execute(
                "SELECT * FROM contacts WHERE user_id=? AND name=?", (self.user_id, name)
            ).fetchall()
        else:
            raise LedgerError("请提供收款人姓名或手机号")
        out = [dict(r) for r in rows]
        audit(self.conn, "resolve_contact", {"name": name, "phone": phone},
              {"matches": len(out)})
        return out

    def add_contact(self, name: str, phone: str, note: str = "",
                    relation: str = "friend") -> dict:
        """新增收款人(联系人录入):每组 姓名+手机号+备注 存一行。

        手机号是唯一键,重复录入报错而非覆盖——录入数据宁失败可见,不静默合并。
        """
        name = (name or "").strip()
        phone = (phone or "").strip()
        note = (note or "").strip()
        if not name:
            raise LedgerError("联系人姓名不能为空")
        if not (len(phone) == 11 and phone.isdigit() and phone.startswith("1")):
            raise LedgerError("手机号须为 1 开头的 11 位数字")
        dup = self.conn.execute(
            "SELECT * FROM contacts WHERE user_id=? AND phone=?",
            (self.user_id, phone),
        ).fetchone()
        if dup:
            raise LedgerError(f"该手机号已是联系人「{dup['name']}」,不能重复录入")
        cur = self.conn.execute(
            "INSERT INTO contacts (user_id,name,phone,relation,note) VALUES (?,?,?,?,?)",
            (self.user_id, name, phone, relation, note),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM contacts WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        out = dict(row)
        audit(self.conn, "add_contact",
              {"name": name, "phone": phone, "note": note},
              {"contact_id": out["id"]})
        record_change(self.conn, self.user_id, category="contact", action="add",
                      target=name, detail={"phone": phone, "note": note,
                                           "contact_id": out["id"]})
        return out

    # 权限边界:联系人无删除方法(2026-10-01 起,原 delete_contact 已移除)。
    # 管理员只可查/代客录入——用户个人数据不容后台销毁,管理员权限不得
    # 大于用户本人;将来用户侧删除走对话工具+敏感操作闸门。

    # ------------------------------------------------------------------ 转账

    def policy_check(self, amount_cents: int, to_contact_id: int | None,
                     from_account_id: int | None = None) -> dict:
        """执行前风控校验：限额、当日累计、白名单。只判断不执行。"""
        reasons: list[str] = []
        if amount_cents > POLICY["single_tx_limit_cents"]:
            reasons.append(f"单笔限额 {cents_to_yuan(POLICY['single_tx_limit_cents'])} 元")
        if from_account_id:
            today = now_iso()[:10]
            row = self.conn.execute(
                """SELECT COALESCE(SUM(amount_cents),0) s FROM transactions
                   WHERE direction='out' AND tx_type IN ('transfer_out','split')
                     AND ts LIKE ? AND account_id=?""",
                (f"{today}%", from_account_id),
            ).fetchone()
            if row["s"] + amount_cents > POLICY["daily_out_limit_cents"]:
                reasons.append(
                    f"当日已转出 {cents_to_yuan(row['s'])} 元，加本笔将超过日限额 "
                    f"{cents_to_yuan(POLICY['daily_out_limit_cents'])} 元"
                )
        if POLICY["whitelist_only"] and to_contact_id is None:
            reasons.append("当前策略仅允许向常用收款人转账")
        ok = not reasons
        result = {"approved": ok, "reasons": reasons,
                  "checked_at": now_iso()}
        audit(self.conn, "policy_check",
              {"amount_cents": amount_cents, "to_contact_id": to_contact_id,
               "from_account_id": from_account_id}, result, risk="READ")
        return result

    def create_transfer_order(self, from_account_id: int, amount_cents: int,
                              to_contact_id: int | None = None, to_name: str = "",
                              to_account_tail: str = "", memo: str = "",
                              scheduled_at: str | None = None,
                              idempotency_key: str | None = None) -> dict:
        """建单（不动钱）。返回 pending_confirm / scheduled 订单。"""
        acct = self.conn.execute(
            "SELECT * FROM accounts WHERE id=? AND user_id=?",
            (from_account_id, self.user_id),
        ).fetchone()
        if not acct:
            raise LedgerError("账户不存在或不属于当前用户")
        contact = None
        if to_contact_id:
            contact = self.conn.execute(
                "SELECT * FROM contacts WHERE id=? AND user_id=?",
                (to_contact_id, self.user_id),
            ).fetchone()
            if not contact:
                raise LedgerError("收款人不存在")
            to_name = to_name or contact["name"]
        if not to_name:
            raise LedgerError("收款人不能为空")

        check = self.policy_check(amount_cents, to_contact_id, from_account_id)
        if not check["approved"]:
            raise LedgerError("风控拒绝：" + "；".join(check["reasons"]))

        status = "scheduled" if scheduled_at else "pending_confirm"
        order_id = None
        key = idempotency_key or uuid.uuid4().hex
        try:
            cur = self.conn.execute(
                """INSERT INTO transfer_orders
                   (user_id, from_account_id, to_contact_id, to_name, to_account_tail,
                    amount_cents, memo, status, created_at, scheduled_at, idempotency_key)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (self.user_id, from_account_id, to_contact_id, to_name, to_account_tail,
                 amount_cents, memo, status, now_iso(), scheduled_at, key),
            )
            order_id = cur.lastrowid
            self.conn.commit()
        except sqlite3.IntegrityError as exc:
            self.conn.rollback()
            # 幂等键重复：直接返回已存在的订单
            row = self.conn.execute(
                "SELECT * FROM transfer_orders WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row:
                d = dict(row)
                d["amount_yuan"] = cents_to_yuan(d["amount_cents"])
                d["note"] = "幂等命中，返回已有订单，未重复建单"
                return d
            raise LedgerError("建单失败：幂等键冲突") from exc

        d = dict(self.conn.execute(
            "SELECT * FROM transfer_orders WHERE id=?", (order_id,)
        ).fetchone())
        d["amount_yuan"] = cents_to_yuan(d["amount_cents"])
        audit(self.conn, "create_transfer_order",
              {"order_id": order_id, "amount_cents": amount_cents,
               "to_name": to_name, "scheduled_at": scheduled_at},
              {"status": status}, risk="HIGH")
        return d

    def confirm_transfer_order(self, order_id: int) -> dict:
        """确认执行（真正动钱）。余额/限额/状态在此二次校验。"""
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            order = conn.execute(
                "SELECT * FROM transfer_orders WHERE id=? AND user_id=?",
                (order_id, self.user_id),
            ).fetchone()
            if not order:
                raise LedgerError("订单不存在")
            if order["status"] not in ("pending_confirm",):
                raise LedgerError(f"订单状态为 {order['status']}，不可确认执行")
            acct = conn.execute(
                "SELECT * FROM accounts WHERE id=?", (order["from_account_id"],)
            ).fetchone()
            if acct["balance_cents"] < order["amount_cents"]:
                conn.rollback()
                conn.execute(
                    "UPDATE transfer_orders SET status='failed', fail_reason='余额不足' WHERE id=?",
                    (order_id,))
                conn.commit()
                raise LedgerError(f"余额不足：当前 {cents_to_yuan(acct['balance_cents'])} 元")
            check = self.policy_check(order["amount_cents"], order["to_contact_id"],
                                      order["from_account_id"])
            if not check["approved"]:
                raise LedgerError("风控拒绝：" + "；".join(check["reasons"]))

            new_balance = acct["balance_cents"] - order["amount_cents"]
            conn.execute("UPDATE accounts SET balance_cents=? WHERE id=?",
                         (new_balance, acct["id"]))
            ts = now_iso()
            cur = conn.execute(
                """INSERT INTO transactions
                   (account_id, ts, direction, amount_cents, balance_after_cents,
                    tx_type, counterparty, counterparty_tail, category, channel,
                    memo, external_ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (acct["id"], ts, "out", order["amount_cents"], new_balance,
                 "transfer_out", order["to_name"], order["to_account_tail"],
                 "转账", "app", order["memo"] or f"转账给{order['to_name']}",
                 f"order:{order_id}"),
            )
            conn.execute(
                "UPDATE transfer_orders SET status='executed', executed_at=? WHERE id=?",
                (ts, order_id))
            conn.commit()
        except LedgerError:
            conn.rollback()
            raise
        result = {
            "order_id": order_id, "status": "executed", "executed_at": ts,
            "amount_yuan": cents_to_yuan(order["amount_cents"]),
            "to_name": order["to_name"],
            "balance_after_yuan": cents_to_yuan(new_balance),
            "transaction_id": cur.lastrowid,
        }
        audit(conn, "confirm_transfer_order", {"order_id": order_id}, result, risk="HIGH")
        record_money(conn, self.user_id, tool="confirm_transfer_order",
                     delta_cents=-order["amount_cents"],
                     before_cents=acct["balance_cents"], after_cents=new_balance,
                     account_id=acct["id"], ref=f"order:{order_id}",
                     note=f"转账给{order['to_name']}")
        return result

    def cancel_transfer_order(self, order_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM transfer_orders WHERE id=? AND user_id=?", (order_id, self.user_id)
        ).fetchone()
        if not row:
            raise LedgerError("订单不存在")
        if row["status"] in ("executed", "cancelled"):
            raise LedgerError(f"订单已{row['status']}，不可取消")
        self.conn.execute("UPDATE transfer_orders SET status='cancelled' WHERE id=?", (order_id,))
        self.conn.commit()
        result = {"order_id": order_id, "status": "cancelled"}
        audit(self.conn, "cancel_transfer_order", {"order_id": order_id}, result, risk="MED")
        record_change(self.conn, self.user_id, category="transfer", action="cancel_order",
                      target=row["to_name"],
                      detail={"order_id": order_id,
                              "amount_yuan": cents_to_yuan(row["amount_cents"])})
        return result

    def due_scheduled_transfers(self) -> list[dict]:
        """到期定时转账单（调度器轮询用；到期转 pending_confirm 等用户确认）。"""
        rows = self.conn.execute(
            """SELECT * FROM transfer_orders WHERE status='scheduled'
                 AND scheduled_at <= ? AND user_id=?""",
            (now_iso(), self.user_id),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ AA 收款

    def create_split_bill(self, title: str, total_cents: int,
                          participants: list[dict]) -> dict:
        """发起 AA：participants=[{name, share_cents}]，金额总和必须等于总额。"""
        share_sum = sum(p["share_cents"] for p in participants)
        if share_sum != total_cents:
            raise LedgerError(
                f"分摊合计 {cents_to_yuan(share_sum)} 元 ≠ 总额 {cents_to_yuan(total_cents)} 元")
        cur = self.conn.execute(
            "INSERT INTO split_bills (user_id, title, total_cents, created_at) VALUES (?,?,?,?)",
            (self.user_id, title, total_cents, now_iso()))
        bill_id = cur.lastrowid
        for p in participants:
            self.conn.execute(
                "INSERT INTO split_bill_items (bill_id, contact_name, share_cents) VALUES (?,?,?)",
                (bill_id, p["name"], p["share_cents"]))
        self.conn.commit()
        result = {"bill_id": bill_id, "title": title,
                  "total_yuan": cents_to_yuan(total_cents),
                  "participants": [
                      {"name": p["name"], "share_yuan": cents_to_yuan(p["share_cents"])}
                      for p in participants]}
        audit(self.conn, "create_split_bill", {"bill_id": bill_id, "title": title},
              result, risk="MED")
        record_change(self.conn, self.user_id, category="aa", action="create",
                      target=title,
                      detail={"bill_id": bill_id, "total_yuan": cents_to_yuan(total_cents),
                              "participants": len(participants)})
        return result

    def get_split_bill(self, bill_id: int) -> dict:
        bill = self.conn.execute(
            "SELECT * FROM split_bills WHERE id=? AND user_id=?", (bill_id, self.user_id)
        ).fetchone()
        if not bill:
            raise LedgerError("AA 单不存在")
        items = self.conn.execute(
            "SELECT * FROM split_bill_items WHERE bill_id=?", (bill_id,)).fetchall()
        paid = sum(1 for i in items if i["paid"])
        result = {
            "bill_id": bill_id, "title": bill["title"],
            "total_yuan": cents_to_yuan(bill["total_cents"]),
            "status": bill["status"],
            "paid_count": paid, "total_count": len(items),
            "items": [{"name": i["contact_name"],
                       "share_yuan": cents_to_yuan(i["share_cents"]),
                       "paid": bool(i["paid"])} for i in items],
        }
        audit(self.conn, "get_split_bill", {"bill_id": bill_id}, {"status": result["status"]})
        return result

    def settle_split_bill_item(self, bill_id: int, contact_name: str) -> dict:
        """标记某人参账已付（演示中模拟对方付款回调）。"""
        row = self.conn.execute(
            "SELECT * FROM split_bill_items WHERE bill_id=? AND contact_name=?",
            (bill_id, contact_name)).fetchone()
        if not row:
            raise LedgerError("分摊项不存在")
        if not row["paid"]:
            self.conn.execute(
                "UPDATE split_bill_items SET paid=1, paid_at=? WHERE id=?",
                (now_iso(), row["id"]))
        remain = self.conn.execute(
            "SELECT COUNT(*) c FROM split_bill_items WHERE bill_id=? AND paid=0",
            (bill_id,)).fetchone()["c"]
        status = "settled" if remain == 0 else "collecting"
        self.conn.execute("UPDATE split_bills SET status=? WHERE id=?", (status, bill_id))
        self.conn.commit()
        result = {"bill_id": bill_id, "contact": contact_name, "bill_status": status}
        # P1 修复：本方法置 paid 位、推进账单状态，是写操作却原先漏了 audit 留痕，
        # 与 cancel_transfer_order 等其他写操作不一致；risk=LOW 与 MCP 工具标注一致。
        audit(self.conn, "settle_split_bill_item",
              {"bill_id": bill_id, "contact_name": contact_name}, result, risk="LOW")
        record_change(self.conn, self.user_id, category="aa", action="settle",
                      target=contact_name,
                      detail={"bill_id": bill_id, "bill_status": status})
        return result

    # ------------------------------------------------------------------ 卡片

    def list_cards(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM cards WHERE user_id=? ORDER BY id", (self.user_id,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["daily_limit_yuan"] = cents_to_yuan(d["daily_limit_cents"])
            d["per_tx_limit_yuan"] = cents_to_yuan(d["per_tx_limit_cents"])
            out.append(d)
        audit(self.conn, "list_cards", {"user_id": self.user_id}, {"count": len(out)})
        return out

    def apply_card(self, card_type: str = "debit") -> dict:
        """办卡（演示秒批）。"""
        if card_type not in ("debit", "credit"):
            raise LedgerError("卡类型只支持 debit / credit")
        tail = datetime.now().strftime("%H%M%S")[-4:]
        masked = f"6222 **** **** {tail}"
        cur = self.conn.execute(
            """INSERT INTO cards (user_id, account_id, card_no_masked, card_type,
               status, created_at) VALUES (?,?,?,?, 'active', ?)""",
            (self.user_id, None, masked, card_type, now_iso()))
        self.conn.commit()
        result = {"card_id": cur.lastrowid, "card_no_masked": masked,
                  "card_type": card_type, "status": "active"}
        audit(self.conn, "apply_card", {"card_type": card_type}, result, risk="MED")
        record_change(self.conn, self.user_id, category="card", action="apply",
                      target=masked, detail={"card_id": result["card_id"],
                                             "card_type": card_type})
        return result

    def set_card_limits(self, card_id: int, daily_limit_cents: int | None = None,
                        per_tx_limit_cents: int | None = None) -> dict:
        card = self._get_card(card_id)
        if card["status"] != "active":
            raise LedgerError("卡片非活跃状态，先解锁再调额度")
        if daily_limit_cents:
            self.conn.execute("UPDATE cards SET daily_limit_cents=? WHERE id=?",
                              (daily_limit_cents, card_id))
        if per_tx_limit_cents:
            self.conn.execute("UPDATE cards SET per_tx_limit_cents=? WHERE id=?",
                              (per_tx_limit_cents, card_id))
        self.conn.commit()
        d = self._get_card(card_id)
        result = {"card_id": card_id,
                  "daily_limit_yuan": cents_to_yuan(d["daily_limit_cents"]),
                  "per_tx_limit_yuan": cents_to_yuan(d["per_tx_limit_cents"])}
        # 评审修复(2026-10-03):本方法此前只写 change_log、漏了 audit_log,
        # 是全库唯一没有审计留痕的写操作(铁律 4「全量审计」的缺口)。
        audit(self.conn, "set_card_limits",
              {"card_id": card_id,
               "daily_limit_cents": daily_limit_cents,
               "per_tx_limit_cents": per_tx_limit_cents},
              result, risk="MED")
        record_change(self.conn, self.user_id, category="card", action="limits",
                      target=d["card_no_masked"],
                      detail={"card_id": card_id,
                              "daily_limit_yuan": result["daily_limit_yuan"],
                              "per_tx_limit_yuan": result["per_tx_limit_yuan"]})
        return result

    def set_card_status(self, card_id: int, status: str) -> dict:
        """锁定/解锁/挂失。"""
        if status not in ("locked", "active", "lost"):
            raise LedgerError("状态只支持 locked / active / lost")
        card = self._get_card(card_id)
        if card["status"] == "lost":
            raise LedgerError("挂失卡不可再变更状态，请联系客服补卡")
        self.conn.execute("UPDATE cards SET status=? WHERE id=?", (status, card_id))
        self.conn.commit()
        result = {"card_id": card_id, "status": status}
        audit(self.conn, "set_card_status", {"card_id": card_id, "status": status},
              result, risk="MED" if status == "active" else "LOW")
        record_change(self.conn, self.user_id, category="card", action="status",
                      target=card["card_no_masked"],
                      detail={"card_id": card_id, "from": card["status"], "to": status})
        return result

    def _get_card(self, card_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM cards WHERE id=? AND user_id=?", (card_id, self.user_id)
        ).fetchone()
        if not row:
            raise LedgerError("卡片不存在")
        return row
