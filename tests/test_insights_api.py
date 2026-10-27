"""洞察 API(M5)测试:开关、鉴权、正常返回、LLM stub 与全部降级路径。

仿 tests/test_api_stream.py:临时库 + create_app + TestClient(with 触发
lifespan 后登录拿 token);LLM 一律 stub——engine 侧注入 app.state.insights_llm,
且本文件 autouse 夹具把 get_llm 钉成抛错,任何漏注入的用例只会走规则降级,
全程不联网。

库装配 = _mk_db(用户/账户/联系人/登录态) + 10 只种子产品 + C3 风险测评:
- 用户 1(1990 年生,id_card 可解析)余额 1 万元;
- C3 → 可购上限 R3 → 候选应为 R1-R3 且起购 ≤ 1 万的产品
  (MX006/MX007/MX010 因 R4/R5 被适当性滤掉,DP009 因 2 万起购被滤掉);
- 空审计/空流水 → 冷启动均匀分布 → 候选排序按 兴趣(0.1)×适当性 + bps
  次级键完全确定,可精确断言前三名。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

import agent.insights_engine as engine_mod
from agent.api import create_app
from bank_core.money import yuan_to_cents
from bank_core.seed import PRODUCTS
from test_agent_graph import _mk_db

# C3 + 1 万元余额下的确定性候选前三(均匀兴趣 0.1 × suitability,bps 次级键):
# MX005(0.1×3/3,bps550) > GD008(0.1×3/3,bps480) > BD004(0.1×2/3,bps380)
# 说明:engine 的 top_n(默认 3)同时约束候选集与推荐条数——candidates
# 即 LLM 白名单,候选与推荐基数同一批(与 recommend_candidates 默认口径一致)。
EXPECTED_TOP3 = ["MX005", "GD008", "BD004"]
# 持有 GD008 后的确定性候选前三(gold 档被剔除,后补 BD003)
EXPECTED_TOP3_HOLDING_GD008 = ["MX005", "BD004", "BD003"]


@pytest.fixture(autouse=True)
def _forbid_real_llm(monkeypatch):
    """全文件默认禁用真实 LLM 工厂——「LLM 一律 stub,不真实外呼」的硬保证。

    开发机上通常配着 ZAI_API_KEY:若某个用例漏注入 insights_llm,engine 会
    真的打外网(两次 20s 超时还会拖慢套件,且结果不可复现)。这里把
    get_llm 钉成抛错,漏注入最多走规则降级;验证「无 Key 降级」的用例仍可
    在用例内再覆盖一次(内层 monkeypatch 生效)。
    """
    def _no_outbound(*_args, **_kwargs):
        raise RuntimeError("测试禁用真实 LLM 外呼（请注入 insights_llm stub）")

    monkeypatch.setattr(engine_mod, "get_llm", _no_outbound)


def _mk_insights_db(tmp_path: Path) -> Path:
    """_mk_db 基础上补:有效身份证号(年龄可核验) + 10 只产品 + C3 测评。"""
    db = _mk_db(tmp_path, name="bank_insights.db")
    conn = sqlite3.connect(db)
    # 1990-01-01 生 → 恒已成年(以 2026 年为基准 36 岁),年龄闸门走 verified 分支
    conn.execute("UPDATE users SET id_card='110101199001011230' WHERE id=1")
    for i, (code, name, ptype, risk, bps, min_yuan, *rest) in \
            enumerate(PRODUCTS, start=1):
        # rest = (lock_days, subscription_fee_bps, redemption_fee_bps, intro)
        conn.execute(
            """INSERT INTO wealth_products
               (id, code, name, p_type, risk_level, expected_return_bps,
                min_subscribe_cents, lock_days, subscription_fee_bps,
                redemption_fee_bps, intro)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (i, code, name, ptype, risk, bps,
             yuan_to_cents(str(min_yuan)), *rest))
    conn.execute(
        "INSERT INTO risk_profiles (user_id,answers_json,score,level,updated_at) "
        "VALUES (1,'{}',15,'C3','2026-10-01T00:00:00')")
    conn.commit()
    conn.close()
    return db


def _mk_app(tmp_path: Path, db: Path, insights_llm=None):
    """create_app(图模型用假 LLM,不联网);可选注入洞察专用 LLM stub。"""
    app = create_app(
        llm=FakeMessagesListChatModel(responses=[AIMessage(content="hi")]),
        db_path=db,
        checkpoint_path=tmp_path / "ins_ckpt.sqlite",
        threads_path=tmp_path / "ins_threads.sqlite")
    if insights_llm is not None:
        app.state.insights_llm = insights_llm  # engine 的测试注入口
    return app


def _login(client: TestClient) -> str:
    # 登录两步(2026-10-27 二次校验):密码 → 挑战(演示回显码) → 会话
    c1 = client.post("/api/auth/login",
                     json={"identifier": "13800000000",
                           "password": "Demo@12345"})
    assert c1.status_code == 200, c1.text
    r = client.post("/api/auth/login",
                    json={"identifier": "13800000000",
                          "password": "Demo@12345",
                          "challenge_id": c1.json()["challenge_id"],
                          "code": c1.json()["demo_code"]})
    assert r.status_code == 200
    return r.json()["token"]


class _StubLLM:
    """最小 LLM stub:按序吐预设 content,记录每次收到的消息(不联网)。

    与 engine 的协议只有一条:await ainvoke(messages) -> 带 .content 的对象。
    """

    def __init__(self, *contents: str):
        self._contents = list(contents)
        self.calls: list[list] = []

    async def ainvoke(self, messages, **_kwargs):
        self.calls.append(list(messages))
        content = self._contents.pop(0) if self._contents else "{}"
        return SimpleNamespace(content=content)


_LLM_GOOD = ('{"items": ['
             '{"product_code": "MX005", '
             '"reason": "您近期关注混合类产品，且 R3 完全匹配 C3 风险承受等级。", '
             '"suggested_question": "MX005 现在适合我买入吗？"}, '
             '{"product_code": "GD008", '
             '"reason": "黄金与现有持仓相关性低，可分散组合波动。", '
             '"suggested_question": "要不要开始黄金积存？"}]}')


# ----------------------------------------------------------------- 开关(默认关闭)

@pytest.mark.parametrize("value", [None, "0"])
def test_insights_router_disabled_by_default(tmp_path, monkeypatch, value):
    """未设或显式置 0:路由不注册,两个端点都 404(基线行为完全不变)。"""
    if value is None:
        monkeypatch.delenv("AGENT_INSIGHTS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("AGENT_INSIGHTS_ENABLED", value)
    db = _mk_insights_db(tmp_path)
    app = _mk_app(tmp_path, db)
    with TestClient(app) as client:
        tok = _login(client)
        headers = {"x-bank-token": tok}
        assert client.get("/api/insights", headers=headers).status_code == 404
        assert client.get("/api/insights/eligibility",
                          params={"product_code": "MF001"},
                          headers=headers).status_code == 404


def test_insights_router_enabled_registers_routes(tmp_path, monkeypatch):
    """开关打开:两个端点可达(登录后 200)。"""
    monkeypatch.setenv("AGENT_INSIGHTS_ENABLED", "1")
    db = _mk_insights_db(tmp_path)
    app = _mk_app(tmp_path, db, insights_llm=_StubLLM(_LLM_GOOD))
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get("/api/insights", headers={"x-bank-token": tok})
        assert r.status_code == 200
        assert client.get("/api/insights/eligibility",
                          params={"product_code": "MF001"},
                          headers={"x-bank-token": tok}).status_code == 200


# ----------------------------------------------------------------- 鉴权

def test_insights_requires_login_and_self_scope(tmp_path, monkeypatch):
    """未登录 401;登录后查他人 user_id 403(不泄露他人画像)。"""
    monkeypatch.setenv("AGENT_INSIGHTS_ENABLED", "1")
    db = _mk_insights_db(tmp_path)
    app = _mk_app(tmp_path, db)
    with TestClient(app) as client:
        assert client.get("/api/insights").status_code == 401
        assert client.get("/api/insights/eligibility",
                          params={"product_code": "MF001"}).status_code == 401
        tok = _login(client)
        headers = {"x-bank-token": tok}
        assert client.get("/api/insights", params={"user_id": 2},
                          headers=headers).status_code == 403
        assert client.get("/api/insights", params={"user_id": 1},
                          headers=headers).status_code == 200


# ----------------------------------------------------------------- 正常返回(LLM stub)

def test_insights_happy_path_with_llm(tmp_path, monkeypatch):
    """结构完整:兴趣/信号/资产/持仓/候选/推荐;LLM 产出通过 Pydantic 且
    只含候选产品;兴趣概率和为 1;候选前三与手算一致。"""
    monkeypatch.setenv("AGENT_INSIGHTS_ENABLED", "1")
    db = _mk_insights_db(tmp_path)
    stub = _StubLLM(_LLM_GOOD)
    app = _mk_app(tmp_path, db, insights_llm=stub)
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get("/api/insights", headers={"x-bank-token": tok})
        assert r.status_code == 200
        body = r.json()

        # 聚合视图字段齐(契约冻结点,前端按此渲染)
        for key in ("user_id", "generated_at", "interest", "signals",
                    "accounts", "holdings", "candidates", "recommendations",
                    "recommendation_source", "recommendation_note"):
            assert key in body, f"缺少字段 {key}"
        assert body["user_id"] == 1

        # M1 兴趣分布:冷启动(空审计/空流水)→ 均匀分布、置信度 low
        interest = body["interest"]
        assert interest["confidence"] == "low"
        assert interest["cold_start"] is True
        assert abs(sum(interest["by_product"].values()) - 1.0) < 1e-6
        assert abs(sum(interest["by_type"].values()) - 1.0) < 1e-6
        assert body["signals"] == {}  # 无行为信号 → 无明细

        # 资产快照:1 万元(整数分口径)
        assert body["accounts"]["total_balance_yuan"] == "10000.00"
        assert body["holdings"] == []

        # M3 候选:R≤3 且起购≤1 万的未持有产品按 兴趣×适当性+bps 排序,top_n=3
        codes = [c["code"] for c in body["candidates"]]
        assert codes == EXPECTED_TOP3

        # M4 推荐:LLM 产出原样透传,且每条都在候选集内
        assert body["recommendation_source"] == "llm"
        recs = body["recommendations"]
        assert [x["product_code"] for x in recs] == ["MX005", "GD008"]
        assert {x["product_code"] for x in recs} <= set(codes)
        assert recs[0]["suggested_question"].endswith("？")

        # LLM 收到的是四段式 prompt(系统提示 + 四段注入)
        assert len(stub.calls) == 1
        sys_text = str(stub.calls[0][0].content)
        user_text = str(stub.calls[0][1].content)
        assert "客户洞察助手" in sys_text
        for section in ("【客户兴趣分布】", "【当前资产】", "【已持有产品】",
                        "【可推荐候选】"):
            assert section in user_text


def test_insights_holds_products_excluded_from_candidates(tmp_path, monkeypatch):
    """已持有产品不进候选:持有 top1 候选 GD008 后,它被剔除、后位 BD003
    递补,且兴趣分布走持仓兜底(冷启动先验,gold 档拿到伪信号)。"""
    monkeypatch.setenv("AGENT_INSIGHTS_ENABLED", "1")
    db = _mk_insights_db(tmp_path)
    conn = sqlite3.connect(db)
    # GD008(黄金积存)转为持有:gold 档唯一产品 → 兜底信号精确可断言
    conn.execute(
        "INSERT INTO wealth_holdings (user_id, product_id, principal_cents,"
        " est_value_cents, status, subscribed_at)"
        " VALUES (1, 8, 10000, 10000, 'holding', '2026-10-01T10:00:00')")
    conn.commit()
    conn.close()
    app = _mk_app(tmp_path, db, insights_llm=_StubLLM(_LLM_GOOD))
    with TestClient(app) as client:
        tok = _login(client)
        body = client.get("/api/insights",
                          headers={"x-bank-token": tok}).json()
        codes = [c["code"] for c in body["candidates"]]
        assert "GD008" not in codes  # 已持有 → 剔除
        assert codes == EXPECTED_TOP3_HOLDING_GD008  # 后位递补
        # 持仓兜底先验:GD008 拿到伪信号,fallback 注明来源
        assert body["interest"]["fallback"] == ["wealth_holdings:当前持仓"]
        assert body["signals"]["GD008"]["sources"][0]["source"] == "holding"
        # LLM 白名单跟随新候选集:推荐只能是未持有产品
        recs = {x["product_code"] for x in body["recommendations"]}
        assert recs <= set(codes)


# ----------------------------------------------------------------- 降级路径

def test_insights_degrades_without_llm(tmp_path, monkeypatch):
    """无 LLM(get_llm 抛 RuntimeError):直接降级规则推荐,不 500、不外呼。"""
    import agent.insights_engine as engine_mod

    def _no_key(*_a, **_k):
        raise RuntimeError("未配置 API Key")

    monkeypatch.setenv("AGENT_INSIGHTS_ENABLED", "1")
    monkeypatch.setattr(engine_mod, "get_llm", _no_key)
    db = _mk_insights_db(tmp_path)
    app = _mk_app(tmp_path, db)  # 不注入 insights_llm → 走 get_llm()
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get("/api/insights", headers={"x-bank-token": tok})
        assert r.status_code == 200
        body = r.json()
        assert body["recommendation_source"] == "rules"
        assert "降级" in body["recommendation_note"]
        # 规则结果 = M3 候选 + 模板话术:前三名逐条对应
        recs = body["recommendations"]
        assert [x["product_code"] for x in recs] == EXPECTED_TOP3
        for x in recs:
            assert "适当性匹配" in x["reason"]  # 直接引用候选 reasons[]
            assert x["suggested_question"].startswith("我还没买过")


def test_insights_llm_retry_then_succeed(tmp_path, monkeypatch):
    """首次输出坏 JSON、重试 1 次成功:仍取 LLM 结果,共调用 2 次。"""
    monkeypatch.setenv("AGENT_INSIGHTS_ENABLED", "1")
    db = _mk_insights_db(tmp_path)
    stub = _StubLLM("这不是 JSON：抱歉我无法输出。", _LLM_GOOD)
    app = _mk_app(tmp_path, db, insights_llm=stub)
    with TestClient(app) as client:
        tok = _login(client)
        body = client.get("/api/insights",
                          headers={"x-bank-token": tok}).json()
        assert body["recommendation_source"] == "llm"
        assert [x["product_code"] for x in body["recommendations"]] == \
            ["MX005", "GD008"]
        assert len(stub.calls) == 2  # 重试恰好 1 次


def test_insights_llm_fails_twice_then_rules(tmp_path, monkeypatch):
    """两次都解析失败:降级规则结果(候选前三 + 模板话术),不 500。"""
    monkeypatch.setenv("AGENT_INSIGHTS_ENABLED", "1")
    db = _mk_insights_db(tmp_path)
    stub = _StubLLM("垃圾输出第一次", "垃圾输出第二次")
    app = _mk_app(tmp_path, db, insights_llm=stub)
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get("/api/insights", headers={"x-bank-token": tok})
        assert r.status_code == 200
        body = r.json()
        assert body["recommendation_source"] == "rules"
        assert len(stub.calls) == 2  # 重试 1 次后放弃,不再继续调
        assert [x["product_code"] for x in body["recommendations"]] == \
            EXPECTED_TOP3


def test_insights_llm_hallucinated_code_rejected(tmp_path, monkeypatch):
    """LLM 编造候选外的产品:视为解析失败 → 重试 → 降级规则(防幻觉闸门)。"""
    monkeypatch.setenv("AGENT_INSIGHTS_ENABLED", "1")
    db = _mk_insights_db(tmp_path)
    bad = ('{"items": [{"product_code": "MX999", "reason": "幻觉产品", '
           '"suggested_question": "买吗？"}]}')
    stub = _StubLLM(bad, bad)
    app = _mk_app(tmp_path, db, insights_llm=stub)
    with TestClient(app) as client:
        tok = _login(client)
        body = client.get("/api/insights",
                          headers={"x-bank-token": tok}).json()
        assert body["recommendation_source"] == "rules"
        codes = {x["product_code"] for x in body["recommendations"]}
        assert "MX999" not in codes


def test_insights_no_candidates_returns_empty(tmp_path, monkeypatch):
    """全部持有 → 候选为空 → 推荐为空(source=none),不调 LLM、不 500。"""
    monkeypatch.setenv("AGENT_INSIGHTS_ENABLED", "1")
    db = _mk_insights_db(tmp_path)
    conn = sqlite3.connect(db)
    # 把全部合格产品(R≤3 且起购≤1 万,共 6 只)转为持有 → 候选全集为空
    eligible_codes = {"MF001", "DP002", "BD003", "BD004", "MX005", "GD008"}
    conn.executemany(
        "INSERT INTO wealth_holdings (user_id, product_id, principal_cents,"
        " est_value_cents, status, subscribed_at)"
        " VALUES (1, ?, 10000, 10000, 'holding', '2026-10-01T10:00:00')",
        [(i,) for i, p in enumerate(PRODUCTS, start=1)
         if p[0] in eligible_codes])
    conn.commit()
    conn.close()
    stub = _StubLLM(_LLM_GOOD)  # 若被调用说明空候选没短路
    app = _mk_app(tmp_path, db, insights_llm=stub)
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get("/api/insights", headers={"x-bank-token": tok})
        assert r.status_code == 200
        body = r.json()
        assert body["candidates"] == []
        assert body["recommendations"] == []
        assert body["recommendation_source"] == "none"
        assert stub.calls == []  # 无候选时根本不发起 LLM 调用


# ----------------------------------------------------------------- 引擎单点入口

def test_generate_recommendations_matches_view(tmp_path):
    """M4 单点入口与聚合视图同源:注入 stub 时 items == recommendations。

    两个入口共用 build_insights_view 的实现,这里锁住「子集视图」的一致性,
    防止将来只改一处导致聊天 skill 与 API 给出不同推荐。
    """
    import asyncio

    from agent.insights_engine import build_insights_view, generate_recommendations

    db = _mk_insights_db(tmp_path)
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row  # bank_core 按列名取值
    try:
        async def scenario():
            view = await build_insights_view(conn, 1, llm=_StubLLM(_LLM_GOOD))
            rec = await generate_recommendations(conn, 1, llm=_StubLLM(_LLM_GOOD))
            return view, rec

        view, rec = asyncio.run(scenario())
    finally:
        conn.close()
    assert rec["items"] == view["recommendations"]
    assert rec["source"] == "llm" == view["recommendation_source"]
    assert [x["product_code"] for x in rec["items"]] == ["MX005", "GD008"]


# ----------------------------------------------------------------- 兜底与健壮性

def test_insights_outer_guard_returns_degraded_200(tmp_path, monkeypatch):
    """聚合层抛异常(如库锁):API 外层兜底 200 降级结构,不抛 500。

    降级结构与成功响应字段同构(含 generated_at),前端一套渲染逻辑即可;
    error 是唯一附加键,recommendations 为空且 source=none。
    """
    import agent.insights_api as insights_api_mod

    async def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setenv("AGENT_INSIGHTS_ENABLED", "1")
    monkeypatch.setattr(insights_api_mod, "build_insights_view", _boom)
    db = _mk_insights_db(tmp_path)
    app = _mk_app(tmp_path, db)
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get("/api/insights", headers={"x-bank-token": tok})
        assert r.status_code == 200
        body = r.json()
        assert "洞察暂不可用" in body["error"]
        assert "OperationalError" in body["error"]
        for key in ("user_id", "generated_at", "interest", "signals",
                    "accounts", "holdings", "candidates", "recommendations",
                    "recommendation_source", "recommendation_note"):
            assert key in body, f"降级结构缺少字段 {key}"
        assert body["recommendations"] == []
        assert body["recommendation_source"] == "none"


def test_llm_timeout_env_parse_is_fault_tolerant():
    """AGENT_INSIGHTS_LLM_TIMEOUT 非法/缺失/非正数不得在 import 期炸掉主应用。"""
    assert engine_mod._parse_timeout(None) == 20.0
    assert engine_mod._parse_timeout("   ") == 20.0
    assert engine_mod._parse_timeout("abc") == 20.0
    assert engine_mod._parse_timeout("-3") == 20.0
    assert engine_mod._parse_timeout("5") == 5.0
    assert engine_mod.LLM_TIMEOUT_S > 0


# ----------------------------------------------------------------- 资格校验端点

def test_eligibility_endpoint(tmp_path, monkeypatch):
    """合格产品 eligible=True;超风险等级给出可审计理由;未知产品 404。"""
    monkeypatch.setenv("AGENT_INSIGHTS_ENABLED", "1")
    db = _mk_insights_db(tmp_path)
    app = _mk_app(tmp_path, db)
    with TestClient(app) as client:
        tok = _login(client)
        headers = {"x-bank-token": tok}

        # R1 货币基金 + 1 元起购 + 成年:C3 用户三道闸门全过
        r = client.get("/api/insights/eligibility",
                       params={"product_code": "MF001"}, headers=headers)
        assert r.status_code == 200
        ok = r.json()
        assert ok["eligible"] is True
        assert ok["product_name"] == "余额+货币基金"
        assert set(ok["checks"]) == {"age", "risk", "min_amount", "income"}
        assert ok["checks"]["age"]["verified"] is True  # id_card 可解析
        assert ok["reasons"] == []

        # R4 股基超出 C3 上限:拒绝理由点名风险等级
        r2 = client.get("/api/insights/eligibility",
                        params={"product_code": "MX006"}, headers=headers)
        assert r2.status_code == 200
        denied = r2.json()
        assert denied["eligible"] is False
        assert denied["checks"]["risk"]["ok"] is False
        assert ("超出" in denied["reason"]
                or "风险承受等级" in denied["reason"])

        # 2 万元大额存单超 1 万余额:起购闸门拒绝
        r3 = client.get("/api/insights/eligibility",
                        params={"product_code": "DP009"}, headers=headers)
        assert r3.json()["eligible"] is False
        assert "起购" in r3.json()["reason"] or "低于起购" in r3.json()["reason"]

        # 未知产品 → 404(参数错误,不是资格结论)
        r4 = client.get("/api/insights/eligibility",
                        params={"product_code": "NOPE404"}, headers=headers)
        assert r4.status_code == 404
        assert "不存在" in r4.json()["error"]

        # 缺 product_code → FastAPI 422(query 必填参数)
        assert client.get("/api/insights/eligibility",
                          headers=headers).status_code == 422
