"""分析服务：账单分类统计、月报、异常检测、订阅周期挖掘。

全部为确定性代码：同一个库永远得出同一个结论，LLM 只负责把这些数字讲成人话。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import mean, pstdev

from .db import audit, now_iso
from .money import cents_to_yuan


# 报表口径：排除内部资金划转（自己账户互转、申购/赎回属资产调配，不是收支）
_INTERNAL_FILTER = (
    " AND tx_type NOT IN ('subscribe','redeem')"
    " AND counterparty NOT IN (SELECT name FROM accounts WHERE user_id=:uid)"
)


class AnalysisService:
    def __init__(self, conn: sqlite3.Connection, user_id: int = 1):
        self.conn = conn
        self.user_id = user_id

    # ------------------------------------------------------------- 分类统计

    def category_summary(self, start: str, end: str, direction: str = "out") -> dict:
        rows = self.conn.execute(
            """SELECT category, COUNT(*) n, SUM(amount_cents) s
               FROM transactions
               WHERE account_id IN (SELECT id FROM accounts WHERE user_id=:uid)
                 AND ts >= :start AND ts < :end AND direction=:dir"""
            + _INTERNAL_FILTER + """
               GROUP BY category ORDER BY s DESC""",
            {"uid": self.user_id, "start": start, "end": end, "dir": direction},
        ).fetchall()
        total = sum(r["s"] for r in rows)
        items = []
        for r in rows:
            items.append({
                "category": r["category"], "count": r["n"],
                "amount_yuan": cents_to_yuan(r["s"]),
                "pct": round(r["s"] * 100 / total, 1) if total else 0.0,
            })
        result = {"start": start, "end": end, "direction": direction,
                  "total_yuan": cents_to_yuan(total), "items": items}
        audit(self.conn, "category_summary", {"start": start, "end": end},
              {"total": result["total_yuan"], "categories": len(items)})
        return result

    def top_merchants(self, start: str, end: str, top_n: int = 10) -> list[dict]:
        rows = self.conn.execute(
            """SELECT counterparty, COUNT(*) n, SUM(amount_cents) s
               FROM transactions
               WHERE account_id IN (SELECT id FROM accounts WHERE user_id=:uid)
                 AND ts >= :start AND ts < :end AND direction='out'"""
            + _INTERNAL_FILTER + """
               GROUP BY counterparty ORDER BY s DESC LIMIT :n""",
            {"uid": self.user_id, "start": start, "end": end, "n": top_n},
        ).fetchall()
        out = [{"merchant": r["counterparty"], "count": r["n"],
                "amount_yuan": cents_to_yuan(r["s"])} for r in rows]
        audit(self.conn, "top_merchants", {"start": start, "end": end}, {"n": len(out)})
        return out

    def monthly_report(self, month: str) -> dict:
        """month: 'YYYY-MM'。输出收支总览 + 分类 + 环比。"""
        year, mon = month.split("-")
        start = f"{month}-01"
        if mon == "12":
            next_month = f"{int(year) + 1}-01"
        else:
            next_month = f"{year}-{int(mon) + 1:02d}"
        prev_end = start
        prev_start_date = (datetime.strptime(start, "%Y-%m-%d")
                           - timedelta(days=1)).replace(day=1)
        prev_start = prev_start_date.strftime("%Y-%m-%d")

        def _sum(direction: str, s: str, e: str) -> int:
            return self.conn.execute(
                """SELECT COALESCE(SUM(amount_cents),0) s FROM transactions
                   WHERE account_id IN (SELECT id FROM accounts WHERE user_id=:uid)
                     AND ts>=:s AND ts<:e AND direction=:dir"""
                + _INTERNAL_FILTER,
                {"uid": self.user_id, "s": s, "e": e, "dir": direction}).fetchone()["s"]

        income = _sum("in", start, next_month)
        expense = _sum("out", start, next_month)
        prev_income = _sum("in", prev_start, prev_end)
        prev_expense = _sum("out", prev_start, prev_end)

        def _mom(cur: int, prev: int):
            if prev == 0:
                return None
            return round((cur - prev) * 100 / prev, 1)

        cats = self.category_summary(start, next_month, "out")
        result = {
            "month": month,
            "income_yuan": cents_to_yuan(income),
            "expense_yuan": cents_to_yuan(expense),
            "net_yuan": cents_to_yuan(income - expense),
            "mom_income_pct": _mom(income, prev_income),
            "mom_expense_pct": _mom(expense, prev_expense),
            "top_categories": cats["items"][:6],
            "savings_rate_pct": round((income - expense) * 100 / income, 1) if income else None,
        }
        audit(self.conn, "monthly_report", {"month": month},
              {"income": result["income_yuan"], "expense": result["expense_yuan"]})
        return result

    # ------------------------------------------------------------- 异常检测

    def detect_anomalies(self, days: int = 90) -> list[dict]:
        """三条确定性规则（可解释、可回放）：
        R1 重复扣款：7 天内同一对方+同一金额出现 ≥3 次
        R2 大额离群：金额超过该商户历史均值 + 3σ（且样本 ≥5）
        R3 异常时段大额：00:00-05:59 之间单笔 ≥2000 元
        """
        end = now_iso()
        start = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = self.conn.execute(
            """SELECT * FROM transactions
               WHERE account_id IN (SELECT id FROM accounts WHERE user_id=?)
                 AND ts >= ? AND direction='out'
               ORDER BY ts""",
            (self.user_id, start),
        ).fetchall()
        anomalies: list[dict] = []

        # R1 重复扣款
        groups: dict[tuple, list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            groups[(r["counterparty"], r["amount_cents"])].append(r)
        for (cp, amt), txs in groups.items():
            for t in txs:
                window = [x for x in txs
                          if abs(_pd(t["ts"]) - _pd(x["ts"])) <= timedelta(days=7)]
                if len(window) >= 3:
                    anomalies.append({
                        "rule": "R1_重复扣款",
                        "evidence": (f"7 天内 {cp} 同金额 "
                                     f"{cents_to_yuan(amt)} 元扣款 {len(window)} 次"),
                        "transaction_ids": [x["id"] for x in window],
                        "suggest": "可能是重复订阅或重复支付，建议核对",
                    })
                    break

        # R2 大额离群（按商户历史）
        by_cp: dict[str, list[int]] = defaultdict(list)
        for r in rows:
            by_cp[r["counterparty"]].append(r["amount_cents"])
        for r in rows:
            hist = by_cp[r["counterparty"]]
            if len(hist) >= 5 and r["amount_cents"] > 20000:  # 200 元起检
                m, s = mean(hist), pstdev(hist)
                if s > 0 and r["amount_cents"] > m + 3 * s:
                    anomalies.append({
                        "rule": "R2_大额离群",
                        "evidence": (f"{r['counterparty']} 本笔 "
                                     f"{cents_to_yuan(r['amount_cents'])} 元，"
                                     f"远超其历史均值 {cents_to_yuan(round(m))} 元"),
                        "transaction_ids": [r["id"]],
                        "ts": r["ts"],
                        "suggest": "请确认是否本人操作",
                    })

        # R3 异常时段大额
        for r in rows:
            hour = int(r["ts"][11:13])
            if 0 <= hour <= 5 and r["amount_cents"] >= 200000:
                anomalies.append({
                    "rule": "R3_异常时段大额",
                    "evidence": (f"{r['ts']} 在凌晨 {hour} 点消费 "
                                 f"{cents_to_yuan(r['amount_cents'])} 元（{r['counterparty']}）"),
                    "transaction_ids": [r["id"]],
                    "ts": r["ts"],
                    "suggest": "凌晨大额消费，建议核实",
                })

        anomalies.sort(key=lambda a: a.get("ts", ""), reverse=True)
        audit(self.conn, "detect_anomalies", {"days": days},
              {"found": len(anomalies)})
        return anomalies

    # ------------------------------------------------------------- 订阅挖掘

    def detect_subscriptions(self, window_days: int = 365) -> list[dict]:
        """周期性扣费挖掘（默认看满一年窗口）：
        同一(商户,金额)在窗口内出现 ≥3 次、相邻间隔方差小（±3 天）→ 判定订阅。
        同时检测"涨价/降价"：同商户最新金额 ≠ 常见金额 → 提示。
        结果写入 subscriptions 表（幂等 upsert）。
        """
        start = (datetime.now() - timedelta(days=window_days)).strftime("%Y-%m-%d")
        rows = self.conn.execute(
            """SELECT * FROM transactions
               WHERE account_id IN (SELECT id FROM accounts WHERE user_id=?)
                 AND ts >= ? AND direction='out' AND counterparty != ''
               ORDER BY ts""",
            (self.user_id, start),
        ).fetchall()

        groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            groups[r["counterparty"]].append(r)

        found: list[dict] = []
        for cp, txs in groups.items():
            # 按金额聚类（同一商户可能有多档订阅），取出现最多的一档定周期
            by_amt: dict[int, list[sqlite3.Row]] = defaultdict(list)
            for t in txs:
                by_amt[t["amount_cents"]].append(t)
            major_amt, major_txs = max(by_amt.items(), key=lambda kv: len(kv[1]))
            if len(major_txs) < 3:
                continue
            dates = sorted(datetime.fromisoformat(t["ts"][:19]) for t in major_txs)
            gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
            base = gaps[0] if gaps else 0
            if not all(abs(g - base) <= 3 for g in gaps) or base < 7 or base > 95:
                continue
            # 下次扣款按商户"最新一笔"推算（涨过价也算最新档期）
            latest_tx = max(txs, key=lambda t: t["ts"])
            last_date = datetime.fromisoformat(latest_tx["ts"][:19])
            nxt = (last_date + timedelta(days=base)).strftime("%Y-%m-%d")
            # 涨价/降价提示：最新金额偏离常见金额
            price_note = ""
            if latest_tx["amount_cents"] > major_amt:
                price_note = (f"注意：最新一期已涨至 "
                              f"{cents_to_yuan(latest_tx['amount_cents'])} 元"
                              f"（原 {cents_to_yuan(major_amt)} 元）")
            elif latest_tx["amount_cents"] < major_amt:
                price_note = (f"好消息：最新一期降至 "
                              f"{cents_to_yuan(latest_tx['amount_cents'])} 元"
                              f"（原 {cents_to_yuan(major_amt)} 元）")
            rec = {
                "merchant": cp,
                "category": major_txs[-1]["category"],
                "amount_yuan": cents_to_yuan(latest_tx["amount_cents"]),
                "base_amount_yuan": cents_to_yuan(major_amt),
                "period_days": base,
                "next_charge_date": nxt,
                "annual_cost_yuan": cents_to_yuan(round(365 / base * major_amt)),
                "price_change": price_note,
                "last_tx_id": latest_tx["id"],
            }
            found.append(rec)
            self._upsert_subscription(rec)

        # 已取消的订阅不要覆盖回 active；同商户多档金额只留一条（优先带涨价提示的）
        merged: dict[str, dict] = {}
        for rec in found:
            cur = merged.get(rec["merchant"])
            if cur is None or (not cur["price_change"] and rec["price_change"]):
                merged[rec["merchant"]] = rec
        found = list(merged.values())
        found.sort(key=lambda x: x["annual_cost_yuan"], reverse=True)
        audit(self.conn, "detect_subscriptions", {"window_days": window_days},
              {"found": len(found)})
        return found

    def _upsert_subscription(self, rec: dict) -> None:
        row = self.conn.execute(
            "SELECT id, status FROM subscriptions WHERE user_id=? AND merchant_name=?",
            (self.user_id, rec["merchant"])).fetchone()
        if row is None:
            self.conn.execute(
                """INSERT INTO subscriptions
                   (user_id, merchant_name, category, amount_cents, period_days,
                    next_charge_date, status, last_tx_id, detected_at)
                   VALUES (?,?,?,?,?,?,'active',?,?)""",
                (self.user_id, rec["merchant"], rec["category"],
                 _yuan(rec["base_amount_yuan"]),
                 rec["period_days"], rec["next_charge_date"], rec["last_tx_id"], now_iso()))
        elif row["status"] == "active":
            self.conn.execute(
                """UPDATE subscriptions SET amount_cents=?, period_days=?,
                   next_charge_date=?, last_tx_id=?, detected_at=? WHERE id=?""",
                (_yuan(rec["amount_yuan"]), rec["period_days"], rec["next_charge_date"],
                 rec["last_tx_id"], now_iso(), row["id"]))
        self.conn.commit()

    def list_subscriptions(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? ORDER BY status, next_charge_date",
            (self.user_id,)).fetchall()
        out = []
        for r in rows:
            out.append({
                "id": r["id"], "merchant": r["merchant_name"], "status": r["status"],
                "amount_yuan": cents_to_yuan(r["amount_cents"]),
                "period_days": r["period_days"], "next_charge_date": r["next_charge_date"],
                "annual_cost_yuan": cents_to_yuan(round(365 / r["period_days"]
                                                       * r["amount_cents"])),
            })
        audit(self.conn, "list_subscriptions", {}, {"n": len(out)})
        return out

    def cancel_subscription(self, sub_id: int) -> dict:
        """一键取消代扣（演示：改状态 + 记审计，不再有后续扣款）。"""
        row = self.conn.execute(
            "SELECT * FROM subscriptions WHERE id=? AND user_id=?", (sub_id, self.user_id)
        ).fetchone()
        if not row:
            raise ValueError("订阅不存在")
        if row["status"] == "cancelled":
            return {"subscription_id": sub_id, "status": "cancelled",
                    "note": "已是取消状态"}
        self.conn.execute(
            "UPDATE subscriptions SET status='cancelled' WHERE id=?", (sub_id,))
        self.conn.commit()
        result = {"subscription_id": sub_id, "merchant": row["merchant_name"],
                  "status": "cancelled",
                  "note": "代扣协议已解除，下一期不再扣款"}
        audit(self.conn, "cancel_subscription", {"sub_id": sub_id}, result, risk="MED")
        return result


def _yuan(yuan_str: str) -> int:
    """'30.00' -> 3000 分（内部用）。"""
    from .money import yuan_to_cents
    return yuan_to_cents(yuan_str)


def _pd(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:19])
