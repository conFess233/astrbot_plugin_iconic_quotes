"""OneBot 群角色、AstrBot 管理员与名单权限判定。"""

from __future__ import annotations

import asyncio
import time
from typing import Any


class RoleLookupError(RuntimeError):
    """OneBot 无法可靠返回群角色。"""


class PermissionService:
    """执行不持久化的短期群角色查询。"""

    def __init__(self, ttl_seconds: float = 60.0):
        self.ttl_seconds = ttl_seconds
        self._cache: dict[tuple[str, str], tuple[float, str]] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def list_allows(settings: dict[str, Any], group_id: str, user_id: str) -> bool:
        """名单优先级为黑名单高于白名单。"""
        if group_id in settings["group_blacklist"]:
            return False
        if settings["group_whitelist"] and group_id not in settings["group_whitelist"]:
            return False
        if user_id in settings["user_blacklist"]:
            return False
        return not settings["user_whitelist"] or user_id in settings["user_whitelist"]

    async def allows(
        self,
        event: Any,
        settings: dict[str, Any],
        operation: str,
    ) -> bool:
        """判断当前操作者是否命中指定操作的任一角色。"""
        roles = set(settings[f"{operation}_roles"])
        if "everyone" in roles:
            return True
        if "bot_admin" in roles and bool(event.is_admin()):
            return True
        group_roles = roles & {"owner", "admin", "member"}
        if not group_roles:
            return False
        role = await self._group_role(event)
        return role in group_roles

    async def _group_role(self, event: Any) -> str:
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        key = (group_id, user_id)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        async with self._lock:
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                return cached[1]
            result = await call_onebot_action(
                event,
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=True,
            )
            if not isinstance(result, dict):
                raise RoleLookupError("暂时无法验证群权限")
            role = str(result.get("role") or "")
            if role not in {"owner", "admin", "member"}:
                raise RoleLookupError("暂时无法验证群权限")
            self._cache[key] = (time.monotonic() + self.ttl_seconds, role)
            return role


def _unwrap_onebot(value: Any) -> Any:
    if isinstance(value, dict) and "data" in value:
        return value.get("data")
    return value


async def call_onebot_action(event: Any, action: str, **params: Any) -> Any:
    """兼容当前 aiocqhttp 暴露的两种 call_action 位置。"""
    bot = getattr(event, "bot", None)
    candidates = [getattr(bot, "call_action", None)]
    candidates.append(getattr(getattr(bot, "api", None), "call_action", None))
    last_error: Exception | None = None
    for caller in candidates:
        if not callable(caller):
            continue
        try:
            return _unwrap_onebot(await caller(action, **params))
        except Exception as exc:  # noqa: BLE001 - OneBot 实现异常类型不统一。
            last_error = exc
    if last_error:
        raise last_error
    raise RuntimeError("当前 OneBot 适配器未提供 call_action")
