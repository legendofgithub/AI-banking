"""办结追问的确定性兜底单测(与图执行无关,毫秒级)。

背景:播报词由 LLM 生成,提示词已要求业务办结后追问同类业务;
本模块保证模型漏问时也能补上一句,且模型已问过时不重复。
"""

from agent.graph import _fallback_announcement, _with_followup


def test_transfer_executed_followup_appended():
    text = _with_followup("已向张三转账 500.00 元。", {"kind": "executed"})
    assert text.endswith("还需要给其他人转账吗?")
    assert "已向张三转账 500.00 元。" in text


def test_subscription_cancelled_followup_appended():
    text = _with_followup("自动扣费已取消,下一期起不再扣款。",
                          {"kind": "sub_cancelled"})
    assert text.endswith("还需要取消其他订阅吗?")


def test_model_already_asked_not_duplicated():
    text = "已取消。还需要取消其他订阅吗?"
    assert _with_followup(text, {"kind": "sub_cancelled"}) == text


def test_other_notices_untouched():
    for kind in ("sub_list", "cancelled", "blocked", "sub_cancel_kept"):
        text = "处理完成。"
        assert _with_followup(text, {"kind": kind}) == text


def test_fallback_still_carries_order_id():
    # 兜底文案带单号的纪律不被追问逻辑破坏
    text = _fallback_announcement({"kind": "executed", "order": {"id": 9}})
    assert "订单号 9" in text
