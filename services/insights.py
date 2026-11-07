"""整群统计：参与排行、收录排行与个人统计。纯计算，不访问网络和数据库。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ..models import QuoteRecord
from ..utils.timefmt import local_time


@dataclass(slots=True)
class UserStats:
    """一名成员在当前群的统计；合并转发整体计为一条。"""

    total: int = 0
    messages: int = 0
    forwards: int = 0
    with_images: int = 0
    earliest: str | None = None
    latest: str | None = None


@dataclass(slots=True)
class Insights:
    total: int = 0
    users: dict[str, UserStats] = field(default_factory=dict)
    recorders: Counter[str] = field(default_factory=Counter)
    names: dict[str, str] = field(default_factory=dict)

    def ranking(self) -> list[tuple[str, int]]:
        """按参与条数从多到少；并列时按 QQ 排序，结果稳定。"""
        return sorted(
            ((user_id, stats.total) for user_id, stats in self.users.items()),
            key=lambda item: (-item[1], item[0]),
        )

    def rank_of(self, user_id: str) -> int | None:
        for index, (candidate, _) in enumerate(self.ranking(), start=1):
            if candidate == user_id:
                return index
        return None


def _participants(record: QuoteRecord) -> set[str]:
    if record.type == "message":
        return (
            {record.author.user_id}
            if record.author and record.author.user_id
            else set()
        )
    return {node.author.user_id for node in record.iter_nodes() if node.author.user_id}


def compute(records: list[QuoteRecord]) -> Insights:
    result = Insights(total=len(records))
    for record in records:
        for author in record.iter_authors():
            if author.user_id and author.nickname:
                result.names[author.user_id] = author.nickname
        for user_id in _participants(record):
            stats = result.users.setdefault(user_id, UserStats())
            stats.total += 1
            if record.type == "message":
                stats.messages += 1
            else:
                stats.forwards += 1
            if record.media_hashes():
                stats.with_images += 1
            if record.recorded_at:
                if not stats.earliest or record.recorded_at < stats.earliest:
                    stats.earliest = record.recorded_at
                if not stats.latest or record.recorded_at > stats.latest:
                    stats.latest = record.recorded_at
        if record.recorded_by.user_id:
            result.recorders[record.recorded_by.user_id] += 1
    return result


def ranking_text(insights: Insights, names: dict[str, str], *, limit: int) -> str:
    """排行榜文本；names 为 QQ → 当前显示名。"""

    def name(user_id: str) -> str:
        return names.get(user_id) or insights.names.get(user_id) or user_id

    lines = [f"群典排行（共 {insights.total} 条）"]
    lines += [
        f"{index}. {name(user_id)}：{count} 条"
        for index, (user_id, count) in enumerate(insights.ranking()[:limit], start=1)
    ]
    recorders = sorted(insights.recorders.items(), key=lambda item: (-item[1], item[0]))
    if recorders:
        lines.append("最勤快的收录者")
        lines += [
            f"{index}. {name(user_id)}：{count} 条"
            for index, (user_id, count) in enumerate(recorders[:3], start=1)
        ]
    return "\n".join(lines)


def user_text(insights: Insights, user_id: str, display_name: str) -> str | None:
    """个人统计文本；该成员既没有记录也没有收录过时返回 None。"""
    stats = insights.users.get(user_id)
    recorded = insights.recorders.get(user_id, 0)
    if stats is None and not recorded:
        return None
    current: Any = stats or UserStats()
    lines = [f"{display_name} 的群典统计"]
    rank = insights.rank_of(user_id)
    lines.append(
        f"参与记录：{current.total} 条"
        + (f"（第 {rank} 名 / 共 {len(insights.users)} 人）" if rank else "")
    )
    lines += [
        f"普通金句：{current.messages}",
        f"合并转发：{current.forwards}",
        f"含图片记录：{current.with_images}",
        f"最早收录：{local_time(current.earliest) or '无'}",
        f"最近收录：{local_time(current.latest) or '无'}",
        f"收录过的群典：{recorded} 条",
    ]
    return "\n".join(lines)
