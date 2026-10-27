"""bank_core/insights.py 单测：M1 兴趣度 / M2 准入 / M3 候选集。

覆盖 Phase 1/2 验收用例：
- 全零信号冷启动（均匀分布 + confidence=low + 兜底链路）
- 单信号主导、时间衰减边界（含 τ 整数天处 e^-1）
- 概率和为 1（by_product 与 by_type 两套口径）
- 准入三类拒绝理由（年龄/风险/起购）与 id_card 空串老用户兜底
- 全持有时候选为空、新用户候选受风险上限约束

时间全部钉死 NOW，兴趣度公式在测试里独立重算，防"自己测自己"。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime

import pytest

from bank_core.db import init_db
from bank_core.insights import (
    ALPHA,
    BETA,
    PRODUCT_ELIGIBILITY,
    PRODUCT_TYPES,
    TAU_DAYS,
    _age_from_id_card,
    check_eligibility,
    compute_interest_scores,
    recommend_candidates,
)
from bank_core.money import yuan_to_cents
from bank_core.seed import PRODUCTS

NOW = datetime(2026, 10, 27, 12, 0, 0)
# 产品 id 与 seed.PRODUCTS 顺序一致（测试库显式指定 id，禁用自增漂移）
CODE_BY_ID = {i: p[0] for i, p in enumerate(PRODUCTS, start=1)}
ID_BY_CODE = {c: i for i, c in CODE_BY_ID.items()}


# ----------------------------------------------------------------- 夹具
@pytest.fixture()
def db(tmp_path):
    """已建表 + 已铺 10 只产品的库；用户/账户/信号由各用例按需补齐。"""
    conn = init_db(tmp_path / "ins.db")
    for i, (code, name, ptype, risk, bps, min_yuan, *_rest) in \
            enumerate(PRODUCTS, start=1):
        # _rest = (lock_days, subscription_fee_bps, redemption_fee_bps, intro)
        conn.execute(
            """INSERT INTO wealth_products
               (id, code, name, p_type, risk_level, expected_return_bps,
                min_subscribe_cents, lock_days, subscription_fee_bps,
                redemption_fee_bps, intro)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (i, code, name, ptype, risk, bps, yuan_to_cents(str(min_yuan)),
             *_rest))
    conn.commit()
    yield conn
    conn.close()


def _mk_user(conn, uid=1, id_card=""):
    conn.execute(
        "INSERT INTO users (id, name, phone, id_card, created_at) "
        "VALUES (?,?,?,?, '2026-01-01T00:00:00')",
        (uid, f"用户{uid}", f"1380000{uid:04d}", id_card))
    conn.commit()


def _mk_account(conn, uid, balance_cents):
    cur = conn.execute(
        "INSERT INTO accounts (user_id, type, name, balance_cents, opened_at) "
        "VALUES (?, 'checking', '测试户', ?, '2026-01-01T00:00:00')",
        (uid, balance_cents))
    conn.commit()
    return cur.lastrowid


def _set_risk(conn, uid, level, score=15):
    conn.execute(
        "INSERT INTO risk_profiles (user_id, answers_json, score, level,"
        " updated_at) VALUES (?, '{}', ?, ?, '2026-10-01T00:00:00')",
        (uid, score, level))
    conn.commit()


def _audit(conn, tool, args, ts):
    conn.execute(
        "INSERT INTO audit_log (ts, tool, args_json, result_json, risk) "
        "VALUES (?, ?, ?, '{}', 'READ')",
        (ts, tool, json.dumps(args, ensure_ascii=False)))
    conn.commit()


def _tx(conn, account_id, ts, tx_type, counterparty, amount_cents=10000):
    conn.execute(
        "INSERT INTO transactions (account_id, ts, direction, amount_cents,"
        " balance_after_cents, tx_type, counterparty) VALUES (?,?, 'out', ?, 0, ?, ?)",
        (account_id, ts, amount_cents, tx_type, counterparty))
    conn.commit()


def _holding(conn, uid, product_id, ts="2026-10-01T10:00:00"):
    conn.execute(
        "INSERT INTO wealth_holdings (user_id, product_id, principal_cents,"
        " est_value_cents, status, subscribed_at) "
        "VALUES (?,?, 10000, 10000, 'holding', ?)", (uid, product_id, ts))
    conn.commit()


def _write_memory(uid, module, content, updated_at):
    """往 conftest 隔离出来的 AGENT_MEMORY_DB 写一条声明偏好（表结构与
    agent/memory.py:_CREATE_SQL 一致）。"""
    path = os.environ["AGENT_MEMORY_DB"]
    m = sqlite3.connect(path)
    m.execute(
        "CREATE TABLE IF NOT EXISTS agent_memory ("
        "user_id INTEGER NOT NULL, module TEXT NOT NULL,"
        " content_json TEXT NOT NULL, updated_at TEXT NOT NULL,"
        " PRIMARY KEY (user_id, module))")
    m.execute("INSERT INTO agent_memory VALUES (?,?,?,?)",
              (uid, module, json.dumps(content, ensure_ascii=False), updated_at))
    m.commit()
    m.close()


def _expected_softmax(weights: dict[str, float]) -> dict[str, float]:
    """测试侧独立重算 softmax，与实现解耦。"""
    exps = {k: math.exp(v) for k, v in weights.items()}
    total = sum(exps.values())
    return {k: v / total for k, v in exps.items()}


# ------------------------------------------------------------ M1 兴趣度
def test_cold_start_all_zero_signals_is_uniform(db):
    """全零信号：softmax(全 0) 退化为均匀分布，标 low + 无兜底。"""
    _mk_user(db)
    scores = compute_interest_scores(db, 1, now=NOW)

    assert scores["cold_start"] is True
    assert scores["confidence"] == "low"
    assert scores["fallback"] == []
    assert scores["signals"] == {}
    assert len(scores["by_product"]) == len(PRODUCTS)
    for prob in scores["by_product"].values():
        assert prob == pytest.approx(1 / len(PRODUCTS), abs=1e-9)
    # 类型聚合 = 各类型产品数 × 均匀概率（seed: 债2/混合4/货基1/存2/金1）
    assert scores["by_type"]["bond"] == pytest.approx(2 / 10, abs=1e-6)
    assert scores["by_type"]["mixed"] == pytest.approx(4 / 10, abs=1e-6)
    assert scores["by_type"]["money_fund"] == pytest.approx(1 / 10, abs=1e-6)


def test_empty_product_library_is_documented_degenerate(db):
    """空产品库（未播种裸库）：显式退化输出，不是均匀分布。

    docstring 已明确：此时 by_product={}、by_type 五档全 0，概率和不成立。
    """
    _mk_user(db)
    db.execute("DELETE FROM wealth_products")
    db.commit()
    scores = compute_interest_scores(db, 1, now=NOW)

    assert scores["by_product"] == {}
    assert scores["by_type"] == {t: 0.0 for t in PRODUCT_TYPES}
    assert sum(scores["by_type"].values()) == 0.0
    assert scores["signals"] == {}
    assert scores["cold_start"] is True
    assert scores["confidence"] == "low"


def test_cold_start_memory_prior(db):
    """冷启动兜底第一档：agent_memory 声明偏好（gold）→ 黄金产品概率最高。"""
    _mk_user(db)
    _write_memory(1, "profile", {"preferred_type": "gold"},
                  "2026-10-26T12:00:00")  # 距 NOW 整 1 天，recency=e^-1
    scores = compute_interest_scores(db, 1, now=NOW)

    assert scores["cold_start"] is True
    assert scores["fallback"] == ["agent_memory:preferred_type=gold"]
    # 只有 GD008（gold）带伪信号：w = 0.6×1 + 0.4×e^-1
    w = ALPHA * 1.0 + BETA * math.exp(-1 / TAU_DAYS)
    expected = _expected_softmax(
        {"GD008": w, **{c: 0.0 for c in CODE_BY_ID.values() if c != "GD008"}})
    assert scores["by_product"]["GD008"] == pytest.approx(
        expected["GD008"], abs=1e-6)
    assert scores["by_type"]["gold"] == pytest.approx(
        expected["GD008"], abs=1e-6)
    assert scores["signals"]["GD008"]["sources"] == [
        {"source": "memory:preferred_type", "count": 1}]
    assert scores["signals"]["GD008"]["last_seen"] == "2026-10-26T12:00:00"


def test_cold_start_holdings_prior(db):
    """冷启动兜底第二档：无记忆但有持仓 → 持仓产品成为先验。"""
    _mk_user(db)
    _holding(db, 1, ID_BY_CODE["BD003"], ts="2026-10-25T12:00:00")
    scores = compute_interest_scores(db, 1, now=NOW)

    assert scores["cold_start"] is True
    assert scores["fallback"] == ["wealth_holdings:当前持仓"]
    assert scores["by_product"]["BD003"] == max(scores["by_product"].values())
    assert scores["signals"]["BD003"]["sources"] == [
        {"source": "holding", "count": 1}]


def test_cold_start_memory_prior_product_name(db):
    """真实写入路径：graph.py 存的是最大持仓**产品名**（top_holding），
    冷启动兜底必须能按产品名命中，而不是只认 p_type。"""
    _mk_user(db)
    _write_memory(1, "profile", {"preferred_type": "稳健纯债基金"},
                  "2026-10-26T12:00:00")  # 距 NOW 整 1 天，recency=e^-1
    scores = compute_interest_scores(db, 1, now=NOW)

    assert scores["cold_start"] is True
    assert scores["fallback"] == ["agent_memory:preferred_type=稳健纯债基金"]
    assert scores["by_product"]["BD003"] == max(scores["by_product"].values())
    assert scores["signals"]["BD003"]["sources"] == [
        {"source": "memory:preferred_type", "count": 1}]
    # 产品名是精确匹配：同类型另一只债券基金不应被连带加权
    assert "BD004" not in scores["signals"]


def test_cold_start_memory_prior_skips_unmatched_profile_value(db):
    """profile 里是脏偏好时继续试 wealth 的有效产品名，不能一条脏值全丢。"""
    _mk_user(db)
    _write_memory(1, "profile", {"preferred_type": "不存在的产品"},
                  "2026-10-26T12:00:00")
    _write_memory(1, "wealth", {"preferred_type": "稳健纯债基金"},
                  "2026-10-20T12:00:00")
    scores = compute_interest_scores(db, 1, now=NOW)

    assert scores["fallback"] == ["agent_memory:preferred_type=稳健纯债基金"]
    assert scores["signals"]["BD003"]["last_seen"] == "2026-10-20T12:00:00"


def test_cold_start_memory_prior_timestamp_from_own_module(db):
    """记忆先验的时间戳取其来源模块：profile 声明优先且用 profile 的
    updated_at（不能拿 wealth 模块的时间算衰减基准）。"""
    _mk_user(db)
    _write_memory(1, "profile", {"preferred_type": "gold"},
                  "2026-10-26T12:00:00")
    _write_memory(1, "wealth", {"preferred_type": "bond"},
                  "2026-10-01T12:00:00")
    scores = compute_interest_scores(db, 1, now=NOW)

    assert scores["fallback"] == ["agent_memory:preferred_type=gold"]
    assert scores["signals"]["GD008"]["last_seen"] == "2026-10-26T12:00:00"
    assert "BD003" not in scores["signals"]


def test_cold_start_memory_prior_without_products_falls_back(db):
    """偏好类型在本库没有产品：不输出"伪命中"，继续走持仓兜底。"""
    _mk_user(db)
    db.execute("DELETE FROM wealth_products WHERE p_type = 'gold'")
    db.commit()
    _write_memory(1, "profile", {"preferred_type": "gold"},
                  "2026-10-26T12:00:00")
    _holding(db, 1, ID_BY_CODE["BD003"], ts="2026-10-25T12:00:00")
    scores = compute_interest_scores(db, 1, now=NOW)

    assert scores["fallback"] == ["wealth_holdings:当前持仓"]
    assert "GD008" not in scores["by_product"]
    assert scores["signals"]["BD003"]["sources"] == [
        {"source": "holding", "count": 1}]
    assert sum(scores["by_product"].values()) == pytest.approx(1.0, abs=1e-5)


def test_single_signal_dominates(db):
    """单信号：一次产品比较（audit）→ 该产品概率独占鳌头且公式吻合。"""
    _mk_user(db)
    _audit(db, "compare_products", {"ids": [ID_BY_CODE["MF001"]]},
           NOW.isoformat())
    scores = compute_interest_scores(db, 1, now=NOW)

    assert scores["cold_start"] is False
    assert scores["confidence"] == "medium"  # 1 件行为证据 < 5
    w = ALPHA * 1.0 + BETA * 1.0  # 唯一信号：freq 归一=1，距今天数=0 → recency=1
    expected = _expected_softmax(
        {"MF001": w, **{c: 0.0 for c in CODE_BY_ID.values() if c != "MF001"}})
    assert scores["by_product"]["MF001"] == pytest.approx(
        expected["MF001"], abs=1e-6)
    assert scores["by_product"]["MF001"] == max(scores["by_product"].values())
    assert scores["signals"]["MF001"]["freq"] == 1.0
    assert scores["signals"]["MF001"]["count"] == 1
    assert scores["signals"]["MF001"]["last_seen"] == NOW.isoformat()


def test_bare_list_products_is_not_a_signal(db):
    """无过滤条件的全量列表不指向任何产品：不算信号，仍按冷启动处理
    （否则一次翻列表就会把 confidence 抬到 high，分布却还是均匀的）。"""
    _mk_user(db)
    _audit(db, "list_products",
           {"p_type": None, "max_risk": None, "keyword": None},
           NOW.isoformat())
    scores = compute_interest_scores(db, 1, now=NOW)

    assert scores["cold_start"] is True
    assert scores["signals"] == {}
    assert scores["confidence"] == "low"


def test_list_products_filters_are_anded(db):
    """list_products 的 p_type/keyword 是 AND（复刻 wealth.py 的 SQL），
    且同一行两个条件命中同一产品只算一次。"""
    _mk_user(db)
    _audit(db, "list_products",
           {"p_type": "bond", "max_risk": None, "keyword": "双利"},
           NOW.isoformat())
    scores = compute_interest_scores(db, 1, now=NOW)

    # 只有 BD004「双利增强债券」同时满足 bond + 双利
    assert set(scores["signals"]) == {"BD004"}
    assert scores["signals"]["BD004"]["freq"] == 0.5
    assert scores["cold_start"] is False

    # "纯债" 同时被 p_type=bond 与 keyword 命中 → 仍只记 0.5（不是 1.0）
    _audit(db, "list_products",
           {"p_type": "bond", "max_risk": None, "keyword": "纯债"},
           NOW.isoformat())
    s = compute_interest_scores(db, 1, now=NOW)
    assert s["signals"]["BD003"]["freq"] == 0.5


def test_list_products_max_risk_filter_applied(db):
    """max_risk 也是过滤条件：mixed 档里 >R3 的 MX006/MX007/MX010 不应被计入。"""
    _mk_user(db)
    _audit(db, "list_products",
           {"p_type": "mixed", "max_risk": 3, "keyword": None},
           NOW.isoformat())
    scores = compute_interest_scores(db, 1, now=NOW)

    assert set(scores["signals"]) == {"MX005"}


def test_time_decay_boundary(db):
    """衰减边界：今天/恰 1τ 天/30 天前三笔信号，权重严格递减且逐点吻合。"""
    _mk_user(db)
    _audit(db, "compare_products", {"ids": [ID_BY_CODE["MF001"]]},
           NOW.isoformat())                       # 距今 0 天 → e^0 = 1
    _audit(db, "compare_products", {"ids": [ID_BY_CODE["BD003"]]},
           "2026-10-26T12:00:00")                 # 恰 1/τ 天 → e^-1
    _audit(db, "compare_products", {"ids": [ID_BY_CODE["MX005"]]},
           "2026-09-27T12:00:00")                 # 30 天 → e^-30 ≈ 0
    scores = compute_interest_scores(db, 1, now=NOW)

    weights = {
        "MF001": ALPHA * 1.0 + BETA * math.exp(-0 / TAU_DAYS),
        "BD003": ALPHA * 1.0 + BETA * math.exp(-1 / TAU_DAYS),
        "MX005": ALPHA * 1.0 + BETA * math.exp(-30 / TAU_DAYS),
    }
    expected = _expected_softmax(
        {**weights, **{c: 0.0 for c in CODE_BY_ID.values()
                       if c not in weights}})
    assert scores["by_product"]["MF001"] > scores["by_product"]["BD003"] \
        > scores["by_product"]["MX005"]
    for code, prob in expected.items():
        assert scores["by_product"][code] == pytest.approx(prob, abs=1e-6)


def test_probabilities_sum_to_one_and_trade_weight(db):
    """多信号混合：两套分布概率和均为 1；申赎权重(2.0)高于浏览(1.0)。"""
    _mk_user(db)
    acct = _mk_account(db, 1, 1000000)
    _audit(db, "list_products", {"p_type": "bond"}, "2026-10-27T08:00:00")
    _audit(db, "list_products", {"p_type": "bond"}, "2026-10-26T08:00:00")
    _audit(db, "compare_products", {"ids": [ID_BY_CODE["MF001"],
                                            ID_BY_CODE["BD003"]]},
           "2026-10-25T08:00:00")
    _tx(db, acct, "2026-10-27T09:00:00", "subscribe", "黄金积存")
    _tx(db, acct, "2026-10-24T09:00:00", "subscribe", "黄金积存")
    _tx(db, acct, "2026-10-27T10:00:00", "redeem", "成长混合基金")
    scores = compute_interest_scores(db, 1, now=NOW)

    # 6 件行为动作（2 次类型浏览 + 1 次比较 + 3 笔申赎）≥ 5 → high；
    # 一次比较里的产品数不重复计事件（旧实现会把这里算成 9 件）
    assert scores["confidence"] == "high"
    assert sum(scores["by_product"].values()) == pytest.approx(1.0, abs=1e-5)
    assert sum(scores["by_type"].values()) == pytest.approx(1.0, abs=1e-5)
    assert scores["by_type"]["bond"] == pytest.approx(
        scores["by_product"]["BD003"] + scores["by_product"]["BD004"], abs=1e-6)
    # 申赎两笔 ×2.0 = 4.0；浏览/比较各按 0.5/1.0 落到对应产品
    assert scores["signals"]["GD008"]["freq"] == 4.0
    assert scores["signals"]["GD008"]["count"] == 2
    assert scores["signals"]["MF001"]["freq"] == 1.0
    assert scores["signals"]["BD003"]["freq"] == pytest.approx(
        1.0 + 0.5 + 0.5)  # 点名比较 1 次 + 两轮 bond 类型浏览各摊 0.5
    assert {s["source"] for s in scores["signals"]["GD008"]["sources"]} == \
        {"tx:subscribe"}


def test_trade_signals_are_user_scoped(db):
    """申赎流水按账户归属过滤：别人的申购不抬高我的兴趣分布。"""
    _mk_user(db, uid=1)
    _mk_user(db, uid=2)
    other_acct = _mk_account(db, 2, 0)
    _tx(db, other_acct, NOW.isoformat(), "subscribe", "黄金积存")

    mine = compute_interest_scores(db, 1, now=NOW)
    assert mine["cold_start"] is True
    assert mine["signals"] == {}

    theirs = compute_interest_scores(db, 2, now=NOW)
    assert theirs["cold_start"] is False
    assert theirs["signals"]["GD008"]["freq"] == 2.0  # 申赎权重 2.0


def test_audit_signals_ignored_in_multi_user_db(db):
    """audit_log 没有 user_id：多用户库无法归因 → 浏览信号一律跳过。

    否则任何人的一次比较都会出现在每个用户的兴趣分布里，零行为用户
    会拿到 cold_start=False / confidence=high 和别人的兴趣分布。
    """
    _mk_user(db, uid=1)
    _mk_user(db, uid=2)
    _audit(db, "compare_products", {"ids": [ID_BY_CODE["MF001"]]},
           NOW.isoformat())

    for uid in (1, 2):
        scores = compute_interest_scores(db, uid, now=NOW)
        assert scores["cold_start"] is True
        assert scores["signals"] == {}
        assert scores["confidence"] == "low"
        assert scores["fallback"] == []  # 两用户都无记忆/持仓


def test_one_compare_call_of_many_products_counts_one_event(db):
    """置信度按用户动作计数：一次比较 5 只产品 = 1 件证据，不是 5 件。"""
    _mk_user(db)
    _audit(db, "compare_products",
           {"ids": [ID_BY_CODE[c] for c in ("MF001", "DP002", "BD003",
                                            "BD004", "MX005")]},
           NOW.isoformat())
    scores = compute_interest_scores(db, 1, now=NOW)

    assert len(scores["signals"]) == 5  # 5 只产品都有信号明细
    assert scores["cold_start"] is False
    # 1 件 < 5：旧实现按命中产品数累加会直接 high
    assert scores["confidence"] == "medium"


# ------------------------------------------------------------- M2 准入
def test_eligibility_three_rejection_reasons(db):
    """三类拒绝理由同框：未成年 + 未测评买 R5 + 余额低于起购。"""
    _mk_user(db, uid=2, id_card="44030020100501123X")  # 2010-05-01 → 16 岁
    result = check_eligibility(db, 2, "MX010", now=NOW)

    assert result["eligible"] is False
    assert len(result["reasons"]) == 3
    assert "年龄未满 18 周岁" in result["reasons"][0]
    assert "尚未完成风险测评" in result["reasons"][1]
    assert "单账户可用余额最高 0.00 元低于起购金额 100.00 元" in result["reasons"][2]
    assert result["reason"] == "；".join(result["reasons"])
    assert result["checks"]["age"] == {"ok": False, "verified": True,
                                       "age": 16, "min_age": 18}
    assert result["checks"]["risk"]["ok"] is False
    assert result["checks"]["risk"]["user_level"] is None
    assert result["checks"]["min_amount"]["ok"] is False
    assert result["checks"]["min_amount"]["basis"] == "max_single_account"
    assert result["checks"]["income"] == {"ok": True, "enabled": False,
                                          "note": "收入门槛未启用"}


def test_eligibility_empty_id_card_old_user_fallback(db):
    """老用户 id_card 空串：年龄门槛不据此拒绝，标记未核验（可审计）。"""
    _mk_user(db, id_card="")  # seed 播种的存量用户即空串
    _set_risk(db, 1, "C3")
    _mk_account(db, 1, 1000000)
    result = check_eligibility(db, 1, "BD003", now=NOW)  # R2 ≤ C3，余额充足

    assert result["eligible"] is True
    assert result["reasons"] == []
    assert result["checks"]["age"]["verified"] is False
    assert result["checks"]["age"]["ok"] is True
    assert "未核验" in result["checks"]["age"]["note"]
    assert result["checks"]["risk"] == {"ok": True, "product_risk": "R2",
                                        "user_level": "C3", "risk_cap": 3}


def test_eligibility_malformed_id_card_falls_back(db):
    """非法证件号（2 月 30 日）与空串同口径：不据此拒绝、标记未核验。"""
    _mk_user(db, id_card="44030019900230123X")
    _set_risk(db, 1, "C3")
    _mk_account(db, 1, 1000000)
    result = check_eligibility(db, 1, "BD003", now=NOW)

    assert result["eligible"] is True
    assert result["checks"]["age"]["ok"] is True
    assert result["checks"]["age"]["verified"] is False
    assert "未核验" in result["checks"]["age"]["note"]


def test_age_parser_rejects_future_and_absurd_birth_years(db):
    """年份段是未来/早于 1900 的脏号按不可解析处理，不能算成负数年龄拒绝。

    真实库主演示用户 11 的身份证就是 18 位但月份非法；这里锁住同类脏数据
    （2090/9184 这类"日期合法但年份荒谬"）走老用户兜底而非未成年拒绝。
    """
    for card in ("44030020900615123X",   # 2090-06-15 → 未来
                 "44030091840615123X",   # 9184-06-15 → 未来
                 "44030018900615123X"):  # 1890-06-15 → 早于 1900
        assert _age_from_id_card(card, today=NOW.date()) is None

    _mk_user(db, uid=21, id_card="44030020900615123X")
    _set_risk(db, 21, "C5")
    _mk_account(db, 21, 1000000)
    result = check_eligibility(db, 21, "MF001", now=NOW)

    assert result["checks"]["age"]["ok"] is True
    assert result["checks"]["age"]["verified"] is False
    assert "未核验" in result["checks"]["age"]["note"]
    assert result["eligible"] is True
    assert not any("岁" in r for r in result["reasons"])  # 不再出现「-64 岁」


def test_eligibility_age_parsing_18_and_15_digit(db):
    """身份证年龄解析：18 位、15 位老号、18 岁生日当天边界。"""
    _mk_user(db, uid=11, id_card="44030019900615123X")  # 1990-06-15 → 36 岁
    _mk_user(db, uid=12, id_card="440300900615123")     # 15 位老号 → 36 岁
    _mk_user(db, uid=13, id_card="44030020081027123X")  # 2026-10-27 满 18
    _mk_user(db, uid=14, id_card="44030020081028123X")  # 差一天仍 17 岁
    _set_risk(db, 11, "C5")
    _set_risk(db, 12, "C5")
    _set_risk(db, 13, "C5")
    _set_risk(db, 14, "C5")
    for uid in (11, 12, 13):
        _mk_account(db, uid, 1000000)

    for uid in (11, 12, 13):
        checks = check_eligibility(db, uid, "MF001", now=NOW)["checks"]["age"]
        assert checks["verified"] is True
        assert checks["ok"] is True
    assert check_eligibility(db, 11, "MF001", now=NOW)["checks"]["age"]["age"] == 36
    assert check_eligibility(db, 12, "MF001", now=NOW)["checks"]["age"]["age"] == 36
    assert check_eligibility(db, 13, "MF001", now=NOW)["checks"]["age"]["age"] == 18
    r14 = check_eligibility(db, 14, "MF001", now=NOW)
    assert r14["checks"]["age"]["age"] == 17
    assert any("年龄未满" in r for r in r14["reasons"])


def test_eligibility_min_amount_uses_single_account_not_sum(db):
    """起购按单账户余额判定（对齐 wealth.py 扣款闸门），不合计多账户。

    两张各 600 元、合计 1200 元：DP002 起购 1000 元。合计口径会误判可买，
    但任何一张账户单独都付不起，真实申购会「余额不足」。
    """
    _mk_user(db)
    _set_risk(db, 1, "C5")
    _mk_account(db, 1, 60000)   # 600 元
    _mk_account(db, 1, 60000)   # 再 600 元，合计 1200 元
    result = check_eligibility(db, 1, "DP002", now=NOW)  # 起购 1000 元

    assert result["eligible"] is False
    assert result["checks"]["min_amount"]["available_cents"] == 60000
    assert result["checks"]["min_amount"]["basis"] == "max_single_account"
    assert "单账户可用余额最高 600.00 元" in result["reason"]

    # 候选理由同样不能再写「起购 1000 ≤ 可用余额 1200」这种错话：
    # DP002 不应进入候选（其余起购 ≤600 的产品仍可推荐）
    codes = [c["code"] for c in recommend_candidates(
        db, 1, top_n=10, now=NOW)]
    assert "DP002" not in codes
    assert "MF001" in codes


def test_eligibility_risk_gate_matches_wealth_rule(db):
    """风险门槛口径对齐 wealth.py：C2 可购 R2、拦 R3；无测评只放 R1。"""
    _mk_user(db)
    _set_risk(db, 1, "C2")
    _mk_account(db, 1, 1000000)
    assert check_eligibility(db, 1, "BD003", now=NOW)["eligible"] is True
    r = check_eligibility(db, 1, "MX005", now=NOW)  # R3 > C2 上限 R2
    assert r["eligible"] is False
    assert "产品风险 R3 超出您的风险承受等级 C2（最高可购 R2）" in r["reasons"]

    _mk_user(db, uid=9)
    _mk_account(db, 9, 1000000)  # 无测评用户：R1 放行、R2 拦截
    assert check_eligibility(db, 9, "MF001", now=NOW)["eligible"] is True
    assert check_eligibility(db, 9, "BD003", now=NOW)["eligible"] is False


def test_eligibility_unknown_product_raises(db):
    _mk_user(db)
    with pytest.raises(ValueError, match="产品不存在"):
        check_eligibility(db, 1, "NOPE")


def test_product_eligibility_table_shape():
    """门槛表覆盖全部 10 只种子产品；收入门槛默认不启用。"""
    codes = {p[0] for p in PRODUCTS}
    assert set(PRODUCT_ELIGIBILITY) == codes
    for cfg in PRODUCT_ELIGIBILITY.values():
        assert cfg["min_age"] == 18
        assert cfg["income_tier"] is None
    assert set(PRODUCT_TYPES) == {"money_fund", "deposit", "bond",
                                  "mixed", "gold"}


# ------------------------------------------------------------- M3 候选
def test_recommend_all_held_returns_empty(db):
    """全持有 → 候选为空（「未持有」是候选集的第一道过滤）。"""
    _mk_user(db)
    _set_risk(db, 1, "C5")
    _mk_account(db, 1, 1000000)
    for pid in CODE_BY_ID:
        _holding(db, 1, pid)
    assert recommend_candidates(db, 1, now=NOW) == []


def test_recommend_new_user_capped_at_r1(db):
    """新用户（无测评）候选受风险上限约束：只出 R1，且受起购金额拦截。"""
    _mk_user(db, uid=3)
    _mk_account(db, 3, 100000)  # 1000 元：拦掉起购 2 万的 DP009
    _audit(db, "compare_products", {"ids": [ID_BY_CODE["MF001"]]},
           NOW.isoformat())  # 浏览信号抬高 MF001 的排序

    cands = recommend_candidates(db, 3, top_n=5, now=NOW)
    assert [c["code"] for c in cands] == ["MF001", "DP002"]
    for c in cands:
        assert c["risk_level"] == "R1"
        assert c["suitability"] == 1.0  # R1 / 未测评上限 1
        assert len(c["reasons"]) == 3
        assert "未持有" in c["reasons"][2]
        assert "未测评，按 R1 上限" in c["reasons"][1]
    assert cands[0]["interest_prob"] > cands[1]["interest_prob"]
    assert recommend_candidates(db, 3, top_n=1, now=NOW) == cands[:1]
    assert recommend_candidates(db, 3, top_n=0, now=NOW) == []


def test_recommend_ranks_by_interest_times_suitability(db):
    """排序 = 兴趣 × 适当性：R3 产品用满 C3 风险预算，可反超高兴趣 R1。"""
    _mk_user(db, uid=4)
    _set_risk(db, 4, "C3")
    _mk_account(db, 4, 1000000)
    for _ in range(3):  # DP002(R1) 兴趣：3 次比较
        _audit(db, "compare_products", {"ids": [ID_BY_CODE["DP002"]]},
               NOW.isoformat())
    _audit(db, "compare_products", {"ids": [ID_BY_CODE["MX005"]]},
           NOW.isoformat())  # MX005(R3) 兴趣：1 次

    cands = recommend_candidates(db, 4, top_n=3, now=NOW)
    codes = [c["code"] for c in cands]
    assert codes[0] == "MX005"  # 兴趣 0.145×适当性 1.0 > DP002 兴趣 0.217×1/3
    assert "DP002" in codes
    top = cands[0]
    assert top["score"] == pytest.approx(
        top["interest_prob"] * top["suitability"], abs=1e-6)
    assert top["suitability"] == pytest.approx(3 / 3)
    assert "适当性匹配：产品 R3 ≤ 可购上限 R3（C3 级）" in top["reasons"][1]
    assert "1 次产品比较" in top["reasons"][0]
    # 默认 top_n=3（下游 M4/API 依赖的默认口径）
    assert len(recommend_candidates(db, 4, now=NOW)) == 3
