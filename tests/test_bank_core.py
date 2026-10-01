"""假银行核心测试：先跑通资金安全铁律，再验证分析戏眼。"""

from __future__ import annotations

import sqlite3

import pytest

from bank_core.analysis import AnalysisService
from bank_core.db import connect, init_db
from bank_core.events import EventService
from bank_core.ledger import LedgerError, LedgerService
from bank_core.money import cents_to_yuan, yuan_to_cents
from bank_core.seed import seed
from bank_core.wealth import WealthService


# ----------------------------------------------------------------- 基础设施

@pytest.fixture()
def seeded_db(tmp_path):
    path = tmp_path / "bank.db"
    seed(path)
    conn = connect(path)
    yield conn
    conn.close()


def _mk_account(conn: sqlite3.Connection, balance_cents: int = 1000000) -> int:
    conn.execute("INSERT OR IGNORE INTO users (id,name,phone,created_at) "
                 "VALUES (1,'测试用户','13800000000','2026-01-01T00:00:00')")
    cur = conn.execute(
        "INSERT INTO accounts (user_id,type,name,balance_cents,opened_at) "
        "VALUES (1,'checking','t',?,'2026-01-01T00:00:00')", (balance_cents,))
    conn.commit()
    return cur.lastrowid


# ----------------------------------------------------------------- 金额换算

def test_money_roundtrip():
    assert yuan_to_cents("5000") == 500000
    assert yuan_to_cents("99.9") == 9990
    assert yuan_to_cents("0.01") == 1
    assert cents_to_yuan(9990) == "99.90"
    with pytest.raises(ValueError):
        yuan_to_cents("-5")
    with pytest.raises(ValueError):
        yuan_to_cents("abc")


# ----------------------------------------------------------------- 转账闸门

def test_transfer_two_step_and_balance(tmp_path):
    conn = init_db(tmp_path / "t.db")
    acct = _mk_account(conn, 1000000)
    conn.execute("INSERT INTO contacts (id,user_id,name,phone,relation,note) "
                 "VALUES (1,1,'林悦','13900008821','spouse','')")
    conn.commit()
    led = LedgerService(conn, 1)

    # 建单不动钱
    order = led.create_transfer_order(acct, 50000, to_contact_id=1, memo="test")
    assert order["status"] == "pending_confirm"
    bal = conn.execute("SELECT balance_cents FROM accounts WHERE id=?", (acct,)).fetchone()
    assert bal["balance_cents"] == 1000000  # 未扣款

    # 确认才扣款，且流水带余额快照
    res = led.confirm_transfer_order(order["id"])
    assert res["status"] == "executed"
    assert res["balance_after_yuan"] == "9500.00"
    tx = conn.execute("SELECT * FROM transactions WHERE external_ref=?",
                      (f"order:{order['id']}",)).fetchone()
    assert tx["balance_after_cents"] == 950000


def test_transfer_insufficient_funds(tmp_path):
    conn = init_db(tmp_path / "t.db")
    acct = _mk_account(conn, 1000)  # 10 元
    led = LedgerService(conn, 1)
    order = led.create_transfer_order(acct, 50000, to_name="某人")
    with pytest.raises(LedgerError, match="余额不足"):
        led.confirm_transfer_order(order["id"])
    status = conn.execute("SELECT status FROM transfer_orders WHERE id=?",
                          (order["id"],)).fetchone()
    assert status["status"] == "failed"


def test_transfer_idempotency(tmp_path):
    conn = init_db(tmp_path / "t.db")
    acct = _mk_account(conn, 1000000)
    led = LedgerService(conn, 1)
    o1 = led.create_transfer_order(acct, 10000, to_name="A", idempotency_key="k1")
    o2 = led.create_transfer_order(acct, 10000, to_name="A", idempotency_key="k1")
    assert o1["id"] == o2["id"] and "幂等命中" in o2.get("note", "")


def test_daily_limit_gate(tmp_path):
    conn = init_db(tmp_path / "t.db")
    acct = _mk_account(conn, 100000000)  # 100 万，余额足够
    led = LedgerService(conn, 1)
    # 当日累计超 1 万 -> 第二笔在建单时即被拒
    o1 = led.create_transfer_order(acct, 800000, to_name="A")
    led.confirm_transfer_order(o1["id"])
    with pytest.raises(LedgerError, match="风控拒绝"):
        led.create_transfer_order(acct, 300000, to_name="B")  # 8000+3000 > 10000


def test_split_bill_math(tmp_path):
    conn = init_db(tmp_path / "t.db")
    conn.execute("INSERT INTO users (id,name,phone,created_at) "
                 "VALUES (1,'测试用户','13800000000','2026-01-01T00:00:00')")
    conn.commit()
    led = LedgerService(conn, 1)
    bill = led.create_split_bill("火锅", 30000,
                                 [{"name": "老王", "share_cents": 10000},
                                  {"name": "小刘", "share_cents": 10000},
                                  {"name": "我", "share_cents": 10000}])
    d = led.get_split_bill(bill["bill_id"])
    assert d["total_yuan"] == "300.00" and d["paid_count"] == 0
    led.settle_split_bill_item(bill["bill_id"], "老王")
    d = led.get_split_bill(bill["bill_id"])
    assert d["paid_count"] == 1 and d["status"] == "collecting"
    led.settle_split_bill_item(bill["bill_id"], "小刘")
    led.settle_split_bill_item(bill["bill_id"], "我")
    assert led.get_split_bill(bill["bill_id"])["status"] == "settled"
    with pytest.raises(LedgerError):
        led.create_split_bill("错账", 10000, [{"name": "A", "share_cents": 4000}])


def test_card_lost_irreversible(tmp_path):
    conn = init_db(tmp_path / "t.db")
    conn.execute("INSERT OR IGNORE INTO users (id,name,phone,created_at) "
                 "VALUES (1,'u','p','2026-01-01T00:00:00')")
    cur = conn.execute(
        "INSERT INTO cards (user_id,card_no_masked,card_type,status,created_at) "
        "VALUES (1,'6222****1','debit','active','2026-01-01T00:00:00')")
    conn.commit()
    led = LedgerService(conn, 1)
    led.set_card_status(cur.lastrowid, "lost")
    with pytest.raises(LedgerError, match="挂失"):
        led.set_card_status(cur.lastrowid, "active")


# ----------------------------------------------------------------- 理财闸门

def test_wealth_risk_gate_and_two_step(tmp_path):
    conn = init_db(tmp_path / "t.db")
    acct = _mk_account(conn, 100000000)
    conn.execute(
        "INSERT INTO wealth_products (code,name,p_type,risk_level,expected_return_bps,"
        "min_subscribe_cents) VALUES ('X1','高风险股票','mixed',5,900,100)")
    conn.execute(
        "INSERT INTO risk_profiles (user_id,answers_json,score,level,updated_at) "
        "VALUES (1,'{}',15,'C3','2026-01-01T00:00:00')")
    conn.commit()
    w = WealthService(conn, 1)
    pid = conn.execute("SELECT id FROM wealth_products").fetchone()["id"]
    # 风险超限直接拒绝
    with pytest.raises(Exception, match="风险"):
        w.subscribe_product(pid, 10000, acct, confirmed=True)
    # 降为 R2 可买：先未确认 -> 再确认扣款
    conn.execute("UPDATE wealth_products SET risk_level=2 WHERE id=?", (pid,))
    conn.commit()
    r1 = w.subscribe_product(pid, 10000, acct, confirmed=False)
    assert r1["status"] == "pending_confirm"
    bal0 = conn.execute("SELECT balance_cents FROM accounts WHERE id=?",
                        (acct,)).fetchone()["balance_cents"]
    r2 = w.subscribe_product(pid, 10000, acct, confirmed=True)
    assert r2["status"] == "executed"
    bal1 = conn.execute("SELECT balance_cents FROM accounts WHERE id=?",
                        (acct,)).fetchone()["balance_cents"]
    assert bal1 == bal0 - 10000


# ----------------------------------------------------------------- 种子与分析戏眼

def test_seed_deterministic(tmp_path):
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    seed(a)
    seed(b)
    qa = connect(a)
    qb = connect(b)
    sa = qa.execute("SELECT COUNT(*) n, COALESCE(SUM(amount_cents),0) s "
                    "FROM transactions").fetchone()
    sb = qb.execute("SELECT COUNT(*) n, COALESCE(SUM(amount_cents),0) s "
                    "FROM transactions").fetchone()
    assert (sa["n"], sa["s"]) == (sb["n"], sb["s"])
    qa.close()
    qb.close()


def test_seed_balance_invariant(seeded_db):
    conn = seeded_db
    for acct in conn.execute("SELECT * FROM accounts").fetchall():
        ins = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM transactions "
            "WHERE account_id=? AND direction='in'", (acct["id"],)).fetchone()["s"]
        outs = conn.execute(
            "SELECT COALESCE(SUM(amount_cents),0) s FROM transactions "
            "WHERE account_id=? AND direction='out'", (acct["id"],)).fetchone()["s"]
        opening = 4500000 if acct["id"] == 1 else 0  # 期初 45000 元
        assert acct["balance_cents"] == opening + ins - outs
        # 账单时间序下的余额快照链必须连续（戏眼回插日期不破坏对账单展示）
        prev = opening
        for r in conn.execute(
                "SELECT direction, amount_cents, balance_after_cents "
                "FROM transactions WHERE account_id=? ORDER BY ts, id",
                (acct["id"],)).fetchall():
            prev += r["amount_cents"] if r["direction"] == "in" else -r["amount_cents"]
            assert r["balance_after_cents"] == prev
        assert prev == acct["balance_cents"]


def test_subscription_detection(seeded_db):
    found = AnalysisService(seeded_db, 1).detect_subscriptions()
    merchants = {f["merchant"] for f in found}
    assert "腾讯视频VIP" in merchants
    assert "中国移动" in merchants
    assert "张阿姨" in merchants  # 房租也是周期性支出
    tx = next(f for f in found if f["merchant"] == "腾讯视频VIP")
    assert "涨" in tx["price_change"]  # 涨价戏眼


def test_anomaly_detection(seeded_db):
    anomalies = AnalysisService(seeded_db, 1).detect_anomalies()
    rules = {a["rule"] for a in anomalies}
    assert "R1_重复扣款" in rules  # 迅雷 15 元 x3
    assert "R3_异常时段大额" in rules  # 京东凌晨 4999
    r1 = next(a for a in anomalies if a["rule"] == "R1_重复扣款")
    assert "迅雷" in r1["evidence"]


def test_monthly_report_math(seeded_db):
    conn = seeded_db
    month = conn.execute(
        "SELECT substr(ts,1,7) m FROM transactions GROUP BY m ORDER BY m DESC LIMIT 1"
    ).fetchone()["m"]
    rep = AnalysisService(conn, 1).monthly_report(month)
    yuan = lambda s: float(s)  # noqa: E731
    assert yuan(rep["income_yuan"]) - yuan(rep["expense_yuan"]) == pytest.approx(
        yuan(rep["net_yuan"]), abs=0.01)


# ----------------------------------------------------------------- 事件联动

def test_events_and_linkage(seeded_db):
    ev = EventService(seeded_db, 1)
    upcoming = ev.list_events(upcoming_days=60)
    titles = [e["title"] for e in upcoming]
    assert any("生日" in t for t in titles)
    plan = ev.suggest_linkage(upcoming[0]["id"])
    assert any(s["action"] == "reserve_money" for s in plan["steps"])
    assert all(s["needs_confirm"] for s in plan["steps"])  # 只建议不越权


def test_due_tasks_fire(seeded_db):
    ev = EventService(seeded_db, 1)
    past = "2020-01-01T00:00:00"
    ev.schedule_reminder("旧提醒", past)
    fired = ev.run_due_tasks()
    assert any(f["kind"] == "reminder" for f in fired)


# ----------------------------------------------------------------- 收款人录入

def test_add_contact_roundtrip(seeded_db):
    """每组 姓名+手机号+备注 存一行;入库后 resolve_contact 能按姓名/手机号找到。"""
    led = LedgerService(seeded_db, 1)
    row = led.add_contact("张三", "13800138000", "房租合租室友")
    assert row["name"] == "张三"
    assert row["phone"] == "13800138000"
    assert row["note"] == "房租合租室友"
    by_name = led.resolve_contact(name="张三")
    by_phone = led.resolve_contact(phone="13800138000")
    assert by_name and by_phone and by_name[0]["id"] == row["id"]
    # 再录第二组 → 又一行,互不覆盖
    row2 = led.add_contact("李四", "13900139000", "")
    assert row2["id"] != row["id"] and row2["note"] == ""


def test_add_contact_duplicate_phone(seeded_db):
    """手机号唯一:重复录入报错,原记录不被覆盖。"""
    led = LedgerService(seeded_db, 1)
    led.add_contact("王五", "13700137000", "健身搭子")
    with pytest.raises(LedgerError, match="已是联系人"):
        led.add_contact("王五二号", "13700137000", "想顶替")
    kept = led.resolve_contact(phone="13700137000")
    assert len(kept) == 1 and kept[0]["name"] == "王五"


def test_add_contact_validation(seeded_db):
    """姓名/手机号格式由确定性代码把关:空姓名、非 1 开头、位数不对全部拒绝。"""
    led = LedgerService(seeded_db, 1)
    with pytest.raises(LedgerError, match="姓名"):
        led.add_contact("", "13800138000")
    with pytest.raises(LedgerError, match="手机号"):
        led.add_contact("赵六", "23800138000")   # 不以 1 开头
    with pytest.raises(LedgerError, match="手机号"):
        led.add_contact("赵六", "1380013800")    # 10 位
    with pytest.raises(LedgerError, match="手机号"):
        led.add_contact("赵六", "138abc8000")    # 含字母
    assert led.resolve_contact(name="赵六") == []
