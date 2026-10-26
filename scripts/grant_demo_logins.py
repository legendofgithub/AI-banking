"""给还没有登录能力的档案用户幂等补齐登录凭证(演示库专用)。

背景:users 表里的人物分两种——注册来的(自带 auth_users 行)和种子/演示
造出来的纯档案(只能当收款人,登不进系统)。本脚本把后者统一补上:

- 登录账号 = users.phone(1 开头 11 位,与注册链路同规则);
- 登录密码 = Demo@12345(与演示主账号陈明同一套,答辩好记好念);
- 支付密码 = 888888(同演示口径,受 4 次错误锁定保护)。

幂等:已有 auth 行的用户一律跳过,重复运行零副作用。注意本脚本只给**库里
现有**的用户补凭证——若用 bank_core.seed 重播种,users 2-8 这类手工注册的
人物本身不在种子里,需先重建人物再跑本脚本。

--reset:把登录密码**不是** Demo@12345 的老账号也统一成演示口令(只改登录
密码,不动支付密码)。注意不可逆——老密码明文无处可查,重置前确认无人再用。

用法:python scripts/grant_demo_logins.py [--db 路径] [--reset]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bank_core.auth_core import verify_password  # noqa: E402
from bank_core.db import connect  # noqa: E402
from agent.auth import ensure_demo_auth, hash_password  # noqa: E402

DEMO_LOGIN_PASSWORD = "Demo@12345"
DEMO_PAY_PASSWORD = "888888"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None, help="bank.db 路径(默认 data/bank.db)")
    ap.add_argument("--reset", action="store_true",
                    help="把非演示口令的老账号登录密码统一重置为 Demo@12345")
    args = ap.parse_args()

    conn = connect(args.db)
    created = 0
    for u in conn.execute(
            "SELECT u.id, u.name, u.phone FROM users u WHERE u.phone != '' "
            "AND NOT EXISTS (SELECT 1 FROM auth_users a WHERE a.user_id=u.id)"):
        if ensure_demo_auth(conn, u["id"], login_password=DEMO_LOGIN_PASSWORD,
                            pay_password=DEMO_PAY_PASSWORD):
            created += 1
            print(f"[新建] {u['id']} {u['name']} {u['phone']}")
    print(f"本次新建登录 {created} 个;其余已有凭证")

    if args.reset:
        rows = conn.execute(
            "SELECT user_id, identifier, password_hash FROM auth_users").fetchall()
        for r in rows:
            if verify_password(DEMO_LOGIN_PASSWORD, r["password_hash"]):
                continue
            conn.execute("UPDATE auth_users SET password_hash=? WHERE user_id=?",
                         (hash_password(DEMO_LOGIN_PASSWORD), r["user_id"]))
            print(f"[重置] {r['user_id']} {r['identifier']} 登录密码 → {DEMO_LOGIN_PASSWORD}")
        conn.commit()

    print("\n== 全部可登录账号实测核验 ==")
    print(f"{'ID':<5}{'姓名':<10}{'登录账号':<16}{'登录密码'}")
    for r in conn.execute(
            """SELECT a.user_id, u.name, a.identifier, a.password_hash
               FROM auth_users a JOIN users u ON u.id=a.user_id
               ORDER BY a.user_id"""):
        pwd = (DEMO_LOGIN_PASSWORD
               if verify_password(DEMO_LOGIN_PASSWORD, r["password_hash"])
               else "(非演示口令,--reset 可统一)")
        print(f"{r['user_id']:<5}{r['name']:<10}{r['identifier']:<16}{pwd}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
