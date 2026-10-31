"""ClawBot 接入一键准备:检测/安装 OpenClaw + 微信插件 + 转发技能。

用法(在项目根目录):
    .venv/Scripts/python.exe scripts/setup_clawbot.py          # 安装/修复
    .venv/Scripts/python.exe scripts/setup_clawbot.py --check  # 只体检,不改动

做四件事(幂等,可反复跑):
  1. 检查 Node 版本,据此挑 OpenClaw 版本(新版要 Node>=24.22.1,否则用 2026.6.35)
  2. 安装 OpenClaw 到独立前缀 ~/openclaw-new(npm -g 在部分机器会被静默挡下,
     失败时自动回退到独立前缀——这是实测踩过的坑)
  3. 安装腾讯官方微信插件 @tencent-weixin/openclaw-weixin
  4. 把本仓库 integrations/openclaw/skills/bankagent-relay 复制到
     ~/.openclaw/workspace/skills/bankagent-relay

装完之后仍需**人工三步**(脚本会打印提示):
  ① 给 OpenClaw 配一个可用模型(默认 deepseek/deepseek-chat)
  ② 在 webui 顶栏「微信 ClawBot」按钮扫码(或跑 openclaw channels login)
  ③ 在微信里发「绑定 手机号」绑定演示账户
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL_SRC = ROOT / "integrations" / "openclaw" / "skills" / "bankagent-relay"
PREFIX = Path.home() / "openclaw-new"
OPENCLAW_DIR = Path.home() / ".openclaw"
PLUGIN = "@tencent-weixin/openclaw-weixin"
CHANNEL = "openclaw-weixin"
AGENT_API = os.environ.get("AGENT_API_PORT", "8800")

# 插件要求 OpenClaw >=2026.5.12;新版 OpenClaw 又要求 Node>=24.22.1。
# Node 不够时退到 2026.6.35(要求 >=22.19),功能对 ClawBot 已足够。
FALLBACK_VERSION = "2026.6.35"
NODE_NEW = (24, 22, 1)
NODE_FALLBACK = (22, 19, 0)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def say(msg: str) -> None:
    print(msg, flush=True)


def node_version() -> tuple[tuple[int, int, int], str] | None:
    exe = shutil.which("node")
    if not exe:
        return None
    out = run([exe, "--version"])
    m = re.match(r"v(\d+)\.(\d+)\.(\d+)", out.stdout.strip())
    if not m:
        return None
    return tuple(int(x) for x in m.groups()), exe  # type: ignore[return-value]


def npm_exe() -> str | None:
    """npm 的 .cmd 垫片在 Windows 上偶发不可用,优先用 node 直调 npm-cli.js。"""
    node = shutil.which("node")
    if node:
        for rel in ("node_modules/npm/bin/npm-cli.js",):
            cand = Path(node).parent / rel
            if cand.exists():
                return str(cand)
    return shutil.which("npm")


def openclaw_cli() -> Path | None:
    """定位 openclaw 的 dist/index.js(与 agent/clawbot.py 的探测保持一致)。"""
    for cand in (
        PREFIX / "node_modules" / "openclaw" / "dist" / "index.js",
        Path.home() / "AppData" / "Roaming" / "npm" / "node_modules"
        / "openclaw" / "dist" / "index.js",
    ):
        if cand.exists():
            return cand
    return None


def openclaw_version(node: str, cli: Path) -> str:
    out = run([node, str(cli), "--version"])
    return (out.stdout or out.stderr).strip().splitlines()[0] if out.stdout \
        else "?"


def install_openclaw(node: str, ver: tuple[int, int, int]) -> Path | None:
    npm = npm_exe()
    if npm is None:
        say("  ✗ 找不到 npm,请先安装 Node.js")
        return None
    ver_s = ".".join(map(str, ver))
    target = "latest" if ver >= NODE_NEW else FALLBACK_VERSION
    say(f"  · 目标 OpenClaw 版本:{target}(Node {ver_s})")

    # 先试全局安装(用户已装过的机器通常走这条,升级更自然)
    args = [node, npm, "install", "-g", f"openclaw@{target}"] if npm.endswith(
        ".js") else [npm, "install", "-g", f"openclaw@{target}"]
    say("  · 尝试全局安装…")
    res = run(args)
    if res.returncode != 0:
        say(f"    全局安装失败(常见:npm 全局目录被占用),回退独立前缀 {PREFIX}")
        PREFIX.mkdir(parents=True, exist_ok=True)
        local = [node, npm, "install", "--prefix", str(PREFIX),
                 f"openclaw@{target}"] if npm.endswith(".js") else \
                [npm, "install", "--prefix", str(PREFIX), f"openclaw@{target}"]
        res = run(local)
        if res.returncode != 0:
            say(f"  ✗ 安装失败:{res.stderr[-400:]}")
            return None
    return openclaw_cli()


def install_plugin(node: str, cli: Path) -> bool:
    say("  · 安装微信插件…")
    res = run([node, str(cli), "plugins", "install", PLUGIN])
    if res.returncode != 0:
        say(f"  ✗ 插件安装失败:{res.stderr[-400:]}")
        return False
    return True


def install_skill() -> bool:
    if not SKILL_SRC.exists():
        say(f"  ✗ 仓库里找不到技能源目录:{SKILL_SRC}")
        return False
    dst = OPENCLAW_DIR / "workspace" / "skills" / "bankagent-relay"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("SKILL.md", "relay.py"):
        shutil.copy2(SKILL_SRC / name, dst / name)
    say(f"  ✓ 技能已安装到 {dst}")
    return True


def probe_backend() -> str:
    url = f"http://127.0.0.1:{AGENT_API}/api/wechat/clawbot/state"
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        return f"{d.get('status')} / {d.get('message') or '-'}"
    except (urllib.error.URLError, OSError, ValueError):
        return "后端未启动(8800 不可达)"
    except Exception:  # noqa: BLE001
        return "状态未知"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只体检,不安装")
    args = ap.parse_args()

    say("=" * 62)
    say(" ClawBot 接入体检/安装")
    say("=" * 62)

    nv = node_version()
    if nv is None:
        say("✗ 未检测到 Node.js。请先安装 Node 22+ (https://nodejs.org)")
        return 2
    ver, node = nv
    say(f"✓ Node {'.'.join(map(str, ver))}  ({node})")
    if ver < NODE_FALLBACK:
        say(f"✗ Node 版本过低:ClawBot 链路至少要 {'.'.join(map(str, NODE_FALLBACK))}")
        return 2
    if ver < NODE_NEW:
        say(f"  · 低于 {'/'.join(map(str, NODE_NEW))}:将使用 OpenClaw {FALLBACK_VERSION}")
        say("  · 想用最新版请先升级 Node")

    cli = openclaw_cli()
    if cli:
        say(f"✓ OpenClaw 已安装:{openclaw_version(node, cli)}")
        say(f"  {cli}")
    else:
        say("· OpenClaw 未安装")
        if args.check:
            say("  (--check 模式不安装)")
        else:
            cli = install_openclaw(node, ver)
            if cli is None:
                return 1
            say(f"✓ OpenClaw 已安装:{openclaw_version(node, cli)}")

    if cli and not args.check:
        install_plugin(node, cli)
    skill_dst = OPENCLAW_DIR / "workspace" / "skills" / "bankagent-relay" / "SKILL.md"
    if skill_dst.exists():
        say(f"✓ 转发技能已就位:{skill_dst}")
    elif not args.check:
        install_skill()
    else:
        say("· 转发技能未安装(--check 不改动)")

    say(f"✓ BankAgent 后端(8800):{probe_backend()}")
    say("-" * 62)
    say("装完仍要做的人工三步:")
    say("  ① 配模型:openclaw models set deepseek/deepseek-chat")
    say("     (需先在 ~/.openclaw/openclaw.json 的 models.providers 里填好该供应商的")
    say("      baseUrl/apiKey,或跑 openclaw configure 交互配置)")
    say("  ② 扫码:打开 webui(http://127.0.0.1:3000)顶栏「微信 ClawBot」按钮")
    say("     或终端跑:openclaw channels login --channel openclaw-weixin")
    say("  ③ 绑定:在微信 ClawBot 对话里发「绑定 你的手机号」")
    say("-" * 62)
    say("排查用:openclaw channels status / openclaw sessions --json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
