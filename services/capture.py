"""收录引用消息、合并转发与回复快照，转换为与适配器解耦的记录模型。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
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
from ..utils.timefmt import error_reason, now_iso, utc_iso
from .media import MediaInfo, MediaStore
from .members import MemberService
from .onebot import (
    FORWARD_TYPES,
    NESTED_NODE_SENTINEL,
    REPLY_TYPES,
    RawForward,
    RawReply,
    RawSticker,
    call_action,
    forward_payload_nodes,
    payload_components,
    raw_reply_data,
    segment_components,
    unwrap,
)

# (新增字节数, 容量上限) → 是否有足够空间；实现方负责按需清理头像缓存。
ReserveSpace = Callable[[int, int], Awaitable[bool]]


class CaptureError(RuntimeError):
    """无法完整、可靠地收录本次消息。"""


@dataclass(slots=True)
class CaptureSession:
    """一次收录的累计状态：限额计数、新写入的媒体与忽略的消息段。"""

    settings: dict[str, Any]
    media: dict[str, MediaInfo] = field(default_factory=dict)
    created: list[MediaInfo] = field(default_factory=list)
    images: int = 0
    nodes: int = 0
    ignored_segments: int = 0

    @property
    def new_bytes(self) -> int:
        return sum(info.size for info in self.created)


def _as_author(
    user_id: Any,
    nickname: Any,
    *,
    raw_user_id: Any = None,
    identity_source: str | None = None,
) -> AuthorSnapshot:
    normalized_id = str(user_id).strip() if user_id not in (None, "", 0, "0") else None
    normalized_raw = (
        str(raw_user_id).strip()
        if raw_user_id not in (None, "", 0, "0")
        else normalized_id
    )
    return AuthorSnapshot(
        user_id=normalized_id,
        nickname=str(nickname or normalized_id or "未知用户").strip(),
        raw_user_id=normalized_raw,
        identity_source=identity_source or ("onebot" if normalized_id else "unknown"),
    )


def _payload_author(payload: dict[str, Any]) -> AuthorSnapshot:
    sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
    return _as_author(
        sender.get("user_id") or payload.get("user_id"),
        sender.get("card") or sender.get("nickname"),
    )


def _text_length(segments: Iterable[QuoteSegment]) -> int:
    return sum(
        len(segment.text or "") for segment in segments if segment.type == "text"
    )


def _reply_text_length(reply: ReplySnapshot | None) -> int:
    return sum(_text_length(item.segments) for item in reply.chain()) if reply else 0


def _check_text_limit(segments: list[QuoteSegment], limit: int) -> int:
    length = _text_length(segments)
    if length > limit:
        raise CaptureError("文字长度超过配置上限")
    return length


def _forward_id(component: Any) -> str | None:
    """当前分支的转发 ID，供祖先链循环检测。"""
    if isinstance(component, RawForward):
        return component.forward_id or None
    if isinstance(component, Comp.Forward):
        value = str(getattr(component, "id", "") or "")
        return value if value != NESTED_NODE_SENTINEL else None
    return None


def _parse_forward_node(raw_node: Any) -> tuple[AuthorSnapshot, list[Any], str | None]:
    if isinstance(raw_node, Comp.Node):
        return (
            _as_author(raw_node.uin, raw_node.name),
            list(raw_node.content),
            utc_iso(raw_node.time),
        )
    if not isinstance(raw_node, dict):
        raise CaptureError("合并转发节点格式无效")
    data = raw_node.get("data") if isinstance(raw_node.get("data"), dict) else raw_node
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
    if not isinstance(raw_content, (str, list)):
        raise CaptureError("合并转发节点内容格式无效")
    return author, segment_components(raw_content), utc_iso(data.get("time"))


def _find_history_message(
    payload: Any, message_id: str, message_seq: str | None, group_id: str
) -> dict[str, Any] | None:
    normalized = unwrap(payload)
    if not isinstance(normalized, dict):
        return None
    raw_messages = normalized.get("messages") or normalized.get("message")
    if isinstance(raw_messages, dict):
        raw_messages = [raw_messages]
    if not isinstance(raw_messages, list):
        return None
    for item in raw_messages:
        candidate = unwrap(item)
        if not isinstance(candidate, dict) or not payload_components(candidate):
            continue
        candidate_group = str(candidate.get("group_id") or "")
        if candidate_group and candidate_group != str(group_id):
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


class QuoteCapture:
    """把 OneBot 消息转换为群典记录；媒体先写入内容寻址文件，由调用方登记引用。"""

    def __init__(
        self,
        media: MediaStore,
        members: MemberService,
        reserve_space: ReserveSpace,
    ):
        self.media = media
        self.members = members
        self.reserve_space = reserve_space

    async def extract(self, event: Any, session: CaptureSession) -> QuoteRecord:
        """从唯一引用或合并转发来源构造记录，并计算内容哈希。"""
        messages = list(event.get_messages())
        replies = [item for item in messages if isinstance(item, REPLY_TYPES)]
        if not replies and (raw := raw_reply_data(event)):
            replies.append(
                RawReply(
                    message_id=str(raw.get("id") or raw.get("message_id") or ""),
                    message_seq=str(raw.get("seq") or raw.get("message_seq") or "")
                    or None,
                )
            )
        forwards = [item for item in messages if isinstance(item, FORWARD_TYPES)]
        if len(replies) + len(forwards) != 1:
            raise CaptureError("一次只能添加一个引用或合并转发来源")
        group_id = str(event.get_group_id())
        operator = _as_author(event.get_sender_id(), event.get_sender_name())
        if replies:
            record = await self._from_reply(
                event, replies[0], group_id, operator, session
            )
        else:
            record = await self._from_forward(
                event, forwards[0], group_id, operator, session
            )
        record.content_hash = calculate_record_hash(record)
        return record

    # 来源 -------------------------------------------------------------

    async def _from_reply(
        self,
        event: Any,
        reply: Any,
        group_id: str,
        operator: AuthorSnapshot,
        session: CaptureSession,
    ) -> QuoteRecord:
        settings = session.settings
        chain = list(getattr(reply, "chain", None) or [])
        raw = raw_reply_data(event)
        author = _as_author(
            getattr(reply, "sender_id", None), getattr(reply, "sender_nickname", None)
        )
        sent_at = utc_iso(getattr(reply, "time", None))
        source_id = (
            str(
                getattr(reply, "message_id", None)
                or getattr(reply, "id", "")
                or raw.get("id")
                or raw.get("message_id")
                or ""
            )
            or None
        )
        source_seq = (
            str(
                getattr(reply, "message_seq", None)
                or getattr(reply, "seq", "")
                or raw.get("seq")
                or raw.get("message_seq")
                or ""
            )
            or None
        )
        # AstrBot 已展开 Reply.chain 时直接采用，避免重复请求易失消息 ID。
        if not chain and (source_id or source_seq):
            payload = await self._get_message(
                event, source_id or "", group_id=group_id, message_seq=source_seq
            )
            chain = payload_components(payload) or chain
            author = _payload_author(payload)
            sent_at = utc_iso(payload.get("time")) or sent_at
        if not chain:
            raise CaptureError("无法读取被引用消息，消息可能已过期")
        author = await self._resolve_author(event, group_id, author)
        nested_sources = [item for item in chain if isinstance(item, FORWARD_TYPES)]
        if len(nested_sources) > 1:
            raise CaptureError("引用消息中包含多个合并转发，无法确定唯一来源")
        if nested_sources:
            return await self._from_forward(
                event,
                nested_sources[0],
                group_id,
                operator,
                session,
                source_message_id=source_id,
            )
        await self._check_author_allowed(event, group_id, author, settings)
        reply_snapshot, reply_missing = await self._capture_reply(
            event, chain, group_id, session, visited={source_id} if source_id else set()
        )
        segments = await self._segments(chain, group_id, session, nested_forbidden=True)
        if not segments and reply_snapshot is None:
            raise CaptureError("引用消息没有可保存的文字、图片或表情")
        text_length = _check_text_limit(segments, settings["max_text_chars"])
        if (
            text_length + _reply_text_length(reply_snapshot)
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
            recorded_at=now_iso(),
            recorded_by=operator,
            identity_incomplete=author.user_id is None,
            reply=reply_snapshot,
            reply_missing=reply_missing,
        )

    async def _from_forward(
        self,
        event: Any,
        component: Any,
        group_id: str,
        operator: AuthorSnapshot,
        session: CaptureSession,
        source_message_id: str | None = None,
    ) -> QuoteRecord:
        raw_nodes, forward_id = await self._load_forward(event, component)
        nodes, total_text = await self._capture_nodes(
            event,
            raw_nodes,
            group_id,
            session,
            depth=1,
            ancestors={forward_id} if forward_id else set(),
        )
        if total_text > session.settings["max_forward_text_chars"]:
            raise CaptureError("合并转发累计文字长度超过配置上限")
        record = QuoteRecord(
            id=uuid.uuid4().hex,
            type="forward",
            group_id=group_id,
            author=None,
            nodes=nodes,
            source_message_id=source_message_id,
            recorded_at=now_iso(),
            recorded_by=operator,
            source_forward_id=forward_id,
        )
        record.identity_incomplete = record.has_unknown_authors()
        return record

    async def _load_forward(
        self, event: Any, component: Any
    ) -> tuple[list[Any], str | None]:
        if isinstance(component, RawForward):
            forward_id = component.forward_id or None
            # NapCat 已把完整子节点放在段内时优先使用，避免再次查询后被拍平。
            if component.content:
                raw_nodes = component.content
            elif forward_id:
                raw_nodes = forward_payload_nodes(
                    await self._get_forward(event, forward_id)
                )
            else:
                raise CaptureError("合并转发既没有内联内容，也没有可回退读取的消息 ID")
        elif isinstance(component, Comp.Forward):
            forward_id = str(getattr(component, "id", "") or "")
            if not forward_id or forward_id == NESTED_NODE_SENTINEL:
                raise CaptureError("合并转发缺少可读取的消息 ID")
            raw_nodes = forward_payload_nodes(
                await self._get_forward(event, forward_id)
            )
        elif isinstance(component, Comp.Nodes):
            raw_nodes, forward_id = list(component.nodes), None
        elif isinstance(component, Comp.Node):
            raw_nodes, forward_id = [component], None
        else:
            raise CaptureError("合并转发来源格式无效")
        if not raw_nodes:
            raise CaptureError("合并转发内容为空或已过期")
        return raw_nodes, forward_id

    async def _capture_nodes(
        self,
        event: Any,
        raw_nodes: list[Any],
        group_id: str,
        session: CaptureSession,
        *,
        depth: int,
        ancestors: set[str],
    ) -> tuple[list[ForwardNode], int]:
        settings = session.settings
        # 节点上限按整棵树累计，而不是每一层分别计算。
        session.nodes += len(raw_nodes)
        if session.nodes > settings["max_forward_nodes"]:
            raise CaptureError("合并转发节点数超过配置上限")
        nodes: list[ForwardNode] = []
        total_text = 0
        for raw_node in raw_nodes:
            author, chain, sent_at = _parse_forward_node(raw_node)
            author = await self._resolve_author(event, group_id, author)
            await self._check_author_allowed(event, group_id, author, settings)
            reply_snapshot, reply_missing = await self._capture_reply(
                event, chain, group_id, session, visited=set()
            )
            segments: list[QuoteSegment] = []
            nested_forwards: list[NestedForward] = []
            ordinary: list[Any] = []
            for component in chain:
                if not isinstance(component, FORWARD_TYPES):
                    ordinary.append(component)
                    continue
                segments.extend(await self._segments(ordinary, group_id, session))
                ordinary = []
                if depth >= settings["max_nested_forward_depth"]:
                    raise CaptureError(
                        f"嵌套合并转发深度超过配置上限 {settings['max_nested_forward_depth']}"
                    )
                child_id = _forward_id(component)
                if child_id and child_id in ancestors:
                    raise CaptureError(f"第 {depth + 1} 层嵌套合并转发存在循环引用")
                try:
                    child_raw, child_id = await self._load_forward(event, component)
                    child_nodes, child_text = await self._capture_nodes(
                        event,
                        child_raw,
                        group_id,
                        session,
                        depth=depth + 1,
                        ancestors={*ancestors, child_id} if child_id else ancestors,
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
            segments.extend(await self._segments(ordinary, group_id, session))
            if not segments and reply_snapshot is None and not nested_forwards:
                raise CaptureError("合并转发包含无法保存的空节点")
            total_text += _check_text_limit(segments, settings["max_text_chars"])
            total_text += _reply_text_length(reply_snapshot)
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

    async def _capture_reply(
        self,
        event: Any,
        chain: list[Any],
        group_id: str,
        session: CaptureSession,
        *,
        visited: set[str],
        depth: int = 1,
    ) -> tuple[ReplySnapshot | None, bool]:
        settings = session.settings
        replies = [item for item in chain if isinstance(item, REPLY_TYPES)]
        if len(replies) > 1:
            raise CaptureError("消息包含多个回复来源，无法可靠本地化")
        if not replies:
            return None, False
        component = replies[0]
        if isinstance(component, RawReply):
            message_id, message_seq = component.message_id, component.message_seq
        else:
            message_id = str(getattr(component, "id", "") or "")
            message_seq = str(getattr(component, "seq", "") or "") or None
        if not message_id and not message_seq:
            raise CaptureError("回复关系缺少可读取的消息 ID")
        visit_key = message_id or f"seq:{message_seq}"
        if visit_key in visited:
            return ReplySnapshot(
                author=AuthorSnapshot(nickname="更早的回复"),
                segments=[],
                truncated=True,
                source_message_id=message_id or None,
                source_message_seq=message_seq,
            ), False
        fallback_chain = (
            list(getattr(component, "chain", None) or [])
            if isinstance(component, Comp.Reply)
            else []
        )
        incomplete = False
        try:
            payload = await self._get_message(
                event, message_id, group_id=group_id, message_seq=message_seq
            )
            message_seq = (
                str(
                    payload.get("real_seq")
                    or payload.get("msg_seq")
                    or message_seq
                    or payload.get("message_seq")
                    or ""
                )
                or None
            )
            reply_chain = fallback_chain or payload_components(payload)
            author = _payload_author(payload)
        except CaptureError as exc:
            logger.warning(
                "群典：引用源无法追溯，已标记引用丢失；消息ID=%s，消息序号=%s，层级=%s，原因=%s",
                message_id or "无",
                message_seq or "无",
                depth,
                error_reason(exc),
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
        segments = await self._segments(
            reply_chain, group_id, session, nested_forbidden=True
        )
        nested_exists = any(isinstance(item, REPLY_TYPES) for item in reply_chain)
        truncated = nested_exists and depth >= settings["max_reply_depth"]
        nested, reply_missing = None, False
        if nested_exists and not truncated:
            nested, reply_missing = await self._capture_reply(
                event,
                reply_chain,
                group_id,
                session,
                visited={*visited, visit_key},
                depth=depth + 1,
            )
        if not segments and nested is None and not truncated:
            raise CaptureError("被回复消息没有可保存的文字、图片或表情")
        _check_text_limit(segments, settings["max_text_chars"])
        return ReplySnapshot(
            author=author,
            segments=segments,
            reply=nested,
            truncated=truncated,
            source_message_id=message_id,
            source_message_seq=message_seq,
            incomplete=incomplete,
            reply_missing=reply_missing,
        ), False

    # 消息段 -----------------------------------------------------------

    async def _segments(
        self,
        chain: list[Any],
        group_id: str,
        session: CaptureSession,
        *,
        nested_forbidden: bool = False,
    ) -> list[QuoteSegment]:
        result: list[QuoteSegment] = []

        def append_text(value: str) -> None:
            if result and result[-1].type == "text":
                result[-1].text = (result[-1].text or "") + value
            else:
                result.append(QuoteSegment(type="text", text=value))

        for component in chain:
            if isinstance(component, Comp.Plain):
                if component.text:
                    append_text(str(component.text))
            elif isinstance(component, Comp.At):
                name = str(getattr(component, "name", "") or "").strip()
                target = str(getattr(component, "qq", "") or "")
                append_text(f"@{name or ('全体成员' if target == 'all' else target)}")
            elif isinstance(component, Comp.Image):
                result.append(await self._save_media(component, session, label="图片"))
            elif isinstance(component, RawSticker):
                sticker = await self._save_media(
                    Comp.Image(file=component.file, url=component.url),
                    session,
                    label="贴纸",
                )
                sticker.type = "sticker"
                sticker.emoji_package_id = component.emoji_package_id or None
                sticker.emoji_id = component.emoji_id or None
                sticker.key = component.key or None
                sticker.summary = component.summary or None
                result.append(sticker)
            elif isinstance(component, Comp.Face):
                face_id = str(getattr(component, "id", "") or "").strip()
                if not face_id:
                    raise CaptureError("QQ 表情缺少表情 ID")
                result.append(QuoteSegment(type="face", face_id=face_id))
            elif isinstance(component, FORWARD_TYPES):
                if nested_forbidden:
                    raise CaptureError("暂不支持嵌套合并转发")
            elif isinstance(component, REPLY_TYPES):
                continue
            else:
                session.ignored_segments += 1
        return result

    async def _save_media(
        self, image: Comp.Image, session: CaptureSession, *, label: str
    ) -> QuoteSegment:
        settings = session.settings
        session.images += 1
        if session.images > settings["max_images_per_record"]:
            raise CaptureError(
                "图片数量超过配置上限"
                if label == "图片"
                else "图片与贴纸数量超过配置上限"
            )
        try:
            path = await image.convert_to_file_path()
            data = await asyncio.to_thread(Path(path).read_bytes)
            if len(data) > settings["max_image_mb"] * 1024 * 1024:
                raise ValueError("图片超过单张大小限制")
            limit = settings["max_media_mb"] * 1024 * 1024
            if not await self.reserve_space(session.new_bytes + len(data), limit):
                raise ValueError("全局媒体存储容量已满")
            info, created = await asyncio.to_thread(self.media.write, data)
        except (OSError, ValueError) as exc:
            raise CaptureError(f"{label}保存失败: {exc}") from exc
        except Exception as exc:  # 宿主下载媒体的异常类型不统一，统一归为收录失败。
            raise CaptureError(f"{label}保存失败: {error_reason(exc)}") from exc
        session.media[info.sha256] = info
        if created:
            session.created.append(info)
        return QuoteSegment(
            type="image", sha256=info.sha256, mime=info.mime, size=info.size
        )

    # 作者 -------------------------------------------------------------

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
        if (await self.members.profile(event, group_id, author.user_id)).is_bot:
            raise CaptureError("配置不允许收录机器人发送的消息")

    async def _resolve_author(
        self, event: Any, group_id: str, author: AuthorSnapshot
    ) -> AuthorSnapshot:
        if author.identity_source == "compat_unverified" and author.raw_user_id:
            # data.user_id 在部分实现中并非节点作者；仅在群成员接口能验证时采用。
            name = (
                await self.members.profile(event, group_id, author.raw_user_id)
            ).name
            if name:
                author.user_id = author.raw_user_id
                author.identity_source = "compat_validated"
                author.nickname = name
            return author
        if author.user_id is None:
            return author
        name = (await self.members.profile(event, group_id, author.user_id)).name
        if name:
            author.nickname = name
        return author

    # OneBot 读取 ------------------------------------------------------

    async def _get_message(
        self,
        event: Any,
        message_id: str,
        *,
        group_id: str,
        message_seq: str | None = None,
    ) -> dict[str, Any]:
        if message_id:
            for value in dict.fromkeys(
                (message_id, int(message_id) if message_id.isdigit() else message_id)
            ):
                for key in ("message_id", "id"):
                    try:
                        payload = unwrap(
                            await call_action(event, "get_msg", **{key: value})
                        )
                        if isinstance(payload, dict) and payload_components(payload):
                            return payload
                    except Exception as exc:  # noqa: BLE001 - OneBot 实现异常类型不统一。
                        logger.debug(
                            "群典：读取引用消息失败；参数=%s，值=%s，原因=%s",
                            key,
                            value,
                            error_reason(exc),
                        )
        # NapCat 的群历史接口按 message_seq 定位；部分旧实现把消息 ID 当作序号。
        for raw_value in dict.fromkeys(
            value for value in (message_seq, message_id) if value
        ):
            value: Any = int(raw_value) if str(raw_value).isdigit() else raw_value
            for variant in (
                {"message_seq": value, "count": 20, "reverseOrder": True},
                {"message_seq": value, "count": 20},
            ):
                try:
                    payload = await call_action(
                        event,
                        "get_group_msg_history",
                        group_id=int(group_id),
                        **variant,
                    )
                    matched = _find_history_message(
                        payload, message_id, message_seq, group_id
                    )
                    if matched is not None:
                        return matched
                except Exception as exc:  # noqa: BLE001 - NapCat 扩展异常不统一。
                    logger.debug(
                        "群典：读取群历史回复失败；消息序号=%s，倒序=%s，原因=%s",
                        value,
                        variant.get("reverseOrder", False),
                        error_reason(exc),
                    )
        logger.warning(
            "群典：引用消息读取失败；消息ID=%s，消息序号=%s，群=%s，原因=所有读取方式均未返回可用消息",
            message_id or "无",
            message_seq or "无",
            group_id,
        )
        raise CaptureError("无法读取被引用消息，消息可能已过期")

    async def _get_forward(self, event: Any, forward_id: str) -> dict[str, Any]:
        """兼容 get_forward_msg 参数名和 ID 类型的差异。"""
        for value in dict.fromkeys(
            (forward_id, int(forward_id) if forward_id.isdigit() else forward_id)
        ):
            for key in ("message_id", "id"):
                try:
                    payload = unwrap(
                        await call_action(event, "get_forward_msg", **{key: value})
                    )
                    if forward_payload_nodes(payload):
                        return payload
                except Exception as exc:  # noqa: BLE001 - OneBot 实现异常类型不统一。
                    logger.debug(
                        "群典：读取合并转发失败；参数=%s，值=%s，原因=%s",
                        key,
                        value,
                        error_reason(exc),
                    )
        logger.warning(
            "群典：合并转发读取失败；转发ID=%s，原因=所有参数变体均未返回可用节点",
            forward_id,
        )
        raise CaptureError("无法读取合并转发，消息可能已过期")
