"""卡片临时图片的保守后处理。"""

from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageChops


def trim_card_canvas(path_value: str) -> bool:
    """在边界可信时只裁去底部连续纯色画布，并返回是否发生裁剪。"""
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
        boundary_is_confident = (
            top <= 16
            and right - left >= width * 0.7
            and height - bottom >= 8
        )
        if not boundary_is_confident:
            return False
        image.crop((0, 0, width, min(height, bottom + 2))).save(
            temporary,
            format=image_format,
        )
        os.replace(temporary, path)
        return True
    except (OSError, ValueError):
        return False
    finally:
        temporary.unlink(missing_ok=True)
