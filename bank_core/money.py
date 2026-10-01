"""金额换算：边界一律字符串"元"，内部一律整数"分"。"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def yuan_to_cents(yuan: str | int | float) -> int:
    """'5000' / '99.9' / 5000 -> 分。非法输入抛 ValueError。"""
    if isinstance(yuan, (int, float)):
        yuan = str(yuan)
    try:
        d = Decimal(yuan.strip())
    except InvalidOperation as exc:  # pragma: no cover
        raise ValueError(f"金额格式不合法: {yuan!r}") from exc
    cents = (d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if cents <= 0:
        raise ValueError(f"金额必须为正数: {yuan!r}")
    return int(cents)


def cents_to_yuan(cents: int) -> str:
    """分 -> '5000.00' 字符串（两位小数）。"""
    return f"{(Decimal(cents) / 100).quantize(Decimal('0.01')):f}"
