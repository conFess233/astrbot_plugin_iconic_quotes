"""AstrBot 群典插件入口。"""

from __future__ import annotations

import asyncio
import contextlib
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

from .models import QuoteRecord
from .services.cooldown import GlobalCooldown
from .services.onebot import CaptureError, OneBotQuoteExtractor
from .services.permissions import PermissionService, RoleLookupError
from .services.renderer import QuoteRenderer
from .services.settings import SettingsService
from .services.storage import DuplicateQuoteError, QuoteStorage, StorageError
from .services.web_manager import WebManager

PLUGIN_NAME = "astrbot_plugin_iconic_quotes"


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
        self.storage = QuoteStorage(
            plugin_data_root,
            global_settings["storage_subdir"],
        )
        self.extractor = OneBotQuoteExtractor(self.storage)
        self.permissions = PermissionService()
        self.cooldown = GlobalCooldown()
        self.renderer = QuoteRenderer(self, self.storage)
        self.web = WebManager(
            self.storage,
            self.settings,
            self.renderer,
            self.config,
        )
        self._pending_deletions: dict[tuple[str, str], PendingDeletion] = {}
        self._sent_in_operation = False
        self._register_web_routes()

    def _register_web_routes(self) -> None:
        """注册只通过 Dashboard Plugin Pages 调用的管理 API。"""
        routes = (
            ("stats", self.web.stats, ["GET"], "群典统计"),
            ("records", self.web.records, ["GET"], "浏览群典"),
            ("records/delete", self.web.delete, ["POST"], "删除群典"),
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
        await self.storage.initialize()
        await self.renderer.initialize()
        logger.info("群典插件初始化完成，存储目录: %s", self.storage.root)

    async def terminate(self) -> None:
        """插件停用或重载时释放网络资源和待确认状态。"""
        self._pending_deletions.clear()
        await self.web.close()
        await self.renderer.close()
        logger.info("群典插件已停止")

    @filter.command("添加群典")
    async def add_quote(self, event: AstrMessageEvent):
        """收录当前消息引用的一条消息或合并转发。"""
        await self._dispatch(event, "add", self._add_quote)

    @filter.command("群典")
    async def query_quote(self, event: AstrMessageEvent, argument: str = ""):
        """随机发送群典；参数 info 用于查看当前群统计。"""
        if (
            argument.strip().casefold() == "info"
            or self._command_tail(
                event.message_str,
                "群典",
            ).casefold()
            == "info"
        ):
            await self._dispatch(event, "info", self._show_info)
            return
        await self._dispatch(event, "query", self._send_random_quote)

    @filter.command("删除群典")
    async def delete_quote(self, event: AstrMessageEvent, keyword: str = ""):
        """预览正文包含指定字符串的记录，并创建删除确认。"""
        search = self._command_tail(event.message_str, "删除群典") or keyword
        await self._dispatch(
            event,
            "delete",
            lambda current, values: self._prepare_delete(current, values, search),
        )

    @filter.command("确认删除")
    async def confirm_delete(self, event: AstrMessageEvent):
        """在 60 秒内确认当前用户最近一次删除预览。"""
        await self._dispatch(event, "delete", self._confirm_delete)

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def keyword_listener(self, event: AstrMessageEvent):
        """处理无需命令前缀的精确关键词和 @用户 群典。"""
        if getattr(event, "is_at_or_wake_command", False):
            return
        if self._is_bot_message(event):
            return
        text = event.message_str.strip()
        plain_text = "".join(
            str(item.text)
            for item in event.get_messages()
            if isinstance(item, Comp.Plain)
        ).strip()
        group_id = str(event.get_group_id() or "")
        values = (
            self.settings.for_group(group_id)
            if group_id
            else self.settings.global_settings()
        )
        has_source = self._has_capture_source(event)
        add_match = values["add_keyword_enabled"] and text in values["add_keywords"]
        query_match = (
            values["query_keyword_enabled"]
            and (
                text in values["query_keywords"]
                or (
                    bool(self._mentioned_users(event))
                    and plain_text in values["query_keywords"]
                )
            )
        )
        if add_match and has_source:
            await self._dispatch(event, "add", self._add_quote)
        elif query_match:
            await self._dispatch(event, "query", self._send_random_quote)
        elif add_match:
            await self._dispatch(event, "add", self._add_quote)

    async def _dispatch(
        self,
        event: AstrMessageEvent,
        operation: str,
        action: Callable[[AstrMessageEvent, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """统一执行平台、名单、冷却、角色权限和异常反馈。"""
        event.should_call_llm(False)
        platform = event.get_platform_name()
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")
        if platform != "aiocqhttp" or not group_id:
            values = self.settings.global_settings()
            notice_key = group_id or f"private:{platform}:{user_id}"
            decision = await self.cooldown.try_enter(notice_key)
            if not decision.accepted:
                if decision.should_notify:
                    await self._send_without_retry(event, values["cooldown_message"])
                return
            sent = await self._send_without_retry(
                event,
                "当前平台或会话不支持群典功能。",
            )
            await self.cooldown.leave(
                values["global_cooldown_ms"],
                sent_message=sent,
            )
            return
        values = self.settings.for_group(group_id)
        if not self.permissions.list_allows(values, group_id, user_id):
            return
        decision = await self.cooldown.try_enter(group_id)
        if not decision.accepted:
            if decision.should_notify:
                await self._send_without_retry(event, values["cooldown_message"])
            return
        self._sent_in_operation = False
        try:
            try:
                allowed = await self.permissions.allows(event, values, operation)
            except RoleLookupError:
                await self._send_text(event, "暂时无法验证群权限。", values)
                return
            if not allowed:
                await self._send_text(event, "你没有执行此操作的权限。", values)
                return
            await action(event, values)
        except DuplicateQuoteError as exc:
            await self._send_text(
                event,
                (
                    "该群典已存在。\n"
                    f"记录 ID：{exc.record.id[:8]}"
                ),
                values,
            )
        except (CaptureError, StorageError, ValueError) as exc:
            await self._send_text(event, f"操作失败：{exc}", values)
        except Exception:  # noqa: BLE001 - Handler 边界必须隔离未知插件/适配器异常。
            logger.exception("群典操作失败: operation=%s group=%s", operation, group_id)
            with contextlib.suppress(Exception):
                await self._send_text(event, "操作失败，请稍后重试。", values)
        finally:
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
        await self.storage.add(record, values["max_records_per_group"])
        info = await self.storage.info(record.group_id, values["max_records_per_group"])
        author = self._record_author_name(record)
        warning = (
            f"；已忽略 {self.extractor.last_ignored_segments} 个不支持的消息段"
            if self.extractor.last_ignored_segments
            else ""
        )
        await self._send_text(
            event,
            f"已收录 {author} 的群典，当前共 {info['total']} 条。\n记录 ID：{record.id[:8]}{warning}",
            values,
        )

    async def _send_random_quote(
        self,
        event: AstrMessageEvent,
        values: dict[str, Any],
    ) -> None:
        targets = self._mentioned_users(event)
        if len(targets) > 1:
            await self._send_text(event, "一次只能指定一名用户。", values)
            return
        target_id = targets[0] if targets else None
        records, broken = await self.storage.select_random(
            str(event.get_group_id()),
            values["send_count"],
            target_id,
        )
        if not records:
            if broken:
                message = "暂无可用群典，请联系 Bot 管理员检查存储。"
            elif target_id:
                message = "该用户在当前群暂无可用群典。"
            else:
                message = "当前群还没有群典。"
            await self._send_text(event, message, values)
            return
        await self._send_records(event, records, values)

    async def _send_records(
        self,
        event: AstrMessageEvent,
        records: list[QuoteRecord],
        values: dict[str, Any],
        *,
        preview: bool = False,
    ) -> bool:
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
                    await self._send_chain(
                        event, self.renderer.text_chain(record), values
                    )
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
                await self._send_card_with_fallback(event, record, values)
            else:
                await self._send_chain(event, self.renderer.text_chain(record), values)
        return True

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
            logger.exception("金句卡片生成失败: record_id=%s", record.id)
            await self._send_chain(event, self.renderer.text_chain(record), values)
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
        nodes = self.renderer.forward_nodes(records, replay=replay)
        try:
            await self._send_chain(event, [Comp.Nodes(nodes)], values)
            return True
        except Exception:  # noqa: BLE001 - OneBot 适配器异常类型不稳定。
            logger.exception(
                "合并转发发送失败: group=%s record_ids=%s",
                event.get_group_id(),
                [record.id for record in records],
            )
            await self._send_text(event, "合并转发发送失败，请稍后重试。", values)
            return False

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
