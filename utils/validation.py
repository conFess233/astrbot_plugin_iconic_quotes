"""输入、图片及 CSS 的安全校验。"""

from __future__ import annotations

import re
from pathlib import Path

IMAGE_SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
)


def identify_image(data: bytes) -> tuple[str, str]:
    """依据真实文件头识别允许保存的图片。"""
    for signature, extension, mime in IMAGE_SIGNATURES:
        if data.startswith(signature):
            return extension, mime
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    raise ValueError("仅支持 JPEG、PNG、WebP 和 GIF 图片")


def safe_storage_path(data_root: Path, relative_value: str) -> Path:
    """解析不允许逃逸插件数据根目录的相对存储路径。"""
    value = relative_value.strip().replace("\\", "/")
    candidate = Path(value)
    if not value or candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("存储路径必须是插件数据目录下的安全相对路径")
    root = data_root.resolve()
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise ValueError("存储路径不能越出 AstrBot 插件数据目录")
    return resolved


def validate_numeric_id(value: object, label: str = "ID") -> str:
    """校验 OneBot 使用的正整数 ID。"""
    text = str(value or "").strip()
    if not text.isdigit() or int(text) <= 0:
        raise ValueError(f"{label} 必须是正整数")
    return text


def sanitize_custom_css(value: str) -> str:
    """拒绝可能加载外部资源或破坏页面边界的 CSS。"""
    if len(value) > 100_000:
        raise ValueError("自定义 CSS 不能超过 100000 个字符")
    lowered = re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL).casefold()
    forbidden = ("@import", "url(", "expression(", "javascript:", "</style")
    if any(token in lowered for token in forbidden):
        raise ValueError("自定义 CSS 不允许导入或引用外部资源")
    return value
