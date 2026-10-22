"""``python -m live_deck.fish`` 的入口。"""

from __future__ import annotations

import asyncio

from .engine import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
