"""演示网关：把假银行包成 REST API + 托管试用界面。

  python -m bank_core.web_api            # http://127.0.0.1:8788

这不是最终产品形态——最终形态里这一层的"点击操作"会换成 LangGraph
编排的对话 Agent（见 docs 研发计划 M1），本层保留为：
  1) 假银行的随身"网银"试用页
  2) 未来正式前端的兜底/演示后端
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .analysis import AnalysisService
from .db import init_db
from .events import EventService
from .ledger import LedgerError, LedgerService
from .wealth import WealthError, WealthService

app = FastAPI(title="AI Banking 演示银行", version="0.1.0")

_conn = init_db()
_ledger = LedgerService(_conn, 1)
_analysis = AnalysisService(_conn, 1)
_wealth = WealthService(_conn, 1)
_events = EventService(_conn, 1)


class TransferIn(BaseModel):
    from_account_id: int = 1
    to_contact_id: int | None = None
    to_name: str = ""
    amount_yuan: str
    memo: str = ""
    scheduled_at: str | None = None


class OrderIn(BaseModel):
    order_id: int


class CardStatusIn(BaseModel):
    card_id: int
    status: str


class CardLimitIn(BaseModel):
    card_id: int
    daily_limit_yuan: str | None = None
    per_tx_limit_yuan: str | None = None


class SubscribeIn(BaseModel):
    product_id: int
    amount_yuan: str
    from_account_id: int = 1
    confirmed: bool = False


class RedeemIn(BaseModel):
    holding_id: int
    confirmed: bool = False


class SubCancelIn(BaseModel):
    subscription_id: int


class EventIn(BaseModel):
    event_type: str
    title: str
    event_date: str
    repeat_yearly: bool = False
    note: str = ""


def _err(exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


# ---------------------------------------------------------------- 账户/流水

@app.get("/api/accounts")
def accounts():
    return _ledger.list_accounts()


@app.get("/api/contacts")
def contacts():
    # 走服务层：同 list_accounts 一样过 audit_log 留痕（原先此处裸 SQL 绕过审计）
    return _ledger.list_contacts()


@app.get("/api/transactions")
def transactions(limit: int = 20, category: str | None = None,
                 counterparty: str | None = None):
    return _ledger.get_transactions(limit=limit, category=category,
                                    counterparty=counterparty)


@app.get("/api/report/{month}")
def report(month: str):
    try:
        return _analysis.monthly_report(month)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ---------------------------------------------------------------- 转账

@app.post("/api/transfer/order")
def transfer_order(body: TransferIn):
    try:
        return _ledger.create_transfer_order(
            body.from_account_id,
            _to_cents(body.amount_yuan),
            body.to_contact_id, body.to_name, "", body.memo, body.scheduled_at)
    except (LedgerError, ValueError) as e:
        return _err(e)


@app.post("/api/transfer/confirm")
def transfer_confirm(body: OrderIn):
    try:
        return _ledger.confirm_transfer_order(body.order_id)
    except LedgerError as e:
        return _err(e)


@app.post("/api/transfer/cancel")
def transfer_cancel(body: OrderIn):
    try:
        return _ledger.cancel_transfer_order(body.order_id)
    except LedgerError as e:
        return _err(e)


def _to_cents(yuan: str) -> int:
    from .money import yuan_to_cents
    return yuan_to_cents(yuan)


# ---------------------------------------------------------------- 订阅/异常

@app.get("/api/subscriptions")
def subscriptions(refresh: bool = True):
    if refresh:
        recs = {r["merchant"]: r for r in _analysis.detect_subscriptions()}
    else:
        recs = {}
    subs = _analysis.list_subscriptions()
    for s in subs:  # 涨价/降价提示由挖掘结果带出
        s["price_change"] = recs.get(s["merchant"], {}).get("price_change", "")
    return subs


@app.post("/api/subscriptions/cancel")
def subscription_cancel(body: SubCancelIn):
    try:
        return _analysis.cancel_subscription(body.subscription_id)
    except ValueError as e:
        return _err(e)


@app.get("/api/anomalies")
def anomalies(days: int = 90):
    return _analysis.detect_anomalies(days)


# ---------------------------------------------------------------- 理财

@app.get("/api/products")
def products():
    return _wealth.list_products()


@app.get("/api/holdings")
def holdings():
    return _wealth.get_holdings()


@app.get("/api/risk")
def risk():
    return _wealth.get_risk_profile() or {"level": "未测评"}


@app.post("/api/wealth/subscribe")
def wealth_subscribe(body: SubscribeIn):
    try:
        return _wealth.subscribe_product(body.product_id,
                                         _to_cents(body.amount_yuan),
                                         body.from_account_id, body.confirmed)
    except (WealthError, ValueError) as e:
        return _err(e)


@app.post("/api/wealth/redeem")
def wealth_redeem(body: RedeemIn):
    try:
        return _wealth.redeem_product(body.holding_id, body.confirmed)
    except WealthError as e:
        return _err(e)


# ---------------------------------------------------------------- 卡片

@app.get("/api/cards")
def cards():
    return _ledger.list_cards()


@app.post("/api/cards/status")
def card_status(body: CardStatusIn):
    try:
        return _ledger.set_card_status(body.card_id, body.status)
    except LedgerError as e:
        return _err(e)


@app.post("/api/cards/limits")
def card_limits(body: CardLimitIn):
    try:
        return _ledger.set_card_limits(
            body.card_id,
            _to_cents(body.daily_limit_yuan) if body.daily_limit_yuan else None,
            _to_cents(body.per_tx_limit_yuan) if body.per_tx_limit_yuan else None)
    except (LedgerError, ValueError) as e:
        return _err(e)


# ---------------------------------------------------------------- 事件/联动

@app.get("/api/events")
def events(upcoming_days: int | None = None):
    return _events.list_events(upcoming_days)


@app.post("/api/events")
def add_event(body: EventIn):
    try:
        return _events.add_event(body.event_type, body.title, body.event_date,
                                 body.repeat_yearly, body.note)
    except ValueError as e:
        return _err(e)


@app.get("/api/linkage/{event_id}")
def linkage(event_id: int):
    try:
        return _events.suggest_linkage(event_id)
    except ValueError as e:
        return _err(e)


@app.get("/api/tasks")
def tasks():
    return _events.list_scheduled_tasks()


@app.post("/api/tasks/run")
def tasks_run():
    return _events.run_due_tasks()


# ---------------------------------------------------------------- 静态试用页

_DEMO_DIR = Path(__file__).resolve().parent.parent / "demo"
app.mount("/", StaticFiles(directory=_DEMO_DIR, html=True), name="demo")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8788)
