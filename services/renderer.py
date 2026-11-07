"""普通文字或图片群典的卡片渲染。"""

from __future__ import annotations

import asyncio
import html
import math
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..models import QuoteRecord
from ..utils.help import help_card_data
from ..utils.timefmt import local_time
from .avatar_cache import AvatarCacheService
from .media import MediaStore
from .t2i import T2IClient

TEMPLATES = Path(__file__).resolve().parent.parent / "resources" / "templates"
TEXT_PER_PAGE = 1200
IMAGES_PER_PAGE = 4
# 与 card_scale 对应的设备像素比；图片缩放后在该清晰度下仍然锐利。
CARD_SCALES = {"normal": 1.0, "high": 1.3, "ultra": 1.8}
MAX_IMAGE_ASPECT = 3
# 按单页正文长度选择字号档位，短句更醒目，长文更紧凑。
TEXT_SIZES = ((24, "xl"), (90, "lg"), (300, "md"))
# 以下尺寸与 card.html 保持一致，单位 rem（1rem = 卡片宽度 / 40）。
ROOT_DIVISOR = 40
CONTENT_REM = 33.2
# 上下内边距 6、引文上边距 1.6、页脚 6.25。
CHROME_REM = 13.85
IMAGE_MARGIN_REM = 1.5
IMAGE_HEIGHT_REM = {1: 30.0, 2: 16.3, 3: 10.7, 4: 33.2}
# 档位 → (字号 em, 行高)，从大到小。
FONT_METRICS = {
    "xl": (2.15, 1.55),
    "lg": (1.6, 1.7),
    "md": (1.22, 1.8),
    "sm": (1.02, 1.85),
}
LETTER_SPACING_EM = 0.02
NARROW_CHAR_EM = 0.6
MIN_PAGE_LINES = 3


def text_size(text: str) -> str:
    length = len(text.strip())
    return next((size for limit, size in TEXT_SIZES if length <= limit), "sm")


def _char_width(char: str, font_em: float) -> float:
    wide = unicodedata.east_asian_width(char) in {"W", "F"}
    return ((1.0 if wide else NARROW_CHAR_EM) + LETTER_SPACING_EM) * font_em


def _advance(char: str, x: float, lines: int, font_em: float) -> tuple[float, int]:
    """把一个字符排进当前行，返回新的行内位置和行数。"""
    if char == "\n":
        return 0.0, lines + 1
    width = _char_width(char, font_em)
    if x + width > CONTENT_REM:
        return width, lines + 1
    return x + width, lines


def text_height(text: str, size: str) -> float:
    """估算正文高度（rem）。只用于分页和选字号，略有误差也不会裁切内容。"""
    font_em, line_height = FONT_METRICS[size]
    x, lines = 0.0, 1
    for char in text:
        x, lines = _advance(char, x, lines, font_em)
    return lines * font_em * line_height


def fit_size(text: str, available_rem: float) -> str:
    """按字数选字号；换行很多放不下时逐档缩小。"""
    sizes = list(FONT_METRICS)
    for size in sizes[sizes.index(text_size(text)) :]:
        if text_height(text, size) <= available_rem:
            return size
    return sizes[-1]


def split_text(text: str, available: Callable[[int], float]) -> list[str]:
    """按估算高度和字数上限分页；只有分页处的换行符会被去掉，不丢失其他内容。"""
    font_em, line_height = FONT_METRICS["sm"]
    line_rem = font_em * line_height
    pages: list[str] = []
    start = 0
    x, lines = 0.0, 1
    limit = max(MIN_PAGE_LINES, int(available(0) / line_rem))
    for index, char in enumerate(text):
        x, lines = _advance(char, x, lines, font_em)
        if lines > limit or index - start >= TEXT_PER_PAGE:
            pages.append(text[start:index].strip("\n"))
            start = index
            x, lines = _advance(char, 0.0, 1, font_em)
            limit = max(MIN_PAGE_LINES, int(available(len(pages)) / line_rem))
    pages.append(text[start:].strip("\n"))
    return [page for page in pages if page] or [""]


class CardRenderer:
    """按文字和图片数量拆页，交给文转图服务渲染。"""

    def __init__(self, t2i: T2IClient, media: MediaStore, avatars: AvatarCacheService):
        self.t2i = t2i
        self.media = media
        self.avatars = avatars
        self._templates: dict[str, str] = {}

    async def _template(self, name: str) -> str:
        if name not in self._templates:
            self._templates[name] = await asyncio.to_thread(
                (TEMPLATES / name).read_text, "utf-8"
            )
        return self._templates[name]

    def _page_images(self, segments: list[Any], settings: dict[str, Any]) -> list[str]:
        """按卡片版式需要的像素缩小图片，避免把原图整张发给文转图服务。"""
        columns = {1: 1, 2: 2, 3: 3}.get(len(segments), 2)
        width = math.ceil(
            settings["card_width"] * CARD_SCALES[settings["card_scale"]] / columns
        )
        return [
            self.media.scaled_data_url(
                segment.sha256, segment.mime, width, width * MAX_IMAGE_ASPECT
            )
            for segment in segments
        ]

    async def help_card_path(self, overview: str, settings: dict[str, Any]) -> str:
        """把渲染后的帮助文本生成一张卡片；返回临时 PNG 路径，调用方负责删除。"""
        data = {
            "width": settings["card_width"],
            "theme": settings["card_theme"],
            **help_card_data(overview),
        }
        return await self.t2i.render(await self._template("help.html"), data, settings)

    async def card_paths(
        self, record: QuoteRecord, settings: dict[str, Any]
    ) -> list[str]:
        """返回临时 PNG 路径；调用方负责删除。"""
        if record.type != "message" or record.author is None:
            raise ValueError("卡片只适用于普通群典")
        template = await self._template("card.html")
        text = "".join(
            segment.text or "" for segment in record.segments if segment.type == "text"
        ).strip()
        segments = [
            segment
            for segment in record.segments
            if segment.type == "image" and segment.sha256
        ]
        chunks = [
            segments[index : index + IMAGES_PER_PAGE]
            for index in range(0, len(segments), IMAGES_PER_PAGE)
        ]
        image_pages = await asyncio.to_thread(
            lambda: [self._page_images(chunk, settings) for chunk in chunks]
        ) or [[]]
        rem = settings["card_width"] / ROOT_DIVISOR

        def available(page: int) -> float:
            """这一页扣除版式和图片后留给正文的高度（rem）。"""
            height = settings["card_max_height"] / rem - CHROME_REM
            if page < len(chunks):
                height -= IMAGE_MARGIN_REM + IMAGE_HEIGHT_REM[len(chunks[page])]
            return height

        text_pages = split_text(text, available)
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
                    "size": fit_size(page_text, available(index)),
                    "custom_css": settings["card_custom_css"],
                    "avatar": avatar,
                    "text": html.escape(page_text),
                    "images": image_pages[index] if index < len(image_pages) else [],
                    "nickname": html.escape(record.author.display_name),
                    "recorded_at": f"记录于 {recorded}" if recorded else "记录时间未知",
                    "page": index + 1,
                    "pages": pages,
                }
                paths.append(await self.t2i.render(template, data, settings))
        except BaseException:
            for path in paths:
                await asyncio.to_thread(Path(path).unlink, missing_ok=True)
            raise
        return paths
