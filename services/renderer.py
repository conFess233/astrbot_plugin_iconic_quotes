"""群典文字、图片卡片与合并转发消息构造。"""

from __future__ import annotations

import asyncio
import base64
import html
import inspect
from datetime import datetime
from typing import Any

import astrbot.api.message_components as Comp

from ..models import (
    ForwardNode,
    NestedForward,
    QuoteRecord,
    QuoteSegment,
    ReplySnapshot,
)
from ..utils.image_processing import trim_card_canvas
from .avatar_cache import AvatarCacheService
from .storage import QuoteStorage, StorageError


class _OneBotReply(Comp.Reply):
    """为 NapCat 保留原生消息序号，同时兼容标准 OneBot 的消息 ID。"""

    def toDict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": str(self.id)}
        if self.seq not in (None, 0, "0", ""):
            data["seq"] = str(self.seq)
        return {"type": "reply", "data": data}


CARD_TEMPLATE = """
<!doctype html>
<html><head><meta charset="utf-8"><style>
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: transparent; }
.quote-card {
  width: {{ width }}px; min-height: {{ min_height }}px; max-height: {{ max_height }}px;
  overflow: hidden; display: grid; grid-template-columns: 250px 1fr; gap: 42px;
  padding: 54px; color: #29251f; background: linear-gradient(135deg, #f7f2e8, #e9dfcc);
  border: 2px solid rgba(91, 70, 42, .2); border-radius: 28px;
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
}
.avatar { width: 220px; height: 220px; border-radius: 50%; object-fit: cover; background: rgba(255,255,255,.55); }
.avatar.empty { border: 2px dashed rgba(91,70,42,.25); }
.content { min-width: 0; display: flex; flex-direction: column; align-self: start; }
.quote { font-size: 42px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.quote::before { content: "“"; font-size: 72px; color: #9a6d38; vertical-align: -.2em; }
.quote::after { content: "”"; font-size: 72px; color: #9a6d38; vertical-align: -.2em; }
.images { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 24px; }
.images img { width: 100%; max-height: 460px; object-fit: contain; border-radius: 14px; background: rgba(255,255,255,.48); }
.meta { margin-top: 28px; text-align: right; color: #625746; font-size: 25px; line-height: 1.45; }
.page { opacity: .65; font-size: 19px; }
{{ custom_css }}
</style></head><body>
<article class="quote-card">
  {% if avatar %}<img class="avatar" src="{{ avatar }}">{% else %}<div class="avatar empty"></div>{% endif %}
  <section class="content">
    <div class="quote">{{ text }}</div>
    {% if images %}<div class="images">{% for image in images %}<img src="{{ image }}">{% endfor %}</div>{% endif %}
    <div class="meta"><div>{{ nickname }}</div><div>{{ recorded_at }}</div>{% if pages > 1 %}<div class="page">{{ page }}/{{ pages }}</div>{% endif %}</div>
  </section>
</article></body></html>
"""


class MarketFaceComponent(Comp.BaseMessageComponent):
    """AstrBot 尚未内建的 OneBot mface 发送组件。"""

    type: Comp.ComponentType = Comp.ComponentType.Image
    emoji_package_id: str
    emoji_id: str
    key: str
    summary: str

    def toDict(self) -> dict[str, Any]:
        return {
            "type": "mface",
            "data": {
                "emoji_package_id": self.emoji_package_id,
                "emoji_id": self.emoji_id,
                "key": self.key,
                "summary": self.summary,
            },
        }


class NativeTimeNode(Comp.Node):
    """尽力向 OneBot 自定义节点附加非标准 time 字段。"""

    async def to_dict(self) -> dict[str, Any]:
        payload = await super().to_dict()
        payload["data"]["time"] = int(self.time or 0)
        return payload


class QuoteRenderer:
    """创建跨 Handler 可复用的消息链和卡片。"""

    def __init__(
        self,
        plugin: Any,
        storage: QuoteStorage,
        avatars: AvatarCacheService,
    ):
        self.plugin = plugin
        self.storage = storage
        self.avatars = avatars

    async def initialize(self) -> None:
        """保留旧生命周期入口，由头像服务统一管理网络资源。"""

    async def close(self) -> None:
        """保留旧生命周期入口。"""

    @staticmethod
    def missing_reply_warning(records: list[QuoteRecord]) -> str | None:
        """生成可供普通消息与合集共用的引用丢失提示。"""
        missing_ids = [
            record.id[:8] for record in records if record.has_missing_replies()
        ]
        if not missing_ids:
            return None
        return "以下群典存在无法追溯的引用内容：" + "、".join(missing_ids)

    async def avatar_data_url(
        self, user_id: str | None, settings: dict[str, Any]
    ) -> str:
        """按配置取得实时或本地化 QQ 头像。"""
        return await self.avatars.data_url(user_id, settings)

    def text_chain(
        self,
        record: QuoteRecord,
        *,
        native_stickers: bool = True,
        native_replies: bool = True,
    ) -> list[Any]:
        """构造保留原图顺序的文字发送链。"""
        if record.type != "message" or record.author is None:
            raise ValueError("文字链只适用于普通群典")
        nickname = record.author.nickname or record.author.user_id or "未知用户"
        chain = self._reply_to_components(
            record.reply,
            native_stickers=native_stickers,
            native_replies=native_replies,
        )
        if record.segments and all(
            segment.type == "text" for segment in record.segments
        ):
            text = "".join(segment.text or "" for segment in record.segments)
            chain.append(Comp.Plain(f"“{text}”——{nickname}"))
            return chain
        chain.extend(
            self._segments_to_components(
                record.segments,
                native_stickers=native_stickers,
            )
        )
        chain.append(Comp.Plain(f"——{nickname}"))
        return chain

    async def card_paths(
        self, record: QuoteRecord, settings: dict[str, Any]
    ) -> list[str]:
        """按文字和图片数量拆页，通过 AstrBot 配置的端点渲染卡片。"""
        if record.type != "message" or record.author is None:
            raise ValueError("卡片只适用于普通群典")
        text = "".join(
            segment.text or "" for segment in record.segments if segment.type == "text"
        )
        image_urls = [
            self._media_data_url(segment)
            for segment in record.segments
            if segment.type == "image"
        ]
        text_chunks = [
            text[index : index + 1200] for index in range(0, len(text), 1200)
        ] or [""]
        image_chunks = [
            image_urls[index : index + 4] for index in range(0, len(image_urls), 4)
        ] or [[]]
        page_count = max(len(text_chunks), len(image_chunks))
        avatar = await self.avatar_data_url(record.author.user_id, settings)
        paths: list[str] = []
        for index in range(page_count):
            data = {
                "width": settings["card_width"],
                "min_height": (
                    0 if settings["card_auto_height"] else settings["card_min_height"]
                ),
                "max_height": settings["card_max_height"],
                "custom_css": settings["card_custom_css"],
                "avatar": avatar,
                "text": html.escape(
                    text_chunks[index] if index < len(text_chunks) else ""
                ),
                "images": image_chunks[index] if index < len(image_chunks) else [],
                "nickname": html.escape(
                    record.author.nickname or record.author.user_id or "未知用户"
                ),
                "recorded_at": self._display_time(record.recorded_at),
                "page": index + 1,
                "pages": page_count,
            }
            path = await self.plugin.html_render(
                CARD_TEMPLATE,
                data,
                return_url=False,
                options={"full_page": True, "type": "png"},
            )
            await asyncio.to_thread(trim_card_canvas, path)
            paths.append(path)
        return paths

    def _media_data_url(self, segment: QuoteSegment) -> str:
        if not segment.path:
            return ""
        data = self.storage.resolve_media_path(segment.path).read_bytes()
        encoded = base64.b64encode(data).decode()
        return f"data:{segment.mime or 'image/png'};base64,{encoded}"

    @staticmethod
    def _display_time(value: str) -> str:
        try:
            return "记录于 " + datetime.fromisoformat(value).astimezone().strftime(
                "%Y-%m-%d %H:%M"
            )
        except (ValueError, TypeError):
            return "记录时间未知"

    def forward_nodes(
        self,
        records: list[QuoteRecord],
        *,
        replay: bool = False,
        native_stickers: bool = True,
        native_replies: bool = True,
    ) -> list[Any]:
        """构造不依赖历史 res_id 的展开式合并转发节点。"""
        nodes: list[Any] = []
        if replay:
            nodes.append(
                Comp.Node(uin="0", name="群典", content=[Comp.Plain("群典存档回放")])
            )
        if warning := self.missing_reply_warning(records):
            nodes.append(
                Comp.Node(uin="0", name="引用丢失提示", content=[Comp.Plain(warning)])
            )
        for record in records:
            if record.type == "forward":
                nodes.extend(
                    self._forward_level_nodes(
                        record.nodes,
                        native_stickers=native_stickers,
                        native_replies=native_replies,
                    )
                )
                continue
            assert record.author is not None
            nickname = record.author.nickname or record.author.user_id or "未知用户"
            if record.reply and not native_replies:
                nodes.extend(
                    self._reply_fallback_nodes(
                        record.reply,
                        native_stickers=native_stickers,
                    )
                )
            content = self._reply_to_components(
                record.reply,
                native_stickers=native_stickers,
                native_replies=native_replies,
            )
            content.extend(
                self._segments_to_components(
                    record.segments,
                    native_stickers=native_stickers,
                )
            )
            nodes.append(
                Comp.Node(
                    uin=record.author.user_id or "0",
                    name=nickname,
                    content=content,
                )
            )
        return nodes

    async def raw_forward_nodes(
        self,
        records: list[QuoteRecord],
        *,
        replay: bool = False,
        native_stickers: bool = True,
        native_replies: bool = True,
    ) -> list[dict[str, Any]]:
        """递归构造 NapCat 可识别的 OneBot 原始合并转发节点。"""
        result: list[dict[str, Any]] = []
        if replay:
            result.append(
                self._raw_node(
                    uin="0",
                    name="群典",
                    content=[{"type": "text", "data": {"text": "群典存档回放"}}],
                )
            )
        if warning := self.missing_reply_warning(records):
            result.append(
                self._raw_node(
                    uin="0",
                    name="引用丢失提示",
                    content=[{"type": "text", "data": {"text": warning}}],
                )
            )
        for record in records:
            if record.type == "forward":
                result.extend(
                    await self._raw_forward_level_nodes(
                        record.nodes,
                        native_stickers=native_stickers,
                        native_replies=native_replies,
                    )
                )
                continue
            assert record.author is not None
            content = self._reply_to_components(
                record.reply,
                native_stickers=native_stickers,
                native_replies=native_replies,
            )
            content.extend(
                self._segments_to_components(
                    record.segments,
                    native_stickers=native_stickers,
                )
            )
            result.append(
                self._raw_node(
                    uin=record.author.user_id or "0",
                    name=record.author.nickname or record.author.user_id or "未知用户",
                    content=await self._components_to_payload(content),
                )
            )
        return result

    async def raw_burst_nodes(
        self,
        records: list[QuoteRecord],
        *,
        target_name: str,
        total: int,
        page: int,
        pages: int,
        skipped: int,
        native_stickers: bool,
        native_replies: bool,
        time_mode: str,
        identity_incomplete: bool = False,
    ) -> list[dict[str, Any]]:
        """把整页爆典构造成一次原子发送的原始节点树。"""
        title = f"聊天记录：{target_name}\n"
        title += (
            f"共 {total} 条｜第 {page} / {pages} 页" if pages > 1 else f"共 {total} 条"
        )
        if skipped:
            title += f"\n另有 {skipped} 条记录完全损坏，无法展示"
        if identity_incomplete:
            title += "\n部分历史节点的作者身份尚未确认"
        missing_media = sum(
            self.storage.missing_media_count(record) for record in records
        )
        if missing_media:
            title += f"\n本页有 {missing_media} 处媒体资源缺失"
        result = [
            self._raw_node(
                uin="0",
                name="群典",
                content=[{"type": "text", "data": {"text": title}}],
            )
        ]
        if warning := self.missing_reply_warning(records):
            result.append(
                self._raw_node(
                    uin="0",
                    name="引用丢失提示",
                    content=[{"type": "text", "data": {"text": warning}}],
                )
            )
        for record in records:
            native_time = self._native_time(record.recorded_at)
            if time_mode == "text":
                result.append(
                    self._raw_node(
                        uin="0",
                        name="记录时间",
                        content=[
                            {
                                "type": "text",
                                "data": {"text": self._record_time(record.recorded_at)},
                            }
                        ],
                    )
                )
            if record.type == "forward":
                children = await self._raw_forward_level_nodes(
                    record.nodes,
                    native_stickers=native_stickers,
                    native_replies=native_replies,
                )
                result.append(
                    self._raw_node(
                        uin="0",
                        name="聊天记录存档",
                        content=children,
                        timestamp=native_time if time_mode == "native" else 0,
                        shell_count=len(children),
                    )
                )
                continue
            assert record.author is not None
            components = self._reply_to_components(
                record.reply,
                native_stickers=native_stickers,
                native_replies=native_replies,
            )
            components.extend(
                self._segments_to_components(
                    record.segments,
                    native_stickers=native_stickers,
                )
            )
            result.append(
                self._raw_node(
                    uin=record.author.user_id or "0",
                    name=record.author.nickname or record.author.user_id or "未知用户",
                    content=await self._components_to_payload(components),
                    timestamp=native_time if time_mode == "native" else 0,
                )
            )
        return result

    async def _raw_forward_level_nodes(
        self,
        nodes: list[ForwardNode],
        *,
        native_stickers: bool,
        native_replies: bool,
    ) -> list[dict[str, Any]]:
        """按存档位置递归展开；普通内容与子 node 不混放，避免 NapCat 丢段。"""
        result: list[dict[str, Any]] = []
        for node in nodes:
            if node.reply and not native_replies:
                result.extend(
                    await self._components_to_payload(
                        self._reply_fallback_nodes(
                            node.reply,
                            native_stickers=native_stickers,
                        )
                    )
                )
            if not node.nested_forwards:
                components = self._reply_to_components(
                    node.reply,
                    native_stickers=native_stickers,
                    native_replies=native_replies,
                )
                components.extend(
                    self._segments_to_components(
                        node.segments,
                        native_stickers=native_stickers,
                    )
                )
                result.append(
                    self._raw_author_node(
                        node,
                        await self._components_to_payload(components),
                    )
                )
                continue

            by_position: dict[int, list[NestedForward]] = {}
            for nested in node.nested_forwards:
                by_position.setdefault(nested.position, []).append(nested)
            chunk: list[QuoteSegment] = []
            reply_pending = bool(node.reply and native_replies)

            async def flush(
                current_node: ForwardNode = node,
                current_chunk: list[QuoteSegment] = chunk,
            ) -> None:
                nonlocal reply_pending
                components: list[Any] = []
                if reply_pending:
                    components.extend(
                        self._reply_to_components(
                            current_node.reply,
                            native_stickers=native_stickers,
                            native_replies=True,
                        )
                    )
                    reply_pending = False
                components.extend(
                    self._segments_to_components(
                        current_chunk,
                        native_stickers=native_stickers,
                    )
                )
                if components:
                    result.append(
                        self._raw_author_node(
                            current_node,
                            await self._components_to_payload(components),
                        )
                    )
                current_chunk.clear()

            for index in range(len(node.segments) + 1):
                if by_position.get(index):
                    await flush()
                for nested in by_position.get(index, []):
                    children = await self._raw_forward_level_nodes(
                        nested.nodes,
                        native_stickers=native_stickers,
                        native_replies=native_replies,
                    )
                    result.append(
                        self._raw_author_node(
                            node,
                            children,
                            shell_count=len(children),
                        )
                    )
                if index < len(node.segments):
                    chunk.append(node.segments[index])
            await flush()
        return result

    async def _components_to_payload(
        self, components: list[Any]
    ) -> list[dict[str, Any]]:
        """兼容 AstrBot 组件的同步 toDict 与异步 to_dict 两套协议。"""
        result: list[dict[str, Any]] = []
        for component in components:
            if isinstance(component, Comp.Image):
                converter = getattr(component, "convert_to_base64", None)
                if callable(converter):
                    encoded = converter()
                    if inspect.isawaitable(encoded):
                        encoded = await encoded
                    result.append(
                        {
                            "type": "image",
                            "data": {"file": f"base64://{encoded}"},
                        }
                    )
                    continue
            converter = getattr(component, "to_dict", None)
            if callable(converter):
                payload = converter()
                if inspect.isawaitable(payload):
                    payload = await payload
            else:
                converter = getattr(component, "toDict", None)
                if not callable(converter):
                    raise TypeError("消息组件不支持 OneBot 序列化")
                payload = converter()
            if isinstance(payload, dict):
                result.append(payload)
        return result

    @classmethod
    def _raw_author_node(
        cls,
        node: ForwardNode,
        content: list[dict[str, Any]],
        *,
        shell_count: int | None = None,
    ) -> dict[str, Any]:
        return cls._raw_node(
            uin=node.author.user_id or "0",
            name=node.author.nickname or node.author.user_id or "未知用户",
            content=content,
            timestamp=cls._native_time(node.source_sent_at or ""),
            shell_count=shell_count,
        )

    @classmethod
    def _raw_node(
        cls,
        *,
        uin: str,
        name: str,
        content: list[dict[str, Any]],
        timestamp: int = 0,
        shell_count: int | None = None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "user_id": str(uin),
            "nickname": name,
            "content": content,
        }
        if timestamp:
            data["time"] = timestamp
        if shell_count is not None:
            # NapCat 会把只含 node 的 content 识别为下一层聊天记录。
            data.update(
                {
                    "source": "聊天记录",
                    "summary": f"查看 {shell_count} 条转发消息",
                    "prompt": "[聊天记录]",
                    "news": cls.raw_forward_news(content),
                }
            )
        return {"type": "node", "data": data}

    @classmethod
    def raw_forward_meta(cls, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """生成与嵌套层一致的 NapCat 合并转发卡片元数据。"""
        return {
            "source": "聊天记录",
            "summary": f"查看 {len(messages)} 条转发消息",
            "prompt": "[聊天记录]",
            "news": cls.raw_forward_news(messages),
        }

    @staticmethod
    def raw_forward_news(
        messages: list[dict[str, Any]],
    ) -> list[dict[str, str]]:
        """从前四个节点提取不含路径的安全文字预览。"""
        result: list[dict[str, str]] = []
        for message in messages[:4]:
            data = message.get("data") if isinstance(message, dict) else None
            if not isinstance(data, dict):
                continue
            nickname = str(data.get("nickname") or "未知用户")
            content = data.get("content")
            parts: list[str] = []
            if isinstance(content, list):
                for segment in content:
                    if not isinstance(segment, dict):
                        continue
                    segment_type = str(segment.get("type") or "")
                    segment_data = segment.get("data")
                    if segment_type == "text" and isinstance(segment_data, dict):
                        text = str(segment_data.get("text") or "").strip()
                        if text:
                            parts.append(text)
                    elif segment_type == "node":
                        parts.append("[聊天记录]")
                    elif segment_type in {"image", "mface"}:
                        parts.append("[图片]")
                    elif segment_type == "face":
                        parts.append("[表情]")
            summary = " ".join(parts).replace("\n", " ").strip() or "[消息]"
            result.append({"text": f"{nickname}：{summary[:60]}"})
        return result or [{"text": "聊天记录"}]

    def burst_nodes(
        self,
        records: list[QuoteRecord],
        *,
        target_name: str,
        total: int,
        page: int,
        pages: int,
        skipped: int,
        native_stickers: bool,
        native_replies: bool,
        time_mode: str,
        identity_incomplete: bool = False,
    ) -> list[Any]:
        """构造按时间排列的个人完整群典合集。"""
        title = f"聊天记录：{target_name}\n"
        title += (
            f"共 {total} 条｜第 {page} / {pages} 页" if pages > 1 else f"共 {total} 条"
        )
        if skipped:
            title += f"\n另有 {skipped} 条记录完全损坏，无法展示"
        if identity_incomplete:
            title += "\n部分历史节点的作者身份尚未确认"
        missing_media = sum(
            self.storage.missing_media_count(record) for record in records
        )
        if missing_media:
            title += f"\n本页有 {missing_media} 处媒体资源缺失"
        result: list[Any] = [
            Comp.Node(uin="0", name="群典", content=[Comp.Plain(title)])
        ]
        if warning := self.missing_reply_warning(records):
            result.append(
                Comp.Node(
                    uin="0",
                    name="引用丢失提示",
                    content=[Comp.Plain(warning)],
                )
            )
        for record in records:
            timestamp = self._record_time(record.recorded_at)
            native_time = self._native_time(record.recorded_at)
            if time_mode == "text":
                result.append(
                    Comp.Node(
                        uin="0",
                        name="记录时间",
                        content=[Comp.Plain(timestamp)],
                    )
                )
            if record.type == "message":
                assert record.author is not None
                content: list[Any] = []
                if record.reply and not native_replies:
                    result.extend(
                        self._reply_fallback_nodes(
                            record.reply,
                            native_stickers=native_stickers,
                        )
                    )
                content.extend(
                    self._reply_to_components(
                        record.reply,
                        native_stickers=native_stickers,
                        native_replies=native_replies,
                    )
                )
                content.extend(
                    self._segments_to_components(
                        record.segments,
                        native_stickers=native_stickers,
                    )
                )
                result.append(
                    self._burst_node(
                        uin=record.author.user_id or "0",
                        name=record.author.nickname
                        or record.author.user_id
                        or "未知用户",
                        content=content,
                        native_time=native_time,
                        time_mode=time_mode,
                    )
                )
                continue
            inner = self._forward_level_nodes(
                record.nodes,
                native_stickers=native_stickers,
                native_replies=native_replies,
            )
            result.append(
                self._burst_node(
                    uin="0",
                    name="聊天记录存档",
                    content=[Comp.Plain("以下为本地存档的聊天记录")],
                    native_time=native_time,
                    time_mode=time_mode,
                )
            )
            result.extend(inner)
        return result

    def _forward_level_nodes(
        self,
        nodes: list[ForwardNode],
        *,
        native_stickers: bool,
        native_replies: bool,
    ) -> list[Any]:
        result: list[Any] = []
        for node in nodes:
            if node.reply and not native_replies:
                result.extend(
                    self._reply_fallback_nodes(
                        node.reply,
                        native_stickers=native_stickers,
                    )
                )
            result.extend(
                self._flatten_node(
                    node,
                    native_stickers=native_stickers,
                    native_replies=native_replies,
                )
            )
        return result

    def _flatten_node(
        self,
        node: ForwardNode,
        *,
        native_stickers: bool,
        native_replies: bool,
    ) -> list[Any]:
        result: list[Any] = []
        by_position: dict[int, list[NestedForward]] = {}
        for nested in node.nested_forwards:
            by_position.setdefault(nested.position, []).append(nested)
        chunk: list[QuoteSegment] = []
        first_chunk = True

        def flush() -> None:
            nonlocal first_chunk
            content: list[Any] = []
            if first_chunk:
                content.extend(
                    self._reply_to_components(
                        node.reply,
                        native_stickers=native_stickers,
                        native_replies=native_replies,
                    )
                )
            content.extend(
                self._segments_to_components(chunk, native_stickers=native_stickers)
            )
            if content:
                result.append(self._author_node(node, content))
                first_chunk = False
            chunk.clear()

        for index in range(len(node.segments) + 1):
            if by_position.get(index):
                flush()
            for nested in by_position.get(index, []):
                result.extend(
                    self._forward_level_nodes(
                        nested.nodes,
                        native_stickers=native_stickers,
                        native_replies=native_replies,
                    )
                )
            if index < len(node.segments):
                chunk.append(node.segments[index])
        flush()
        return result

    @staticmethod
    def _author_node(node: ForwardNode, content: list[Any]) -> Any:
        return Comp.Node(
            uin=node.author.user_id or "0",
            name=node.author.nickname or node.author.user_id or "未知用户",
            content=content,
        )

    @staticmethod
    def _burst_node(
        *,
        uin: str,
        name: str,
        content: list[Any],
        native_time: int,
        time_mode: str,
    ) -> Any:
        if time_mode == "native":
            return NativeTimeNode(
                uin=uin,
                name=name,
                content=content,
                time=native_time,
            )
        return Comp.Node(uin=uin, name=name, content=content)

    @staticmethod
    def _native_time(value: str) -> int:
        try:
            return int(datetime.fromisoformat(value).timestamp())
        except (TypeError, ValueError):
            return 0

    def _segments_to_components(
        self,
        segments: list[QuoteSegment],
        *,
        native_stickers: bool = True,
    ) -> list[Any]:
        result: list[Any] = []
        for segment in segments:
            if segment.type == "text" and segment.text:
                result.append(Comp.Plain(segment.text))
            elif segment.type in {"image", "sticker"} and segment.path:
                try:
                    media_path = self.storage.resolve_media_path(segment.path)
                    if not self.storage.media_segment_valid(segment):
                        raise StorageError("媒体哈希不匹配")
                except (OSError, StorageError):
                    label = "表情" if segment.type == "sticker" else "图片"
                    result.append(Comp.Plain(f"[{label}资源缺失]"))
                    continue
                if (
                    segment.type == "sticker"
                    and native_stickers
                    and segment.emoji_package_id
                    and segment.emoji_id
                    and segment.key
                ):
                    result.append(
                        MarketFaceComponent(
                            emoji_package_id=segment.emoji_package_id,
                            emoji_id=segment.emoji_id,
                            key=segment.key,
                            summary=segment.summary or "[商城表情]",
                        )
                    )
                    continue
                result.append(Comp.Image.fromFileSystem(media_path))
            elif segment.type in {"image", "sticker"}:
                label = "表情" if segment.type == "sticker" else "图片"
                result.append(Comp.Plain(f"[{label}资源缺失]"))
            elif segment.type == "face" and segment.face_id:
                result.append(Comp.Face(id=int(segment.face_id)))
        return result

    def _reply_to_components(
        self,
        reply: ReplySnapshot | None,
        *,
        native_stickers: bool,
        native_replies: bool,
    ) -> list[Any]:
        if reply is None:
            return []
        if not native_replies:
            return []
        if not reply.source_message_id:
            raise ValueError("回复快照缺少原始消息 ID")
        return [
            _OneBotReply(
                id=reply.source_message_id,
                seq=reply.source_message_seq or 0,
            )
        ]

    def _reply_fallback_nodes(
        self,
        reply: ReplySnapshot,
        *,
        native_stickers: bool,
    ) -> list[Any]:
        result: list[Any] = []
        if reply.reply:
            result.extend(
                self._reply_fallback_nodes(
                    reply.reply,
                    native_stickers=native_stickers,
                )
            )
        content = self._segments_to_components(
            reply.segments,
            native_stickers=native_stickers,
        )
        if reply.truncated:
            content.append(Comp.Plain("更早的回复已省略"))
        if reply.incomplete:
            content.append(Comp.Plain("[回复内容来自消息内嵌快照]"))
        if content:
            result.append(
                Comp.Node(
                    uin=reply.author.user_id or "0",
                    name=reply.author.nickname or reply.author.user_id or "未知用户",
                    content=content,
                )
            )
        return result

    @staticmethod
    def _record_time(value: str) -> str:
        try:
            return (
                datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            )
        except (TypeError, ValueError):
            return "记录时间未知"
