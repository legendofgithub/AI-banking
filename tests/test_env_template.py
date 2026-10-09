"""环境模板与密钥防泄露守卫 —— "clone 后只填一个 Key"契约。

背景(2026-10-09,仓库转公开):
- 队友拿到代码后,唯一必填项是 LLM API Key(ZAI_API_KEY),其余由
  scripts/setup_env.py 自动生成(AUTH_SECRET 本机随机)或模板默认值
  (端点/模型/端口)补齐;
- Key 写入 webui/.env.local;任何 .env 变体都被根 .gitignore 通配拦截,
  从机制上杜绝"把 Key 提交进公开仓库"。

本文件守三件事(全部零依赖、秒级):
1) .gitignore 真的拦得住所有 .env 变体(git check-ignore 实测,不靠人眼),
   且模板 .env.example 不被误伤;
2) .env.example 覆盖全部配置键、默认值齐全(一个 Key 真的够用)、不含密钥;
3) setup_env.py 行为正确:密钥随机、默认值保留、拒绝覆盖已有文件、
   --api-key 参数落盘。

改契约(比如新增配置键)请同步:webui/.env.example、scripts/setup_env.py、
docs/协作与接口契约.md §7.3,然后更新本文件的 REQUIRED_KEYS。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE = ROOT / "webui" / ".env.example"
SETUP_PY = ROOT / "scripts" / "setup_env.py"

# 代码实际读取、且必须出现在模板里的配置键(webui 侧 process.env.* +
# agent/llm.py 的 os.environ ZAI_*;模板残留的 POSTGRES_URL 等不在其列)
REQUIRED_KEYS = {
    "AUTH_SECRET",
    "AUTH_URL",
    "NEXT_PUBLIC_AGENT_API_PORT",
    "ZAI_API_KEY",
    "ZAI_BASE_URL",
    "ZAI_MODEL",
}

# 这些键在模板里必须有默认值——队友只填 ZAI_API_KEY 一个就能跑
KEYS_WITH_DEFAULTS = {"AUTH_URL", "NEXT_PUBLIC_AGENT_API_PORT", "ZAI_BASE_URL", "ZAI_MODEL"}


def _kv(text: str) -> dict[str, str]:
    return {
        m.group(1): m.group(2).strip()
        for m in re.finditer(r"^([A-Z_][A-Z_0-9]*)=(.*)$", text, re.M)
    }


def _check_ignore(path: str) -> bool:
    return subprocess.run(
        ["git", "check-ignore", "-q", path], cwd=ROOT,
        capture_output=True).returncode == 0


# ------------------------------------------------ 1) 泄露通道:gitignore 实测
@pytest.mark.parametrize("victim", [
    "webui/.env.local",          # 标准落点(setup_env.py 的输出)
    "webui/.env.production",     # 手滑改名变体
    "webui/.env",                # 无后缀变体
    ".env",                      # 根目录
    ".env.local",
    ".env.development.local",    # Next.js 约定的其他变体
])
def test_gitignore_blocks_every_env_variant(victim: str) -> None:
    """任何 .env 变体都必须被 .gitignore 拦下——这是 Key 不泄露的机制保证。"""
    assert _check_ignore(victim), (
        f"{victim} 没有被 .gitignore 拦截。队友把 Key 填进去后一次 git add -A "
        f"就会提交进公开仓库。请补 .gitignore 的 .env* 通配规则。"
    )


def test_env_example_survives_gitignore() -> None:
    """模板 .env.example 是队友的唯一参照,不能被通配规则误伤。"""
    assert not _check_ignore("webui/.env.example")


# --------------------------------------- 2) 模板完整性:一个 Key 真的够用
def test_template_covers_all_required_keys() -> None:
    missing = REQUIRED_KEYS - set(_kv(ENV_EXAMPLE.read_text(encoding="utf-8")))
    assert not missing, (
        f"webui/.env.example 缺配置键 {sorted(missing)}:队友照模板配不出可跑环境。"
        f"新键请同时更新 scripts/setup_env.py 与 docs/协作与接口契约.md §7.3。"
    )


def test_template_defaults_make_one_key_enough() -> None:
    blank = {k for k, v in _kv(ENV_EXAMPLE.read_text(encoding="utf-8")).items()
             if k in KEYS_WITH_DEFAULTS and not v}
    assert not blank, (
        f"模板里 {sorted(blank)} 没有默认值,队友还得手填——违背『只填一个 Key』。"
    )


def test_template_has_no_secrets() -> None:
    """模板本身不得携带任何真实密钥(仓库是公开的,历史里也擦不掉)。"""
    kv = _kv(ENV_EXAMPLE.read_text(encoding="utf-8"))
    assert kv.get("ZAI_API_KEY", "x") == "", "模板的 ZAI_API_KEY 必须留空"
    assert kv.get("AUTH_SECRET", "x") == "", "模板的 AUTH_SECRET 必须留空(由 setup 随机生成)"
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert not re.search(r"(sk|key)-[A-Za-z0-9]{16,}", text, re.I), (
        "模板里出现了疑似真实密钥的字符串。"
    )


# ------------------------------------------------- 3) setup_env.py 行为
def _run_setup(tmp_path: Path, *extra: str) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    out = tmp_path / ".env.local"
    subprocess.run(
        [sys.executable, str(SETUP_PY),
         "--template", str(ENV_EXAMPLE), "--out", str(out),
         "--non-interactive", *extra],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    return out


def test_setup_generates_random_secret_and_preserves_defaults(tmp_path: Path) -> None:
    """AUTH_SECRET 本机随机(两台机器/两次生成不得相同),其余默认值原样保留。"""
    out1 = _run_setup(tmp_path / "a")
    out2 = _run_setup(tmp_path / "b")
    kv1, kv2 = _kv(out1.read_text(encoding="utf-8")), _kv(out2.read_text(encoding="utf-8"))

    assert len(kv1["AUTH_SECRET"]) >= 32, "AUTH_SECRET 熵不够(应 token_urlsafe(32) 起)"
    assert kv1["AUTH_SECRET"] != kv2["AUTH_SECRET"], "两次生成的 AUTH_SECRET 相同"

    tmpl = _kv(ENV_EXAMPLE.read_text(encoding="utf-8"))
    for k in KEYS_WITH_DEFAULTS:
        assert kv1[k] == tmpl[k], f"{k} 的默认值没从模板带过来"
    assert kv1["ZAI_API_KEY"] == ""


def test_setup_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    """已有 .env.local 时默认跳过——防止重跑脚本悄悄换掉 AUTH_SECRET(会话全失效)。"""
    out = _run_setup(tmp_path)
    before = out.read_text(encoding="utf-8")
    _run_setup(tmp_path)  # 不带 --force
    assert out.read_text(encoding="utf-8") == before


def test_setup_accepts_api_key_argument(tmp_path: Path) -> None:
    out = _run_setup(tmp_path, "--api-key", "sk-test-not-a-real-key")
    kv = _kv(out.read_text(encoding="utf-8"))
    assert kv["ZAI_API_KEY"] == "sk-test-not-a-real-key"
