"""群典记录的数据结构与序列化逻辑。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


@dataclass(slots=True)
class AuthorSnapshot:
    """保存收录时可稳定取得的发送者信息。"""

    platform: str = "aiocqhttp"
    user_id: str | None = None
    nickname: str = ""
    # raw_user_id 保留 OneBot 原始字段，便于在不污染内容哈希的前提下追溯错误映射。
    raw_user_id: str | None = None
    identity_source: str = "legacy"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AuthorSnapshot:
        user_id = value.get("user_id")
        raw_user_id = value.get("raw_user_id")
        return cls(
            platform=str(value.get("platform") or "aiocqhttp"),
            user_id=str(user_id) if user_id not in (None, "") else None,
            nickname=str(value.get("nickname") or ""),
            raw_user_id=(str(raw_user_id) if raw_user_id not in (None, "") else None),
            identity_source=str(value.get("identity_source") or "legacy"),
        )


@dataclass(slots=True)
class QuoteSegment:
    """插件可持久化并重放的消息段。"""

    type: Literal["text", "image", "face", "sticker"]
    text: str | None = None
    path: str | None = None
    sha256: str | None = None
    mime: str | None = None
    size: int | None = None
    face_id: str | None = None
    emoji_package_id: str | None = None
    emoji_id: str | None = None
    key: str | None = None
    summary: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> QuoteSegment:
        segment_type = str(value.get("type") or "")
        if segment_type not in {"text", "image", "face", "sticker"}:
            raise ValueError(f"不支持的消息段类型: {segment_type}")
        return cls(
            type=segment_type,
            text=value.get("text"),
            path=value.get("path"),
            sha256=value.get("sha256"),
            mime=value.get("mime"),
            size=value.get("size"),
            face_id=(
                str(value["face_id"])
                if value.get("face_id") not in (None, "")
                else None
            ),
            emoji_package_id=(
                str(value["emoji_package_id"])
                if value.get("emoji_package_id") not in (None, "")
                else None
            ),
            emoji_id=(
                str(value["emoji_id"])
                if value.get("emoji_id") not in (None, "")
                else None
            ),
            key=str(value["key"]) if value.get("key") not in (None, "") else None,
            summary=(
                str(value["summary"])
                if value.get("summary") not in (None, "")
                else None
            ),
        )


@dataclass(slots=True)
class ReplySnapshot:
    """不依赖易失 OneBot 消息 ID 的本地回复快照。"""

    author: AuthorSnapshot
    segments: list[QuoteSegment]
    reply: ReplySnapshot | None = None
    truncated: bool = False
    source_message_id: str | None = None
    source_message_seq: str | None = None
    incomplete: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReplySnapshot:
        nested = value.get("reply")
        return cls(
            author=AuthorSnapshot.from_dict(value.get("author") or {}),
            segments=[
                QuoteSegment.from_dict(item)
                for item in value.get("segments", [])
                if isinstance(item, dict)
            ],
            reply=cls.from_dict(nested) if isinstance(nested, dict) else None,
            truncated=bool(value.get("truncated", False)),
            source_message_id=(
                str(value["source_message_id"])
                if value.get("source_message_id") not in (None, "")
                else None
            ),
            source_message_seq=(
                str(value["source_message_seq"])
                if value.get("source_message_seq") not in (None, "", 0, "0")
                else None
            ),
            incomplete=bool(value.get("incomplete", False)),
        )


@dataclass(slots=True)
class NestedForward:
    """节点正文中按原位置保存的子合并转发。"""

    position: int
    nodes: list[ForwardNode]
    source_forward_id: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> NestedForward:
        return cls(
            position=max(0, int(value.get("position", 0))),
            nodes=[
                ForwardNode.from_dict(item)
                for item in value.get("nodes", [])
                if isinstance(item, dict)
            ],
            source_forward_id=(
                str(value["source_forward_id"])
                if value.get("source_forward_id") not in (None, "")
                else None
            ),
        )


@dataclass(slots=True)
class ForwardNode:
    """合并转发中的一个原始节点。"""

    author: AuthorSnapshot
    segments: list[QuoteSegment]
    source_sent_at: str | None = None
    reply: ReplySnapshot | None = None
    nested_forwards: list[NestedForward] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ForwardNode:
        return cls(
            author=AuthorSnapshot.from_dict(value.get("author") or {}),
            segments=[
                QuoteSegment.from_dict(item)
                for item in value.get("segments", [])
                if isinstance(item, dict)
            ],
            source_sent_at=value.get("source_sent_at"),
            reply=(
                ReplySnapshot.from_dict(value["reply"])
                if isinstance(value.get("reply"), dict)
                else None
            ),
            nested_forwards=[
                NestedForward.from_dict(item)
                for item in value.get("nested_forwards", [])
                if isinstance(item, dict)
            ],
        )


@dataclass(slots=True)
class QuoteRecord:
    """一条普通群典或一个完整合并转发合集。"""

    id: str
    type: Literal["message", "forward"]
    group_id: str
    author: AuthorSnapshot | None
    segments: list[QuoteSegment] = field(default_factory=list)
    nodes: list[ForwardNode] = field(default_factory=list)
    content_hash: str = ""
    source_message_id: str | None = None
    source_sent_at: str | None = None
    recorded_at: str = ""
    recorded_by: AuthorSnapshot = field(default_factory=AuthorSnapshot)
    identity_incomplete: bool = False
    reply: ReplySnapshot | None = None
    source_forward_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为可直接写入 JSON 的字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> QuoteRecord:
        record_type = str(value.get("type") or "")
        if record_type not in {"message", "forward"}:
            raise ValueError(f"不支持的群典类型: {record_type}")
        author_value = value.get("author")
        return cls(
            id=str(value["id"]),
            type=record_type,
            group_id=str(value["group_id"]),
            author=(
                AuthorSnapshot.from_dict(author_value)
                if isinstance(author_value, dict)
                else None
            ),
            segments=[
                QuoteSegment.from_dict(item)
                for item in value.get("segments", [])
                if isinstance(item, dict)
            ],
            nodes=[
                ForwardNode.from_dict(item)
                for item in value.get("nodes", [])
                if isinstance(item, dict)
            ],
            content_hash=str(value.get("content_hash") or ""),
            source_message_id=value.get("source_message_id"),
            source_sent_at=value.get("source_sent_at"),
            recorded_at=str(value.get("recorded_at") or ""),
            recorded_by=AuthorSnapshot.from_dict(value.get("recorded_by") or {}),
            identity_incomplete=bool(value.get("identity_incomplete", False)),
            reply=(
                ReplySnapshot.from_dict(value["reply"])
                if isinstance(value.get("reply"), dict)
                else None
            ),
            source_forward_id=(
                str(value["source_forward_id"])
                if value.get("source_forward_id") not in (None, "")
                else None
            ),
        )

    def image_segments(self) -> list[QuoteSegment]:
        """返回正文与回复快照中的全部本地媒体段。"""
        result = [
            segment for segment in self.segments if segment.type in {"image", "sticker"}
        ]
        result.extend(self._reply_media(self.reply))
        for node in self.nodes:
            result.extend(self._node_media(node))
        return result

    @classmethod
    def _node_media(cls, node: ForwardNode) -> list[QuoteSegment]:
        result = [
            segment for segment in node.segments if segment.type in {"image", "sticker"}
        ]
        result.extend(cls._reply_media(node.reply))
        for nested in node.nested_forwards:
            for child in nested.nodes:
                result.extend(cls._node_media(child))
        return result

    @classmethod
    def _reply_media(cls, reply: ReplySnapshot | None) -> list[QuoteSegment]:
        if reply is None:
            return []
        return [
            segment
            for segment in reply.segments
            if segment.type in {"image", "sticker"}
        ] + cls._reply_media(reply.reply)

    def searchable_text(self) -> str:
        """返回仅由正文组成的删除搜索文本。"""
        parts = [
            segment.text or "" for segment in self.segments if segment.type == "text"
        ]
        parts.extend(self._reply_text(self.reply))
        for node in self.nodes:
            parts.extend(self._node_text(node))
        return "\n".join(part for part in parts if part)

    def authored_text(self, user_id: str) -> str:
        """返回指定 QQ 用户本人在记录中发送的全部文字。"""
        parts: list[str] = []
        if self.type == "message":
            if not self.author or self.author.user_id != user_id:
                return ""
            parts.extend(self._segment_text(self.segments))
            self._collect_authored_reply_text(self.reply, user_id, parts)
        else:
            for node in self.nodes:
                self._collect_authored_node_text(node, user_id, parts)
        return "\n".join(part for part in parts if part)

    @staticmethod
    def _segment_text(segments: list[QuoteSegment]) -> list[str]:
        return [
            segment.text or ""
            for segment in segments
            if segment.type == "text" and segment.text
        ]

    @classmethod
    def _collect_authored_reply_text(
        cls,
        reply: ReplySnapshot | None,
        user_id: str,
        parts: list[str],
    ) -> None:
        if reply is None:
            return
        if reply.author.user_id == user_id:
            parts.extend(cls._segment_text(reply.segments))
        cls._collect_authored_reply_text(reply.reply, user_id, parts)

    @classmethod
    def _collect_authored_node_text(
        cls,
        node: ForwardNode,
        user_id: str,
        parts: list[str],
    ) -> None:
        if node.author.user_id == user_id:
            parts.extend(cls._segment_text(node.segments))
        cls._collect_authored_reply_text(node.reply, user_id, parts)
        for nested in node.nested_forwards:
            for child in nested.nodes:
                cls._collect_authored_node_text(child, user_id, parts)

    @classmethod
    def _node_text(cls, node: ForwardNode) -> list[str]:
        result = [
            segment.text or "" for segment in node.segments if segment.type == "text"
        ]
        result.extend(cls._reply_text(node.reply))
        for nested in node.nested_forwards:
            for child in nested.nodes:
                result.extend(cls._node_text(child))
        return result

    @classmethod
    def _reply_text(cls, reply: ReplySnapshot | None) -> list[str]:
        if reply is None:
            return []
        return [
            segment.text or "" for segment in reply.segments if segment.type == "text"
        ] + cls._reply_text(reply.reply)

    def has_native_segments(self) -> bool:
        """返回记录是否包含不适合 CSS 卡片表达的原生消息段。"""
        return (
            any(segment.type in {"face", "sticker"} for segment in self._all_segments())
            or self.reply is not None
            or any(self._node_has_reply(node) for node in self.nodes)
        )

    @classmethod
    def _node_has_reply(cls, node: ForwardNode) -> bool:
        return node.reply is not None or any(
            cls._node_has_reply(child)
            for nested in node.nested_forwards
            for child in nested.nodes
        )

    def has_stickers(self) -> bool:
        """判断正文或回复快照中是否包含商城表情。"""
        return any(segment.type == "sticker" for segment in self._all_segments())

    def has_replies(self) -> bool:
        """判断正文或任意递归转发节点是否包含回复快照。"""
        return self.reply is not None or any(
            self._node_has_reply(node) for node in self.nodes
        )

    def has_nested_forwards(self) -> bool:
        """判断记录是否包含任意层级的子合并转发。"""
        return any(self._node_has_nested(node) for node in self.nodes)

    @classmethod
    def _node_has_nested(cls, node: ForwardNode) -> bool:
        return bool(node.nested_forwards) or any(
            cls._node_has_nested(child)
            for nested in node.nested_forwards
            for child in nested.nodes
        )

    def _all_segments(self) -> list[QuoteSegment]:
        result = list(self.segments)
        result.extend(self._reply_segments(self.reply))
        for node in self.nodes:
            result.extend(self._node_segments(node))
        return result

    @classmethod
    def _node_segments(cls, node: ForwardNode) -> list[QuoteSegment]:
        result = list(node.segments) + cls._reply_segments(node.reply)
        for nested in node.nested_forwards:
            for child in nested.nodes:
                result.extend(cls._node_segments(child))
        return result

    @classmethod
    def _reply_segments(cls, reply: ReplySnapshot | None) -> list[QuoteSegment]:
        if reply is None:
            return []
        return list(reply.segments) + cls._reply_segments(reply.reply)

    def involves_user(self, user_id: str) -> bool:
        """判断用户是否是普通消息作者或聊天记录中的任一节点作者。"""
        if self.type == "message":
            return bool(self.author and self.author.user_id == user_id)
        return any(self._node_involves_user(node, user_id) for node in self.nodes)

    @classmethod
    def _node_involves_user(cls, node: ForwardNode, user_id: str) -> bool:
        return node.author.user_id == user_id or any(
            cls._node_involves_user(child, user_id)
            for nested in node.nested_forwards
            for child in nested.nodes
        )

    def personal_owner_id(self) -> str | None:
        """返回可安全归入个人随机池的 QQ 号。"""
        if self.type == "message":
            return self.author.user_id if self.author else None
        ids = {
            author_id
            for node in self.nodes
            for author_id in self._node_author_ids(node)
        }
        if len(ids) == 1 and None not in ids:
            return next(iter(ids))
        return None

    @classmethod
    def _node_author_ids(cls, node: ForwardNode) -> list[str | None]:
        result = [node.author.user_id]
        for nested in node.nested_forwards:
            for child in nested.nodes:
                result.extend(cls._node_author_ids(child))
        return result
