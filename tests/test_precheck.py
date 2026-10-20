"""订阅代扣预审(事前知情)测试:滚动数学、涨价对照、ack 抑制、skip 顺延、
边界拒绝、老库自愈补列。"""

from __future__ import annotations

import sqlite3

import pytest

from bank_core.analysis import AnalysisService
from bank_core.db import connect, init_db
from bank_core.seed import seed


@pytest.fixture()
def seeded_db(tmp_path):
    path = tmp_path / "bank.db"
    seed(path)
    conn = init_db(path)  # SCHEMA+迁移幂等,老库新库统一入口
    yield conn
    conn.close()


def _svc(conn: sqlite3.Connection) -> AnalysisService:
    return AnalysisService(conn, user_id=1)


def _sub(conn: sqlite3.Connection, merchant: str) -> sqlite3.Row:
    return conn.execute(
        "SELECT * FROM subscriptions WHERE user_id=1 AND merchant_name=?",
        (merchant,)).fetchone()


def _bring_into_window(conn: sqlite3.Connection, merchant: str,
                       as_of: str = "2026-10-20") -> None:
    """把指定订阅的下期扣费拨到 as_of 次日,保证落进 3 天窗口。"""
    row = _sub(conn, merchant)
    conn.execute(
        "UPDATE subscriptions SET next_charge_date=? WHERE id=?",
        ("2026-10-21", row["id"]))
    conn.commit()


def test_roll_forward_for_stale_dates(seeded_db):
    """过期/滞后的 next_charge_date 按周期滚到 as_of 起的首个未来日;
    窗口外(如 30 天周期滚到月底)不出现。种子日期锚点不定,先拨入再验。"""
    _bring_into_window(seeded_db, "网易云音乐")
    svc = _svc(seeded_db)
    items = svc.upcoming_precharges(as_of="2026-10-20T09:00:00", within_days=3)
    dates = [i["next_charge_date"] for i in items]
    assert dates, "拨入窗口后应有扣费项"
    for d in dates:
        assert "2026-10-20" <= d <= "2026-10-23"
    item = items[0]
    for key in ("sub_id", "merchant", "amount_yuan", "base_amount_yuan",
                "price_note", "next_charge_date", "period_days", "days_left",
                "annual_yuan"):
        assert key in item, f"预审项缺字段 {key}"
    assert 0 <= item["days_left"] <= 3
    wy = [i for i in items if i["merchant"] == "网易云音乐"]
    assert wy and wy[0]["next_charge_date"] == "2026-10-21"
    # 房租未拨入,不该混进窗口
    assert not [i for i in items if i["merchant"] == "张阿姨"]


def test_price_note_flags_markup(seeded_db):
    """腾讯视频戏眼:当前 30 元 vs 常见档 25 元 → 预审项带涨价提示。"""
    _bring_into_window(seeded_db, "腾讯视频VIP")
    items = _svc(seeded_db).upcoming_precharges(
        as_of="2026-10-20T09:00:00", within_days=3)
    tx = [i for i in items if i["merchant"] == "腾讯视频VIP"]
    assert tx, "拨入窗口后应出现"
    assert "已涨价" in tx[0]["price_note"]
    assert tx[0]["base_amount_yuan"] == "25.00"
    assert tx[0]["amount_yuan"] == "30.00"


def test_allow_suppresses_until_next_cycle(seeded_db):
    """放行:ack 记录本期、next 不动、同周期不再出现在预审清单。"""
    _bring_into_window(seeded_db, "腾讯视频VIP")
    row = _sub(seeded_db, "腾讯视频VIP")
    res = _svc(seeded_db).decide_precharge(
        row["id"], "allow", charge_date="2026-10-21")
    assert res["decision"] == "allow" and res["charge_date"] == "2026-10-21"
    after = _sub(seeded_db, "腾讯视频VIP")
    assert after["precheck_ack_date"] == "2026-10-21"
    assert after["next_charge_date"] == "2026-10-21"  # 放行不改日程
    items = _svc(seeded_db).upcoming_precharges(
        as_of="2026-10-20T09:00:00", within_days=3)
    assert not [i for i in items if i["merchant"] == "腾讯视频VIP"]


def test_skip_defers_one_cycle(seeded_db):
    """跳过本期:扣费日顺延一个周期(30 天),note 说明下期日期。"""
    row = _sub(seeded_db, "腾讯视频VIP")
    res = _svc(seeded_db).decide_precharge(
        row["id"], "skip", charge_date="2026-11-20")
    assert res["note"].count("2026-12-20") == 1
    after = _sub(seeded_db, "腾讯视频VIP")
    assert after["next_charge_date"] == "2026-12-20"
    assert after["precheck_ack_date"] == "2026-11-20"


def test_decide_rejections(seeded_db):
    """非法 decision / 已取消订阅 → 明确拒绝(解约必须走 cancel,MED)。"""
    row = _sub(seeded_db, "腾讯视频VIP")
    with pytest.raises(ValueError, match="allow/skip"):
        _svc(seeded_db).decide_precharge(row["id"], "unsubscribe")
    seeded_db.execute(
        "UPDATE subscriptions SET status='cancelled' WHERE id=?", (row["id"],))
    seeded_db.commit()
    with pytest.raises(ValueError, match="已取消"):
        _svc(seeded_db).decide_precharge(row["id"], "allow")


def test_old_db_self_heals_column(tmp_path):
    """老库(手工建无预审列的表)首次调用预审时自动补列。"""
    path = tmp_path / "old.db"
    seed(path)
    conn = connect(path)
    conn.execute("CREATE TABLE subscriptions_old_backup AS "
                 "SELECT * FROM subscriptions")
    conn.execute("DROP TABLE subscriptions")
    # 重建一个没有 precheck_ack_date 的旧版表结构,再灌回数据
    conn.execute("""
        CREATE TABLE subscriptions (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,
            merchant_name TEXT NOT NULL, category TEXT NOT NULL DEFAULT '订阅',
            amount_cents INTEGER NOT NULL, period_days INTEGER NOT NULL,
            next_charge_date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            last_tx_id INTEGER, detected_at TEXT NOT NULL,
            UNIQUE (user_id, merchant_name))
    """)
    conn.execute(
        "INSERT INTO subscriptions SELECT id,user_id,merchant_name,category,"
        "amount_cents,period_days,next_charge_date,status,last_tx_id,detected_at "
        "FROM subscriptions_old_backup")
    conn.commit()
    svc = AnalysisService(conn, user_id=1)
    svc.upcoming_precharges(as_of="2026-10-20T09:00:00")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(subscriptions)")}
    assert "precheck_ack_date" in cols
    conn.close()
