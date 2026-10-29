"""群成员信息缓存与历史作者身份的读取时纠正。

成员列表、单人资料和群角色共用这一处有容量上限的短期缓存，替代各模块各自缓存与逐人请求。
"""

from __future__ import annotations

import copy
import time
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..models import AuthorSnapshot, QuoteRecord
from ..utils.hashing import normalize_search
from ..utils.timefmt import error_reason
from .onebot import call_action, member_name

PROFILE_TTL = 300.0
ROLE_TTL = 60.0
CACHE_LIMIT = 4096


class RoleLookupError(RuntimeError):
    """OneBot 无法可靠返回群角色。"""


@dataclass(slots=True)
class MemberProfile:
    name: str | None
    is_bot: bool | None


@dataclass(slots=True)
class Directory:
    """群成员列表派生的索引：唯一昵称 → QQ、成员 QQ 集合、QQ → 规范化名称。"""

    unique_names: dict[str, str]
    member_ids: set[str]
    names_by_id: dict[str, set[str]]


@dataclass(slots=True)
class IdentityResolution:
    """一次查询得到的副本、身份缺失数和成员列表可用状态。"""

    records: list[QuoteRecord]
    incomplete_count: int
    member_lookup_failed: bool


class _TTLCache:
    def __init__(self, ttl: float, limit: int = CACHE_LIMIT):
        self.ttl = ttl
        self.limit = limit
        self._items: OrderedDict[Any, tuple[float, Any]] = OrderedDict()

    def get(self, key: Any) -> tuple[bool, Any]:
        item = self._items.get(key)
        if item is None or item[0] <= time.monotonic():
            self._items.pop(key, None)
            return False, None
        self._items.move_to_end(key)
        return True, item[1]

    def put(self, key: Any, value: Any) -> None:
        self._items[key] = (time.monotonic() + self.ttl, value)
        self._items.move_to_end(key)
        while len(self._items) > self.limit:
            self._items.popitem(last=False)

    def clear(self) -> None:
        self._items.clear()


class MemberService:
    """群成员资料、角色与作者身份纠正。"""

    def __init__(self) -> None:
        self._profiles = _TTLCache(PROFILE_TTL)
        self._roles = _TTLCache(ROLE_TTL)
        self._directories = _TTLCache(PROFILE_TTL, limit=256)

    def clear(self) -> None:
        self._profiles.clear()
        self._roles.clear()
        self._directories.clear()

    async def profile(self, event: Any, group_id: str, user_id: str) -> MemberProfile:
        """群名片优先的显示名与是否机器人；失败时返回空资料并短期缓存。"""
        key = (group_id, user_id)
        hit, cached = self._profiles.get(key)
        if hit:
            return cached
        profile = MemberProfile(None, None)
        try:
            payload = await call_action(
                event,
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=False,
            )
            if isinstance(payload, dict):
                is_bot = None
                if "is_robot" in payload or "is_bot" in payload:
                    is_bot = bool(payload.get("is_robot") or payload.get("is_bot"))
                profile = MemberProfile(member_name(payload), is_bot)
        except Exception as exc:  # noqa: BLE001 - OneBot 群成员接口异常类型不统一。
            logger.debug(
                "群典：读取群成员信息失败；群=%s，用户=%s，原因=%s",
                group_id,
                user_id,
                error_reason(exc),
            )
        self._profiles.put(key, profile)
        return profile

    async def role(self, event: Any) -> str:
        """当前发送者的群角色；不可用时抛出 RoleLookupError。"""
        key = (str(event.get_group_id()), str(event.get_sender_id()))
        hit, cached = self._roles.get(key)
        if hit:
            return cached
        result = await call_action(
            event,
            "get_group_member_info",
            group_id=int(key[0]),
            user_id=int(key[1]),
            no_cache=True,
        )
        role = str(result.get("role") or "") if isinstance(result, dict) else ""
        if role not in {"owner", "admin", "member"}:
            raise RoleLookupError("暂时无法验证群权限")
        self._roles.put(key, role)
        return role

    async def directory(self, event: Any, group_id: str) -> Directory:
        """完整成员列表的索引；失败时抛出异常。"""
        hit, cached = self._directories.get(group_id)
        if hit:
            return cached
        payload = await call_action(
            event, "get_group_member_list", group_id=int(group_id), no_cache=False
        )
        members = (
            [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, list)
            else []
        )
        if not members:
            raise RuntimeError("群成员列表为空")
        owners: dict[str, set[str]] = {}
        names_by_id: dict[str, set[str]] = {}
        for member in members:
            user_id = str(member.get("user_id") or member.get("uin") or "").strip()
            if not user_id:
                continue
            names_by_id.setdefault(user_id, set())
            for name in (member.get("card"), member.get("nickname")):
                normalized = normalize_search(str(name or "").strip())
                if normalized:
                    owners.setdefault(normalized, set()).add(user_id)
                    names_by_id[user_id].add(normalized)
        directory = Directory(
            unique_names={
                name: next(iter(ids)) for name, ids in owners.items() if len(ids) == 1
            },
            member_ids=set(names_by_id),
            names_by_id=names_by_id,
        )
        self._directories.put(group_id, directory)
        return directory

    async def refresh_names(
        self, event: Any, group_id: str, records: Iterable[QuoteRecord]
    ) -> None:
        """发送前按 QQ 刷新为当前群名片，失败时保留存档快照。"""
        by_id: dict[str, list[AuthorSnapshot]] = {}
        for record in records:
            for author in record.iter_authors():
                if author.user_id:
                    by_id.setdefault(author.user_id, []).append(author)
        for user_id, snapshots in by_id.items():
            name = (await self.profile(event, group_id, user_id)).name
            if name:
                for snapshot in snapshots:
                    snapshot.nickname = name

    async def candidate_names(
        self, event: Any, group_id: str, user_id: str, settings: dict[str, Any]
    ) -> set[str]:
        """可能被纠正为该用户的规范化昵称：手工别名与当前群名片/昵称。"""
        names = {
            alias
            for alias, target in self.alias_map(group_id, settings).items()
            if target == user_id
        }
        try:
            names.update(
                (await self.directory(event, group_id)).names_by_id.get(user_id, set())
            )
        except Exception as exc:  # noqa: BLE001 - 成员列表不可用时只按存档 QQ 与别名查询。
            logger.debug("群典：读取群成员列表失败，原因=%s", error_reason(exc))
        return names

    async def resolve_records(
        self,
        event: Any,
        group_id: str,
        records: Iterable[QuoteRecord],
        settings: dict[str, Any],
    ) -> IdentityResolution:
        """按手工别名、已存 QQ、唯一昵称的顺序纠正转发节点与回复作者。"""
        # 深拷贝是有意为之：存档永远不因一次查询被隐式改写。
        resolved = copy.deepcopy(list(records))
        manual = self.alias_map(group_id, settings)
        directory: Directory | None = None
        lookup_failed = False
        try:
            directory = await self.directory(event, group_id)
        except Exception:  # noqa: BLE001 - OneBot 实现的异常类型不统一。
            lookup_failed = True
        incomplete = 0
        for record in resolved:
            record_incomplete = 0
            for node in record.iter_nodes():
                self._resolve_author(node.author, manual, directory)
                record_incomplete += node.author.user_id is None
            # 回复快照同样不是普通消息作者，可安全应用管理员别名。
            for reply in record.iter_replies():
                self._resolve_author(reply.author, manual, directory)
                record_incomplete += reply.author.user_id is None
            record.identity_incomplete = record_incomplete > 0
            incomplete += record_incomplete
        return IdentityResolution(resolved, incomplete, lookup_failed)

    @staticmethod
    def _resolve_author(
        author: AuthorSnapshot,
        manual: dict[str, str],
        directory: Directory | None,
    ) -> None:
        normalized = normalize_search(author.nickname)
        member_ids = directory.member_ids if directory else None
        unique_names = directory.unique_names if directory else {}
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
            return
        if member_ids is not None and author.user_id not in member_ids:
            author.raw_user_id = author.raw_user_id or author.user_id
            author.user_id = None
        author.identity_source = "unknown"

    @staticmethod
    def alias_map(group_id: str, settings: dict[str, Any]) -> dict[str, str]:
        """群内“规范化别名 → QQ”。"""
        groups = settings.get("author_aliases")
        group = groups.get(str(group_id), {}) if isinstance(groups, dict) else {}
        return {
            normalize_search(alias): str(user_id)
            for user_id, aliases in group.items()
            for alias in aliases
        }
