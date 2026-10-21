#!/usr/bin/env python
"""AI Banking 一键启动器(全量重启 + 后台静默)。

旧版 启动演示.bat 用 `start /min` 起四个服务,任务栏会长期挂四个 cmd 窗口。
本启动器只保留一个前台窗口显示启动进度,四个服务改为无窗口后台进程,
标准输出/错误落到 logs/<服务>.log 与 logs/<服务>.err.log。关闭启动器窗口
不会杀掉后台服务,停止仍用 停止演示.bat。

保留的既有语义(AGENTS.md):
- 全量重启:先按端口 PID 停旧进程再起新的(Python / next 都不热加载);
- 只按 PID 杀,绝不 taskkill /IM node.exe 全杀;
- 后端一律 .venv\\Scripts\\python.exe;
- ZAI_* 从 webui/.env.local 回填(Key / 端点 / 模型必须同源);
- 先 next build 再 next start,不走 next dev。

用法:
    启动演示.bat                    # 全量构建 + 启动
    启动演示.bat --skip-build       # 跳过前端构建(仅重启服务)
    启动演示.bat --no-open          # 不自动打开浏览器(调试/自动化用)
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
WEBUI = ROOT / "webui"
LOG_DIR = ROOT / "logs"
PORTS = (8800, 8789, 8788, 3000)
ZAI_KEYS = ("ZAI_API_KEY", "ZAI_BASE_URL", "ZAI_MODEL")

# 控制台程序专用:子进程完全无窗口(DETACHED 语义由独立隐藏 console 达成)。
CREATE_NO_WINDOW = 0x08000000


def parse_listener_pids(netstat_text: str) -> dict[int, list[int]]:
    """从 `netstat -ano` 输出提取 {端口: [PID,...]},只认 LISTENING 行。

    本地地址可能是 0.0.0.0:3000 / 127.0.0.1:3000 / [::]:3000;按最后一个
    冒号取端口并做精确匹配,避免 3000 误伤 30000。同一端口的 IPv4/IPv6
    两行会指向同一 PID,这里去重。
    """
    found: dict[int, list[int]] = {}
    for line in netstat_text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[-2].upper() != "LISTENING":
            continue
        local = parts[1]
        if ":" not in local:
            continue
        port_text = local.rsplit(":", 1)[1]
        if not port_text.isdigit():
            continue
        port = int(port_text)
        if port not in PORTS:
            continue
        pid_text = parts[-1]
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        pids = found.setdefault(port, [])
        if pid not in pids:
            pids.append(pid)
    return found


def stop_old_services(rounds: int = 2) -> None:
    """按端口 PID 停旧进程。两轮是旧脚本的既有设计:防止 next dev 的父进程重拉。"""
    print("[停止] 清理旧进程...")
    for index in range(rounds):
        try:
            raw = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True,
                text=True,
                errors="replace",
                timeout=30,
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"  [警告] netstat 执行失败: {exc}")
            return
        listeners = parse_listener_pids(raw)
        if not listeners:
            print("  (没有发现占用演示端口的进程)")
        for port in PORTS:
            for pid in listeners.get(port, []):
                print(f"  端口 {port} -> 停止 PID {pid}")
                subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                )
        if index + 1 < rounds:
            time.sleep(1.5)


def load_env_subset(path: Path) -> dict[str, str]:
    """读取 .env.local 里的 ZAI_*;只认 KEY=VALUE,忽略注释/空行/export。"""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key not in ZAI_KEYS:
            continue
        values[key] = value.strip().strip('"').strip("'")
    return values


def ensure_data() -> None:
    """首次运行自举:生成 .env.local、播种演示库(与旧脚本口径一致)。"""
    env_file = WEBUI / ".env.local"
    if not env_file.exists():
        print()
        print("[首次运行] webui/.env.local 不存在,进入一次式配置(全程只差粘贴一个 API Key)...")
        subprocess.run([str(PY), "scripts/setup_env.py"], cwd=str(ROOT))
    if not (ROOT / "data" / "bank.db").exists():
        print("[首次运行] 演示库 data/bank.db 不存在,播种 12 个月种子数据...")
        rc = subprocess.run(
            [str(PY), "-m", "bank_core.seed"], cwd=str(ROOT)
        ).returncode
        if rc != 0:
            print("  [警告] 播种失败,服务可能没有演示数据(见上方输出)")


def apply_zai_env(values: dict[str, str]) -> None:
    """把 .env.local 的 ZAI_* 回填进子进程环境(已有的其它环境变量不动)。"""
    for key in ZAI_KEYS:
        if key in values:
            os.environ[key] = values[key]
    if values.get("ZAI_BASE_URL"):
        print(
            "[配置] ZAI_* 取自 webui/.env.local  "
            f"端点={values.get('ZAI_BASE_URL', '')}  "
            f"模型={values.get('ZAI_MODEL', '')}"
        )
    if not values.get("ZAI_API_KEY"):
        print("[警告] 未找到 ZAI_API_KEY —— 对话会报\"未配置 API Key\"。")
        print("       请在 webui/.env.local 里配置,或在 webui 界面设置里填 BYOK Key。")


def build_frontend() -> bool:
    print()
    print("[构建] 前端生产包(约 1-2 分钟请勿关闭本窗口)...")
    env = os.environ.copy()
    env["NODE_OPTIONS"] = "--max-old-space-size=2048"
    rc = subprocess.run(
        ["cmd.exe", "/c", "npx next build"], cwd=str(WEBUI), env=env
    ).returncode
    if rc != 0:
        print("[警告] 前端构建失败,将尝试用上一次的产物启动。")
        return False
    return True


def start_hidden(name: str, args: list[str], cwd: Path) -> subprocess.Popen:
    """无窗口启动一个服务,stdout/stderr 追加到 logs/<name>.{log,err.log}。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"\n===== {stamp} 启动 {name} =====\n".encode()
    with open(LOG_DIR / f"{name}.log", "ab") as out, open(
        LOG_DIR / f"{name}.err.log", "ab"
    ) as err:
        out.write(header)
        out.flush()
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=CREATE_NO_WINDOW,
        )
    print(f"[启动] {name} (隐藏窗口,日志 logs/{name}.log)")
    return proc


def wait_tcp(port: int, timeout: float, label: str) -> bool:
    """轮询 TCP 监听状态;服务起来要几秒,别用固定 sleep 赌。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                print(f"  [就绪] {label} -> http://127.0.0.1:{port}")
                return True
        except OSError:
            time.sleep(0.5)
    print(f"  [失败] {label} 在 {int(timeout)} 秒内未监听,请查看 logs/{label}.err.log")
    return False


def http_get(port: int, path: str, timeout: float = 3) -> tuple[int, str] | None:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read(8192).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        conn.close()


def lan_host() -> str:
    """8800 探测的本机局域网 IP(手机扫码/收藏用的地址);取不到返回空串。"""
    result = http_get(8800, "/api/lan-info")
    if not result or result[0] != 200:
        return ""
    try:
        host = str(json.loads(result[1]).get("host") or "")
    except (ValueError, AttributeError):
        return ""
    if host in {"", "127.0.0.1", "0.0.0.0"}:
        return ""
    return host


def print_summary(ok: bool) -> None:
    print()
    print("=" * 44)
    if not ok:
        print("  启动未完全成功:有服务没监听端口")
        print(f"  请查看 {LOG_DIR} 下的 .err.log,或运行 停止演示.bat 后重试")
        print("=" * 44)
        return
    print("  全部服务已就绪")
    print("  前端对话:   http://127.0.0.1:3000")
    print("  管理后台:   http://127.0.0.1:8789")
    print("  网银演示:   http://127.0.0.1:8788")
    print("  演示账号:   13800002233 / Demo@12345")
    print("  支付密码:   888888")
    host = lan_host()
    if host:
        print(f"  手机端:     http://{host}:3000 (手机连同一 Wi-Fi)")
    health = http_get(8800, "/health")
    if health and health[0] == 200:
        try:
            model = str(json.loads(health[1]).get("model") or "")
        except (ValueError, AttributeError):
            model = ""
        if model:
            print(f"  当前模型:   {model}")
    print(f"  后台日志:   {LOG_DIR}")
    print("  本窗口可以关闭,不影响后台;停止服务请运行 停止演示.bat")
    print("=" * 44)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Banking 一键启动(全量重启)")
    parser.add_argument(
        "--skip-build", action="store_true", help="跳过 next build,直接用上一次产物启动"
    )
    parser.add_argument(
        "--no-open", action="store_true", help="启动完成后不自动打开浏览器"
    )
    args = parser.parse_args(argv)

    print("=" * 44)
    print("  AI Banking Agent - 一键启动(全量重启)")
    print("=" * 44)
    print()

    if not PY.exists():
        print(f"[错误] 找不到 {PY}")
        print("       请先创建虚拟环境并装依赖:")
        print("       py -3.12 -m venv .venv")
        print("       .venv\\Scripts\\python.exe -m pip install -r requirements.txt")
        return 1

    stop_old_services()
    ensure_data()
    apply_zai_env(load_env_subset(WEBUI / ".env.local"))
    print()

    # 8800 是登录/聊天后端;8788/8789 是演示网银与管理台,手机端用不到但演示要用。
    start_hidden("agent-api", [str(PY), "-m", "agent.api"], ROOT)
    start_hidden("admin-api", [str(PY), "-m", "bank_core.admin_api"], ROOT)
    start_hidden("web-api", [str(PY), "-m", "bank_core.web_api"], ROOT)

    if args.skip_build and not (WEBUI / ".next" / "BUILD_ID").exists():
        print("[提示] --skip-build 但没有上次构建产物,改为执行构建。")
        args.skip_build = False
    if not args.skip_build:
        build_frontend()

    start_hidden("webui", ["cmd.exe", "/c", "npx next start"], WEBUI)

    print()
    print("等待服务就绪...")
    backends_ok = all(
        [
            wait_tcp(8800, 40, "agent-api"),
            wait_tcp(8789, 40, "admin-api"),
            wait_tcp(8788, 40, "web-api"),
        ]
    )
    webui_ok = wait_tcp(3000, 90, "webui")
    ok = backends_ok and webui_ok

    print_summary(ok)
    if ok and not args.no_open:
        webbrowser.open("http://127.0.0.1:3000")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
