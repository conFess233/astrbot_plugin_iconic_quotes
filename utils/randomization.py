"""随机发送数量的边界收敛。"""

from __future__ import annotations

import random
from collections.abc import Callable


def resolve_send_count(
    maximum: int,
    random_enabled: bool,
    chooser: Callable[[int, int], int] | None = None,
) -> int:
    """返回固定数量，或在 1 到配置上限之间抽取一个数量。"""
    upper = max(1, int(maximum))
    if not random_enabled:
        return upper
    return (chooser or random.randint)(1, upper)
