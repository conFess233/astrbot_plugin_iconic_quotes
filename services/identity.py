"""合并转发与回复快照的作者身份临时纠正。"""

from __future__ import annotations

import copy
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from ..models import AuthorSnapshot, ForwardNode, QuoteRecord, ReplySnapshot
from ..utils.hashing import normalize_search
from .permissions import call_onebot_action


@dataclass(slots=True)
class IdentityResolution:
    """一次查询得到的副本、身份缺失数和成员列表可用状态。"""

    records: list[QuoteRecord]
    incomplete_count: int
    member_lookup_failed: bool


class AuthorIdentityService:
    """按手工别名、已存 QQ、唯一昵称的顺序纠正非普通作者。"""

    def __init__(self) -> None:
        self._member_cache: dict[str, tuple[float, dict[str, str], set[str]]] = {}

    async def resolve_records(
        self,
        event: Any,
        group_id: str,
        records: Iterable[QuoteRecord],
        settings: dict[str, Any],
    ) -> IdentityResolution:
        # 深拷贝是有意为之：旧 JSON 永远不因一次查询被隐式重写。
        resolved = copy.deepcopy(list(records))
        manual = self._manual_alias_map(group_id, settings)
        unique_names: dict[str, str] = {}
        member_ids: set[str] | None = None
        lookup_failed = False
        try:
            unique_names, member_ids = await self._member_directory(event, group_id)
        except Exception:  # noqa: BLE001 - OneBot 实现的异常类型不统一。
            lookup_failed = True

        incomplete = 0
        for record in resolved:
            record_incomplete = 0
            if record.type == "forward":
                for node in record.nodes:
                    record_incomplete += self._resolve_node(
                        node, manual, unique_names, member_ids
                    )
            # 回复快照同样不是普通消息作者，可安全应用管理员别名。
            record_incomplete += self._resolve_reply(
                record.reply, manual, unique_names, member_ids
            )
            record.identity_incomplete = record_incomplete > 0
            incomplete += record_incomplete
        return IdentityResolution(resolved, incomplete, lookup_failed)

    @classmethod
    def _resolve_node(
        cls,
        node: ForwardNode,
        manual: dict[str, str],
        unique_names: dict[str, str],
        member_ids: set[str] | None,
    ) -> int:
        cls._resolve_author(node.author, manual, unique_names, member_ids)
        incomplete = int(node.author.user_id is None)
        incomplete += cls._resolve_reply(node.reply, manual, unique_names, member_ids)
        for nested in node.nested_forwards:
            for child in nested.nodes:
                incomplete += cls._resolve_node(child, manual, unique_names, member_ids)
        return incomplete

    @classmethod
    def _resolve_reply(
        cls,
        reply: ReplySnapshot | None,
        manual: dict[str, str],
        unique_names: dict[str, str],
        member_ids: set[str] | None,
    ) -> int:
        if reply is None:
            return 0
        cls._resolve_author(reply.author, manual, unique_names, member_ids)
        return int(reply.author.user_id is None) + cls._resolve_reply(
            reply.reply, manual, unique_names, member_ids
        )

    @staticmethod
    def _resolve_author(
        author: AuthorSnapshot,
        manual: dict[str, str],
        unique_names: dict[str, str],
        member_ids: set[str] | None,
    ) -> None:
        normalized = normalize_search(author.nickname)
        # 手工别名可覆盖旧数据中的错误 QQ；普通消息作者不会经过此方法。
        if normalized in manual:
            author.user_id = manual[normalized]
            author.identity_source = "manual_alias"
            return
        if author.user_id and (member_ids is None or author.user_id in member_ids):
            return
        # 成员列表可用且旧 QQ 不在群内时，才允许唯一昵称纠正错误历史字段。
        if normalized in unique_names:
            author.user_id = unique_names[normalized]
            author.identity_source = "unique_nickname"
        else:
            if member_ids is not None and author.user_id not in member_ids:
                author.raw_user_id = author.raw_user_id or author.user_id
                author.user_id = None
            author.identity_source = "unknown"

    @staticmethod
    def _manual_alias_map(group_id: str, settings: dict[str, Any]) -> dict[str, str]:
        groups = settings.get("author_aliases")
        group = groups.get(str(group_id), {}) if isinstance(groups, dict) else {}
        return {
            normalize_search(alias): str(user_id)
            for user_id, aliases in group.items()
            for alias in aliases
        }

    async def _member_directory(
        self, event: Any, group_id: str
    ) -> tuple[dict[str, str], set[str]]:
        cached = self._member_cache.get(group_id)
        if cached and cached[0] > time.monotonic():
            return cached[1], cached[2]
        payload = await call_onebot_action(
            event, "get_group_member_list", group_id=int(group_id), no_cache=False
        )
        members = self._member_list(payload)
        if not members:
            raise RuntimeError("群成员列表为空")
        owners: dict[str, set[str]] = {}
        for member in members:
            user_id = str(member.get("user_id") or member.get("uin") or "").strip()
            if not user_id:
                continue
            for name in (member.get("card"), member.get("nickname")):
                normalized = normalize_search(str(name or "").strip())
                if normalized:
                    owners.setdefault(normalized, set()).add(user_id)
        unique = {
            name: next(iter(user_ids))
            for name, user_ids in owners.items()
            if len(user_ids) == 1
        }
        member_ids = {
            str(member.get("user_id") or member.get("uin") or "").strip()
            for member in members
        }
        member_ids.discard("")
        # 查询和发送可能连续进行，短缓存可避免反复拉取完整群成员列表。
        self._member_cache[group_id] = (
            time.monotonic() + 300,
            unique,
            member_ids,
        )
        return unique, member_ids

    @classmethod
    def _member_list(cls, payload: Any) -> list[dict[str, Any]]:
        current = payload
        for _ in range(4):
            if isinstance(current, list):
                return [item for item in current if isinstance(item, dict)]
            if not isinstance(current, dict):
                return []
            current = current.get("data")
        return []
