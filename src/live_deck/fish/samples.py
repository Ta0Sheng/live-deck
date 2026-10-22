"""样本检索：把「最近 N 条」换成「和此刻最像的 N 条 + 最近几条兜底」。

为什么不再只取最近 N 条
------------------------
`data/samples/*.md` 是 append-only 的，但旧实现只读最后 24/12 条。第 25 条好评出现时，
第 1 条就永远进不了上下文 —— 鱼只有「最近偏好」，没有「长期经验」。

这个模块做的事：

1. 把样本解析成 ``(场面, 回复, 备注)``；
2. 用字符二元组算当前语境和样本场面的相似度（不依赖分词 / 不花钱）；
3. 选 top-k 相似 + 最近 N 条兜底 —— 新反馈永远能起作用，旧经验也不会被挤掉。

纯本地、无依赖，可以直接单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Sample",
    "format_sample",
    "load_samples",
    "parse_line",
    "select_samples",
    "similarity",
]

_HEADER = ("#", ">")
_SEP = re.compile(r"\s*(?:——|—|--)\s*")
_PUNCT = re.compile(r"[\s，。！？、,.!?~～：:；;“”\"'‘’（）()\[\]【】《》<>/\\|·]+")
_NOTE_MAX = 40
_SCENE_MAX = 48


@dataclass(frozen=True)
class Sample:
    """一条样本：在什么场面、说了什么、为什么（可选）。"""

    scene: str
    reply: str
    note: str = ""
    order: int = 0

    def prompt(self, mark: str = "✓") -> str:
        """渲染成 prompt 里的一行。"""
        scene = f"【{self.scene[:_SCENE_MAX]}】" if self.scene else ""
        note = f"（{self.note[:_NOTE_MAX]}）" if self.note else ""
        return f"{mark} {scene}{self.reply}{note}"


def parse_line(line: str, order: int = 0) -> Sample | None:
    """解析一行 ``- 【场面】回复 —— 备注``；解析不出就返回 None。"""
    text = line.strip().lstrip("-·* ").strip()
    if not text or text.startswith(_HEADER):
        return None

    scene = ""
    reply = text
    if text.startswith("【"):
        end = text.find("】")
        if end > 0:
            scene = text[1:end].strip()
            reply = text[end + 1 :].strip()

    note = ""
    match = _SEP.search(reply)
    if match:
        note = reply[match.end() :].strip()
        reply = reply[: match.start()].strip()

    if not reply:
        return None
    return Sample(scene=scene, reply=reply, note=note, order=order)


def load_samples(path: str | Path, limit: int | None = None) -> list[Sample]:
    """读样本文件；``limit`` 只保留最后 N 行（用于快速调试）。"""
    file = Path(path)
    if not file.is_file():
        return []
    try:
        lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if limit and limit > 0:
        lines = lines[-limit:]
    samples: list[Sample] = []
    for index, line in enumerate(lines):
        sample = parse_line(line, order=index)
        if sample is not None:
            samples.append(sample)
    return samples


def _normalize(text: str) -> str:
    return _PUNCT.sub("", (text or "").lower())


def _grams(text: str, size: int = 2) -> set[str]:
    if not text:
        return set()
    if len(text) <= size:
        return {text}
    return {text[i : i + size] for i in range(len(text) - size + 1)}


def similarity(query: str, text: str) -> float:
    """0–1。短文本被长语境「包含」时得分高，而不是被长度稀释。

    用 70% 包含度 + 30% Dice 的混合：纯 Jaccard 会让 10 字的场面和 300 字的语境
    永远接近 0；纯包含度又容易被「主播」「这个」这类高频词抬高。
    """
    left, right = _normalize(query), _normalize(text)
    if not left or not right:
        return 0.0
    a, b = _grams(left), _grams(right)
    inter = len(a & b)
    if not inter:
        return 0.0
    containment = inter / max(1, len(a))
    dice = 2 * inter / max(1, len(a) + len(b))
    return 0.7 * containment + 0.3 * dice


def select_samples(
    pool: list[Sample],
    context: str,
    k: int = 8,
    recent: int = 4,
    min_score: float = 0.0,
) -> list[Sample]:
    """选「像此刻的」k 条 + 「最近」recent 条；去重，总数不超过 k+recent。

    相似和最近分开，是为了同时满足两件事：
    * 旧经验能回来（相似检索）；
    * 你刚点的 ✔/✘ 立刻生效（最近兜底），不会被相似度淹没。
    """
    if not pool:
        return []
    k = max(0, int(k))
    recent = max(0, int(recent))

    scored: list[tuple[float, int, Sample]] = []
    for index, sample in enumerate(pool):
        probe = sample.scene or sample.reply
        scored.append((similarity(probe, context), index, sample))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)

    chosen: list[Sample] = []
    chosen_index: set[int] = set()
    for score, index, sample in scored:
        if len(chosen) >= k:
            break
        if score <= min_score:
            break
        chosen.append(sample)
        chosen_index.add(index)

    if recent:
        for index in range(max(0, len(pool) - recent), len(pool)):
            if len(chosen) >= k + recent:
                break
            if index not in chosen_index:
                chosen.append(pool[index])
                chosen_index.add(index)

    # 一条相似的都没有（冷启动 / 全是新话题）：退回最近 k 条，别让 prompt 空着
    if not chosen:
        chosen = pool[-k:] if k else pool[-recent:]
    return chosen


def format_sample(sample: Sample, mark: str = "✓") -> str:
    return sample.prompt(mark)
