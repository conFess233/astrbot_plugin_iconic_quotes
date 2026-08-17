"""卡片临时图片的保守后处理。"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageChops


def trim_card_canvas(path_value: str) -> bool:
    """在卡片贴合左上角时裁去右侧、底部的连续纯色画布。"""
    path = Path(path_value)
    temporary = path.with_name(f".{path.name}.trimmed")
    try:
        with Image.open(path) as source:
            image = source.convert("RGBA")
            image.load()
            image_format = source.format or "PNG"
        width, height = image.size
        if width < 2 or height < 2:
            return False
        comparison = image.convert("RGB")
        background = Image.new(
            "RGB",
            comparison.size,
            comparison.getpixel((width - 1, height - 1)),
        )
        bounds = ImageChops.difference(comparison, background).getbbox()
        if not bounds:
            return False
        left, top, right, bottom = bounds
        content_width = right - left
        content_height = bottom - top
        boundary_is_confident = (
            left <= 16
            and top <= 16
            and content_width >= min(320, width * 0.25)
            and content_height >= min(160, height * 0.25)
        )
        if not boundary_is_confident:
            return False
        target_width = min(width, right + 2) if width - right >= 8 else width
        target_height = min(height, bottom + 2) if height - bottom >= 8 else height
        if target_width == width and target_height == height:
            return False
        image.crop((0, 0, target_width, target_height)).save(
            temporary,
            format=image_format,
        )
        os.replace(temporary, path)
        return True
    except (OSError, ValueError):
        return False
    finally:
        temporary.unlink(missing_ok=True)
