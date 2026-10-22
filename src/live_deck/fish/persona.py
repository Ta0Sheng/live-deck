"""人格文件加载。

人格和记忆是两件事：

* **人格**（``persona/persona.md``）是这个角色「是谁、怎么说话」，进仓库、随项目走。
* **记忆**（``fish.memory`` 指向的文件、``data/`` 里的样本和知识库）是这个角色
  「经历过什么」，不进仓库，新电脑上是空的。

人格文件就是一段 Markdown，原样作为 system prompt 发给模型。文件顶部的
``<!-- ... -->`` 注释会在加载时去掉，方便写「这段为什么这么改」。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import get_settings

_COMMENT = re.compile(r"<!--.*?-->", re.S)


class PersonaError(RuntimeError):
    """人格文件缺失或为空。"""


def _path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    return get_settings().persona_file


def strip_comments(text: str) -> str:
    return _COMMENT.sub("", text)


def load_persona(path: str | Path | None = None) -> str:
    """读人格文件，去掉注释和首尾空白。"""
    file = _path(path)
    try:
        raw = file.read_text(encoding="utf-8")
    except OSError as exc:
        raise PersonaError(
            f"读不到人格文件：{file}\n"
            "  仓库里应该自带 persona/persona.md；如果你删了，从 git 里恢复一份，"
            "或在 config.toml 的 fish.persona 指向你自己的文件。"
        ) from exc
    text = strip_comments(raw).strip()
    if not text:
        raise PersonaError(f"人格文件是空的：{file}")
    return text


def persona_stats(path: str | Path | None = None) -> dict[str, int | str]:
    """给 ``live-deck doctor`` 看的一点点统计。"""
    file = _path(path)
    text = load_persona(file)
    return {
        "path": str(file),
        "chars": len(text),
        "lines": text.count("\n") + 1,
    }
