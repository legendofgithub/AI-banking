"""给 faker 演示账户生成一套完整的银行数据（可重复运行）。

生成内容：
- 100 笔转账流水（60 出 / 40 入），按时间排序、balance_after 连续；
  出账同时写 transfer_orders（status=executed），入账写 transfer_in 流水；
- 12 个按月自动扣费订阅（active），下次扣费日分布在未来 1-28 天；
- 20 笔理财持仓（现有 9 个 C1-C4 产品，不同本金与盈亏）；
- faker 的风险测评（C4）。

只操作 --user 指定的用户；重复运行时先清空该用户的交易/订单/订阅/持仓/风险测评，
不影响其他用户。默认库 data/bank.db。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bank_core.db import init_db  # noqa: E402

OUT_NAMES = [
    "王五", "李四", "张三", "赵六", "陈晨", "刘洋", "周杰", "吴敏",
    "郑爽", "孙悦", "朱琳", "胡军", "高翔", "林峰", "何静", "郭涛",
    "马超", "罗敏", "梁静", "宋佳", "谢娜", "韩雪", "唐嫣", "冯绍峰",
    "邓超", "黄晓明", "杨幂", "刘亦菲", "彭于晏", "胡歌",
]
IN_NAMES = [
    "深圳市云启科技有限公司", "支付宝转账", "微信转账", "王五", "李四",
    "张三", "赵六", "陈晨", "刘洋", "周杰", "吴敏", "郑爽", "孙悦", "朱琳",
]
OUT_MEMOS = [
    "房租", "还款", "聚餐AA", "借款", "家用", "货款", "装修尾款", "旅游分摊",
    "孩子学费", "物业费", "水电费", "人情往来", "聚会", "车贷", "保险费",
    "采购", "报销", "定金", "尾款", "转账",
]
IN_MEMOS = [
    "还款", "退款", "AA收款", "借款归还", "项目分成", "报销到账",
    "房租分摊", "货款回收", "兼职收入", "红包", "转账", "结算款",
    "押金退回", "代收",
]

# (商户, 分类, 每月金额元, 周期天, 距今天数)
SUBSCRIPTIONS = [
    ("腾讯视频VIP", "视频会员", 30, 30, 3),
    ("爱奇艺黄金VIP", "视频会员", 25, 30, 6),
    ("优酷酷喵会员", "视频会员", 29, 31, 9),
    ("网易云音乐黑胶", "音乐会员", 18, 30, 12),
    ("QQ音乐绿钻", "音乐会员", 15, 31, 15),
    ("iCloud云存储", "云存储", 6, 30, 2),
    ("Keep会员", "健康", 19, 30, 18),
    ("美团外卖神券", "外卖", 15, 30, 21),
    ("京东PLUS会员", "购物", 12, 31, 24),
    ("山姆会员店", "购物", 26, 30, 5),
    ("盒马X会员", "购物", 25, 31, 27),
    ("得到听书会员", "阅读", 29, 30, 11),
]

RISK_ANSWERS = {
    "稳健偏好": 4,
    "投资经验": 4,
    "亏损容忍": 4,
    "投资期限": 4,
    "收入稳定": 4,
}

PNL_RANGES = {
    1: (0.002, 0.020),    # 货基/存款
    2: (-0.020, 0.040),   # 债基
    3: (-0.080, 0.100),   # 混合/黄金
    4: (-0.150, 0.180),   # 高波动混合
}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _yuan(cents: int) -> str:
    return f"{cents / 100:.2f}"


def generate(conn: sqlite3.Connection, user_name: str, seed: int) -> dict:
    conn.row_factory = sqlite3.Row
    rng = random.Random(seed)
    user = conn.execute(
        "SELECT * FROM users WHERE name=?", (user_name,)).fetchone()
    if user is None:
        raise SystemExit(f"找不到用户 {user_name!r}")
    user_id = int(user["id"])
    acct = conn.execute(
        "SELECT * FROM accounts WHERE user_id=? AND type='checking' "
        "ORDER BY id LIMIT 1", (user_id,)).fetchone()
    if acct is None:
        raise SystemExit(f"用户 {user_name!r} 没有活期账户")
    account_id = int(acct["id"])

    conn.execute("BEGIN IMMEDIATE")
    # 只清 faker 自己的数据；先清引用方，再清流水。
    conn.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM wealth_holdings WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM risk_profiles WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM transfer_orders WHERE user_id=?", (user_id,))
    conn.execute("DELETE FROM transactions WHERE account_id=?", (account_id,))

    today = date.today()
    now = _iso(datetime.now())
    specs: list[dict] = []
    directions = ["out"] * 60 + ["in"] * 40
    rng.shuffle(directions)
    used_ts: set[str] = set()

    for i, direction in enumerate(directions):
        while True:
            day = today - timedelta(days=rng.randint(1, 175))
            dt = datetime.combine(
                day, time(rng.randint(8, 21), rng.randint(0, 59),
                          rng.randint(0, 59)))
            ts = _iso(dt)
            if ts not in used_ts:
                used_ts.add(ts)
                break
        if direction == "out":
            amount = rng.choice([
                200, 300, 500, 800, 1000, 1200, 1500, 2000,
                3000, 5000, 8000, 12000, 20000,
            ]) * 100
            name = rng.choice(OUT_NAMES)
            memo = rng.choice(OUT_MEMOS)
        else:
            amount = rng.choice([
                500, 800, 1000, 1500, 2000, 3000, 5000,
                8000, 10000, 15000, 30000,
            ]) * 100
            name = rng.choice(IN_NAMES)
            memo = rng.choice(IN_MEMOS)
        specs.append({
            "direction": direction, "amount": amount, "name": name,
            "memo": memo, "ts": ts, "tail": f"{rng.randint(0, 9999):04d}",
        })

    specs.sort(key=lambda s: s["ts"])
    total_in = sum(s["amount"] for s in specs if s["direction"] == "in")
    total_out = sum(s["amount"] for s in specs if s["direction"] == "out")
    target_final = 11_499_900  # 保持 faker 现有余额 ¥114,999.00
    opening = target_final - (total_in - total_out)
    if opening < 1_000_000:  # 至少 ¥10,000 期初，避免任何时点余额为负
        opening = 1_000_000
    final = opening + total_in - total_out

    balance = opening
    orders = 0
    for idx, s in enumerate(specs):
        if s["direction"] == "out":
            cur = conn.execute(
                """INSERT INTO transfer_orders
                   (user_id,from_account_id,to_contact_id,to_name,
                    to_account_tail,amount_cents,memo,status,fail_reason,
                    created_at,scheduled_at,executed_at,idempotency_key)
                   VALUES (?,?,NULL,?,?,?,?, 'executed','',?,NULL,?,?)""",
                (user_id, account_id, s["name"], s["tail"],
                 s["amount"], s["memo"], s["ts"], s["ts"],
                 f"faker-demo:{idx}"))
            ext = f"order:{cur.lastrowid}"
            orders += 1
            balance -= s["amount"]
        else:
            ext = f"faker-demo:in:{idx}"
            balance += s["amount"]
        if balance < 0:  # 理论上不会发生；防御性截断
            raise SystemExit("余额计算出负值，请调整金额范围")
        conn.execute(
            """INSERT INTO transactions
               (account_id,ts,direction,amount_cents,balance_after_cents,
                tx_type,counterparty,counterparty_tail,category,channel,
                memo,card_id,external_ref)
               VALUES (?,?,?,?,?,?,?,?, '转账','app',?,NULL,?)""",
            (account_id, s["ts"], s["direction"], s["amount"], balance,
             "transfer_out" if s["direction"] == "out" else "transfer_in",
             s["name"], s["tail"], s["memo"], ext))

    # 账户生命周期与流水时间对齐
    conn.execute(
        "UPDATE accounts SET balance_cents=?, opened_at=? WHERE id=?",
        (final, _iso(datetime.combine(today - timedelta(days=180),
                                     time(9, 0))), account_id))
    conn.execute(
        "UPDATE users SET created_at=? WHERE id=?",
        (_iso(datetime.combine(today - timedelta(days=365),
                               time(9, 0))), user_id))

    monthly_total = 0
    for merchant, category, amount_yuan, period, offset in SUBSCRIPTIONS:
        amount = amount_yuan * 100
        monthly_total += amount
        conn.execute(
            """INSERT INTO subscriptions
               (user_id,merchant_name,category,amount_cents,period_days,
                next_charge_date,precheck_ack_date,status,last_tx_id,detected_at)
               VALUES (?,?,?,?,?,?, '', 'active', NULL, ?)""",
            (user_id, merchant, category, amount, period,
             (today + timedelta(days=offset)).isoformat(), now))

    conn.execute(
        """INSERT INTO risk_profiles
           (user_id,answers_json,score,level,updated_at)
           VALUES (?,?,?,?,?)""",
        (user_id, json.dumps(RISK_ANSWERS, ensure_ascii=False), 20, "C4", now))

    products = conn.execute(
        "SELECT * FROM wealth_products WHERE risk_level<=4 ORDER BY id"
    ).fetchall()
    holdings = []
    for i in range(20):
        p = products[i % len(products)]
        principal = rng.randint(5_000, 50_000) * 100
        lo, hi = PNL_RANGES[int(p["risk_level"])]
        pnl = rng.uniform(lo, hi)
        est = max(1, round(principal * (1 + pnl)))
        sub_dt = datetime.now() - timedelta(
            days=rng.randint(5, 330), hours=rng.randint(0, 12))
        conn.execute(
            """INSERT INTO wealth_holdings
               (user_id,product_id,principal_cents,est_value_cents,
                status,subscribed_at,redeemed_at)
               VALUES (?,?,?,?, 'holding', ?, NULL)""",
            (user_id, int(p["id"]), principal, est, _iso(sub_dt)))
        holdings.append((p["name"], principal, est))

    conn.commit()
    total_principal = sum(h[1] for h in holdings)
    total_value = sum(h[2] for h in holdings)
    return {
        "user_id": user_id, "account_id": account_id,
        "transactions": len(specs), "orders": orders,
        "total_in": total_in, "total_out": total_out,
        "opening": opening, "final": final,
        "subscriptions": len(SUBSCRIPTIONS), "monthly_total": monthly_total,
        "holdings": len(holdings),
        "principal": total_principal, "value": total_value,
        "pnl": total_value - total_principal,
        "worst": min(holdings, key=lambda h: h[2] - h[1]),
        "best": max(holdings, key=lambda h: h[2] - h[1]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 faker 演示账户数据")
    parser.add_argument("--db", default=str(ROOT / "data" / "bank.db"))
    parser.add_argument("--user", default="faker")
    parser.add_argument("--seed", type=int, default=20261021)
    args = parser.parse_args()

    conn = init_db(args.db)
    try:
        stats = generate(conn, args.user, args.seed)
    finally:
        conn.close()

    print(f"faker: user={stats['user_id']} account={stats['account_id']}")
    print(f"转账流水: {stats['transactions']} 笔 "
          f"(出 {stats['total_out'] / 100:.2f} / 入 {stats['total_in'] / 100:.2f})")
    print(f"转账订单: {stats['orders']} 笔 executed")
    print(f"期初 -> 期末: {stats['opening'] / 100:.2f} -> "
          f"{stats['final'] / 100:.2f} 元")
    print(f"订阅: {stats['subscriptions']} 个 active, "
          f"每月合计 {stats['monthly_total'] / 100:.2f} 元")
    print(f"理财: {stats['holdings']} 笔, 本金 "
          f"{stats['principal'] / 100:.2f} 元, 估值 "
          f"{stats['value'] / 100:.2f} 元, 盈亏 "
          f"{stats['pnl'] / 100:+.2f} 元")
    print(f"最赚: {stats['best'][0]} {stats['best'][1] / 100:.2f} -> "
          f"{stats['best'][2] / 100:.2f}")
    print(f"最亏: {stats['worst'][0]} {stats['worst'][1] / 100:.2f} -> "
          f"{stats['worst'][2] / 100:.2f}")


if __name__ == "__main__":
    main()
