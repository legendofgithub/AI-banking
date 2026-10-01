"""事件与调度服务：用户事件（生日/纪念日/账单日）、提醒任务、到期处理。

跨场景联动的"好记性"就落在这两张表：Agent 从对话里听到"下周我老婆生日"，
调 add_event 记下来；调度器（或演示时的手动触发）轮询 due 任务主动提醒。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta

from .db import audit, now_iso
from .ledger import LedgerService


class EventService:
    def __init__(self, conn: sqlite3.Connection, user_id: int = 1):
        self.conn = conn
        self.user_id = user_id

    # ------------------------------------------------------------- 用户事件

    def list_events(self, upcoming_days: int | None = None) -> list[dict]:
        sql = "SELECT * FROM user_events WHERE user_id=?"
        args: list = [self.user_id]
        rows = self.conn.execute(sql + " ORDER BY event_date", args).fetchall()
        out = []
        today = date.today()
        for r in rows:
            d = datetime.strptime(r["event_date"], "%Y-%m-%d").date()
            if r["repeat_yearly"]:
                d = d.replace(year=today.year)
                if d < today:
                    d = d.replace(year=today.year + 1)
            days_left = (d - today).days
            if upcoming_days is not None and days_left > upcoming_days:
                continue
            out.append({
                "id": r["id"], "event_type": r["event_type"], "title": r["title"],
                "date": d.isoformat(), "days_left": days_left,
                "repeat_yearly": bool(r["repeat_yearly"]), "note": r["note"],
            })
        audit(self.conn, "list_events", {"upcoming_days": upcoming_days}, {"n": len(out)})
        return out

    def add_event(self, event_type: str, title: str, event_date: str,
                  repeat_yearly: bool = False, note: str = "") -> dict:
        if event_type not in ("birthday", "anniversary", "payday", "bill_day", "custom"):
            raise ValueError("event_type 不合法")
        datetime.strptime(event_date, "%Y-%m-%d")  # 校验格式
        cur = self.conn.execute(
            """INSERT INTO user_events
               (user_id, event_type, title, event_date, repeat_yearly, note)
               VALUES (?,?,?,?,?,?)""",
            (self.user_id, event_type, title, event_date, int(repeat_yearly), note))
        self.conn.commit()
        result = {"event_id": cur.lastrowid, "title": title, "date": event_date,
                  "repeat_yearly": repeat_yearly}
        audit(self.conn, "add_event", result, result, risk="LOW")
        return result

    # ------------------------------------------------------------- 提醒/调度

    def schedule_reminder(self, title: str, run_at: str, payload: dict | None = None) -> dict:
        """创建提醒任务（run_at: ISO 时间）。"""
        cur = self.conn.execute(
            """INSERT INTO scheduled_tasks
               (user_id, task_type, payload_json, run_at, created_at)
               VALUES (?,?,?,?,?)""",
            (self.user_id, "reminder",
             json.dumps({"title": title, **(payload or {})}, ensure_ascii=False),
             run_at, now_iso()))
        self.conn.commit()
        result = {"task_id": cur.lastrowid, "type": "reminder", "title": title,
                  "run_at": run_at}
        audit(self.conn, "schedule_reminder", result, result, risk="LOW")
        return result

    def list_scheduled_tasks(self, status: str = "pending") -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM scheduled_tasks WHERE user_id=? AND status=? ORDER BY run_at",
            (self.user_id, status)).fetchall()
        return [{"id": r["id"], "type": r["task_type"], "run_at": r["run_at"],
                 "payload": json.loads(r["payload_json"]), "status": r["status"]}
                for r in rows]

    def run_due_tasks(self, as_of: str | None = None) -> list[dict]:
        """处理所有到期任务（演示用同步触发；生产换 Celery/APScheduler 轮询）。
        - reminder: 标记 done，返回提醒内容（由渠道层推送）
        - scheduled_transfer: 到期转账单转为 pending_confirm，等用户确认
        as_of：可选"当前时间"（ISO 字符串，默认 None=now）。演示时间旅行用——
        比赛现场把时间拨到生日前 2 天，即可验证联动提醒照常触发；
        as_of=None 时行为与旧版完全一致（既有测试零改动）。
        返回项携带 payload 详情：联动提醒里含 plan_id/action_idx，
        供编排层识别"这是哪个计划的哪个动作该执行了"。
        """
        ledger = LedgerService(self.conn, self.user_id)
        now = as_of or now_iso()
        fired: list[dict] = []
        rows = self.conn.execute(
            "SELECT * FROM scheduled_tasks WHERE user_id=? AND status='pending' AND run_at<=?",
            (self.user_id, now)).fetchall()
        for r in rows:
            payload = json.loads(r["payload_json"])
            if r["task_type"] == "reminder":
                self.conn.execute(
                    "UPDATE scheduled_tasks SET status='done' WHERE id=?", (r["id"],))
                fired.append({"task_id": r["id"], "kind": "reminder",
                              "message": payload.get("title", "提醒"),
                              "payload": payload})
            elif r["task_type"] == "scheduled_transfer":
                order_id = payload.get("order_id")
                self.conn.execute(
                    "UPDATE transfer_orders SET status='pending_confirm' "
                    "WHERE id=? AND status='scheduled'", (order_id,))
                self.conn.execute(
                    "UPDATE scheduled_tasks SET status='done' WHERE id=?", (r["id"],))
                fired.append({"task_id": r["id"], "kind": "scheduled_transfer_due",
                              "order_id": order_id,
                              "message": "定时转账已到期，待用户确认执行",
                              "payload": payload})
            self.conn.commit()
        audit(self.conn, "run_due_tasks", {"as_of": as_of}, {"fired": len(fired)})
        return fired

    # ------------------------------------------------------------- 联动建议

    def suggest_linkage(self, event_id: int) -> dict:
        """针对事件生成"联动计划草稿"（只建议，不执行——铁律：主动但不越权）。
        例：生日事件 → 预留资金建议 + 提前 2 天购物提醒。
        """
        ev = self.conn.execute(
            "SELECT * FROM user_events WHERE id=? AND user_id=?",
            (event_id, self.user_id)).fetchone()
        if not ev:
            raise ValueError("事件不存在")
        d = datetime.strptime(ev["event_date"], "%Y-%m-%d").date()
        today = date.today()
        if ev["repeat_yearly"]:
            d = d.replace(year=today.year)
            if d < today:
                d = d.replace(year=today.year + 1)
        days_left = (d - today).days
        plan = {
            "event": ev["title"], "date": d.isoformat(), "days_left": days_left,
            "steps": [
                {"action": "reserve_money",
                 "suggest": "预留一笔资金（金额与用户确认，如 1000 元）",
                 "needs_confirm": True},
                {"action": "shopping_reminder",
                 "run_at": (d - timedelta(days=2)).isoformat(),
                 "suggest": "提前 2 天提醒准备礼物（鲜花/蛋糕）",
                 "needs_confirm": True},
            ],
            "note": "以上为计划草稿，每一步执行前均需用户确认",
        }
        audit(self.conn, "suggest_linkage", {"event_id": event_id},
              {"steps": len(plan["steps"])})
        return plan
