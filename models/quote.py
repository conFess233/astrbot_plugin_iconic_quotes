"""群典记录的数据结构、序列化与消息树遍历。"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

MEDIA_TYPES = frozenset({"image", "sticker"})


def _optional_str(value: Any, *, empty: tuple[Any, ...] = (None, "")) -> str | None:
    return str(value) if value not in empty else None


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
        return cls(
            platform=str(value.get("platform") or "aiocqhttp"),
            user_id=_optional_str(value.get("user_id")),
            nickname=str(value.get("nickname") or ""),
            raw_user_id=_optional_str(value.get("raw_user_id")),
            identity_source=str(value.get("identity_source") or "legacy"),
        )

    @property
    def display_name(self) -> str:
        return self.nickname or self.user_id or "未知用户"


@dataclass(slots=True)
class QuoteSegment:
    """插件可持久化并重放的消息段；媒体按 sha256 内容寻址，不保存路径。"""

    type: Literal["text", "image", "face", "sticker"]
    text: str | None = None
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
        size = value.get("size")
        sha256 = value.get("sha256")
        return cls(
            type=segment_type,
            text=value.get("text"),
            sha256=str(sha256).lower() if sha256 else None,
            mime=value.get("mime"),
            size=int(size) if isinstance(size, int) and size >= 0 else None,
            face_id=_optional_str(value.get("face_id")),
            emoji_package_id=_optional_str(value.get("emoji_package_id")),
            emoji_id=_optional_str(value.get("emoji_id")),
            key=_optional_str(value.get("key")),
            summary=_optional_str(value.get("summary")),
        )

    @property
    def is_media(self) -> bool:
        return self.type in MEDIA_TYPES


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
    reply_missing: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReplySnapshot:
        nested = value.get("reply")
        return cls(
            author=AuthorSnapshot.from_dict(value.get("author") or {}),
            segments=_segments(value.get("segments")),
            reply=cls.from_dict(nested) if isinstance(nested, dict) else None,
            truncated=bool(value.get("truncated", False)),
            source_message_id=_optional_str(value.get("source_message_id")),
            source_message_seq=_optional_str(
                value.get("source_message_seq"), empty=(None, "", 0, "0")
            ),
            incomplete=bool(value.get("incomplete", False)),
            reply_missing=bool(value.get("reply_missing", False)),
        )

    def chain(self) -> Iterator[ReplySnapshot]:
        """自身及更早的回复，按由近到远排列。"""
        current: ReplySnapshot | None = self
        while current is not None:
            yield current
            current = current.reply


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
            nodes=_nodes(value.get("nodes")),
            source_forward_id=_optional_str(value.get("source_forward_id")),
        )


@dataclass(slots=True)
class ForwardNode:
    """合并转发中的一个原始节点。"""

    author: AuthorSnapshot
    segments: list[QuoteSegment]
    source_sent_at: str | None = None
    reply: ReplySnapshot | None = None
    reply_missing: bool = False
    nested_forwards: list[NestedForward] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ForwardNode:
        reply = value.get("reply")
        return cls(
            author=AuthorSnapshot.from_dict(value.get("author") or {}),
            segments=_segments(value.get("segments")),
            source_sent_at=value.get("source_sent_at"),
            reply=ReplySnapshot.from_dict(reply) if isinstance(reply, dict) else None,
            reply_missing=bool(value.get("reply_missing", False)),
            nested_forwards=[
                NestedForward.from_dict(item)
                for item in value.get("nested_forwards") or []
                if isinstance(item, dict)
            ],
        )

    def walk(self) -> Iterator[ForwardNode]:
        """自身及全部子转发节点，按原始顺序深度优先。"""
        yield self
        for nested in self.nested_forwards:
            for child in nested.nodes:
                yield from child.walk()

    def depth(self) -> int:
        """含自身在内的聊天记录层数。"""
        return 1 + max(
            (
                child.depth()
                for nested in self.nested_forwards
                for child in nested.nodes
            ),
            default=0,
        )

    def has_content(self) -> bool:
        return bool(self.segments or self.reply or self.nested_forwards) and all(
            nested.nodes and all(child.has_content() for child in nested.nodes)
            for nested in self.nested_forwards
        )


def _segments(value: Any) -> list[QuoteSegment]:
    return [
        QuoteSegment.from_dict(item) for item in value or [] if isinstance(item, dict)
    ]


def _nodes(value: Any) -> list[ForwardNode]:
    return [
        ForwardNode.from_dict(item) for item in value or [] if isinstance(item, dict)
    ]


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
    reply_missing: bool = False
    source_forward_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """转换为可直接写入 JSON 的字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> QuoteRecord:
        record_type = str(value.get("type") or "")
        if record_type not in {"message", "forward"}:
            raise ValueError(f"不支持的群典类型: {record_type}")
        author = value.get("author")
        reply = value.get("reply")
        return cls(
            id=str(value["id"]),
            type=record_type,
            group_id=str(value["group_id"]),
            author=AuthorSnapshot.from_dict(author)
            if isinstance(author, dict)
            else None,
            segments=_segments(value.get("segments")),
            nodes=_nodes(value.get("nodes")),
            content_hash=str(value.get("content_hash") or ""),
            source_message_id=_optional_str(value.get("source_message_id")),
            source_sent_at=value.get("source_sent_at"),
            recorded_at=str(value.get("recorded_at") or ""),
            recorded_by=AuthorSnapshot.from_dict(value.get("recorded_by") or {}),
            identity_incomplete=bool(value.get("identity_incomplete", False)),
            reply=ReplySnapshot.from_dict(reply) if isinstance(reply, dict) else None,
            reply_missing=bool(value.get("reply_missing", False)),
            source_forward_id=_optional_str(value.get("source_forward_id")),
        )

    # 遍历 -------------------------------------------------------------

    def iter_nodes(self) -> Iterator[ForwardNode]:
        """全部转发节点（含任意层级子转发），按原始顺序。"""
        for node in self.nodes:
            yield from node.walk()

    def iter_replies(self) -> Iterator[ReplySnapshot]:
        """正文与全部节点的回复快照，含更早的回复链。"""
        if self.reply is not None:
            yield from self.reply.chain()
        for node in self.iter_nodes():
            if node.reply is not None:
                yield from node.reply.chain()

    def iter_authors(self) -> Iterator[AuthorSnapshot]:
        """普通作者、节点作者与回复作者。"""
        if self.author is not None:
            yield self.author
        for node in self.iter_nodes():
            yield node.author
        for reply in self.iter_replies():
            yield reply.author

    def iter_segments(self) -> Iterator[QuoteSegment]:
        """正文、回复快照与全部节点中的消息段。"""
        yield from self.segments
        for reply in self.iter_replies():
            yield from reply.segments
        for node in self.iter_nodes():
            yield from node.segments

    # 派生判断 ---------------------------------------------------------

    def image_segments(self) -> list[QuoteSegment]:
        """全部本地媒体段（图片与商城表情）。"""
        return [segment for segment in self.iter_segments() if segment.is_media]

    def media_hashes(self) -> set[str]:
        return {segment.sha256 for segment in self.image_segments() if segment.sha256}

    def searchable_text(self) -> str:
        """仅由正文组成的删除搜索文本。"""
        return "\n".join(_texts(self.iter_segments()))

    def authored_text(self, user_id: str) -> str:
        """指定 QQ 用户本人在记录中发送的全部文字。"""
        parts: list[str] = []
        if self.type == "message":
            if not self.author or self.author.user_id != user_id:
                return ""
            parts.extend(_texts(self.segments))
            if self.reply is not None:
                for reply in self.reply.chain():
                    if reply.author.user_id == user_id:
                        parts.extend(_texts(reply.segments))
            return "\n".join(parts)
        for node in self.iter_nodes():
            if node.author.user_id == user_id:
                parts.extend(_texts(node.segments))
            if node.reply is not None:
                for reply in node.reply.chain():
                    if reply.author.user_id == user_id:
                        parts.extend(_texts(reply.segments))
        return "\n".join(parts)

    def involves_user(self, user_id: str) -> bool:
        """用户是普通消息作者或聊天记录中的任一节点作者。"""
        if self.type == "message":
            return bool(self.author and self.author.user_id == user_id)
        return any(node.author.user_id == user_id for node in self.iter_nodes())

    def has_replies(self) -> bool:
        return self.reply is not None or any(
            node.reply is not None for node in self.iter_nodes()
        )

    def has_stickers(self) -> bool:
        return any(segment.type == "sticker" for segment in self.iter_segments())

    def has_native_segments(self) -> bool:
        """是否包含不适合 CSS 卡片表达的原生消息段。"""
        return self.has_replies() or any(
            segment.type in {"face", "sticker"} for segment in self.iter_segments()
        )

    def has_missing_replies(self) -> bool:
        """是否存在无法追溯的引用。"""
        return (
            self.reply_missing
            or any(node.reply_missing for node in self.iter_nodes())
            or any(reply.reply_missing for reply in self.iter_replies())
        )

    def forward_depth(self) -> int:
        """聊天记录原始层数；最外层计为第一层，普通消息为 0。"""
        if self.type != "forward":
            return 0
        return max((node.depth() for node in self.nodes), default=1)

    def has_content(self) -> bool:
        if self.type == "message":
            return self.author is not None and bool(self.segments or self.reply)
        return bool(self.nodes) and all(node.has_content() for node in self.nodes)

    def has_unknown_authors(self) -> bool:
        return any(node.author.user_id is None for node in self.iter_nodes())


def _texts(segments: Any) -> list[str]:
    return [
        segment.text for segment in segments if segment.type == "text" and segment.text
    ]


def remove_forward_node(nodes: list[ForwardNode], path: list[int]) -> int:
    """按稳定路径删除转发节点，返回删除的节点总数（含子节点）。

    路径为 [节点, 子转发, 节点, 子转发, 节点 …]，长度为奇数；被删空的子转发一并移除。
    路径失效时抛出 IndexError。
    """
    index = path[0]
    if index >= len(nodes):
        raise IndexError("节点路径已失效")
    if len(path) == 1:
        return sum(1 for _ in nodes.pop(index).walk())
    node = nodes[index]
    nested_index = path[1]
    if nested_index >= len(node.nested_forwards):
        raise IndexError("嵌套节点路径已失效")
    nested = node.nested_forwards[nested_index]
    removed = remove_forward_node(nested.nodes, path[2:])
    nested.source_forward_id = None
    if not nested.nodes:
        node.nested_forwards.pop(nested_index)
    return removed
