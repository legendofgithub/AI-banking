"""客户洞察推荐引擎(M4):四段注入 + Pydantic 结构化输出 + 规则降级。

设计要点两件事:
1. prompt 四段式结构(参考 prompts.py:2-40):客户兴趣分布 + 当前资产负债 +
   已持有产品 + 可推荐候选 → 让模型产出至多 3 条「用户可能想问的问题」;
   文案按本项目业务口径中文重写,不搬越南语原文。
2. schema 强约束输出的**模式**(本项目此前靠 json_from_content 容错解析):
   先要求模型输出纯 JSON,再用 Pydantic 模型校验形状与边界,并强制
   product_code 只能取自候选集(防幻觉产品)。

稳定性铁律(方案 M4「保证永不 500」):
- LLM 解析失败 → 重试 1 次 → 仍失败 → 降级为纯规则结果
  (bank_core.insights.recommend_candidates + 确定性模板话术);
- 无 API Key / 无网络 / 调用超时 / 输出为空 → 同样降级,绝不向上抛异常。

SQL 只读:本模块只发 SELECT(持仓/账户快照),不写任何表。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from agent.llm import get_llm
from bank_core.insights import compute_interest_scores, recommend_candidates
from bank_core.money import cents_to_yuan

_log = logging.getLogger("agent.insights")


def _parse_timeout(raw: str | None, default: float = 20.0) -> float:
    """环境变量 → LLM 超时秒数;缺失/非法/非正数一律回落默认值。

    为什么不能直接 float(os.environ[...]):本模块在 agent/api.py 顶部被导入,
    一个手滑的 AGENT_INSIGHTS_LLM_TIMEOUT=20s 就会在 import 期抛 ValueError,
    连 AGENT_INSIGHTS_ENABLED=0 的主应用一起带崩——违反「开关关闭时现有行为
    完全不变」。配置错误只该让洞察链路退回默认超时/规则降级。
    """
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# 洞察是辅助链路,超时比主链路(api.LLM_TIMEOUT_S=60)更紧:宁可降级规则
# 结果,也不让一个旁路接口拖住前端渲染。engine 不 import agent.api
# (那会形成 api→insights_api→engine→api 的环),常量自带。
LLM_TIMEOUT_S = _parse_timeout(os.environ.get("AGENT_INSIGHTS_LLM_TIMEOUT"))

MAX_RECOMMENDATIONS = 3  # 与方案 M4「3 条用户可能想问的问题」对齐

# 产品类型 → 展示词(话术模板用;与 bank_core.insights.PRODUCT_TYPES 同键)
_TYPE_LABELS: dict[str, str] = {
    "money_fund": "货币基金",
    "deposit": "定期存款",
    "bond": "债券基金",
    "mixed": "混合基金",
    "gold": "黄金积存",
}


# ---------------------------------------------------------------- Pydantic 契约
class RecommendationItem(BaseModel):
    """单条推荐:指向候选产品 + 一句理由 + 一个用户可能想问的问题。"""

    product_code: str = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=200)
    suggested_question: str = Field(min_length=1, max_length=120)


class RecommendationOut(BaseModel):
    """LLM 输出的整体形状(items 上限即「最多 3 条」约束的 schema 落点)。"""

    items: list[RecommendationItem] = Field(default_factory=list,
                                            max_length=MAX_RECOMMENDATIONS)


# ---------------------------------------------------------------- 只读快照
def _accounts_snapshot(conn: sqlite3.Connection, user_id: int) -> dict:
    """用户全部账户明细 + 两种口径的余额汇总。

    total_balance_yuan: 全部账户合计，仅用于展示资产规模；
    max_single_account_yuan: 单张账户最大余额——与准入/申购闸门同口径
    （wealth.py:149-154 只校验 from_account_id 单张账户，不能合并多账户）。
    """
    rows = conn.execute(
        "SELECT name, type, balance_cents FROM accounts "
        "WHERE user_id=? ORDER BY id", (user_id,)).fetchall()
    accounts = [{"name": r["name"], "type": r["type"],
                 "balance_yuan": cents_to_yuan(int(r["balance_cents"]))}
                for r in rows]
    total = sum(int(r["balance_cents"]) for r in rows)
    max_single = max((int(r["balance_cents"]) for r in rows), default=0)
    return {"total_balance_yuan": cents_to_yuan(total),
            "max_single_account_yuan": cents_to_yuan(max_single),
            "accounts": accounts}


def _holdings_snapshot(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    """当前持仓(status='holding',与 M3 剔除口径一致)。"""
    rows = conn.execute(
        """SELECT p.code, p.name, p.p_type, p.risk_level, h.principal_cents,
                  h.est_value_cents, h.subscribed_at
           FROM wealth_holdings h
           JOIN wealth_products p ON p.id = h.product_id
           WHERE h.user_id=? AND h.status='holding'
           ORDER BY h.subscribed_at, p.code""", (user_id,)).fetchall()
    return [{
        "code": r["code"], "name": r["name"],
        "p_type": r["p_type"], "risk_level": f"R{int(r['risk_level'])}",
        "principal_yuan": cents_to_yuan(int(r["principal_cents"])),
        "est_value_yuan": cents_to_yuan(int(r["est_value_cents"])),
        "subscribed_at": r["subscribed_at"],
    } for r in rows]


# ---------------------------------------------------------------- 规则降级
def _rule_recommendations(candidates: list[dict]) -> list[dict]:
    """纯规则结果:M3 候选(已按 兴趣×适当性 排好序)+ 确定性模板话术。

    不调 LLM、可离线执行——这是 LLM 任何形态失败后的最终兜底。
    reason 直接引用候选自带的 reasons[](兴趣人话/适当性依据/起购对比,
    全部来自 bank_core.insights 的确定性拼接,可审计)。
    """
    out: list[dict] = []
    for c in candidates[:MAX_RECOMMENDATIONS]:
        label = _TYPE_LABELS.get(c["p_type"], c["p_type"])
        out.append({
            "product_code": c["code"],
            "reason": "；".join(c["reasons"]),
            "suggested_question": (
                f"我还没买过{label}，{c['name']}"
                f"（{c['risk_level']}，起购 {c['min_subscribe_yuan']} 元）"
                "适合我吗？"),
        })
    return out


# ---------------------------------------------------------------- prompt 构造
_SYSTEM_PROMPT = (
    "你是银行零售网的客户洞察助手。根据客户的兴趣分布、资产与持仓、"
    "可推荐候选产品，生成至多 "
    f"{MAX_RECOMMENDATIONS} 条个性化推荐。要求：\n"
    "1. product_code 只能从「可推荐候选」列表中选择，不得编造列表外的产品；\n"
    "2. reason 用一句中文说明推荐依据（引用兴趣信号与适当性结论），不超过 120 字；\n"
    "3. suggested_question 是这位客户可能想问的问题（以客户口吻），不超过 60 字；\n"
    "4. 只输出纯 JSON（不要 markdown 代码块、不要解释文字），格式：\n"
    '   {"items": [{"product_code": "...", "reason": "...", '
    '"suggested_question": "..."}]}\n'
    "5. 推荐按匹配度从高到低排列；候选不足时全量输出。"
)


def _fmt_pct(prob: float) -> str:
    return f"{prob:.1%}"


def _build_user_prompt(dist: dict, candidates: list[dict],
                       holdings: list[dict], accounts: dict) -> str:
    """四段式注入(结构迁自参考项目 prompts.py,文案按本项目重写)。"""
    by_type = "、".join(
        f"{_TYPE_LABELS.get(t, t)} {_fmt_pct(p)}"
        for t, p in sorted(dist["by_type"].items(), key=lambda kv: -kv[1]))
    top_products = sorted(dist["by_product"].items(), key=lambda kv: -kv[1])[:5]
    by_product = "、".join(f"{c} {_fmt_pct(p)}" for c, p in top_products)

    lines = [
        f"【客户兴趣分布】（置信度 {dist['confidence']}"
        f"{'，冷启动（行为信号不足）' if dist['cold_start'] else ''}）",
        f"按类型：{by_type}",
        f"按产品（前 5）：{by_product}",
        "",
        "【当前资产】",
        f"账户合计余额：{accounts['total_balance_yuan']} 元"
        f"（单张账户最高 {accounts['max_single_account_yuan']} 元；"
        "起购资格按单账户判定，不能合并多账户）",
    ]
    for a in accounts["accounts"]:
        lines.append(f"- {a['name']}（{a['type']}）：{a['balance_yuan']} 元")

    lines.append("")
    if holdings:
        lines.append("【已持有产品】")
        for h in holdings:
            lines.append(
                f"- {h['code']} {h['name']}（{h['risk_level']}）"
                f"本金 {h['principal_yuan']} 元、估值 {h['est_value_yuan']} 元")
    else:
        lines.append("【已持有产品】暂无理财持仓")

    lines += ["", "【可推荐候选】（未持有且已通过 年龄/风险/起购 三道准入，按匹配度降序）"]
    for i, c in enumerate(candidates, start=1):
        lines.append(
            f"{i}. {c['code']} {c['name']} | "
            f"{_TYPE_LABELS.get(c['p_type'], c['p_type'])} | {c['risk_level']} | "
            f"起购 {c['min_subscribe_yuan']} 元 | 兴趣概率 {_fmt_pct(c['interest_prob'])}")
        for r in c["reasons"]:
            lines.append(f"   依据：{r}")
    lines.append("")
    lines.append("请按系统要求输出纯 JSON。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 解析与校验
def _loads_lenient(text: str) -> Any:
    """容错 JSON 加载:先整体,剥代码围栏,再截取首尾配对括号(与
    agent.graph 的 json_from_content 同思路,但本模块自持,不 import graph)。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z0-9_-]*\s*|\s*```$", "", s).strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = s.find(opener), s.rfind(closer)
        if 0 <= start < end:
            try:
                return json.loads(s[start:end + 1])
            except ValueError:
                continue
    raise ValueError("无法从模型输出中解析出 JSON")


def _extract_items(raw: Any, allowed_codes: set[str]) -> list[dict]:
    """模型输出 → Pydantic 校验 → 候选白名单过滤。

    任何一步不符合预期都抛异常(由调用方计入重试/降级),包括:
    形状不对、字段缺失/超长、product_code 不在候选集、items 为空。
    空输出按失败处理:此时候选集非空,规则降级兜得住。
    """
    text = raw if isinstance(raw, str) else str(raw or "")
    data = _loads_lenient(text)
    if isinstance(data, dict):
        data = data.get("items")
    if not isinstance(data, list):
        raise ValueError("输出不是 {\"items\": [...]} 形状的 JSON")
    validated = RecommendationOut(items=data)  # ValidationError 冒泡给上层
    items: list[dict] = []
    seen: set[str] = set()
    for it in validated.items:
        if it.product_code not in allowed_codes:
            raise ValueError(f"product_code 不在候选集: {it.product_code}")
        if it.product_code in seen:
            continue  # 去重:同一产品只保留首条(排序靠前的更匹配)
        seen.add(it.product_code)
        items.append(it.model_dump())
    if not items:
        raise ValueError("items 为空")
    return items[:MAX_RECOMMENDATIONS]


def _default_llm() -> Any:
    """解析生产 LLM:无 API Key 等任何失败都返回 None(规则降级,不抛)。"""
    try:
        return get_llm()
    except Exception as exc:  # noqa: BLE001 —— 洞察链路绝不因 LLM 配置缺失而 500
        _log.info("洞察推荐:LLM 不可用(%s),走规则降级", type(exc).__name__)
        return None


# ---------------------------------------------------------------- 主流程
async def _recommend(dist: dict, candidates: list[dict], holdings: list[dict],
                     accounts: dict, llm: Any) -> dict:
    """LLM 生成 + 两段重试 + 规则降级。返回 {items, source, note}。

    source: 'llm'(模型产出) | 'rules'(模板降级) | 'none'(无候选,不必推荐)。
    本函数自身不抛异常——所有失败路径都收敛为规则结果。
    """
    if not candidates:
        return {"items": [], "source": "none",
                "note": "暂无可推荐候选（已全部持有或均未达准入门槛）"}
    fallback = _rule_recommendations(candidates)
    model = llm if llm is not None else _default_llm()
    if model is None:
        return {"items": fallback, "source": "rules",
                "note": "LLM 未配置，已降级为规则推荐"}

    allowed = {c["code"] for c in candidates}
    prompt = _build_user_prompt(dist, candidates, holdings, accounts)
    last_error = "未知错误"
    for _attempt in (1, 2):  # 首次 + 重试 1 次(方案 M4 固定语义)
        try:
            resp = await asyncio.wait_for(
                model.ainvoke([SystemMessage(content=_SYSTEM_PROMPT),
                               HumanMessage(content=prompt)]),
                timeout=LLM_TIMEOUT_S)
            items = _extract_items(getattr(resp, "content", ""), allowed)
            return {"items": items, "source": "llm", "note": ""}
        except Exception as exc:  # noqa: BLE001 —— 超时/网络/解析失败一律重试或降级
            last_error = f"{type(exc).__name__}: {exc}"
            # 只告警不中断:重试/降级是既定策略,日志留给运维定位模型侧问题
            _log.warning("洞察推荐 LLM 第 %d/2 次调用失败:%s", _attempt,
                         last_error[:200])
    _log.warning("洞察推荐已降级为规则结果:%s", last_error[:200])
    return {"items": fallback, "source": "rules",
            "note": f"LLM 输出解析失败，已降级为规则推荐（{last_error[:160]}）"}


async def build_insights_view(conn: sqlite3.Connection, user_id: int,
                              llm: Any = None, top_n: int = 3,
                              now: datetime | None = None) -> dict:
    """GET /api/insights 的聚合视图:兴趣 + 信号 + 资产 + 持仓 + 候选 + 推荐。

    查询只做一次,LLM/降级复用同一份中间结果(不重复跑 M1-M3)。
    top_n 同时约束候选条数与推荐上限(默认 3,即方案 M4 的「3 条」):
    candidates 即 LLM 白名单——候选与推荐基数同一批,不给模型列表外
    的选择空间。
    """
    now = now or datetime.now()
    dist = compute_interest_scores(conn, user_id, now=now)
    candidates = recommend_candidates(conn, user_id, top_n=top_n, now=now)
    holdings = _holdings_snapshot(conn, user_id)
    accounts = _accounts_snapshot(conn, user_id)
    rec = await _recommend(dist, candidates, holdings, accounts, llm)
    return {
        "user_id": user_id,
        "generated_at": now.isoformat(),
        "interest": {k: dist[k] for k in
                     ("by_product", "by_type", "confidence", "cold_start",
                      "fallback")},
        "signals": dist["signals"],
        "accounts": accounts,
        "holdings": holdings,
        "candidates": candidates,
        "recommendations": rec["items"],
        "recommendation_source": rec["source"],
        "recommendation_note": rec["note"],
    }


async def generate_recommendations(conn: sqlite3.Connection, user_id: int,
                                   llm: Any = None, top_n: int = 3,
                                   now: datetime | None = None) -> dict:
    """M4 单点入口:聚合视图的推荐子集,返回 {items, source, note}。

    与 build_insights_view 共用同一实现(后者是它的超集),避免「候选计算
    口径」两处维护;留给将来聊天 skill 直接取推荐列表。
    llm: 注入的聊天模型(测试 stub);None 则按 agent.llm.get_llm() 解析,
    解析失败自动走规则降级。
    """
    view = await build_insights_view(conn, user_id, llm=llm, top_n=top_n,
                                     now=now)
    return {"items": view["recommendations"],
            "source": view["recommendation_source"],
            "note": view["recommendation_note"]}


__all__ = [
    "LLM_TIMEOUT_S",
    "MAX_RECOMMENDATIONS",
    "RecommendationItem",
    "RecommendationOut",
    "build_insights_view",
    "compute_interest_scores",
    "generate_recommendations",
    "recommend_candidates",
]
