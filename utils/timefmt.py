"""时间格式化与异常摘要。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def utc_iso(timestamp: Any = None) -> str | None:
    """把 OneBot 秒级时间戳转换为 UTC ISO 字符串；无效值返回 None。"""
    if timestamp in (None, "", 0, "0"):
        return None
    try:
        return datetime.fromtimestamp(float(timestamp), UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def local_time(value: str | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str | None:
    """按本地时区显示 ISO 时间；无法解析时返回 None。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).astimezone().strftime(fmt)
    except (TypeError, ValueError):
        return None


def unix_seconds(value: str | None) -> int:
    """ISO 时间转秒级时间戳；无法解析时返回 0。"""
    try:
        return int(datetime.fromisoformat(value or "").timestamp())
    except (TypeError, ValueError):
        return 0


def error_reason(error: BaseException | None) -> str:
    """生成适合日志的异常摘要，不包含消息正文或完整载荷。"""
    if error is None:
        return "未返回具体异常"
    detail = str(error).strip()
    return f"{type(error).__name__}: {detail}" if detail else type(error).__name__
