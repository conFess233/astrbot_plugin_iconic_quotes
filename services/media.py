"""全局内容寻址的群典媒体文件。

文件位于 ``media/<sha256 前两位>/<sha256>.<扩展名>``，数据库只记录哈希、类型与大小。
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

from ..utils.validation import MIME_EXTENSIONS, identify_image

KEEP_ORIGINAL_BYTES = 512 * 1024


@dataclass(slots=True, frozen=True)
class MediaInfo:
    sha256: str
    mime: str
    size: int


def downscale(data: bytes, max_width: int, max_height: int) -> tuple[bytes, str | None]:
    """把图片缩小到给定范围，返回 (数据, MIME)；MIME 为 None 表示原样返回。

    只用于展示，不改动存档原图。尺寸和体积都不超标时不重新编码；
    动图只取首帧，因为卡片截图和缩略图都是静态的。解析失败时交回原图。
    """
    try:
        with Image.open(io.BytesIO(data)) as source:
            if (
                source.width <= max_width
                and source.height <= max_height
                and len(data) <= KEEP_ORIGINAL_BYTES
            ):
                return data, None
            source.seek(0)
            source.load()
            image = ImageOps.exif_transpose(source)
            image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            if image.mode in {"RGBA", "LA", "P"}:
                image.convert("RGBA").save(output, format="PNG", optimize=True)
                return output.getvalue(), "image/png"
            image.convert("RGB").save(output, format="JPEG", quality=85, optimize=True)
            return output.getvalue(), "image/jpeg"
    except Exception:  # noqa: BLE001 - Pillow 对异常图片的报错类型不统一，展示时退回原图。
        return data, None


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

    def scaled_data_url(
        self, sha256: str, mime: str | None, max_width: int, max_height: int
    ) -> str:
        """按显示尺寸缩小后的 data URL；图片已足够小时返回原图。"""
        data, actual = downscale(self.read(sha256, mime), max_width, max_height)
        encoded = base64.b64encode(data).decode()
        return f"data:{actual or mime or 'image/png'};base64,{encoded}"

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
