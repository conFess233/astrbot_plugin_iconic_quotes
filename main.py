"""AstrBot 群典插件入口：注册 Handler、组装服务与生命周期。"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .services.avatar_cache import AvatarCacheService
from .services.backup import BackupService
from .services.capture import QuoteCapture
from .services.command_parser import (
    COMMAND_EVENT_KEY,
    call_input,
    capture_source_count,
    is_bot_message,
    is_command_event,
    route_keyword,
    route_query_command,
)
from .services.forward_builder import ForwardBuilder
from .services.legacy_import import LegacyMigrator
from .services.media import MediaStore
from .services.members import MemberService
from .services.operations import QuoteOperations
from .services.permissions import PermissionService
from .services.renderer import CardRenderer
from .services.sender import Sender
from .services.settings import GROUP_DATA_KEYS, SettingsService
from .services.storage import QuoteStorage
from .services.t2i import T2IClient
from .services.web_api import WebApi
from .utils.timefmt import error_reason
from .utils.validation import match_command_syntax

PLUGIN_NAME = "astrbot_plugin_iconic_quotes"
ORPHAN_GRACE_SECONDS = 86400


class IconicQuotesPlugin(Star):
    """记录、检索和管理 OneBot 11 QQ 群聊金句。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.settings = SettingsService(config)
        plugin_data = Path(get_astrbot_plugin_data_path())
        self.data_root = plugin_data / PLUGIN_NAME
        self.storage = QuoteStorage(self.data_root / "quotes.sqlite3")
        self.media = MediaStore(self.data_root / "media")
        self.members = MemberService()
        self.avatars = AvatarCacheService(self.storage, self.data_root / "avatars")
        self.builder = ForwardBuilder(self.media)
        # 未单独配置文转图服务时走 AstrBot 的 html_render；用 lambda 便于测试替换。
        self.t2i = T2IClient(
            lambda *args, **kwargs: self.html_render(*args, **kwargs),
            self.data_root / "tmp",
        )
        self.cards = CardRenderer(self.t2i, self.media, self.avatars)
        self.operations = QuoteOperations(
            settings=self.settings,
            storage=self.storage,
            media=self.media,
            capture=QuoteCapture(self.media, self.members, self.avatars.reserve),
            members=self.members,
            permissions=PermissionService(self.members),
            avatars=self.avatars,
            builder=self.builder,
            sender=Sender(self.builder),
            cards=self.cards,
        )
        self.web = WebApi(
            config=config,
            settings=self.settings,
            storage=self.storage,
            media=self.media,
            avatars=self.avatars,
            members=self.members,
            backups=BackupService(self.storage, self.media, self.data_root / "backups"),
            cards=self.cards,
            operations=self.operations,
            data_root=self.data_root,
        )
        legacy_subdir = str(config.get("storage_subdir") or "iconic_quotes").strip(
            "/\\"
        )
        self._legacy_roots = [
            self.data_root / legacy_subdir,
            plugin_data / legacy_subdir,
        ]
        for route, handler, methods, description in self.web.routes():
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{route}", handler, methods, description
            )

    async def initialize(self) -> None:
        """打开数据库、迁移旧数据、载入群级配置并建立头像 HTTP Client。"""
        self.media.ensure()
        await self.storage.open()
        await LegacyMigrator(self.storage, self.media, self.avatars.directory).migrate(
            self._legacy_roots
        )
        await self._load_group_settings()
        await self.avatars.initialize()
        await self._sweep_orphan_media()
        logger.info("群典：初始化完成")

    async def _load_group_settings(self) -> None:
        """群级覆盖与别名以 SQLite 为准；1.x 存在宿主配置中的旧值迁入后清空。"""
        overrides, aliases = await self.storage.group_settings()
        legacy = {key: self.config.get(key) for key in GROUP_DATA_KEYS}
        if any(legacy.values()):
            if overrides or aliases:
                logger.warning("群典：宿主配置与数据库都有群级配置，以数据库为准")
            else:
                probe = SettingsService(self.config)
                probe.attach_group_data(
                    legacy["group_overrides"] or {}, legacy["author_aliases"] or {}
                )
                values = probe.global_settings()
                overrides, aliases = values["group_overrides"], values["author_aliases"]
                await self.storage.save_group_settings(overrides, aliases)
                logger.info("群典：群级覆盖与作者别名已迁入数据库")
            for key in GROUP_DATA_KEYS:
                self.config[key] = {}
            try:
                await asyncio.to_thread(self.config.save_config)
            except Exception as exc:  # noqa: BLE001 - 数据已入库，清理宿主旧值失败只影响冗余。
                logger.warning("群典：清理宿主旧配置失败，原因=%s", error_reason(exc))
        self.settings.attach_group_data(overrides, aliases)

    async def _sweep_orphan_media(self) -> None:
        """删除磁盘上未登记且超过一天的媒体文件（收录中断留下的孤儿）。"""
        registered = await self.storage.media_rows()

        def sweep() -> int:
            removed = 0
            cutoff = time.time() - ORPHAN_GRACE_SECONDS
            for sha256, path in self.media.files():
                if sha256 not in registered and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                    removed += 1
            return removed

        if removed := await asyncio.to_thread(sweep):
            logger.info("群典：已清理 %s 个未引用的媒体文件", removed)

    async def terminate(self) -> None:
        """停用或重载时释放待确认状态、网络资源与数据库连接。"""
        self.operations.pending_deletions.clear()
        await self.web.close()
        await self.avatars.close()
        await self.t2i.close()
        await self.storage.close()
        logger.info("群典：已停止")

    # 指令 -------------------------------------------------------------

    def _wake_prefixes(self, event: AstrMessageEvent) -> Any:
        try:
            return self.context.get_config(
                getattr(event, "unified_msg_origin", None)
            ).get("wake_prefix", ["/"])
        except Exception:  # noqa: BLE001 - 兼容不同 AstrBot 配置代理实现。
            return ["/"]

    def _values(self, event: AstrMessageEvent) -> dict[str, Any]:
        group_id = str(event.get_group_id() or "")
        return (
            self.settings.for_group(group_id)
            if group_id
            else self.settings.global_settings()
        )

    @filter.command("添加群典")
    async def add_quote(self, event: AstrMessageEvent):
        """收录当前消息引用的一条消息或合并转发。"""
        if call_input(event).text not in {"添加群典", "/添加群典"}:
            return
        event.set_extra(COMMAND_EVENT_KEY, True)
        await self.operations.run(
            event, "add", self.operations.add, trigger_source="command"
        )

    @filter.command("群典")
    async def query_quote(self, event: AstrMessageEvent, argument: str = ""):
        """随机发送群典；参数 info 查看统计，help 查看帮助。"""
        # 框架可能把“@用户 + 普通文本”交给命令过滤器；只有正文完整匹配调用语法时才接管。
        route = route_query_command(call_input(event), self._values(event))
        if route is None:
            return
        event.set_extra(COMMAND_EVENT_KEY, True)
        await self._run_route(event, route.operation, route.argument, "command")

    @filter.command("爆典")
    async def burst_quote(self, event: AstrMessageEvent, argument: str = ""):
        """分页获取当前群中指定成员参与的全部群典记录。"""
        matched, page = match_command_syntax(call_input(event).text, ["爆典"])
        if not matched:
            return
        event.set_extra(COMMAND_EVENT_KEY, True)
        await self._run_route(event, "burst", page, "command")

    @filter.command("删除群典")
    async def delete_quote(self, event: AstrMessageEvent, keyword: str = ""):
        """预览正文包含指定字符串的记录，并创建删除确认。"""
        matched, search = match_command_syntax(call_input(event).text, ["删除群典"])
        if not matched:
            return
        event.set_extra(COMMAND_EVENT_KEY, True)
        await self.operations.run(
            event,
            "delete",
            lambda current, ctx: self.operations.prepare_delete(
                current, ctx, search or ""
            ),
            trigger_source="command",
        )

    @filter.command("确认删除")
    async def confirm_delete(self, event: AstrMessageEvent):
        """在 60 秒内确认当前用户最近一次删除预览。"""
        if call_input(event).text not in {"确认删除", "/确认删除"}:
            return
        event.set_extra(COMMAND_EVENT_KEY, True)
        await self.operations.run(
            event, "delete", self.operations.confirm_delete, trigger_source="command"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_listener(self, event: AstrMessageEvent):
        """处理无前缀关键词调用与机器人戳一戳。"""
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict) and raw.get("post_type") == "notice":
            await self._handle_poke(event, raw)
            return
        if is_command_event(event, self._wake_prefixes(event)) or is_bot_message(event):
            return
        call = call_input(event)
        if not call.text:
            return
        route = route_keyword(
            call, self._values(event), has_source=capture_source_count(event) > 0
        )
        if route is None:
            return
        event.stop_event()
        await self._run_route(event, route.operation, route.argument, "keyword")

    async def _run_route(
        self, event: AstrMessageEvent, operation: str, argument: str | None, source: str
    ) -> None:
        ops = self.operations
        actions = {
            "add": ops.add,
            "info": ops.info,
            "help": ops.help,
            "query": lambda current, ctx: ops.query(current, ctx, argument),
            "burst": lambda current, ctx: ops.burst(current, ctx, argument),
        }
        await ops.run(event, operation, actions[operation], trigger_source=source)

    async def _handle_poke(self, event: AstrMessageEvent, raw: dict) -> None:
        """只消费当前群成员戳机器人的通知，复用查询权限和全局冷却。"""
        if (
            event.get_platform_name() != "aiocqhttp"
            or raw.get("notice_type") != "notify"
            or raw.get("sub_type") != "poke"
            or not event.get_group_id()
            or str(raw.get("target_id", "")) != str(event.get_self_id())
            or not raw.get("user_id")
            or str(raw["user_id"]) == str(event.get_self_id())
        ):
            return
        if not self.settings.for_group(str(event.get_group_id()))["poke_enabled"]:
            return
        event.stop_event()
        await self.operations.run(
            event, "query", self.operations.poke, trigger_source="poke"
        )
