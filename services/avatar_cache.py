"""QQ 头像的可选本地缓存与容量管理。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
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


class AvatarCacheService:
    """按 QQ 号缓存一份头像，并在容量不足时执行 LRU 清理。"""

    def __init__(self, storage: QuoteStorage):
        self.storage = storage
        self.directory = storage.avatars_dir
        self.index_path = self.directory / "index.json"
        self.blank_avatar_path = (
            Path(__file__).resolve().parent.parent / "assets" / "blank_avatar.svg"
        )
        self.http: aiohttp.ClientSession | None = None
        self._index: dict[str, dict[str, float | int]] = {}
        self._memory_cache: dict[str, tuple[float, bytes | None]] = {}
        self._lock = asyncio.Lock()
        self._refresh_tasks: dict[str, asyncio.Task[None]] = {}
        self._next_touch_persist = 0.0

    async def initialize(self) -> None:
        """创建目录、载入索引并建立可复用 HTTP Client。"""
        self.directory.mkdir(parents=True, exist_ok=True)
        self._index = await asyncio.to_thread(self._load_index_sync)
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6))

    async def close(self) -> None:
        """取消后台刷新并关闭 HTTP Client。"""
        tasks = tuple(self._refresh_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._refresh_tasks.clear()
        async with self._lock:
            await asyncio.to_thread(self._save_index_sync)
        if self.http and not self.http.closed:
            await self.http.close()

    async def rebind_storage_root(self) -> None:
        """存储迁移后切换到新头像目录并重新载入索引。"""
        async with self._lock:
            self.directory = self.storage.avatars_dir
            self.index_path = self.directory / "index.json"
            self.directory.mkdir(parents=True, exist_ok=True)
            self._index = await asyncio.to_thread(self._load_index_sync)
            self._memory_cache.clear()

    async def data_url(self, user_id: str | None, settings: dict[str, Any]) -> str:
        """取得卡片头像；本地化关闭时仅使用短期内存缓存。"""
        normalized = self._normalize_user_id(user_id)
        if not normalized:
            return await self.blank_data_url()
        if not settings["localize_avatars"]:
            data = await self._online_avatar(normalized, settings)
            return self._data_url(data) if data else await self.blank_data_url()

        path = self._avatar_path(normalized)
        if path.is_file():
            try:
                data = await asyncio.to_thread(self._validated_cached_bytes, path)
                await self._touch(normalized, len(data))
                if self._is_expired(normalized, settings["avatar_cache_ttl_days"]):
                    self._schedule_refresh(normalized, settings)
                return self._data_url(data)
            except (OSError, UnidentifiedImageError, ValueError):
                await self._discard(normalized)

        data = await self._download_and_store(normalized, settings)
        return self._data_url(data) if data else await self.blank_data_url()

    async def prefetch(self, user_id: str | None, settings: dict[str, Any]) -> None:
        """新收录普通金句后尽力补全头像，不让失败影响收录结果。"""
        try:
            normalized = self._normalize_user_id(user_id)
            if not normalized or not settings["localize_avatars"]:
                return
            path = self._avatar_path(normalized)
            if path.is_file():
                await self._touch(normalized, path.stat().st_size)
                if self._is_expired(normalized, settings["avatar_cache_ttl_days"]):
                    self._schedule_refresh(normalized, settings)
                return
            await self._download_and_store(normalized, settings)
        except Exception:  # noqa: BLE001 - 头像失败不能改变已经完成的金句收录。
            logger.exception("新金句头像预缓存失败: user=%s", user_id)

    async def blank_data_url(self) -> str:
        data = await asyncio.to_thread(self.blank_avatar_path.read_bytes)
        return f"data:image/svg+xml;base64,{base64.b64encode(data).decode()}"

    async def cached_path(self, user_id: str | None) -> Path:
        """仅返回已有本地头像或内置占位图，不触发网络下载。"""
        normalized = self._normalize_user_id(user_id)
        if normalized:
            path = self._avatar_path(normalized)
            if path.is_file():
                try:
                    size = await asyncio.to_thread(self._validate_cached_path, path)
                    await self._touch(normalized, size)
                    return path
                except (OSError, UnidentifiedImageError, ValueError):
                    await self._discard(normalized)
        return self.blank_avatar_path

    async def stats(self) -> dict[str, int]:
        """返回头像缓存数量和磁盘占用。"""
        async with self._lock:
            files = [path for path in self.directory.glob("*.jpg") if path.is_file()]
            return {
                "count": len(files),
                "bytes": sum(path.stat().st_size for path in files),
            }

    async def cleanup_unreferenced(self) -> dict[str, int]:
        """删除没有任何群典作者引用的头像。"""
        referenced = await self.storage.referenced_author_ids()
        async with self._lock, self.storage._maintenance_lock:
            deleted, freed = self._delete_candidates(
                [
                    user_id
                    for user_id in self._cached_user_ids()
                    if user_id not in referenced
                ]
            )
            await asyncio.to_thread(self._save_index_sync)
        return {"deleted": deleted, "freed_bytes": freed}

    async def clear(self) -> dict[str, int]:
        """清空全部可再生头像缓存。"""
        async with self._lock, self.storage._maintenance_lock:
            deleted, freed = self._delete_candidates(self._cached_user_ids())
            self._memory_cache.clear()
            await asyncio.to_thread(self._save_index_sync)
        return {"deleted": deleted, "freed_bytes": freed}

    async def _online_avatar(
        self, user_id: str, settings: dict[str, Any]
    ) -> bytes | None:
        cached = self._memory_cache.get(user_id)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        data = await self._download(user_id, settings)
        self._memory_cache[user_id] = (
            time.monotonic() + (300 if data else 60),
            data,
        )
        return data

    async def _download_and_store(
        self, user_id: str, settings: dict[str, Any]
    ) -> bytes | None:
        async with self._lock:
            path = self._avatar_path(user_id)
            if path.is_file():
                return await asyncio.to_thread(path.read_bytes)
            data = await self._download(user_id, settings)
            if not data:
                return None
            async with self.storage._maintenance_lock:
                if not await self._make_room(
                    len(data),
                    int(settings["max_media_mb"]) * 1024 * 1024,
                    protect=user_id,
                ):
                    logger.warning("头像缓存空间不足，已使用空白头像: user=%s", user_id)
                    return None
                await asyncio.to_thread(self._atomic_write, path, data)
            now = time.time()
            self._index[user_id] = {
                "fetched_at": now,
                "last_used": now,
                "size": len(data),
            }
            await asyncio.to_thread(self._save_index_sync)
            return data

    async def _refresh(self, user_id: str, settings: dict[str, Any]) -> None:
        data = await self._download(user_id, settings)
        if not data:
            return
        async with self._lock, self.storage._maintenance_lock:
            path = self._avatar_path(user_id)
            old_size = path.stat().st_size if path.is_file() else 0
            extra = max(0, len(data) - old_size)
            if not await self._make_room(
                extra,
                int(settings["max_media_mb"]) * 1024 * 1024,
                protect=user_id,
            ):
                logger.warning(
                    "头像缓存刷新空间不足，已继续使用旧头像: user=%s", user_id
                )
                return
            await asyncio.to_thread(self._atomic_write, path, data)
            now = time.time()
            self._index[user_id] = {
                "fetched_at": now,
                "last_used": now,
                "size": len(data),
            }
            await asyncio.to_thread(self._save_index_sync)

    async def _refresh_safely(self, user_id: str, settings: dict[str, Any]) -> None:
        try:
            await self._refresh(user_id, settings)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 后台刷新不能泄漏未处理任务异常。
            logger.exception("后台刷新 QQ 头像失败: user=%s", user_id)

    async def _download(self, user_id: str, settings: dict[str, Any]) -> bytes | None:
        if self.http is None or self.http.closed:
            await self.initialize()
        assert self.http is not None
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
                    return await asyncio.to_thread(self._normalize_image, raw)
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                OSError,
                UnidentifiedImageError,
                ValueError,
            ) as exc:
                if attempt + 1 >= attempts:
                    logger.warning("QQ 头像获取失败: user=%s error=%s", user_id, exc)
                    return None
                await asyncio.sleep(delay)
        return None

    async def _make_room(self, incoming: int, limit: int, *, protect: str) -> bool:
        usage = await self.storage.media_usage_bytes()
        if usage + incoming <= limit:
            return True
        referenced = await self.storage.referenced_author_ids()
        candidates = sorted(
            (user_id for user_id in self._cached_user_ids() if user_id != protect),
            key=lambda user_id: (
                user_id in referenced,
                float(self._index.get(user_id, {}).get("last_used", 0)),
            ),
        )
        changed = False
        for user_id in candidates:
            self._delete_candidates([user_id])
            changed = True
            usage = await self.storage.media_usage_bytes()
            if usage + incoming <= limit:
                await asyncio.to_thread(self._save_index_sync)
                return True
        if changed:
            await asyncio.to_thread(self._save_index_sync)
        return usage + incoming <= limit

    async def _touch(self, user_id: str, size: int) -> None:
        async with self._lock:
            item = self._index.setdefault(
                user_id,
                {"fetched_at": time.time(), "last_used": 0, "size": size},
            )
            item["last_used"] = time.time()
            item["size"] = size
            if time.monotonic() >= self._next_touch_persist:
                self._next_touch_persist = time.monotonic() + 60
                await asyncio.to_thread(self._save_index_sync)

    async def _discard(self, user_id: str) -> None:
        async with self._lock, self.storage._maintenance_lock:
            self._delete_candidates([user_id])
            await asyncio.to_thread(self._save_index_sync)

    def _schedule_refresh(self, user_id: str, settings: dict[str, Any]) -> None:
        if user_id in self._refresh_tasks:
            return
        task = asyncio.create_task(self._refresh_safely(user_id, dict(settings)))
        self._refresh_tasks[user_id] = task
        task.add_done_callback(
            lambda completed, key=user_id: self._refresh_tasks.pop(key, None)
        )

    def _is_expired(self, user_id: str, ttl_days: int) -> bool:
        if ttl_days == 0:
            return False
        fetched_at = float(self._index.get(user_id, {}).get("fetched_at", 0))
        return fetched_at + ttl_days * 86400 <= time.time()

    def _cached_user_ids(self) -> list[str]:
        return [path.stem for path in self.directory.glob("*.jpg") if path.is_file()]

    def _delete_candidates(self, user_ids: list[str]) -> tuple[int, int]:
        deleted = 0
        freed = 0
        for user_id in user_ids:
            path = self._avatar_path(user_id)
            if not path.is_file():
                self._index.pop(user_id, None)
                continue
            freed += path.stat().st_size
            path.unlink(missing_ok=True)
            self._index.pop(user_id, None)
            self._memory_cache.pop(user_id, None)
            deleted += 1
        return deleted, freed

    def _load_index_sync(self) -> dict[str, dict[str, float | int]]:
        if not self.index_path.is_file():
            return {}
        try:
            payload = json.loads(self.index_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            result: dict[str, dict[str, float | int]] = {}
            for raw_user_id, raw_item in payload.items():
                user_id = self._normalize_user_id(raw_user_id)
                if not user_id or not isinstance(raw_item, dict):
                    continue
                try:
                    result[user_id] = {
                        "fetched_at": float(raw_item.get("fetched_at", 0)),
                        "last_used": float(raw_item.get("last_used", 0)),
                        "size": max(0, int(raw_item.get("size", 0))),
                    }
                except (TypeError, ValueError):
                    continue
            return result
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning("头像缓存索引损坏，已按现有文件重建")
            return {}

    def _save_index_sync(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=self.directory,
            prefix=".avatar-index-",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self._index, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.index_path)
        finally:
            with contextlib.suppress(OSError):
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def _avatar_path(self, user_id: str) -> Path:
        return self.directory / f"{user_id}.jpg"

    @staticmethod
    def _validated_cached_bytes(path: Path) -> bytes:
        data = path.read_bytes()
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        return data

    @classmethod
    def _validate_cached_path(cls, path: Path) -> int:
        return len(cls._validated_cached_bytes(path))

    @staticmethod
    def _normalize_user_id(user_id: str | None) -> str:
        value = str(user_id or "").strip()
        return value if value.isdigit() else ""

    @staticmethod
    def _normalize_image(data: bytes) -> bytes:
        with Image.open(io.BytesIO(data)) as source:
            source.load()
            image = ImageOps.exif_transpose(source).convert("RGB")
            image = ImageOps.fit(
                image,
                (AVATAR_SIZE, AVATAR_SIZE),
                method=Image.Resampling.LANCZOS,
            )
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=88, optimize=True)
            return output.getvalue()

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=".avatar-",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(OSError):
                if os.path.exists(temporary):
                    os.unlink(temporary)

    @staticmethod
    def _data_url(data: bytes) -> str:
        return f"data:image/jpeg;base64,{base64.b64encode(data).decode()}"
