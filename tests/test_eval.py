"""评测集：从日志抽场面、存取、统计。"""

from __future__ import annotations

import json

from live_deck import eval as ev


def _make_logs(tmp_path):
    logs = tmp_path / "logs"
    danmaku = tmp_path / "danmaku"
    logs.mkdir()
    danmaku.mkdir()
    lines = []
    for index in range(24):
        minute = index // 2
        second = (index % 2) * 30
        lines.append(f"[20:{minute:02d}:{second:02d}] 主播说的第 {index} 句测试台词")
    (logs / "asr-live-123456.log").write_text("\n".join(lines), encoding="utf-8")
    rows = [
        {"t": "20:02:10", "u": "观众甲", "x": "这关我也卡过"},
        {"t": "20:02:20", "u": "观众乙", "x": "先清小怪"},
    ]
    (danmaku / "123456.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    return logs, danmaku


def test_collect_scenes_from_logs(tmp_path):
    logs, danmaku = _make_logs(tmp_path)
    scenes = ev.collect_scenes(logs, danmaku, n=5)
    assert len(scenes) == 5
    assert scenes[0].room == "123456"
    assert "【主播在说话" in scenes[0].context
    assert "最近弹幕" in scenes[0].context
    assert "观众" in scenes[0].context


def test_collect_scenes_dedupes(tmp_path):
    logs, danmaku = _make_logs(tmp_path)
    scenes = ev.collect_scenes(logs, danmaku, n=50)
    assert len({scene.context for scene in scenes}) == len(scenes)


def test_collect_scenes_falls_back_to_examples(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    scenes = ev.collect_scenes(empty, None, n=3)
    assert len(scenes) == 3
    assert scenes[0].source == "example"
    assert scenes[0].id.startswith("demo-")


def test_scene_roundtrip(tmp_path):
    scenes = [
        ev.Scene(id="1", room="1", time="20:00:00", context="上下文"),
        ev.Scene(id="2", room="1", time="20:00:05", context="另一个"),
    ]
    path = tmp_path / "scenes.jsonl"
    ev.save_scenes(scenes, path)
    loaded = ev.load_scenes(path)
    assert [scene.id for scene in loaded] == ["1", "2"]
    assert loaded[1].context == "另一个"


def test_summarize():
    rows = [
        {"reply": "四字", "score": 4, "skipped": False},
        {"reply": "六个字啊", "score": 2, "skipped": False},
        {"reply": "", "score": 0, "skipped": True},
    ]
    stats = ev.summarize(rows)
    assert stats["n"] == 3
    assert stats["scored"] == 2
    assert stats["mean"] == 3.0
    assert stats["skip_rate"] == 0.3333 or abs(stats["skip_rate"] - 1 / 3) < 0.01
    assert stats["avg_len"] == 3.0


def test_read_write_run(tmp_path):
    path = tmp_path / "runs" / "x.jsonl"
    meta = {"model": "deepseek-chat", "retrieval": True}
    rows = [{"scene": "1", "reply": "好", "score": 4, "skipped": False}]
    ev.save_run(path, meta, rows)
    loaded_meta, loaded_rows = ev.read_run(path)
    assert loaded_meta["model"] == "deepseek-chat"
    assert loaded_rows[0]["score"] == 4
