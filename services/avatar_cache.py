"""QQ 头像的可选本地缓存与媒体容量管理。

头像文件位于 ``avatars/<QQ>.jpg``，索引（获取时间、最近使用、大小）保存在 SQLite。
群典图片不可再生，容量不足时只淘汰头像：无引用优先，其次最久未使用。
"""

from __future__ import annotations

import asyncio
import base64
import io
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import aiohttp
from astrbot.api import logger
from PIL import Image, ImageOps, UnidentifiedImageError

from .storage import QuoteStorage

AVATAR_SIZE = 160
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
TOUCH_FLUSH_SECONDS = 60
BLANK_AVATAR = (
    Path(__file__).resolve().parent.parent / "resources" / "images" / "blank_avatar.svg"
)


def _normalize_user_id(user_id: str | None) -> str:
    value = str(user_id or "").strip()
    return value if value.isdigit() else ""


def _normalize_image(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        image = ImageOps.exif_transpose(source).convert("RGB")
        image = ImageOps.fit(
            image, (AVATAR_SIZE, AVATAR_SIZE), method=Image.Resampling.LANCZOS
        )
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=88, optimize=True)
        return output.getvalue()


def _validated_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    with Image.open(io.BytesIO(data)) as image:
        image.verify()
    return data


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".avatar-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def data_url(data: bytes, mime: str = "image/jpeg") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


class AvatarCacheService:
    """按 QQ 号缓存一份头像，并负责整个媒体目录的容量预留。"""

    def __init__(self, storage: QuoteStorage, directory: Path):
        self.storage = storage
        self.directory = directory
        self.http: aiohttp.ClientSession | None = None
        self._index: dict[str, dict[str, float]] = {}
        self._dirty: set[str] = set()
        self._memory: dict[str, tuple[float, bytes | None]] = {}
        self._lock = asyncio.Lock()
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._next_flush = 0.0

    async def initialize(self) -> None:
        """创建目录、载入索引并建立可复用 HTTP Client。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        rows = await self.storage.avatar_rows()
        self._index = {
            user_id: {
                "fetched_at": float(row["fetched_at"]),
                "last_used": float(row["last_used"]),
                "size": int(row["size"]),
            }
            for user_id, row in rows.items()
        }
        # 索引与磁盘不一致时以文件为准：缺文件的索引删除，未登记的文件补登记。
        on_disk = {
            path.stem: path for path in self.directory.glob("*.jpg") if path.is_file()
        }
        stale = [user_id for user_id in self._index if user_id not in on_disk]
        for user_id in stale:
            self._index.pop(user_id)
        await self.storage.avatar_delete(stale)
        now = time.time()
        for user_id, path in on_disk.items():
            if user_id not in self._index and _normalize_user_id(user_id):
                self._index[user_id] = {
                    "fetched_at": 0.0,
                    "last_used": now,
                    "size": path.stat().st_size,
                }
                self._dirty.add(user_id)
        await self._flush(force=True)
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6))

    async def close(self) -> None:
        """取消后台刷新、写回使用时间并关闭 HTTP Client。"""
        tasks = tuple(self._refresh_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_tasks.clear()
        try:
            await self._flush(force=True)
        finally:
            if self.http and not self.http.closed:
                await self.http.close()

    # 读取 -------------------------------------------------------------

    async def blank_data_url(self) -> str:
        data = await asyncio.to_thread(BLANK_AVATAR.read_bytes)
        return data_url(data, "image/svg+xml")

    async def data_url(self, user_id: str | None, settings: dict[str, Any]) -> str:
        """卡片头像；本地化关闭时只使用短期内存缓存。"""
        normalized = _normalize_user_id(user_id)
        if not normalized:
            return await self.blank_data_url()
        if not settings["localize_avatars"]:
            data = await self._online(normalized, settings)
            return data_url(data) if data else await self.blank_data_url()
        data = await self._cached_bytes(normalized, settings)
        if data is None:
            data = await self._download_and_store(normalized, settings)
        return data_url(data) if data else await self.blank_data_url()

    async def cached_data_url(self, user_id: str | None) -> str:
        """仅返回已有本地头像或占位图，不触发网络下载。"""
        normalized = _normalize_user_id(user_id)
        if normalized:
            data = await self._cached_bytes(normalized, None)
            if data is not None:
                return data_url(data)
        return await self.blank_data_url()

    async def prefetch(self, user_id: str | None, settings: dict[str, Any]) -> None:
        """新收录普通金句后尽力补全头像，失败不影响收录结果。"""
        try:
            normalized = _normalize_user_id(user_id)
            if not normalized or not settings["localize_avatars"]:
                return
            if await self._cached_bytes(normalized, settings) is None:
                await self._download_and_store(normalized, settings)
        except Exception:  # noqa: BLE001 - 头像失败不能改变已经完成的收录。
            logger.warning("群典：头像预缓存失败")

    async def _cached_bytes(
        self, user_id: str, settings: dict[str, Any] | None
    ) -> bytes | None:
        path = self._path(user_id)
        if not path.is_file():
            return None
        try:
            data = await asyncio.to_thread(_validated_bytes, path)
        except (OSError, UnidentifiedImageError, ValueError):
            await self._delete([user_id])
            return None
        await self._touch(user_id, len(data))
        if settings is not None and self._expired(
            user_id, settings["avatar_cache_ttl_days"]
        ):
            self._schedule_refresh(user_id, settings)
        return data

    # 统计与清理 -------------------------------------------------------

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            return {
                "count": len(self._index),
                "bytes": int(sum(item["size"] for item in self._index.values())),
            }

    async def cleanup_unreferenced(self) -> dict[str, int]:
        """删除没有任何群典作者引用的头像。"""
        referenced = await self.storage.referenced_user_ids()
        return await self._delete(
            [uid for uid in list(self._index) if uid not in referenced]
        )

    async def clear(self) -> dict[str, int]:
        """清空全部可再生头像缓存。"""
        self._memory.clear()
        return await self._delete(list(self._index))

    async def reserve(
        self, incoming: int, limit: int, *, protect: str | None = None
    ) -> bool:
        """确保再写入 incoming 字节后不超过 limit；必要时按 LRU 淘汰头像。"""
        await self._flush(force=True)
        usage = await self.storage.media_usage()
        if usage + incoming <= limit:
            return True
        referenced = await self.storage.referenced_user_ids()
        async with self._lock:
            candidates = sorted(
                (uid for uid in self._index if uid != protect),
                key=lambda uid: (uid in referenced, self._index[uid]["last_used"]),
            )
            freed_target = usage + incoming - limit
            victims: list[str] = []
            freed = 0
            for uid in candidates:
                if freed >= freed_target:
                    break
                victims.append(uid)
                freed += int(self._index[uid]["size"])
        if victims:
            await self._delete(victims)
        return usage - freed + incoming <= limit

    # 内部 -------------------------------------------------------------

    def _path(self, user_id: str) -> Path:
        return self.directory / f"{user_id}.jpg"

    def _expired(self, user_id: str, ttl_days: int) -> bool:
        if ttl_days == 0:
            return False
        fetched_at = self._index.get(user_id, {}).get("fetched_at", 0.0)
        return fetched_at + ttl_days * 86400 <= time.time()

    async def _touch(self, user_id: str, size: int) -> None:
        async with self._lock:
            item = self._index.setdefault(
                user_id, {"fetched_at": 0.0, "last_used": 0.0, "size": size}
            )
            item["last_used"] = time.time()
            item["size"] = size
            self._dirty.add(user_id)
        await self._flush()

    async def _flush(self, *, force: bool = False) -> None:
        """最近使用时间批量写回，避免每次读取头像都写库。"""
        if not self._dirty or (not force and time.monotonic() < self._next_flush):
            return
        self._next_flush = time.monotonic() + TOUCH_FLUSH_SECONDS
        async with self._lock:
            rows = {
                uid: (item["fetched_at"], item["last_used"], int(item["size"]))
                for uid in self._dirty
                if (item := self._index.get(uid))
            }
            self._dirty.clear()
        await self.storage.avatar_upsert(rows)

    async def _delete(self, user_ids: list[str]) -> dict[str, int]:
        deleted = freed = 0
        async with self._lock:
            for user_id in user_ids:
                path = self._path(user_id)
                if path.is_file():
                    freed += path.stat().st_size
                    path.unlink(missing_ok=True)
                    deleted += 1
                self._index.pop(user_id, None)
                self._dirty.discard(user_id)
                self._memory.pop(user_id, None)
        await self.storage.avatar_delete(user_ids)
        return {"deleted": deleted, "freed_bytes": freed}

    async def _online(self, user_id: str, settings: dict[str, Any]) -> bytes | None:
        cached = self._memory.get(user_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        data = await self._download(user_id, settings)
        self._memory[user_id] = (time.monotonic() + (300 if data else 60), data)
        return data

    async def _store(self, user_id: str, data: bytes, settings: dict[str, Any]) -> bool:
        path = self._path(user_id)
        old_size = path.stat().st_size if path.is_file() else 0
        limit = int(settings["max_media_mb"]) * 1024 * 1024
        if not await self.reserve(max(0, len(data) - old_size), limit, protect=user_id):
            return False
        await asyncio.to_thread(_atomic_write, path, data)
        now = time.time()
        async with self._lock:
            self._index[user_id] = {
                "fetched_at": now,
                "last_used": now,
                "size": len(data),
            }
            self._dirty.add(user_id)
        await self._flush(force=True)
        return True

    async def _download_and_store(
        self, user_id: str, settings: dict[str, Any]
    ) -> bytes | None:
        data = await self._download(user_id, settings)
        if not data:
            return None
        if not await self._store(user_id, data, settings):
            logger.warning("群典：头像缓存空间不足，已使用空白头像")
            return None
        return data

    async def _refresh(self, user_id: str, settings: dict[str, Any]) -> None:
        try:
            data = await self._download(user_id, settings)
            if data and not await self._store(user_id, data, settings):
                logger.warning("群典：头像缓存刷新空间不足，已继续使用旧头像")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 后台刷新不能泄漏未处理任务异常。
            logger.warning("群典：后台刷新 QQ 头像失败")

    def _schedule_refresh(self, user_id: str, settings: dict[str, Any]) -> None:
        if user_id in self._refresh_tasks:
            return
        task = asyncio.create_task(self._refresh(user_id, dict(settings)))
        self._refresh_tasks[user_id] = task
        task.add_done_callback(
            lambda _done, key=user_id: self._refresh_tasks.pop(key, None)
        )

    async def _download(self, user_id: str, settings: dict[str, Any]) -> bytes | None:
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6))
        attempts = int(settings["send_retry_count"]) + 1
        delay = int(settings["send_retry_delay_ms"]) / 1000
        url = f"https://q1.qlogo.cn/g?b=qq&nk={int(user_id)}&s=160"
        for attempt in range(attempts):
            try:
                async with self.http.get(
                    url, allow_redirects=True, max_redirects=3
                ) as response:
                    response.raise_for_status()
                    raw = await response.read()
                    if not raw or len(raw) > MAX_DOWNLOAD_BYTES:
                        raise ValueError("头像响应大小异常")
                    return await asyncio.to_thread(_normalize_image, raw)
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
                UnidentifiedImageError,
                ValueError,
            ):
                if attempt + 1 >= attempts:
                    logger.warning("群典：QQ 头像获取失败，已使用空白头像")
                    return None
                await asyncio.sleep(delay)
        return None
