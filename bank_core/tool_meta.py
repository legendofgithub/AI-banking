"""银行工具「身份证」注册表：45 个 MCP 工具的机读风险元数据（纯数据，零依赖）。

为什么需要这一层
----------------
mcp_server.py 里每个工具的 docstring 都带 [READ]/[LOW]/[MED]/[HIGH·动钱] 标签，
但那只是**给模型和人看的文字**，程序没法据此做判断。这里把同样的信息升级成
**结构化字段**（借鉴 Claude Code 的 Tool 契约：isReadOnly / isDestructive /
isConcurrencySafe，fail-closed——不声明就当危险处理），供三层消费：

1. **测试层对账**（tests/test_tool_meta.py）：注册表必须与 FastMCP 实际注册的
   工具**逐一对账**（多一个少一个都红）、与每个工具 docstring 的风险标签**一致**
   （改了 docstring 忘了改注册表、或反之，立即漂移可见）；
2. **编排层白名单视图**（agent.bank.BankTools.scoped）：按 allow 放行工具，
   分析类技能物理摸不到动钱工具；
3. **未来的 LLM 挂载点**：真要给模型注入工具 schema 时，按 scenarios /
   read_only 过滤即可，数据已就位。

字段口径（与 docstring 标签一一对应）
------------------------------------
- ``risk``: READ（只读查询，可自动放行）/ LOW（写用户资料不动钱）/
  MED（需确认的状态变更）/ HIGH（资金/两步/动钱链路，必须走闸门）。
  注意 risk 是「确认等级」，不是「是否只读」：detect_subscriptions 标 [READ]
  （不要求确认）但会入库写入，read_only=False。
- ``read_only``: 严格口径——本次调用**不产生任何状态写入**才算 True。
- ``money``: 直接动钱（真实扣款/划转）。建单类（create_transfer_order /
  create_linkage_plan）只创建待确认单，**不算**动钱。
- ``two_step``: 两步确认类（先建单/待确认，再 confirmed/confirm 执行）。
- ``destructive``: 不可逆操作（set_card_status 的挂失分支）。
- ``sensitive``: 读但敏感（verify_pay_password，审计必须脱敏）。
- ``concurrency_safe``: 可与其他调用并发。**默认 False**（fail-closed），
  ``read_only`` 也**默认 False**——新工具忘声明 = 按危险处理。
- ``scenarios``: 场景标签（common/transfer/billing/subscription/wealth/card/
  events/linkage），供场景化挂载过滤。

维护铁律：改 mcp_server.py 的工具集时**必须同步本注册表**，否则全量测试红
（对账在 tests/test_tool_meta.py）。导入时自检不变量，数据自相矛盾直接炸。
"""

from __future__ import annotations

from dataclasses import dataclass

# ------------------------------------------------------------------ 口径常量

RISKS: tuple[str, ...] = ("READ", "LOW", "MED", "HIGH")

SCENARIOS: frozenset[str] = frozenset({
    "common",        # 通用只读/基础能力（各场景都会用）
    "transfer",      # 转账 / AA 收款
    "billing",       # 账单分析 / 异常检测
    "subscription",  # 订阅 / 代扣
    "wealth",        # 理财
    "card",          # 卡片管理
    "events",        # 事件 / 提醒 / 定时任务
    "linkage",       # 跨场景联动计划
})


@dataclass(frozen=True)
class ToolMeta:
    """一个工具的身份证（字段口径见模块 docstring）。"""

    name: str
    risk: str
    read_only: bool = False          # fail-closed：不声明 = 会写入
    money: bool = False              # fail-closed：不声明 = 可能不动钱，但按危险对待
    destructive: bool = False
    two_step: bool = False
    sensitive: bool = False
    concurrency_safe: bool = False   # fail-closed：不声明 = 不可并发
    scenarios: tuple[str, ...] = ()


def _t(name: str, risk: str, *, scenarios: tuple[str, ...], **kw: bool) -> tuple[str, ToolMeta]:
    return name, ToolMeta(name, risk, scenarios=scenarios, **kw)


# ------------------------------------------------------------------ 注册表
# 与 bank_core/mcp_server.py 的 @mcp.tool 一一对应（含顺序），漂移由测试对账兜住。

REGISTRY: dict[str, ToolMeta] = dict([
    # —— 1. 账户与流水 ——
    _t("get_accounts", "READ", read_only=True, scenarios=("common",)),
    _t("get_transactions", "READ", read_only=True, scenarios=("common", "billing")),

    # —— 2. 智能转账 / AA ——
    _t("resolve_contact", "READ", read_only=True, scenarios=("transfer",)),
    _t("add_contact", "LOW", scenarios=("transfer",)),
    _t("policy_check", "READ", read_only=True, scenarios=("transfer",)),
    _t("create_transfer_order", "HIGH", two_step=True, scenarios=("transfer",)),
    _t("confirm_transfer_order", "HIGH", money=True, scenarios=("transfer",)),
    _t("cancel_transfer_order", "MED", scenarios=("transfer",)),
    _t("get_policy", "READ", read_only=True, scenarios=("transfer", "common")),
    _t("verify_pay_password", "READ", read_only=True, sensitive=True,
       scenarios=("common",)),
    _t("create_split_bill", "MED", scenarios=("transfer",)),
    _t("get_split_bill", "READ", read_only=True, scenarios=("transfer",)),
    _t("settle_split_bill_item", "LOW", scenarios=("transfer",)),

    # —— 3. 账单分析 ——
    _t("category_summary", "READ", read_only=True, scenarios=("billing",)),
    _t("top_merchants", "READ", read_only=True, scenarios=("billing",)),
    _t("monthly_report", "READ", read_only=True, scenarios=("billing", "common")),
    # 年度报告(2026-10-26):全年汇总 + 逐月走势,纯只读
    _t("annual_report", "READ", read_only=True, scenarios=("billing", "common")),
    _t("detect_anomalies", "READ", read_only=True, scenarios=("billing",)),

    # —— 4. 订阅 / 代扣 ——
    # detect_subscriptions 标 [READ]（不要求确认）但会把挖掘结果入库 → read_only=False
    _t("detect_subscriptions", "READ", scenarios=("subscription",)),
    _t("list_subscriptions", "READ", read_only=True, scenarios=("subscription", "billing")),
    _t("cancel_subscription", "MED", scenarios=("subscription",)),
    _t("list_upcoming_precharges", "READ", read_only=True, scenarios=("subscription",)),
    _t("decide_precharge", "LOW", scenarios=("subscription",)),

    # —— 5. 理财 ——
    _t("list_wealth_products", "READ", read_only=True, scenarios=("wealth",)),
    _t("get_product", "READ", read_only=True, scenarios=("wealth",)),
    _t("compare_products", "READ", read_only=True, scenarios=("wealth",)),
    _t("get_risk_profile", "READ", read_only=True, scenarios=("wealth",)),
    _t("set_risk_profile", "MED", scenarios=("wealth",)),
    _t("get_holdings", "READ", read_only=True, scenarios=("wealth",)),
    _t("subscribe_product", "HIGH", money=True, two_step=True, scenarios=("wealth",)),
    _t("redeem_product", "HIGH", money=True, two_step=True, scenarios=("wealth",)),

    # —— 6. 卡片 ——
    _t("list_cards", "READ", read_only=True, scenarios=("card", "common")),
    _t("apply_card", "MED", scenarios=("card",)),
    _t("set_card_limits", "MED", scenarios=("card",)),
    _t("set_card_status", "MED", destructive=True, scenarios=("card",)),

    # —— 7. 事件与提醒 ——
    _t("list_events", "READ", read_only=True, scenarios=("events", "linkage")),
    _t("add_event", "LOW", scenarios=("events",)),
    _t("schedule_reminder", "LOW", scenarios=("events", "linkage")),
    _t("list_scheduled_tasks", "READ", read_only=True, scenarios=("events", "common")),
    # run_due_tasks 会推进任务状态；定时转账到期也只转待确认，绝不动钱
    _t("run_due_tasks", "LOW", scenarios=("events",)),

    # —— 8. 跨场景联动 ——
    _t("suggest_linkage", "READ", read_only=True, scenarios=("linkage",)),
    _t("create_linkage_plan", "HIGH", two_step=True, scenarios=("linkage",)),
    _t("get_linkage_plan", "READ", read_only=True, scenarios=("linkage",)),
    _t("execute_linkage_action", "HIGH", money=True, scenarios=("linkage",)),
    _t("cancel_linkage_plan", "MED", scenarios=("linkage",)),
])

# 四个真正动钱的工具（确认级 HIGH + money=True）——答辩时讲「最小动钱面」就用这行
MONEY_TOOLS: frozenset[str] = frozenset(
    name for name, m in REGISTRY.items() if m.money)


# ------------------------------------------------------------------ 查询助手

def meta(name: str) -> ToolMeta | None:
    return REGISTRY.get(name)


def tools_for_scenario(scenario: str, *, include_money: bool = False) -> tuple[str, ...]:
    """某场景的工具清单（fail-closed：资金类工具默认不随场景下发）。"""
    if scenario not in SCENARIOS:
        raise ValueError(f"未知场景 {scenario!r}，可选：{sorted(SCENARIOS)}")
    return tuple(
        name for name, m in REGISTRY.items()
        if scenario in m.scenarios and (include_money or not m.money))


def read_only_tools() -> frozenset[str]:
    return frozenset(name for name, m in REGISTRY.items() if m.read_only)


# ------------------------------------------------------------------ 对账与自检

_DOCSTRING_RISK_HEADS = ("READ", "LOW", "MED", "HIGH")


def parse_docstring_risk(doc: str | None) -> str | None:
    """解析 docstring 开头的风险标签：[READ] / [HIGH·动钱] / [READ·敏感] → READ/HIGH/READ。"""
    if not doc:
        return None
    stripped = doc.lstrip()
    if not stripped.startswith("["):
        return None
    head = stripped[1:stripped.find("]")] if "]" in stripped else ""
    base = head.split("·", 1)[0].strip()
    return base if base in _DOCSTRING_RISK_HEADS else None


def validate_registry(expected: dict[str, str | None]) -> list[str]:
    """注册表与真实工具集对账。expected: {工具名: docstring 风险标签(可 None)}。

    返回漂移清单（空 = 一致）：
    - 真实注册了但注册表没有 → 「缺少元数据」（新工具忘了登记，fail-closed 拦下）；
    - 注册表有但没注册 → 「注册表多余」（工具删了登记没删）；
    - 两边风险等级不一致 → 「风险漂移」。
    """
    problems: list[str] = []
    for name, doc_risk in expected.items():
        m = REGISTRY.get(name)
        if m is None:
            problems.append(f"缺少元数据：工具 {name} 已注册但 tool_meta.REGISTRY 未登记")
            continue
        if doc_risk is None:
            problems.append(f"docstring 缺风险标签：{name} 开头没有 [READ]/[LOW]/[MED]/[HIGH]")
        elif m.risk != doc_risk:
            problems.append(
                f"风险漂移：{name} docstring 标 [{doc_risk}]，注册表登记 {m.risk}")
    extras = sorted(set(REGISTRY) - set(expected))
    for name in extras:
        problems.append(f"注册表多余：{name} 在 REGISTRY 里但未注册为 @mcp.tool")
    return problems


def _check_invariants() -> None:
    """数据自检：自相矛盾的身份证在导入时就炸（而不是上线后才发现）。"""
    problems: list[str] = []
    for name, m in REGISTRY.items():
        where = f"tool_meta[{name}]"
        if m.risk not in RISKS:
            problems.append(f"{where}: 未知风险等级 {m.risk!r}")
        if not m.scenarios:
            problems.append(f"{where}: scenarios 不能为空（至少挂 common）")
        elif not set(m.scenarios) <= SCENARIOS:
            problems.append(f"{where}: 未知场景 {set(m.scenarios) - SCENARIOS}")
        if m.money and m.read_only:
            problems.append(f"{where}: money 与 read_only 互斥")
        if m.two_step and m.read_only:
            problems.append(f"{where}: two_step 必然写状态，与 read_only 互斥")
        if m.destructive and m.read_only:
            problems.append(f"{where}: destructive 与 read_only 互斥")
        if m.sensitive and m.risk != "READ":
            problems.append(f"{where}: sensitive 只用于修饰 READ 类工具")
        if m.read_only and m.risk != "READ":
            problems.append(f"{where}: read_only=True 的工具 risk 必须是 READ")
        if m.risk == "HIGH" and m.concurrency_safe:
            problems.append(f"{where}: HIGH 工具不允许声明 concurrency_safe")
        if m.money and m.risk != "HIGH":
            problems.append(f"{where}: 动钱工具 risk 必须是 HIGH")
    if problems:
        raise RuntimeError(
            "bank_core.tool_meta 注册表自检失败（先修注册表再启动）:\n"
            + "\n".join(problems))


_check_invariants()

__all__ = [
    "REGISTRY",
    "RISKS",
    "SCENARIOS",
    "MONEY_TOOLS",
    "ToolMeta",
    "meta",
    "tools_for_scenario",
    "read_only_tools",
    "parse_docstring_risk",
    "validate_registry",
]
