"""跨层接口契约守卫 —— 多人并行开发的"合同测试"。

为什么需要这个文件
------------------
本项目前后端由不同人并行开发:Python 侧(agent/bank_core)与 TypeScript 侧
(webui)之间只有一条真实接缝,它是四段串联的:

    graph.interrupt({"type": ...})      agent/graph.py   闸门载荷类型
        ↓  _interrupt_frames 映射
    {"type": "data-<name>", "data":…}   agent/api.py     SSE 数据部件
        ↓  UIMessageStream 线协议
    type === "data-<name>"              webui/…/bank-parts.tsx  前端渲染

任何一层单方面增删类型,另外两层都不会报错——**只会"卡片静默不显示"**,
而这类故障在演示现场最难查。本测试把这条接缝变成红灯:
契约被破坏时 pytest 直接失败,而不是等到合并/演示时才暴露。

契约变更的正确姿势
------------------
    1) 三处一起改:graph(抛类型) → api(映射) → 前端(渲染);
    2) 同步更新 docs/协作与接口契约.md 的契约表;
    3) 本文件一般无需改动即会自动通过 —— 只改一处则必然失败。

维护提示
--------
本文件用**源码静态解析**而非 import,原因是 graph.py 的图构建需要 MCP 工具与
LLM,静态解析可以让契约检查保持零依赖、毫秒级。代价是:如果将来把 graph.py
重构为包(节点注册列表挪走),需要同步调整下面的 RE_NODE_REGISTRY。
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

GRAPH_PY = ROOT / "agent" / "graph.py"
API_PY = ROOT / "agent" / "api.py"
BANK_PARTS = ROOT / "webui" / "components" / "chat" / "bank-parts.tsx"

# ---- 闸门类型的两处来源 -----------------------------------------------------
# 1. 闸门节点工厂的第一个位置参数:_make_gate_node("confirm_transfer", …)
RE_GATE_TYPE = re.compile(r'_make_gate_node\(\s*"([a-z_]+)"')
# 2. 直接字面量 interrupt:反查缺槽(ask_slot)与同名消歧(pick_contact)
RE_INTERRUPT_LITERAL = re.compile(r'interrupt\(\{\s*"type":\s*"([a-z_]+)"')

# ---- api.py:处理了哪些闸门类型 / 发出哪些 data-* -----------------------------
RE_API_HANDLED = re.compile(r'ptype\s*==\s*"([a-z_]+)"')
RE_DATA_TYPE = re.compile(r'"type":\s*"(data-[a-z-]+)"')

# ---- 前端处理哪些 data-* ----------------------------------------------------
RE_FE_DATA = re.compile(r'type\s*===\s*"(data-[a-z-]+)"')

# ---- SSE 帧词表(Vercel AI SDK 5 UIMessageChunk,见 agent/api.py 头部注释) ----
WIRE_VOCAB = frozenset({
    "start", "text-start", "text-delta", "text-end", "error", "finish",
})
RE_FRAME_TYPE = re.compile(r'_frame\(\{\s*"type":\s*"([A-Za-z-]+)"')


def _read(path: Path) -> str:
    assert path.is_file(), f"契约文件不存在,可能被改名/移动:{path}"
    return path.read_text(encoding="utf-8")


def _gate_types_in_graph() -> set[str]:
    """graph.py 会抛出的全部闸门载荷类型。"""
    src = _read(GRAPH_PY)
    return set(RE_GATE_TYPE.findall(src)) | set(RE_INTERRUPT_LITERAL.findall(src))


def _gate_types_handled_by_api() -> set[str]:
    """api.py::_interrupt_frames 显式分支处理的闸门类型。"""
    return set(RE_API_HANDLED.findall(_read(API_PY)))


def _data_types_emitted_by_api() -> set[str]:
    return set(RE_DATA_TYPE.findall(_read(API_PY)))


def _data_types_handled_by_frontend() -> set[str]:
    return set(RE_FE_DATA.findall(_read(BANK_PARTS)))


def _announce_nodes() -> set[str]:
    src = _read(API_PY)
    m = re.search(r"ANNOUNCE_NODES\s*=\s*frozenset\(\{(.*?)\}\)", src, re.S)
    assert m, "api.py 里找不到 ANNOUNCE_NODES 定义(被重构了?请同步本测试)"
    return set(re.findall(r'"([a-z_]+)"', m.group(1)))


def _registered_nodes() -> set[str]:
    """graph.py 建图段 `for name, fn in [...]` 里注册的节点名。"""
    src = _read(GRAPH_PY)
    m = re.search(r"for name, fn in \[(.*?)\]:", src, re.S)
    assert m, (
        "graph.py 里找不到 `for name, fn in [...]` 节点注册块。"
        "若已把建图段重构为别的写法,请更新本测试的 RE_NODE_REGISTRY 解析逻辑。"
    )
    return set(re.findall(r'\("([a-z_]+)",', m.group(1)))


# --------------------------------------------------------------- 契约 1:闸门链
def test_every_gate_type_is_mapped_by_api() -> None:
    """graph 抛出的每个闸门类型,api.py 都必须有映射分支。

    漏一个的后果:该场景的确认卡片**不会渲染**,用户只看到一句问题文本,
    无法确认也无法取消——演示现场最致命的一类故障。
    """
    missing = _gate_types_in_graph() - _gate_types_handled_by_api()
    assert not missing, (
        f"graph.py 新增了闸门类型 {sorted(missing)},但 agent/api.py 的 "
        f"_interrupt_frames 没有对应分支,前端将收不到卡片。"
        f"请在 _interrupt_frames 里补映射,并更新 docs/协作与接口契约.md。"
    )


def test_api_has_no_orphan_gate_mapping() -> None:
    """api.py 也不应该保留图已不再抛出的闸门映射(死代码)。

    这条失败不一定代表线上故障,但意味着**契约已被单方面改动**:
    要么图的类型改名了(api 未跟进),要么 api 遗留了旧分支。
    确认属于有意保留时,请更新本测试与契约文档。
    """
    orphans = _gate_types_handled_by_api() - _gate_types_in_graph()
    assert not orphans, (
        f"agent/api.py 映射了 {sorted(orphans)},但 graph.py 从不抛出这些类型。"
        f"请确认是改名未同步还是遗留死代码,并更新 docs/协作与接口契约.md。"
    )


# ------------------------------------------------- 契约 2:前后端 data-* 一致性
def test_data_part_types_match_between_backend_and_frontend() -> None:
    """后端发出的 data-* 集合,必须与前端处理的 data-* 集合完全一致。

    这是本项目跨语言(后端 Python / 前端 TS)唯一的类型耦合点。
    后端多发一个 → 前端静默不渲染;前端多认一个 → 该卡片永远不出现。
    两个方向都只会在演示时被发现,所以放在这里当红灯。
    """
    backend = _data_types_emitted_by_api()
    frontend = _data_types_handled_by_frontend()

    only_backend = backend - frontend
    only_frontend = frontend - backend
    assert not only_backend, (
        f"后端发出但前端未处理:{sorted(only_backend)}。"
        f"请在 webui/components/chat/bank-parts.tsx 的 BankDataPart 里补分支。"
    )
    assert not only_frontend, (
        f"前端处理但后端从不发出:{sorted(only_frontend)}。"
        f"请在后端补发,或从 BankDataPart 删除死分支。"
    )
    assert backend, "后端 data-* 集合为空,解析逻辑可能已失效(检查 RE_DATA_TYPE)"
    assert frontend, "前端 data-* 集合为空,解析逻辑可能已失效(检查 RE_FE_DATA)"


# --------------------------------------------------- 契约 3:SSE 线协议词表
def test_sse_frames_stay_within_wire_vocabulary() -> None:
    """所有字面量帧都必须落在 AI SDK 5 的 UIMessageChunk 词表内。

    典型踩坑:把 text-delta 写成 textDelta、把 error 写成 errorMessage,
    前端 useChat 会整帧丢弃——流看起来"卡住",但服务端日志一切正常。
    """
    frame_types = set(RE_FRAME_TYPE.findall(_read(API_PY)))
    assert frame_types, "没有解析到任何 _frame({\"type\": …}),检查解析逻辑"
    illegal = {
        t for t in frame_types
        if t not in WIRE_VOCAB and not t.startswith("data-")
    }
    assert not illegal, (
        f"agent/api.py 里出现了协议外的帧类型:{sorted(illegal)}。"
        f"合法词表见文件头注释:{sorted(WIRE_VOCAB)}(自定义部件须用 data- 前缀)。"
    )


# --------------------------------------------- 契约 4:播报节点名单与图保持一致
def test_announce_nodes_all_exist_in_graph() -> None:
    """ANNOUNCE_NODES 里的每个节点都必须在图里真实注册。

    这名单决定"哪些节点的消息会播报给用户"。节点被改名而名单未同步时,
    该场景**不再播报任何文本**,用户只看到卡片没有说明——静默降级。
    """
    missing = _announce_nodes() - _registered_nodes()
    assert not missing, (
        f"agent/api.py 的 ANNOUNCE_NODES 引用了图中不存在的节点:{sorted(missing)}。"
        f"节点若已改名,请同步 ANNOUNCE_NODES(否则该场景不再播报文本)。"
    )
