"""全局内容寻址的群典媒体文件。

文件位于 ``media/<sha256 前两位>/<sha256>.<扩展名>``，数据库只记录哈希、类型与大小。
"""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..utils.validation import MIME_EXTENSIONS, identify_image


@dataclass(slots=True, frozen=True)
class MediaInfo:
    sha256: str
    mime: str
    size: int


class MediaStore:
    """只负责文件读写与校验；引用计数和容量由 SQLite 记录。"""

    def __init__(self, root: Path):
        self.root = root

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sha256: str, mime: str | None) -> Path:
        sha256 = sha256.lower()
        if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
            raise ValueError("媒体哈希无效")
        extension = MIME_EXTENSIONS.get(mime or "", ".bin")
        return self.root / sha256[:2] / f"{sha256}{extension}"

    def write(self, data: bytes) -> tuple[MediaInfo, bool]:
        """校验格式并写入；返回媒体信息与本次是否新建了文件。"""
        if not data:
            raise ValueError("图片内容为空")
        _, mime = identify_image(data)
        info = MediaInfo(hashlib.sha256(data).hexdigest(), mime, len(data))
        target = self.path(info.sha256, mime)
        if target.is_file() and target.stat().st_size == info.size:
            return info, False
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=target.parent, prefix=".media-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return info, True

    def quick_ok(self, sha256: str, mime: str | None, size: int | None) -> bool:
        """发送前的廉价检查：文件存在且大小一致。"""
        try:
            stat = self.path(sha256, mime).stat()
        except (OSError, ValueError):
            return False
        return size is None or stat.st_size == size

    def verify(self, sha256: str, mime: str | None) -> bool:
        """完整性检查：重新计算 SHA-256。"""
        try:
            data = self.path(sha256, mime).read_bytes()
        except (OSError, ValueError):
            return False
        return hashlib.sha256(data).hexdigest() == sha256

    def read(self, sha256: str, mime: str | None) -> bytes:
        return self.path(sha256, mime).read_bytes()

    def data_url(self, sha256: str, mime: str | None) -> str:
        encoded = base64.b64encode(self.read(sha256, mime)).decode()
        return f"data:{mime or 'image/png'};base64,{encoded}"

    def delete(self, sha256: str, mime: str | None) -> None:
        try:
            self.path(sha256, mime).unlink(missing_ok=True)
        except (OSError, ValueError):
            return

    def files(self) -> list[tuple[str, Path]]:
        """磁盘上全部媒体文件（哈希, 路径），用于清理孤儿文件。"""
        if not self.root.is_dir():
            return []
        return [
            (path.stem.lower(), path)
            for path in self.root.glob("*/*")
            if path.is_file() and not path.name.startswith(".")
        ]
