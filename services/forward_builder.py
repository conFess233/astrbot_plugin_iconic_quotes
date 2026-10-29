"""群典消息构造：唯一的 OneBot 合并转发节点树构造器与普通消息链。

原生多层与展开式降级共用同一套递归，差别只在子转发是作为子树保留还是平铺到同一层。
所有方法会读取媒体文件，调用方应在线程中执行。
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

import astrbot.api.message_components as Comp

from ..models import (
    ForwardNode,
    NestedForward,
    QuoteRecord,
    QuoteSegment,
    ReplySnapshot,
)
from ..utils.timefmt import local_time, unix_seconds
from .media import MediaStore
from .onebot import MarketFaceComponent, OneBotReply

Raw = dict[str, Any]


@dataclass(slots=True, frozen=True)
class BuildOptions:
    """一次发送尝试的降级组合。"""

    native_replies: bool = True
    native_stickers: bool = True
    flatten: bool = False
    time_mode: str = "text"
    missing: frozenset[str] = field(default_factory=frozenset)


@dataclass(slots=True, frozen=True)
class BurstHeader:
    target_name: str
    total: int
    page: int
    pages: int
    skipped: int = 0
    identity_incomplete: bool = False


def missing_reply_warning(records: list[QuoteRecord]) -> str | None:
    """普通消息与合集共用的引用丢失提示。"""
    ids = [record.id[:8] for record in records if record.has_missing_replies()]
    return "以下群典存在无法追溯的引用内容：" + "、".join(ids) if ids else None


def record_time(value: str) -> str:
    return local_time(value) or "记录时间未知"


def _text(value: str) -> Raw:
    return {"type": "text", "data": {"text": value}}


def _media_label(segment: QuoteSegment) -> str:
    return f"[{'表情' if segment.type == 'sticker' else '图片'}资源缺失]"


def _sticker_native(segment: QuoteSegment, options: BuildOptions) -> bool:
    return bool(
        segment.type == "sticker"
        and options.native_stickers
        and segment.emoji_package_id
        and segment.emoji_id
        and segment.key
    )


def forward_news(messages: list[Raw]) -> list[dict[str, str]]:
    """从前四个节点提取不含路径的文字预览。"""
    result: list[dict[str, str]] = []
    for message in messages[:4]:
        data = message.get("data") if isinstance(message, dict) else None
        if not isinstance(data, dict):
            continue
        parts: list[str] = []
        for segment in data.get("content") or []:
            if not isinstance(segment, dict):
                continue
            kind = str(segment.get("type") or "")
            if kind == "text":
                text = str((segment.get("data") or {}).get("text") or "").strip()
                if text:
                    parts.append(text)
            elif kind == "node":
                parts.append("[聊天记录]")
            elif kind in {"image", "mface"}:
                parts.append("[图片]")
            elif kind == "face":
                parts.append("[表情]")
        summary = " ".join(parts).replace("\n", " ").strip() or "[消息]"
        result.append({"text": f"{data.get('nickname') or '未知用户'}：{summary[:60]}"})
    return result or [{"text": "聊天记录"}]


def forward_meta(messages: list[Raw]) -> dict[str, Any]:
    """NapCat 合并转发卡片元数据，与嵌套层保持一致。"""
    return {
        "source": "聊天记录",
        "summary": f"查看 {len(messages)} 条转发消息",
        "prompt": "[聊天记录]",
        "news": forward_news(messages),
    }


def raw_node(
    *,
    uin: str | None,
    name: str,
    content: list[Raw],
    timestamp: int = 0,
    shell: bool = False,
) -> Raw:
    data: dict[str, Any] = {
        "user_id": str(uin or "0"),
        "nickname": name,
        "content": content,
    }
    if timestamp:
        data["time"] = timestamp
    if shell:
        # NapCat 会把只含 node 的 content 识别为下一层聊天记录。
        data.update(forward_meta(content))
    return {"type": "node", "data": data}


class ForwardBuilder:
    """把记录转换为 OneBot 原始节点树或 AstrBot 消息链。"""

    def __init__(self, media: MediaStore):
        self.media = media

    # 原始段 -----------------------------------------------------------

    def _segments(
        self, segments: list[QuoteSegment], options: BuildOptions
    ) -> list[Raw]:
        result: list[Raw] = []
        for segment in segments:
            if segment.type == "text" and segment.text:
                result.append(_text(segment.text))
            elif segment.type == "face" and segment.face_id:
                result.append({"type": "face", "data": {"id": int(segment.face_id)}})
            elif segment.is_media:
                if not segment.sha256 or segment.sha256 in options.missing:
                    result.append(_text(_media_label(segment)))
                elif _sticker_native(segment, options):
                    result.append(
                        {
                            "type": "mface",
                            "data": {
                                "emoji_package_id": segment.emoji_package_id,
                                "emoji_id": segment.emoji_id,
                                "key": segment.key,
                                "summary": segment.summary or "[商城表情]",
                            },
                        }
                    )
                else:
                    try:
                        data = self.media.read(segment.sha256, segment.mime)
                    except (OSError, ValueError):
                        result.append(_text(_media_label(segment)))
                        continue
                    encoded = base64.b64encode(data).decode()
                    result.append(
                        {"type": "image", "data": {"file": f"base64://{encoded}"}}
                    )
        return result

    @staticmethod
    def _reply(reply: ReplySnapshot | None, options: BuildOptions) -> list[Raw]:
        if reply is None or not options.native_replies:
            return []
        if not reply.source_message_id:
            raise ValueError("回复快照缺少原始消息 ID")
        return [
            OneBotReply(
                id=reply.source_message_id, seq=reply.source_message_seq or 0
            ).toDict()
        ]

    def _reply_fallback(self, reply: ReplySnapshot, options: BuildOptions) -> list[Raw]:
        """原生回复不可用时，把被回复内容拆为独立节点（由远到近）。"""
        result: list[Raw] = []
        if reply.reply:
            result.extend(self._reply_fallback(reply.reply, options))
        content = self._segments(reply.segments, options)
        if reply.truncated:
            content.append(_text("更早的回复已省略"))
        if reply.incomplete:
            content.append(_text("[回复内容来自消息内嵌快照]"))
        if content:
            result.append(
                raw_node(
                    uin=reply.author.user_id,
                    name=reply.author.display_name,
                    content=content,
                )
            )
        return result

    def _author_node(
        self, node: ForwardNode, content: list[Raw], *, shell: bool = False
    ) -> Raw:
        return raw_node(
            uin=node.author.user_id,
            name=node.author.display_name,
            content=content,
            timestamp=unix_seconds(node.source_sent_at),
            shell=shell,
        )

    def _level(self, nodes: list[ForwardNode], options: BuildOptions) -> list[Raw]:
        """按存档位置递归构造；普通内容与子 node 不混放，避免 NapCat 丢段。"""
        result: list[Raw] = []
        for node in nodes:
            if node.reply and not options.native_replies:
                result.extend(self._reply_fallback(node.reply, options))
            reply = self._reply(node.reply, options)
            if not node.nested_forwards:
                result.append(
                    self._author_node(
                        node, reply + self._segments(node.segments, options)
                    )
                )
                continue
            by_position: dict[int, list[NestedForward]] = {}
            for nested in node.nested_forwards:
                by_position.setdefault(nested.position, []).append(nested)
            chunk: list[QuoteSegment] = []

            def flush(
                current: ForwardNode = node, pending: list[QuoteSegment] = chunk
            ) -> None:
                nonlocal reply
                content = reply + self._segments(pending, options)
                if content:
                    result.append(self._author_node(current, content))
                    reply = []
                pending.clear()

            for index in range(len(node.segments) + 1):
                if by_position.get(index):
                    flush()
                for nested in by_position.get(index, []):
                    children = self._level(nested.nodes, options)
                    if options.flatten:
                        result.extend(children)
                    else:
                        result.append(self._author_node(node, children, shell=True))
                if index < len(node.segments):
                    chunk.append(node.segments[index])
            flush()
        return result

    def _message_nodes(
        self, record: QuoteRecord, options: BuildOptions, *, timestamp: int = 0
    ) -> list[Raw]:
        assert record.author is not None
        result: list[Raw] = []
        if record.reply and not options.native_replies:
            result.extend(self._reply_fallback(record.reply, options))
        result.append(
            raw_node(
                uin=record.author.user_id,
                name=record.author.display_name,
                content=self._reply(record.reply, options)
                + self._segments(record.segments, options),
                timestamp=timestamp,
            )
        )
        return result

    # 对外构造 ---------------------------------------------------------

    def forward(
        self,
        records: list[QuoteRecord],
        options: BuildOptions,
        *,
        replay: bool,
        hints: bool = True,
    ) -> list[Raw]:
        """多条普通记录聚合，或一条合并转发记录的回放。

        hints 为 False 时不插入“群典存档回放”“引用丢失提示”等提示节点。
        """
        result: list[Raw] = []
        if hints and replay:
            result.append(
                raw_node(uin="0", name="群典", content=[_text("群典存档回放")])
            )
        if hints and (warning := missing_reply_warning(records)):
            result.append(
                raw_node(uin="0", name="引用丢失提示", content=[_text(warning)])
            )
        for record in records:
            if record.type == "forward":
                result.extend(self._level(record.nodes, options))
            else:
                result.extend(self._message_nodes(record, options))
        return result

    def burst(
        self,
        records: list[QuoteRecord],
        header: BurstHeader,
        options: BuildOptions,
        *,
        hints: bool = True,
    ) -> list[Raw]:
        """按时间排列的个人合集；原生模式下每条转发记录是一个子树。

        标题节点带有页码信息，始终保留；hints 只控制引用丢失提示节点。
        """
        title = f"聊天记录：{header.target_name}\n"
        title += (
            f"共 {header.total} 条｜第 {header.page} / {header.pages} 页"
            if header.pages > 1
            else f"共 {header.total} 条"
        )
        if header.skipped:
            title += f"\n另有 {header.skipped} 条记录完全损坏，无法展示"
        if header.identity_incomplete:
            title += "\n部分历史节点的作者身份尚未确认"
        missing = sum(
            1
            for record in records
            for segment in record.image_segments()
            if not segment.sha256 or segment.sha256 in options.missing
        )
        if missing:
            title += f"\n本页有 {missing} 处媒体资源缺失"
        result = [raw_node(uin="0", name="群典", content=[_text(title)])]
        if hints and (warning := missing_reply_warning(records)):
            result.append(
                raw_node(uin="0", name="引用丢失提示", content=[_text(warning)])
            )
        for record in records:
            timestamp = (
                unix_seconds(record.recorded_at) if options.time_mode == "native" else 0
            )
            if options.time_mode == "text":
                result.append(
                    raw_node(
                        uin="0",
                        name="记录时间",
                        content=[_text(record_time(record.recorded_at))],
                    )
                )
            if record.type == "message":
                result.extend(self._message_nodes(record, options, timestamp=timestamp))
                continue
            children = self._level(record.nodes, options)
            if options.flatten:
                result.append(
                    raw_node(
                        uin="0",
                        name="聊天记录存档",
                        content=[_text("以下为本地存档的聊天记录")],
                        timestamp=timestamp,
                    )
                )
                result.extend(children)
            else:
                result.append(
                    raw_node(
                        uin="0",
                        name="聊天记录存档",
                        content=children,
                        timestamp=timestamp,
                        shell=True,
                    )
                )
        return result

    def text_chain(self, record: QuoteRecord, options: BuildOptions) -> list[Any]:
        """单条普通记录的消息链：“正文”——昵称，图片与表情按原顺序保留。"""
        if record.type != "message" or record.author is None:
            raise ValueError("文字链只适用于普通群典")
        nickname = record.author.display_name
        chain: list[Any] = []
        if record.reply is not None and options.native_replies:
            if not record.reply.source_message_id:
                raise ValueError("回复快照缺少原始消息 ID")
            chain.append(
                OneBotReply(
                    id=record.reply.source_message_id,
                    seq=record.reply.source_message_seq or 0,
                )
            )
        if record.segments and all(
            segment.type == "text" for segment in record.segments
        ):
            text = "".join(segment.text or "" for segment in record.segments)
            chain.append(Comp.Plain(f"“{text}”——{nickname}"))
            return chain
        for segment in record.segments:
            if segment.type == "text" and segment.text:
                chain.append(Comp.Plain(segment.text))
            elif segment.type == "face" and segment.face_id:
                chain.append(Comp.Face(id=int(segment.face_id)))
            elif segment.is_media:
                if not segment.sha256 or segment.sha256 in options.missing:
                    chain.append(Comp.Plain(_media_label(segment)))
                elif _sticker_native(segment, options):
                    chain.append(
                        MarketFaceComponent(
                            emoji_package_id=segment.emoji_package_id,
                            emoji_id=segment.emoji_id,
                            key=segment.key,
                            summary=segment.summary or "[商城表情]",
                        )
                    )
                else:
                    chain.append(
                        Comp.Image.fromFileSystem(
                            str(self.media.path(segment.sha256, segment.mime))
                        )
                    )
        chain.append(Comp.Plain(f"——{nickname}"))
        return chain
