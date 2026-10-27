"""客户行为洞察：兴趣度引擎 + 声明式准入 + 「可买未持有」候选集。

核心机制两件:「归一化频次 × α + 时间衰减 × β → softmax 概率分布」
的兴趣度分布,与「门槛表 → 资格校验 → 未持有 × 达标 = 候选集」的
next-best-offer 候选;信号源、产品体系、等级口径全部按本项目
SQLite 库实现(无 Mongo/Qdrant/信用分等外部依赖)。

三条边界（方案第四节硬约束）：
1. 纯只读：本模块只发 SELECT，不写任何表、不调 audit()（否则自己会污染
   兴趣信号源）；资金路径的强校验仍在 wealth.py 申购闸门，本模块只做
   推荐前预筛，不改 wealth.py 的任何行为。
2. 零新依赖：仅标准库（math/json/os/sqlite3/datetime/pathlib）。
3. 风险等级映射复用 wealth.py 的 _LEVEL_TO_RISK（单一事实源，防止两处
   口径漂移），等级含义：C1-C5 可购 R1-R(同号) 产品。

已知限制（方案 M1「必须接受」项 + 本库事实）：
- audit_log 无 user_id 列（db.py:232-239 建表即如此，全行共享）。浏览类信号
  只在**单用户库**（users 仅 1 行）时消费——此时归因无歧义；多用户库一律
  跳过（宁可冷启动，也不能把别人的浏览算到本人头上）。给 audit_log 补
  user_id 属改表/埋点，超出本方案「不建表」约束。
- audit_log 只在真实工具调用时产生，演示库可能稀疏 → 冷启动兜底到
  「声明偏好(agent_memory) / 当前持仓」，并在输出中标注 confidence=low。
- 年龄门槛只在 users.id_card 可解析时生效；空串/脏号按老用户兜底放行并标
  verified=False（见 check_eligibility）。真实演示库多数用户 id_card 为空
  （seed 存量数据），该闸门实际覆盖率有限——这是数据现状，只读路径无法
  补写证件号，不能据此把存量用户全部拒掉。
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import date, datetime
from pathlib import Path

from .money import cents_to_yuan
from .wealth import _LEVEL_TO_RISK  # noqa: 私有名跨模块引用：同一包内刻意共享口径

# ---------------------------------------------------------------- M1 常量
# 兴趣度 = ALPHA×归一化频次 + BETA×时间衰减，再对全体产品 softmax。
# 取值对齐参考项目 tools.py:76-107（alpha=0.6、beta=0.4、tau=1440 分钟）；
# 本项目信号粒度是"天"，tau 换算成 1.0 天（= 1440 分钟），含义不变。
ALPHA = 0.6
BETA = 0.4
TAU_DAYS = 1.0

# 信号强度：申赎是真金白银的行为，强于"看了一眼"；类型级浏览（按 p_type
# 查产品列表）按类型内每只产品分摊，权重要再低一档。
_WEIGHT_TRADE = 2.0       # transactions 里 subscribe/redeem 一笔
_WEIGHT_VIEW = 1.0        # audit_log 里点名看某只产品（compare_products）
_WEIGHT_TYPE_BROWSE = 0.5  # audit_log 里按 p_type/keyword 浏览，摊到类型内每只
_WEIGHT_PRIOR = 1.0       # 冷启动兜底伪信号（声明偏好 / 当前持仓）

# 冷启动之外的置信度分档：行为动作总数（每次浏览工具调用/每笔申赎各记
# 1 件，不按一次调用里涉及的产品数放大；不含冷启动伪信号）
_CONFIDENCE_MEDIUM_MIN = 5  # ≥5 件行为证据才敢说 high

# 产品类型全集（对齐 db.py wealth_products.p_type 的 CHECK 白名单），
# by_type 固定输出这五档，无产品的档位为 0.0。
PRODUCT_TYPES: tuple[str, ...] = ("money_fund", "deposit", "bond", "mixed", "gold")

# ---------------------------------------------------------------- M2 常量
# 声明式准入门槛表（迁移自参考项目 utils_setting.py 的三张约束表结构，
# 列含义按本项目重建）：
#   min_age     最低年龄（周岁），从 users.id_card 解析出生日期核验；
#   income_tier 收入层级门槛——保留字段、默认不启用（本项目收入是自由
#               文本，见方案 M2「解析不可靠」，等结构化后再开）。
# 风险门槛与起购金额**不在**此表重复配置：以 wealth_products.risk_level /
# min_subscribe_cents 实时值为准（单一事实源，避免表间漂移）。
PRODUCT_ELIGIBILITY: dict[str, dict] = {
    "MF001": {"min_age": 18, "income_tier": None},
    "DP002": {"min_age": 18, "income_tier": None},
    "BD003": {"min_age": 18, "income_tier": None},
    "BD004": {"min_age": 18, "income_tier": None},
    "MX005": {"min_age": 18, "income_tier": None},
    "MX006": {"min_age": 18, "income_tier": None},
    "MX007": {"min_age": 18, "income_tier": None},
    "GD008": {"min_age": 18, "income_tier": None},
    "DP009": {"min_age": 18, "income_tier": None},
    "MX010": {"min_age": 18, "income_tier": None},
}
_DEFAULT_ELIGIBILITY = {"min_age": 18, "income_tier": None}

# 风险承受等级 → 可购产品风险上限（口径 = wealth.py 申购闸门同一张表）
LEVEL_TO_RISK = _LEVEL_TO_RISK

# 默认记忆库路径与 agent/memory.db_path() 同一定位规则：环境变量优先，
# 否则项目根 data/agent_memory.sqlite。bank_core 不 import agent 包
# （分层：agent 依赖 bank_core，反向 import 会把 agent 拖进核心层测试）。
_DEFAULT_MEMORY_DB = Path(__file__).resolve().parent.parent / "data" / "agent_memory.sqlite"


# ---------------------------------------------------------------- 小工具
def _naive(dt: datetime) -> datetime:
    """统一成无时区 datetime：库里时间串带/不带 +08:00 混存，跨感知
    相减会 TypeError，一律视为本地时间剥掉 tzinfo。"""
    if dt.tzinfo is not None:
        return dt.replace(tzinfo=None)
    return dt


def _parse_ts(raw: str | None) -> datetime | None:
    """ISO 时间串 → naive datetime；解析失败返回 None（脏数据不当信号时间）。"""
    if not raw:
        return None
    try:
        return _naive(datetime.fromisoformat(str(raw)))
    except ValueError:
        return None


def _softmax(weights: dict[str, float]) -> dict[str, float]:
    """标准 softmax。本模块权重上界 = ALPHA+BETA = 1.0，e^w 无溢出风险。"""
    exps = {k: math.exp(v) for k, v in weights.items()}
    total = sum(exps.values())
    return {k: v / total for k, v in exps.items()}


def _load_products(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """全部理财产品，按 code 排序保证输出顺序确定。"""
    return conn.execute(
        "SELECT * FROM wealth_products ORDER BY code").fetchall()


def _risk_cap(conn: sqlite3.Connection, user_id: int) -> tuple[int, str | None]:
    """用户可购风险上限。无测评 → (1, None)，与 wealth.py:133-139 的
    「无测评一律限购 R1」口径一致（推荐预筛不能比资金闸门更松）。"""
    row = conn.execute(
        "SELECT level FROM risk_profiles WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return 1, None
    return LEVEL_TO_RISK.get(row["level"], 1), row["level"]


def _age_from_id_card(id_card: str, today: date | None = None) -> int | None:
    """身份证号 → 周岁（以 today 为基准日，默认今天，供测试钉死时间）。
    支持 18 位（第 7-14 位 YYYYMMDD）与 15 位老号（第 7-12 位 YYMMDD，
    年份补 19）；格式/日期/年份范围（未来或早于 1900）非法返回 None
    （调用方按老用户兜底处理，不据此拒绝）。不做校验位核验——准入只关心
    出生日期，实名核验是注册环节的事。"""
    s = "".join(str(id_card or "").split()).upper()
    try:
        if len(s) == 18 and s[:17].isdigit():
            birth = date(int(s[6:10]), int(s[10:12]), int(s[12:14]))
        elif len(s) == 15 and s.isdigit():
            birth = date(1900 + int(s[6:8]), int(s[8:10]), int(s[10:12]))
        else:
            return None
    except ValueError:
        return None
    today = today or date.today()
    # 年份段是脏数据时 date() 仍可能构造出"合法日期"：出生在未来的
    # （如 2090/9184）会算出负数年龄，被当成"未成年"拒绝并写出
    # 「-64 岁」这种荒谬理由；早于 1900 的同样不可信。一律按不可解析
    # 处理，走老用户兜底（与空串/非法月份同口径）。
    if birth > today or birth.year < 1900:
        return None
    return today.year - birth.year - (
        (today.month, today.day) < (birth.month, birth.day))


# ---------------------------------------------------------------- M1 信号采集
def _as_int(value) -> int | None:
    """JSON 里的 id 可能是 int 也可能是数字串；其余一律 None（不当信号）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _audit_signals_attributable(conn: sqlite3.Connection) -> bool:
    """audit_log 能否把浏览信号无歧义地归到被分析用户。

    audit_log 没有 user_id 列（db.py:232-239），只有**单用户库**（users
    仅 1 行）时"全行浏览 = 本人浏览"才成立。多用户库一律返回 False，
    调用方跳过浏览信号——否则库里任何人的一次 list/compare 都会被算到
    每个被分析用户头上，零行为用户也会拿到别人的兴趣分布和 high 置信度。
    """
    row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    return bool(row) and int(row["n"]) == 1


def _collect_audit_signals(
        conn: sqlite3.Connection, products: list[sqlite3.Row],
        sig: dict[str, dict]) -> int:
    """audit_log 浏览类信号 → 就地累加进 sig。返回**用户动作数**。

    只取能落到具体产品的调用，并**复刻对应工具的真实过滤语义**：
    - compare_products {"ids": [...]}   → 点名比较，每只被比较产品记 1.0；
    - list_products {"p_type","max_risk","keyword"} → 三个条件是 AND
      （wealth.py:37-40 的 SQL 就是 WHERE 串联），命中产品各摊 0.5。
      无 p_type 也无 keyword 的「随便翻翻」不指向任何产品，不计信号——
      否则一次全量列表就会把置信度抬到 high，兴趣分布却仍是均匀的。
    申赎虽也落 audit（subscribe_product/redeem_product），但用户归属在
    transactions 里才有（audit_log 无 user_id），为避免重复计数这里不计。
    get_holdings 参数为空、无产品指向，同样不计。

    调用方必须先用 _audit_signals_attributable 确认单用户库。
    事件数按**一次工具调用 = 1 件**计，不按命中产品数放大：一次比较 5 只
    产品只是一次用户动作，不能直接凑满 5 件证据把置信度抬到 high。
    """
    by_id = {_as_int(p["id"]): p["code"] for p in products}

    events = 0
    rows = conn.execute(
        "SELECT ts, tool, args_json FROM audit_log "
        "WHERE tool IN ('list_products', 'compare_products') ORDER BY id"
    ).fetchall()
    for r in rows:
        try:
            args = json.loads(r["args_json"] or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(args, dict):
            continue
        ts = _parse_ts(r["ts"])
        hits: dict[str, float] = {}
        if r["tool"] == "compare_products":
            ids = args.get("ids") or []
            if isinstance(ids, list):
                for pid in ids:
                    code = by_id.get(_as_int(pid))
                    if code:
                        hits[code] = _WEIGHT_VIEW
        else:  # list_products
            p_type = str(args.get("p_type") or "").strip()
            keyword = str(args.get("keyword") or "").strip().lower()
            if p_type or keyword:
                max_risk = _as_int(args.get("max_risk"))
                for p in products:
                    if p_type and p["p_type"] != p_type:
                        continue
                    if keyword and keyword not in p["name"].lower() \
                            and keyword not in (p["intro"] or "").lower():
                        continue
                    if max_risk is not None and int(p["risk_level"]) > max_risk:
                        continue
                    # 同一行 p_type 与 keyword 同时命中只算一次（dict 天然去重）
                    hits[p["code"]] = _WEIGHT_TYPE_BROWSE
        if hits:
            events += 1  # 一次工具调用 = 一件用户动作（不按命中产品数放大）
        for code, weight in hits.items():
            entry = sig.setdefault(code, _new_signal())
            entry["freq"] += weight
            entry["count"] += 1
            src = f"audit:{r['tool']}"
            entry["sources"][src] = entry["sources"].get(src, 0) + 1
            if ts and (entry["last_seen"] is None or ts > entry["last_seen"]):
                entry["last_seen"] = ts
    return events


def _collect_trade_signals(
        conn: sqlite3.Connection, user_id: int,
        products: list[sqlite3.Row], sig: dict[str, dict]) -> int:
    """用户本人的申赎流水 → 信号（行为强度权重 _WEIGHT_TRADE）。

    subscribe_product/redeem_product 落流水时 counterparty 恰为产品全名
    （wealth.py:180/248），按名精确匹配；join accounts 限定本人账户。
    """
    name_to_code = {p["name"]: p["code"] for p in products}
    events = 0
    rows = conn.execute(
        """SELECT t.ts, t.tx_type, t.counterparty FROM transactions t
           JOIN accounts a ON a.id = t.account_id
           WHERE a.user_id = ? AND t.tx_type IN ('subscribe', 'redeem')
           ORDER BY t.id""",
        (user_id,)).fetchall()
    for r in rows:
        code = name_to_code.get(r["counterparty"])
        if not code:
            continue  # 非产品申赎流水（理论上不会出现在这两种 tx_type 里）
        ts = _parse_ts(r["ts"])
        entry = sig.setdefault(code, _new_signal())
        entry["freq"] += _WEIGHT_TRADE
        entry["count"] += 1
        src = f"tx:{r['tx_type']}"
        entry["sources"][src] = entry["sources"].get(src, 0) + 1
        if ts and (entry["last_seen"] is None or ts > entry["last_seen"]):
            entry["last_seen"] = ts
        events += 1
    return events


def _read_memory_prior(user_id: int) -> list[tuple[str, str | None]]:
    """agent_memory 声明偏好候选：[(preferred_type, updated_at), ...]。

    只读、软失败：库文件不存在/表不存在/JSON 脏数据一律返回空列表，
    绝不让记忆问题拖垮主流程（对齐 agent/memory.py 的失败软处理铁律）。
    本库的 preferred_type 有**两种口径**，两种都返回、由调用方分别匹配：
    - graph.py:4739/4754 写入的是最大持仓的**产品名**（top_holding，如
      「稳健纯债基金」「余额+货币基金」）——这是真实写入路径；
    - 历史数据/人工写入可能是一档 p_type（如 "gold"）。
    按优先级返回**所有**非空候选（profile 的声明优先于 wealth 的持仓推断），
    调用方逐个尝试匹配；某个候选在本库没有对应产品时继续试下一个，不能因为
    profile 一条脏值就丢掉 wealth 的有效偏好。
    时间戳按**提供该 preferred_type 的模块**取：两个模块各有一份 updated_at，
    取"最后一行"会让时间衰减用错基准。
    """
    raw = os.environ.get("AGENT_MEMORY_DB", "").strip()
    path = Path(raw) if raw else _DEFAULT_MEMORY_DB
    if not path.is_file():
        return []
    try:
        mconn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = mconn.execute(
                "SELECT module, content_json, updated_at FROM agent_memory "
                "WHERE user_id = ? AND module IN ('profile', 'wealth')",
                (user_id,)).fetchall()
        finally:
            mconn.close()
    except sqlite3.Error:
        return []
    content: dict[str, tuple[dict, str]] = {}
    for module, content_json, updated_at in rows:
        try:
            parsed = json.loads(content_json or "{}")
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            content[str(module)] = (parsed, updated_at or "")
    candidates: list[tuple[str, str | None]] = []
    for module in ("profile", "wealth"):  # 声明偏好优先于持仓推断
        parsed, updated_at = content.get(module, ({}, ""))
        pref = str(parsed.get("preferred_type") or "").strip()
        if pref:  # 产品名或 p_type 都可能是合法值，匹配交给调用方
            candidates.append((pref, updated_at or None))
    return candidates


def _new_signal() -> dict:
    """信号累加器：freq=加权频次，count=原始事件数（解释用）。"""
    return {"freq": 0.0, "count": 0, "last_seen": None, "sources": {}}


def _apply_cold_start_priors(
        conn: sqlite3.Connection, user_id: int,
        products: list[sqlite3.Row], sig: dict[str, dict]) -> list[str]:
    """零行为信号时的兜底：声明偏好 → 当前持仓。返回**实际生效**的兜底来源。

    持仓作为"已持有"先验只喂兴趣分布（by_type 体现既有配置倾向），
    不影响 M3——M3 的候选集另行剔除已持有产品。
    声明偏好先按**产品名**精确匹配（graph.py 的真实写入口径），再按 p_type
    匹配（历史口径）；候选按 profile → wealth 优先级逐个尝试，某个候选在本库
    没有对应产品时继续试下一个。全部未命中时继续走持仓兜底：不能输出一条
    "看起来命中了、其实权重全零"的 fallback 说明（审计会据此误判）。
    """
    pref: str | None = None
    matched: list[sqlite3.Row] = []
    for candidate, updated_at in _read_memory_prior(user_id):
        matched = [p for p in products if p["name"] == candidate]
        if not matched and candidate in PRODUCT_TYPES:
            matched = [p for p in products if p["p_type"] == candidate]
        if matched:
            pref = candidate
            break
    if matched and pref:
        for p in matched:
            entry = sig.setdefault(p["code"], _new_signal())
            entry["freq"] += _WEIGHT_PRIOR
            entry["count"] += 1
            entry["sources"]["memory:preferred_type"] = \
                entry["sources"].get("memory:preferred_type", 0) + 1
            ts = _parse_ts(updated_at)
            if ts and (entry["last_seen"] is None or ts > entry["last_seen"]):
                entry["last_seen"] = ts
        return [f"agent_memory:preferred_type={pref}"]

    rows = conn.execute(
        """SELECT p.code, MAX(h.subscribed_at) AS last_at
           FROM wealth_holdings h JOIN wealth_products p ON p.id = h.product_id
           WHERE h.user_id = ? AND h.status = 'holding'
           GROUP BY p.code""",
        (user_id,)).fetchall()
    for r in rows:
        entry = sig.setdefault(r["code"], _new_signal())
        entry["freq"] += _WEIGHT_PRIOR
        entry["count"] += 1
        entry["sources"]["holding"] = entry["sources"].get("holding", 0) + 1
        ts = _parse_ts(r["last_at"])
        if ts and (entry["last_seen"] is None or ts > entry["last_seen"]):
            entry["last_seen"] = ts
    return ["wealth_holdings:当前持仓"] if rows else []


# ---------------------------------------------------------------- M1 主入口
def compute_interest_scores(
        conn: sqlite3.Connection, user_id: int,
        now: datetime | None = None) -> dict:
    """用户对各理财产品的兴趣概率分布（可解释、可测试）。

    算法（迁移自参考项目，参数对齐）：对每只产品
        weight = ALPHA × (freq / max_freq) + BETA × exp(-距今天数 / TAU_DAYS)
    再 softmax 成概率。全零信号时权重全 0 → softmax 自然退化为均匀分布
    （即冷启动兜底之一），另以声明偏好/持仓先验加权。

    返回：
        by_product  {product_code: prob}   概率和为 1（产品库非空时）
        by_type     {p_type: prob}         按类型聚合，五档齐全；产品库非空时和为 1
        signals     {product_code: {freq, last_seen, sources[]}}  可解释明细
        confidence  low（冷启动）/ medium（行为动作 <5 件）/ high（≥5 件）
        cold_start / fallback             兜底是否触发、命中哪种先验

    产品库为空（未播种的裸库）时提前返回：by_product={}、by_type 五档全 0，
    概率和不为 1（无产品可归一化）——这是显式退化路径，不是均匀分布。
    """
    now = _naive(now) if now else datetime.now()
    products = _load_products(conn)
    codes = [p["code"] for p in products]
    result_base = {
        "user_id": user_id, "generated_at": now.isoformat(),
        "by_product": {c: 0.0 for c in codes},
        "by_type": {t: 0.0 for t in PRODUCT_TYPES},
        "signals": {}, "confidence": "low", "cold_start": True, "fallback": [],
    }
    if not products:  # 空产品库（理论只在未播种的裸库出现）
        return result_base

    # 1) 信号采集：audit 浏览只在单用户库可归因（无 user_id 列，见模块头
    #    "已知限制"）+ 本人申赎（按账户归属过滤，始终可用）
    sig: dict[str, dict] = {}
    events = 0
    if _audit_signals_attributable(conn):
        events += _collect_audit_signals(conn, products, sig)
    events += _collect_trade_signals(conn, user_id, products, sig)
    cold_start = events == 0
    fallback = _apply_cold_start_priors(conn, user_id, products, sig) \
        if cold_start else []

    # 2) 兴趣权重 → softmax 概率
    max_freq = max((v["freq"] for v in sig.values()), default=0.0)
    weights: dict[str, float] = {}
    for code in codes:
        entry = sig.get(code)
        if not entry or (entry["freq"] <= 0 and entry["last_seen"] is None):
            weights[code] = 0.0
            continue
        norm_freq = entry["freq"] / max_freq if max_freq > 0 else 0.0
        recency = 0.0
        if entry["last_seen"] is not None:
            # 距今天数：未来时间戳（时钟偏移）截为 0，避免 recency>1 超权
            days = max((now - entry["last_seen"]).total_seconds() / 86400, 0)
            recency = math.exp(-days / TAU_DAYS)
        weights[code] = round(ALPHA * norm_freq + BETA * recency, 6)
    probs = _softmax(weights)

    by_product = {c: round(probs[c], 6) for c in codes}
    by_type_raw = {t: 0.0 for t in PRODUCT_TYPES}
    for p in products:  # 类型聚合：类型概率 = 该类型各产品概率之和
        by_type_raw[p["p_type"]] = by_type_raw.get(p["p_type"], 0.0) \
            + by_product[p["code"]]
    by_type = {t: round(v, 6) for t, v in by_type_raw.items()}

    # 3) 可解释明细：只列真有信号的产品（含冷启动伪信号）
    signals = {}
    for code, entry in sig.items():
        if entry["count"] <= 0 and not entry["sources"]:
            continue
        signals[code] = {
            "freq": round(entry["freq"], 2),
            "count": entry["count"],
            "last_seen": entry["last_seen"].isoformat() if entry["last_seen"] else None,
            "sources": [{"source": k, "count": v}
                        for k, v in sorted(entry["sources"].items())],
        }

    confidence = "low" if cold_start else (
        "medium" if events < _CONFIDENCE_MEDIUM_MIN else "high")
    return {
        "user_id": user_id, "generated_at": now.isoformat(),
        "by_product": by_product, "by_type": by_type, "signals": signals,
        "confidence": confidence, "cold_start": cold_start, "fallback": fallback,
    }


# ---------------------------------------------------------------- M2 准入
def check_eligibility(
        conn: sqlite3.Connection, user_id: int, product_code: str,
        now: datetime | None = None) -> dict:
    """产品准入资格校验（推荐前预筛 + 合规留痕），返回可审计结果。

    三道活闸门 + 一道预留闸门：
    - 年龄：PRODUCT_ELIGIBILITY.min_age，出生日期取 users.id_card（18/15
      位）。**老用户空串/脏号兜底**：无法解析时不据此拒绝（seed 播种的
      存量用户 id_card 就是空串），标记 verified=False 供审计。
    - 风险测评：产品 R 级 ≤ 用户 C 级上限；未测评一律只放行 R1——口径
      与 wealth.py:133-143 申购闸门逐字对齐（预筛不得比闸门松）。
    - 起购金额：用户**单张账户**可用余额的最大值 ≥ min_subscribe_cents。
      口径对齐 wealth.py:149-154 的真实闸门（只校验 from_account_id 单张
      账户，多账户余额不能合并）；预筛按「存在一张账户付得起」判定，
      否则多账户小额分散的用户会被误判为可买、候选理由也会写错。
    - 收入层级：字段保留、默认 None 不启用。

    返回 {"eligible", "checks": {age/risk/min_amount/income}, "reasons": [...],
    "reason": "；".join(reasons)}；产品或用户不存在抛 ValueError（调用方
    的参数错误，不是用户资格状态）。
    """
    now = _naive(now) if now else datetime.now()
    product = conn.execute(
        "SELECT * FROM wealth_products WHERE code=?", (product_code,)).fetchone()
    if not product:
        raise ValueError(f"产品不存在: {product_code}")
    user = conn.execute(
        "SELECT id_card FROM users WHERE id=?", (user_id,)).fetchone()
    if not user:
        raise ValueError(f"用户不存在: {user_id}")

    cfg = PRODUCT_ELIGIBILITY.get(product_code, _DEFAULT_ELIGIBILITY)
    checks: dict[str, dict] = {}
    reasons: list[str] = []

    # -- 年龄 -------------------------------------------------------
    min_age = int(cfg["min_age"])
    age = _age_from_id_card(user["id_card"], today=now.date())
    if age is None:
        checks["age"] = {"ok": True, "verified": False, "min_age": min_age,
                         "note": "未登记有效身份证号，年龄门槛未核验（老用户兜底）"}
    elif age >= min_age:
        checks["age"] = {"ok": True, "verified": True, "age": age,
                         "min_age": min_age}
    else:
        checks["age"] = {"ok": False, "verified": True, "age": age,
                         "min_age": min_age}
        reasons.append(f"年龄未满 {min_age} 周岁（据身份证解析为 {age} 岁）")

    # -- 风险测评 ---------------------------------------------------
    risk = int(product["risk_level"])
    cap, level = _risk_cap(conn, user_id)
    checks["risk"] = {"ok": risk <= cap, "product_risk": f"R{risk}",
                      "user_level": level, "risk_cap": cap}
    if risk > cap:
        if level is None:
            reasons.append(
                f"尚未完成风险测评，按适当性要求暂不能购买 R{risk} 产品"
                f"「{product['name']}」，请先完成风险测评")
        else:
            reasons.append(
                f"产品风险 R{risk} 超出您的风险承受等级 {level}"
                f"（最高可购 R{cap}）")

    # -- 起购金额（对齐资金闸门：只看单张账户，不合计多账户） ---------
    row = conn.execute(
        "SELECT COALESCE(MAX(balance_cents), 0) AS s FROM accounts "
        "WHERE user_id = ?", (user_id,)).fetchone()
    available = int(row["s"]) if row else 0
    min_cents = int(product["min_subscribe_cents"])
    checks["min_amount"] = {
        "ok": available >= min_cents,
        "available_cents": available, "min_subscribe_cents": min_cents,
        "available_yuan": cents_to_yuan(available),
        "min_subscribe_yuan": cents_to_yuan(min_cents),
        "basis": "max_single_account"}
    if available < min_cents:
        reasons.append(f"单账户可用余额最高 {cents_to_yuan(available)} 元"
                       f"低于起购金额 {cents_to_yuan(min_cents)} 元")

    # -- 收入层级（预留，默认不启用） --------------------------------
    tier = cfg.get("income_tier")
    if tier is None:
        checks["income"] = {"ok": True, "enabled": False,
                            "note": "收入门槛未启用"}
    else:
        # 预留分支：本项目收入仍是非结构化自由文本，无法核验 → **fail-closed**
        # （配置了门槛却验不了，不得放行）。字段结构化后只需补校验规则，
        # 返回结构不变。
        checks["income"] = {"ok": False, "enabled": True, "verified": False,
                            "required": tier}
        reasons.append(f"收入层级未达 {tier} 要求（收入尚未结构化，无法核验）")

    return {
        "user_id": user_id, "product_code": product_code,
        "product_name": product["name"],
        "eligible": not reasons, "checks": checks,
        "reasons": reasons, "reason": "；".join(reasons),
        "checked_at": now.isoformat(),
    }


# ---------------------------------------------------------------- M3 候选集
def recommend_candidates(
        conn: sqlite3.Connection, user_id: int, top_n: int = 3,
        now: datetime | None = None) -> list[dict]:
    """「可买未持有」候选集（交叉营销 / NBO），按 兴趣概率 × 适当性 排序。

    过滤：剔除当前持仓（status='holding'）→ 逐只过 check_eligibility，
    三道闸门全过才进候选。排序键：
        score = interest_prob × suitability，suitability = R级/可购上限
    ——适当性上限内、越贴近用户风险承受能力的产品越匹配（同等兴趣下
    优先用满已评估的风险预算，收益空间更大）；并列时按业绩基准降序、
    再按 code 升序，保证完全确定。

    每条候选带 reasons[]（命中哪些信号、适当性依据、起购对比），
    供 M4 推荐话术与合规留痕直接引用。起购对比与准入同口径：单张账户
    可用余额（不合计多账户），与 wealth.py 真实扣款闸门一致。
    """
    now = _naive(now) if now else datetime.now()
    if top_n <= 0:
        return []
    products = _load_products(conn)
    held = {r["code"] for r in conn.execute(
        """SELECT p.code FROM wealth_holdings h
           JOIN wealth_products p ON p.id = h.product_id
           WHERE h.user_id = ? AND h.status = 'holding'""",
        (user_id,)).fetchall()}
    scores = compute_interest_scores(conn, user_id, now=now)
    cap, level = _risk_cap(conn, user_id)
    # 与 check_eligibility / wealth.py 申购闸门同口径：单张账户，不合计
    row = conn.execute(
        "SELECT COALESCE(MAX(balance_cents), 0) AS s FROM accounts "
        "WHERE user_id = ?", (user_id,)).fetchone()
    available_yuan = cents_to_yuan(int(row["s"])) if row else "0.00"

    out: list[dict] = []
    for p in products:
        if p["code"] in held:
            continue
        elig = check_eligibility(conn, user_id, p["code"], now=now)
        if not elig["eligible"]:
            continue
        risk = int(p["risk_level"])
        suitability = round(risk / cap, 4) if cap > 0 else 0.0
        prob = scores["by_product"].get(p["code"], 0.0)
        out.append({
            "code": p["code"], "name": p["name"], "p_type": p["p_type"],
            "risk_level": f"R{risk}",
            "interest_prob": prob,
            "suitability": suitability,
            "score": round(prob * suitability, 6),
            "min_subscribe_yuan": cents_to_yuan(int(p["min_subscribe_cents"])),
            "reasons": [
                _interest_reason(scores, p["code"]),
                f"适当性匹配：产品 R{risk} ≤ 可购上限 R{cap}"
                f"（{level + ' 级' if level else '未测评，按 R1 上限'}）",
                f"未持有；起购 {cents_to_yuan(int(p['min_subscribe_cents']))} 元"
                f" ≤ 单账户可用余额 {available_yuan} 元",
            ],
        })
    out.sort(key=lambda c: (-c["score"],
                            -_bps_of(products, c["code"]), c["code"]))
    return out[:top_n]


def _bps_of(products: list[sqlite3.Row], code: str) -> int:
    """业绩基准（万分比）——并列时的次级排序键，取自产品行。"""
    for p in products:
        if p["code"] == code:
            return int(p["expected_return_bps"])
    return 0


# 信号来源 → 展示词（reasons 人话化，纯确定性映射）
_SOURCE_LABELS = {
    "audit:compare_products": "产品比较",
    "audit:list_products": "产品浏览",
    "tx:subscribe": "申购",
    "tx:redeem": "赎回",
    "memory:preferred_type": "声明偏好兜底",
    "holding": "持仓兜底",
}


def _interest_reason(scores: dict, code: str) -> str:
    """把 signals 明细压成一句人话（纯确定性拼接，不调 LLM）。"""
    prob = scores["by_product"].get(code, 0.0)
    entry = scores.get("signals", {}).get(code)
    if not entry or not entry.get("sources"):
        return f"兴趣概率 {prob:.1%}（无直接行为信号）"
    parts = [f"{s['count']} 次"
             f"{_SOURCE_LABELS.get(s['source'], s['source'])}"
             for s in entry["sources"]]
    return f"兴趣概率 {prob:.1%}（{'、'.join(parts)}）"
