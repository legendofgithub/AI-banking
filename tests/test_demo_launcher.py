"""启动器纯逻辑测试:netstat 监听解析 / .env.local 的 ZAI_* 读取 / 环境回填。

启动器真正拉服务的行为由 `启动演示.bat` 实机冒烟覆盖;这里只测不碰进程、
不碰网络的纯函数,保证端口精确匹配与配置回填口径不漂移。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "demo_launcher.py"


def _load_launcher():
    spec = importlib.util.spec_from_file_location("demo_launcher", LAUNCHER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


launcher = _load_launcher()


def test_parse_listener_pids_only_listening_and_exact_ports() -> None:
    """只认 LISTENING,端口精确匹配(3000 不能误伤 30000),IPv4/IPv6 去重。"""
    sample = """
活动连接

  协议  本地地址          外部地址        状态           PID
  TCP    0.0.0.0:3000       0.0.0.0:0       LISTENING       1234
  TCP    127.0.0.1:3000     0.0.0.0:0       LISTENING       1234
  TCP    [::]:3000          [::]:0          LISTENING       1234
  TCP    0.0.0.0:30000      0.0.0.0:0       LISTENING       9999
  TCP    0.0.0.0:8800       0.0.0.0:0       LISTENING       5678
  TCP    192.168.1.5:50000  1.2.3.4:443     ESTABLISHED     7777
  TCP    127.0.0.1:8788     127.0.0.1:50001 ESTABLISHED     6666
"""
    assert launcher.parse_listener_pids(sample) == {3000: [1234], 8800: [5678]}


def test_parse_listener_pids_empty_output() -> None:
    assert launcher.parse_listener_pids("") == {}
    assert launcher.parse_listener_pids("没有任何连接\n") == {}


def test_load_env_subset_reads_only_zai_keys(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "# 注释行\n"
        "\n"
        "ZAI_API_KEY=sk-with=equals\n"
        'ZAI_BASE_URL = "https://example.test/v1"\n'
        "ZAI_MODEL='deepseek-chat'\n"
        "AUTH_SECRET=not-returned\n"
        "OTHER_KEY=1\n",
        encoding="utf-8",
    )
    assert launcher.load_env_subset(env_file) == {
        "ZAI_API_KEY": "sk-with=equals",
        "ZAI_BASE_URL": "https://example.test/v1",
        "ZAI_MODEL": "deepseek-chat",
    }


def test_load_env_subset_handles_export_prefix(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.local"
    env_file.write_text("export ZAI_MODEL=glm-4.6\n", encoding="utf-8")
    assert launcher.load_env_subset(env_file) == {"ZAI_MODEL": "glm-4.6"}


def test_load_env_subset_missing_file(tmp_path: Path) -> None:
    assert launcher.load_env_subset(tmp_path / "nope.env") == {}


def test_apply_zai_env_overwrites_stale_values(monkeypatch) -> None:
    """环境变量里可能残留过期 Key/端点;必须由 .env.local 同源覆盖。"""
    for key in launcher.ZAI_KEYS:
        monkeypatch.setenv(key, "stale")
    launcher.apply_zai_env(
        {
            "ZAI_API_KEY": "fresh-key",
            "ZAI_BASE_URL": "https://fresh.test/v1",
            "ZAI_MODEL": "fresh-model",
        }
    )
    assert os.environ["ZAI_API_KEY"] == "fresh-key"
    assert os.environ["ZAI_BASE_URL"] == "https://fresh.test/v1"
    assert os.environ["ZAI_MODEL"] == "fresh-model"


def test_apply_zai_env_leaves_existing_when_key_absent(monkeypatch) -> None:
    for key in launcher.ZAI_KEYS:
        monkeypatch.setenv(key, "kept")
    launcher.apply_zai_env({"ZAI_MODEL": "only-model"})
    assert os.environ["ZAI_API_KEY"] == "kept"
    assert os.environ["ZAI_BASE_URL"] == "kept"
    assert os.environ["ZAI_MODEL"] == "only-model"
