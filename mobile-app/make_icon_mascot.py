# -*- coding: utf-8 -*-
"""从吉祥物照片生成全套 App 图标(与 make_icon.py 的程序化方案二选一,本脚本产物为当前生效版)。

输入 mobile-app/assets/mascot-raw.png(用户提供的吉祥物图),
第一步自动抹除头顶的微众银行 logo——项目规范禁用微众品牌资产,
答辩作品不得带官方标识;修补方式 = 头部平滑渐变上做横向插值 + 局部柔化。

产出(app/src/main/res/ 下,覆盖 make_icon.py 的产物):
- mipmap-{mdpi..xxxhdpi}/ic_launcher.png   旧版启动图标(浅青底 + 居中吉祥物)
- drawable/ic_launcher_background.png      自适应背景层(浅青纯色)
- drawable/ic_launcher_foreground.png      自适应前景层(吉祥物,留安全区)

重新生成:.venv/Scripts/python.exe mobile-app/make_icon_mascot.py
"""

from PIL import Image, ImageFilter
from pathlib import Path

ROOT = Path(__file__).parent
RES = ROOT / "app" / "src" / "main" / "res"
SRC = ROOT / "assets" / "mascot-raw.png"
CLEAN = ROOT / "assets" / "mascot-clean.png"

DENSITIES = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
ADAPTIVE_OUT = 432  # xxxhdpi 108dp x4

# logo 搜索窗口(相对原图):头顶区域,避开上方蝴蝶结/新芽与下方眼睛
WIN = (0.28, 0.215, 0.72, 0.335)
DARK_THRESH = 185  # 头部亮度 230+,logo 深青 <160


def find_logo_box(im):
    """在头顶窗口内找深色像素包围盒(即 logo),四周留 pad。"""
    w, h = im.size
    win = (
        int(w * WIN[0]),
        int(h * WIN[1]),
        int(w * WIN[2]),
        int(h * WIN[3]),
    )
    region = im.crop(win).convert("L")
    mask = region.point(lambda v: 255 if v < DARK_THRESH else 0)
    bbox = mask.getbbox()
    if bbox is None:
        raise SystemExit("未在头顶窗口内找到 logo,检查窗口/阈值")
    pad = 10
    return (
        max(0, win[0] + bbox[0] - pad),
        max(0, win[1] + bbox[1] - pad),
        min(w, win[0] + bbox[2] + pad),
        min(h, win[1] + bbox[3] + pad),
    )


def inpaint_horizontal(im, box):
    """头部是平滑浅色渐变,对暗像素逐行做左右亮端线性插值即可无痕修补。"""
    px = im.load()
    gray = im.convert("L")
    gp = gray.load()
    x0, y0, x1, y1 = box
    for y in range(y0, y1):
        if all(gp[x, y] >= DARK_THRESH for x in range(x0, x1)):
            continue
        left = x0 - 1
        while left > 0 and gp[left, y] < DARK_THRESH:
            left -= 1
        right = x1 + 1
        while right < im.size[0] - 1 and gp[right, y] < DARK_THRESH:
            right += 1
        c0, c1 = px[left, y], px[right, y]
        span = right - left
        for x in range(left + 1, right):
            t = (x - left) / span
            px[x, y] = tuple(round(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
    # 修补区局部柔化,融进头部渐变(带羽化蒙版,不伤周边)
    m = 14
    zone = (x0 - m, y0 - m, x1 + m, y1 + m)
    soft = im.crop(zone).filter(ImageFilter.GaussianBlur(3))
    mask = Image.new("L", soft.size, 0)
    inner = Image.new("L", (soft.size[0] - 2 * m, soft.size[1] - 2 * m), 255)
    mask.paste(inner, (m, m))
    mask = mask.filter(ImageFilter.GaussianBlur(4))
    im.paste(soft, zone, mask)
    return im


def build(im):
    """生成并写入全套图标资源:全出血照片方案,无合成接缝。

    自适应图标的圆形蒙版直接裁在照片上;吉祥物位于画面中心 ~56% 内,
    蒙版可视区(中心 66%)完全覆盖,不需要前景层(透明占位)。
    """
    for dpi, side in DENSITIES.items():
        im.resize((side, side), Image.LANCZOS).save(
            RES / f"mipmap-{dpi}" / "ic_launcher.png"
        )

    im.resize((ADAPTIVE_OUT, ADAPTIVE_OUT), Image.LANCZOS).save(
        RES / "drawable" / "ic_launcher_background.png"
    )
    Image.new("RGBA", (ADAPTIVE_OUT, ADAPTIVE_OUT), (0, 0, 0, 0)).save(
        RES / "drawable" / "ic_launcher_foreground.png"
    )
    print(f"图标已生成(全出血照片): 源={im.size[0]}x{im.size[1]}")


def crop_watermark(im):
    """去掉右下角「豆包AI生成」水印:裁掉底部 10.5% 后取居中正方形。"""
    w, h = im.size
    kept = int(h * 0.895)
    side = kept
    x0 = (w - side) // 2
    return im.crop((x0, 0, x0 + side, kept))


def main():
    im = Image.open(SRC).convert("RGB")
    box = find_logo_box(im)
    print(f"logo 区域: {box}")
    im = inpaint_horizontal(im, box)
    im = crop_watermark(im)
    im.save(CLEAN)
    build(im)
    print(f"干净原图已存 {CLEAN}")


if __name__ == "__main__":
    main()
