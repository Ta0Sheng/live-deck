"""礼物预算：一个按天记账的小账本。

``fish.allow_gift`` 只是「允许花钱」，花多少由这里卡住。默认
``fish.gift_daily_budget = 0`` —— 一分钱都不许花。

账本写在 ``data/state/gift-ledger.json``，不进 git。
"""

from __future__ import annotations

import json
import time
from pathlib import Path


class GiftBudget:
    def __init__(self, ledger: Path, daily_limit: int) -> None:
        self.ledger = Path(ledger)
        self.daily_limit = max(0, int(daily_limit))

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    def _read(self) -> dict:
        try:
            data = json.loads(self.ledger.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if data.get("date") != self._today():
            return {}
        return data

    def spent_today(self) -> int:
        try:
            return int(self._read().get("spent") or 0)
        except (TypeError, ValueError):
            return 0

    def can_spend(self, amount: int) -> bool:
        """预算关着（<=0）永远返回 False —— 这是硬闸，不依赖模型自觉。"""
        if self.daily_limit <= 0 or amount <= 0:
            return False
        return self.spent_today() + int(amount) <= self.daily_limit

    def record(self, amount: int) -> None:
        if amount <= 0:
            return
        spent = self.spent_today() + int(amount)
        try:
            self.ledger.parent.mkdir(parents=True, exist_ok=True)
            self.ledger.write_text(
                json.dumps({"date": self._today(), "spent": spent}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
