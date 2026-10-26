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

    def annual_report(self, year: int | str, as_of: str | None = None) -> dict:
        """年度账单报告:全年收支总览 + 逐月走势 + 分类/商户 Top + 同比。

        year: 2026 或 "2026";as_of 截止日(默认今天;当年报告只统计到当天,
        往年按自然年 12-31 收口)。
        口径与月报一致:排除 subscribe/redeem 与"收款方是本人账户"的内部划转
        ——资产调配不是收支。所有数字均为原始流水汇总,本函数不做任何推断。
        """
        y = int(str(year).strip()[:4])
        ref = (as_of or now_iso())[:10]
        start = f"{y}-01-01"
        full_end = f"{y + 1}-01-01"  # 次年首日(半开区间上界)
        current = f"{y:04d}" == ref[:4]
        if current:
            end = (datetime.strptime(ref, "%Y-%m-%d")
                   + timedelta(days=1)).strftime("%Y-%m-%d")
            end_inclusive = ref
            months_covered = int(ref[5:7])
        else:
            end = full_end
            end_inclusive = f"{y}-12-31"
            months_covered = 12 if y < int(ref[:4]) else 0

        rows = self.conn.execute(
            """SELECT substr(ts,1,7) m, direction d, SUM(amount_cents) s
               FROM transactions
               WHERE account_id IN (SELECT id FROM accounts WHERE user_id=:uid)
                 AND ts>=:s AND ts<:e"""
            + _INTERNAL_FILTER + """
               GROUP BY m, d""",
            {"uid": self.user_id, "s": start, "e": end}).fetchall()
        by_month: dict[str, dict[str, int]] = defaultdict(lambda: {"in": 0, "out": 0})
        for r in rows:
            by_month[r["m"]][r["d"]] += r["s"]

        monthly: list[dict] = []
        for mon in range(1, max(months_covered, 0) + 1):
            key = f"{y:04d}-{mon:02d}"
            inc, exp = by_month[key]["in"], by_month[key]["out"]
            monthly.append({"month": key, "income_yuan": cents_to_yuan(inc),
                            "expense_yuan": cents_to_yuan(exp),
                            "net_yuan": cents_to_yuan(inc - exp)})
        income = sum(by_month[k]["in"] for k in by_month)
        expense = sum(by_month[k]["out"] for k in by_month)

        # 同比:上年同口径年度合计(无数据 → None,前端不显示)
        prev_start, prev_end = f"{y - 1}-01-01", f"{y}-01-01"
        prow = self.conn.execute(
            """SELECT direction d, COALESCE(SUM(amount_cents),0) s
               FROM transactions
               WHERE account_id IN (SELECT id FROM accounts WHERE user_id=:uid)
                 AND ts>=:s AND ts<:e"""
            + _INTERNAL_FILTER + """
               GROUP BY direction""",
            {"uid": self.user_id, "s": prev_start, "e": prev_end}).fetchall()
        prev = {r["d"]: r["s"] for r in prow}

        def _yoy(cur: int, before: int):
            if not before:
                return None
            return round((cur - before) * 100 / before, 1)

        exp_months = [m for m in monthly if m["expense_yuan"]]
        max_month = max(exp_months, key=lambda m: float(m["expense_yuan"]),
                        default=None) if exp_months else None
        min_month = min(exp_months, key=lambda m: float(m["expense_yuan"]),
                        default=None) if exp_months else None
        cats = self.category_summary(start, end, "out")
        merchants = self.top_merchants(start, end, 8)
        result = {
            "year": y,
            "start": start, "end": end_inclusive,
            "months_covered": len(monthly),
            "income_yuan": cents_to_yuan(income),
            "expense_yuan": cents_to_yuan(expense),
            "net_yuan": cents_to_yuan(income - expense),
            "savings_rate_pct": round((income - expense) * 100 / income, 1)
            if income else None,
            "avg_monthly_expense_yuan": cents_to_yuan(round(expense / len(monthly)))
            if monthly else "0.00",
            "max_month": max_month and {"month": max_month["month"],
                                        "expense_yuan": max_month["expense_yuan"]},
            "min_month": min_month and {"month": min_month["month"],
                                        "expense_yuan": min_month["expense_yuan"]},
            "yoy_income_pct": _yoy(income, prev.get("in", 0)),
            "yoy_expense_pct": _yoy(expense, prev.get("out", 0)),
            "monthly": monthly,
            "top_categories": cats["items"][:8],
            "top_merchants": merchants,
        }
        audit(self.conn, "annual_report", {"year": y, "as_of": end_inclusive},
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
                "category": r["category"],
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
        # 双库留痕(2026-10-07 补):对话链路取消代扣也要进管理台修改记录
        from .recorder import record_change
        record_change(self.conn, self.user_id, category="subscription",
                      action="cancel", target=row["merchant_name"],
                      detail={"sub_id": sub_id,
                              "amount_cents": row["amount_cents"]})
        return result

    # ------------------------------------------------- 代扣预审(事前知情)

    def _ensure_precheck_col(self) -> None:
        """老库自愈补列(CREATE TABLE IF NOT EXISTS 不改已存在的表;
        订阅预审列 2026-10-20 加入,任何连接路径首次使用时自动补齐)。"""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(subscriptions)")}
        if cols and "precheck_ack_date" not in cols:
            self.conn.execute(
                "ALTER TABLE subscriptions ADD COLUMN "
                "precheck_ack_date TEXT NOT NULL DEFAULT ''")
            self.conn.commit()

    @staticmethod
    def _roll_next(next_date: str, as_of_date: str, period_days: int) -> str:
        """挖掘滞后的 next_charge_date 可能在过去——按周期滚到 as_of 起的首个未来日。"""
        d = datetime.strptime(next_date, "%Y-%m-%d").date()
        a = datetime.strptime(as_of_date, "%Y-%m-%d").date()
        while d < a:
            d += timedelta(days=period_days)
        return d.isoformat()

    def upcoming_precharges(self, as_of: str | None = None,
                            within_days: int = 3) -> list[dict]:
        """即将扣费预审清单(纯只读,不动钱)。

        语义:对每个 active 订阅,给出 as_of 起未来 within_days 天内将发生的
        下一期扣费(含金额与历史常见档的涨价对照);已放行/跳过的周期
        (precheck_ack_date 命中)不再重复提醒。
        """
        self._ensure_precheck_col()
        as_of_date = (as_of or now_iso())[:10]
        rows = self.conn.execute(
            "SELECT * FROM subscriptions WHERE user_id=? AND status='active' "
            "ORDER BY next_charge_date", (self.user_id,)).fetchall()
        out = []
        for r in rows:
            period = r["period_days"] or 30
            nxt = self._roll_next(r["next_charge_date"], as_of_date, period)
            gap = (datetime.strptime(nxt, "%Y-%m-%d")
                   - datetime.strptime(as_of_date, "%Y-%m-%d")).days
            if gap > within_days or (r["precheck_ack_date"] or "") == nxt:
                continue
            # 涨价对照:订阅行存的是常见档金额;下一期预期扣费取该商户
            # 最新一笔交易,与常见档(transactions 无 user_id,经 accounts 过滤)
            base = self.conn.execute(
                "SELECT t.amount_cents, COUNT(*) c FROM transactions t "
                "JOIN accounts a ON a.id = t.account_id "
                "WHERE a.user_id=? AND t.counterparty=? "
                "GROUP BY t.amount_cents ORDER BY c DESC LIMIT 1",
                (self.user_id, r["merchant_name"])).fetchone()
            latest = self.conn.execute(
                "SELECT t.amount_cents FROM transactions t "
                "JOIN accounts a ON a.id = t.account_id "
                "WHERE a.user_id=? AND t.counterparty=? "
                "ORDER BY t.ts DESC LIMIT 1",
                (self.user_id, r["merchant_name"])).fetchone()
            base_cents = base["amount_cents"] if base else r["amount_cents"]
            cur_cents = latest["amount_cents"] if latest else r["amount_cents"]
            note = ""
            if base_cents and cur_cents != base_cents:
                note = (f"较常见扣费 {cents_to_yuan(base_cents)} 元"
                        + ("已涨价" if cur_cents > base_cents else "已降价"))
            out.append({
                "sub_id": r["id"], "merchant": r["merchant_name"],
                "category": r["category"],
                "amount_yuan": cents_to_yuan(cur_cents),
                "base_amount_yuan": cents_to_yuan(base_cents),
                "price_note": note, "next_charge_date": nxt,
                "period_days": period, "days_left": gap,
                "annual_yuan": cents_to_yuan(round(365 / period * r["amount_cents"])),
            })
        audit(self.conn, "upcoming_precharges",
              {"as_of": as_of_date, "within_days": within_days}, {"n": len(out)})
        return out

    def decide_precharge(self, sub_id: int, decision: str,
                         charge_date: str | None = None) -> dict:
        """代扣预审决策:allow=放行本期(仅记录);skip=跳过本期(顺延一周期)。

        不动钱(沙箱无自动扣款引擎),决策留 audit + change_log;
        解约走 cancel_subscription(MED/密码闸门),不在此列。
        """
        self._ensure_precheck_col()
        if decision not in ("allow", "skip"):
            raise ValueError("decision 只允许 allow/skip")
        row = self.conn.execute(
            "SELECT * FROM subscriptions WHERE id=? AND user_id=?",
            (sub_id, self.user_id)).fetchone()
        if not row:
            raise ValueError("订阅不存在")
        if row["status"] != "active":
            raise ValueError("订阅已取消,无需预审")
        period = row["period_days"] or 30
        ack = charge_date or self._roll_next(
            row["next_charge_date"], now_iso()[:10], period)
        if decision == "skip":
            nxt = (datetime.strptime(ack, "%Y-%m-%d")
                   + timedelta(days=period)).date().isoformat()
            self.conn.execute(
                "UPDATE subscriptions SET precheck_ack_date=?, next_charge_date=? "
                "WHERE id=?", (ack, nxt, sub_id))
            note = f"已跳过 {ack} 这一期,下期 {nxt}"
        else:
            self.conn.execute(
                "UPDATE subscriptions SET precheck_ack_date=? WHERE id=?",
                (ack, sub_id))
            note = f"已放行 {ack} 这一期扣费"
        self.conn.commit()
        result = {"subscription_id": sub_id, "merchant": row["merchant_name"],
                  "decision": decision, "charge_date": ack, "note": note}
        audit(self.conn, "decide_precharge",
              {"sub_id": sub_id, "decision": decision, "charge_date": ack},
              result, risk="LOW")
        from .recorder import record_change
        record_change(self.conn, self.user_id, category="subscription",
                      action=f"precheck_{decision}", target=row["merchant_name"],
                      detail={"sub_id": sub_id, "charge_date": ack,
                              "amount_cents": row["amount_cents"]})
        return result


def _yuan(yuan_str: str) -> int:
    """'30.00' -> 3000 分（内部用）。"""
    from .money import yuan_to_cents
    return yuan_to_cents(yuan_str)


def _pd(ts: str) -> datetime:
    return datetime.fromisoformat(ts[:19])
