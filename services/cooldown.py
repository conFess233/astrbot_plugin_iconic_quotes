"""插件全局忙碌与冷却状态。"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


@dataclass(slots=True)
class CooldownDecision:
    """一次触发进入全局门闩后的判定结果。"""

    accepted: bool
    should_notify: bool = False


class GlobalCooldown:
    """不排队的全局互斥与冷却门闩。"""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._busy = False
        self._deadline = 0.0
        self._notified_groups: set[str] = set()

    async def try_enter(self, group_id: str) -> CooldownDecision:
        """尝试开始操作；繁忙或冷却时每群仅通知一次。"""
        async with self._lock:
            now = time.monotonic()
            if not self._busy and now >= self._deadline:
                self._busy = True
                self._notified_groups.clear()
                return CooldownDecision(accepted=True)
            should_notify = group_id not in self._notified_groups
            self._notified_groups.add(group_id)
            return CooldownDecision(accepted=False, should_notify=should_notify)

    async def leave(self, cooldown_ms: int, *, sent_message: bool) -> None:
        """结束操作，并从最后一批成功发送后开始冷却。"""
        async with self._lock:
            self._busy = False
            if sent_message and cooldown_ms > 0:
                self._deadline = time.monotonic() + cooldown_ms / 1000
            else:
                self._deadline = time.monotonic()
                self._notified_groups.clear()
