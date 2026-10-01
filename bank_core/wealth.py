"""理财服务：产品库、风险测评、持仓、申购/赎回（同样两步确认走订单）。"""

from __future__ import annotations

import sqlite3

from .db import audit, now_iso
from .money import cents_to_yuan
from .recorder import record_change, record_money

# 风险匹配规则：用户风险等级 C1-C5 可购买 R1-R(等级) 产品
_LEVEL_TO_RISK = {"C1": 1, "C2": 2, "C3": 3, "C4": 4, "C5": 5}


class WealthError(Exception):
    pass


class WealthService:
    def __init__(self, conn: sqlite3.Connection, user_id: int = 1):
        self.conn = conn
        self.user_id = user_id

    def list_products(self, p_type: str | None = None,
                      max_risk_level: int | None = None,
                      keyword: str | None = None) -> list[dict]:
        sql = "SELECT * FROM wealth_products WHERE 1=1"
        args: list = []
        if p_type:
            sql += " AND p_type=?"
            args.append(p_type)
        if max_risk_level:
            sql += " AND risk_level<=?"
            args.append(max_risk_level)
        if keyword:
            sql += " AND (name LIKE ? OR intro LIKE ?)"
            args += [f"%{keyword}%", f"%{keyword}%"]
        sql += " ORDER BY expected_return_bps DESC"
        rows = self.conn.execute(sql, args).fetchall()
        out = [self._product_d(r) for r in rows]
        audit(self.conn, "list_products",
              {"p_type": p_type, "max_risk": max_risk_level, "keyword": keyword},
              {"n": len(out)})
        return out

    def get_product(self, product_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM wealth_products WHERE id=?",
                                (product_id,)).fetchone()
        if not row:
            raise WealthError("产品不存在")
        return self._product_d(row)

    def compare_products(self, product_ids: list[int]) -> list[dict]:
        out = []
        for pid in product_ids:
            row = self.conn.execute("SELECT * FROM wealth_products WHERE id=?",
                                    (pid,)).fetchone()
            if row:
                out.append(self._product_d(row))
        audit(self.conn, "compare_products", {"ids": product_ids}, {"n": len(out)})
        return out

    def get_risk_profile(self) -> dict | None:
        row = self.conn.execute("SELECT * FROM risk_profiles WHERE user_id=?",
                                (self.user_id,)).fetchone()
        if not row:
            return None
        return {"level": row["level"], "score": row["score"],
                "updated_at": row["updated_at"]}

    def set_risk_profile(self, answers: dict[str, int]) -> dict:
        """对话式测评落库：answers 为题目->分数(1-5)，总分映射 C1-C5。"""
        total = sum(answers.values())
        # 5 题满分 25：≤9 C1，≤13 C2，≤17 C3，≤21 C4，其余 C5
        level = "C1" if total <= 9 else "C2" if total <= 13 else \
                "C3" if total <= 17 else "C4" if total <= 21 else "C5"
        import json
        self.conn.execute(
            """INSERT INTO risk_profiles (user_id, answers_json, score, level, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 answers_json=excluded.answers_json, score=excluded.score,
                 level=excluded.level, updated_at=excluded.updated_at""",
            (self.user_id, json.dumps(answers, ensure_ascii=False), total, level, now_iso()))
        self.conn.commit()
        result = {"level": level, "score": total,
                  "max_product_risk": f"R{_LEVEL_TO_RISK[level]}"}
        audit(self.conn, "set_risk_profile", {"score": total}, result, risk="MED")
        record_change(self.conn, self.user_id, category="wealth",
                      action="risk_assessment", target=level,
                      detail={"score": total, "answers": len(answers)})
        return result

    def get_holdings(self) -> list[dict]:
        rows = self.conn.execute(
            """SELECT h.*, p.name, p.code, p.p_type, p.risk_level
               FROM wealth_holdings h JOIN wealth_products p ON p.id=h.product_id
               WHERE h.user_id=? AND h.status='holding'""",
            (self.user_id,)).fetchall()
        out = [{
            "holding_id": r["id"], "product": r["name"], "code": r["code"],
            "p_type": r["p_type"], "risk_level": r["risk_level"],
            "principal_yuan": cents_to_yuan(r["principal_cents"]),
            "est_value_yuan": cents_to_yuan(r["est_value_cents"]),
            "profit_yuan": cents_to_yuan(r["est_value_cents"] - r["principal_cents"]),
            "profit_pct": round((r["est_value_cents"] - r["principal_cents"])
                                * 100 / r["principal_cents"], 2),
        } for r in rows]
        audit(self.conn, "get_holdings", {}, {"n": len(out)})
        return out

    def subscribe_product(self, product_id: int, amount_cents: int,
                          from_account_id: int, confirmed: bool = False) -> dict:
        """申购。confirmed=False 只返回"待确认单"（Agent 拿给用户确认）；
        confirmed=True 才真正扣款建仓。风险等级超限直接拒绝。"""
        product = self.conn.execute("SELECT * FROM wealth_products WHERE id=?",
                                    (product_id,)).fetchone()
        if not product:
            raise WealthError("产品不存在")
        if amount_cents < product["min_subscribe_cents"]:
            raise WealthError(
                f"低于起购金额 {cents_to_yuan(product['min_subscribe_cents'])} 元")
        profile = self.get_risk_profile()
        if profile and product["risk_level"] > _LEVEL_TO_RISK[profile["level"]]:
            raise WealthError(
                f"产品风险 R{product['risk_level']} 超出您的风险承受等级 "
                f"{profile['level']}（最高可购 R{_LEVEL_TO_RISK[profile['level']]}）")
        if not confirmed:
            return {"status": "pending_confirm", "product": product["name"],
                    "amount_yuan": cents_to_yuan(amount_cents),
                    "note": "请向用户确认后再调用 confirmed=True 执行"}

        acct = self.conn.execute("SELECT * FROM accounts WHERE id=? AND user_id=?",
                                 (from_account_id, self.user_id)).fetchone()
        if not acct:
            raise WealthError("账户不存在")
        if acct["balance_cents"] < amount_cents:
            raise WealthError("余额不足")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            fee = amount_cents * product["subscription_fee_bps"] // 10000
            net = amount_cents - fee
            new_balance = acct["balance_cents"] - amount_cents
            self.conn.execute("UPDATE accounts SET balance_cents=? WHERE id=?",
                              (new_balance, from_account_id))
            ts = now_iso()
            cur = self.conn.execute(
                """INSERT INTO transactions
                   (account_id, ts, direction, amount_cents, balance_after_cents,
                    tx_type, counterparty, category, memo, external_ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (from_account_id, ts, "out", amount_cents, new_balance,
                 "subscribe", product["name"], "理财申购",
                 f"申购{product['name']}", f"wealth:sub:{product_id}:{ts}"))
            self.conn.execute(
                """INSERT INTO wealth_holdings
                   (user_id, product_id, principal_cents, est_value_cents,
                    status, subscribed_at) VALUES (?,?,?,?, 'holding', ?)""",
                (self.user_id, product_id, net, net, ts))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        result = {"status": "executed", "product": product["name"],
                  "amount_yuan": cents_to_yuan(amount_cents),
                  "fee_yuan": cents_to_yuan(fee),
                  "balance_after_yuan": cents_to_yuan(new_balance)}
        audit(self.conn, "subscribe_product",
              {"product_id": product_id, "amount_cents": amount_cents}, result, risk="HIGH")
        record_money(self.conn, self.user_id, tool="subscribe_product",
                     delta_cents=-amount_cents,
                     before_cents=acct["balance_cents"], after_cents=new_balance,
                     account_id=from_account_id,
                     ref=f"wealth:sub:{product_id}",
                     note=f"申购{product['name']}")
        return result

    def redeem_product(self, holding_id: int, confirmed: bool = False) -> dict:
        """全额赎回（演示简化）。"""
        h = self.conn.execute(
            "SELECT h.*, p.name, p.redemption_fee_bps FROM wealth_holdings h "
            "JOIN wealth_products p ON p.id=h.product_id WHERE h.id=? AND h.user_id=?",
            (holding_id, self.user_id)).fetchone()
        if not h or h["status"] != "holding":
            raise WealthError("持仓不存在或已赎回")
        value = h["est_value_cents"]
        fee = value * h["redemption_fee_bps"] // 10000
        net = value - fee
        if not confirmed:
            return {"status": "pending_confirm", "holding_id": holding_id,
                    "product": h["name"], "redeem_net_yuan": cents_to_yuan(net),
                    "note": "请向用户确认后再调用 confirmed=True 执行"}
        # 找一张活期卡接收赎回款（简化）
        acct = self.conn.execute(
            "SELECT * FROM accounts WHERE user_id=? AND type='checking' ORDER BY id",
            (self.user_id,)).fetchone()
        ts = now_iso()
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            new_balance = acct["balance_cents"] + net
            self.conn.execute("UPDATE accounts SET balance_cents=? WHERE id=?",
                              (new_balance, acct["id"]))
            self.conn.execute(
                """INSERT INTO transactions
                   (account_id, ts, direction, amount_cents, balance_after_cents,
                    tx_type, counterparty, category, memo, external_ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (acct["id"], ts, "in", net, new_balance, "redeem", h["name"],
                 "理财赎回", f"赎回{h['name']}", f"wealth:red:{holding_id}:{ts}"))
            self.conn.execute(
                "UPDATE wealth_holdings SET status='redeemed', redeemed_at=? WHERE id=?",
                (ts, holding_id))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        result = {"status": "executed", "product": h["name"],
                  "redeem_net_yuan": cents_to_yuan(net),
                  "fee_yuan": cents_to_yuan(fee),
                  "balance_after_yuan": cents_to_yuan(new_balance)}
        audit(self.conn, "redeem_product", {"holding_id": holding_id}, result, risk="HIGH")
        record_money(self.conn, self.user_id, tool="redeem_product",
                     delta_cents=net,
                     before_cents=acct["balance_cents"], after_cents=new_balance,
                     account_id=acct["id"], ref=f"wealth:red:{holding_id}",
                     note=f"赎回{h['name']}")
        return result

    def _product_d(self, row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "code": row["code"], "name": row["name"],
            "p_type": row["p_type"], "risk_level": f"R{row['risk_level']}",
            "expected_return_pct": row["expected_return_bps"] / 100,
            "min_subscribe_yuan": cents_to_yuan(row["min_subscribe_cents"]),
            "lock_days": row["lock_days"],
            "subscription_fee_pct": row["subscription_fee_bps"] / 100,
            "redemption_fee_pct": row["redemption_fee_bps"] / 100,
            "intro": row["intro"],
        }
