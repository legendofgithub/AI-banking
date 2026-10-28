"""make_phone_qr 字体选择与出图冒烟:三平台字体候选不缺、有字体不静默降级。

Windows 专属字体路径曾让 macOS 落到 PIL 默认字体,二维码卡上的中文全成
方块(2026-10-28)。这里把「三平台候选齐全」钉成契约,并验证 _font 在有
候选字体的机器上真的取到 TrueType 而非悄悄退回默认,最后出一张图冒烟。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from PIL import ImageFont

ROOT = Path(__file__).resolve().parents[1]
QR_SCRIPT = ROOT / "scripts" / "make_phone_qr.py"


def _load_qr_script():
    spec = importlib.util.spec_from_file_location("make_phone_qr", QR_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


qr = _load_qr_script()


def test_font_candidates_cover_all_platforms() -> None:
    joined = "\n".join(qr._FONT_CANDIDATES).replace("\\", "/")
    assert "Windows/Fonts" in joined, "字体候选缺 Windows 路径"
    assert "/System/Library/Fonts/" in joined, "字体候选缺 macOS 路径"
    assert "/usr/share/fonts/" in joined, "字体候选缺 Linux 路径"


def test_font_returns_truetype_when_candidate_exists() -> None:
    if not any(Path(p).is_file() for p in qr._FONT_CANDIDATES):
        pytest.skip("本机没有任何候选 CJK 字体(裸 Linux CI 场景)")
    assert isinstance(qr._font(26), ImageFont.FreeTypeFont), (
        "候选字体存在却退到了 PIL 默认字体——二维码卡上的中文字会变方块"
    )


def test_build_qr_writes_png(tmp_path: Path) -> None:
    out = tmp_path / "手机试用二维码.png"
    qr.build_qr("https://example.com/download", out)
    assert out.is_file(), "二维码卡未生成"
    assert out.stat().st_size > 1000, "二维码卡体积异常,疑似空白图"
