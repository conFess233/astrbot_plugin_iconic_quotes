"""OneBot 11 引用、合并转发与消息段规范化。"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger

from ..models import (
    AuthorSnapshot,
    ForwardNode,
    QuoteRecord,
    QuoteSegment,
    ReplySnapshot,
)
from ..utils.hashing import calculate_record_hash
from .permissions import call_onebot_action
from .storage import QuoteStorage, StorageError


class CaptureError(RuntimeError):
    """表示无法完整、可靠地收录本次消息。"""


@dataclass(slots=True)
class _RawReply:
    message_id: str


@dataclass(slots=True)
class _RawSticker:
    file: str
    url: str
    emoji_package_id: str
    emoji_id: str
    key: str
    summary: str


def _utc_iso(timestamp: Any = None) -> str | None:
    if timestamp in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _as_author(user_id: Any, nickname: Any) -> AuthorSnapshot:
    normalized_id = str(user_id).strip() if user_id not in (None, "", 0, "0") else None
    name = str(nickname or normalized_id or "未知用户").strip()
    return AuthorSnapshot(user_id=normalized_id, nickname=name)


def _raw_component(value: dict[str, Any]) -> Any:
    component_type = str(value.get("type") or "").lower()
    data = value.get("data") if isinstance(value.get("data"), dict) else {}
    if component_type == "text":
        return Comp.Plain(str(data.get("text") or ""))
    if component_type in {"image", "mface"} and (
        component_type == "mface" or data.get("emoji_id")
    ):
        return _RawSticker(
            file=str(data.get("file") or data.get("url") or ""),
            url=str(data.get("url") or ""),
            emoji_package_id=str(data.get("emoji_package_id") or ""),
            emoji_id=str(data.get("emoji_id") or ""),
            key=str(data.get("key") or ""),
            summary=str(data.get("summary") or "[商城表情]"),
        )
    if component_type == "image":
        return Comp.Image(
            file=str(data.get("file") or data.get("url") or ""),
            url=str(data.get("url") or ""),
        )
    if component_type == "at":
        return Comp.At(qq=data.get("qq", ""), name=data.get("name", ""))
    if component_type == "face":
        try:
            return Comp.Face(id=int(data.get("id")))
        except (TypeError, ValueError):
            return None
    if component_type == "reply":
        return _RawReply(str(data.get("id") or data.get("message_id") or ""))
    if component_type == "forward":
        return Comp.Forward(id=str(data.get("id") or ""))
    if component_type == "node":
        # 用 Forward 哨兵交给统一的嵌套来源检查，不能静默忽略 Node。
        return Comp.Forward(id="__nested_node__")
    return None


class OneBotQuoteExtractor:
    """把 OneBot 消息转换为与适配器对象解耦的记录模型。"""

    def __init__(self, storage: QuoteStorage):
        self.storage = storage
        self.last_ignored_segments = 0
        self._bot_cache: dict[tuple[str, str], tuple[float, bool | None]] = {}

    async def extract(
        self,
        event: Any,
        settings: dict[str, Any],
    ) -> QuoteRecord:
        """从唯一引用或合并转发来源构造群典记录。"""
        messages = list(event.get_messages())
        self.last_ignored_segments = 0
        replies = [item for item in messages if isinstance(item, Comp.Reply)]
        forwards = [
            item
            for item in messages
            if isinstance(item, (Comp.Forward, Comp.Node, Comp.Nodes))
        ]
        if len(replies) + len(forwards) != 1:
            raise CaptureError("一次只能添加一个引用或合并转发来源")
        group_id = str(event.get_group_id())
        operator = _as_author(event.get_sender_id(), event.get_sender_name())
        try:
            if replies:
                record = await self._from_reply(
                    event,
                    replies[0],
                    group_id,
                    operator,
                    settings,
                )
            else:
                record = await self._from_forward_component(
                    event,
                    forwards[0],
                    group_id,
                    operator,
                    settings,
                )
            record.content_hash = calculate_record_hash(record)
            return record
        except Exception:
            await self.storage.cleanup_group_orphans(group_id)
            raise

    async def _from_reply(
        self,
        event: Any,
        reply: Any,
        group_id: str,
        operator: AuthorSnapshot,
        settings: dict[str, Any],
    ) -> QuoteRecord:
        chain = list(getattr(reply, "chain", None) or [])
        author = _as_author(
            getattr(reply, "sender_id", None),
            getattr(reply, "sender_nickname", None),
        )
        sent_at = _utc_iso(getattr(reply, "time", None))
        source_id = str(getattr(reply, "id", "") or "") or None
        if source_id:
            try:
                payload = await self._get_message(event, source_id)
                payload_chain = self._payload_components(payload)
                if payload_chain:
                    chain = payload_chain
                sender = (
                    payload.get("sender")
                    if isinstance(payload.get("sender"), dict)
                    else {}
                )
                author = _as_author(
                    sender.get("user_id") or payload.get("user_id"),
                    sender.get("card") or sender.get("nickname"),
                )
                sent_at = _utc_iso(payload.get("time")) or sent_at
            except CaptureError:
                if not chain:
                    raise
        nested_sources = [
            item
            for item in chain
            if isinstance(item, (Comp.Forward, Comp.Node, Comp.Nodes))
        ]
        if len(nested_sources) > 1:
            raise CaptureError("引用消息中包含多个合并转发，无法确定唯一来源")
        if nested_sources:
            return await self._from_forward_component(
                event,
                nested_sources[0],
                group_id,
                operator,
                settings,
                source_message_id=source_id,
            )
        await self._check_author_allowed(event, group_id, author, settings)
        counter = [0]
        reply_snapshot = await self._capture_reply_from_chain(
            event,
            chain,
            group_id,
            settings,
            counter,
            visited={source_id} if source_id else set(),
        )
        segments, ignored = await self._segments(
            chain,
            group_id,
            settings,
            counter,
            nested_forbidden=True,
        )
        self.last_ignored_segments += ignored
        if not segments and reply_snapshot is None:
            raise CaptureError("引用消息没有可保存的文字、图片或表情")
        text_length = self._validate_text_limits(segments, settings["max_text_chars"])
        if (
            text_length + self._reply_text_length(reply_snapshot)
            > settings["max_forward_text_chars"]
        ):
            raise CaptureError("消息与回复链累计文字长度超过配置上限")
        return QuoteRecord(
            id=uuid.uuid4().hex,
            type="message",
            group_id=group_id,
            author=author,
            segments=segments,
            source_message_id=source_id,
            source_sent_at=sent_at,
            recorded_at=datetime.now(UTC).isoformat(),
            recorded_by=operator,
            identity_incomplete=author.user_id is None,
            reply=reply_snapshot,
        )

    async def _from_forward_component(
        self,
        event: Any,
        component: Any,
        group_id: str,
        operator: AuthorSnapshot,
        settings: dict[str, Any],
        source_message_id: str | None = None,
    ) -> QuoteRecord:
        if isinstance(component, Comp.Forward):
            forward_id = str(getattr(component, "id", "") or "")
            if not forward_id:
                raise CaptureError("合并转发缺少可读取的消息 ID")
            payload = await self._get_forward_message(event, forward_id)
            raw_nodes = self._forward_payload_nodes(payload)
        elif isinstance(component, Comp.Nodes):
            raw_nodes = list(component.nodes)
        else:
            raw_nodes = [component]
        if not raw_nodes:
            raise CaptureError("合并转发内容为空或已过期")
        if len(raw_nodes) > settings["max_forward_nodes"]:
            raise CaptureError("合并转发节点数超过配置上限")
        nodes: list[ForwardNode] = []
        image_counter = [0]
        total_text = 0
        for raw_node in raw_nodes:
            author, chain, sent_at = self._parse_forward_node(raw_node)
            await self._check_author_allowed(event, group_id, author, settings)
            reply_snapshot = await self._capture_reply_from_chain(
                event,
                chain,
                group_id,
                settings,
                image_counter,
                visited=set(),
            )
            segments, ignored = await self._segments(
                chain,
                group_id,
                settings,
                image_counter,
                nested_forbidden=True,
            )
            self.last_ignored_segments += ignored
            if not segments and reply_snapshot is None:
                raise CaptureError("合并转发包含无法保存的空节点")
            node_text = self._validate_text_limits(segments, settings["max_text_chars"])
            total_text += node_text + self._reply_text_length(reply_snapshot)
            nodes.append(
                ForwardNode(
                    author=author,
                    segments=segments,
                    source_sent_at=sent_at,
                    reply=reply_snapshot,
                )
            )
        if total_text > settings["max_forward_text_chars"]:
            raise CaptureError("合并转发累计文字长度超过配置上限")
        return QuoteRecord(
            id=uuid.uuid4().hex,
            type="forward",
            group_id=group_id,
            author=None,
            nodes=nodes,
            source_message_id=source_message_id,
            recorded_at=datetime.now(UTC).isoformat(),
            recorded_by=operator,
            identity_incomplete=any(node.author.user_id is None for node in nodes),
        )

    async def _get_message(self, event: Any, message_id: str) -> dict[str, Any]:
        attempts: Iterable[Any] = (
            message_id,
            int(message_id) if message_id.isdigit() else message_id,
        )
        for value in attempts:
            for key in ("message_id", "id"):
                try:
                    payload = await call_onebot_action(event, "get_msg", **{key: value})
                    if isinstance(payload, dict):
                        return payload
                except Exception as exc:  # noqa: BLE001 - OneBot 实现异常类型不统一。
                    logger.debug("读取引用消息失败: action=get_msg error=%s", exc)
                    continue
        raise CaptureError("无法读取被引用消息，消息可能已过期")

    async def _get_forward_message(self, event: Any, forward_id: str) -> dict[str, Any]:
        """兼容 OneBot 实现对 get_forward_msg 参数名和 ID 类型的差异。"""
        attempts: Iterable[Any] = (
            forward_id,
            int(forward_id) if forward_id.isdigit() else forward_id,
        )
        for value in attempts:
            for key in ("message_id", "id"):
                try:
                    payload = await call_onebot_action(
                        event,
                        "get_forward_msg",
                        **{key: value},
                    )
                    if isinstance(payload, dict):
                        return payload
                except Exception as exc:  # noqa: BLE001 - OneBot 实现异常类型不统一。
                    logger.debug(
                        "读取合并转发失败: action=get_forward_msg error=%s", exc
                    )
        raise CaptureError("无法读取合并转发，消息可能已过期")

    @staticmethod
    def _payload_components(payload: dict[str, Any]) -> list[Any]:
        raw = payload.get("message")
        if not isinstance(raw, list):
            raw = payload.get("content")
        if not isinstance(raw, list):
            return []
        return [
            component
            for item in raw
            if isinstance(item, dict)
            if (component := _raw_component(item))
        ]

    @staticmethod
    def _forward_payload_nodes(payload: Any) -> list[Any]:
        if isinstance(payload, dict):
            raw = (
                payload.get("messages")
                or payload.get("message")
                or payload.get("nodes")
            )
            if isinstance(raw, list):
                return raw
        return []

    @staticmethod
    def _parse_forward_node(
        raw_node: Any,
    ) -> tuple[AuthorSnapshot, list[Any], str | None]:
        if isinstance(raw_node, Comp.Node):
            return (
                _as_author(raw_node.uin, raw_node.name),
                list(raw_node.content),
                _utc_iso(raw_node.time),
            )
        if not isinstance(raw_node, dict):
            raise CaptureError("合并转发节点格式无效")
        data = (
            raw_node.get("data") if isinstance(raw_node.get("data"), dict) else raw_node
        )
        if str(raw_node.get("type") or "").lower() == "forward":
            raise CaptureError("暂不支持嵌套合并转发")
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        author = _as_author(
            data.get("user_id") or data.get("uin") or sender.get("user_id"),
            data.get("nickname")
            or data.get("name")
            or sender.get("card")
            or sender.get("nickname"),
        )
        raw_content = data.get("content") or data.get("message")
        if not isinstance(raw_content, list):
            raise CaptureError("合并转发节点内容格式无效")
        chain = [
            component
            for item in raw_content
            if isinstance(item, dict)
            if (component := _raw_component(item))
        ]
        return author, chain, _utc_iso(data.get("time"))

    async def _segments(
        self,
        chain: list[Any],
        group_id: str,
        settings: dict[str, Any],
        image_counter: list[int],
        *,
        nested_forbidden: bool,
    ) -> tuple[list[QuoteSegment], int]:
        result: list[QuoteSegment] = []
        ignored = 0
        for component in chain:
            if isinstance(component, Comp.Plain):
                text = str(component.text)
                if text:
                    if result and result[-1].type == "text":
                        result[-1].text = (result[-1].text or "") + text
                    else:
                        result.append(QuoteSegment(type="text", text=text))
            elif isinstance(component, Comp.At):
                name = str(getattr(component, "name", "") or "").strip()
                target = str(getattr(component, "qq", "") or "")
                text = f"@{name or ('全体成员' if target == 'all' else target)}"
                if result and result[-1].type == "text":
                    result[-1].text = (result[-1].text or "") + text
                else:
                    result.append(QuoteSegment(type="text", text=text))
            elif isinstance(component, Comp.Image):
                image_counter[0] += 1
                if image_counter[0] > settings["max_images_per_record"]:
                    raise CaptureError("图片数量超过配置上限")
                try:
                    image_path = await component.convert_to_file_path()
                    data = await asyncio.to_thread(Path(image_path).read_bytes)
                    saved = await self.storage.save_image(
                        group_id,
                        data,
                        max_image_bytes=settings["max_image_mb"] * 1024 * 1024,
                        max_media_bytes=settings["max_media_mb"] * 1024 * 1024,
                    )
                except (OSError, ValueError, StorageError) as exc:
                    raise CaptureError(f"图片保存失败: {exc}") from exc
                result.append(QuoteSegment.from_dict(saved))
            elif isinstance(component, Comp.Face):
                face_id = str(getattr(component, "id", "") or "").strip()
                if not face_id:
                    raise CaptureError("QQ 表情缺少表情 ID")
                result.append(QuoteSegment(type="face", face_id=face_id))
            elif isinstance(component, _RawSticker):
                image_counter[0] += 1
                if image_counter[0] > settings["max_images_per_record"]:
                    raise CaptureError("图片与贴纸数量超过配置上限")
                try:
                    image = Comp.Image(file=component.file, url=component.url)
                    image_path = await image.convert_to_file_path()
                    data = await asyncio.to_thread(Path(image_path).read_bytes)
                    saved = await self.storage.save_image(
                        group_id,
                        data,
                        max_image_bytes=settings["max_image_mb"] * 1024 * 1024,
                        max_media_bytes=settings["max_media_mb"] * 1024 * 1024,
                    )
                except (OSError, ValueError, StorageError) as exc:
                    raise CaptureError(f"贴纸保存失败: {exc}") from exc
                sticker = QuoteSegment.from_dict(saved)
                sticker.type = "sticker"
                sticker.emoji_package_id = component.emoji_package_id or None
                sticker.emoji_id = component.emoji_id or None
                sticker.key = component.key or None
                sticker.summary = component.summary or None
                result.append(sticker)
            elif isinstance(component, (Comp.Forward, Comp.Node, Comp.Nodes)):
                if nested_forbidden:
                    raise CaptureError("暂不支持嵌套合并转发")
            elif isinstance(component, (Comp.Reply, _RawReply)):
                continue
            else:
                ignored += 1
        return result, ignored

    async def _capture_reply_from_chain(
        self,
        event: Any,
        chain: list[Any],
        group_id: str,
        settings: dict[str, Any],
        image_counter: list[int],
        *,
        visited: set[str],
        depth: int = 1,
    ) -> ReplySnapshot | None:
        replies = [item for item in chain if isinstance(item, (Comp.Reply, _RawReply))]
        if len(replies) > 1:
            raise CaptureError("消息包含多个回复来源，无法可靠本地化")
        if not replies:
            return None
        component = replies[0]
        message_id = (
            component.message_id
            if isinstance(component, _RawReply)
            else str(getattr(component, "id", "") or "")
        )
        if not message_id:
            raise CaptureError("回复关系缺少可读取的消息 ID")
        if message_id in visited:
            return ReplySnapshot(
                author=AuthorSnapshot(nickname="更早的回复"),
                segments=[],
                truncated=True,
            )
        next_visited = {*visited, message_id}
        fallback_chain = (
            list(getattr(component, "chain", None) or [])
            if isinstance(component, Comp.Reply)
            else []
        )
        try:
            payload = await self._get_message(event, message_id)
            reply_chain = self._payload_components(payload) or fallback_chain
            sender = (
                payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
            )
            author = _as_author(
                sender.get("user_id") or payload.get("user_id"),
                sender.get("card") or sender.get("nickname"),
            )
        except CaptureError:
            if not fallback_chain:
                raise CaptureError("无法读取被回复消息，回复快照保存失败") from None
            reply_chain = fallback_chain
            author = _as_author(
                getattr(component, "sender_id", None),
                getattr(component, "sender_nickname", None),
            )
        await self._check_author_allowed(event, group_id, author, settings)
        segments, ignored = await self._segments(
            reply_chain,
            group_id,
            settings,
            image_counter,
            nested_forbidden=True,
        )
        self.last_ignored_segments += ignored
        nested_exists = any(
            isinstance(item, (Comp.Reply, _RawReply)) for item in reply_chain
        )
        truncated = nested_exists and depth >= settings["max_reply_depth"]
        nested = None
        if nested_exists and not truncated:
            nested = await self._capture_reply_from_chain(
                event,
                reply_chain,
                group_id,
                settings,
                image_counter,
                visited=next_visited,
                depth=depth + 1,
            )
        if not segments and nested is None and not truncated:
            raise CaptureError("被回复消息没有可保存的文字、图片或表情")
        self._validate_text_limits(segments, settings["max_text_chars"])
        return ReplySnapshot(
            author=author,
            segments=segments,
            reply=nested,
            truncated=truncated,
        )

    @classmethod
    def _reply_text_length(cls, reply: ReplySnapshot | None) -> int:
        if reply is None:
            return 0
        return sum(
            len(segment.text or "")
            for segment in reply.segments
            if segment.type == "text"
        ) + cls._reply_text_length(reply.reply)

    @staticmethod
    def _validate_text_limits(segments: list[QuoteSegment], limit: int) -> int:
        length = sum(
            len(segment.text or "") for segment in segments if segment.type == "text"
        )
        if length > limit:
            raise CaptureError("文字长度超过配置上限")
        return length

    async def _check_author_allowed(
        self,
        event: Any,
        group_id: str,
        author: AuthorSnapshot,
        settings: dict[str, Any],
    ) -> None:
        excluded = set(settings["excluded_author_ids"])
        if author.user_id in excluded:
            raise CaptureError("该用户不允许被收录")
        if excluded and author.user_id is None:
            raise CaptureError("发送者身份不完整，无法完成禁止收录名单校验")
        if settings["allow_bot_authors"] or author.user_id is None:
            return
        cache_key = (group_id, author.user_id)
        cached = self._bot_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            is_bot = cached[1]
        else:
            is_bot = None
            try:
                payload = await call_onebot_action(
                    event,
                    "get_group_member_info",
                    group_id=int(group_id),
                    user_id=int(author.user_id),
                    no_cache=False,
                )
                if isinstance(payload, dict) and (
                    "is_robot" in payload or "is_bot" in payload
                ):
                    is_bot = bool(payload.get("is_robot") or payload.get("is_bot"))
            except Exception as exc:  # noqa: BLE001 - 机器人标记不是 OneBot 11 标准字段。
                logger.debug(
                    "无法读取群成员机器人标记: user=%s error=%s", author.user_id, exc
                )
            self._bot_cache[cache_key] = (time.monotonic() + 300, is_bot)
        if is_bot:
            raise CaptureError("配置不允许收录机器人发送的消息")
