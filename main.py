"""AstrBot 群典插件入口。"""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .models import AuthorSnapshot, ForwardNode, QuoteRecord, ReplySnapshot
from .services.avatar_cache import AvatarCacheService
from .services.cooldown import GlobalCooldown
from .services.identity import AuthorIdentityService
from .services.onebot import CaptureError, OneBotQuoteExtractor
from .services.permissions import PermissionService, RoleLookupError, call_onebot_action
from .services.renderer import QuoteRenderer
from .services.settings import SettingsService
from .services.storage import DuplicateQuoteError, QuoteStorage, StorageError
from .services.web_manager import WebManager
from .utils.hashing import calculate_record_hash
from .utils.randomization import resolve_send_count
from .utils.validation import match_command_syntax

PLUGIN_NAME = "astrbot_plugin_iconic_quotes"
COMMAND_EVENT_KEY = "iconic_quotes_command_event"


@dataclass(slots=True)
class PendingDeletion:
    """锁定一次群聊删除确认所需的不可变信息。"""

    expires_at: float
    record_hashes: dict[str, str]


class IconicQuotesPlugin(Star):
    """记录、检索和管理 OneBot 11 QQ 群聊金句。"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context, config)
        self.config = config
        self.settings = SettingsService(config)
        global_settings = self.settings.global_settings()
        plugin_data_root = Path(get_astrbot_plugin_data_path())
        data_root = plugin_data_root / PLUGIN_NAME
        storage_subdir = global_settings["storage_subdir"]
        self.storage = QuoteStorage(
            data_root,
            storage_subdir,
            legacy_root=plugin_data_root / storage_subdir,
        )
        self.extractor = OneBotQuoteExtractor(self.storage)
        self.permissions = PermissionService()
        self.cooldown = GlobalCooldown()
        self.identities = AuthorIdentityService()
        self.avatars = AvatarCacheService(self.storage)
        self.renderer = QuoteRenderer(self, self.storage, self.avatars)
        self.web = WebManager(
            self.storage,
            self.settings,
            self.renderer,
            self.avatars,
            self.config,
        )
        self._pending_deletions: dict[tuple[str, str], PendingDeletion] = {}
        self._sent_in_operation = False
        self._operation_log_override: str | None = None
        self._operation_error_logged = False
        self._register_web_routes()

    def _register_web_routes(self) -> None:
        """注册只通过 Dashboard Plugin Pages 调用的管理 API。"""
        routes = (
            ("stats", self.web.stats, ["GET"], "群典统计"),
            ("records", self.web.records, ["GET"], "浏览群典"),
            ("records/delete", self.web.delete, ["POST"], "删除群典"),
            (
                "records/nodes/delete",
                self.web.delete_forward_node,
                ["POST"],
                "删除合并转发节点",
            ),
            ("audit", self.web.audit, ["GET"], "删除审计"),
            ("config", self.web.get_config, ["GET"], "读取配置"),
            ("config/save", self.web.save_config, ["POST"], "保存配置"),
            ("backup/export", self.web.export, ["GET"], "导出备份"),
            ("backup/import", self.web.import_backup, ["POST"], "预检导入备份"),
            (
                "backup/import/commit",
                self.web.commit_import,
                ["POST"],
                "确认导入备份",
            ),
            ("storage/migrate", self.web.migrate, ["POST"], "迁移存储"),
            ("preview", self.web.preview, ["POST"], "预览卡片"),
            ("media-data", self.web.media_data, ["GET"], "预览群典图片"),
            ("avatar-data", self.web.avatar_data, ["GET"], "预览本地头像"),
            ("avatars/stats", self.web.avatar_stats, ["GET"], "头像缓存统计"),
            (
                "avatars/cleanup",
                self.web.cleanup_avatars,
                ["POST"],
                "清理无引用头像",
            ),
            ("avatars/clear", self.web.clear_avatars, ["POST"], "清空头像缓存"),
        )
        for route, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{route}",
                handler,
                methods,
                description,
            )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/media/<path:relative_path>",
            self.web.media,
            ["GET"],
            "读取群典图片",
        )

    async def initialize(self) -> None:
        """初始化数据目录和可复用头像 HTTP Client。"""
        legacy_backup = await self.storage.initialize()
        await self.avatars.initialize()
        await self.renderer.initialize()
        if legacy_backup:
            logger.info("群典：旧数据迁移完成")
        elif (
            self.storage.legacy_root
            and self.storage.legacy_root.is_dir()
            and any(self.storage.legacy_root.iterdir())
            and self.storage.legacy_root != self.storage.root
        ):
            logger.warning("群典：检测到未迁移的旧数据目录")
        logger.info("群典：初始化完成")

    async def terminate(self) -> None:
        """插件停用或重载时释放网络资源和待确认状态。"""
        self._pending_deletions.clear()
        await self.web.close()
        await self.renderer.close()
        await self.avatars.close()
        logger.info("群典：已停止")

    @filter.command("添加群典")
    async def add_quote(self, event: AstrMessageEvent):
        """收录当前消息引用的一条消息或合并转发。"""
        event.set_extra(COMMAND_EVENT_KEY, True)
        await self._dispatch(event, "add", self._add_quote, trigger_source="command")

    @filter.command("群典")
    async def query_quote(self, event: AstrMessageEvent, argument: str = ""):
        """随机发送群典；参数 info 用于查看当前群统计。"""
        event.set_extra(COMMAND_EVENT_KEY, True)
        targets = self._mentioned_users(event)
        if not targets and (
            argument.strip().casefold() == "info"
            or self._command_tail(
                event.message_str,
                "群典",
            ).casefold()
            == "info"
        ):
            await self._dispatch(
                event, "info", self._show_info, trigger_source="command"
            )
            return
        _, search_keyword = self._match_query_syntax(
            self._plain_text(event),
            ["群典"],
        )
        await self._dispatch(
            event,
            "query",
            lambda current, values: self._send_random_quote(
                current,
                values,
                search_keyword,
            ),
            trigger_source="command",
        )

    @filter.command("爆典")
    async def burst_quote(self, event: AstrMessageEvent, argument: str = ""):
        """分页获取当前群中指定成员参与的全部群典记录。"""
        event.set_extra(COMMAND_EVENT_KEY, True)
        _, page_value = self._match_burst_syntax(self._plain_text(event), ["爆典"])
        if page_value is None and argument.strip().isdigit():
            page_value = argument.strip()
        await self._dispatch(
            event,
            "burst",
            lambda current, values: self._send_burst(current, values, page_value),
            trigger_source="command",
        )

    @filter.command("删除群典")
    async def delete_quote(self, event: AstrMessageEvent, keyword: str = ""):
        """预览正文包含指定字符串的记录，并创建删除确认。"""
        event.set_extra(COMMAND_EVENT_KEY, True)
        search = self._command_tail(event.message_str, "删除群典") or keyword
        await self._dispatch(
            event,
            "delete",
            lambda current, values: self._prepare_delete(current, values, search),
            trigger_source="command",
        )

    @filter.command("确认删除")
    async def confirm_delete(self, event: AstrMessageEvent):
        """在 60 秒内确认当前用户最近一次删除预览。"""
        event.set_extra(COMMAND_EVENT_KEY, True)
        await self._dispatch(
            event, "delete", self._confirm_delete, trigger_source="command"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_listener(self, event: AstrMessageEvent):
        """处理无需命令前缀的精确关键词和 @用户 群典。"""
        if self._is_command_event(event):
            return
        if self._is_bot_message(event):
            return
        text = event.message_str.strip()
        plain_text = self._plain_text(event)
        group_id = str(event.get_group_id() or "")
        values = (
            self.settings.for_group(group_id)
            if group_id
            else self.settings.global_settings()
        )
        has_source = self._has_capture_source(event)
        add_match = values["add_keyword_enabled"] and text in values["add_keywords"]
        query_syntax_match, search_keyword = self._match_query_syntax(
            plain_text,
            values["query_keywords"],
        )
        query_match = (
            values["query_keyword_enabled"]
            and query_syntax_match
            and (search_keyword is None or bool(self._mentioned_users(event)))
        )
        burst_match, burst_page = self._match_burst_syntax(
            plain_text,
            values["burst_keywords"],
        )
        if values["burst_keyword_enabled"] and burst_match:
            event.stop_event()
            await self._dispatch(
                event,
                "burst",
                lambda current, current_values: self._send_burst(
                    current,
                    current_values,
                    burst_page,
                ),
                trigger_source="keyword",
            )
        elif add_match and has_source:
            event.stop_event()
            await self._dispatch(
                event, "add", self._add_quote, trigger_source="keyword"
            )
        elif query_match:
            event.stop_event()
            await self._dispatch(
                event,
                "query",
                lambda current, current_values: self._send_random_quote(
                    current,
                    current_values,
                    search_keyword,
                ),
                trigger_source="keyword",
            )
        elif add_match:
            event.stop_event()
            await self._dispatch(
                event, "add", self._add_quote, trigger_source="keyword"
            )

    async def _dispatch(
        self,
        event: AstrMessageEvent,
        operation: str,
        action: Callable[[AstrMessageEvent, dict[str, Any]], Awaitable[None]],
        *,
        trigger_source: str,
    ) -> None:
        """统一执行平台、名单、冷却、角色权限和异常反馈。"""
        event.should_call_llm(False)
        platform = event.get_platform_name()
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")
        _ = trigger_source
        self._operation_log_override = None
        self._operation_error_logged = False
        operation_label = {
            "add": "添加",
            "query": "查询",
            "burst": "爆典发送",
            "info": "统计查询",
            "delete": "删除",
        }.get(operation, "操作")

        def log_result(result: str) -> None:
            if result == "success" and self._operation_error_logged:
                return
            if result == "success" and self._operation_log_override:
                logger.info("群典：%s", self._operation_log_override)
                self._operation_log_override = None
                return
            result_label = {
                "success": "成功",
                "cooldown": "被冷却",
                "unsupported_platform_or_session": "平台不支持",
                "list_blocked": "被名单拦截",
                "permission_lookup_failed": "权限检查失败",
                "permission_denied": "权限不足",
                "duplicate": "重复记录",
                "operation_failed": "失败",
            }.get(result, result)
            logger.info("群典：%s%s", operation_label, result_label)

        if platform != "aiocqhttp" or not group_id:
            try:
                values = self.settings.global_settings()
            except Exception:  # noqa: BLE001 - 无有效配置时只能使用无重试提示。
                logger.exception("群典：配置读取失败")
                await self._send_without_retry(
                    event, "群典插件配置无效，请联系管理员。"
                )
                return
            notice_key = group_id or f"private:{platform}:{user_id}"
            decision = await self.cooldown.try_enter(notice_key)
            if not decision.accepted:
                if decision.should_notify:
                    await self._send_without_retry(event, values["cooldown_message"])
                log_result("cooldown")
                return
            sent = await self._send_without_retry(
                event,
                "当前平台或会话不支持群典功能。",
            )
            await self.cooldown.leave(
                values["global_cooldown_ms"],
                sent_message=sent,
            )
            log_result("unsupported_platform_or_session")
            return
        try:
            values = self.settings.for_group(group_id)
        except Exception:  # noqa: BLE001 - 配置错误必须隔离在 Handler 边界。
            logger.exception("群典：配置读取失败")
            await self._send_without_retry(event, "群典插件配置无效，请联系管理员。")
            return
        if not self.permissions.list_allows(values, group_id, user_id):
            log_result("list_blocked")
            return
        decision = await self.cooldown.try_enter(group_id)
        if not decision.accepted:
            if decision.should_notify:
                await self._send_without_retry(event, values["cooldown_message"])
            log_result("cooldown")
            return
        self._sent_in_operation = False
        result = "success"
        try:
            try:
                allowed = await self.permissions.allows(event, values, operation)
            except RoleLookupError:
                result = "permission_lookup_failed"
                await self._send_text(event, "暂时无法验证群权限。", values)
                return
            if not allowed:
                result = "permission_denied"
                await self._send_text(event, "你没有执行此操作的权限。", values)
                return
            await action(event, values)
        except DuplicateQuoteError as exc:
            result = "duplicate"
            await self._send_text(
                event,
                (f"该群典已存在。\n记录 ID：{exc.record.id[:8]}"),
                values,
            )
        except (CaptureError, StorageError, ValueError) as exc:
            result = "operation_failed"
            await self._send_text(event, f"操作失败：{exc}", values)
        except Exception:  # noqa: BLE001 - Handler 边界必须隔离未知插件/适配器异常。
            result = "unexpected_error"
            logger.exception("群典：%s失败", operation_label)
            with contextlib.suppress(Exception):
                await self._send_text(event, "操作失败，请稍后重试。", values)
        finally:
            if result != "unexpected_error":
                log_result(result)
            await self.cooldown.leave(
                values["global_cooldown_ms"],
                sent_message=self._sent_in_operation,
            )

    async def _add_quote(
        self,
        event: AstrMessageEvent,
        values: dict[str, Any],
    ) -> None:
        record = await self.extractor.extract(event, values)
        # 新收录的转发/回复作者在落盘前完成别名与唯一昵称解析；旧数据仍只读纠正。
        resolution = await self.identities.resolve_records(
            event, record.group_id, [record], values
        )
        record = resolution.records[0]
        record.content_hash = calculate_record_hash(record)
        await self.storage.add(record, values["max_records_per_group"])
        if record.type == "message" and record.author:
            await self.avatars.prefetch(record.author.user_id, values)
        info = await self.storage.info(record.group_id, values["max_records_per_group"])
        author = self._record_author_name(record)
        warning = (
            f"；已忽略 {self.extractor.last_ignored_segments} 个不支持的消息段"
            if self.extractor.last_ignored_segments
            else ""
        )
        if resolution.incomplete_count:
            warning += f"\n另有 {resolution.incomplete_count} 个作者身份未能确认。"
        await self._send_text(
            event,
            f"已收录 {author} 的群典，当前共 {info['total']} 条。\n记录 ID：{record.id[:8]}{warning}",
            values,
        )

    async def _send_random_quote(
        self,
        event: AstrMessageEvent,
        values: dict[str, Any],
        search_keyword: str | None = None,
    ) -> None:
        targets = self._mentioned_users(event)
        if len(targets) > 1:
            await self._send_text(event, "一次只能指定一名用户。", values)
            return
        target_id = targets[0] if targets else None
        keyword = (search_keyword or "").strip()
        if keyword and not target_id:
            await self._send_text(event, "关键词查询必须指定一名用户。", values)
            return
        if len(keyword) > values["max_query_keyword_chars"]:
            await self._send_text(
                event,
                f"查询关键词不能超过 {values['max_query_keyword_chars']} 个字符。",
                values,
            )
            return
        requested_count = resolve_send_count(
            values["send_count"],
            values["random_send_count"],
        )
        group_id = str(event.get_group_id())
        resolution = await self.identities.resolve_records(
            event,
            group_id,
            await self.storage.records(group_id),
            values,
        )
        records, broken = await self.storage.select_random(
            group_id,
            requested_count,
            target_id,
            keyword or None,
            candidate_records=resolution.records,
        )
        if not records:
            if target_id and keyword:
                displayed = keyword if len(keyword) <= 30 else f"{keyword[:30]}…"
                message = f"未找到该用户包含“{displayed}”的可用群典。"
            elif broken:
                message = "暂无可用群典，请联系 Bot 管理员检查存储。"
            elif target_id:
                message = "该用户在当前群暂无可用群典。"
            else:
                message = "当前群还没有群典。"
            await self._send_text(event, message, values)
            return
        if target_id and resolution.member_lookup_failed:
            await self._send_text(
                event, "群成员列表读取失败，本次结果可能不完整。", values
            )
        if any(
            record.type == "forward" and record.identity_incomplete
            for record in records
        ):
            await self._send_text(event, "部分历史节点的作者身份尚未确认。", values)
        await self._send_records(event, records, values)

    async def _send_burst(
        self,
        event: AstrMessageEvent,
        values: dict[str, Any],
        page_value: str | None,
    ) -> None:
        targets = self._mentioned_users(event)
        if len(targets) != 1:
            await self._send_text(event, "用法：爆典 @某人 [页码]", values)
            return
        target_id = targets[0]
        group_id = str(event.get_group_id())
        resolution = await self.identities.resolve_records(
            event,
            group_id,
            await self.storage.records(group_id),
            values,
        )
        records, broken = await self.storage.records_for_user(
            group_id,
            target_id,
            candidate_records=resolution.records,
        )
        if not records:
            message = (
                "该用户的群典记录均存在资源缺失，请联系管理员检查插件数据。"
                if broken
                else "未找到该用户的群典记录。"
            )
            await self._send_text(event, message, values)
            return
        if resolution.member_lookup_failed:
            await self._send_text(
                event, "群成员列表读取失败，本次结果可能不完整。", values
            )
        page_size = values["burst_page_size"]
        pages = math.ceil(len(records) / page_size)
        if page_value is None and len(records) > page_size:
            await self._send_text(
                event,
                f"当前记录数量大于 {page_size} 条，将分页发送。当前共有 {pages} 页，"
                "请发送“爆典 @某人 {页码}”获取。",
                values,
            )
            return
        try:
            page = int(page_value) if page_value is not None else 1
        except (TypeError, ValueError):
            page = 0
        if page < 1 or page > pages:
            await self._send_text(event, f"页码无效，有效范围为 1 到 {pages}。", values)
            return
        start = (page - 1) * page_size
        selected = records[start : start + page_size]
        await self._refresh_record_author_names(event, selected)
        target_name = await self._burst_target_name(event, target_id, records)
        configured_time_mode = values["burst_time_mode"]
        time_modes = [configured_time_mode]
        if configured_time_mode == "native":
            time_modes.append("none")
        variants: list[tuple[bool, bool, bool, str]] = []
        for time_mode in time_modes:
            variants.append((True, True, True, time_mode))
        if any(record.has_replies() for record in selected):
            for time_mode in time_modes:
                variants.append((True, True, False, time_mode))
        if any(record.type == "forward" for record in selected):
            for time_mode in time_modes:
                variants.append((False, True, True, time_mode))
                variants.append((False, True, False, time_mode))
        if any(record.has_stickers() for record in selected):
            variants.extend(
                (nested, False, native_replies, time_mode)
                for nested, _, native_replies, time_mode in list(variants)
            )
        last_error: Exception | None = None
        for nested, native_stickers, native_replies, time_mode in dict.fromkeys(
            variants
        ):
            try:
                nodes = self.renderer.burst_nodes(
                    selected,
                    target_name=target_name,
                    total=len(records),
                    page=page,
                    pages=pages,
                    skipped=broken,
                    identity_incomplete=any(
                        record.identity_incomplete for record in selected
                    ),
                    nested=nested,
                    native_stickers=native_stickers,
                    native_replies=native_replies,
                    time_mode=time_mode,
                )
                await self._send_chain(event, [Comp.Nodes(nodes)], values)
                if (
                    not nested
                    or not native_stickers
                    or not native_replies
                    or time_mode != configured_time_mode
                ):
                    self._operation_log_override = "已降级发送"
                return
            except Exception as exc:  # noqa: BLE001 - OneBot 错误类型不统一。
                last_error = exc
                logger.debug("群典：爆典发送尝试失败，准备降级")
        if last_error is not None:
            self._operation_error_logged = True
            logger.error(
                "群典：爆典发送失败",
                exc_info=(type(last_error), last_error, last_error.__traceback__),
            )
        await self._send_text(
            event,
            "爆典合集发送失败，请稍后重试或联系管理员检查 OneBot 日志。",
            values,
        )

    async def _burst_target_name(
        self,
        event: AstrMessageEvent,
        target_id: str,
        records: list[QuoteRecord],
    ) -> str:
        try:
            payload = await call_onebot_action(
                event,
                "get_group_member_info",
                group_id=int(event.get_group_id()),
                user_id=int(target_id),
                no_cache=False,
            )
            if isinstance(payload, dict):
                name = str(payload.get("card") or payload.get("nickname") or "").strip()
                if name:
                    return name
        except Exception:  # noqa: BLE001 - 昵称查询失败应回退存档。
            logger.debug("群典：读取目标群名片失败")

        def find_node_name(node: ForwardNode) -> str | None:
            if node.author.user_id == target_id and node.author.nickname:
                return node.author.nickname
            for nested in reversed(node.nested_forwards):
                for child in reversed(nested.nodes):
                    if name := find_node_name(child):
                        return name
            return None

        for record in reversed(records):
            if (
                record.author
                and record.author.user_id == target_id
                and record.author.nickname
            ):
                return record.author.nickname
            for node in reversed(record.nodes):
                if name := find_node_name(node):
                    return name
        return target_id

    async def _send_records(
        self,
        event: AstrMessageEvent,
        records: list[QuoteRecord],
        values: dict[str, Any],
        *,
        preview: bool = False,
    ) -> bool:
        await self._refresh_record_author_names(event, records)
        if preview:
            for record in records:
                if record.type == "forward":
                    if not await self._send_forward(
                        event,
                        [record],
                        values,
                        replay=True,
                    ):
                        return False
                else:
                    await self._send_record_chain(event, record, values)
            return True
        if len(records) > 1 and values["aggregate_multiple"]:
            ordinary = [record for record in records if record.type == "message"]
            forwarded = [record for record in records if record.type == "forward"]
            if ordinary:
                await self._send_forward(event, ordinary, values, replay=False)
            for record in forwarded:
                await self._send_forward(event, [record], values, replay=True)
            return True
        for record in records:
            if record.type == "forward":
                await self._send_forward(event, [record], values, replay=True)
            elif values["send_mode"] == "card":
                if record.has_native_segments():
                    await self._send_record_chain(event, record, values)
                else:
                    await self._send_card_with_fallback(event, record, values)
            else:
                await self._send_record_chain(event, record, values)
        return True

    async def _refresh_record_author_names(
        self,
        event: AstrMessageEvent,
        records: list[QuoteRecord],
    ) -> None:
        """发送前按 QQ ID 刷新当前群名片，失败时保留存档快照。"""
        authors: list[AuthorSnapshot] = []

        def collect_reply(reply: ReplySnapshot | None) -> None:
            if reply is None:
                return
            authors.append(reply.author)
            collect_reply(reply.reply)

        def collect_node(node: ForwardNode) -> None:
            authors.append(node.author)
            collect_reply(node.reply)
            for nested in node.nested_forwards:
                for child in nested.nodes:
                    collect_node(child)

        for record in records:
            if record.author:
                authors.append(record.author)
            collect_reply(record.reply)
            for node in record.nodes:
                collect_node(node)
        by_id: dict[str, list[AuthorSnapshot]] = {}
        for author in authors:
            if author.user_id:
                by_id.setdefault(author.user_id, []).append(author)
        for user_id, snapshots in by_id.items():
            try:
                payload = await call_onebot_action(
                    event,
                    "get_group_member_info",
                    group_id=int(event.get_group_id()),
                    user_id=int(user_id),
                    no_cache=False,
                )
                if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
                    payload = payload["data"]
                if not isinstance(payload, dict):
                    continue
                name = str(payload.get("card") or payload.get("nickname") or "").strip()
                if name:
                    for snapshot in snapshots:
                        snapshot.nickname = name
            except Exception:  # noqa: BLE001 - 昵称刷新失败应保留存档快照。
                logger.debug("群典：刷新作者群名片失败")

    async def _send_card_with_fallback(
        self,
        event: AstrMessageEvent,
        record: QuoteRecord,
        values: dict[str, Any],
    ) -> None:
        paths: list[str] = []
        try:
            paths = await self.renderer.card_paths(record, values)
            await self._send_chain(
                event,
                [Comp.Image.fromFileSystem(path) for path in paths],
                values,
            )
        except Exception:  # noqa: BLE001 - 渲染端点可抛出第三方异常。
            logger.debug("群典：卡片生成失败，准备降级")
            self._operation_log_override = "已降级发送"
            await self._send_record_chain(event, record, values)
            await self._send_text(
                event, "金句图片生成失败，已使用文字方式发送。", values
            )
        finally:
            for path in paths:
                with contextlib.suppress(OSError):
                    Path(path).unlink(missing_ok=True)

    async def _send_forward(
        self,
        event: AstrMessageEvent,
        records: list[QuoteRecord],
        values: dict[str, Any],
        *,
        replay: bool,
    ) -> bool:
        variants: list[tuple[bool, bool, bool]] = [(True, True, True)]
        if any(record.has_replies() for record in records):
            variants.append((True, False, True))
        if any(record.has_nested_forwards() for record in records):
            variants.extend([(False, True, True), (False, False, True)])
        if any(record.has_stickers() for record in records):
            variants.extend(
                (native_nested, native_replies, False)
                for native_nested, native_replies, _ in list(variants)
            )
        last_error: Exception | None = None
        for native_nested, native_replies, native_stickers in dict.fromkeys(variants):
            try:
                nodes = self.renderer.forward_nodes(
                    records,
                    replay=replay,
                    native_stickers=native_stickers,
                    native_replies=native_replies,
                    native_nested=native_nested,
                )
                await self._send_chain(event, [Comp.Nodes(nodes)], values)
                if not (native_nested and native_replies and native_stickers):
                    self._operation_log_override = "已降级发送"
                return True
            except Exception as exc:  # noqa: BLE001 - OneBot 错误类型不统一。
                last_error = exc
                logger.debug("群典：合并转发尝试失败，准备降级")
        if last_error is not None:
            self._operation_error_logged = True
            logger.error(
                "群典：合并转发发送失败",
                exc_info=(type(last_error), last_error, last_error.__traceback__),
            )
        await self._send_text(event, "合并转发发送失败，请稍后重试。", values)
        return False

    async def _send_record_chain(
        self,
        event: AstrMessageEvent,
        record: QuoteRecord,
        values: dict[str, Any],
    ) -> None:
        sticker_modes = [True, False] if record.has_stickers() else [True]
        last_error: Exception | None = None
        for native_stickers in sticker_modes:
            try:
                await self._send_chain(
                    event,
                    self.renderer.text_chain(
                        record,
                        native_stickers=native_stickers,
                        native_replies=True,
                    ),
                    values,
                )
                return
            except Exception as exc:  # noqa: BLE001 - OneBot 回复与贴纸异常不统一。
                last_error = exc
                if not record.reply:
                    continue
                break
        if record.reply:
            for native_stickers in sticker_modes:
                try:
                    nodes = self.renderer.forward_nodes(
                        [record],
                        native_stickers=native_stickers,
                        native_replies=False,
                        native_nested=False,
                    )
                    await self._send_chain(event, [Comp.Nodes(nodes)], values)
                    self._operation_log_override = "已降级发送"
                    return
                except Exception as exc:  # noqa: BLE001 - OneBot 错误类型不统一。
                    last_error = exc
        if last_error:
            raise last_error

    async def _show_info(
        self,
        event: AstrMessageEvent,
        values: dict[str, Any],
    ) -> None:
        info = await self.storage.info(
            str(event.get_group_id()),
            values["max_records_per_group"],
        )
        await self._send_text(
            event,
            "\n".join(
                (
                    f"群典总数：{info['total']} / {info['limit']}",
                    f"普通金句：{info['messages']}",
                    f"合并转发：{info['forwards']}",
                    f"含图片记录：{info['with_images']}",
                    f"异常记录：{info['broken']}",
                    f"图片占用：{self._format_bytes(info['image_bytes'])}",
                    f"最早收录：{self._display_time(info['earliest'])}",
                    f"最近收录：{self._display_time(info['latest'])}",
                )
            ),
            values,
        )

    async def _prepare_delete(
        self,
        event: AstrMessageEvent,
        values: dict[str, Any],
        keyword: str,
    ) -> None:
        if not keyword.strip():
            await self._send_text(event, "请提供要匹配的正文字符串。", values)
            return
        matches = await self.storage.search(str(event.get_group_id()), keyword)
        if not matches:
            await self._send_text(event, "没有找到包含该字符串的群典。", values)
            return
        if len(matches) > values["delete_preview_limit"]:
            await self._send_text(
                event,
                f"共命中 {len(matches)} 条，超过预览上限，请缩小关键词或在后台管理。",
                values,
            )
            return
        await self._send_text(event, f"以下 {len(matches)} 条群典将被删除：", values)
        if not await self._send_records(event, matches, values, preview=True):
            await self._send_text(
                event,
                "删除预览未能完整发送，本次未创建删除确认。",
                values,
            )
            return
        await self._send_text(event, "请在 60 秒内发送 /确认删除。", values)
        key = (str(event.get_group_id()), str(event.get_sender_id()))
        self._pending_deletions[key] = PendingDeletion(
            expires_at=time.monotonic() + 60,
            record_hashes={record.id: record.content_hash for record in matches},
        )

    async def _confirm_delete(
        self,
        event: AstrMessageEvent,
        values: dict[str, Any],
    ) -> None:
        key = (str(event.get_group_id()), str(event.get_sender_id()))
        pending = self._pending_deletions.get(key)
        if not pending or pending.expires_at < time.monotonic():
            self._pending_deletions.pop(key, None)
            await self._send_text(event, "没有有效的待确认删除操作。", values)
            return
        deleted = await self.storage.delete_ids(
            key[0],
            set(pending.record_hashes),
            deleted_by=key[1],
            source="command",
            expected_hashes=pending.record_hashes,
            audit_limit=values["audit_limit"],
        )
        self._pending_deletions.pop(key, None)
        await self._send_text(event, f"已删除 {deleted} 条群典。", values)

    async def _send_text(
        self,
        event: AstrMessageEvent,
        text: str,
        values: dict[str, Any],
    ) -> None:
        await self._send_chain(event, [Comp.Plain(text)], values)

    async def _send_chain(
        self,
        event: AstrMessageEvent,
        chain: list[Any],
        values: dict[str, Any],
    ) -> None:
        attempts = values["send_retry_count"] + 1
        delay = values["send_retry_delay_ms"] / 1000
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                await event.send(MessageChain(chain))
                self._sent_in_operation = True
                return
            except (asyncio.TimeoutError, TimeoutError) as exc:
                last_error = exc
                if not values["retry_on_ambiguous_failure"]:
                    break
            except Exception as exc:  # noqa: BLE001 - OneBot 适配器异常类型不稳定。
                last_error = exc
            if attempt + 1 < attempts:
                logger.debug("群典：消息发送尝试失败，准备重试")
                await asyncio.sleep(delay)
        assert last_error is not None
        raise last_error

    @staticmethod
    async def _send_without_retry(event: AstrMessageEvent, text: str) -> bool:
        """用于冷却或平台提示，避免提示本身递归进入重试状态。"""
        try:
            await event.send(MessageChain([Comp.Plain(text)]))
            return True
        except Exception:  # noqa: BLE001 - 冷却提示失败不能递归重试。
            return False

    @staticmethod
    def _has_capture_source(event: AstrMessageEvent) -> bool:
        return any(
            isinstance(item, (Comp.Reply, Comp.Forward, Comp.Node, Comp.Nodes))
            for item in event.get_messages()
        )

    def _is_command_event(self, event: AstrMessageEvent) -> bool:
        """识别命令事件，避免去前缀后的正文再次命中关键词监听器。"""
        if getattr(event, "is_at_or_wake_command", False) or event.get_extra(
            COMMAND_EVENT_KEY,
            False,
        ):
            return True
        candidates = [str(getattr(event.message_obj, "message_str", "") or "")]
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict) and isinstance(raw.get("raw_message"), str):
            candidates.append(raw["raw_message"])
        try:
            astrbot_config = self.context.get_config(
                getattr(event, "unified_msg_origin", None)
            )
            configured = astrbot_config.get("wake_prefix", ["/"])
        except Exception:  # noqa: BLE001 - 兼容不同 AstrBot 配置代理实现。
            configured = ["/"]
        if isinstance(configured, str):
            prefixes = [configured]
        elif isinstance(configured, (list, tuple, set)):
            prefixes = configured
        else:
            prefixes = ["/"]
        return any(
            prefix and candidate.strip().startswith(str(prefix))
            for prefix in prefixes
            for candidate in candidates
        )

    @staticmethod
    def _mentioned_users(event: AstrMessageEvent) -> list[str]:
        self_id = str(event.get_self_id() or "")
        result = []
        for item in event.get_messages():
            if not isinstance(item, Comp.At):
                continue
            target = str(item.qq)
            if target not in {"", "all", self_id} and target not in result:
                result.append(target)
        return result

    @staticmethod
    def _plain_text(event: AstrMessageEvent) -> str:
        return " ".join(
            str(item.text).strip()
            for item in event.get_messages()
            if isinstance(item, Comp.Plain) and str(item.text).strip()
        ).strip()

    @staticmethod
    def _match_burst_syntax(
        plain_text: str,
        keywords: list[str],
    ) -> tuple[bool, str | None]:
        return match_command_syntax(plain_text, keywords)

    @staticmethod
    def _match_query_syntax(
        plain_text: str,
        keywords: list[str],
    ) -> tuple[bool, str | None]:
        return match_command_syntax(plain_text, keywords)

    @staticmethod
    def _is_bot_message(event: AstrMessageEvent) -> bool:
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return True
        raw = getattr(event.message_obj, "raw_message", None)
        sender = raw.get("sender", {}) if isinstance(raw, dict) else {}
        return bool(sender.get("is_bot") or sender.get("is_robot"))

    @staticmethod
    def _command_tail(text: str, command: str) -> str:
        index = text.find(command)
        if index < 0:
            return ""
        return text[index + len(command) :].strip()

    @staticmethod
    def _record_author_name(record: QuoteRecord) -> str:
        if record.type == "message" and record.author:
            return record.author.nickname or record.author.user_id or "未知用户"
        return "合并转发"

    @staticmethod
    def _display_time(value: str | None) -> str:
        if not value:
            return "无"
        try:
            return (
                datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            )
        except ValueError:
            return value

    @staticmethod
    def _format_bytes(value: int) -> str:
        number = float(value)
        for unit in ("B", "KB", "MB", "GB"):
            if number < 1024 or unit == "GB":
                return f"{number:.1f} {unit}"
            number /= 1024
        return f"{number:.1f} GB"
