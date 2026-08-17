"""群典文字、图片卡片与合并转发消息构造。"""

from __future__ import annotations

import asyncio
import base64
import html
import time
from datetime import datetime
from typing import Any

import aiohttp
import astrbot.api.message_components as Comp

from ..models import QuoteRecord, QuoteSegment
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
.content { min-width: 0; display: flex; flex-direction: column; }
.quote { flex: 1; font-size: 42px; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
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


class QuoteRenderer:
    """创建跨 Handler 可复用的消息链和卡片。"""

    def __init__(self, plugin: Any, storage: QuoteStorage):
        self.plugin = plugin
        self.storage = storage
        self.http: aiohttp.ClientSession | None = None
        self._avatar_cache: dict[str, tuple[float, bytes | None]] = {}

    async def initialize(self) -> None:
        """在插件异步生命周期内创建 HTTP Client。"""
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6))

    async def close(self) -> None:
        """释放头像 HTTP Client。"""
        if self.http and not self.http.closed:
            await self.http.close()

    async def avatar_data_url(self, user_id: str | None) -> str:
        """实时取得 QQ 头像；失败时返回空白且不落盘。"""
        if not user_id:
            return ""
        cached = self._avatar_cache.get(user_id)
        if not cached or cached[0] <= time.monotonic():
            try:
                if self.http is None or self.http.closed:
                    await self.initialize()
                url = f"https://q1.qlogo.cn/g?b=qq&nk={int(user_id)}&s=640"
                assert self.http is not None
                async with self.http.get(
                    url, allow_redirects=True, max_redirects=3
                ) as response:
                    response.raise_for_status()
                    data = await response.read()
                    if len(data) > 2 * 1024 * 1024:
                        raise ValueError("头像文件过大")
                    self._avatar_cache[user_id] = (time.monotonic() + 300, data)
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError):
                self._avatar_cache[user_id] = (time.monotonic() + 60, None)
        data = self._avatar_cache[user_id][1]
        return (
            f"data:image/jpeg;base64,{base64.b64encode(data).decode()}" if data else ""
        )

    def text_chain(self, record: QuoteRecord) -> list[Any]:
        """构造保留原图顺序的文字发送链。"""
        if record.type != "message" or record.author is None:
            raise ValueError("文字链只适用于普通群典")
        text = "".join(
            segment.text or "" for segment in record.segments if segment.type == "text"
        )
        nickname = record.author.nickname or record.author.user_id or "未知用户"
        chain: list[Any] = []
        if text:
            chain.append(Comp.Plain(f"“{text}”——{nickname}"))
        for segment in record.segments:
            if segment.type == "image" and segment.path:
                chain.append(
                    Comp.Image.fromFileSystem(
                        self.storage.resolve_media_path(segment.path)
                    )
                )
        if not text:
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
        avatar = await self.avatar_data_url(record.author.user_id)
        paths: list[str] = []
        for index in range(page_count):
            data = {
                "width": settings["card_width"],
                "min_height": settings["card_min_height"],
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
        self, records: list[QuoteRecord], *, replay: bool = False
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
                            content=self._segments_to_components(node.segments),
                        )
                    )
                continue
            assert record.author is not None
            text = "".join(
                segment.text or ""
                for segment in record.segments
                if segment.type == "text"
            )
            nickname = record.author.nickname or record.author.user_id or "未知用户"
            content: list[Any] = []
            if text:
                content.append(Comp.Plain(f"“{text}”——{nickname}"))
            content.extend(
                Comp.Image.fromFileSystem(self.storage.resolve_media_path(segment.path))
                for segment in record.segments
                if segment.type == "image" and segment.path
            )
            nodes.append(Comp.Node(uin="0", name="群典", content=content))
        return nodes

    def _segments_to_components(self, segments: list[QuoteSegment]) -> list[Any]:
        result: list[Any] = []
        for segment in segments:
            if segment.type == "text" and segment.text:
                result.append(Comp.Plain(segment.text))
            elif segment.type == "image" and segment.path:
                result.append(
                    Comp.Image.fromFileSystem(
                        self.storage.resolve_media_path(segment.path)
                    )
                )
        return result
