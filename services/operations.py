"""聊天侧业务流程：统一的准入检查，以及添加、查询、爆典、统计、帮助与删除。"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger

from ..models import QuoteRecord
from ..utils.hashing import calculate_record_hash, normalize_search
from ..utils.help import build_help_text, operation_usage
from ..utils.randomization import resolve_send_count
from ..utils.timefmt import error_reason, local_time
from ..utils.validation import parse_record_id
from .avatar_cache import AvatarCacheService
from .capture import CaptureError, CaptureSession, QuoteCapture
from .command_parser import call_input, capture_source_count
from .cooldown import GroupCooldown
from .forward_builder import BurstHeader, ForwardBuilder, missing_reply_warning
from .insights import compute, ranking_text, user_text
from .media import MediaStore
from .members import MemberService, RoleLookupError
from .permissions import PermissionService
from .reactions import ReactionTracker
from .renderer import CardRenderer
from .sender import AmbiguousSendError, OperationContext, Sender
from .settings import SettingsService
from .storage import DuplicateQuoteError, QuoteStorage, StorageError

Action = Callable[[Any, OperationContext], Awaitable[None]]

OPERATION_LABELS = {
    "add": "添加",
    "query": "查询",
    "burst": "爆典发送",
    "info": "统计查询",
    "delete": "删除",
    "help": "帮助",
}
RESULT_LABELS = {
    "success": "成功",
    "cooldown": "被冷却",
    "unsupported_platform_or_session": "平台不支持",
    "list_blocked": "被名单拦截",
    "permission_lookup_failed": "权限检查失败",
    "permission_denied": "权限不足",
    "duplicate": "重复记录",
    "operation_failed": "失败",
    "usage_error": "参数错误",
}
DELETE_CONFIRM_SECONDS = 60
RANKING_LIMIT = 10
RANDOM_BATCH = 20
# 表情投票遇到冷却时在后台等待，不能因此丢掉已经达到阈值的那一票。
REACTION_RETRY_SECONDS = 1.0
REACTION_RETRIES = 30


class UsageError(ValueError):
    """已识别插件操作，但调用参数或来源格式不完整。"""

    def __init__(self, message: str, operation: str):
        super().__init__(message)
        self.operation = operation


@dataclass(slots=True)
class PendingDeletion:
    """锁定一次群聊删除确认所需的不可变信息。"""

    expires_at: float
    record_hashes: dict[str, str]


def format_bytes(value: int) -> str:
    number = float(value)
    for unit in ("B", "KB", "MB"):
        if number < 1024:
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} GB"


class QuoteOperations:
    """聊天指令、关键词与戳一戳共用的业务入口。"""

    def __init__(
        self,
        *,
        settings: SettingsService,
        storage: QuoteStorage,
        media: MediaStore,
        capture: QuoteCapture,
        members: MemberService,
        permissions: PermissionService,
        avatars: AvatarCacheService,
        builder: ForwardBuilder,
        sender: Sender,
        cards: CardRenderer,
    ):
        self.settings = settings
        self.storage = storage
        self.media = media
        self.capture = capture
        self.members = members
        self.permissions = permissions
        self.avatars = avatars
        self.builder = builder
        self.sender = sender
        self.cards = cards
        self.cooldown = GroupCooldown()
        # 媒体容量预留、文件写入与孤儿文件删除要和数据库登记保持一致，跨群串行。
        self.media_lock = asyncio.Lock()
        self.pending_deletions: dict[tuple[str, str], PendingDeletion] = {}
        self.reactions = ReactionTracker()

    # 准入与异常边界 ---------------------------------------------------

    async def run(
        self, event: Any, operation: str, action: Action, *, trigger_source: str
    ) -> None:
        """统一执行平台、名单、冷却、角色权限和异常反馈。"""
        event.should_call_llm(False)
        platform = event.get_platform_name()
        group_id = str(event.get_group_id() or "")
        user_id = str(event.get_sender_id() or "")
        label = (
            "戳一戳发送"
            if trigger_source == "poke"
            else OPERATION_LABELS.get(operation, "操作")
        )
        ctx: OperationContext | None = None
        failure: str | None = None

        def log(result: str) -> None:
            if result == "success" and ctx is not None:
                if ctx.error_logged:
                    return
                if ctx.log_override:
                    logger.info("群典：%s", ctx.log_override)
                    return
            text = RESULT_LABELS.get(result, result)
            if failure:
                logger.warning("群典：%s%s，原因=%s", label, text, failure)
            else:
                logger.info("群典：%s%s", label, text)

        try:
            values = (
                self.settings.for_group(group_id)
                if platform == "aiocqhttp" and group_id
                else self.settings.global_settings()
            )
        except Exception:  # noqa: BLE001 - 配置错误必须隔离在 Handler 边界。
            logger.exception("群典：配置读取失败")
            await self.sender.notice(event, "群典插件配置无效，请联系管理员。")
            return
        if platform != "aiocqhttp" or not group_id:
            key = group_id or f"private:{platform}:{user_id}"
            decision = await self.cooldown.try_enter(key)
            if not decision.accepted:
                if decision.should_notify:
                    await self.sender.notice(event, values["cooldown_message"])
                log("cooldown")
                return
            sent = await self.sender.notice(event, "当前平台或会话不支持群典功能。")
            await self.cooldown.leave(
                key, values["global_cooldown_ms"], sent_message=sent
            )
            log("unsupported_platform_or_session")
            return
        if not self.permissions.list_allows(values, group_id, user_id):
            log("list_blocked")
            return
        # 先查权限：没有权限的人不占用群冷却，不影响其他成员。
        try:
            allowed = await self.permissions.allows(event, values, operation)
        except RoleLookupError as exc:
            failure = error_reason(exc)
            if self.cooldown.allow_notice(
                group_id, user_id, values["global_cooldown_ms"]
            ):
                await self.sender.notice(event, "暂时无法验证群权限。")
            log("permission_lookup_failed")
            return
        if not allowed:
            if self.cooldown.allow_notice(
                group_id, user_id, values["global_cooldown_ms"]
            ):
                await self.sender.notice(event, "你没有执行此操作的权限。")
            log("permission_denied")
            return
        # 戳一戳很容易连续触发，冷却中不回复。
        decision = await self.cooldown.try_enter(
            group_id, quiet=trigger_source == "poke"
        )
        if not decision.accepted:
            if decision.should_notify:
                await self.sender.notice(event, values["cooldown_message"])
            log("cooldown")
            return
        ctx = OperationContext(values)
        result = "success"
        try:
            await action(event, ctx)
        except DuplicateQuoteError as exc:
            result = "duplicate"
            await self.sender.text(
                event, ctx, f"该群典已存在。\n记录 ID：{exc.record.id[:8]}"
            )
        except AmbiguousSendError as exc:
            result, failure = "operation_failed", error_reason(exc)
            with contextlib.suppress(Exception):
                await self.sender.text(
                    event, ctx, "消息发送结果未知，已停止重试和降级，请先检查群聊。"
                )
        except UsageError as exc:
            result, failure = "usage_error", error_reason(exc)
            usage = operation_usage(values, exc.operation)
            message = str(exc)
            if values["help_enabled"] and usage:
                message += f"\n\n用法：{usage}"
            await self.sender.text(event, ctx, message)
        except (CaptureError, StorageError, ValueError) as exc:
            result, failure = "operation_failed", error_reason(exc)
            await self.sender.text(event, ctx, f"操作失败：{exc}")
        except Exception as exc:  # noqa: BLE001 - Handler 边界必须隔离未知插件/适配器异常。
            result = "unexpected_error"
            logger.exception("群典：%s失败，原因=%s", label, error_reason(exc))
            with contextlib.suppress(Exception):
                await self.sender.text(event, ctx, "操作失败，请稍后重试。")
        finally:
            if result != "unexpected_error":
                log(result)
            await self.cooldown.leave(
                group_id, values["global_cooldown_ms"], sent_message=ctx.sent
            )

    # 添加 -------------------------------------------------------------

    async def add(self, event: Any, ctx: OperationContext) -> None:
        values = ctx.values
        sources = capture_source_count(event)
        if sources != 1:
            raise UsageError(
                "请引用一条消息或携带一个合并转发。"
                if sources == 0
                else "一次只能添加一个引用或合并转发来源。",
                "add",
            )
        session = CaptureSession(values)
        await self.sender.text(
            event,
            ctx,
            await self._capture_and_store(event, values, session, self.capture.extract),
        )

    async def _capture_and_store(
        self,
        event: Any,
        values: dict[str, Any],
        session: CaptureSession,
        extract: Callable[[Any, CaptureSession], Awaitable[QuoteRecord]],
        *,
        note: str = "",
    ) -> str:
        """收录并落盘，返回给群里的结果文本；失败时清理本次新写入的媒体。"""
        async with self.media_lock:
            return await self._capture_locked(event, values, session, extract, note)

    async def _capture_locked(
        self,
        event: Any,
        values: dict[str, Any],
        session: CaptureSession,
        extract: Callable[[Any, CaptureSession], Awaitable[QuoteRecord]],
        note: str,
    ) -> str:
        try:
            record = await extract(event, session)
            # 新收录的转发/回复作者在落盘前完成别名与唯一昵称纠正；旧数据只在读取时纠正。
            resolution = await self.members.resolve_records(
                event, record.group_id, [record], values
            )
            record = resolution.records[0]
            record.content_hash = calculate_record_hash(record)
            await self.storage.add(
                record, values["max_records_per_group"], session.media.values()
            )
        except BaseException:
            await self._discard_new_media(session)
            raise
        if record.type == "message" and record.author:
            await self.avatars.prefetch(record.author.user_id, values)
        total = await self.storage.count(record.group_id)
        author = (
            record.author.nickname or record.author.user_id or "未知用户"
            if record.type == "message" and record.author
            else "合并转发"
        )
        warning = (
            f"；已忽略 {session.ignored_segments} 个不支持的消息段"
            if session.ignored_segments
            else ""
        )
        if resolution.incomplete_count:
            warning += f"\n另有 {resolution.incomplete_count} 个作者身份未能确认。"
        if record.has_missing_replies():
            warning += "\n该记录存在无法追溯的引用内容。"
        return (
            f"已收录 {author} 的群典{note}，当前共 {total} 条。"
            f"\n记录 ID：{record.id[:8]}{warning}"
        )

    async def reaction(self, event: Any, raw: dict[str, Any]) -> None:
        """成员的表情回应达到阈值时收录该消息；失败只写日志，不打扰群聊。"""
        group_id = str(event.get_group_id())
        user_id = str(event.get_sender_id())
        message_id = str(raw.get("message_id") or "")
        values = self.settings.for_group(group_id)
        if (
            not values["reaction_capture_enabled"]
            or not message_id
            or not self.permissions.list_allows(values, group_id, user_id)
        ):
            return
        allowed = set(values["reaction_capture_emoji_ids"])
        emojis = {
            str(item.get("emoji_id"))
            for item in raw.get("likes") or []
            if isinstance(item, dict) and item.get("emoji_id") not in (None, "")
        }
        if allowed:
            emojis &= allowed
        if not emojis:
            return
        added = raw.get("is_add", True) is not False
        if added:
            try:
                if not await self.permissions.allows(event, values, "add"):
                    return
            except RoleLookupError:
                return
        votes = self.reactions.apply(
            group_id,
            message_id,
            user_id,
            emojis,
            added=added,
            threshold=values["reaction_capture_threshold"],
        )
        if votes is None:
            return
        for attempt in range(REACTION_RETRIES):
            if (await self.cooldown.try_enter(group_id, quiet=True)).accepted:
                break
            if attempt + 1 < REACTION_RETRIES:
                await asyncio.sleep(REACTION_RETRY_SECONDS)
        else:
            logger.warning("群典：表情投票收录放弃，原因=冷却等待超时")
            return
        ctx = OperationContext(values)
        try:
            await self.sender.text(
                event,
                ctx,
                await self._capture_and_store(
                    event,
                    values,
                    CaptureSession(values),
                    lambda current, session: self.capture.extract_message(
                        current, message_id, session
                    ),
                    note=f"（{votes} 人表情回应）",
                ),
            )
            logger.info("群典：表情投票收录成功")
        except DuplicateQuoteError:
            logger.debug("群典：表情投票命中已收录的消息")
        except Exception as exc:  # noqa: BLE001 - 投票不是用户直接发起的操作，失败不回群。
            logger.warning("群典：表情投票收录失败，原因=%s", error_reason(exc))
        finally:
            await self.cooldown.leave(
                group_id, values["global_cooldown_ms"], sent_message=ctx.sent
            )

    async def _discard_new_media(self, session: CaptureSession) -> None:
        """收录失败时删除本次新写入、且未被任何记录登记的媒体文件。"""
        if not session.created:
            return
        with contextlib.suppress(Exception):
            registered = await self.storage.media_rows(
                info.sha256 for info in session.created
            )
            for info in session.created:
                if info.sha256 not in registered:
                    await asyncio.to_thread(self.media.delete, info.sha256, info.mime)

    # 媒体健康 ---------------------------------------------------------

    async def media_missing(self, records: list[QuoteRecord]) -> frozenset[str]:
        """发送前对本次记录做廉价检查（存在且大小一致），并同步数据库缺失标记。"""
        segments = {
            s.sha256: s for r in records for s in r.image_segments() if s.sha256
        }
        if not segments:
            return frozenset()
        rows = await self.storage.media_rows(segments)

        def check() -> dict[str, bool]:
            return {
                sha: not self.media.quick_ok(sha, row["mime"], row["size"])
                for sha, row in rows.items()
            }

        states = await asyncio.to_thread(check)
        changed = {
            sha: missing
            for sha, missing in states.items()
            if bool(rows[sha]["missing"]) != missing
        }
        await self.storage.set_media_state(changed)
        missing = {sha for sha, value in states.items() if value}
        missing.update(sha for sha in segments if sha not in rows)
        return frozenset(missing)

    # 查询 -------------------------------------------------------------

    async def query(
        self, event: Any, ctx: OperationContext, keyword: str | None = None
    ) -> None:
        values = ctx.values
        targets = call_input(event).targets
        if len(targets) > 1:
            raise UsageError("一次只能指定一名用户。", "query")
        target = targets[0] if targets else None
        keyword = (keyword or "").strip()
        if len(keyword) > values["max_query_keyword_chars"]:
            raise UsageError(
                f"查询关键词不能超过 {values['max_query_keyword_chars']} 个字符。",
                "query",
            )
        group_id = str(event.get_group_id())
        if not target and (prefix := parse_record_id(keyword)):
            matches = await self.storage.find_by_prefix(group_id, prefix)
            if len(matches) > 1:
                await self.sender.text(
                    event, ctx, "匹配到多条记录，请提供更长的记录 ID。"
                )
                return
            if matches:
                await self._send_by_id(event, ctx, matches[0])
                return
            # 没有对应记录时，按普通文字继续搜索。
        count = resolve_send_count(values["send_count"], values["random_send_count"])
        lookup_failed = False
        if target:
            names = await self.members.candidate_names(event, group_id, target, values)
            ids, broken = await self.storage.candidates(
                group_id, user_id=target, names=names
            )
            resolution = await self.members.resolve_records(
                event, group_id, await self.storage.load(ids), values
            )
            lookup_failed = resolution.member_lookup_failed
            needle = normalize_search(keyword)
            pool = {
                record.id: record
                for record in resolution.records
                if not self._is_hidden(record, values)
                and (
                    needle in normalize_search(record.authored_text(target))
                    if needle
                    else record.involves_user(target)
                )
            }
            order = await self._fresh_order(group_id, list(pool), values)
            records, newly_broken = await self._sample_healthy(
                [pool[item] for item in order], count
            )
            broken += newly_broken
        else:
            records, broken = await self._pick_random(
                event, group_id, values, count, normalize_search(keyword)
            )
        if not records:
            shown = keyword if len(keyword) <= 30 else f"{keyword[:30]}…"
            if target and keyword:
                message = f"未找到该用户包含“{shown}”的可用群典。"
            elif keyword:
                message = f"未找到包含“{shown}”的可用群典。"
            elif broken:
                message = "暂无可用群典，请联系 Bot 管理员检查存储。"
            elif target:
                message = "该用户在当前群暂无可用群典。"
            else:
                message = "当前群还没有群典。"
            await self.sender.text(event, ctx, message)
            return
        if target and lookup_failed:
            await self.sender.text(
                event, ctx, "群成员列表读取失败，本次结果可能不完整。"
            )
        if any(
            record.type == "forward" and record.identity_incomplete
            for record in records
        ):
            await self.sender.text(event, ctx, "部分历史节点的作者身份尚未确认。")
        await self._send_and_mark(event, ctx, records)

    async def _send_by_id(
        self, event: Any, ctx: OperationContext, record: QuoteRecord
    ) -> None:
        """发送用户指定的记录；媒体缺失时由发送链路用占位文字代替。"""
        resolution = await self.members.resolve_records(
            event, record.group_id, [record], ctx.values
        )
        if self._is_hidden(resolution.records[0], ctx.values):
            await self.sender.text(event, ctx, "这条群典涉及禁止收录的用户，无法发送。")
            return
        await self._send_and_mark(event, ctx, resolution.records)

    async def _send_and_mark(
        self, event: Any, ctx: OperationContext, records: list[QuoteRecord]
    ) -> None:
        """发送并登记发送历史，供下一次随机抽取避开。"""
        if not await self.send_records(event, ctx, records):
            return
        try:
            await self.storage.mark_sent(
                str(event.get_group_id()), [record.id for record in records]
            )
        except StorageError as exc:
            logger.warning("群典：发送历史写入失败，原因=%s", error_reason(exc))

    @staticmethod
    def _is_hidden(record: QuoteRecord, values: dict[str, Any]) -> bool:
        """禁止收录的用户参与过的记录不再发送；存档保留，移出名单后恢复。"""
        excluded = set(values["excluded_author_ids"])
        return bool(excluded) and any(
            author.user_id in excluded for author in record.iter_authors()
        )

    async def _fresh_order(
        self, group_id: str, ids: list[str], values: dict[str, Any]
    ) -> list[str]:
        """随机排序，最近发送过的排到最后。

        避开的数量不超过候选的一半，所以小群也能轮换，而不会变成固定顺序。
        """
        order = random.sample(ids, len(ids))
        limit = min(values["anti_repeat_window"], len(ids) // 2)
        if limit <= 0:
            return order
        candidates = set(ids)
        recent = await self.storage.recent_sent(group_id, values["anti_repeat_window"])
        avoid = [item for item in recent if item in candidates][:limit]
        blocked = set(avoid)
        return [item for item in order if item not in blocked] + avoid[::-1]

    async def _pick_random(
        self,
        event: Any,
        group_id: str,
        values: dict[str, Any],
        count: int,
        needle: str = "",
    ) -> tuple[list[QuoteRecord], int]:
        """全群随机抽取（可限定正文子串）；只加载需要的批次，不读整群。"""
        ids, broken = await self.storage.candidates(
            group_id, needle=needle, excluded=values["excluded_author_ids"]
        )
        order = await self._fresh_order(group_id, ids, values)
        step = max(count, RANDOM_BATCH)
        records: list[QuoteRecord] = []
        for start in range(0, len(order), step):
            resolution = await self.members.resolve_records(
                event,
                group_id,
                await self.storage.load(order[start : start + step]),
                values,
            )
            visible = [
                record
                for record in resolution.records
                if not self._is_hidden(record, values)
            ]
            chosen, newly_broken = await self._sample_healthy(
                visible, count - len(records)
            )
            records += chosen
            broken += newly_broken
            if len(records) >= count:
                break
        return records, broken

    async def _sample_healthy(
        self, pool: list[QuoteRecord], count: int
    ) -> tuple[list[QuoteRecord], int]:
        """按给定顺序抽取；记录的媒体已缺失则跳过并标记，再继续抽取。

        返回选中记录与本次新发现的缺失记录数。
        """
        chosen: list[QuoteRecord] = []
        broken = 0
        for record in pool:
            if len(chosen) >= count:
                break
            if await self.media_missing([record]):
                broken += 1
            else:
                chosen.append(record)
        return chosen, broken

    async def poke(self, event: Any, ctx: OperationContext) -> None:
        """戳一戳使用独立的发送配置快照，不影响普通查询。"""
        values = dict(ctx.values)
        values.update(
            send_count=values["poke_send_count"],
            random_send_count=False,
            send_mode=values["poke_send_mode"],
        )
        if values["send_mode"] == "card":
            values["aggregate_multiple"] = False
        ctx.values = values
        await self.query(event, ctx)

    # 发送 -------------------------------------------------------------

    async def send_records(
        self,
        event: Any,
        ctx: OperationContext,
        records: list[QuoteRecord],
        *,
        preview: bool = False,
    ) -> bool:
        values = ctx.values
        await self.members.refresh_names(event, str(event.get_group_id()), records)
        ctx.missing = await self.media_missing(records)
        if not preview and len(records) > 1 and values["aggregate_multiple"]:
            ordinary = [record for record in records if record.type == "message"]
            if ordinary and not await self._send_forward(
                event, ctx, ordinary, replay=False
            ):
                return False
            for record in records:
                if record.type == "forward" and not await self._send_forward(
                    event, ctx, [record], replay=True
                ):
                    return False
            return True
        for record in records:
            if record.type == "forward":
                if not await self._send_forward(event, ctx, [record], replay=True):
                    return False
                continue
            if warning := missing_reply_warning([record]):
                await self.sender.text(event, ctx, warning)
            if (
                not preview
                and values["send_mode"] == "card"
                and not record.has_native_segments()
            ):
                await self._send_card(event, ctx, record)
            else:
                await self.sender.record_chain(event, ctx, record)
        return True

    async def _send_forward(
        self,
        event: Any,
        ctx: OperationContext,
        records: list[QuoteRecord],
        *,
        replay: bool,
    ) -> bool:
        return await self.sender.forward(
            event,
            ctx,
            lambda options: self.builder.forward(
                records, options, replay=replay, hints=ctx.values["forward_hint_nodes"]
            ),
            depth=max((record.forward_depth() for record in records), default=1),
            has_replies=any(record.has_replies() for record in records),
            has_stickers=any(record.has_stickers() for record in records),
            label="记录=" + ",".join(record.id[:8] for record in records) + "，",
        )

    async def _send_card(
        self, event: Any, ctx: OperationContext, record: QuoteRecord
    ) -> None:
        paths: list[str] = []
        try:
            paths = await self.cards.card_paths(record, ctx.values)
            await self.sender.chain(
                event, ctx, [Comp.Image.fromFileSystem(path) for path in paths]
            )
        except AmbiguousSendError:
            raise
        except Exception as exc:  # noqa: BLE001 - 渲染服务异常类型不稳定，降级为文字。
            logger.warning(
                "群典：卡片生成失败，准备降级；记录=%s，原因=%s",
                record.id,
                error_reason(exc),
            )
            ctx.log_override = "已降级发送"
            await self.sender.record_chain(event, ctx, record)
            await self.sender.text(event, ctx, "金句图片生成失败，已使用文字方式发送。")
        finally:
            for path in paths:
                with contextlib.suppress(OSError):
                    Path(path).unlink(missing_ok=True)

    # 爆典 -------------------------------------------------------------

    async def burst(
        self, event: Any, ctx: OperationContext, page_value: str | None
    ) -> None:
        values = ctx.values
        targets = call_input(event).targets
        if len(targets) != 1:
            raise UsageError(
                "爆典需要指定一名用户。"
                if not targets
                else "爆典一次只能指定一名用户。",
                "burst",
            )
        target = targets[0]
        group_id = str(event.get_group_id())
        names = await self.members.candidate_names(event, group_id, target, values)
        ids, _ = await self.storage.candidates(
            group_id, user_id=target, names=names, include_broken=True
        )
        resolution = await self.members.resolve_records(
            event, group_id, await self.storage.load(ids), values
        )
        involved = [
            record
            for record in resolution.records
            if record.involves_user(target) and not self._is_hidden(record, values)
        ]
        records = [record for record in involved if record.has_content()]
        skipped = len(involved) - len(records)
        records.sort(key=lambda record: record.recorded_at)
        if not records:
            await self.sender.text(
                event,
                ctx,
                "该用户的群典记录均存在资源缺失，请联系管理员检查插件数据。"
                if skipped
                else "未找到该用户的群典记录。",
            )
            return
        if resolution.member_lookup_failed:
            await self.sender.text(
                event, ctx, "群成员列表读取失败，本次结果可能不完整。"
            )
        page_size = values["burst_page_size"]
        pages = math.ceil(len(records) / page_size)
        if page_value is None and len(records) > page_size:
            raise UsageError(
                f"当前记录数量大于 {page_size} 条，将分页发送。当前共有 {pages} 页，"
                "请指定要获取的页码。",
                "burst",
            )
        try:
            page = int(page_value) if page_value is not None else 1
        except (TypeError, ValueError):
            page = 0
        if not 1 <= page <= pages:
            raise UsageError(f"页码无效，有效范围为 1 到 {pages}。", "burst")
        selected = records[(page - 1) * page_size : page * page_size]
        await self.members.refresh_names(event, group_id, selected)
        ctx.missing = await self.media_missing(selected)
        header = BurstHeader(
            target_name=await self._target_name(event, group_id, target, records),
            total=len(records),
            page=page,
            pages=pages,
            skipped=skipped,
            identity_incomplete=any(record.identity_incomplete for record in selected),
        )
        # 爆典自身会再包一层合集，因此按最终节点树深度预检。
        await self.sender.forward(
            event,
            ctx,
            lambda options: self.builder.burst(
                selected, header, options, hints=values["forward_hint_nodes"]
            ),
            depth=max(
                record.forward_depth() + 1 if record.type == "forward" else 1
                for record in selected
            ),
            has_replies=any(record.has_replies() for record in selected),
            has_stickers=any(record.has_stickers() for record in selected),
            configured_time_mode=values["burst_time_mode"],
            failure_text="爆典合集发送失败，请稍后重试或联系管理员检查 OneBot 日志。",
            label=f"爆典页码={page}/{pages}，",
        )

    async def _target_name(
        self, event: Any, group_id: str, target: str, records: list[QuoteRecord]
    ) -> str:
        """当前群名片优先，失败时取最近一次存档昵称。"""
        if name := (await self.members.profile(event, group_id, target)).name:
            return name
        for record in reversed(records):
            if (
                record.author
                and record.author.user_id == target
                and record.author.nickname
            ):
                return record.author.nickname
            for node in reversed(list(record.iter_nodes())):
                if node.author.user_id == target and node.author.nickname:
                    return node.author.nickname
        return target

    # 统计与帮助 -------------------------------------------------------

    async def info(self, event: Any, ctx: OperationContext) -> None:
        targets = call_input(event).targets
        if len(targets) > 1:
            raise UsageError("一次只能指定一名用户。", "info")
        if targets:
            await self._user_info(event, ctx, targets[0])
            return
        info = await self.storage.info(str(event.get_group_id()))
        await self.sender.text(
            event,
            ctx,
            "\n".join(
                (
                    f"群典总数：{info['total']} / {ctx.values['max_records_per_group']}",
                    f"普通金句：{info['messages']}",
                    f"合并转发：{info['forwards']}",
                    f"含图片记录：{info['with_images']}",
                    f"异常记录：{info['broken']}",
                    f"图片占用：{format_bytes(info['image_bytes'])}",
                    f"最早收录：{local_time(info['earliest']) or '无'}",
                    f"最近收录：{local_time(info['latest']) or '无'}",
                )
            ),
        )

    async def _insights(self, event: Any, group_id: str, values: dict[str, Any]) -> Any:
        """排行与个人统计共用：纠正作者身份、去掉被隐藏的记录后再统计。"""
        records = await self.storage.group_records(group_id)
        await self.members.resolve_in_place(event, group_id, records, values)

        def run() -> Any:
            return compute(
                [record for record in records if not self._is_hidden(record, values)]
            )

        return await asyncio.to_thread(run)

    async def rank(self, event: Any, ctx: OperationContext) -> None:
        """当前群的参与排行与收录排行。"""
        group_id = str(event.get_group_id())
        insights = await self._insights(event, group_id, ctx.values)
        if not insights.total:
            await self.sender.text(event, ctx, "当前群还没有群典。")
            return
        top = [user_id for user_id, _ in insights.ranking()[:RANKING_LIMIT]]
        top += [user_id for user_id, _ in insights.recorders.most_common(3)]
        names = {}
        for user_id in dict.fromkeys(top):
            if name := (await self.members.profile(event, group_id, user_id)).name:
                names[user_id] = name
        await self.sender.text(
            event, ctx, ranking_text(insights, names, limit=RANKING_LIMIT)
        )

    async def _user_info(self, event: Any, ctx: OperationContext, target: str) -> None:
        group_id = str(event.get_group_id())
        insights = await self._insights(event, group_id, ctx.values)
        name = (await self.members.profile(event, group_id, target)).name
        text = user_text(insights, target, name or insights.names.get(target) or target)
        await self.sender.text(event, ctx, text or "该用户在当前群暂无群典。")

    # 定时推送 ---------------------------------------------------------

    async def push_daily(self, event: Any, values: dict[str, Any]) -> None:
        """每日一典：没有可用记录时不发送。"""
        group_id = str(event.get_group_id())
        records, _ = await self._pick_random(event, group_id, values, 1)
        if not records:
            return
        ctx = OperationContext(values)
        await self.sender.text(event, ctx, "今日群典")
        await self._send_and_mark(event, ctx, records)

    async def push_on_this_day(
        self, event: Any, values: dict[str, Any], now: datetime
    ) -> None:
        """那年今日：往年同月同日收录的记录；当天没有时不发送。"""
        group_id = str(event.get_group_id())
        month_day = now.strftime("%m-%d")
        year = now.strftime("%Y")
        ids = [
            record_id
            for record_id, recorded_at in await self.storage.dated_candidates(
                group_id, values["excluded_author_ids"]
            )
            if (stamp := local_time(recorded_at, "%Y-%m-%d"))
            and stamp[5:] == month_day
            and stamp[:4] < year
        ]
        if not ids:
            return
        resolution = await self.members.resolve_records(
            event, group_id, await self.storage.load(ids), values
        )
        visible = [
            record
            for record in resolution.records
            if not self._is_hidden(record, values)
        ]
        records, _ = await self._sample_healthy(
            random.sample(visible, len(visible)), values["on_this_day_count"]
        )
        if not records:
            return
        ctx = OperationContext(values)
        await self.sender.text(event, ctx, f"那年今日（{now.month} 月 {now.day} 日）")
        await self.send_records(event, ctx, records)

    async def help(self, event: Any, ctx: OperationContext) -> None:
        """发送当前群配置渲染后的总帮助。"""
        values = ctx.values
        if not values["help_enabled"]:
            await self.sender.text(event, ctx, "群典帮助功能未启用。")
            return
        overview = build_help_text(values)
        if values["help_send_mode"] == "card" and await self._send_help_card(
            event, ctx, overview
        ):
            return
        await self.sender.text(event, ctx, overview)

    async def _send_help_card(
        self, event: Any, ctx: OperationContext, overview: str
    ) -> bool:
        """发送帮助卡片；渲染失败返回 False 由调用方改发文字，结果未知时不再补发。"""
        path: str | None = None
        try:
            path = await self.cards.help_card_path(overview, ctx.values)
            await self.sender.chain(event, ctx, [Comp.Image.fromFileSystem(path)])
            return True
        except AmbiguousSendError:
            raise
        except Exception as exc:  # noqa: BLE001 - 渲染服务异常类型不稳定，降级为文字。
            logger.warning(
                "群典：帮助卡片生成失败，改为文字发送；原因=%s", error_reason(exc)
            )
            ctx.log_override = "帮助卡片失败，已改为文字"
            return False
        finally:
            if path:
                with contextlib.suppress(OSError):
                    Path(path).unlink(missing_ok=True)

    # 删除 -------------------------------------------------------------

    async def prepare_delete(
        self, event: Any, ctx: OperationContext, keyword: str
    ) -> None:
        values = ctx.values
        needle = normalize_search(keyword)
        if not needle:
            raise UsageError("请提供要匹配的正文字符串。", "delete")
        group_id = str(event.get_group_id())
        matches: list[QuoteRecord] = []
        if prefix := parse_record_id(keyword):
            matches = await self.storage.find_by_prefix(group_id, prefix)
            if len(matches) > 1:
                await self.sender.text(
                    event, ctx, "匹配到多条记录，请提供更长的记录 ID。"
                )
                return
        if not matches:
            matches = await self.storage.search(group_id, needle)
        if not matches:
            await self.sender.text(event, ctx, "没有找到包含该字符串的群典。")
            return
        if len(matches) > values["delete_preview_limit"]:
            await self.sender.text(
                event,
                ctx,
                f"共命中 {len(matches)} 条，超过预览上限，请缩小关键词或在后台管理。",
            )
            return
        header = f"以下 {len(matches)} 条群典将被删除："
        if len(matches) > 1:
            # 命中多条时给出 ID，便于改用 /删除群典 #ID 只删其中一条。
            ids = "、".join(record.id[:8] for record in matches)
            header += f"\n记录 ID：{ids}"
        await self.sender.text(event, ctx, header)
        if not await self.send_records(event, ctx, matches, preview=True):
            await self.sender.text(
                event, ctx, "删除预览未能完整发送，本次未创建删除确认。"
            )
            return
        await self.sender.text(
            event, ctx, f"请在 {DELETE_CONFIRM_SECONDS} 秒内发送 /确认删除。"
        )
        self.pending_deletions[(group_id, str(event.get_sender_id()))] = (
            PendingDeletion(
                expires_at=time.monotonic() + DELETE_CONFIRM_SECONDS,
                record_hashes={record.id: record.content_hash for record in matches},
            )
        )

    async def confirm_delete(self, event: Any, ctx: OperationContext) -> None:
        key = (str(event.get_group_id()), str(event.get_sender_id()))
        pending = self.pending_deletions.pop(key, None)
        if not pending or pending.expires_at < time.monotonic():
            await self.sender.text(event, ctx, "没有有效的待确认删除操作。")
            return
        async with self.media_lock:
            deleted, orphans = await self.storage.delete_records(
                key[0],
                set(pending.record_hashes),
                deleted_by=key[1],
                source="command",
                expected_hashes=pending.record_hashes,
                audit_limit=ctx.values["audit_limit"],
            )
            await self.delete_files(orphans)
        await self.sender.text(event, ctx, f"已删除 {deleted} 条群典。")

    async def delete_files(self, orphans: list[tuple[str, str]]) -> None:
        def run() -> None:
            for sha256, mime in orphans:
                self.media.delete(sha256, mime)

        if orphans:
            await asyncio.to_thread(run)
