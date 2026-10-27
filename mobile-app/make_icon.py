# -*- coding: utf-8 -*-
"""生成手机 App 图标:红蓝品牌渐变 + 白色银行建筑主体。

产出(mobile-app/app/src/main/res/ 下):
- mipmap-{mdpi..xxxhdpi}/ic_launcher.png  旧版启动图标(全构图圆角方形)
- drawable/ic_launcher_background.png     自适应图标背景层(全出血渐变)
- drawable/ic_launcher_foreground.png     自适应图标前景层(白色主体,留安全区)

设计对齐 webui 品牌色:红 #D32E19 / 紫 #6C97E9 / 蓝 #3E60CE。
重新生成:.venv/Scripts/python.exe mobile-app/make_icon.py
"""

from PIL import Image, ImageDraw, ImageFilter
from pathlib import Path

RES = Path(__file__).parent / "app" / "src" / "main" / "res"

BRAND_RED = (211, 46, 25)     # --brand-red
BRAND_VIOLET = (108, 151, 233)  # --brand-violet
BRAND_BLUE = (62, 96, 206)    # --brand-blue

# 品牌红占视觉主导:前 55% 走红→紫,后 45% 走紫→蓝
STOPS = [(0.0, BRAND_RED), (0.55, BRAND_VIOLET), (1.0, BRAND_BLUE)]


def _lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient(size):
    """对角线(t = (x+y)/2)多段线性渐变,256 取样再放大保证平滑。"""
    n = 256
    small = Image.new("RGB", (n, n))
    px = small.load()
    for y in range(n):
        for x in range(n):
            t = (x + y) / (2 * (n - 1))
            for (t0, c0), (t1, c1) in zip(STOPS, STOPS[1:]):
                if t <= t1:
                    px[x, y] = _lerp(c0, c1, (t - t0) / (t1 - t0))
                    break
    return small.resize((size, size), Image.BILINEAR)


def draw_landmark(draw, cx, cy, w):
    """白色实体银行主体(山花+檐部+四柱+台基+台阶),归一化几何按 w 缩放。

    四柱节奏与 lucide landmark 一致,实体化后小尺寸下仍可辨识。
    """
    h = w * 1.1  # 主体略高瘦,台阶压底
    left, top = cx - w / 2, cy - h / 2
    white = (255, 255, 255, 255)

    def rect(nx0, ny0, nx1, ny1):
        draw.rectangle(
            [left + nx0 * w, top + ny0 * h, left + nx1 * w, top + ny1 * h],
            fill=white,
        )

    # 山花(等腰三角,坡度偏陡更古典)+ 出檐
    draw.polygon(
        [
            (left + 0.5 * w, top),
            (left + 0.99 * w, top + 0.26 * h),
            (left + 0.01 * w, top + 0.26 * h),
        ],
        fill=white,
    )
    # 檐部
    rect(0.02, 0.30, 0.98, 0.38)
    # 四柱(修长柱身,含柱头微出宽)
    for c in (0.16, 0.387, 0.613, 0.84):
        rect(c - 0.052, 0.42, c + 0.052, 0.77)
    # 台基 + 两级台阶
    rect(0.03, 0.80, 0.97, 0.87)
    rect(0.09, 0.91, 0.91, 0.965)
    rect(0.17, 0.985, 0.83, 1.0)


def compose_full(size=1024):
    """旧版全构图:圆角方形渐变底 + 主体居中 + 柔光/内描边细节。"""
    S = 1024  # 超采样
    base = gradient(S).convert("RGBA")

    # 左上柔光,给渐变一点体积感
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-S * 0.25, -S * 0.35, S * 0.75, S * 0.45], fill=(255, 255, 255, 26))
    glow = glow.filter(ImageFilter.GaussianBlur(S // 18))
    base = Image.alpha_composite(base, glow)

    # 白色主体,占画布约 52%
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw_landmark(ImageDraw.Draw(layer), S / 2, S / 2, S * 0.5)
    base = Image.alpha_composite(base, layer)

    # 内描边 hairline(与 webui gradient-hairline 呼应)
    d = ImageDraw.Draw(base)
    inset = S // 64
    d.rounded_rectangle(
        [inset, inset, S - inset, S - inset],
        radius=S // 5,
        outline=(255, 255, 255, 60),
        width=S // 170,
    )

    # 圆角遮罩
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S, S], radius=S // 5, fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(base, (0, 0), mask)
    return out.resize((size, size), Image.LANCZOS)


def compose_foreground(size=432):
    """自适应图标前景:透明底 + 55% 宽主体(66/108 安全区内)。"""
    S = 1024
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw_landmark(ImageDraw.Draw(layer), S / 2, S / 2, S * 0.55)
    return layer.resize((size, size), Image.LANCZOS)


def main():
    # 旧版 mipmap(48/72/96/144/192)
    for dpi, px in [("mdpi", 48), ("hdpi", 72), ("xhdpi", 96),
                    ("xxhdpi", 144), ("xxxhdpi", 192)]:
        d = RES / f"mipmap-{dpi}"
        d.mkdir(parents=True, exist_ok=True)
        compose_full(px).save(d / "ic_launcher.png")
    # 自适应图标背景(全出血渐变 432)与前景
    gradient(432).save(RES / "drawable" / "ic_launcher_background.png")
    compose_foreground(432).save(RES / "drawable" / "ic_launcher_foreground.png")
    print("icons written to", RES)


if __name__ == "__main__":
    main()
