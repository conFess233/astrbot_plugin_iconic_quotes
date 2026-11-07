"""表情回应投票：统计每条消息有多少不同成员回应了指定表情。

只依赖“谁在哪条消息上加/减了哪个表情”，不使用事件中的 count 字段，
因为不同 OneBot 实现对它的含义不一致。票数保存在内存中，重启后重新累计；
已收录的消息即使再次达到阈值，也会被内容哈希去重。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field

VOTE_TTL_SECONDS = 3 * 86400
MAX_TRACKED_MESSAGES = 5000


@dataclass(slots=True)
class _Votes:
    updated_at: float
    voters: dict[str, set[str]] = field(default_factory=dict)
    triggered: bool = False


class ReactionTracker:
    """按 (群, 消息) 累计投票，每条消息最多触发一次。"""

    def __init__(self) -> None:
        self._items: OrderedDict[tuple[str, str], _Votes] = OrderedDict()

    def _prune(self, now: float) -> None:
        while self._items:
            oldest = next(iter(self._items.values()))
            if (
                len(self._items) <= MAX_TRACKED_MESSAGES
                and now - oldest.updated_at <= VOTE_TTL_SECONDS
            ):
                break
            self._items.popitem(last=False)

    def apply(
        self,
        group_id: str,
        message_id: str,
        user_id: str,
        emoji_ids: Iterable[str],
        *,
        added: bool,
        threshold: int,
    ) -> int | None:
        """登记一次回应；刚好首次达到阈值时返回票数，否则返回 None。"""
        now = time.monotonic()
        self._prune(now)
        key = (group_id, message_id)
        votes = self._items.get(key)
        if votes is None:
            if not added:
                return None
            votes = _Votes(updated_at=now)
            self._items[key] = votes
        self._items.move_to_end(key)
        votes.updated_at = now
        emojis = votes.voters.setdefault(user_id, set())
        if added:
            emojis.update(emoji_ids)
        else:
            emojis.difference_update(emoji_ids)
        if not emojis:
            votes.voters.pop(user_id, None)
        count = len(votes.voters)
        if votes.triggered or count < threshold:
            return None
        votes.triggered = True
        return count
