"""人格加载：注释剥离、缺文件时的报错、统计。"""

from __future__ import annotations

import pytest

from live_deck.fish import persona


def test_repo_persona_loads():
    text = persona.load_persona()
    assert "大肥鱼" in text
    assert text.startswith("你是")
    # HTML 注释不该被发给模型
    assert "<!--" not in text
    assert "加载时会自动去掉" not in text


def test_stats():
    stats = persona.persona_stats()
    assert stats["chars"] > 200
    assert stats["lines"] > 5


def test_missing_persona_raises(tmp_path):
    with pytest.raises(persona.PersonaError):
        persona.load_persona(tmp_path / "nope.md")


def test_empty_persona_raises(tmp_path):
    path = tmp_path / "empty.md"
    path.write_text("<!-- only a comment -->", encoding="utf-8")
    with pytest.raises(persona.PersonaError):
        persona.load_persona(path)


def test_custom_persona_path(tmp_path, monkeypatch):
    path = tmp_path / "mine.md"
    path.write_text("你是测试角色。\n只输出 OK。\n", encoding="utf-8")
    assert persona.load_persona(path).startswith("你是测试角色")
