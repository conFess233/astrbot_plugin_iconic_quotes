"""OneBot 11 引用、合并转发与消息段规范化。"""

from __future__ import annotations

import asyncio
import html
import re
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger

from ..models import (
    AuthorSnapshot,
    ForwardNode,
    NestedForward,
    QuoteRecord,
    QuoteSegment,
    ReplySnapshot,
)
from ..utils.hashing import calculate_record_hash
from .permissions import call_onebot_action
from .storage import QuoteStorage, StorageError


class CaptureError(RuntimeError):
    """表示无法完整、可靠地收录本次消息。"""


def _error_reason(error: BaseException) -> str:
    """生成不含消息正文和接口载荷的异常摘要。"""
    detail = str(error).strip()
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__


@dataclass(slots=True)
class _RawReply:
    message_id: str
    message_seq: str | None = None


@dataclass(slots=True)
class _RawSticker:
    file: str
    url: str
    emoji_package_id: str
    emoji_id: str
    key: str
    summary: str


@dataclass(slots=True)
class _RawForward:
    """保留 OneBot forward 段的内联节点，避免转成组件时丢失 content。"""

    forward_id: str
    content: list[Any]


def _utc_iso(timestamp: Any = None) -> str | None:
    if timestamp in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _as_author(
    user_id: Any,
    nickname: Any,
    *,
    raw_user_id: Any = None,
    identity_source: str | None = None,
) -> AuthorSnapshot:
    normalized_id = str(user_id).strip() if user_id not in (None, "", 0, "0") else None
    name = str(nickname or normalized_id or "未知用户").strip()
    normalized_raw = (
        str(raw_user_id).strip()
        if raw_user_id not in (None, "", 0, "0")
        else normalized_id
    )
    return AuthorSnapshot(
        user_id=normalized_id,
        nickname=name,
        raw_user_id=normalized_raw,
        identity_source=identity_source or ("onebot" if normalized_id else "unknown"),
    )


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
        return _RawReply(
            str(data.get("id") or data.get("message_id") or ""),
            str(data.get("seq") or data.get("message_seq") or "") or None,
        )
    if component_type == "forward":
        inline = data.get("content")
        return _RawForward(
            forward_id=str(
                data.get("res_id") or data.get("resid") or data.get("id") or ""
            ),
            content=list(inline) if isinstance(inline, list) else [],
        )
    if component_type == "node":
        # 用 Forward 哨兵交给统一的嵌套来源检查，不能静默忽略 Node。
        return Comp.Forward(id="__nested_node__")
    return None


_CQ_PATTERN = re.compile(r"\[CQ:([a-zA-Z0-9_]+)((?:,[^\]]*)?)\]")


def _cq_components(value: str) -> list[Any]:
    """解析 OneBot 可能返回的 CQ 字符串，未知段保留为普通文字。"""
    result: list[Any] = []
    cursor = 0
    for match in _CQ_PATTERN.finditer(value):
        if match.start() > cursor:
            result.append(Comp.Plain(html.unescape(value[cursor : match.start()])))
        data: dict[str, str] = {}
        raw_params = match.group(2).lstrip(",")
        for item in raw_params.split(",") if raw_params else []:
            key, separator, raw_value = item.partition("=")
            if separator:
                data[key] = html.unescape(raw_value)
        component = _raw_component({"type": match.group(1), "data": data})
        result.append(component or Comp.Plain(html.unescape(match.group(0))))
        cursor = match.end()
    if cursor < len(value):
        result.append(Comp.Plain(html.unescape(value[cursor:])))
    return result


class OneBotQuoteExtractor:
    """把 OneBot 消息转换为与适配器对象解耦的记录模型。"""

    def __init__(self, storage: QuoteStorage):
        self.storage = storage
        self.last_ignored_segments = 0
        self._member_cache: dict[
            tuple[str, str], tuple[float, str | None, bool | None]
        ] = {}

    async def extract(
        self,
        event: Any,
        settings: dict[str, Any],
    ) -> QuoteRecord:
        """从唯一引用或合并转发来源构造群典记录。"""
        messages = list(event.get_messages())
        self.last_ignored_segments = 0
        replies = [
            item for item in messages if isinstance(item, (Comp.Reply, _RawReply))
        ]
        if not replies:
            raw_reply = self._raw_reply_data(event)
            if raw_reply:
                replies.append(
                    _RawReply(
                        message_id=str(
                            raw_reply.get("id") or raw_reply.get("message_id") or ""
                        ),
                        message_seq=str(
                            raw_reply.get("seq") or raw_reply.get("message_seq") or ""
                        )
                        or None,
                    )
                )
        forwards = [
            item
            for item in messages
            if isinstance(item, (_RawForward, Comp.Forward, Comp.Node, Comp.Nodes))
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
        raw_reply = self._raw_reply_data(event)
        author = _as_author(
            getattr(reply, "sender_id", None),
            getattr(reply, "sender_nickname", None),
        )
        sent_at = _utc_iso(getattr(reply, "time", None))
        source_id = (
            str(
                getattr(reply, "message_id", None)
                or getattr(reply, "id", "")
                or raw_reply.get("id")
                or raw_reply.get("message_id")
                or ""
            )
            or None
        )
        source_seq = (
            str(
                getattr(reply, "message_seq", None)
                or getattr(reply, "seq", "")
                or raw_reply.get("seq")
                or raw_reply.get("message_seq")
                or ""
            )
            or None
        )
        # AstrBot 已成功展开 Reply.chain 时直接采用，避免重复请求易失消息 ID。
        if not chain and (source_id or source_seq):
            payload = await self._get_message(
                event,
                source_id or "",
                group_id=group_id,
                message_seq=source_seq,
            )
            payload_chain = self._payload_components(payload)
            if payload_chain:
                chain = payload_chain
            sender = (
                payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
            )
            author = _as_author(
                sender.get("user_id") or payload.get("user_id"),
                sender.get("card") or sender.get("nickname"),
            )
            sent_at = _utc_iso(payload.get("time")) or sent_at
        if not chain:
            raise CaptureError("无法读取被引用消息，消息可能已过期")
        author = await self._resolve_author(event, group_id, author)
        nested_sources = [
            item
            for item in chain
            if isinstance(item, (_RawForward, Comp.Forward, Comp.Node, Comp.Nodes))
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
        reply_snapshot, reply_missing = await self._capture_reply_from_chain(
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
            reply_missing=reply_missing,
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
        raw_nodes, forward_id = await self._load_forward_source(event, component)
        image_counter = [0]
        nodes, total_text = await self._capture_forward_nodes(
            event,
            raw_nodes,
            group_id,
            settings,
            image_counter,
            depth=1,
            node_counter=[0],
            ancestor_ids={forward_id} if forward_id else set(),
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
            identity_incomplete=any(
                author_id is None
                for node in nodes
                for author_id in QuoteRecord._node_author_ids(node)
            ),
            source_forward_id=forward_id,
        )

    async def _load_forward_source(
        self,
        event: Any,
        component: Any,
    ) -> tuple[list[Any], str | None]:
        if isinstance(component, _RawForward):
            forward_id = component.forward_id or None
            # NapCat 已把完整子节点放在段内时，优先使用它，避免再次查询后被拍平。
            if component.content:
                raw_nodes = component.content
            elif forward_id:
                payload = await self._get_forward_message(event, forward_id)
                raw_nodes = self._forward_payload_nodes(payload)
            else:
                raise CaptureError("合并转发既没有内联内容，也没有可回退读取的消息 ID")
        elif isinstance(component, Comp.Forward):
            forward_id = str(getattr(component, "id", "") or "")
            if not forward_id or forward_id == "__nested_node__":
                raise CaptureError("合并转发缺少可读取的消息 ID")
            payload = await self._get_forward_message(event, forward_id)
            raw_nodes = self._forward_payload_nodes(payload)
        elif isinstance(component, Comp.Nodes):
            raw_nodes = list(component.nodes)
            forward_id = None
        elif isinstance(component, Comp.Node):
            raw_nodes = [component]
            forward_id = None
        else:
            raise CaptureError("合并转发来源格式无效")
        if not raw_nodes:
            raise CaptureError("合并转发内容为空或已过期")
        return raw_nodes, forward_id

    async def _capture_forward_nodes(
        self,
        event: Any,
        raw_nodes: list[Any],
        group_id: str,
        settings: dict[str, Any],
        image_counter: list[int],
        *,
        depth: int,
        node_counter: list[int],
        ancestor_ids: set[str],
    ) -> tuple[list[ForwardNode], int]:
        # 上限按整棵树累计，而不是每一层分别计算。
        node_counter[0] += len(raw_nodes)
        if node_counter[0] > settings["max_forward_nodes"]:
            raise CaptureError("合并转发节点数超过配置上限")
        nodes: list[ForwardNode] = []
        total_text = 0
        for raw_node in raw_nodes:
            author, chain, sent_at = self._parse_forward_node(raw_node)
            author = await self._resolve_author(event, group_id, author)
            await self._check_author_allowed(event, group_id, author, settings)
            reply_snapshot, reply_missing = await self._capture_reply_from_chain(
                event,
                chain,
                group_id,
                settings,
                image_counter,
                visited=set(),
            )
            segments: list[QuoteSegment] = []
            nested_forwards: list[NestedForward] = []
            ordinary_chunk: list[Any] = []

            for component in chain:
                if not isinstance(
                    component, (_RawForward, Comp.Forward, Comp.Node, Comp.Nodes)
                ):
                    ordinary_chunk.append(component)
                    continue
                await self._append_segment_chunk(
                    ordinary_chunk,
                    segments,
                    group_id,
                    settings,
                    image_counter,
                )
                if depth >= settings["max_nested_forward_depth"]:
                    raise CaptureError(
                        "嵌套合并转发深度超过配置上限 "
                        f"{settings['max_nested_forward_depth']}"
                    )
                child_id = self._forward_component_id(component)
                if child_id and child_id in ancestor_ids:
                    raise CaptureError(f"第 {depth + 1} 层嵌套合并转发存在循环引用")
                try:
                    child_raw, child_id = await self._load_forward_source(
                        event, component
                    )
                    child_nodes, child_text = await self._capture_forward_nodes(
                        event,
                        child_raw,
                        group_id,
                        settings,
                        image_counter,
                        depth=depth + 1,
                        node_counter=node_counter,
                        ancestor_ids=(
                            {*ancestor_ids, child_id} if child_id else ancestor_ids
                        ),
                    )
                except CaptureError as exc:
                    raise CaptureError(
                        f"第 {depth + 1} 层嵌套合并转发保存失败: {exc}"
                    ) from exc
                nested_forwards.append(
                    NestedForward(
                        position=len(segments),
                        nodes=child_nodes,
                        source_forward_id=child_id,
                    )
                )
                total_text += child_text
            await self._append_segment_chunk(
                ordinary_chunk,
                segments,
                group_id,
                settings,
                image_counter,
            )
            if not segments and reply_snapshot is None and not nested_forwards:
                raise CaptureError("合并转发包含无法保存的空节点")
            node_text = self._validate_text_limits(segments, settings["max_text_chars"])
            total_text += node_text + self._reply_text_length(reply_snapshot)
            nodes.append(
                ForwardNode(
                    author=author,
                    segments=segments,
                    source_sent_at=sent_at,
                    reply=reply_snapshot,
                    reply_missing=reply_missing,
                    nested_forwards=nested_forwards,
                )
            )
        return nodes, total_text

    @staticmethod
    def _forward_component_id(component: Any) -> str | None:
        """只提取当前分支的转发 ID，供祖先链循环检测使用。"""
        if isinstance(component, _RawForward):
            return component.forward_id or None
        if isinstance(component, Comp.Forward):
            value = str(getattr(component, "id", "") or "")
            return value if value != "__nested_node__" else None
        return None

    async def _append_segment_chunk(
        self,
        chunk: list[Any],
        target: list[QuoteSegment],
        group_id: str,
        settings: dict[str, Any],
        image_counter: list[int],
    ) -> None:
        if not chunk:
            return
        parsed, ignored = await self._segments(
            chunk,
            group_id,
            settings,
            image_counter,
            nested_forbidden=False,
        )
        target.extend(parsed)
        self.last_ignored_segments += ignored
        chunk.clear()

    @staticmethod
    def _raw_reply_data(event: Any) -> dict[str, Any]:
        """从 AstrBot 保留的 OneBot 原始事件中补取引用 ID 与消息序号。"""
        message_obj = getattr(event, "message_obj", None)
        raw = getattr(message_obj, "raw_message", None)
        if not isinstance(raw, Mapping):
            return {}
        message = raw.get("message")
        if isinstance(message, list):
            for item in message:
                if not isinstance(item, Mapping) or item.get("type") != "reply":
                    continue
                data = item.get("data")
                return dict(data) if isinstance(data, Mapping) else {}
        raw_text = raw.get("raw_message")
        if isinstance(raw_text, str):
            for component in _cq_components(raw_text):
                if isinstance(component, _RawReply):
                    return {
                        "id": component.message_id,
                        "seq": component.message_seq,
                    }
        return {}

    async def _get_message(
        self,
        event: Any,
        message_id: str,
        *,
        group_id: str | None = None,
        message_seq: str | None = None,
    ) -> dict[str, Any]:
        if message_id:
            attempts: Iterable[Any] = (
                message_id,
                int(message_id) if message_id.isdigit() else message_id,
            )
            for value in attempts:
                for key in ("message_id", "id"):
                    try:
                        payload = await call_onebot_action(
                            event, "get_msg", **{key: value}
                        )
                        if isinstance(payload, dict):
                            normalized = self._unwrap_payload(payload)
                            if self._payload_components(normalized):
                                return normalized
                    except Exception as exc:  # noqa: BLE001 - OneBot 实现异常类型不统一。
                        logger.debug(
                            "群典：读取引用消息失败；接口=get_msg，参数=%s，值=%s，原因=%s",
                            key,
                            value,
                            _error_reason(exc),
                        )
                        continue
        if group_id:
            # NapCat 的群历史接口按 message_seq 定位；部分旧实现把消息 ID 当作序号。
            sequence_candidates = list(
                dict.fromkeys(value for value in (message_seq, message_id) if value)
            )
            for raw_value in sequence_candidates:
                if not raw_value:
                    continue
                value: Any = int(raw_value) if str(raw_value).isdigit() else raw_value
                history_variants = (
                    {"message_seq": value, "count": 20, "reverseOrder": True},
                    {"message_seq": value, "count": 20},
                )
                for variant in history_variants:
                    try:
                        payload = await call_onebot_action(
                            event,
                            "get_group_msg_history",
                            group_id=int(group_id),
                            **variant,
                        )
                        matched = self._find_history_message(
                            payload,
                            message_id,
                            message_seq,
                            group_id=group_id,
                        )
                        if matched is not None:
                            return matched
                    except Exception as exc:  # noqa: BLE001 - NapCat 扩展异常不统一。
                        logger.debug(
                            "群典：读取群历史回复失败；消息序号=%s，倒序=%s，原因=%s",
                            value,
                            variant.get("reverseOrder", False),
                            _error_reason(exc),
                        )
        logger.warning(
            "群典：引用消息读取失败；消息ID=%s，消息序号=%s，群=%s，原因=所有读取方式均未返回可用消息",
            message_id or "无",
            message_seq or "无",
            group_id or "未知",
        )
        raise CaptureError("无法读取被引用消息，消息可能已过期")

    @classmethod
    def _find_history_message(
        cls,
        payload: Any,
        message_id: str,
        message_seq: str | None,
        *,
        group_id: str | None = None,
    ) -> dict[str, Any] | None:
        normalized = cls._unwrap_payload(payload)
        raw_messages = normalized.get("messages") or normalized.get("message")
        if isinstance(raw_messages, dict):
            raw_messages = [raw_messages]
        if not isinstance(raw_messages, list):
            return None
        for item in raw_messages:
            if not isinstance(item, dict):
                continue
            candidate = cls._unwrap_payload(item)
            if not cls._payload_components(candidate):
                continue
            candidate_group = str(candidate.get("group_id") or "")
            if group_id and candidate_group and candidate_group != str(group_id):
                continue
            identifiers = {
                str(candidate.get(key) or "")
                for key in ("message_id", "id", "message_seq", "real_id", "real_seq")
            }
            if (message_id and message_id in identifiers) or (
                message_seq and message_seq in identifiers
            ):
                return candidate
        return None

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
                        normalized = self._unwrap_payload(payload)
                        if self._forward_payload_nodes(normalized):
                            return normalized
                except Exception as exc:  # noqa: BLE001 - OneBot 实现异常类型不统一。
                    logger.debug(
                        "群典：读取合并转发失败；参数=%s，值=%s，原因=%s",
                        key,
                        value,
                        _error_reason(exc),
                    )
        logger.warning(
            "群典：合并转发读取失败；转发ID=%s，原因=所有参数变体均未返回可用节点",
            forward_id,
        )
        raise CaptureError("无法读取合并转发，消息可能已过期")

    @classmethod
    def _unwrap_payload(cls, payload: Any) -> dict[str, Any]:
        current = payload
        for _ in range(4):
            if not isinstance(current, dict):
                return {}
            nested = current.get("data")
            if not isinstance(nested, dict):
                return current
            if any(
                key in current for key in ("message", "content", "messages", "nodes")
            ):
                return current
            current = nested
        return current if isinstance(current, dict) else {}

    @staticmethod
    def _payload_components(payload: dict[str, Any]) -> list[Any]:
        raw = payload.get("message")
        if raw in (None, ""):
            raw = payload.get("content")
        if isinstance(raw, str):
            return _cq_components(raw)
        if not isinstance(raw, list):
            return []
        return [
            component
            for item in raw
            if isinstance(item, dict)
            if (component := _raw_component(item))
        ]

    @classmethod
    def _forward_payload_nodes(cls, payload: Any) -> list[Any]:
        if isinstance(payload, dict):
            payload = cls._unwrap_payload(payload)
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
        sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
        author = _as_author(
            sender.get("user_id") or sender.get("uin") or data.get("uin"),
            sender.get("card")
            or data.get("nickname")
            or data.get("name")
            or sender.get("nickname"),
            raw_user_id=(
                data.get("user_id")
                or data.get("uin")
                or sender.get("user_id")
                or sender.get("uin")
            ),
            identity_source=(
                "sender"
                if sender.get("user_id") or sender.get("uin")
                else "node_uin"
                if data.get("uin")
                else "compat_unverified"
            ),
        )
        raw_content = data.get("content") or data.get("message")
        if isinstance(raw_content, str):
            return author, _cq_components(raw_content), _utc_iso(data.get("time"))
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
            elif isinstance(
                component, (_RawForward, Comp.Forward, Comp.Node, Comp.Nodes)
            ):
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
    ) -> tuple[ReplySnapshot | None, bool]:
        replies = [item for item in chain if isinstance(item, (Comp.Reply, _RawReply))]
        if len(replies) > 1:
            raise CaptureError("消息包含多个回复来源，无法可靠本地化")
        if not replies:
            return None, False
        component = replies[0]
        message_id = (
            component.message_id
            if isinstance(component, _RawReply)
            else str(getattr(component, "id", "") or "")
        )
        source_message_seq = (
            component.message_seq
            if isinstance(component, _RawReply)
            else str(getattr(component, "seq", "") or "") or None
        )
        if not message_id and not source_message_seq:
            raise CaptureError("回复关系缺少可读取的消息 ID")
        visit_key = message_id or f"seq:{source_message_seq}"
        if visit_key in visited:
            return ReplySnapshot(
                author=AuthorSnapshot(nickname="更早的回复"),
                segments=[],
                truncated=True,
                source_message_id=message_id or None,
                source_message_seq=source_message_seq,
            ), False
        next_visited = {*visited, visit_key}
        fallback_chain = (
            list(getattr(component, "chain", None) or [])
            if isinstance(component, Comp.Reply)
            else []
        )
        incomplete = False
        try:
            payload = await self._get_message(
                event,
                message_id,
                group_id=group_id,
                message_seq=source_message_seq,
            )
            source_message_seq = (
                str(
                    payload.get("real_seq")
                    or payload.get("msg_seq")
                    or source_message_seq
                    or payload.get("message_seq")
                    or ""
                )
                or None
            )
            reply_chain = fallback_chain or self._payload_components(payload)
            sender = (
                payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
            )
            author = _as_author(
                sender.get("user_id") or payload.get("user_id"),
                sender.get("card") or sender.get("nickname"),
            )
        except CaptureError as exc:
            logger.warning(
                "群典：引用源无法追溯，已标记引用丢失；消息ID=%s，消息序号=%s，层级=%s，原因=%s",
                message_id or "无",
                source_message_seq or "无",
                depth,
                _error_reason(exc),
            )
            if not fallback_chain:
                # 当前正文仍可保存时，只丢弃无法追溯的更早引用关系。
                return None, True
            reply_chain = fallback_chain
            incomplete = True
            author = _as_author(
                getattr(component, "sender_id", None),
                getattr(component, "sender_nickname", None),
            )
        author = await self._resolve_author(event, group_id, author)
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
        reply_missing = False
        if nested_exists and not truncated:
            nested, reply_missing = await self._capture_reply_from_chain(
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
            source_message_id=message_id,
            source_message_seq=source_message_seq,
            incomplete=incomplete,
            reply_missing=reply_missing,
        ), False

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
        _, is_bot = await self._member_info(event, group_id, author.user_id)
        if is_bot:
            raise CaptureError("配置不允许收录机器人发送的消息")

    async def _resolve_author(
        self,
        event: Any,
        group_id: str,
        author: AuthorSnapshot,
    ) -> AuthorSnapshot:
        if author.identity_source == "compat_unverified" and author.raw_user_id:
            # data.user_id 在部分实现中并非节点作者；仅在群成员 API 能验证时采用。
            name, _ = await self._member_info(event, group_id, author.raw_user_id)
            if name:
                author.user_id = author.raw_user_id
                author.identity_source = "compat_validated"
                author.nickname = name
            return author
        if author.user_id is None:
            return author
        name, _ = await self._member_info(event, group_id, author.user_id)
        if name:
            author.nickname = name
        return author

    async def _member_info(
        self,
        event: Any,
        group_id: str,
        user_id: str,
    ) -> tuple[str | None, bool | None]:
        cache_key = (group_id, user_id)
        cached = self._member_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1], cached[2]
        name: str | None = None
        is_bot: bool | None = None
        try:
            payload = await call_onebot_action(
                event,
                "get_group_member_info",
                group_id=int(group_id),
                user_id=int(user_id),
                no_cache=False,
            )
            payload = self._unwrap_payload(payload)
            name = str(payload.get("card") or payload.get("nickname") or "").strip()
            name = name or None
            if "is_robot" in payload or "is_bot" in payload:
                is_bot = bool(payload.get("is_robot") or payload.get("is_bot"))
        except Exception as exc:  # noqa: BLE001 - OneBot 群成员扩展字段不统一。
            logger.debug(
                "群典：读取群成员信息失败；群=%s，用户=%s，原因=%s",
                group_id,
                user_id,
                _error_reason(exc),
            )
        self._member_cache[cache_key] = (time.monotonic() + 300, name, is_bot)
        return name, is_bot
