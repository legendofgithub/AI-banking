"""12 个月种子数据生成器：一键造出一个"有故事"的练功假银行。

确定性：每次播种都用 random.Random(2026) 新实例，结果完全一致。
真实性：同月流水先按时间排序再落库，balance_after 与账单时间序严格一致。

埋好的演示"戏眼"（评委演示用）：
1. 订阅族（固定扣款日）：腾讯视频VIP 8号（25→30 元涨价）、网易云音乐 12号 18、
   iCloud云存储 20号 6、Keep会员 5号 19、中国移动 3号 128 —— 供订阅挖掘
2. R1 重复扣款：「迅雷白金会员」15 元 3 天内扣 3 次（约 3 周前）
3. R3 异常时段大额：京东 4,999 元 凌晨 03:47（约 5 周前）
4. 房租 6,800/月 固定转账（会被订阅挖掘识别为"周期性支出"，属真实特性）
5. 到期演示：3 天后有一笔待执行定时转账（林悦 家用 500）
6. 生日戏眼：林悦生日 = 播种日 + 4 天（近期可演示，如 2026-09-22 播种即 09-26），
   供「锁定预算 → 生日前 2 天订购鲜花/蛋糕」联动剧本现场演出

用法：python -m bank_core.seed [--db 路径]
"""

from __future__ import annotations

import json
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .analysis import AnalysisService
from .db import init_db, now_iso, reset_db
from .ledger import LedgerService
from .money import yuan_to_cents

TODAY = date.today()

USER = {"id": 1, "name": "陈明", "phone": "13800002233"}

CONTACTS = [
    ("林悦", "13900008821", "spouse", "老婆·建行尾号8821"),
    ("陈朵朵", None, "daughter", "女儿"),
    ("张阿姨", "13500003456", "landlord", "房东·收房租"),
    ("老王", "13600001234", "colleague", "同事·球友"),
    ("小刘", "13700005678", "colleague", "同事"),
    ("王秀兰", "13300009999", "parent", "妈妈"),
]

PRODUCTS = [
    # (code, name, type, risk, bps, min元, lock_days, sub_fee_bps, red_fee_bps, intro)
    ("MF001", "余额+货币基金", "money_fund", 1, 185, 1, 0, 0, 0, "随存随取，七日年化约1.85%"),
    ("DP002", "安享定期90天", "deposit", 1, 220, 1000, 90, 0, 0, "90天定存，到期还本付息"),
    ("BD003", "稳健纯债基金", "bond", 2, 320, 100, 0, 0, 15, "中低风险，历史年化约3.2%"),
    ("BD004", "双利增强债券", "bond", 2, 380, 1000, 0, 8, 20, "二级债基，波动略高"),
    ("MX005", "成长混合基金", "mixed", 3, 550, 100, 0, 15, 50, "股债混合，适合持有1年以上"),
    ("MX006", "科技创新股票基金", "mixed", 4, 850, 100, 0, 15, 50, "高波动，追求长期增值"),
    ("MX007", "全球配置混合(QDII)", "mixed", 4, 720, 500, 0, 20, 60, "海外分散配置"),
    ("GD008", "黄金积存", "gold", 3, 480, 1, 0, 30, 30, "挂钩金价，避险资产"),
    ("DP009", "大额存单365天", "deposit", 1, 250, 20000, 365, 0, 0, "一年期大额存单"),
    ("MX010", "新兴产业混合", "mixed", 5, 1050, 100, 0, 15, 50, "高预期高波动，仅适合C5客户"),
]

# 日常商户：(名称, 类别, 月频次范围, 单笔金额区间(元), 渠道)
DAILY_MERCHANTS = [
    ("美团外卖", "餐饮", (10, 16), (18, 46), "online"),
    ("饿了么", "餐饮", (4, 8), (20, 45), "online"),
    ("瑞幸咖啡", "餐饮", (8, 12), (9.9, 32), "pos"),
    ("盒马鲜生", "商超", (3, 5), (80, 300), "online"),
    ("山姆会员店", "商超", (1, 2), (200, 600), "pos"),
    ("京东商城", "网购", (2, 4), (60, 500), "online"),
    ("淘宝", "网购", (1, 3), (40, 350), "online"),
    ("滴滴出行", "交通", (6, 10), (12, 60), "online"),
    ("深圳通", "交通", (12, 20), (3, 12), "pos"),
    ("万达影城", "娱乐", (0, 2), (40, 90), "pos"),
    ("海底捞", "餐饮", (0, 2), (150, 420), "pos"),
    ("叮当快药", "医疗", (0, 1), (20, 120), "online"),
]

# 订阅族（固定扣款日，保证周期挖掘稳定命中）：(商户, 金额元, 扣款日)
SUBSCRIPTIONS = [
    ("腾讯视频VIP", "25", 8),
    ("网易云音乐", "18", 12),
    ("iCloud云存储", "6", 20),
    ("Keep会员", "19", 5),
]


def _month_starts(first: date, last: date) -> list[date]:
    out = []
    d = date(first.year, first.month, 1)
    while d <= last:
        out.append(d)
        d = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return out


def _months_ago(ref: date, n: int) -> tuple[int, int]:
    total = ref.year * 12 + ref.month - 1 - n
    return total // 12, total % 12 + 1


def seed(db_path: str | Path | None = None) -> dict:
    rng = random.Random(2026)  # 每次播种独立实例 -> 完全确定性
    reset_db(db_path)
    conn = init_db(db_path)

    # ------------------------------------------------------------- 基础档案
    conn.execute("INSERT INTO users (id,name,phone,created_at) VALUES (?,?,?,?)",
                 (USER["id"], USER["name"], USER["phone"], now_iso()))
    conn.execute(
        """INSERT INTO accounts (id,user_id,type,name,balance_cents,opened_at)
           VALUES (1,?, 'checking','生活主账户',?,?),
                  (2,?, 'savings','理财专户',?,?)""",
        (USER["id"], 0, now_iso(), USER["id"], 0, now_iso()))
    conn.executemany(
        "INSERT INTO cards (id,user_id,account_id,card_no_masked,card_type,status,"
        "daily_limit_cents,per_tx_limit_cents,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        [(1, 1, 1, "6222 **** **** 8821", "debit", "active", 500000, 200000, now_iso()),
         (2, 1, 1, "6222 **** **** 6673", "debit", "locked", 500000, 100000, now_iso()),
         (3, 1, None, "6225 **** **** 0231", "credit", "active", 1000000, 500000, now_iso())])
    for i, (name, phone, rel, note) in enumerate(CONTACTS, start=1):
        conn.execute(
            "INSERT INTO contacts (id,user_id,name,phone,relation,note) VALUES (?,?,?,?,?,?)",
            (i, 1, name, phone or f"1370000{i:04d}", rel, note))
    for (code, name, ptype, risk, bps, min_yuan, lock, sfee, rfee, intro) in PRODUCTS:
        conn.execute(
            """INSERT INTO wealth_products
               (code,name,p_type,risk_level,expected_return_bps,min_subscribe_cents,
                lock_days,subscription_fee_bps,redemption_fee_bps,intro)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (code, name, ptype, risk, bps, yuan_to_cents(str(min_yuan)),
             lock, sfee, rfee, intro))
    conn.execute(
        """INSERT INTO risk_profiles (user_id,answers_json,score,level,updated_at)
           VALUES (1,'{"稳健偏好":3,"投资经验":3,"亏损容忍":3,"投资期限":3,"收入稳定":3}',15,'C3',?)""",
        (now_iso(),))
    conn.commit()

    # ------------------------------------------------------------- 流水
    # 先整月收集、按时间排序、再顺序落库：balance_after 与账单时间序严格一致
    balances = {1: yuan_to_cents("45000"), 2: 0}

    def tx(acct: int, day: date, hour: int, minute: int, direction: str,
           amount_cents: int, tx_type: str, counterparty: str, category: str,
           channel: str = "app", memo: str = "", tail: str = "") -> None:
        amt = amount_cents if direction == "in" else -amount_cents
        balances[acct] += amt
        ts = f"{day.isoformat()}T{hour:02d}:{minute:02d}:00"
        conn.execute(
            """INSERT INTO transactions
               (account_id,ts,direction,amount_cents,balance_after_cents,tx_type,
                counterparty,counterparty_tail,category,channel,memo)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (acct, ts, direction, amount_cents, balances[acct], tx_type,
             counterparty, tail, category, channel, memo))

    first_day = TODAY - timedelta(days=365)
    for m0 in _month_starts(first_day, TODAY):
        y, m = m0.year, m0.month
        ndays = (date(y + (m == 12), m % 12 + 1, 1) - m0).days

        def d(day: int) -> date | None:
            dd = date(y, m, day)
            return dd if day <= ndays and dd <= TODAY else None

        ev: list[tuple] = []  # (day, hour, minute, acct, direction, amount, type, cp, cat, ch, memo, tail)

        # 固定月度
        if (dd := d(1)):
            ev.append((1, 9, 5, 1, "out", yuan_to_cents("6800"), "transfer_out",
                       "张阿姨", "房租", "app", "每月房租", "3456"))
        if (dd := d(3)):
            ev.append((3, 10, 12, 1, "out", yuan_to_cents("128"), "pos",
                       "中国移动", "通讯", "online", "话费套餐", ""))
        if (dd := d(5)):
            ev.append((5, 15, 30, 1, "out", rng.randint(15000, 35000), "online",
                       "深圳燃气&供电", "公共事业", "online", "水电燃气", ""))
        if (dd := d(10)):
            ev.append((10, 9, 0, 1, "in", yuan_to_cents("18500"), "salary",
                       "深圳市云启科技有限公司", "工资", "app", "月度工资", ""))
        if (dd := d(11)):  # 自动理财：活期 -> 理财专户 -> 货基
            ev.append((11, 8, 30, 1, "out", yuan_to_cents("2000"), "transfer_out",
                       "理财专户", "转账", "app", "自动理财转入", ""))
            ev.append((11, 8, 30, 2, "in", yuan_to_cents("2000"), "transfer_in",
                       "生活主账户", "转账", "app", "自动理财转入", ""))
            ev.append((11, 8, 31, 2, "out", yuan_to_cents("2000"), "subscribe",
                       "余额+货币基金", "理财", "app", "余额+自动申购", ""))
        if (dd := d(15)):
            ev.append((15, 7, 0, 1, "out", rng.randint(60000, 120000), "repayment",
                       "信用卡还款(0231)", "还款", "app", "信用卡自动还款", ""))

        # 订阅族（固定扣款日；腾讯视频近 3 个月涨价 25->30）
        hike = (y, m) >= _months_ago(TODAY, 3)
        for name, amt, day in SUBSCRIPTIONS:
            if d(day):
                price = "30" if (name == "腾讯视频VIP" and hike) else amt
                ev.append((day, 8, 0, 1, "out", yuan_to_cents(price), "online",
                           name, "订阅", "online", "会员自动续费", ""))

        # 日常随机消费
        for (name, cat, (lo, hi), (alo, ahi), ch) in DAILY_MERCHANTS:
            for _ in range(rng.randint(lo, hi)):
                day = rng.randint(1, ndays)
                if not d(day):
                    continue
                amount = round(rng.uniform(alo, ahi), 1)
                ev.append((day, rng.randint(8, 22), rng.randint(0, 59), 1, "out",
                           yuan_to_cents(str(amount)),
                           "pos" if ch == "pos" else "online",
                           name, cat, ch, "", ""))

        for e in sorted(ev, key=lambda x: (x[0], x[1], x[2], x[7])):
            day_d = date(y, m, e[0])
            tx(e[3], day_d, e[1], e[2], e[4], e[5], e[6], e[7], e[8], e[9], e[10], e[11])

    # ------------------------------------------------------------- 戏眼
    w3 = TODAY - timedelta(days=21)
    for i in range(3):  # R1 重复扣款（3 天内同商户同金额 3 笔）
        tx(1, w3 + timedelta(days=i), 14, 30 + i, "out", yuan_to_cents("15"),
           "online", "迅雷白金会员", "订阅", "online", "会员续费")
    w5 = TODAY - timedelta(days=35)  # R3 凌晨大额
    tx(1, w5, 3, 47, "out", yuan_to_cents("4999"), "online",
       "京东商城", "网购", "online", "笔记本电脑内存条x2")

    # 插入顺序 ≠ 账单时间序（戏眼日期回插），按时间序统一重算余额快照
    for aid, opening in ((1, yuan_to_cents("45000")), (2, 0)):
        prev = opening
        for r in conn.execute(
                "SELECT id, direction, amount_cents FROM transactions "
                "WHERE account_id=? ORDER BY ts, id", (aid,)).fetchall():
            prev += r["amount_cents"] if r["direction"] == "in" else -r["amount_cents"]
            conn.execute("UPDATE transactions SET balance_after_cents=? WHERE id=?",
                         (prev, r["id"]))
        balances[aid] = prev
    conn.execute("UPDATE accounts SET balance_cents=? WHERE id=1", (balances[1],))
    conn.execute("UPDATE accounts SET balance_cents=? WHERE id=2", (balances[2],))
    conn.commit()

    # ------------------------------------------------------------- 持仓/事件/到期演示
    months = len(_month_starts(first_day, TODAY))
    principal = yuan_to_cents("2000") * months
    conn.execute(
        """INSERT INTO wealth_holdings
           (user_id,product_id,principal_cents,est_value_cents,status,subscribed_at)
           VALUES (1,1,?,?, 'holding', ?)""",
        (principal, int(principal * 1.021), now_iso()))
    conn.execute(
        """INSERT INTO wealth_holdings
           (user_id,product_id,principal_cents,est_value_cents,status,subscribed_at)
           VALUES (1,3,?,?, 'holding', ?)""",
        (yuan_to_cents("10000"), yuan_to_cents("10180"), now_iso()))

    events = [
        # 林悦生日：近期能演示(播种日+4 天,2026-09 播种即 2026-09-26),
        # 生日前 2 天订购鲜花/蛋糕的联动提醒正好落在演示窗口内
        ("birthday", "林悦的生日", (TODAY + timedelta(days=4)).strftime("%Y-%m-%d"),
         1, "老婆，记得提前准备礼物"),
        ("anniversary", "结婚纪念日", "2026-05-20", 1, ""),
        ("payday", "发薪日", "2026-09-10", 1, "每月10号"),
        ("bill_day", "房租缴纳日", "2026-09-01", 1, "每月1号"),
    ]
    for (t, title, dt, rep, note) in events:
        conn.execute(
            """INSERT INTO user_events (user_id,event_type,title,event_date,
               repeat_yearly,note) VALUES (1,?,?,?,?,?)""", (t, title, dt, rep, note))

    # 到期演示：3 天后执行的定时转账（scheduled 状态，到期转待确认）
    ledger = LedgerService(conn, 1)
    run_at = (datetime.now() + timedelta(days=3)).replace(
        microsecond=0, second=0).isoformat()
    order = ledger.create_transfer_order(
        from_account_id=1, amount_cents=yuan_to_cents("500"),
        to_contact_id=1, memo="下周家用", scheduled_at=run_at)
    conn.execute(
        """INSERT INTO scheduled_tasks
           (user_id, task_type, payload_json, run_at, created_at)
           VALUES (1, 'scheduled_transfer', ?, ?, ?)""",
        (json.dumps({"order_id": order["id"], "title": "定时转账：林悦 家用 500 元"},
                    ensure_ascii=False), run_at, now_iso()))
    conn.commit()

    # 订阅挖掘结果入库（幂等 upsert）
    AnalysisService(conn, 1).detect_subscriptions()
    conn.commit()

    summary = {
        "transactions": conn.execute("SELECT COUNT(*) c FROM transactions").fetchone()["c"],
        "balance_main_yuan": f"{balances[1]/100:.2f}",
        "balance_wealth_yuan": f"{balances[2]/100:.2f}",
        "subscriptions_detected": conn.execute(
            "SELECT COUNT(*) c FROM subscriptions").fetchone()["c"],
        "db": str(db_path or "默认 data/bank.db"),
    }
    conn.close()
    return summary


if __name__ == "__main__":
    target = sys.argv[sys.argv.index("--db") + 1] if "--db" in sys.argv else None
    print(seed(target))
