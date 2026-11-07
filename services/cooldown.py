"""按群的忙碌与冷却状态：一个群的慢操作不影响其他群。"""

from __future__ import annotations

import time
from dataclasses import dataclass

# 状态表超过这个大小时顺手清掉已结束的条目；只是防止无限增长，不影响判定。
PRUNE_THRESHOLD = 1024


@dataclass(slots=True)
class CooldownDecision:
    """一次触发进入门闩后的判定结果。"""

    accepted: bool
    should_notify: bool = False


@dataclass(slots=True)
class _GroupState:
    busy: bool = False
    deadline: float = 0.0
    notified: bool = False


class GroupCooldown:
    """不排队的按群互斥与冷却门闩；方法内没有 await，事件循环上天然串行。"""

    def __init__(self) -> None:
        self._groups: dict[str, _GroupState] = {}
        self._notices: dict[tuple[str, str], float] = {}

    def _prune(self, now: float) -> None:
        if len(self._groups) > PRUNE_THRESHOLD:
            self._groups = {
                key: state
                for key, state in self._groups.items()
                if state.busy or state.deadline > now
            }
        if len(self._notices) > PRUNE_THRESHOLD:
            self._notices = {
                key: deadline
                for key, deadline in self._notices.items()
                if deadline > now
            }

    async def try_enter(
        self, group_id: str, *, quiet: bool = False
    ) -> CooldownDecision:
        """尝试开始操作；繁忙或冷却时每群仅通知一次。

        quiet 用于不向群里提示的情况（戳一戳、后台重试），不占用该群的通知机会。
        """
        now = time.monotonic()
        self._prune(now)
        state = self._groups.setdefault(group_id, _GroupState())
        if not state.busy and now >= state.deadline:
            state.busy = True
            state.notified = False
            return CooldownDecision(accepted=True)
        if quiet:
            return CooldownDecision(accepted=False)
        should_notify = not state.notified
        state.notified = True
        return CooldownDecision(accepted=False, should_notify=should_notify)

    async def leave(
        self, group_id: str, cooldown_ms: int, *, sent_message: bool
    ) -> None:
        """结束操作，并从最后一批成功发送后开始冷却。"""
        if sent_message and cooldown_ms > 0:
            state = self._groups.setdefault(group_id, _GroupState())
            state.busy = False
            state.deadline = time.monotonic() + cooldown_ms / 1000
        else:
            self._groups.pop(group_id, None)

    def allow_notice(self, group_id: str, user_id: str, cooldown_ms: int) -> bool:
        """限制权限提示的频率：无权限的用户不占用群冷却，也不能借此刷屏。"""
        now = time.monotonic()
        self._prune(now)
        key = (group_id, user_id)
        if self._notices.get(key, 0.0) > now:
            return False
        if cooldown_ms > 0:
            self._notices[key] = now + cooldown_ms / 1000
        return True
