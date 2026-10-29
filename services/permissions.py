"""OneBot 群角色、AstrBot 管理员与名单权限判定。"""

from __future__ import annotations

from typing import Any

from .members import MemberService


class PermissionService:
    """名单与角色判定；群角色查询由成员服务短期缓存。"""

    def __init__(self, members: MemberService):
        self.members = members

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
        self, event: Any, settings: dict[str, Any], operation: str
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
        return await self.members.role(event) in group_roles
