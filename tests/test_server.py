"""服务端纯函数：字幕行解析、鱼日志解析、模式文件。"""

from __future__ import annotations

from live_deck.deck import server


def test_asr_line_parsing():
    match = server.ASR_LINE.match("[12:34:56] 今天先打这一关")
    assert match and match.group(1) == "12:34:56"
    assert match.group(2) == "今天先打这一关"


def test_asr_speech_filter():
    assert server.asr_is_speech("今天先打这一关")
    assert not server.asr_is_speech("。")
    assert not server.asr_is_speech("模型已加载")
    assert not server.asr_is_speech("room 123 -> 456 flv")


def test_fish_log_lines():
    preview = server.parse_fish_line("17:23:00 准备发：[4/5] 这关我卡了三天（5.1s 后）")
    assert preview and preview["kind"] == "preview"
    assert preview["text"] == "[4/5] 这关我卡了三天"

    draft = server.parse_fish_line("17:23:08 【草稿】[4/5] 绕后那条路")
    assert draft and draft["kind"] == "draft"

    sent = server.parse_fish_line("17:23:16 已发 #3: [5/5] 藏这么深")
    assert sent and sent["kind"] == "sent" and sent["n"] == 3

    assert server.parse_fish_line("随便一行日志") is None


def test_mode_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MODE_FILE", tmp_path / "state" / "mode.txt")
    server.write_mode("live")
    assert server.read_mode() == "live"
    server.write_mode("draft")
    assert server.read_mode() == "draft"


def test_mode_defaults_to_draft_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "MODE_FILE", tmp_path / "nope.txt")
    assert server.read_mode() == "draft"
