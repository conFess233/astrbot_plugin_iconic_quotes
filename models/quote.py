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
    """仅表示插件支持的文字或本地图片消息段。"""

    type: Literal["text", "image"]
    text: str | None = None
    path: str | None = None
    sha256: str | None = None
    mime: str | None = None
    size: int | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> QuoteSegment:
        segment_type = str(value.get("type") or "")
        if segment_type not in {"text", "image"}:
            raise ValueError(f"不支持的消息段类型: {segment_type}")
        return cls(
            type=segment_type,
            text=value.get("text"),
            path=value.get("path"),
            sha256=value.get("sha256"),
            mime=value.get("mime"),
            size=value.get("size"),
        )


@dataclass(slots=True)
class ForwardNode:
    """合并转发中的一个原始节点。"""

    author: AuthorSnapshot
    segments: list[QuoteSegment]
    source_sent_at: str | None = None

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
        )

    def image_segments(self) -> list[QuoteSegment]:
        """返回记录中全部图片段。"""
        result = [segment for segment in self.segments if segment.type == "image"]
        for node in self.nodes:
            result.extend(
                segment for segment in node.segments if segment.type == "image"
            )
        return result

    def searchable_text(self) -> str:
        """返回仅由正文组成的删除搜索文本。"""
        parts = [
            segment.text or "" for segment in self.segments if segment.type == "text"
        ]
        for node in self.nodes:
            parts.extend(
                segment.text or ""
                for segment in node.segments
                if segment.type == "text"
            )
        return "\n".join(part for part in parts if part)

    def personal_owner_id(self) -> str | None:
        """返回可安全归入个人随机池的 QQ 号。"""
        if self.type == "message":
            return self.author.user_id if self.author else None
        ids = {node.author.user_id for node in self.nodes}
        if len(ids) == 1 and None not in ids:
            return next(iter(ids))
        return None
