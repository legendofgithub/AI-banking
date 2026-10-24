"""Token 估算函数测试(施工文档条目 2 的公共底座)。

为什么先测这个:`estimate_tokens` / `estimate_messages_tokens` 是条目 4(L2 阈值压缩)
的前置件,同时是**所有假模型路径都会走到的回落分支**——FakeMessagesListChatModel
产出的 chunk 不带 `usage_metadata`,端点计量拿不到就必须靠它。因此它必须是
纯函数:无 I/O、无日志、无随机,相同输入必然相同输出。

本文件全部同步用例,不建库、不联网、不启动图。
"""
from __future__ import annotations

import agent.llm as llm_mod


# ── estimate_tokens:空与非字符串 ──────────────────────────────────────

def test_none_and_empty_are_zero():
    assert llm_mod.estimate_tokens(None) == 0
    assert llm_mod.estimate_tokens("") == 0


def test_non_string_is_coerced_not_crash():
    """计费口径里混进 int/Decimal/None 都不许抛异常(金额一律整数分,可能直接传进来)。"""
    assert llm_mod.estimate_tokens(12345) == llm_mod.estimate_tokens("12345")
    assert llm_mod.estimate_tokens(0) == llm_mod.estimate_tokens("0")


# ── estimate_tokens:口径 ──────────────────────────────────────────────

def test_ascii_is_quarter_token_per_char():
    """纯 ASCII:4 字符 ≈ 1 token,向上取整。"""
    assert llm_mod.estimate_tokens("a") == 1        # ceil(0.25)
    assert llm_mod.estimate_tokens("abcd") == 1     # ceil(1.0)
    assert llm_mod.estimate_tokens("abcde") == 2    # ceil(1.25)
    assert llm_mod.estimate_tokens("hello world") == 3   # 11 字符 → ceil(2.75)


def test_cjk_is_one_token_per_char():
    assert llm_mod.estimate_tokens("你") == 1
    assert llm_mod.estimate_tokens("你好") == 2
    assert llm_mod.estimate_tokens("向张三转账五百元") == 8


def test_fullwidth_punctuation_counts_as_cjk():
    """全角标点与 CJK 标点区按 1 token/字符,不能漏到 0.25 那一档。"""
    assert llm_mod.estimate_tokens("，。！") == 3
    assert llm_mod.estimate_tokens("「」") == 2


def test_mixed_chinese_ascii():
    """中英混排按字符分别计价:'转账500元' → 3 CJK + 3 ASCII。"""
    # CJK: 转/账/元 = 3;其余 '500' = 3 → 3 + 0.75 → ceil = 4
    assert llm_mod.estimate_tokens("转账500元") == 4


def test_magnitude_is_right_for_large_text():
    """只要求量级正确(用于阈值判断,不用于计费):1 万中文 → 1 万 token。"""
    assert llm_mod.estimate_tokens("苹" * 10_000) == 10_000
    assert llm_mod.estimate_tokens("x" * 10_000) == 2_500


# ── 纯函数性质 ────────────────────────────────────────────────────────

def test_is_deterministic_and_side_effect_free(caplog):
    """同一输入连续调用结果必须一致;且不得产生任何日志(会被放大到 413 条用例)。"""
    text = "帮我看看这个月的账单,房租是不是又涨了"
    first = llm_mod.estimate_tokens(text)
    assert first == llm_mod.estimate_tokens(text) == llm_mod.estimate_tokens(text)
    assert first > 0
    assert caplog.records == [], "估算函数不得写日志——全量测试里每条用例都会走它"


# ── estimate_messages_tokens ──────────────────────────────────────────

class _Msg:
    """最小消息替身:只要带 .content 就能算,不需要 import langchain。"""

    def __init__(self, content: str):
        self.content = content


def test_messages_tokens_sums_content_plus_overhead():
    msgs = [_Msg("你好"), _Msg("abcd")]
    # (2 CJK) + (1 ASCII) + 2 条 × 4 开销
    assert llm_mod.estimate_messages_tokens(msgs) == 2 + 1 + 8


def test_messages_tokens_handles_empty_and_plain_objects():
    assert llm_mod.estimate_messages_tokens([]) == 0
    assert llm_mod.estimate_messages_tokens(None) == 0
    # 没有 .content 的裸对象:退化成按字符串算,不抛异常
    assert llm_mod.estimate_messages_tokens(["你好"]) == llm_mod.estimate_tokens("你好") + 4


def test_messages_tokens_grows_with_history():
    """30 轮历史的估算必须显著大于近 4 轮——这是条目 4 L2 阈值判断的基础。"""
    short = [_Msg("你好")] * 4
    long_ = [_Msg("这是一条比较长的历史消息,里面有账单分析的 JSON 结果")] * 30
    assert llm_mod.estimate_messages_tokens(long_) > llm_mod.estimate_messages_tokens(short) * 5


# ── 对抗性探测(AGENTS.md 铁律 6:宣布完成前必须真跑一遍)─────────────
# 探测 1:超长输入会不会拖垮全量测试(每条用例都会走这条分支)
# 探测 2:异形输入(Decimal/bytes/emoji/列表)会不会抛异常
# 探测 3:阈值边界附近是否稳定(条目 4 的 L2 触发点就在 6000 附近)

def test_probe_1_million_chars_does_not_blow_up():
    """探测 1:100 万字符必须秒级返回且量级正确——不能有 O(n²) 或隐式全量拷贝。"""
    import time

    text = "转账" * 500_000          # 100 万 CJK 字符
    t0 = time.perf_counter()
    got = llm_mod.estimate_tokens(text)
    elapsed = time.perf_counter() - t0
    assert got == 1_000_000, f"量级错了: {got}"
    assert elapsed < 3.0, f"太慢({elapsed:.2f}s),全量测试会被拖垮"


def test_probe_2_exotic_inputs_never_raise():
    """探测 2:金额/二进制/emoji/容器都可能混进 content,一律不许抛异常。"""
    from decimal import Decimal

    for weird in (Decimal("1234.56"), b"\xe4\xbd\xa0\xe5\xa5\xbd", "🙂🙂",
                  ["a", "b"], {"content": "x"}, object(), True):
        got = llm_mod.estimate_tokens(weird)
        assert isinstance(got, int) and got >= 0, f"{weird!r} 得到非法值 {got!r}"
    # 消息列表里混进 None / 非消息对象同样不许炸
    assert isinstance(llm_mod.estimate_messages_tokens([None, _Msg("你好"), 42]), int)


def test_probe_3_threshold_boundary_is_stable():
    """探测 3:L2 的触发判断是 `估算 > 6000`,边界两侧必须稳定可分且不抖动。"""
    just_under = "苹" * 5_996         # + 少量开销仍应 < 6000
    just_over = "苹" * 6_001          # 应 > 6000
    assert llm_mod.estimate_tokens(just_under) < 6000
    assert llm_mod.estimate_tokens(just_over) > 6000
    # 同一份文本反复估算必须字节级一致,否则 L2 会在边界上反复开关
    vals = {llm_mod.estimate_tokens(just_under) for _ in range(5)}
    assert len(vals) == 1
