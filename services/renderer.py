"""群典文字、图片卡片与合并转发消息构造。"""

from __future__ import annotations

import asyncio
import base64
import html
from datetime import datetime
from typing import Any

import astrbot.api.message_components as Comp

from ..models import QuoteRecord, QuoteSegment, ReplySnapshot
from ..utils.image_processing import trim_card_canvas
from .avatar_cache import AvatarCacheService
from .storage import QuoteStorage

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
    ) -> list[Any]:
        """构造保留原图顺序的文字发送链。"""
        if record.type != "message" or record.author is None:
            raise ValueError("文字链只适用于普通群典")
        nickname = record.author.nickname or record.author.user_id or "未知用户"
        chain = self._reply_to_components(
            record.reply,
            native_stickers=native_stickers,
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
    ) -> list[Any]:
        """构造新聚合转发或原生转发回放节点。"""
        nodes: list[Any] = []
        if replay:
            nodes.append(
                Comp.Node(uin="0", name="群典", content=[Comp.Plain("群典存档回放")])
            )
        for record in records:
            if record.type == "forward":
                for node in record.nodes:
                    nodes.append(
                        Comp.Node(
                            uin=node.author.user_id or "0",
                            name=node.author.nickname or "未知用户",
                            content=self._reply_to_components(
                                node.reply,
                                native_stickers=native_stickers,
                            )
                            + self._segments_to_components(
                                node.segments,
                                native_stickers=native_stickers,
                            ),
                        )
                    )
                continue
            assert record.author is not None
            nickname = record.author.nickname or record.author.user_id or "未知用户"
            content = self._reply_to_components(
                record.reply,
                native_stickers=native_stickers,
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

    def burst_nodes(
        self,
        records: list[QuoteRecord],
        *,
        target_name: str,
        total: int,
        page: int,
        pages: int,
        skipped: int,
        nested: bool,
        native_stickers: bool,
        time_mode: str,
    ) -> list[Any]:
        """构造按时间排列的个人完整群典合集。"""
        title = f"聊天记录：{target_name}\n"
        title += (
            f"共 {total} 条｜第 {page} / {pages} 页" if pages > 1 else f"共 {total} 条"
        )
        if skipped:
            title += f"\n另有 {skipped} 条记录因资源不完整已跳过"
        result: list[Any] = [
            Comp.Node(uin="0", name="群典", content=[Comp.Plain(title)])
        ]
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
                content.extend(
                    self._reply_to_components(
                        record.reply,
                        native_stickers=native_stickers,
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
            inner = [
                Comp.Node(
                    uin=node.author.user_id or "0",
                    name=node.author.nickname or node.author.user_id or "未知用户",
                    content=self._reply_to_components(
                        node.reply,
                        native_stickers=native_stickers,
                    )
                    + self._segments_to_components(
                        node.segments,
                        native_stickers=native_stickers,
                    ),
                )
                for node in record.nodes
            ]
            if nested:
                result.append(
                    self._burst_node(
                        uin="0",
                        name="聊天记录存档",
                        content=inner,
                        native_time=native_time,
                        time_mode=time_mode,
                    )
                )
            else:
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
                result.append(
                    Comp.Image.fromFileSystem(
                        self.storage.resolve_media_path(segment.path)
                    )
                )
            elif segment.type == "face" and segment.face_id:
                result.append(Comp.Face(id=int(segment.face_id)))
        return result

    def _reply_to_components(
        self,
        reply: ReplySnapshot | None,
        *,
        native_stickers: bool,
    ) -> list[Any]:
        if reply is None:
            return []
        name = reply.author.nickname or reply.author.user_id or "未知用户"
        result: list[Any] = [Comp.Plain(f"↩ 回复 {name}\n┌ ")]
        result.extend(
            self._segments_to_components(
                reply.segments,
                native_stickers=native_stickers,
            )
        )
        if reply.reply:
            result.append(Comp.Plain("\n"))
            result.extend(
                self._reply_to_components(
                    reply.reply,
                    native_stickers=native_stickers,
                )
            )
        if reply.truncated:
            result.append(Comp.Plain("\n更早的回复已省略"))
        result.append(Comp.Plain("\n└\n"))
        return result

    @staticmethod
    def _record_time(value: str) -> str:
        try:
            return (
                datetime.fromisoformat(value).astimezone().strftime("%Y-%m-%d %H:%M:%S")
            )
        except (TypeError, ValueError):
            return "记录时间未知"
