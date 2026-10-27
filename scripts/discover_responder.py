#!/usr/bin/env python
"""手机 App 局域网发现应答器(纯标准库,零依赖)。

手机端 App 打开时向本子网广播 UDP 包"AIBANK_DISCOVER_V1"(端口 58713),
本应答器收到后回单播"AIBANK_V1|http://<局域网IP>:3000",App 即自动填入
电脑地址直连——用户免打字。与 mDNS 相比省一个第三方依赖;路由器开了
AP 隔离时广播会被拦,App 侧保留扫码配对/手动输入兜底。

设计约束:
- 由 demo_launcher.py 作为隐藏服务拉起,随停止脚本/端口清理一起退出
  (它不监听任何 TCP 端口,停止脚本按 PID 杀不到——所以自己监听一个
  不对外宣告的 TCP keepalive 口?不需要:launcher 全量重启时会按
  发现脚本的特征清杀,见 demo_launcher.stop_old_services 的补充逻辑);
- 只应答内网/回环来源,不向公网地址泄露局域网拓扑;
- 回包带 webui 端口(默认 3000),与 /api/lan-info 的 IP 探测同一手法。
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys

DISCOVERY_MAGIC = b"AIBANK_DISCOVER_V1"
REPLY_PREFIX = "AIBANK_V1|"
DISCOVERY_PORT = 58713


def lan_ip() -> str:
    """UDP connect 探测默认路由出口 IP(不真正发包,与 agent/api.py 同手法)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return str(s.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def is_private(ip: str) -> bool:
    """只服务内网与回环来源,避免把局域网地址回给公网探测者。"""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback


def main() -> int:
    webui_port = int(os.environ.get("WEBUI_PORT", "3000"))
    reply = f"{REPLY_PREFIX}http://{lan_ip()}:{webui_port}".encode("utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if os.name == "nt":
        # Windows 的 SO_REUSEADDR 允许第二个 socket 抢绑同一端口,必须用独占;
        # POSIX 用 REUSEADDR 避免 TIME_WAIT 类问题(UDP 无此忧,仅习惯一致)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError as exc:
        print(f"[错误] UDP {DISCOVERY_PORT} 绑定失败(可能已有应答器在跑): {exc}")
        return 1

    print(f"[就绪] 手机发现应答器: UDP {DISCOVERY_PORT} -> {reply.decode()}")
    sys.stdout.flush()
    while True:
        try:
            data, addr = sock.recvfrom(1024)
        except OSError:
            continue
        if not data.startswith(DISCOVERY_MAGIC):
            continue
        if not is_private(addr[0]):
            print(f"[忽略] 非内网来源 {addr[0]}")
            continue
        sock.sendto(reply, addr)
        print(f"[应答] {addr[0]}:{addr[1]} -> {reply.decode()}")
        sys.stdout.flush()


if __name__ == "__main__":
    sys.exit(main())
