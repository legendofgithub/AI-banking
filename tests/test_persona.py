"""agent.persona 人设提示词测试：存取 / 默认态 / 越界防御 / 失败软处理。

AGENT_PERSONA_DB 由 conftest autouse 隔离到 tmp_path,不碰运行库。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import persona


EMPTY = {"text": "", "temperature": None}


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AGENT_PERSONA_DB", str(tmp_path / "persona.sqlite"))


# ---------------------------------------------------------------- 存取与默认

def test_unset_persona_is_default_empty():
    assert persona.get_persona(1) == EMPTY


def test_set_then_get_roundtrip():
    assert persona.set_persona(1, "说话像江湖侠客", 0.9)
    got = persona.get_persona(1)
    assert got["text"] == "说话像江湖侠客"
    assert got["temperature"] == pytest.approx(0.9)


def test_blank_text_clears_persona():
    persona.set_persona(1, "热情外向", 0.7)
    assert persona.set_persona(1, "   ")
    assert persona.get_persona(1) == EMPTY


def test_temperature_out_of_range_treated_as_unset():
    """越界温度一律 None(未设置),绝不拿去实例化模型。"""
    persona.set_persona(1, "稳重", 1.5)
    assert persona.get_persona(1) == {"text": "稳重", "temperature": None}
    persona.set_persona(1, "稳重", -0.1)
    assert persona.get_persona(1)["temperature"] is None
    persona.set_persona(1, "稳重", "不是数")
    assert persona.get_persona(1)["temperature"] is None


def test_text_is_trimmed_and_capped():
    persona.set_persona(1, "  好" + "长" * 500)
    got = persona.get_persona(1)
    assert got["text"].startswith("好")
    assert len(got["text"]) <= persona.PERSONA_MAX_CHARS


def test_persona_is_isolated_between_users():
    persona.set_persona(1, "侠客", 0.3)
    persona.set_persona(2, "管家", 0.5)
    assert persona.get_persona(1)["text"] == "侠客"
    assert persona.get_persona(2)["text"] == "管家"


def test_anonymous_is_rejected():
    for uid in (None, 0, -1, 3.5, "abc", "", True):
        assert persona.set_persona(uid, "x") is False
        assert persona.get_persona(uid) == EMPTY


# ---------------------------------------------------------------- 失败软处理

def test_broken_db_path_fails_soft(tmp_path: Path, monkeypatch):
    blocker = tmp_path / "not-a-file"
    blocker.mkdir()
    monkeypatch.setenv("AGENT_PERSONA_DB", str(blocker))
    assert persona.set_persona(1, "x", 0.5) is False
    assert persona.get_persona(1) == EMPTY


def test_read_does_not_create_db_file(tmp_path: Path, monkeypatch):
    target = tmp_path / "nope.sqlite"
    monkeypatch.setenv("AGENT_PERSONA_DB", str(target))
    assert persona.get_persona(1) == EMPTY
    assert not target.exists()
