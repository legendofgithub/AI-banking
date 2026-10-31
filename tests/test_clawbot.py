"""微信 ClawBot 接入服务测试:子进程生命周期 → 二维码解析 → 状态机。

不碰真实 OpenClaw:CLAWBOT_NODE 指向当前解释器、CLAWBOT_CLI 指向临时假脚本,
假脚本按 argv 分派 login/status,输出与真实 CLI 同形的文案与链接。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from fastapi.testclient import TestClient

from agent.api import create_app
from test_agent_graph import _mk_db

FAKE_CLI = '''\
import sys, time, os
args = sys.argv[1:]
if "status" in args:
    mode = os.environ.get("FAKE_STATUS", "running")
    if mode == "running":
        print("- openclaw-weixin 286add22a0a3-im-bot: enabled, configured, "
              "running, in:3m ago", flush=True)
    else:
        print("- openclaw-weixin 286add22a0a3-im-bot: enabled, configured, "
              "stopped", flush=True)
    sys.exit(0)
mode = os.environ.get("FAKE_LOGIN", "qr")
print("正在启动...", flush=True)
if mode == "expired":
    print("Channel login failed: Error: 二维码多次失效,连接流程已停止。", flush=True)
    sys.exit(1)
print("若二维码未能显示或无法使用,你可以访问以下链接以继续:", flush=True)
print("https://liteapp.weixin.qq.com/q/7GiQu1?qrcode=deadbeefcafe&bot_type=3", flush=True)
print("正在等待操作...", flush=True)
time.sleep(float(os.environ.get("FAKE_SLEEP", "30")))
'''


def _app(tmp_path, monkeypatch, login="qr", status="running", sleep="30"):
    cli = tmp_path / "fake_openclaw.py"
    cli.write_text(FAKE_CLI, encoding="utf-8")
    monkeypatch.setenv("CLAWBOT_NODE", sys.executable)
    monkeypatch.setenv("CLAWBOT_CLI", str(cli))
    monkeypatch.setenv("FAKE_LOGIN", login)
    monkeypatch.setenv("FAKE_STATUS", status)
    monkeypatch.setenv("FAKE_SLEEP", sleep)
    monkeypatch.setenv("AGENT_LAN_INFO_INTERVAL", "0")
    return create_app(llm=None, db_path=_mk_db(tmp_path),
                      checkpoint_path=tmp_path / "cb_ckpt.sqlite",
                      threads_path=tmp_path / "cb_threads.sqlite")


def _wait_state(client, pred, timeout=12.0) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        last = client.get("/api/wechat/clawbot/state").json()
        if pred(last):
            return last
        time.sleep(0.15)
    raise AssertionError(f"状态未达预期,最后:{last}")


def test_qr_flow_reaches_waiting(tmp_path, monkeypatch):
    """拉起登录 → 解析出 liteapp 链接 → waiting + gen 自增;二维码可渲染成 PNG。"""
    app = _app(tmp_path, monkeypatch, status="stopped")
    with TestClient(app) as client:
        r = client.post("/api/wechat/clawbot/start").json()
        assert r["cli_ready"] is True
        st = _wait_state(client, lambda s: s["status"] == "waiting")
        assert st["qr_url"].startswith("https://liteapp.weixin.qq.com/q/")
        assert st["gen"] >= 1

        png = client.get("/api/qr", params={"data": st["qr_url"]})
        assert png.status_code == 200
        assert png.headers["content-type"] == "image/png"
        assert png.content[:8] == b"\x89PNG\r\n\x1a\n"
        app.state.clawbot._proc.kill()


def test_start_is_idempotent_while_alive(tmp_path, monkeypatch):
    """会话在跑时重复 start 不重复起进程(gen 不变)。"""
    app = _app(tmp_path, monkeypatch, status="stopped")
    with TestClient(app) as client:
        client.post("/api/wechat/clawbot/start")
        st1 = _wait_state(client, lambda s: s["status"] == "waiting")
        st2 = client.post("/api/wechat/clawbot/start").json()
        assert st2["gen"] == st1["gen"] and st2["alive"] is True
        app.state.clawbot._proc.kill()


def test_connected_skips_qr(tmp_path, monkeypatch):
    """channels status 显示 running → 直接 connected,不再要二维码。"""
    app = _app(tmp_path, monkeypatch, status="running")
    with TestClient(app) as client:
        st = client.post("/api/wechat/clawbot/start").json()
        assert st["status"] == "connected"
        assert st["qr_url"] == ""


def test_expired_qr_marks_failed(tmp_path, monkeypatch):
    """CLI 报二维码多次失效 → failed + 可读提示。"""
    app = _app(tmp_path, monkeypatch, login="expired", status="stopped")
    with TestClient(app) as client:
        client.post("/api/wechat/clawbot/start")
        st = _wait_state(client, lambda s: s["status"] == "failed")
        assert "失效" in st["message"]


def test_force_qr_not_swallowed_by_previous_connection(tmp_path, monkeypatch):
    """回归:已连接的机器点「重新生成二维码」,必须真的出码。

    旧实现里两个落点都会把新一轮的 starting/waiting 翻回 connected:
    ① 上一轮 _watch 的 finally 调 is_connected(旧进程收尾);
    ② is_connected 见 status=running 就改状态。
    结果是 force 之后永远看不到二维码。用轮次代号 + "有活动会话不落状态"修复。
    """
    app = _app(tmp_path, monkeypatch, status="running")
    with TestClient(app) as client:
        assert client.post("/api/wechat/clawbot/start").json()["status"] == "connected"
        # 换假脚本行为:status 仍报 running(模拟旧连接还在),但 login 会出码
        monkeypatch.setenv("FAKE_STATUS", "running")
        client.post("/api/wechat/clawbot/start", params={"force": "true"})
        st = _wait_state(client, lambda s: s["status"] == "waiting")
        assert st["qr_url"].startswith("https://liteapp.weixin.qq.com/q/")
        app.state.clawbot._proc.kill()


def test_missing_cli_reports_unavailable(tmp_path, monkeypatch):
    """找不到 openclaw 入口 → unavailable,前端显示安装指引而不是死等。"""
    monkeypatch.setenv("CLAWBOT_CLI", str(tmp_path / "nope" / "index.js"))
    monkeypatch.setenv("CLAWBOT_NODE", sys.executable)
    app = create_app(llm=None, db_path=_mk_db(tmp_path),
                     checkpoint_path=tmp_path / "x_ckpt.sqlite",
                     threads_path=tmp_path / "x_threads.sqlite")
    # 显式路径指向不存在的文件 = 等效"没装":直接 unavailable(前端显示
    # 安装指引),而不是拉起一个注定 MODULE_NOT_FOUND 的 node 再等超时。
    with TestClient(app) as client:
        st = client.post("/api/wechat/clawbot/start").json()
        assert st["status"] == "unavailable"
        assert st["cli_ready"] is False
        assert "OpenClaw" in st["message"]
