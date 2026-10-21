"""agent.plan 纯函数测试：事件匹配 / 订阅候选 / 可支配 / 计划分支 / 预算编辑。

全部在内存里跑：不建库、不调 LLM、不读全局状态，可在任意机器独立复现。
"""

from __future__ import annotations

from datetime import date, timedelta

from agent.plan import (
    DEFAULT_ACTIONS,
    DEFAULT_SAFETY_BUFFER_YUAN,
    PLAN_FACT_KEYS,
    build_plan,
    compute_disposable,
    constraints_of,
    filter_cut_candidates,
    filter_upcoming_outflows,
    find_event,
    make_plan_id,
    parse_budget_edit,
    plan_card_view,
    wants_cut,
)

TODAY = date.today()


def _d(days: int) -> str:
    return (TODAY + timedelta(days=days)).isoformat()


def _facts(income="5000.00", expense="1000.00", upcoming=(), *, days_left=4,
           buffer=DEFAULT_SAFETY_BUFFER_YUAN, source="monthly_report",
           balance_yuan=None):
    """月报口径事实快照：默认 5000 - 1000 - 0 - 500 = 3500.00。"""
    facts = compute_disposable(income, expense, upcoming, buffer,
                               source=source, balance_yuan=balance_yuan)
    facts.update({
        "event_title": "林悦的生日",
        "event_date": _d(days_left),
        "days_left": days_left,
    })
    return facts


def _sub(sub_id: int, merchant: str, amount: str, category: str = "订阅",
         status: str = "active", period_days: int = 30,
         annual: str | None = None) -> dict:
    return {
        "id": sub_id,
        "merchant": merchant,
        "status": status,
        "category": category,
        "amount_yuan": amount,
        "period_days": period_days,
        "next_charge_date": _d(20),
        "annual_cost_yuan": annual if annual is not None else amount,
    }


SUBS = [
    _sub(1, "腾讯视频VIP", "30.00", annual="353.23"),
    _sub(2, "Keep会员", "19.00", annual="231.17"),
    _sub(3, "张阿姨", "6800.00", category="房租", annual="6800.00"),
    _sub(4, "理财专户", "2000.00", category="转账", annual="2000.00"),
    _sub(5, "余额+货币基金", "2000.00", category="理财", annual="2000.00"),
    _sub(6, "信用卡还款(0231)", "3000.00", category="还款", annual="3000.00"),
    _sub(7, "中国移动", "128.00", category="通讯", annual="128.00"),
    _sub(8, "旧会员", "9.90", status="cancelled", annual="9.90"),
]


def _gates(plan: dict) -> list:
    return [step["gate"] for step in plan["steps"]]


# ------------------------------------------------------------------ 事件匹配

def test_find_event_same_date_only():
    """同名不同日不得命中：用户明说的日期不能被旧事件静默覆盖。"""
    events = [
        {"id": 1, "event_type": "birthday", "title": "林悦的生日",
         "date": _d(354), "days_left": 354},
        {"id": 2, "event_type": "birthday", "title": "妈妈的生日",
         "date": _d(30), "days_left": 30},
    ]
    assert find_event(events, "林悦的生日", _d(4)) is None
    assert find_event(events, "林悦的生日", _d(354))["id"] == 1
    # 妈妈生日不得绑定林悦的生日（同日也不串人）
    assert find_event(events, "妈妈生日", _d(354)) is None
    # 同日泛称"生日"命中生日事件
    assert find_event(events, "生日", _d(354))["id"] == 1
    assert find_event(events, "生日", _d(30))["id"] == 2
    assert find_event(events, "不存在的事件", _d(4)) is None


def test_find_event_nearest_birthday_and_containment():
    """未给日期时：最近的生日事件优先于标题互含。"""
    events = [
        {"id": 1, "event_type": "birthday", "title": "林悦的生日",
         "date": _d(354), "days_left": 354},
        {"id": 2, "event_type": "birthday", "title": "妈妈的生日",
         "date": _d(30), "days_left": 30},
        {"id": 3, "event_type": "custom", "title": "结婚纪念日",
         "date": _d(2), "days_left": 2},
    ]
    assert find_event(events, "生日", "")["id"] == 2
    assert find_event(events, "纪念日", "")["id"] == 3
    assert find_event(events, "", "")["id"] == 2  # 空标题也算泛称"生日"
    assert find_event(events, "不存在的", "") is None
    assert find_event([], "生日", _d(4)) is None


# ------------------------------------------------------------------ 订阅候选

def test_filter_cut_candidates_excludes_rent_and_wealth():
    """房租/理财/还款/话费/非 active 全部排除，只留真实会员。"""
    names = [c["merchant"] for c in filter_cut_candidates(SUBS)]
    assert names == ["腾讯视频VIP", "Keep会员"]
    for bad in ("张阿姨", "理财专户", "余额+货币基金", "中国移动",
                "信用卡还款(0231)", "旧会员"):
        assert bad not in names


def test_filter_cut_candidates_category_blocks_even_with_plain_name():
    rows = [
        _sub(90, "某商户", "100.00", category="理财"),
        _sub(91, "某商户", "100.00", category="转账"),
        _sub(92, "某商户", "100.00", category="还款"),
        _sub(93, "某商户", "100.00", category="房租"),
        _sub(94, "某商户", "0.00"),
        _sub(95, "某商户", "100.00", status="cancelled"),
        {"id": 96, "merchant": "会员A", "status": "active", "category": "订阅",
         "amount_yuan": "100.00", "period_days": 30},
    ]
    assert [c["sub_id"] for c in filter_cut_candidates(rows)] == [96]


def test_filter_cut_candidates_sorted_by_amount():
    """金额降序 → 年化降序 → sub_id 升序。"""
    rows = [
        _sub(5, "A", "20.00", annual="240.00"),
        _sub(3, "B", "20.00", annual="365.00"),
        _sub(9, "C", "20.00", annual="240.00"),
        _sub(4, "D", "30.00", annual="300.00"),
    ]
    assert [c["merchant"] for c in filter_cut_candidates(rows)] == ["D", "B", "A", "C"]
    # 视图字段齐全（前端 PlanCutCandidate 契约）
    view = filter_cut_candidates(rows)[0]
    assert set(view) == {"sub_id", "merchant", "category", "amount_yuan",
                         "period_text", "next_charge_date", "annual_yuan"}
    assert view["period_text"] == "月"
    assert view["annual_yuan"] == "300.00"


def test_filter_upcoming_outflows_excludes_internal():
    """理财专户/货基/账户互转是自有资金，不能重复扣减可支配额度。"""
    items = [
        {"merchant": "张阿姨", "category": "房租", "amount_yuan": "6800"},
        {"merchant": "理财专户", "category": "转账", "amount_yuan": "2000"},
        {"merchant": "余额+货币基金", "category": "理财", "amount_yuan": "1000"},
        {"merchant": "腾讯视频VIP", "category": "订阅", "amount_yuan": "25"},
        {"merchant": "生活主账户", "category": "转账", "amount_yuan": "300"},
    ]
    kept = filter_upcoming_outflows(items, ["生活主账户"])
    assert [i["merchant"] for i in kept] == ["张阿姨", "腾讯视频VIP"]
    assert len(items) == 5  # 不修改入参


# ------------------------------------------------------------------ 可支配额度

def test_compute_disposable_monthly_report():
    result = compute_disposable("5000.00", "1000.00", [])
    assert result["disposable_yuan"] == "3500.00"
    assert result["income_yuan"] == "5000.00"
    assert result["expense_yuan"] == "1000.00"
    assert result["upcoming_yuan"] == "0.00"
    assert result["safety_buffer_yuan"] == "500.00"
    assert result["source"] == "monthly_report"
    assert result["formula"] == (
        "可支配 = 本月收入 ¥5000.00 - 本月已支出 ¥1000.00"
        " - 生日前订阅扣款 ¥0.00 - 安全垫 ¥500.00 = ¥3500.00"
    )


def test_compute_disposable_zero_tolerant():
    """monthly_report 无流水时返回 "0.00"，不能抛 ValueError。"""
    result = compute_disposable("0.00", "0.00", [{"amount_yuan": "0.00"}])
    assert result["disposable_yuan"] == "0.00"
    assert compute_disposable(None, None, [])["disposable_yuan"] == "0.00"
    assert compute_disposable("", "", [])["disposable_yuan"] == "0.00"


def test_compute_disposable_upcoming_nonzero():
    result = compute_disposable(
        "5000", "1000", [{"amount_yuan": "100.00"}, {"amount_yuan": "0.00"}])
    assert result["upcoming_yuan"] == "100.00"
    assert result["disposable_yuan"] == "3400.00"


def test_compute_disposable_prev_month():
    result = compute_disposable("18500.00", "16990.30", [], source="prev_month")
    assert result["source"] == "prev_month"
    assert result["disposable_yuan"] == "1009.70"
    assert "上月收入 ¥18500.00" in result["formula"]
    assert "本月工资未到账，按上月整月测算" in result["formula"]


def test_compute_disposable_fallback_balance():
    result = compute_disposable(None, None, [{"amount_yuan": "300"}],
                                source="balance", balance_yuan="8000")
    assert result["source"] == "balance"
    assert result["income_yuan"] is None
    assert result["expense_yuan"] is None
    assert result["disposable_yuan"] == "7200.00"
    assert "活期余额 ¥8000.00" in result["formula"]
    assert "已回退活期余额" in result["formula"]


# ------------------------------------------------------------------ 计划分支

def test_build_plan_budget_fits():
    """预算 <= 可支配：不砍订阅，只锁预算 + 提前提醒。"""
    plan = build_plan(
        plan_id="plan-test01", plan_version=1,
        goal="帮我规划林悦的生日,预算3000",
        constraints=[], wants_cut=False,
        facts=_facts(), requested_budget_yuan="3000",
        cut_candidates=filter_cut_candidates(SUBS),
    )
    assert plan["recommended_budget_yuan"] == "3000.00"
    assert plan["shortfall_yuan"] == "0.00"
    assert plan["selected_cut"] is None
    assert plan["blocked"] is False
    gates = _gates(plan)
    assert "confirm_linkage" in gates
    assert "confirm_sub_cancel" not in gates
    assert plan["steps"][0] == {
        "idx": 0, "title": "读取账单、账户与订阅事实(只读)",
        "risk": "READ", "gate": None, "done": True,
    }
    assert [s["risk"] for s in plan["steps"][-2:]] == ["LOW", "LOW"]
    assert "无需取消订阅" in plan["note"]
    # 默认礼物动作：鲜花 300 提前 2 天、蛋糕 200 提前 1 天
    assert plan["actions"] == list(DEFAULT_ACTIONS)
    assert plan["steps"][-2]["title"] == "生日前2天提醒订购鲜花(约 ¥300)"
    assert plan["steps"][-1]["title"] == "生日前1天提醒订购蛋糕(约 ¥200)"


def test_build_plan_budget_exceeds_with_cut():
    """预算不足 + 明确要砍 + 有候选：选金额最大的，推荐额封顶可支配+本期释放。"""
    plan = build_plan(
        plan_id="plan-test02", goal="预算3600,钱不够就砍订阅",
        wants_cut=True, facts=_facts(), requested_budget_yuan="3600",
        cut_candidates=filter_cut_candidates(SUBS),
    )
    assert plan["selected_cut"] is not None
    assert plan["selected_cut"]["merchant"] == "腾讯视频VIP"
    assert plan["selected_cut"]["amount_yuan"] == "30.00"
    assert "取消后本期可释放 ¥30.00" in plan["selected_cut"]["reason"]
    assert plan["recommended_budget_yuan"] == "3530.00"
    assert plan["shortfall_yuan"] == "70.00"
    gates = _gates(plan)
    assert gates.index("confirm_sub_cancel") < gates.index("confirm_linkage")
    assert any("取消「腾讯视频VIP」自动扣费" in s["title"] for s in plan["steps"])
    assert "建议取消「腾讯视频VIP」会员" in plan["note"]
    assert "只取消真实会员订阅(房租/理财/还款不可选)" in plan["constraints"]

    # 预算再高也只按"可支配 + 该候选本期金额"封顶
    high = build_plan(
        goal="预算9000,钱不够就砍订阅", wants_cut=True, facts=_facts(),
        requested_budget_yuan="9000",
        cut_candidates=filter_cut_candidates(SUBS),
    )
    assert high["recommended_budget_yuan"] == "3530.00"
    assert high["shortfall_yuan"] == "5470.00"


def test_build_plan_budget_exceeds_no_cut_intent():
    """用户没要求砍：候选存在也不砍，锁定预算下调为可支配。"""
    plan = build_plan(
        goal="预算9000", wants_cut=False, facts=_facts(),
        requested_budget_yuan="9000",
        cut_candidates=filter_cut_candidates(SUBS),
    )
    assert plan["recommended_budget_yuan"] == "3500.00"
    assert plan["shortfall_yuan"] == "5500.00"
    assert plan["selected_cut"] is None
    assert "confirm_sub_cancel" not in _gates(plan)
    assert "差额 ¥5500.00" in plan["note"]


def test_build_plan_budget_exceeds_without_candidates():
    """用户要求砍但没有候选：不插取消步骤，note 说明差额。"""
    plan = build_plan(
        goal="预算9000,钱不够就砍订阅", wants_cut=True, facts=_facts(),
        requested_budget_yuan="9000", cut_candidates=[],
    )
    assert plan["recommended_budget_yuan"] == "3500.00"
    assert plan["shortfall_yuan"] == "5500.00"
    assert plan["selected_cut"] is None
    assert "confirm_sub_cancel" not in _gates(plan)
    assert "没有可取消的会员订阅" in plan["note"]
    assert "差额 ¥5500.00" in plan["note"]


def test_build_plan_degrade_keeps_subscription():
    """砍单被拒/没找到后的降级：订阅保留，重新确认下调后的预算。"""
    plan = build_plan(
        goal="预算3600,钱不够就砍订阅", wants_cut=True,
        degrade_reason="未确认取消", facts=_facts(),
        requested_budget_yuan="3600",
        cut_candidates=filter_cut_candidates(SUBS),
    )
    assert plan["recommended_budget_yuan"] == "3500.00"
    assert plan["selected_cut"] is None
    assert "confirm_sub_cancel" not in _gates(plan)
    assert plan["note"].startswith("订阅保留（未确认取消）")
    assert "是否继续锁定预算 ¥3500.00" in plan["note"]


def test_build_plan_zero_disposable_blocked():
    """可支配为 0 且无候选：blocked，不生成锁定步骤。"""
    plan = build_plan(
        goal="预算3000", wants_cut=False,
        facts=_facts("0.00", "0.00"), requested_budget_yuan="3000",
        cut_candidates=[],
    )
    assert plan["facts"]["disposable_yuan"] == "0.00"
    assert plan["recommended_budget_yuan"] == "0.00"
    assert plan["blocked"] is True
    assert "confirm_linkage" not in _gates(plan)
    assert "confirm_sub_cancel" not in _gates(plan)
    assert plan["note"] == ("本月可支配为 ¥0.00，无法锁定预算；"
                            "建议先取消订阅或降低预算。")


def test_build_plan_zero_disposable_with_cut_uses_released_amount():
    """可支配为 0 但有可砍候选时，推荐额来自本期释放额（不是 0）。"""
    plan = build_plan(
        goal="预算3000,钱不够就砍订阅", wants_cut=True,
        facts=_facts("0.00", "0.00"), requested_budget_yuan="3000",
        cut_candidates=filter_cut_candidates(SUBS),
    )
    assert plan["selected_cut"]["merchant"] == "腾讯视频VIP"
    assert plan["recommended_budget_yuan"] == "30.00"
    assert plan["blocked"] is False


def test_build_plan_past_date_blocked():
    plan = build_plan(
        goal="规划生日,预算3000", wants_cut=False,
        facts=_facts(days_left=-1), requested_budget_yuan="3000",
        cut_candidates=filter_cut_candidates(SUBS),
    )
    assert plan["blocked"] is True
    assert plan["recommended_budget_yuan"] == "0.00"
    assert "生日日期已过" in plan["note"]
    assert "confirm_linkage" not in _gates(plan)
    # 过去日期不再生成"生日前提醒"
    assert len(plan["steps"]) == 1


def test_build_plan_amount_boundaries():
    """金额边界：相等不砍、超 1 分不砍、1 分钱也能锁定、0 预算 blocked。"""
    facts = _facts()  # 可支配 3500.00
    equal = build_plan(facts=facts, requested_budget_yuan="3500.00",
                       cut_candidates=[])
    assert equal["recommended_budget_yuan"] == "3500.00"
    assert equal["shortfall_yuan"] == "0.00"
    assert equal["blocked"] is False

    one_cent_over = build_plan(facts=facts, requested_budget_yuan="3500.01",
                               cut_candidates=[])
    assert one_cent_over["recommended_budget_yuan"] == "3500.00"
    assert one_cent_over["shortfall_yuan"] == "0.01"
    assert one_cent_over["selected_cut"] is None

    tiny = _facts("1.01", "1.00", buffer="0")
    assert tiny["disposable_yuan"] == "0.01"
    tiny_plan = build_plan(facts=tiny, requested_budget_yuan="0.01",
                           cut_candidates=[])
    assert tiny_plan["recommended_budget_yuan"] == "0.01"
    assert tiny_plan["blocked"] is False
    assert "confirm_linkage" in _gates(tiny_plan)

    zero = build_plan(facts=_facts("0.00", "0.00"),
                      requested_budget_yuan="0.01", cut_candidates=[])
    assert zero["recommended_budget_yuan"] == "0.00"
    assert zero["blocked"] is True

    no_budget = build_plan(facts=facts, requested_budget_yuan="0",
                           cut_candidates=[])
    assert no_budget["recommended_budget_yuan"] == "0.00"
    assert no_budget["blocked"] is True
    assert "confirm_linkage" not in _gates(no_budget)
    assert "预算金额必须大于 0" in no_budget["note"]


def test_build_plan_uses_extracted_actions():
    """抽取值给了 actions 就用抽取值；礼物超预算时 note 明确提示。"""
    actions = [{"what": "手表", "merchant": "京东", "days_before": 3,
                "amount_yuan": "4000"}]
    plan = build_plan(
        goal="规划生日,预算3000,提前3天订4000元的手表",
        facts=_facts(), requested_budget_yuan="3000",
        actions=actions, cut_candidates=[],
    )
    assert plan["actions"] == [{
        "what": "手表", "merchant": "京东",
        "days_before": 3, "amount_yuan": "4000",
    }]
    assert "生日前3天提醒订购手表(约 ¥4000)" in [s["title"] for s in plan["steps"]]
    assert "超过锁定预算 ¥3000.00" in plan["note"]
    assert "¥4000.00" in plan["note"]

    # 非法动作全部丢弃 → 回退默认剧本（且默认常量不被改）
    fallback = build_plan(facts=_facts(), requested_budget_yuan="3000",
                          actions=[{"what": "", "amount_yuan": "100"}],
                          cut_candidates=[])
    assert fallback["actions"] == list(DEFAULT_ACTIONS)
    fallback["actions"][0]["what"] = "被改坏"
    assert DEFAULT_ACTIONS[0]["what"] == "鲜花"


def test_build_plan_plan_id():
    """未传 plan_id 时用 uuid 生成；传了则原样保留。"""
    first = build_plan(facts=_facts(), requested_budget_yuan="1000",
                       cut_candidates=[])
    second = build_plan(facts=_facts(), requested_budget_yuan="1000",
                        cut_candidates=[])
    assert first["plan_id"].startswith("plan-")
    assert len(first["plan_id"]) == len("plan-") + 10
    assert first["plan_id"] != second["plan_id"]
    assert build_plan(plan_id="plan-x", facts=_facts(),
                      requested_budget_yuan="1000",
                      cut_candidates=[])["plan_id"] == "plan-x"


def test_build_plan_note_mentions_upcoming_precharge():
    facts = _facts(upcoming=[{"amount_yuan": "120.00"}])
    plan = build_plan(facts=facts, requested_budget_yuan="1000",
                      cut_candidates=[])
    assert plan["facts"]["upcoming_yuan"] == "120.00"
    assert "已扣除生日前预计订阅扣款 ¥120.00" in plan["note"]


# ------------------------------------------------------------------ 计划卡视图

def test_plan_card_view_fields():
    draft = build_plan(
        plan_id="plan-card01", plan_version=2, goal="预算3600,钱不够就砍订阅",
        wants_cut=True, facts=_facts(), requested_budget_yuan="3600",
        cut_candidates=filter_cut_candidates(SUBS),
    )
    view = plan_card_view(draft)
    assert set(view) == {
        "plan_id", "plan_version", "goal", "constraints", "facts",
        "requested_budget_yuan", "recommended_budget_yuan", "shortfall_yuan",
        "cut_candidates", "selected_cut", "steps", "note",
    }
    assert view["plan_id"] == "plan-card01"
    assert view["plan_version"] == 2
    assert set(view["facts"]) == set(PLAN_FACT_KEYS)
    assert "candidates" not in view["facts"]
    assert "event_found" not in view["facts"]
    assert view["selected_cut"]["merchant"] == "腾讯视频VIP"
    # 视图是拷贝：改卡片不污染计划底稿
    view["facts"]["disposable_yuan"] = "0.00"
    view["cut_candidates"].clear()
    assert draft["facts"]["disposable_yuan"] == "3500.00"
    assert draft["cut_candidates"]


# ------------------------------------------------------------------ 用户意图

def test_wants_cut_negation():
    assert wants_cut("钱不够就砍订阅") is True
    assert wants_cut("特别想砍订阅") is True
    assert wants_cut("不砍订阅") is False
    assert wants_cut("那就不砍订阅了") is False
    assert wants_cut("不用砍订阅,帮我规划生日,预算8000") is False
    assert wants_cut("别取消订阅") is False
    assert wants_cut("不是钱不够") is False
    assert wants_cut("千万不要取消订阅") is False
    assert wants_cut("帮我规划生日") is False
    assert wants_cut("") is False


def test_constraints_of_rent():
    assert constraints_of("帮我规划生日,别动房租") == ["不动房租"]
    assert constraints_of("房租不能动,预算3000") == ["不动房租"]
    assert constraints_of("帮我规划生日,预算3000") == []


# ------------------------------------------------------------------ 预算编辑

def test_parse_budget_edit():
    assert parse_budget_edit("预算改成800") == "800.00"
    assert parse_budget_edit("预算调整为 800") == "800.00"
    assert parse_budget_edit("金额降到800") == "800.00"
    assert parse_budget_edit("预算改成800元") == "800.00"
    assert parse_budget_edit("800") == "800.00"
    assert parse_budget_edit("800元") == "800.00"
    assert parse_budget_edit("800.5") == "800.50"
    assert parse_budget_edit("99999") == "99999.00"
    # 6 位支付密码 / 前导零 / 非正数一律不当预算编辑
    assert parse_budget_edit("888888") is None
    assert parse_budget_edit("012345") is None
    assert parse_budget_edit("123456") is None
    assert parse_budget_edit("0") is None
    assert parse_budget_edit("0.00") is None
    # 显式编辑的上限保护；无动词的普通对话不误判
    assert parse_budget_edit("预算改成10000001") is None
    assert parse_budget_edit("确认") is None
    assert parse_budget_edit("取消") is None
    assert parse_budget_edit("") is None


def test_make_plan_id_deterministic():
    first = make_plan_id("thread-1", "帮我规划生日,预算3000")
    assert first == make_plan_id("thread-1", "帮我规划生日,预算3000")
    assert first.startswith("plan-")
    assert len(first) == len("plan-") + 10
    assert first != make_plan_id("thread-2", "帮我规划生日,预算3000")
    assert first != make_plan_id("thread-1", "帮我规划生日,预算800")
