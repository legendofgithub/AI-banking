"""微信 ClawBot 接入:后台代拉 OpenClaw 登录会话,把动态二维码交给 webui。

为什么不是一个静态二维码
------------------------
ClawBot 的二维码是**一次性登录凭证**:由 `openclaw channels login` 现场
向微信官方中继申请,约一两分钟失效,CLI 会自动换新几次后退出。所以前端
点开弹窗时要能"现场取码",并且指望它自己刷新——本模块就是干这个的:

  前端 POST /api/wechat/clawbot/start  → 幂等拉起一个登录会话
  前端 GET  /api/wechat/clawbot/state  → 轮询 {status, qr_url, gen, ...}
  二维码图片本身复用 8800 既有的 /api/qr?data=<qr_url>(本地渲染,不出网)

状态机:idle → starting → waiting(有码) → connected / failed / unavailable

边界
----
- 只负责**本机 OpenClaw 的登录态**,不碰 BankAgent 业务;消息转发仍由
  OpenClaw 技能调 /api/wechat/relay 完成(见 docs/openclaw-bankagent-relay-技能.md)。
- 进程用 asyncio 子进程拉起,进程级单例挂在 app.state,多标签页共用同一会话。
- 路径全部可配:CLAWBOT_NODE / CLAWBOT_CLI / CLAWBOT_CHANNEL。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("agent.clawbot")

LINK_RE = re.compile(r"https://liteapp\.weixin\.qq\.com/q/\S+")
CONN_CACHE_S = 15.0        # channels status 结果缓存(该命令 3-5s,别每次轮询都跑)
STATUS_TIMEOUT_S = 30.0
LOG_KEEP = 40              # 只留最近若干行诊断输出

CHANNEL = os.environ.get("CLAWBOT_CHANNEL", "openclaw-weixin")

# (CLAWBOT_CLI 环境值, 解析结果):snapshot() 每次轮询都会要路径,
# 而 npm root -g 是几百毫秒的子进程调用——必须缓存,env 变化时失效。
_cli_cache: tuple[str, str | None] | None = None


def _node() -> str:
    return (os.environ.get("CLAWBOT_NODE") or shutil.which("node")
            or r"C:\Program Files\nodejs\node.exe")


def _npm_global_root() -> Path | None:
    """npm 全局包目录:npm root -g。让"装在别处"的 OpenClaw 也能被找到。"""
    npm = shutil.which("npm")
    if not npm:
        return None
    try:
        out = subprocess.run([npm, "root", "-g"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace",
                             timeout=8)
    except (OSError, subprocess.SubprocessError):
        return None
    path = Path((out.stdout or "").strip())
    return path if out.returncode == 0 and path.exists() else None


def _cli() -> str | None:
    """定位 openclaw 入口脚本(dist/index.js);装在哪都能用,也可显式指定。

    显式路径指向不存在的文件等效于"没装"——返回 None 让前端显示安装指引,
    而不是拉起一个注定 MODULE_NOT_FOUND 的 node 进程再等它超时。
    """
    global _cli_cache
    explicit = os.environ.get("CLAWBOT_CLI", "")
    if _cli_cache is not None and _cli_cache[0] == explicit:
        return _cli_cache[1]

    resolved: str | None = None
    if explicit:
        resolved = explicit if Path(explicit).exists() else None
    else:
        home = Path.home()
        # 顺序即优先级:本仓库 setup_clawbot.py 的回退安装位(openclaw-new,
        # 装着微信插件的新版)最优先;npm root -g 与全局默认位次之——
        # 那里可能残留没插件的旧版 openclaw,选了它 channels status 永远
        # 查不到 openclaw-weixin,弹窗会错出二维码(2026-10-31 实修)
        candidates = [
            home / "openclaw-new" / "node_modules" / "openclaw" / "dist"
            / "index.js",
            # npm -g 默认位(Windows / POSIX)
            home / "AppData" / "Roaming" / "npm" / "node_modules" / "openclaw"
            / "dist" / "index.js",
            Path("/usr/local/lib/node_modules/openclaw/dist/index.js"),
            Path("/usr/lib/node_modules/openclaw/dist/index.js"),
        ]
        root = _npm_global_root()
        if root is not None:
            candidates.append(root / "openclaw" / "dist" / "index.js")
        resolved = next((str(c) for c in candidates if c.exists()), None)
    _cli_cache = (explicit, resolved)
    return resolved


@dataclass
class _State:
    status: str = "idle"   # idle|starting|waiting|connected|failed|unavailable
    qr_url: str = ""
    gen: int = 0           # 二维码代数:前端据此换图(绕开浏览器同 URL 缓存)
    message: str = ""
    updated_at: float = 0.0


@dataclass
class ClawbotService:
    """一个 app 一份:管理登录子进程 + 连接状态缓存。"""

    st: _State = field(default_factory=_State)
    log: list[str] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _proc: Any = None
    _task: Any = None
    _conn_at: float = 0.0
    _conn: bool = False
    _epoch: int = 0        # 轮次代号:旧一轮的收尾不能改写新一轮状态

    # ---------------------------------------------------------------- 公开接口

    async def start(self, force: bool = False) -> dict:
        """幂等拉起登录会话;已连接则原样返回(除非 force 重新生成二维码)。"""
        async with self._lock:
            cli = _cli()
            if cli is None:
                self.st.status = "unavailable"
                self.st.message = ("本机未找到 OpenClaw(需要先 npm i -g openclaw "
                                   "或设置 CLAWBOT_CLI)")
                return self.snapshot()
            if not force and self._alive():
                return self.snapshot()
            # 连接检查必须穿透 15s 缓存:缓存里可能是上次网关未起时的 False,
            # 吃缓存会把已连接误判成未连接,弹窗错出二维码(2026-10-31 实修)
            if not force and await self.is_connected(force=True):
                self.st.status = "connected"
                self.st.qr_url = ""
                self.st.message = "微信已连接,无需扫码"
                return self.snapshot()

            await self._kill()
            self._epoch += 1
            epoch = self._epoch
            self.st = _State(status="starting", gen=self.st.gen + 1,
                             message="正在向微信申请二维码…", updated_at=time.time())
            self.log.clear()
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    _node(), cli, "channels", "login", "--channel", CHANNEL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
            except OSError as exc:
                self.st.status = "failed"
                self.st.message = f"拉起 OpenClaw 失败:{exc}"
                return self.snapshot()
            self._task = asyncio.create_task(self._watch(self._proc, epoch))
            return self.snapshot()

    async def state(self) -> dict:
        """轮询入口:顺手刷新连接态(带缓存,不阻塞等 CLI)。"""
        if self.st.status in ("idle", "failed") and not self._alive():
            await self.is_connected()   # 已连上的机器首次打开弹窗直接显示已连接
        return self.snapshot()

    async def is_connected(self, force: bool = False) -> bool:
        """是否已连上微信:解析 `channels status` 里 openclaw-weixin 的 running。"""
        now = time.time()
        if not force and now - self._conn_at < CONN_CACHE_S:
            return self._conn
        cli = _cli()
        if cli is None:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                _node(), cli, "channels", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), STATUS_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            # 查不动就沿用上次结论,不误报;但必须留痕——这里静默吞过
            # "CLI 因配置残留拒跑"的故障(2026-10-31 实修前查了半小时)
            logger.warning("channels status 执行失败,沿用上次连接态 %s",
                           self._conn, exc_info=True)
            return self._conn
        text = out.decode("utf-8", "replace")
        ok = bool(re.search(rf"{re.escape(CHANNEL)}\b.*\brunning\b", text))
        if not ok and out:
            logger.warning(
                "channels status 未命中(rc=%s HOME=%r USERPROFILE=%r "
                "node=%r cli=%r 尾部=%r)",
                proc.returncode, os.environ.get("HOME"),
                os.environ.get("USERPROFILE"), _node(), cli, text[-120:])
        self._conn_at, self._conn = time.time(), ok
        # 只在没有活动登录会话时才据旧连接态落状态:正在申请新码期间,
        # 上一次的连接仍是 running,不能把 waiting 翻回 connected(否则
        # 用户永远看不到新二维码)。
        if ok and not self._alive() and self.st.status in (
                "idle", "starting", "waiting", "failed"):
            self.st.status = "connected"
            self.st.message = "微信已连接,无需扫码"
        return ok

    def snapshot(self) -> dict:
        cli_ready = _cli() is not None
        return {
            "status": self.st.status,
            "qr_url": self.st.qr_url,
            "gen": self.st.gen,
            "message": self.st.message,
            "cli_ready": cli_ready,
            "alive": self._alive(),
            "log": self.log[-6:],
        }

    # ---------------------------------------------------------------- 内部

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _watch(self, proc: Any, epoch: int) -> None:
        """读登录进程输出。epoch 不是当前轮次就立刻收手——旧进程被替换后,
        它的 finally 绝不能改写新一轮的状态(否则新码永远显示不出来)。"""
        assert proc.stdout is not None
        try:
            while True:
                raw = await proc.stdout.readline()
                if not raw:
                    break
                if epoch != self._epoch:
                    return
                text = raw.decode("utf-8", "replace").strip()
                if not text or "Config warnings" in text:
                    continue
                self.log.append(text[:200])
                del self.log[:-LOG_KEEP]
                m = LINK_RE.search(text)
                if m:
                    self.st.qr_url = m.group(0)
                    self.st.gen += 1
                    self.st.status = "waiting"
                    self.st.message = "请用微信扫码连接"
                    self.st.updated_at = time.time()
                elif "多次失效" in text or "login failed" in text.lower():
                    self.st.status = "failed"
                    self.st.message = "二维码已失效,请点「重新生成」"
                    self.st.updated_at = time.time()
        finally:
            await proc.wait()
            if epoch != self._epoch:
                return
            if self.st.status in ("starting", "waiting"):
                if await self.is_connected(force=True):
                    self.st.status = "connected"
                    self.st.qr_url = ""
                    self.st.message = "微信已连接,无需扫码"
                else:
                    self.st.status = "failed"
                    self.st.message = "登录未完成,请重新生成二维码"
            self.st.updated_at = time.time()

    async def _kill(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
                await self._proc.wait()
            except (OSError, ProcessLookupError):
                pass
        self._proc = None


def mount(app: Any) -> None:
    """挂载 /api/wechat/clawbot/* (前端顶栏「微信 ClawBot」入口用)。"""
    svc = ClawbotService()
    app.state.clawbot = svc

    @app.post("/api/wechat/clawbot/start")
    async def clawbot_start(force: bool = False) -> dict:
        return await svc.start(force=force)

    @app.get("/api/wechat/clawbot/state")
    async def clawbot_state() -> dict:
        return await svc.state()
