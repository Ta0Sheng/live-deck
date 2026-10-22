"""离线评测：固定场面 → 生成候选 → 盲评 → 和上一版比。

为什么需要它
------------
「有没有变好」不能靠感觉。检索、样本、人格、模型任何一处改动，都可能让某几句更好、
另几句更差。这里做三件事：

1. ``make``：从真实字幕日志里抽 N 个固定场面（含当时弹幕），存成 ``data/eval/scenes.jsonl``；
2. ``run``：对每个场面生成候选句，人工打 1–5 分（或 ``--auto`` 用评审器打分），存一次运行；
3. ``report``：把最近几次运行的均分、跳过率、平均长度并排，看改动是涨是跌。

固定场面 + 同一把尺子，才能把「鱼在长大」变成一条曲线，而不是印象。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ASR_LINE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\]\s*(.+)$")
ASR_NOISE = ("模型已加载", "开始识别", "流类型", "就绪", "结束")
SCENE_HEADER = "【主播在说话，语音识别可能有错字和断句错误，别把错字当话题】"

__all__ = [
    "Scene",
    "collect_scenes",
    "load_scenes",
    "new_run_path",
    "read_run",
    "save_run",
    "save_scenes",
    "summarize",
]


@dataclass
class Scene:
    id: str
    room: str
    time: str
    context: str
    source: str = "log"

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "room": self.room,
            "time": self.time,
            "context": self.context,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Scene":
        return cls(
            id=str(data.get("id") or ""),
            room=str(data.get("room") or ""),
            time=str(data.get("time") or ""),
            context=str(data.get("context") or ""),
            source=str(data.get("source") or "log"),
        )


def _seconds(stamp: str) -> int | None:
    try:
        hour, minute, second = (int(part) for part in stamp.split(":"))
    except (ValueError, AttributeError):
        return None
    return hour * 3600 + minute * 60 + second


def _read_asr(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        match = ASR_LINE.match(line.strip())
        if not match:
            continue
        text = match.group(2).strip()
        if not text or text.startswith("#clean"):
            continue
        if any(noise in text for noise in ASR_NOISE):
            continue
        rows.append((match.group(1), text))
    return rows


def _read_danmaku(path: Path) -> list[tuple[int, str, str]]:
    rows: list[tuple[int, str, str]] = []
    if not path.is_file():
        return rows
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        sec = _seconds(str(item.get("t") or ""))
        text = str(item.get("x") or "").strip()
        if sec is None or not text:
            continue
        rows.append((sec, str(item.get("u") or "观众"), text))
    return rows


def _nearest(
    rows: list[tuple[int, str, str]], target: int, n: int = 5, window: int = 120
) -> list[tuple[int, str, str]]:
    nearby = [row for row in rows if abs(row[0] - target) <= window]
    nearby.sort(key=lambda row: (abs(row[0] - target), row[0]))
    return sorted(nearby[:n], key=lambda row: row[0])


def _build_context(window: list[tuple[str, str]], danmaku: list[tuple[int, str, str]]) -> str:
    lines = [SCENE_HEADER]
    lines.extend(f"[{stamp}] {text}" for stamp, text in window)
    if danmaku:
        lines.append("")
        lines.append("最近弹幕：")
        lines.extend(f"{user}: {text}" for _, user, text in danmaku)
    return "\n".join(lines)


EXAMPLE_SCENES: list[dict[str, str]] = [
    {
        "id": "demo-01",
        "room": "demo",
        "time": "21:00:00",
        "context": "【主播在说话，语音识别可能有错字和断句错误，别把错字当话题】\n"
        "[21:00:00] 这关我卡了三天了 一直过不去\n"
        "[21:00:04] 右边那个箱子是不是能推\n\n最近弹幕：\n观众A: 右边那个箱子可以推\n观众B: 我也是卡这里",
    },
    {
        "id": "demo-02",
        "room": "demo",
        "time": "21:04:00",
        "context": "【主播在说话，语音识别可能有错字和断句错误，别把错字当话题】\n"
        "[21:04:00] 今天人怎么这么少\n"
        "[21:04:05] 是不是都还没下班\n\n最近弹幕：\n观众A: 刚下班\n观众B: 在的",
    },
    {
        "id": "demo-03",
        "room": "demo",
        "time": "21:08:00",
        "context": "【主播在说话，语音识别可能有错字和断句错误，别把错字当话题】\n"
        "[21:08:00] 这个 boss 第二阶段会召唤小怪\n"
        "[21:08:05] 你们谁来教教我\n\n最近弹幕：\n观众A: 先清小怪\n观众B: 别贪刀",
    },
    {
        "id": "demo-04",
        "room": "demo",
        "time": "21:12:00",
        "context": "【主播在说话，语音识别可能有错字和断句错误，别把错字当话题】\n"
        "[21:12:00] 我去倒杯水 马上回来\n"
        "[21:12:03] 你们先聊\n\n最近弹幕：\n观众A: 好\n观众B: 快去快回",
    },
    {
        "id": "demo-05",
        "room": "demo",
        "time": "21:16:00",
        "context": "【主播在说话，语音识别可能有错字和断句错误，别把错字当话题】\n"
        "[21:16:00] 这个游戏的剧情真的写得好\n"
        "[21:16:05] 我上次玩到结局的时候哭了\n\n最近弹幕：\n观众A: 我也是\n观众B: 哪段",
    },
    {
        "id": "demo-06",
        "room": "demo",
        "time": "21:20:00",
        "context": "【主播在说话，语音识别可能有错字和断句错误，别把错字当话题】\n"
        "[21:20:00] 我电脑好像有点卡\n"
        "[21:20:04] 画面一直在掉帧\n\n最近弹幕：\n观众A: 换电脑\n观众B: 看看占用",
    },
    {
        "id": "demo-07",
        "room": "demo",
        "time": "21:24:00",
        "context": "【主播在说话，语音识别可能有错字和断句错误，别把错字当话题】\n"
        "[21:24:00] 下周可能请两天假\n"
        "[21:24:04] 有点事要处理\n\n最近弹幕：\n观众A: 去哪\n观众B: 注意休息",
    },
    {
        "id": "demo-08",
        "room": "demo",
        "time": "21:28:00",
        "context": "【主播在说话，语音识别可能有错字和断句错误，别把错字当话题】\n"
        "[21:28:00] 这首歌是我最近循环最多的\n"
        "[21:28:04] 你们听过吗\n\n最近弹幕：\n观众A: 没听过\n观众B: 好听",
    },
]


def collect_scenes(
    logs_dir: str | Path,
    danmaku_dir: str | Path | None = None,
    n: int = 40,
    seed: int | None = None,
) -> list[Scene]:
    """从 ``asr-live-<room>.log`` 抽固定场面；弹幕按时间就近配对。"""
    del seed  # 目前是确定性的；留参数给以后做随机分层
    logs = sorted(Path(logs_dir).glob("asr-live-*.log"))
    danmaku_root = Path(danmaku_dir) if danmaku_dir else None
    scenes: list[Scene] = []
    seen: set[str] = set()
    if not logs:
        return [Scene.from_dict({**item, "source": "example"}) for item in EXAMPLE_SCENES[:n]]

    per_file = max(1, n // max(1, len(logs)))
    for log in logs:
        room = log.stem.replace("asr-live-", "", 1)
        rows = _read_asr(log)
        if len(rows) < 12:
            continue
        danmaku_rows: list[tuple[int, str, str]] = []
        if danmaku_root is not None:
            danmaku_rows = _read_danmaku(danmaku_root / f"{room}.jsonl")

        step = max(1, (len(rows) - 8) // max(1, per_file * 2))
        picked = 0
        for start in range(7, len(rows), step):
            window = rows[start - 7 : start + 1]
            last_time = window[-1][0]
            sec = _seconds(last_time)
            nearby = _nearest(danmaku_rows, sec, 5) if sec is not None else []
            context = _build_context(window, nearby)
            key = re.sub(r"\s+", "", context)
            if key in seen:
                continue
            seen.add(key)
            scenes.append(
                Scene(
                    id=f"{room}-{start:04d}",
                    room=room,
                    time=last_time,
                    context=context,
                )
            )
            picked += 1
            if picked >= per_file or len(scenes) >= n:
                break
        if len(scenes) >= n:
            break

    if not scenes:
        return [Scene.from_dict({**item, "source": "example"}) for item in EXAMPLE_SCENES[:n]]
    return scenes[:n]


def save_scenes(scenes: list[Scene], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        for scene in scenes:
            handle.write(json.dumps(scene.to_dict(), ensure_ascii=False) + "\n")
    return target


def load_scenes(path: str | Path) -> list[Scene]:
    file = Path(path)
    if not file.is_file():
        return []
    scenes: list[Scene] = []
    for line in file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            scenes.append(Scene.from_dict(json.loads(line)))
        except json.JSONDecodeError:
            continue
    return scenes


def new_run_path(runs_dir: str | Path) -> Path:
    folder = Path(runs_dir)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"


def save_run(path: str | Path, meta: dict[str, Any], rows: list[dict[str, Any]]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"type": "meta", **meta}, ensure_ascii=False) + "\n")
        for row in rows:
            handle.write(json.dumps({"type": "result", **row}, ensure_ascii=False) + "\n")
    return target


def read_run(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    meta: dict[str, Any] = {}
    rows: list[dict[str, Any]] = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return meta, rows
    for line in lines:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if item.get("type") == "meta":
            meta = item
        elif item.get("type") == "result":
            rows.append(item)
    return meta, rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, float | int]:
    """均分、跳过率、平均长度。分数只统计真正打了分的行。"""
    replies = [row for row in rows if row.get("reply")]
    scored = [float(row["score"]) for row in replies if row.get("score")]
    skipped = sum(1 for row in rows if row.get("skipped"))
    lengths = [len(str(row["reply"])) for row in replies if not row.get("skipped")]
    total = len(rows)
    return {
        "n": total,
        "scored": len(scored),
        "mean": round(sum(scored) / len(scored), 2) if scored else 0.0,
        "skip_rate": round(skipped / total, 2) if total else 0.0,
        "avg_len": round(sum(lengths) / len(lengths), 1) if lengths else 0.0,
    }
