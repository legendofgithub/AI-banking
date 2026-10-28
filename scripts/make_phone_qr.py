"""生成手机扫码试用的二维码(含演示账号提示),并自动打开图片。

用法:
    .venv/Scripts/python.exe scripts/make_phone_qr.py            # 自动探测局域网 IP
    .venv/Scripts/python.exe scripts/make_phone_qr.py --url http://192.168.1.5:3000
    ... make_phone_qr.py --out 路径.png --no-open

为什么每次重新生成而不是提交一张固定二维码进仓库:
局域网 IP 由 DHCP 分配,换 WiFi/热点后会变,旧二维码扫出来是死链。
所以仓库只提交本脚本,二维码 PNG 产物默认落在项目根并被 .gitignore 拦截。

依赖:pip install qrcode pillow(纯演示工具链,已进 requirements.txt)。
"""

from __future__ import annotations

import argparse
import socket
import sys
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "手机试用二维码.png"

TITLE = "AI Banking 智能银行助手 · 手机扫码试用"
CREDS = "演示账号 13800002233 / Demo@12345  ·  支付密码 888888"


def detect_lan_ip() -> str:
    """取本机在局域网里的 IP(UDP connect 不真正发包,只让路由层选出口网卡)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# 中文字体候选:各平台常见 CJK 字体路径,逐个试开,第一个能用的生效。
# 全部落空才退 PIL 默认字体——它没有中文字形,二维码卡上的中文会渲染成
# 方块(2026-10-28 在 macOS 上踩到:原先只认 C:\Windows\Fonts)。
_FONT_CANDIDATES = (
    # Windows: 微软雅黑 / 宋体
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simsun.ttc",
    # macOS: 苹方(近年必有)/ 黑体-简 / 冬青黑体
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    # Linux: Noto Sans CJK 常见安装位置(Debian/Ubuntu 与 Fedora/Arch)
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for cand in _FONT_CANDIDATES:
        if Path(cand).is_file():
            try:
                return ImageFont.truetype(cand, size)
            except OSError:
                continue
    return ImageFont.load_default()


def build_qr(url: str, out: Path, *, box_size: int = 12) -> None:
    qr = qrcode.QRCode(
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=box_size,
        border=4,  # 规范要求的静区,别压缩,否则贴图/屏幕反光下扫不出
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="#1a1a1a", back_color="white").convert("RGB")
    qr_w, qr_h = img.size

    title_f, url_f, creds_f = _font(30), _font(26), _font(22)
    pad = 40
    canvas_w = max(qr_w, 760) + pad * 2
    canvas_h = pad + 44 + qr_h + 30 + 40 + 34 + pad
    canvas = Image.new("RGB", (canvas_w, canvas_h), "white")
    draw = ImageDraw.Draw(canvas)
    canvas.paste(img, ((canvas_w - qr_w) // 2, pad + 44))

    def center(text: str, y: int, font, fill: str) -> None:
        w = draw.textlength(text, font=font)
        draw.text(((canvas_w - w) / 2, y), text, font=font, fill=fill)

    center(TITLE, pad, title_f, "#b45309")
    center(url, pad + 44 + qr_h + 26, url_f, "#1a1a1a")
    center(CREDS, pad + 44 + qr_h + 26 + 40, creds_f, "#666666")

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, "PNG")
    print(f"[完成] {out}")
    print(f"       扫码地址: {url}")
    print(f"       手机需与电脑同一 WiFi;连不上先查系统防火墙对本机入站连接的限制")
    print(f"       「AI Banking Mobile Demo 3000/8800」是否存在(见 AGENTS.md 环境坑)。")


def main() -> int:
    ap = argparse.ArgumentParser(description="生成手机扫码试用二维码")
    ap.add_argument("--url", default=None, help="默认 http://<局域网IP>:3000")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-open", action="store_true", help="不自动打开图片")
    args = ap.parse_args()

    url = args.url or f"http://{detect_lan_ip()}:3000"
    build_qr(url, args.out)
    if not args.no_open and sys.platform == "win32":
        import os

        os.startfile(args.out)  # noqa: PTH123 - Windows 默认看图器
    return 0


if __name__ == "__main__":
    sys.exit(main())
