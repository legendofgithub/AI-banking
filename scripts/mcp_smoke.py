"""bank_core MCP 冒烟脚本(只读)。

用 langchain-mcp-adapters 以 stdio 方式拉起 bank_core 的 MCP 服务:
  1) 打印加载到的工具总数与工具名清单;
  2) 调一个只读工具 get_accounts([READ], mcp_server.py 无参无副作用)打印结果摘要。

安全约定(铁律):
- 只做只读操作,绝不调用任何写工具(建单/确认/取消/申赎/取消代扣/挂失等);
- 数据库 seed 到 tempfile.mkdtemp() 临时目录,BANK_CORE_DB 指向临时文件;
  seed 子进程与 MCP server 子进程均显式传入完整环境 + BANK_CORE_DB(继承);
  注意:langchain-mcp-adapters 的 stdio 连接若 env 传 None,子进程只会拿到
  MCP SDK get_default_environment() 的默认子集,不含 BANK_CORE_DB,
  因此必须显式传 {**os.environ, "BANK_CORE_DB": ...};
- 本脚本自身不 import bank_core,不读写 data/bank.db,并在运行前后
  校验 data/bank.db(含 -wal/-shm)的 mtime/size 未被改动。

用法:
  .venv/Scripts/python scripts/mcp_smoke.py

退出码:成功 0;任何一步失败 1(可重复执行、无交互)。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = PROJECT_ROOT / "data" / "bank.db"          # 只做指纹校验,绝不打开
READONLY_TOOL = "get_accounts"                          # [READ] mcp_server.py:46


def _utf8_stdout() -> None:
    """Windows 控制台下强制 UTF-8 输出,避免中文乱码/编解码异常。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def _db_fingerprint() -> list[tuple[str, int, int]] | None:
    """data/bank.db 及 WAL 伴生文件的 (文件名, mtime_ns, size) 指纹;不存在则 None。"""
    fp = []
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(DEFAULT_DB) + suffix)
        if p.exists():
            st = p.stat()
            fp.append((p.name, st.st_mtime_ns, st.st_size))
    return fp or None


def seed_temp_db(db_path: Path) -> None:
    """子进程执行 python -m bank_core.seed --db <临时库>,不触碰默认库。"""
    env = {**os.environ, "BANK_CORE_DB": str(db_path)}
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "bank_core.seed", "--db", str(db_path)],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"seed 失败(exit={proc.returncode})\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    print("[seed] 临时库播种完成:", proc.stdout.strip())


def _read_temp_balances(db_path: Path) -> dict[int, int] | None:
    """只读打开临时库,取 accounts 的 id -> balance_cents,用于与 MCP 结果互证。"""
    uri = f"file:{db_path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            rows = conn.execute(
                "SELECT id, balance_cents FROM accounts ORDER BY id"
            ).fetchall()
            return {rid: bal for rid, bal in rows}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f"[warn] 直读临时库失败(跳过互证): {exc}")
        return None


def _extract_payload(result: object) -> list[dict] | None:
    """从 LangChain 工具返回值中提取账户列表。

    适配器 0.3.x 的 StructuredTool(response_format="content_and_artifact")
    返回 (content, artifact):优先取 artifact.structured_content(fastmcp
    对 list[dict] 返回值会带 structuredContent,可能包成 {"result": [...]});
    否则回退解析 content 文本块的 JSON。
    """
    content, artifact = result, None
    if isinstance(result, tuple) and len(result) == 2:
        content, artifact = result

    sc = getattr(artifact, "structured_content", None) if artifact is not None else None
    if isinstance(sc, dict) and isinstance(sc.get("result"), list):
        return sc["result"]
    if isinstance(sc, list) and sc:
        return sc

    texts: list[str] = []
    if isinstance(content, str):
        texts = [content]
    elif isinstance(content, list):
        texts = [
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
    for text in texts:
        try:
            v = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(v, list) and v:
            return v
        if isinstance(v, dict) and isinstance(v.get("result"), list) and v["result"]:
            return v["result"]
    return None


async def smoke(db_path: Path) -> int:
    """拉起 MCP 服务 → 列工具 → 只读调用 get_accounts。返回进程退出码。"""
    # 关键写法:env 必须显式传完整环境 + BANK_CORE_DB,子进程才能继承临时库路径
    from langchain_mcp_adapters.client import MultiServerMCPClient

    client = MultiServerMCPClient(
        {
            "bank-core": {
                "transport": "stdio",
                "command": sys.executable,
                "args": ["-m", "bank_core.mcp_server"],
                "cwd": str(PROJECT_ROOT),
                "env": {**os.environ, "BANK_CORE_DB": str(db_path)},
            }
        }
    )

    # 1) 工具清单(每次 get_tools 新起一个 stdio 会话,列出后即关)
    tools = await client.get_tools()
    names = sorted(t.name for t in tools)
    print(f"[1] 加载工具总数: {len(tools)}")
    print(f"    工具名清单: {', '.join(names)}")

    # 2) 只读调用 get_accounts
    tool = next((t for t in tools if t.name == READONLY_TOOL), None)
    if tool is None:
        print(f"FAIL: 未找到只读工具 {READONLY_TOOL}")
        return 1
    result = await tool.ainvoke({})
    accounts = _extract_payload(result)
    if not isinstance(accounts, list) or not accounts:
        print(f"FAIL: {READONLY_TOOL} 返回无法解析,原始结果: {result!r}")
        return 1

    print(f"[2] {READONLY_TOOL}([READ]) 摘要: {len(accounts)} 个账户")
    for a in accounts:
        try:
            bal = int(a["balance_cents"]) / 100
            print(f"    - id={a['id']} {a['name']}({a['type']}) 余额 {bal:,.2f} 元")
        except (KeyError, TypeError, ValueError):
            print(f"    - 原始记录: {a!r}")

    # 3) 与临时库直读结果互证:证明 MCP 服务读的确实是临时库
    expected = _read_temp_balances(db_path)
    if expected is not None:
        got = {a["id"]: int(a["balance_cents"]) for a in accounts if "id" in a}
        if got == expected:
            print("[3] 校验: get_accounts 余额与临时库直读一致(MCP 服务确在读临时库)")
        else:
            print(f"FAIL: 余额不一致 MCP={got} 临时库={expected}")
            return 1
    return 0


def main() -> int:
    _utf8_stdout()
    print("== bank_core MCP 冒烟(langchain-mcp-adapters / stdio / 只读)==")

    fp_before = _db_fingerprint()
    print(f"[0] data/bank.db 指纹(运行前): {fp_before}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="bank_mcp_smoke_"))
    db_path = tmp_dir / "bank_smoke.db"
    print(f"[0] 临时库: {db_path}")
    try:
        seed_temp_db(db_path)
        code = asyncio.run(smoke(db_path))
    except Exception as exc:  # noqa: BLE001 —— 冒烟脚本任何异常都要落到非 0 退出
        print(f"FAIL: {type(exc).__name__}: {exc}")
        code = 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        print(f"[cleanup] 已删除临时目录: {tmp_dir}")

    fp_after = _db_fingerprint()
    print(f"[0] data/bank.db 指纹(运行后): {fp_after}")
    if fp_after != fp_before:
        print("FAIL: data/bank.db 在冒烟期间被改动(必须保持只读不动)")
        code = 1
    else:
        print("[0] 校验: data/bank.db 未被触碰")

    print(f"== 冒烟{'通过' if code == 0 else '失败'}(exit={code})==")
    return code


if __name__ == "__main__":
    sys.exit(main())
