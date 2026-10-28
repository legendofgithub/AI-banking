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
from .recorder import record_change, record_money, user_scope

# 默认风控策略（policy_check 与 confirm 共用；后续可配置化）
POLICY = {
    "single_tx_limit_cents": 5_000_000,    # 单笔 5 万
    "daily_out_limit_cents": 1_000_000,    # 当日转出 1 万（演示用，故意收紧便于现场演示拦截）
    "whitelist_only": False,               # True 时仅允许向 contacts 转账
}

# 演示绑卡：绑定即分配的虚拟余额（5 万元）。仅用于沙箱充钱演示，
# 不代表真实银行卡余额；绑定与充钱都不接银行接口。
BOUND_CARD_DEMO_BALANCE_CENTS = 5_000_000


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

    def deposit(self, account_id: int, amount_cents: int, *,
                note: str = "", idempotency_key: str = "") -> dict:
        """[MED] 模拟入金：演示外部账户 → 本人账户，入账即产生余额。

        只做入账，不动用户现有资金，所以不需要支付密码；幂等键写入
        external_ref，重复请求返回第一次结果。生产环境必须换成银行/持牌
        机构的入金回调与对账，不能让用户自助凭空加余额。
        """
        if amount_cents <= 0:
            raise LedgerError("入金金额必须大于 0")
        key = (idempotency_key or uuid.uuid4().hex).strip()
        ref = f"deposit:{self.user_id}:{key}"
        conn = self.conn

        def existing_result() -> dict | None:
            row = conn.execute(
                "SELECT * FROM transactions WHERE external_ref=?", (ref,)
            ).fetchone()
            if not row:
                return None
            return {
                "account_id": row["account_id"],
                "amount_yuan": cents_to_yuan(row["amount_cents"]),
                "balance_after_yuan": cents_to_yuan(row["balance_after_cents"]),
                "transaction_id": row["id"],
                "duplicate": True,
            }

        found = existing_result()
        if found:
            return found

        ts = now_iso()
        try:
            conn.execute("BEGIN IMMEDIATE")
            acct = conn.execute(
                "SELECT * FROM accounts WHERE id=? AND user_id=?",
                (account_id, self.user_id)).fetchone()
            if not acct:
                raise LedgerError("账户不存在")
            # 并发下同一幂等键只入账一次
            found = existing_result()
            if found:
                conn.rollback()
                return found
            before = acct["balance_cents"]
            new_balance = before + amount_cents
            conn.execute("UPDATE accounts SET balance_cents=? WHERE id=?",
                         (new_balance, account_id))
            cur = conn.execute(
                """INSERT INTO transactions
                   (account_id, ts, direction, amount_cents, balance_after_cents,
                    tx_type, counterparty, category, channel, memo, external_ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, ts, "in", amount_cents, new_balance,
                 "transfer_in", "模拟入金", "入金", "app",
                 note or "模拟外部账户转入(演示)", ref))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            found = existing_result()
            if found:
                return found
            raise
        except Exception:
            conn.rollback()
            raise

        result = {
            "account_id": account_id,
            "amount_yuan": cents_to_yuan(amount_cents),
            "balance_after_yuan": cents_to_yuan(new_balance),
            "transaction_id": cur.lastrowid,
            "duplicate": False,
        }
        audit(conn, "deposit",
              {"account_id": account_id, "amount_cents": amount_cents,
               "note": note, "user_id": self.user_id},
              result, risk="MED")
        with user_scope():
            record_money(conn, self.user_id, tool="deposit",
                         delta_cents=amount_cents, before_cents=before,
                         after_cents=new_balance, account_id=account_id,
                         ref=ref, note=note or "模拟入金")
        return result

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

    def transfer_limits(self) -> dict:
        """有效转账限额:用户级覆盖(>0)优先,否则跟随系统默认 POLICY。"""
        row = self.conn.execute(
            "SELECT daily_transfer_limit_cents d, single_transfer_limit_cents s "
            "FROM users WHERE id=?", (self.user_id,)).fetchone()
        daily_override = int(row["d"]) if row else 0
        single_override = int(row["s"]) if row else 0
        return {
            "daily_limit_cents": (daily_override if daily_override > 0
                                  else POLICY["daily_out_limit_cents"]),
            "single_limit_cents": (single_override if single_override > 0
                                   else POLICY["single_tx_limit_cents"]),
            "daily_overridden": daily_override > 0,
            "single_overridden": single_override > 0,
            "default_daily_limit_cents": POLICY["daily_out_limit_cents"],
            "default_single_limit_cents": POLICY["single_tx_limit_cents"],
        }

    def _own_account_by_tail(self, tail: Any) -> dict | None:
        """4 位账户尾号 → 本用户自己名下的账户；不是自己的返回 None。

        内部划转判定的基础：联动锁定单创建时目标尾号就写成 f"{账户id:04d}"
        （linkage.py），"活期→理财专户"因此可识别为划给自己。外部收款人的
        卡尾号（8821 等）即使撞上数字也过不了 user_id 校验，不会误判。
        """
        try:
            aid = int(str(tail or ""))
        except (TypeError, ValueError):
            return None
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE id=? AND user_id=?",
            (aid, self.user_id)).fetchone()
        return dict(row) if row else None

    def policy_check(self, amount_cents: int, to_contact_id: int | None,
                     from_account_id: int | None = None,
                     to_account_tail: str | None = None) -> dict:
        """执行前风控校验：账户级限额、当日累计、白名单。只判断不执行。

        日限额只管**对外**转出：目标是本用户自己账户的内部划转（联动预留、
        预算释放）不占对外额度，当日累计里也剔除划给自己账户的历史流水
        （counterparty_tail 命中自己账户尾号）。否则预留一笔就把当日额度
        吃满，撤回/重划全被自己先前那笔拦死（2026-10-28 实测：预留单 195）。
        """
        limits = self.transfer_limits()
        single_limit = limits["single_limit_cents"]
        daily_limit = limits["daily_limit_cents"]
        reasons: list[str] = []
        if amount_cents > single_limit:
            reasons.append(f"单笔限额 {cents_to_yuan(single_limit)} 元")
        internal = self._own_account_by_tail(to_account_tail) is not None
        if from_account_id and not internal:
            today = now_iso()[:10]
            own_tails = [f"{r['id']:04d}" for r in self.conn.execute(
                "SELECT id FROM accounts WHERE user_id=?", (self.user_id,))]
            ph = ",".join("?" * len(own_tails))
            row = self.conn.execute(
                f"""SELECT COALESCE(SUM(amount_cents),0) s FROM transactions
                   WHERE direction='out' AND tx_type IN ('transfer_out','split')
                     AND ts LIKE ? AND account_id=?
                     AND (counterparty_tail IS NULL
                          OR counterparty_tail NOT IN ({ph}))""",
                (f"{today}%", from_account_id, *own_tails),
            ).fetchone()
            if row["s"] + amount_cents > daily_limit:
                reasons.append(
                    f"当日已转出 {cents_to_yuan(row['s'])} 元，加本笔将超过日限额 "
                    f"{cents_to_yuan(daily_limit)} 元"
                )
        if POLICY["whitelist_only"] and to_contact_id is None and not internal:
            reasons.append("当前策略仅允许向常用收款人转账")
        ok = not reasons
        result = {"approved": ok, "reasons": reasons,
                  "limits": {"daily_limit_yuan": cents_to_yuan(daily_limit),
                             "single_limit_yuan": cents_to_yuan(single_limit)},
                  "internal_transfer": internal,
                  "checked_at": now_iso()}
        audit(self.conn, "policy_check",
              {"amount_cents": amount_cents, "to_contact_id": to_contact_id,
               "from_account_id": from_account_id,
               "to_account_tail": to_account_tail}, result, risk="READ")
        return result

    def _check_transfer_lock(self) -> None:
        """转账功能锁:待确认转账页连续 4 次支付密码错误即锁定,只允许
        管理员在管理台解除(ledger 层是全链路唯一卡点——对话建单/页面确认/
        联动执行全部经由这两个方法,锁在此处即全线生效)。"""
        row = self.conn.execute(
            "SELECT transfer_locked FROM users WHERE id=?", (self.user_id,)
        ).fetchone()
        if row and row["transfer_locked"]:
            raise LedgerError(
                "转账功能已锁定（连续支付密码错误），请联系管理员解除后再次操作")

    def create_transfer_order(self, from_account_id: int, amount_cents: int,
                              to_contact_id: int | None = None, to_name: str = "",
                              to_account_tail: str = "", memo: str = "",
                              scheduled_at: str | None = None,
                              idempotency_key: str | None = None) -> dict:
        """建单（不动钱）。返回 pending_confirm / scheduled 订单。"""
        self._check_transfer_lock()
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

        check = self.policy_check(amount_cents, to_contact_id, from_account_id,
                                  to_account_tail=to_account_tail)
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
        self._check_transfer_lock()
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
                                      order["from_account_id"],
                                      to_account_tail=order["to_account_tail"])
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
            # 内部划转入账:收款尾号命中本用户自己的账户时,钱进那个账户
            # (联动预留 = 活期→理财专户)。此前只记出账,预留的钱在账本上
            # 凭空消失、理财专户永远 0 余额,撤回/回转无从谈起(2026-10-28 实测)。
            internal_credit = None
            dest = self._own_account_by_tail(order["to_account_tail"])
            if dest is not None and dest["id"] != acct["id"]:
                dest_balance = dest["balance_cents"] + order["amount_cents"]
                conn.execute("UPDATE accounts SET balance_cents=? WHERE id=?",
                             (dest_balance, dest["id"]))
                conn.execute(
                    """INSERT INTO transactions
                       (account_id, ts, direction, amount_cents, balance_after_cents,
                        tx_type, counterparty, counterparty_tail, category, channel,
                        memo, external_ref)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (dest["id"], ts, "in", order["amount_cents"], dest_balance,
                     "transfer_in", acct["name"], f"{acct['id']:04d}",
                     "转账", "app", order["memo"] or f"来自{acct['name']}",
                     f"order:{order_id}:in"))  # external_ref 唯一,入账腿加后缀
                internal_credit = {"account_id": dest["id"],
                                   "account_name": dest["name"],
                                   "balance_yuan": cents_to_yuan(dest_balance)}
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
        if internal_credit is not None:
            result["internal_credit"] = internal_credit
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

    def internal_transfer(self, from_account_id: int, to_account_id: int,
                          amount_cents: int, memo: str = "") -> dict:
        """自己名下两账户间划转（联动预算释放/撤回）：余额对移、两腿流水。

        不走对外风控——日限额只管对外转出（见 policy_check 的内部划转豁免），
        撤回自己刚预留的钱不该被自己吃满的额度拦死。源余额不足整笔失败。
        """
        if from_account_id == to_account_id:
            raise LedgerError("划转源与目标是同一账户")
        conn = self.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            src = conn.execute(
                "SELECT * FROM accounts WHERE id=? AND user_id=?",
                (from_account_id, self.user_id)).fetchone()
            dst = conn.execute(
                "SELECT * FROM accounts WHERE id=? AND user_id=?",
                (to_account_id, self.user_id)).fetchone()
            if not src or not dst:
                raise LedgerError("账户不存在")
            if src["balance_cents"] < amount_cents:
                raise LedgerError(
                    f"余额不足：{src['name']} 当前 {cents_to_yuan(src['balance_cents'])} 元")
            ts = now_iso()
            new_src = src["balance_cents"] - amount_cents
            new_dst = dst["balance_cents"] + amount_cents
            conn.execute("UPDATE accounts SET balance_cents=? WHERE id=?",
                         (new_src, src["id"]))
            conn.execute("UPDATE accounts SET balance_cents=? WHERE id=?",
                         (new_dst, dst["id"]))
            for acct, other, direction, balance in (
                    (src, dst, "out", new_src), (dst, src, "in", new_dst)):
                conn.execute(
                    """INSERT INTO transactions
                       (account_id, ts, direction, amount_cents, balance_after_cents,
                        tx_type, counterparty, counterparty_tail, category, channel,
                        memo, external_ref)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (acct["id"], ts, direction, amount_cents, balance,
                     "transfer_out" if direction == "out" else "transfer_in",
                     other["name"], f"{other['id']:04d}",
                     "转账", "app", memo or f"与{other['name']}互转", None))
            conn.commit()
        except LedgerError:
            conn.rollback()
            raise
        result = {
            "from_account_id": from_account_id, "to_account_id": to_account_id,
            "amount_yuan": cents_to_yuan(amount_cents),
            "from_balance_yuan": cents_to_yuan(new_src),
            "to_balance_yuan": cents_to_yuan(new_dst),
        }
        audit(conn, "internal_transfer",
              {"from_account_id": from_account_id, "to_account_id": to_account_id,
               "amount_cents": amount_cents, "memo": memo}, result, risk="MED")
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
        """发起 AA：participants=[{name, phone?, share_cents}]，金额总和必须等于总额。

        phone 选填：给了手机号，交款单就能精确投递给对应的注册用户；
        不给则只在派发时按姓名/联系人手机号反查（见 send_split_requests）。
        """
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
                "INSERT INTO split_bill_items "
                "(bill_id, contact_name, phone, share_cents) VALUES (?,?,?,?)",
                (bill_id, p["name"], str(p.get("phone") or "").strip(),
                 p["share_cents"]))
        self.conn.commit()
        result = {"bill_id": bill_id, "title": title,
                  "total_yuan": cents_to_yuan(total_cents),
                  "participants": [
                      {"name": p["name"], "share_yuan": cents_to_yuan(p["share_cents"])}
                      | ({"phone": str(p["phone"]).strip()} if p.get("phone") else {})
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
            """SELECT i.*, pr.id AS request_id, pr.to_user_id,
                      pr.status AS request_status
               FROM split_bill_items i
               LEFT JOIN payment_requests pr ON pr.item_id = i.id
               WHERE i.bill_id=? ORDER BY i.id""", (bill_id,)).fetchall()
        paid = sum(1 for i in items if i["paid"])
        result = {
            "bill_id": bill_id, "title": bill["title"],
            "total_yuan": cents_to_yuan(bill["total_cents"]),
            "status": bill["status"],
            "paid_count": paid, "total_count": len(items),
            "items": [{"name": i["contact_name"],
                       "share_yuan": cents_to_yuan(i["share_cents"]),
                       "paid": bool(i["paid"]),
                       "request_status": i["request_status"]} for i in items],
        }
        audit(self.conn, "get_split_bill", {"bill_id": bill_id}, {"status": result["status"]})
        return result

    # ---------------------------------------------------- AA 交款单（跨用户）

    _SELF_NAMES = frozenset({"我", "本人", "自己", "我的", "我本人"})

    def _resolve_payee(self, item: sqlite3.Row) -> dict:
        """把一个分摊项解析成「投递给谁」。

        解析顺序（只做确定性匹配，不做模糊猜测）：
        1. 参与人手机号（发起时显式给出）→ users.phone 精确命中；
        2. 发起人的联系人里同名者的手机号 → users.phone 精确命中；
        3. users.name 精确同名（仅当命中恰好 1 人）。
        返回 {"status": delivered|self|not_found|ambiguous, "to_user_id", "candidates"}。
        """
        name = str(item["contact_name"] or "").strip()
        phone = str(item["phone"] or "").strip()
        if not phone and name in self._SELF_NAMES:
            return {"status": "self", "to_user_id": None, "candidates": []}
        if phone:
            rows = self.conn.execute(
                "SELECT id, name FROM users WHERE phone=?", (phone,)).fetchall()
            if len(rows) == 1:
                return self._payee_hit(int(rows[0]["id"]))
            return {"status": "not_found", "to_user_id": None, "candidates": []}

        hits: dict[int, dict] = {}
        contacts = self.conn.execute(
            "SELECT phone FROM contacts WHERE user_id=? AND name=?",
            (self.user_id, name)).fetchall()
        for c in contacts:
            for u in self.conn.execute(
                    "SELECT id, name FROM users WHERE phone=?", (c["phone"],)):
                hits[int(u["id"])] = {"user_id": int(u["id"]), "name": u["name"]}
        for u in self.conn.execute(
                "SELECT id, name FROM users WHERE name=?", (name,)):
            hits[int(u["id"])] = {"user_id": int(u["id"]), "name": u["name"]}

        if len(hits) == 1:
            return self._payee_hit(next(iter(hits)))
        if len(hits) > 1:
            return {"status": "ambiguous", "to_user_id": None,
                    "candidates": list(hits.values())}
        return {"status": "not_found", "to_user_id": None, "candidates": []}

    def _payee_hit(self, user_id: int) -> dict:
        if user_id == self.user_id:
            return {"status": "self", "to_user_id": user_id, "candidates": []}
        return {"status": "delivered", "to_user_id": user_id, "candidates": []}

    def send_split_requests(self, bill_id: int) -> dict:
        """把 AA 账单逐个参与人投递成「交款单」（幂等：同一分摊项只投一次）。

        未匹配到注册用户的参与人保留为普通记账项，不阻塞整单；
        返回逐人投递结果，供 Agent 如实播报“已送达谁 / 谁未注册”。
        """
        bill = self.conn.execute(
            "SELECT * FROM split_bills WHERE id=? AND user_id=?",
            (bill_id, self.user_id)).fetchone()
        if not bill:
            raise LedgerError("AA 单不存在")
        items = self.conn.execute(
            "SELECT * FROM split_bill_items WHERE bill_id=? ORDER BY id",
            (bill_id,)).fetchall()
        deliveries: list[dict] = []
        sent = 0
        for item in items:
            existing = self.conn.execute(
                "SELECT * FROM payment_requests WHERE item_id=?",
                (item["id"],)).fetchone()
            if existing:
                deliveries.append({"name": item["contact_name"],
                                   "status": "already_sent",
                                   "request_id": int(existing["id"]),
                                   "to_user_id": int(existing["to_user_id"])})
                continue
            resolved = self._resolve_payee(item)
            if resolved["status"] != "delivered":
                deliveries.append({"name": item["contact_name"],
                                   "status": resolved["status"],
                                   "candidates": resolved["candidates"]})
                continue
            cur = self.conn.execute(
                """INSERT INTO payment_requests
                   (bill_id, item_id, from_user_id, to_user_id, title,
                    amount_cents, status, created_at)
                   VALUES (?,?,?,?,?,?,'pending',?)""",
                (bill_id, item["id"], self.user_id, resolved["to_user_id"],
                 bill["title"], item["share_cents"], now_iso()))
            sent += 1
            deliveries.append({"name": item["contact_name"],
                               "status": "delivered",
                               "request_id": int(cur.lastrowid),
                               "to_user_id": resolved["to_user_id"]})
        self.conn.commit()
        result = {"bill_id": bill_id, "title": bill["title"],
                  "sent_count": sent, "deliveries": deliveries}
        audit(self.conn, "send_split_requests", {"bill_id": bill_id},
              result, risk="MED")
        record_change(self.conn, self.user_id, category="aa", action="send_requests",
                      target=bill["title"],
                      detail={"bill_id": bill_id, "sent": sent,
                              "deliveries": len(deliveries)})
        return result

    def list_payment_requests(self, status: str = "pending") -> dict:
        """当前用户的交款单收件箱（对方 Agent 投递进来的待付/已付记录）。

        status: pending（默认）| paid | all。pending_count 始终返回，
        便于 Agent 在任意查询里顺带提醒“你还有 N 张待付”。
        """
        if status not in ("pending", "paid", "all"):
            raise LedgerError("status 不合法（pending/paid/all）")
        sql = """SELECT pr.*, u.name AS from_name, sb.status AS bill_status
                 FROM payment_requests pr
                 JOIN users u ON u.id = pr.from_user_id
                 JOIN split_bills sb ON sb.id = pr.bill_id
                 WHERE pr.to_user_id=?"""
        args: list = [self.user_id]
        if status != "all":
            sql += " AND pr.status=?"
            args.append(status)
        rows = self.conn.execute(sql + " ORDER BY pr.id DESC", args).fetchall()
        pending_count = self.conn.execute(
            "SELECT COUNT(*) c FROM payment_requests "
            "WHERE to_user_id=? AND status='pending'", (self.user_id,)).fetchone()["c"]
        requests = [{
            "request_id": int(r["id"]), "bill_id": int(r["bill_id"]),
            "title": r["title"], "from_name": r["from_name"],
            "from_user_id": int(r["from_user_id"]),
            "amount_yuan": cents_to_yuan(r["amount_cents"]),
            "status": r["status"], "created_at": r["created_at"],
            "paid_at": r["paid_at"], "bill_status": r["bill_status"],
        } for r in rows]
        result = {"count": len(requests), "pending_count": pending_count,
                  "requests": requests}
        audit(self.conn, "list_payment_requests", {"status": status},
              {"count": len(requests), "pending_count": pending_count})
        return result

    def pay_payment_request(self, request_id: int) -> dict:
        """收件人确认支付一张交款单：置付款位并推进账单状态（幂等）。

        演示边界与既有 AA 结算一致：只标记已付，不产生真实入账流水；
        生产环境应替换为支付/清算回调（见 docs/产品功能说明.md）。
        """
        row = self.conn.execute(
            """SELECT pr.*, u.name AS from_name
               FROM payment_requests pr JOIN users u ON u.id = pr.from_user_id
               WHERE pr.id=? AND pr.to_user_id=?""",
            (request_id, self.user_id)).fetchone()
        if not row:
            raise LedgerError("交款单不存在或不属于当前用户")
        if row["status"] == "paid":
            progress = self._bill_progress(int(row["bill_id"]))
            result = {"request_id": int(row["id"]), "bill_id": int(row["bill_id"]),
                      "title": row["title"], "from_name": row["from_name"],
                      "amount_yuan": cents_to_yuan(row["amount_cents"]),
                      "status": "paid", "already_paid": True, **progress}
            audit(self.conn, "pay_payment_request", {"request_id": request_id},
                  result, risk="LOW")
            return result
        if row["status"] != "pending":
            raise LedgerError("交款单已失效，不能支付")

        paid_at = now_iso()
        self.conn.execute(
            "UPDATE payment_requests SET status='paid', paid_at=? WHERE id=?",
            (paid_at, request_id))
        self.conn.execute(
            "UPDATE split_bill_items SET paid=1, paid_at=? WHERE id=? AND paid=0",
            (paid_at, row["item_id"]))
        progress = self._bill_progress(int(row["bill_id"]))
        self.conn.execute("UPDATE split_bills SET status=? WHERE id=?",
                          (progress["bill_status"], row["bill_id"]))
        self.conn.commit()
        result = {"request_id": int(row["id"]), "bill_id": int(row["bill_id"]),
                  "title": row["title"], "from_name": row["from_name"],
                  "amount_yuan": cents_to_yuan(row["amount_cents"]),
                  "status": "paid", "already_paid": False, **progress}
        audit(self.conn, "pay_payment_request", {"request_id": request_id},
              result, risk="LOW")
        record_change(self.conn, self.user_id, category="aa", action="pay_request",
                      target=row["title"],
                      detail={"request_id": request_id,
                              "bill_id": int(row["bill_id"]),
                              "amount_yuan": cents_to_yuan(row["amount_cents"])})
        return result

    def _bill_progress(self, bill_id: int) -> dict:
        total = self.conn.execute(
            "SELECT COUNT(*) c FROM split_bill_items WHERE bill_id=?",
            (bill_id,)).fetchone()["c"]
        paid = self.conn.execute(
            "SELECT COUNT(*) c FROM split_bill_items WHERE bill_id=? AND paid=1",
            (bill_id,)).fetchone()["c"]
        return {"bill_status": "settled" if total and paid >= total else "collecting",
                "paid_count": paid, "total_count": total}

    def settle_split_bill_item(self, bill_id: int, contact_name: str) -> dict:
        """标记某人参账已付（演示中模拟对方付款回调）。"""
        row = self.conn.execute(
            "SELECT * FROM split_bill_items WHERE bill_id=? AND contact_name=?",
            (bill_id, contact_name)).fetchone()
        if not row:
            raise LedgerError("分摊项不存在")
        if not row["paid"]:
            paid_at = now_iso()
            self.conn.execute(
                "UPDATE split_bill_items SET paid=1, paid_at=? WHERE id=?",
                (paid_at, row["id"]))
            # 收件箱同步:发起人手动标记已付时,对应交款单不再显示为待付
            self.conn.execute(
                "UPDATE payment_requests SET status='paid', paid_at=? "
                "WHERE item_id=? AND status='pending'", (paid_at, row["id"]))
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
            d["balance_yuan"] = cents_to_yuan(d.get("balance_cents") or 0)
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
        if card["source"] == "bound":
            raise LedgerError("外部绑定卡不支持限额调整（演示只读）")
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
        if card["source"] == "bound":
            raise LedgerError("外部绑定卡不支持锁定/挂失（演示只读）")
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

    # ------------------------------------------------------- 绑定银行卡(演示)
    def list_bound_cards(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM cards WHERE user_id=? AND source='bound' "
            "ORDER BY id DESC", (self.user_id,)).fetchall()
        return [{
            "card_id": r["id"],
            "bank_name": r["bank_name"],
            "card_no_masked": r["card_no_masked"],
            "holder_name": r["holder_name"],
            "balance_yuan": cents_to_yuan(r["balance_cents"]),
            "status": r["status"],
            "created_at": r["created_at"],
            "source": "bound",
        } for r in rows]

    def bind_bound_card(self, bank_name: str, card_no: str,
                        holder_name: str) -> dict:
        """演示绑卡:只校验 16-19 位数字并掩码存储,不校验真伪、不查真实余额。

        绑定后分配固定虚拟余额(BOUND_CARD_DEMO_BALANCE_CENTS),供"充钱"
        选择该卡作为资金来源。完整卡号不入库。
        """
        bank = (bank_name or "").strip()
        holder = (holder_name or "").strip()
        if len(bank) < 2 or len(bank) > 30:
            raise LedgerError("银行名称需为 2-30 个字符")
        if len(holder) < 2 or len(holder) > 30:
            raise LedgerError("开户人姓名需为 2-30 个字符")
        raw = (card_no or "").replace(" ", "").replace("-", "")
        if not (16 <= len(raw) <= 19 and raw.isdigit()):
            raise LedgerError("银行卡号需为 16-19 位数字（演示阶段不校验真伪）")
        masked = f"**** **** **** {raw[-4:]}"
        dup = self.conn.execute(
            "SELECT 1 FROM cards WHERE user_id=? AND source='bound' "
            "AND bank_name=? AND card_no_masked=?",
            (self.user_id, bank, masked)).fetchone()
        if dup:
            raise LedgerError("该银行卡已绑定，请勿重复录入")
        cur = self.conn.execute(
            """INSERT INTO cards
               (user_id, account_id, card_no_masked, card_type, status,
                daily_limit_cents, per_tx_limit_cents, created_at,
                source, bank_name, holder_name, balance_cents)
               VALUES (?, NULL, ?, 'debit', 'active', 0, 0, ?,
                       'bound', ?, ?, ?)""",
            (self.user_id, masked, now_iso(), bank, holder,
             BOUND_CARD_DEMO_BALANCE_CENTS))
        self.conn.commit()
        result = {
            "card_id": cur.lastrowid,
            "bank_name": bank,
            "card_no_masked": masked,
            "holder_name": holder,
            "balance_yuan": cents_to_yuan(BOUND_CARD_DEMO_BALANCE_CENTS),
            "status": "active",
            "source": "bound",
        }
        audit(self.conn, "bind_bound_card",
              {"bank_name": bank, "card_no_masked": masked,
               "holder_name": holder, "user_id": self.user_id},
              result, risk="MED")
        with user_scope():
            record_change(self.conn, self.user_id, category="card", action="bind",
                          target=masked,
                          detail={"card_id": result["card_id"],
                                  "bank_name": bank,
                                  "balance_yuan": result["balance_yuan"],
                                  "demo": True})
        return result

    def unbind_bound_card(self, card_id: int) -> dict:
        card = self._get_card(card_id)
        if card["source"] != "bound":
            raise LedgerError("只能解绑外部绑定卡")
        self.conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
        self.conn.commit()
        result = {"card_id": card_id, "card_no_masked": card["card_no_masked"],
                  "bank_name": card["bank_name"], "unbound": True}
        audit(self.conn, "unbind_bound_card",
              {"card_id": card_id, "user_id": self.user_id}, result, risk="MED")
        with user_scope():
            record_change(self.conn, self.user_id, category="card", action="unbind",
                          target=card["card_no_masked"],
                          detail={"card_id": card_id, "bank_name": card["bank_name"]})
        return result

    def deposit_from_card(self, account_id: int, card_id: int,
                          amount_cents: int, *, note: str = "",
                          idempotency_key: str = "") -> dict:
        """[MED] 从已绑定银行卡(虚拟余额)转入本人账户;幂等,不接真实银行。"""
        if amount_cents <= 0:
            raise LedgerError("入金金额必须大于 0")
        key = (idempotency_key or uuid.uuid4().hex).strip()
        ref = f"carddeposit:{self.user_id}:{key}"
        conn = self.conn

        def existing_result() -> dict | None:
            row = conn.execute(
                "SELECT * FROM transactions WHERE external_ref=?", (ref,)
            ).fetchone()
            if not row:
                return None
            card = conn.execute(
                "SELECT balance_cents FROM cards WHERE id=?", (card_id,)
            ).fetchone()
            return {
                "account_id": row["account_id"],
                "amount_yuan": cents_to_yuan(row["amount_cents"]),
                "balance_after_yuan": cents_to_yuan(row["balance_after_cents"]),
                "transaction_id": row["id"],
                "card_id": card_id,
                "card_balance_after_yuan": cents_to_yuan(
                    card["balance_cents"] if card else 0),
                "source": "card",
                "duplicate": True,
            }

        found = existing_result()
        if found:
            return found

        ts = now_iso()
        try:
            conn.execute("BEGIN IMMEDIATE")
            acct = conn.execute(
                "SELECT * FROM accounts WHERE id=? AND user_id=?",
                (account_id, self.user_id)).fetchone()
            if not acct:
                raise LedgerError("账户不存在")
            card = conn.execute(
                "SELECT * FROM cards WHERE id=? AND user_id=? "
                "AND source='bound' AND status='active'",
                (card_id, self.user_id)).fetchone()
            if not card:
                raise LedgerError("绑定银行卡不存在或已解绑")
            if card["balance_cents"] < amount_cents:
                raise LedgerError(
                    f"银行卡虚拟余额不足:当前 {cents_to_yuan(card['balance_cents'])} 元")
            found = existing_result()
            if found:
                conn.rollback()
                return found
            card_after = card["balance_cents"] - amount_cents
            new_balance = acct["balance_cents"] + amount_cents
            conn.execute("UPDATE cards SET balance_cents=? WHERE id=?",
                         (card_after, card_id))
            conn.execute("UPDATE accounts SET balance_cents=? WHERE id=?",
                         (new_balance, account_id))
            cur = conn.execute(
                """INSERT INTO transactions
                   (account_id, ts, direction, amount_cents, balance_after_cents,
                    tx_type, counterparty, category, channel, memo, external_ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (account_id, ts, "in", amount_cents, new_balance,
                 "transfer_in", f"{card['bank_name']} {card['card_no_masked']}",
                 "入金", "app", note or f"从{card['bank_name']}银行卡转入",
                 ref))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            found = existing_result()
            if found:
                return found
            raise
        except Exception:
            conn.rollback()
            raise

        result = {
            "account_id": account_id,
            "amount_yuan": cents_to_yuan(amount_cents),
            "balance_after_yuan": cents_to_yuan(new_balance),
            "transaction_id": cur.lastrowid,
            "card_id": card_id,
            "card_balance_after_yuan": cents_to_yuan(card_after),
            "source": "card",
            "duplicate": False,
        }
        audit(conn, "card_deposit",
              {"account_id": account_id, "card_id": card_id,
               "amount_cents": amount_cents, "note": note,
               "user_id": self.user_id},
              {**result, "card_before_cents": card["balance_cents"]}, risk="MED")
        with user_scope():
            record_money(conn, self.user_id, tool="card_deposit",
                         delta_cents=amount_cents,
                         before_cents=acct["balance_cents"],
                         after_cents=new_balance, account_id=account_id,
                         ref=ref, note=note or f"从{card['bank_name']}银行卡转入")
        return result
