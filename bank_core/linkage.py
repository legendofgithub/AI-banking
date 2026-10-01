"""跨场景联动服务：事件 → 预算锁定 → 定时提醒 → 到期执行（生日剧本的骨架）。

比赛剧本「检测我爱人生期 → 当月锁定 1000 元活期 → 生日前 2 天订购鲜花/蛋糕」
的资金安全铁律落点（与 ledger/wealth 同一套模式）：
- 建计划只"建单"不动钱：预算锁定 = LedgerService.create_transfer_order 从
  活期（账户1）向理财专户（账户2）建 pending_confirm 转账单（收款人写自己、
  备注'生日预留'），用户确认后才真正划转（两步走铁律）；
- execute_linkage_action 是动钱动作：从活期生成 online 购买流水、真实扣款，
  与 confirm_transfer_order 同一个 BEGIN IMMEDIATE 事务模式，
  幂等键 linkage:{plan_id}:{action_idx} 防重放；
- 所有写操作 audit 留痕：建计划/执行动作 risk=HIGH，取消 risk=MED。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

from .db import audit, now_iso
from .ledger import LedgerError, LedgerService
from .money import cents_to_yuan
from .recorder import record_change, record_money

# 提醒任务在"事件日期 - days_before"当天的固定时刻触发（早上 9 点，演示可控）
REMINDER_HOUR = 9


class LinkageError(Exception):
    """业务拒绝（计划状态冲突、余额不足等），信息可直接展示给用户。"""


def _next_occurrence(event_date: str, repeat_yearly: int, today: date | None = None) -> date:
    """事件日期 → 下一次发生日（repeat_yearly 滚动到今年/明年）。
    与 events.py suggest_linkage 的取日期逻辑保持一致，联动两边看到同一天。
    """
    d = datetime.strptime(event_date, "%Y-%m-%d").date()
    today = today or date.today()
    if repeat_yearly:
        d = d.replace(year=today.year)
        if d < today:
            d = d.replace(year=today.year + 1)
    return d


def _gift_category(action: dict) -> str:
    """购买流水的类别按常理归类：鲜花/蛋糕/礼品 → '礼品'，其余 → '生活'。"""
    text = f"{action.get('what', '')}{action.get('merchant', '')}"
    return "礼品" if any(k in text for k in ("花", "蛋糕", "礼")) else "生活"


class LinkageService:
    def __init__(self, conn: sqlite3.Connection, user_id: int = 1):
        self.conn = conn
        self.user_id = user_id

    # ------------------------------------------------------------- 建计划

    def create_linkage_plan(self, event_id: int, title: str, budget_cents: int,
                            actions: list[dict]) -> dict:
        """建联动计划：预算锁定转账单（只建单）+ 每动作一条提醒任务。

        顺序上先建锁定单再落计划：ledger.create_transfer_order 内部自带
        commit（且自带 policy_check/幂等处理），无法并进外层事务；先建单、
        后在同一事务里落 plan+任务，失败则回滚并取消已建的单——不留半截计划。
        """
        if budget_cents <= 0:
            raise LinkageError("预算金额必须为正数")
        ev = self.conn.execute(
            "SELECT * FROM user_events WHERE id=? AND user_id=?",
            (event_id, self.user_id)).fetchone()
        if not ev:
            raise LinkageError("事件不存在，请先用 add_event 记录生日等事件")
        if not actions:
            raise LinkageError("至少需要一个动作（如订购鲜花/蛋糕）")

        checking = self._account_by_type("checking")
        savings = self._account_by_type("savings")
        user = self.conn.execute(
            "SELECT * FROM users WHERE id=?", (self.user_id,)).fetchone()
        event_day = _next_occurrence(ev["event_date"], ev["repeat_yearly"])

        # 规范化动作：金额转分入库，days_before 限 0-90（防把提醒排到几十年后）
        norm: list[dict] = []
        for i, a in enumerate(actions):
            amount = int(a.get("amount_cents", 0))
            if amount <= 0:
                raise LinkageError(f"第 {i} 个动作金额必须为正数")
            days_before = int(a.get("days_before", 2))
            if not 0 <= days_before <= 90:
                raise LinkageError(f"第 {i} 个动作 days_before 须在 0-90 天内")
            norm.append({
                "type": a.get("type", "order"),
                "what": a.get("what", "礼物"),
                "merchant": a.get("merchant", "商户"),
                "amount_cents": amount,
                "days_before": days_before,
                "done": False, "done_at": None,
            })

        # 预算锁定单：活期 → 理财专户，收款人写自己（陈明），备注'生日预留'。
        # 只建单 pending_confirm 不动钱；用户确认后走 confirm_transfer_order。
        ledger = LedgerService(self.conn, self.user_id)
        lock = ledger.create_transfer_order(
            from_account_id=checking["id"], amount_cents=budget_cents,
            to_name=user["name"], to_account_tail=f"{savings['id']:04d}",
            memo="生日预留")

        try:
            cur = self.conn.execute(
                """INSERT INTO linkage_plans
                   (user_id, event_id, title, budget_cents, lock_order_id,
                    actions_json, status, created_at)
                   VALUES (?,?,?,?,?,?, 'active', ?)""",
                (self.user_id, event_id, title, budget_cents, lock["id"],
                 json.dumps(norm, ensure_ascii=False), now_iso()))
            plan_id = cur.lastrowid
            # 每个动作一条 reminder 任务：run_at = 事件日 - days_before
            for i, a in enumerate(norm):
                run_at = (event_day - timedelta(days=a["days_before"])
                          ).isoformat() + f"T{REMINDER_HOUR:02d}:00:00"
                payload = {
                    "title": f"{ev['title']}前{a['days_before']}天：该订购{a['what']}了"
                             f"（{a['merchant']} 约{cents_to_yuan(a['amount_cents'])} 元）",
                    "plan_id": plan_id, "action_idx": i,
                    "what": a["what"], "merchant": a["merchant"],
                    "amount_cents": a["amount_cents"],
                }
                tcur = self.conn.execute(
                    """INSERT INTO scheduled_tasks
                       (user_id, task_type, payload_json, run_at, created_at)
                       VALUES (?, 'reminder', ?, ?, ?)""",
                    (self.user_id, json.dumps(payload, ensure_ascii=False),
                     run_at, now_iso()))
                a["task_id"] = tcur.lastrowid
            self.conn.execute(
                "UPDATE linkage_plans SET actions_json=? WHERE id=?",
                (json.dumps(norm, ensure_ascii=False), plan_id))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            # 计划没落成，锁定单也不能留（否则冒出一笔无人认领的待确认转账）
            try:
                ledger.cancel_transfer_order(lock["id"])
            except LedgerError:
                pass
            raise

        result = {
            "plan_id": plan_id, "title": title, "status": "active",
            "event": {"id": ev["id"], "type": ev["event_type"],
                      "title": ev["title"], "date": event_day.isoformat()},
            "budget_yuan": cents_to_yuan(budget_cents),
            "lock_order": {"order_id": lock["id"], "status": lock["status"],
                           "amount_yuan": cents_to_yuan(budget_cents),
                           "from_account": checking["name"],
                           "to_name": user["name"], "memo": "生日预留",
                           "note": "锁定单未动钱，需用户 confirm 后才划转"},
            "actions": [{"idx": i, "what": a["what"], "merchant": a["merchant"],
                         "amount_yuan": cents_to_yuan(a["amount_cents"]),
                         "days_before": a["days_before"],
                         "run_at": (event_day - timedelta(days=a["days_before"])
                                    ).isoformat() + f"T{REMINDER_HOUR:02d}:00:00",
                         "task_id": a["task_id"]}
                        for i, a in enumerate(norm)],
        }
        audit(self.conn, "create_linkage_plan",
              {"plan_id": plan_id, "event_id": event_id,
               "budget_cents": budget_cents, "actions": len(norm)},
              {"status": "active", "lock_order_id": lock["id"]}, risk="HIGH")
        record_change(self.conn, self.user_id, category="linkage", action="create",
                      target=title,
                      detail={"plan_id": plan_id, "event": ev["title"],
                              "budget_yuan": cents_to_yuan(budget_cents),
                              "lock_order_id": lock["id"], "actions": len(norm)})
        return result

    # ------------------------------------------------------------- 查计划

    def get_linkage_plan(self, plan_id: int) -> dict:
        """计划全景：锁定单状态 + 各动作/提醒任务进度（闸门卡片与播报用）。"""
        plan = self._get_plan(plan_id)
        ev = self.conn.execute(
            "SELECT * FROM user_events WHERE id=?", (plan["event_id"],)).fetchone()
        actions = json.loads(plan["actions_json"])
        out_actions = []
        for i, a in enumerate(actions):
            task = None
            if a.get("task_id"):
                t = self.conn.execute(
                    "SELECT id, run_at, status FROM scheduled_tasks WHERE id=?",
                    (a["task_id"],)).fetchone()
                if t:
                    task = {"id": t["id"], "run_at": t["run_at"], "status": t["status"]}
            out_actions.append({
                "idx": i, "type": a.get("type", "order"), "what": a["what"],
                "merchant": a["merchant"],
                "amount_yuan": cents_to_yuan(a["amount_cents"]),
                "days_before": a["days_before"], "done": bool(a["done"]),
                "done_at": a.get("done_at"), "task": task,
            })
        lock = None
        if plan["lock_order_id"]:
            o = self.conn.execute(
                "SELECT * FROM transfer_orders WHERE id=?", (plan["lock_order_id"],)
            ).fetchone()
            if o:
                lock = {"order_id": o["id"], "status": o["status"],
                        "amount_yuan": cents_to_yuan(o["amount_cents"]),
                        "to_name": o["to_name"], "memo": o["memo"]}
        done_n = sum(1 for a in actions if a["done"])
        result = {
            "plan_id": plan_id, "title": plan["title"], "status": plan["status"],
            "event": ({"id": ev["id"], "type": ev["event_type"], "title": ev["title"],
                       "date": ev["event_date"]} if ev else None),
            "budget_yuan": cents_to_yuan(plan["budget_cents"]),
            "lock_order": lock, "actions": out_actions,
            "progress": f"{done_n}/{len(actions)}", "all_done": done_n == len(actions),
        }
        audit(self.conn, "get_linkage_plan", {"plan_id": plan_id},
              {"status": plan["status"], "progress": result["progress"]})
        return result

    # ------------------------------------------------------------- 执行动作

    def execute_linkage_action(self, plan_id: int, action_idx: int) -> dict:
        """执行一个动作（动钱）：从活期生成 online 购买流水，真实扣款。

        与 confirm_transfer_order 同一事务模式：BEGIN IMMEDIATE 内二次校验
        余额、更新余额快照、落流水、推进计划状态，要么全成要么全不成。
        """
        plan = self._get_plan(plan_id)
        if plan["status"] != "active":
            raise LinkageError(f"计划状态为 {plan['status']}，不可执行动作")
        actions = json.loads(plan["actions_json"])
        if not 0 <= action_idx < len(actions):
            raise LinkageError(f"动作下标越界：0-{len(actions) - 1}")
        a = actions[action_idx]
        if a["done"]:
            raise LinkageError(f"动作「{a['what']}」已执行过，不可重复扣款")

        acct = self._account_by_type("checking")
        category = _gift_category(a)
        conn = self.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT balance_cents FROM accounts WHERE id=?", (acct["id"],)).fetchone()
            if row["balance_cents"] < a["amount_cents"]:
                conn.rollback()
                raise LinkageError(
                    f"余额不足：当前 {cents_to_yuan(row['balance_cents'])} 元，"
                    f"需 {cents_to_yuan(a['amount_cents'])} 元")
            new_balance = row["balance_cents"] - a["amount_cents"]
            conn.execute("UPDATE accounts SET balance_cents=? WHERE id=?",
                         (new_balance, acct["id"]))
            ts = now_iso()
            cur = conn.execute(
                """INSERT INTO transactions
                   (account_id, ts, direction, amount_cents, balance_after_cents,
                    tx_type, counterparty, counterparty_tail, category, channel,
                    memo, external_ref)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (acct["id"], ts, "out", a["amount_cents"], new_balance,
                 "online", a["merchant"], "", category, "online",
                 f"联动计划·{a['what']}", f"linkage:{plan_id}:{action_idx}"))
            # 动作置 done；全部完成则计划置 done
            a["done"] = True
            a["done_at"] = ts
            all_done = all(x["done"] for x in actions)
            conn.execute(
                "UPDATE linkage_plans SET actions_json=?, status=? WHERE id=?",
                (json.dumps(actions, ensure_ascii=False),
                 "done" if all_done else plan["status"], plan_id))
            # 已购买的动作，剩余 pending 提醒一并取消（不再弹"该订鲜花了"）
            if a.get("task_id"):
                conn.execute(
                    "UPDATE scheduled_tasks SET status='cancelled' "
                    "WHERE id=? AND status='pending'", (a["task_id"],))
            conn.commit()
        except LinkageError:
            raise
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            # external_ref 幂等键撞车 = 并发重放同一动作，宁可失败可见
            raise LinkageError(f"动作「{a['what']}」已产生购买流水，拒绝重复执行") from exc
        except Exception:
            conn.rollback()
            raise
        result = {
            "plan_id": plan_id, "action_idx": action_idx, "what": a["what"],
            "merchant": a["merchant"], "category": category,
            "amount_yuan": cents_to_yuan(a["amount_cents"]),
            "balance_after_yuan": cents_to_yuan(new_balance),
            "transaction_id": cur.lastrowid,
            "plan_status": "done" if all_done else "active",
        }
        audit(self.conn, "execute_linkage_action",
              {"plan_id": plan_id, "action_idx": action_idx,
               "amount_cents": a["amount_cents"]}, result, risk="HIGH")
        record_money(self.conn, self.user_id, tool="execute_linkage_action",
                     delta_cents=-a["amount_cents"],
                     before_cents=row["balance_cents"], after_cents=new_balance,
                     account_id=acct["id"], ref=f"linkage:{plan_id}:{action_idx}",
                     note=f"{a['what']}·{a['merchant']}")
        return result

    # ------------------------------------------------------------- 取消计划

    def cancel_linkage_plan(self, plan_id: int) -> dict:
        """取消未完成的计划：锁定单未执行则一并取消；pending 提醒全部撤销。"""
        plan = self._get_plan(plan_id)
        if plan["status"] == "done":
            raise LinkageError("计划已完成，不可取消")
        if plan["status"] == "cancelled":
            raise LinkageError("计划已取消")

        # 锁定单：pending_confirm/scheduled 可取消；已 executed 的钱已划走，不回收
        lock_status = None
        if plan["lock_order_id"]:
            o = self.conn.execute(
                "SELECT status FROM transfer_orders WHERE id=?",
                (plan["lock_order_id"],)).fetchone()
            lock_status = o["status"] if o else None
            if lock_status in ("pending_confirm", "scheduled"):
                LedgerService(self.conn, self.user_id).cancel_transfer_order(
                    plan["lock_order_id"])
                lock_status = "cancelled"

        # 撤销该计划剩余 pending 提醒任务（按 actions_json 里记录的 task_id）
        cancelled_tasks: list[int] = []
        for a in json.loads(plan["actions_json"]):
            tid = a.get("task_id")
            if tid:
                cur = self.conn.execute(
                    "UPDATE scheduled_tasks SET status='cancelled' "
                    "WHERE id=? AND status='pending'", (tid,))
                if cur.rowcount:
                    cancelled_tasks.append(tid)
        self.conn.execute(
            "UPDATE linkage_plans SET status='cancelled' WHERE id=?", (plan_id,))
        self.conn.commit()
        result = {
            "plan_id": plan_id, "status": "cancelled",
            "lock_order": ({"order_id": plan["lock_order_id"], "status": lock_status}
                           if plan["lock_order_id"] else None),
            "cancelled_task_ids": cancelled_tasks,
            "note": "锁定单与提醒任务已一并撤销" if cancelled_tasks or
                    lock_status == "cancelled" else "",
        }
        audit(self.conn, "cancel_linkage_plan", {"plan_id": plan_id},
              {"status": "cancelled", "lock_order_status": lock_status,
               "cancelled_tasks": len(cancelled_tasks)}, risk="MED")
        record_change(self.conn, self.user_id, category="linkage", action="cancel",
                      target=plan["title"],
                      detail={"plan_id": plan_id, "lock_order_status": lock_status,
                              "cancelled_tasks": len(cancelled_tasks)})
        return result

    # ------------------------------------------------------------- 内部

    def _get_plan(self, plan_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM linkage_plans WHERE id=? AND user_id=?",
            (plan_id, self.user_id)).fetchone()
        if not row:
            raise LinkageError("联动计划不存在")
        return row

    def _account_by_type(self, acct_type: str) -> sqlite3.Row:
        """按类型取该用户的第一个账户（演示库：checking=账户1，savings=账户2）。"""
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE user_id=? AND type=? ORDER BY id",
            (self.user_id, acct_type)).fetchone()
        if not row:
            raise LinkageError(f"缺少 {acct_type} 类型账户，无法完成联动")
        return row
