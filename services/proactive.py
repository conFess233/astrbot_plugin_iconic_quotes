"""定时任务：每日一典、那年今日与自动备份。

定时任务没有消息事件。这里用 ``GroupSession`` 提供发送链路需要的那部分事件接口，
发送、降级和重试逻辑因此可以与聊天指令共用。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timedelta
from typing import Any

from astrbot.api import logger
from astrbot.api.event import MessageChain

from ..utils.timefmt import error_reason
from .backup import BackupService
from .operations import QuoteOperations
from .permissions import PermissionService
from .settings import SettingsService
from .storage import QuoteStorage

TICK_SECONDS = 30
# 重启或停机错过推送时间后，超过这个窗口就不再补发，避免半夜突然发消息。
CATCH_UP_MINUTES = 30


class GroupSession:
    """某个群的“无事件”发送上下文，接口与消息事件中用到的部分一致。"""

    def __init__(self, context: Any, group_id: str, info: dict[str, str]):
        self._context = context
        self._group_id = group_id
        self._umo = info["umo"]
        self._self_id = info["self_id"]
        platform = context.get_platform_inst(info["platform_id"])
        if platform is None:
            raise RuntimeError(f"找不到平台实例 {info['platform_id']}")
        self.bot = platform.get_client()

    def get_group_id(self) -> str:
        return self._group_id

    def get_self_id(self) -> str:
        return self._self_id

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    async def send(self, chain: MessageChain) -> None:
        if not await self._context.send_message(self._umo, chain):
            raise RuntimeError("主动发送未找到匹配的平台")


class SessionRegistry:
    """记住每个群最近一次的会话信息；只有变化时才写库。"""

    def __init__(self, storage: QuoteStorage):
        self.storage = storage
        self.sessions: dict[str, dict[str, str]] = {}

    async def load(self) -> None:
        self.sessions = await self.storage.group_sessions()

    async def note(self, event: Any) -> None:
        group_id = str(event.get_group_id() or "")
        if not group_id or event.get_platform_name() != "aiocqhttp":
            return
        info = {
            "umo": str(event.unified_msg_origin),
            "platform_id": str(event.get_platform_id()),
            "self_id": str(event.get_self_id() or ""),
        }
        if self.sessions.get(group_id) == info:
            return
        self.sessions[group_id] = info
        await self.storage.save_group_session(group_id, **info)


def is_due(now: datetime, clock: str, last_date: str | None) -> bool:
    """今天还没推送过，且当前时间在配置时间之后的补发窗口内。"""
    if last_date == now.date().isoformat():
        return False
    hour, minute = (int(part) for part in clock.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target <= now <= target + timedelta(minutes=CATCH_UP_MINUTES)


class Scheduler:
    """低频轮询；每项任务互相隔离，一项失败不影响其他项。"""

    def __init__(
        self,
        *,
        context: Any,
        settings: SettingsService,
        storage: QuoteStorage,
        operations: QuoteOperations,
        backups: BackupService,
        sessions: SessionRegistry,
    ):
        self.context = context
        self.settings = settings
        self.storage = storage
        self.operations = operations
        self.backups = backups
        self.sessions = sessions
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick(datetime.now().astimezone())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台循环不能因单次失败退出。
                logger.warning("群典：定时任务失败，原因=%s", error_reason(exc))
            await asyncio.sleep(TICK_SECONDS)

    async def tick(self, now: datetime) -> None:
        await self._auto_backup()
        for group_id, info in list(self.sessions.sessions.items()):
            values = self.settings.for_group(group_id)
            if not PermissionService.group_allows(values, group_id):
                continue
            for name, enabled, clock, push in (
                (
                    "daily",
                    values["daily_quote_enabled"],
                    values["daily_quote_time"],
                    self.operations.push_daily,
                ),
                (
                    "on_this_day",
                    values["on_this_day_enabled"],
                    values["on_this_day_time"],
                    lambda event, current: self.operations.push_on_this_day(
                        event, current, now
                    ),
                ),
            ):
                if enabled:
                    await self._push(name, group_id, info, clock, now, push, values)

    async def _push(
        self,
        name: str,
        group_id: str,
        info: dict[str, str],
        clock: str,
        now: datetime,
        push: Any,
        values: dict[str, Any],
    ) -> None:
        key = f"push:{name}:{group_id}"
        if not is_due(now, clock, await self.storage.meta(key)):
            return
        # 发送前先标记：结果未知时宁可少发一次，也不重复推送。
        await self.storage.set_meta(key, now.date().isoformat())
        try:
            await push(GroupSession(self.context, group_id, info), values)
        except Exception as exc:  # noqa: BLE001 - 单个群失败不影响其他群。
            logger.warning(
                "群典：定时推送失败；类型=%s，群=%s，原因=%s",
                name,
                group_id,
                error_reason(exc),
            )

    async def _auto_backup(self) -> None:
        settings = self.settings.global_settings()
        if not settings["auto_backup_enabled"]:
            return
        now = time.time()
        last = float(await self.storage.meta("auto_backup_at") or 0)
        if now - last < settings["auto_backup_interval_hours"] * 3600:
            return
        # 失败时也推迟到下个周期，避免每次轮询都重新打包。
        await self.storage.set_meta("auto_backup_at", str(now))
        fingerprint = await self.storage.backup_fingerprint()
        if fingerprint == await self.storage.meta("auto_backup_fingerprint"):
            return
        try:
            path = await self.backups.export(settings, auto=True)
            await self.storage.set_meta("auto_backup_fingerprint", fingerprint)
            removed = await asyncio.to_thread(
                self.backups.prune_auto, settings["auto_backup_keep"]
            )
        except Exception as exc:  # noqa: BLE001 - 备份失败只记录，不影响聊天功能。
            logger.warning("群典：自动备份失败，原因=%s", error_reason(exc))
            return
        logger.info("群典：自动备份完成；文件=%s，清理旧备份=%s", path.name, removed)
