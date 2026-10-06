"""输入、图片及 CSS 的安全校验。"""

from __future__ import annotations

import re
from collections.abc import Iterable
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


def match_command_syntax(
    plain_text: str,
    keywords: list[str],
) -> tuple[bool, str | None]:
    """完整匹配可选斜杠的关键词，并返回清理空白后的参数尾部。"""
    for keyword in sorted(keywords, key=len, reverse=True):
        match = re.fullmatch(
            rf"/?{re.escape(keyword)}(?:\s+(.+))?",
            plain_text.strip(),
        )
        if match:
            tail = (match.group(1) or "").strip()
            return True, tail or None
    return False, None


def parse_command_segments(
    segments: Iterable[dict], bot_id: str
) -> tuple[str, list[str]]:
    """允许单个目标 @ 位于关键词前，多个成员 @ 不组成调用前缀。

    >>> target = {"type": "at", "data": {"qq": "123"}}
    >>> command = {"type": "text", "data": {"text": "群典"}}
    >>> parse_command_segments([target, command], "999")
    ('群典', ['123'])
    >>> parse_command_segments([target, target, command], "999")
    ('', [])
    >>> chat = {"type": "text", "data": {"text": "普通消息"}}
    >>> match_command_syntax(parse_command_segments([target, chat], "999")[0], ["群典"])
    (False, None)
    """
    parts: list[str] = []
    targets: list[str] = []
    started = False
    addressed_bot = False
    prefixed_target = False
    for segment in segments:
        data = segment.get("data", {})
        if not isinstance(data, dict):
            return "", []
        kind = segment.get("type")
        if kind == "text":
            text = data.get("text", "")
            if not isinstance(text, str):
                return "", []
            started = started or bool(text.strip())
            parts.append(text)
        elif kind == "at":
            target = str(data.get("qq", ""))
            if not started:
                if not bot_id or target in {"", "all"}:
                    return "", []
                if target == bot_id:
                    if addressed_bot:
                        return "", []
                    addressed_bot = True
                else:
                    if prefixed_target:
                        return "", []
                    prefixed_target = True
                    targets.append(target)
            elif target not in {"", "all", bot_id} and target not in targets:
                targets.append(target)
            parts.append(" ")
        elif not started and kind != "reply":
            return "", []
        else:
            # 非文本段分隔两侧正文，不能把碎片拼成新的关键词。
            parts.append(" ")
    return "".join(parts).strip(), targets


def sanitize_custom_css(value: str) -> str:
    """拒绝可能加载外部资源或破坏页面边界的 CSS。"""
    if len(value) > 100_000:
        raise ValueError("自定义 CSS 不能超过 100000 个字符")
    lowered = re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL).casefold()
    forbidden = ("@import", "url(", "expression(", "javascript:", "</style")
    if any(token in lowered for token in forbidden):
        raise ValueError("自定义 CSS 不允许导入或引用外部资源")
    return value
