"""普通文字或图片群典的卡片渲染。"""

from __future__ import annotations

import asyncio
import html
from pathlib import Path
from typing import Any

from ..models import QuoteRecord
from ..utils.timefmt import local_time
from .avatar_cache import AvatarCacheService
from .media import MediaStore
from .t2i import T2IClient

TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "resources" / "templates" / "card.html"
)
TEXT_PER_PAGE = 1200
IMAGES_PER_PAGE = 4
# 按单页正文长度选择字号档位，短句更醒目，长文更紧凑。
TEXT_SIZES = ((24, "xl"), (90, "lg"), (300, "md"))


def text_size(text: str) -> str:
    length = len(text.strip())
    return next((size for limit, size in TEXT_SIZES if length <= limit), "sm")


class CardRenderer:
    """按文字和图片数量拆页，交给文转图服务渲染。"""

    def __init__(self, t2i: T2IClient, media: MediaStore, avatars: AvatarCacheService):
        self.t2i = t2i
        self.media = media
        self.avatars = avatars
        self._template: str | None = None

    async def card_paths(
        self, record: QuoteRecord, settings: dict[str, Any]
    ) -> list[str]:
        """返回临时 PNG 路径；调用方负责删除。"""
        if record.type != "message" or record.author is None:
            raise ValueError("卡片只适用于普通群典")
        if self._template is None:
            self._template = await asyncio.to_thread(TEMPLATE_PATH.read_text, "utf-8")
        text = "".join(
            segment.text or "" for segment in record.segments if segment.type == "text"
        ).strip()
        images = await asyncio.to_thread(
            lambda: [
                self.media.data_url(segment.sha256, segment.mime)
                for segment in record.segments
                if segment.type == "image" and segment.sha256
            ]
        )
        text_pages = [
            text[index : index + TEXT_PER_PAGE]
            for index in range(0, len(text), TEXT_PER_PAGE)
        ] or [""]
        image_pages = [
            images[index : index + IMAGES_PER_PAGE]
            for index in range(0, len(images), IMAGES_PER_PAGE)
        ] or [[]]
        pages = max(len(text_pages), len(image_pages))
        avatar = await self.avatars.data_url(record.author.user_id, settings)
        recorded = local_time(record.recorded_at, "%Y-%m-%d %H:%M")
        paths: list[str] = []
        try:
            for index in range(pages):
                page_text = text_pages[index] if index < len(text_pages) else ""
                data = {
                    "width": settings["card_width"],
                    "min_height": 0
                    if settings["card_auto_height"]
                    else settings["card_min_height"],
                    "max_height": settings["card_max_height"],
                    "theme": settings["card_theme"],
                    "size": text_size(page_text),
                    "custom_css": settings["card_custom_css"],
                    "avatar": avatar,
                    "text": html.escape(page_text),
                    "images": image_pages[index] if index < len(image_pages) else [],
                    "nickname": html.escape(record.author.display_name),
                    "recorded_at": f"记录于 {recorded}" if recorded else "记录时间未知",
                    "page": index + 1,
                    "pages": pages,
                }
                paths.append(await self.t2i.render(self._template, data, settings))
        except BaseException:
            for path in paths:
                await asyncio.to_thread(Path(path).unlink, missing_ok=True)
            raise
        return paths
