from __future__ import annotations

import time


class FixedWindowLimiter:
    def __init__(self) -> None:
        self._window = int(time.monotonic())
        self._counts: dict[str, int] = {}

    def allow(self, key: str, limit: int) -> bool:
        if limit <= 0:
            return True
        now = int(time.monotonic())
        if now != self._window:
            self._window = now
            self._counts.clear()
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        return count <= limit
