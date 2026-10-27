"""客户洞察只读 API(M5):GET /api/insights + GET /api/insights/eligibility。

独立 APIRouter,由 agent/api.py 在 AGENT_INSIGHTS_ENABLED 开关(默认关闭)
下 include——关闭时路由不存在,现有行为与测试完全不变(方案第四节第 4 条)。

鉴权与 api.py 同一套:x-bank-token 头 → auth.resolve_token → 登录用户;
洞察属个人数据,只允许查看本人(user_id 查询参数给了别人 → 403)。

永不 500(仅洞察总览):engine 已把 LLM 全部失败路径收敛为规则降级;
本层对 GET /api/insights 再兜一层异常 → 200 + error 字段的降级结构
(洞察是辅助链路,坏了不拖垮前端)。资格端点只把「产品/用户不存在」
映射为 404,其余数据库故障仍按 500 上报——资格是合规结论,不能伪装成
一个看起来正常的降级值。

SQL 只读:全部走 bank_core.insights 与 agent.insights_engine 的 SELECT。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from agent import auth as auth_mod
from agent.insights_engine import build_insights_view
from bank_core.insights import check_eligibility

router = APIRouter(prefix="/api/insights", tags=["insights"])


def _me_or_401(request: Request, x_bank_token: str | None):
    """x-bank-token → (me, None) 或 (None, 401 JSONResponse)。

    登录判定与 api.py 各端点同源(auth.resolve_token):token 缺失/过期/
    伪造一律 401,不降级为观光态——洞察是个人数据,没有「游客版」。
    """
    me = auth_mod.resolve_token(request.app.state.auth_conn, x_bank_token)
    if not me:
        return None, JSONResponse(
            status_code=401, content={"error": "请先登录后查看客户洞察"})
    return me, None


@router.get("")
async def get_insights(request: Request, user_id: int | None = None,
                       x_bank_token: str | None = Header(default=None)):
    """客户洞察总览:兴趣分布 + 信号明细 + 资产/持仓 + 候选 + 推荐。

    user_id 查询参数可选且仅允许等于登录本人(方案 M5 的 `?user_id=...`
    形态;跨用户查询一律 403,不泄露他人画像)。
    数据连接复用 app.state.auth_conn(bank.db,Row 工厂,只读查询)。
    LLM 注入口 app.state.insights_llm(测试 stub);未设置走 get_llm(),
    失败自动降级为规则推荐(recommendation_source='rules')。
    """
    me, denied = _me_or_401(request, x_bank_token)
    if denied:
        return denied
    uid = me["user_id"]
    if user_id is not None and user_id != uid:
        return JSONResponse(
            status_code=403, content={"error": "只能查看本人的客户洞察"})
    conn = request.app.state.auth_conn
    llm = getattr(request.app.state, "insights_llm", None)
    try:
        view = await build_insights_view(conn, uid, llm=llm)
    except Exception as exc:  # noqa: BLE001 —— 洞察失败不 500,返回可渲染的降级结构
        # 降级结构与成功响应字段一一对应(含 generated_at),前端无需为异常
        # 分支写第二套渲染逻辑;error 字段是唯一的附加键。
        return {
            "user_id": uid, "generated_at": datetime.now().isoformat(),
            "error": f"洞察暂不可用（{type(exc).__name__}）",
            "interest": {"by_product": {}, "by_type": {}, "confidence": "low",
                         "cold_start": True, "fallback": []},
            "signals": {}, "accounts": {"total_balance_yuan": "0.00",
                                        "max_single_account_yuan": "0.00",
                                        "accounts": []},
            "holdings": [], "candidates": [], "recommendations": [],
            "recommendation_source": "none",
            "recommendation_note": f"生成失败：{type(exc).__name__}",
        }
    return view


@router.get("/eligibility")
async def get_eligibility(request: Request, product_code: str,
                          x_bank_token: str | None = Header(default=None)):
    """单产品资格校验(M2):年龄/风险/起购三道闸门 + 可审计拒绝理由。

    product_code 必填;产品或用户不存在是参数错误 → 404(与 api.py 的
    订单不存在 404 口径一致),不是用户资格状态,不与 eligible=False 混淆。
    """
    me, denied = _me_or_401(request, x_bank_token)
    if denied:
        return denied
    try:
        return check_eligibility(request.app.state.auth_conn,
                                 me["user_id"], product_code.strip())
    except ValueError as exc:  # 产品/用户不存在:参数错误而非资格结论
        return JSONResponse(status_code=404, content={"error": str(exc)})
