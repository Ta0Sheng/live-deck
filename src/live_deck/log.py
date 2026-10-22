"""统一的时间戳日志。故意保持极简：所有输出都去 stdout，方便重定向。"""

from __future__ import annotations

import sys
import time


def log(message: str, *, stream=None) -> None:
    """``HH:MM:SS message``。"""
    out = stream if stream is not None else sys.stdout
    print(f"{time.strftime('%H:%M:%S')} {message}", file=out, flush=True)
