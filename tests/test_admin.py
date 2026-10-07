"""管理后台测试：双库记录器（money_ledger / change_log）+ 服务层钩子 + admin API。

覆盖三条主线：
1. 前端动钱（confirm_transfer_order）→ money_ledger 落"前后余额快照"记录；
2. 前端改数据（add_contact）→ change_log 落功能修改记录；
   admin_scope() 内操作 → operator='admin'，与 agent 区分；
3. admin API 全链路（TestClient）：总览/调账（-3000 对账场景）/联系人维护/卡片/撤单，
   每个管理端写操作在双库中 operator=admin。

测试库全部临时（tmp_path），不触碰 data/bank.db 与真实记录库。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bank_core import recorder
from bank_core.admin_api import create_admin_app
from bank_core.db import init_db
from bank_core.ledger import LedgerService

BALANCE = 1_000_000  # 1 万元（分）


def _mk_bank(tmp_path: Path) -> sqlite3.Connection:
    """1 用户(陈明) + 1 checking + 1 联系人(张三) + 1 张活跃借记卡。"""
    conn = init_db(tmp_path / "bank.db")
    conn.execute(
        "INSERT INTO users (id,name,phone,created_at) "
        "VALUES (1,'陈明','13800000000','2026-01-01T00:00:00')")
    conn.execute(
        "INSERT INTO accounts (id,user_id,type,name,balance_cents,opened_at) "
        f"VALUES (1,1,'checking','工资卡',{BALANCE},'2026-01-01T00:00:00')")
    conn.execute(
        "INSERT INTO contacts (id,user_id,name,phone,relation,note) "
        "VALUES (1,1,'张三','13800138000','friend','')")
    conn.execute(
        "INSERT INTO cards (id,user_id,account_id,card_no_masked,card_type,status,"
        "daily_limit_cents,per_tx_limit_cents,created_at) "
        "VALUES (1,1,1,'6222 **** **** 0001','debit','active',"
        "5000000,2000000,'2026-01-01T00:00:00')")
    conn.commit()
    return conn


@pytest.fixture()
def rec_env(tmp_path, monkeypatch):
    """临时银行库；记录库按"跟随银行库目录"规则自动落在同一临时目录，天然隔离。"""
    monkeypatch.delenv("DEMO_USER_NICKNAME", raising=False)
    yield _mk_bank(tmp_path)


def _rows(path: Path, sql: str) -> list[sqlite3.Row]:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    try:
        return c.execute(sql).fetchall()
    finally:
        c.close()


# ----------------------------------------------------------------- 钩子层

def test_transfer_writes_money_record(rec_env):
    """前端确认转账 1000 元 → money_ledger 一条 delta=-1000、前后余额快照。"""
    ledger = LedgerService(rec_env, 1)
    order = ledger.create_transfer_order(1, 100_000, to_contact_id=1, to_name="张三")
    ledger.confirm_transfer_order(order["id"])
    rows = _rows(recorder.money_db_path(rec_env), "SELECT * FROM money_records")
    assert len(rows) == 1
    r = rows[0]
    assert r["tool"] == "confirm_transfer_order"
    assert r["nickname"] == "陈明"
    assert r["delta_cents"] == -100_000
    assert r["before_cents"] == BALANCE
    assert r["after_cents"] == BALANCE - 100_000
    assert r["ref"] == f"order:{order['id']}"
    assert r["operator"] == "agent"


def test_contact_add_writes_change_record_and_operator(rec_env):
    """默认链路 operator=agent；admin_scope 内 operator=admin。"""
    ledger = LedgerService(rec_env, 1)
    ledger.add_contact("王芳", "13700001111", "测试")
    with recorder.admin_scope():
        ledger.add_contact("李四", "13700002222", "管理端录入")
    rows = _rows(recorder.change_db_path(rec_env),
                 "SELECT * FROM change_records ORDER BY id")
    assert [r["action"] for r in rows] == ["add", "add"]
    assert [r["operator"] for r in rows] == ["agent", "admin"]
    assert all(r["category"] == "contact" for r in rows)
    assert all(r["nickname"] == "陈明" for r in rows)


# ----------------------------------------------------------------- API 层

@pytest.fixture()
def client(rec_env, tmp_path):
    app = create_admin_app(tmp_path / "bank.db")
    return TestClient(app)


def test_overview(client):
    r = client.get("/api/overview")
    assert r.status_code == 200
    d = r.json()
    assert d["nickname"] == "陈明"
    assert d["accounts"][0]["balance_yuan"] == "10000.00"
    assert "stats" in d and "today_money_count" in d["stats"]


def test_adjust_minus_3000_roundtrip(client):
    """核心对账场景：管理端扣 3000 → 账户余额少 3000 + money_ledger 留痕。"""
    r = client.post("/api/adjust",
                    json={"account_id": 1, "amount_yuan": "-3000",
                          "reason": "演示对账扣款"})
    assert r.status_code == 200
    assert r.json()["new_balance_yuan"] == "7000.00"
    recs = client.get("/api/money-records").json()["records"]
    assert len(recs) == 1
    rec = recs[0]
    assert rec["delta_yuan"] == "-3000.00"
    assert rec["before_yuan"] == "10000.00"
    assert rec["after_yuan"] == "7000.00"
    assert rec["operator"] == "admin"
    # 余额在业务库里真实变了
    ov = client.get("/api/overview").json()
    assert ov["accounts"][0]["balance_yuan"] == "7000.00"


def test_admin_contact_add_and_delete_forbidden(client):
    """权限边界:管理员可代客录入联系人,但删除入口已移除——
    个人数据不容后台销毁(接口不存在,数据原样保留)。"""
    r = client.post("/api/contacts",
                    json={"name": "王芳", "phone": "13700001111", "note": "后台录入"})
    assert r.status_code == 200
    cid = r.json()["id"]
    recs = client.get("/api/change-records", params={"category": "contact"}
                      ).json()["records"]
    assert recs[0]["operator"] == "admin"
    assert recs[0]["action"] == "add"
    # 删除路由不存在(405),联系人原样保留
    d = client.delete(f"/api/contacts/{cid}")
    assert d.status_code == 405
    names = [c["name"] for c in client.get("/api/contacts").json()["contacts"]]
    assert "王芳" in names


def test_admin_card_lock(client):
    r = client.post("/api/cards/1/status", json={"status": "locked"})
    assert r.status_code == 200
    cards = client.get("/api/cards").json()["cards"]
    assert cards[0]["status"] == "locked"
    recs = client.get("/api/change-records", params={"category": "card"}
                      ).json()["records"]
    assert recs[0]["action"] == "status"
    assert recs[0]["operator"] == "admin"


def test_admin_cancel_pending_order(rec_env, client):
    """建一张待确认单（模拟前端幂等未命中的孤儿单）→ 管理端撤销 + 留痕。"""
    ledger = LedgerService(rec_env, 1)
    order = ledger.create_transfer_order(1, 50_000, to_contact_id=1, to_name="张三")
    r = client.post(f"/api/orders/{order['id']}/cancel")
    assert r.status_code == 200
    orders = client.get("/api/orders", params={"status": "cancelled"}
                        ).json()["orders"]
    assert any(o["id"] == order["id"] for o in orders)
    recs = client.get("/api/change-records", params={"category": "transfer"}
                      ).json()["records"]
    assert recs[0]["action"] == "cancel_order"
    assert recs[0]["operator"] == "admin"
    # 撤单不动钱：money_ledger 仍为空
    assert client.get("/api/money-records").json()["records"] == []


# ----------------------------------------------------------------- 多用户汇总

def test_users_listing_and_within_filter(rec_env, client):
    """用户汇总：全量列出；注册时间筛选命中窗口内用户；总数不受筛选影响。"""
    from datetime import datetime, timedelta
    three_days_ago = (datetime.now() - timedelta(days=3)).isoformat(
        timespec="seconds")
    rec_env.execute(
        "INSERT INTO users (id,name,phone,created_at) "
        "VALUES (2,'测试用户B','13900000002',?)", (three_days_ago,))
    rec_env.execute(
        "INSERT INTO accounts (id,user_id,type,name,balance_cents,opened_at) "
        "VALUES (2,2,'checking','B的工资卡',250000,'2026-01-01T00:00:00')")
    rec_env.commit()

    all_users = client.get("/api/users").json()
    assert all_users["total_users"] == 2
    by_id = {u["user_id"]: u for u in all_users["users"]}
    assert by_id[1]["nickname"] == "陈明"
    assert by_id[2]["total_balance_yuan"] == "2500.00"
    assert by_id[2]["accounts"] == 1

    week = client.get("/api/users", params={"within": "week"}).json()
    assert [u["user_id"] for u in week["users"]] == [2]  # 仅 3 天前注册的 B
    assert week["total_users"] == 2  # 总数不受筛选影响


def test_records_scoped_by_user(rec_env, client):
    """双库记录按用户隔离：user1 转账的流水，user2 查不到。"""
    rec_env.execute(
        "INSERT INTO users (id,name,phone,created_at) "
        "VALUES (2,'测试用户B','13900000002','2026-01-02T00:00:00')")
    rec_env.commit()
    ledger = LedgerService(rec_env, 1)
    order = ledger.create_transfer_order(1, 20_000, to_contact_id=1, to_name="张三")
    ledger.confirm_transfer_order(order["id"])
    u1 = client.get("/api/money-records", params={"user_id": 1}).json()["records"]
    u2 = client.get("/api/money-records", params={"user_id": 2}).json()["records"]
    assert len(u1) == 1 and u1[0]["nickname"] == "陈明"
    assert u2 == []
