"""首次运行自举:从模板生成本地环境文件 webui/.env.local。

背景(2026-10-09,仓库转公开后的"一个 Key 即用"需求):
- 队友 clone 后唯一必填项是 LLM API Key,其余全部自动补齐:
  * AUTH_SECRET 每台机器随机生成(登录会话签名,绝不共用模板里的固定值);
  * 端点/模型/端口走 webui/.env.example 里的默认值(DeepSeek 组合,已实测);
- 生成的 .env.local 被根 .gitignore 的 `.env*` 通配拦截,机制上杜绝
  "把 Key 提交进公开仓库"。守卫测试:tests/test_env_template.py。

用法:
    .venv/Scripts/python.exe scripts/setup_env.py               # 交互式,提示粘贴 Key
    .venv/Scripts/python.exe scripts/setup_env.py --api-key sk-xxx --non-interactive
    .venv/Scripts/python.exe scripts/setup_env.py --force       # 重生成(旧会话全部失效)

    `启动演示.bat` 在检测到 webui/.env.local 缺失时会自动调用本脚本。

    --template/--out 仅测试用(tests/test_env_template.py 指到临时目录),平时不传。

安全约定:本脚本不回显 Key 明文;交互输入走 stdin,不进 shell 历史。
"""

from __future__ import annotations

import argparse
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "webui" / ".env.example"
OUT = ROOT / "webui" / ".env.local"

# 只允许替换这两个键的值;模板其余行(含全部默认值与注释)原样保留
_RE_SECRET = re.compile(r"^(AUTH_SECRET=).*$", re.M)
_RE_API_KEY = re.compile(r"^(ZAI_API_KEY=).*$", re.M)

PROMPT = (
    "\n[一个 Key 即用] 请粘贴 LLM API Key(当前默认端点 DeepSeek):\n"
    "  - 粘贴后回车写入 webui/.env.local(该文件已被 .gitignore 拦截,不会入库);\n"
    "  - 直接回车跳过:之后可编辑该文件,或在 webui 界面『设置』里填 BYOK Key。\n"
    "> "
)


def build_local_env(template_text: str, *, api_key: str,
                    auth_secret: str | None = None) -> str:
    """替换模板中的 AUTH_SECRET / ZAI_API_KEY 两行,其余原样返回。"""
    secret = auth_secret if auth_secret is not None else secrets.token_urlsafe(32)
    text, n1 = _RE_SECRET.subn(lambda m: m.group(1) + secret, template_text)
    if n1 != 1:
        raise SystemExit(f"[错误] 模板里找不到 AUTH_SECRET= 行(模板被改坏?)")
    text, n2 = _RE_API_KEY.subn(lambda m: m.group(1) + api_key, text)
    if n2 != 1:
        raise SystemExit(f"[错误] 模板里找不到 ZAI_API_KEY= 行(模板被改坏?)")
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description="从 webui/.env.example 生成本地 .env.local")
    ap.add_argument("--template", type=Path, default=TEMPLATE)
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--api-key", default=None,
                    help="非交互式传入 Key(不传则交互询问;--non-interactive 时留空)")
    ap.add_argument("--non-interactive", action="store_true",
                    help="不询问,Key 缺省留空(CI/测试用)")
    ap.add_argument("--force", action="store_true",
                    help="覆盖已存在的 .env.local(会重生成 AUTH_SECRET,旧登录会话全部失效)")
    args = ap.parse_args()

    if args.out.exists() and not args.force:
        print(f"[跳过] {args.out} 已存在(要重新生成请加 --force)。")
        return 0

    template_text = args.template.read_text(encoding="utf-8")

    api_key = (args.api_key or "").strip()
    if not api_key and not args.non_interactive:
        try:
            api_key = input(PROMPT).strip()
        except EOFError:
            api_key = ""

    text = build_local_env(template_text, api_key=api_key)
    args.out.write_text(text, encoding="utf-8", newline="\n")

    print(f"[完成] 已生成 {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    print("       AUTH_SECRET : 已随机生成(本机专属,勿外传)")
    print(f"       ZAI_API_KEY : {'已写入' if api_key else '留空 —— 请编辑该文件或在 webui 界面设置里填 BYOK Key'}")
    print("       端点/模型/端口: 取自模板默认值(DeepSeek 组合)")
    print("       该文件被 .gitignore 拦截,不会被提交。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
