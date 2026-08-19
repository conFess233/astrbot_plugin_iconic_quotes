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

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> AuthorSnapshot:
        user_id = value.get("user_id")
        return cls(
            platform=str(value.get("platform") or "aiocqhttp"),
            user_id=str(user_id) if user_id not in (None, "") else None,
            nickname=str(value.get("nickname") or ""),
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
        )


@dataclass(slots=True)
class ForwardNode:
    """合并转发中的一个原始节点。"""

    author: AuthorSnapshot
    segments: list[QuoteSegment]
    source_sent_at: str | None = None
    reply: ReplySnapshot | None = None

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
        )

    def image_segments(self) -> list[QuoteSegment]:
        """返回正文与回复快照中的全部本地媒体段。"""
        result = [
            segment for segment in self.segments if segment.type in {"image", "sticker"}
        ]
        result.extend(self._reply_media(self.reply))
        for node in self.nodes:
            result.extend(
                segment
                for segment in node.segments
                if segment.type in {"image", "sticker"}
            )
            result.extend(self._reply_media(node.reply))
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
            parts.extend(
                segment.text or ""
                for segment in node.segments
                if segment.type == "text"
            )
            parts.extend(self._reply_text(node.reply))
        return "\n".join(part for part in parts if part)

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
            or any(node.reply is not None for node in self.nodes)
        )

    def has_stickers(self) -> bool:
        """判断正文或回复快照中是否包含商城表情。"""
        return any(segment.type == "sticker" for segment in self._all_segments())

    def _all_segments(self) -> list[QuoteSegment]:
        result = list(self.segments)
        result.extend(self._reply_segments(self.reply))
        for node in self.nodes:
            result.extend(node.segments)
            result.extend(self._reply_segments(node.reply))
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
        return any(node.author.user_id == user_id for node in self.nodes)

    def personal_owner_id(self) -> str | None:
        """返回可安全归入个人随机池的 QQ 号。"""
        if self.type == "message":
            return self.author.user_id if self.author else None
        ids = {node.author.user_id for node in self.nodes}
        if len(ids) == 1 and None not in ids:
            return next(iter(ids))
        return None
