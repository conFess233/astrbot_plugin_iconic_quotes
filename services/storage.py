"""按群隔离的 JSON 与图片持久化服务。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import random
import shutil
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..models import QuoteRecord
from ..utils.hashing import calculate_record_hash, normalize_search
from ..utils.validation import identify_image, safe_storage_path

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, SCHEMA_VERSION}


class StorageError(RuntimeError):
    """表示可向用户归类说明的存储错误。"""


class DuplicateQuoteError(StorageError):
    """表示当前群已经存在相同内容哈希。"""

    def __init__(self, record: QuoteRecord):
        super().__init__("该群典已存在")
        self.record = record


class QuoteStorage:
    """管理群 JSON、内容寻址图片、审计和备份。"""

    def __init__(
        self,
        data_root: Path,
        storage_subdir: str,
        *,
        legacy_root: Path | None = None,
    ):
        self.data_root = data_root.resolve()
        self.root = safe_storage_path(self.data_root, storage_subdir)
        self.legacy_root = legacy_root.resolve() if legacy_root else None
        self.groups_dir = self.root / "groups"
        self.images_dir = self.root / "images"
        self.avatars_dir = self.root / "avatars"
        self.backups_dir = self.root / "backups"
        self.audit_path = self.root / "audit.json"
        self._locks: dict[str, asyncio.Lock] = {}
        self._maintenance_lock = asyncio.Lock()
        self._broken_groups: set[str] = set()

    async def initialize(self) -> Path | None:
        """迁移旧版插件数据并创建所需目录，返回旧目录备份路径。"""
        return await asyncio.to_thread(self._initialize_sync)

    def _initialize_sync(self) -> Path | None:
        backup_root = None
        legacy_root = self.legacy_root
        if (
            legacy_root
            and legacy_root != self.root
            and legacy_root.is_dir()
            and any(legacy_root.iterdir())
            and (not self.root.exists() or not any(self.root.iterdir()))
        ):
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_root = legacy_root.with_name(
                f"{legacy_root.name}.backup-{stamp}-{uuid.uuid4().hex[:6]}"
            )
            self._migrate_sync(legacy_root, self.root, backup_root)
        self._ensure_directories()
        return backup_root

    def _ensure_directories(self) -> None:
        for directory in (
            self.root,
            self.groups_dir,
            self.images_dir,
            self.avatars_dir,
            self.backups_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def _lock(self, group_id: str) -> asyncio.Lock:
        return self._locks.setdefault(str(group_id), asyncio.Lock())

    def _group_path(self, group_id: str) -> Path:
        if not str(group_id).isdigit():
            raise StorageError("群号格式无效")
        return self.groups_dir / f"{group_id}.json"

    @staticmethod
    def _new_document(group_id: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "group_id": str(group_id),
            "records": [],
        }

    @staticmethod
    def _atomic_json_write(path: Path, value: Any, *, keep_backup: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if keep_backup and path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        fd, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _validate_document(self, document: Any, group_id: str) -> dict[str, Any]:
        if not isinstance(document, dict):
            raise StorageError("群典 JSON 根节点不是对象")
        version = document.get("schema_version")
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            if isinstance(version, int) and version > SCHEMA_VERSION:
                raise StorageError("数据由更高版本插件创建，请先升级插件")
            raise StorageError(f"不支持的数据版本: {version}")
        if str(document.get("group_id")) != str(group_id):
            raise StorageError("群典 JSON 的群号与文件名不一致")
        if not isinstance(document.get("records"), list):
            raise StorageError("群典 JSON 缺少记录列表")
        seen_ids: set[str] = set()
        for raw in document["records"]:
            if not isinstance(raw, dict):
                raise StorageError("群典记录格式无效")
            record = QuoteRecord.from_dict(raw)
            if record.group_id != str(group_id):
                raise StorageError("记录群号与所在文件不一致")
            if not record.id or record.id in seen_ids:
                raise StorageError("记录 ID 为空或重复")
            seen_ids.add(record.id)
            if len(record.content_hash) != 64 or any(
                ch not in "0123456789abcdef" for ch in record.content_hash.lower()
            ):
                raise StorageError("记录内容哈希无效")
            if (
                calculate_record_hash(record, schema_version=version)
                != record.content_hash
            ):
                raise StorageError("记录内容与哈希不一致")
            if record.type == "message" and (
                record.author is None or (not record.segments and record.reply is None)
            ):
                raise StorageError("普通群典缺少作者或内容")
            if record.type == "forward" and (
                not record.nodes
                or any(
                    not node.segments and node.reply is None for node in record.nodes
                )
            ):
                raise StorageError("合并转发缺少节点内容")
        return document

    @staticmethod
    def _upgrade_document(document: dict[str, Any]) -> None:
        """在下一次业务写入时把 v1 内容哈希惰性升级到 v2。"""
        if document.get("schema_version") == SCHEMA_VERSION:
            return
        records = [QuoteRecord.from_dict(raw) for raw in document["records"]]
        for record in records:
            record.content_hash = calculate_record_hash(record)
        document["records"] = [record.to_dict() for record in records]
        document["schema_version"] = SCHEMA_VERSION

    def _load_document_sync(self, group_id: str) -> dict[str, Any]:
        if group_id in self._broken_groups:
            raise StorageError("当前群存储处于只读故障状态")
        path = self._group_path(group_id)
        if not path.exists():
            return self._new_document(group_id)
        try:
            with path.open(encoding="utf-8-sig") as stream:
                return self._validate_document(json.load(stream), group_id)
        except Exception as primary_error:
            backup = path.with_suffix(path.suffix + ".bak")
            if backup.exists():
                try:
                    with backup.open(encoding="utf-8-sig") as stream:
                        recovered = self._validate_document(json.load(stream), group_id)
                    self._atomic_json_write(path, recovered, keep_backup=False)
                    return recovered
                except (
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                    KeyError,
                    TypeError,
                    ValueError,
                ):
                    # 备份也无效，下面统一进入只读故障状态。
                    ...
            self._broken_groups.add(group_id)
            raise StorageError("群典数据损坏且无法从备份恢复") from primary_error

    async def records(self, group_id: str) -> list[QuoteRecord]:
        """读取当前群的全部记录。"""
        async with self._lock(group_id):
            document = await asyncio.to_thread(self._load_document_sync, group_id)
            return [QuoteRecord.from_dict(raw) for raw in document["records"]]

    async def save_image(
        self,
        group_id: str,
        data: bytes,
        *,
        max_image_bytes: int,
        max_media_bytes: int,
    ) -> dict[str, Any]:
        """校验并按内容哈希保存一张图片。"""
        if not data:
            raise StorageError("图片内容为空")
        if len(data) > max_image_bytes:
            raise StorageError("图片超过单张大小限制")
        extension, mime = identify_image(data)
        digest = hashlib.sha256(data).hexdigest()
        relative = Path("images") / str(group_id) / f"{digest}{extension}"
        destination = self.root / relative
        async with self._maintenance_lock:
            if not destination.exists():
                usage = await self.media_usage_bytes()
                if usage + len(data) > max_media_bytes:
                    await asyncio.to_thread(
                        self._prune_avatar_cache_for_space_sync,
                        len(data),
                        max_media_bytes,
                    )
                    usage = await self.media_usage_bytes()
                    if usage + len(data) > max_media_bytes:
                        raise StorageError("全局媒体存储容量已满")
                await asyncio.to_thread(self._write_bytes_once, destination, data)
        return {
            "type": "image",
            "path": relative.as_posix(),
            "sha256": digest,
            "mime": mime,
            "size": len(data),
        }

    @staticmethod
    def _write_bytes_once(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return
        fd, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=".image-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if not path.exists():
                os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    async def add(self, record: QuoteRecord, max_records: int) -> QuoteRecord:
        """原子添加记录，并在当前群内按内容哈希去重。"""
        async with self._maintenance_lock, self._lock(record.group_id):
            document = await asyncio.to_thread(
                self._load_document_sync,
                record.group_id,
            )
            self._upgrade_document(document)
            records = [QuoteRecord.from_dict(raw) for raw in document["records"]]
            duplicate = next(
                (item for item in records if item.content_hash == record.content_hash),
                None,
            )
            if duplicate:
                await self._cleanup_unreferenced_images_unlocked(
                    record.group_id, records
                )
                raise DuplicateQuoteError(duplicate)
            if len(records) >= max_records:
                await self._cleanup_unreferenced_images_unlocked(
                    record.group_id, records
                )
                raise StorageError("当前群的群典条数已达到上限")
            document["records"].append(record.to_dict())
            await asyncio.to_thread(
                self._atomic_json_write,
                self._group_path(record.group_id),
                document,
            )
            return record

    async def search(self, group_id: str, text: str) -> list[QuoteRecord]:
        """按规范化正文子串检索待删除记录。"""
        needle = normalize_search(text)
        if not needle:
            raise StorageError("删除关键词不能为空")
        return [
            record
            for record in await self.records(group_id)
            if needle in normalize_search(record.searchable_text())
        ]

    async def delete_ids(
        self,
        group_id: str,
        record_ids: set[str],
        *,
        deleted_by: str,
        source: str,
        expected_hashes: dict[str, str] | None = None,
        audit_limit: int = 10_000,
    ) -> int:
        """按已锁定的 ID 删除记录并清理无引用图片。"""
        async with self._maintenance_lock, self._lock(group_id):
            document = await asyncio.to_thread(self._load_document_sync, group_id)
            self._upgrade_document(document)
            current = [QuoteRecord.from_dict(raw) for raw in document["records"]]
            selected = [record for record in current if record.id in record_ids]
            if len(selected) != len(record_ids):
                raise StorageError("待删除记录已发生变化，请重新执行删除命令")
            if expected_hashes and any(
                expected_hashes.get(record.id) != record.content_hash
                for record in selected
            ):
                raise StorageError("待删除记录已发生变化，请重新执行删除命令")
            remaining = [record for record in current if record.id not in record_ids]
            document["records"] = [record.to_dict() for record in remaining]
            await asyncio.to_thread(
                self._atomic_json_write,
                self._group_path(group_id),
                document,
            )
            await self._append_audit(
                group_id, selected, deleted_by, source, audit_limit
            )
            await self._cleanup_unreferenced_images_unlocked(group_id, remaining)
            return len(selected)

    async def select_random(
        self,
        group_id: str,
        count: int,
        author_id: str | None = None,
    ) -> tuple[list[QuoteRecord], int]:
        """从有效记录中等概率无放回抽取。"""
        candidates = await self.records(group_id)
        if author_id:
            candidates = [
                record
                for record in candidates
                if record.personal_owner_id() == author_id
            ]
        healthy: list[QuoteRecord] = []
        broken = 0
        for record in candidates:
            if await asyncio.to_thread(self._record_images_valid, record):
                healthy.append(record)
            else:
                broken += 1
        if not healthy:
            return [], broken
        return random.sample(healthy, min(count, len(healthy))), broken

    async def records_for_user(
        self,
        group_id: str,
        user_id: str,
    ) -> tuple[list[QuoteRecord], int]:
        """按添加时间返回用户参与的全部健康记录及损坏数量。"""
        candidates = [
            record
            for record in await self.records(group_id)
            if record.involves_user(user_id)
        ]
        healthy: list[QuoteRecord] = []
        broken = 0
        for record in candidates:
            if await asyncio.to_thread(self._record_images_valid, record):
                healthy.append(record)
            else:
                broken += 1
        healthy.sort(key=lambda record: record.recorded_at)
        return healthy, broken

    def _record_images_valid(self, record: QuoteRecord) -> bool:
        for segment in record.image_segments():
            if not segment.path or not segment.sha256:
                return False
            path = (self.root / segment.path).resolve()
            if self.root not in path.parents or not path.is_file():
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != segment.sha256:
                return False
        return True

    async def record_is_healthy(self, record: QuoteRecord) -> bool:
        """供管理页检查单条记录引用的图片是否仍完整。"""
        return await asyncio.to_thread(self._record_images_valid, record)

    async def info(self, group_id: str, max_records: int) -> dict[str, Any]:
        """汇总当前群统计，不返回正文。"""
        records = await self.records(group_id)
        broken = 0
        for record in records:
            if not await asyncio.to_thread(self._record_images_valid, record):
                broken += 1
        image_paths = {
            segment.path
            for record in records
            for segment in record.image_segments()
            if segment.path
        }
        image_bytes = sum(
            (self.root / path).stat().st_size
            for path in image_paths
            if (self.root / path).is_file()
        )
        times = sorted(record.recorded_at for record in records if record.recorded_at)
        return {
            "total": len(records),
            "limit": max_records,
            "messages": sum(record.type == "message" for record in records),
            "forwards": sum(record.type == "forward" for record in records),
            "with_images": sum(bool(record.image_segments()) for record in records),
            "broken": broken,
            "image_bytes": image_bytes,
            "earliest": times[0] if times else None,
            "latest": times[-1] if times else None,
        }

    async def media_usage_bytes(self) -> int:
        """计算群典图片与本地头像缓存的合计占用。"""
        return await asyncio.to_thread(self._media_usage_bytes_sync)

    def _media_usage_bytes_sync(self) -> int:
        return sum(
            path.stat().st_size
            for directory in (self.images_dir, self.avatars_dir)
            for path in directory.rglob("*")
            if path.is_file()
        )

    def _prune_avatar_cache_for_space_sync(self, incoming: int, limit: int) -> None:
        """为不可再生的群典图片腾出空间，头像按无引用和 LRU 顺序淘汰。"""
        if self._media_usage_bytes_sync() + incoming <= limit:
            return
        index_path = self.avatars_dir / "index.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8"))
            if not isinstance(index, dict):
                index = {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            index = {}
        referenced = self._referenced_author_ids_sync()
        candidates = sorted(
            (path for path in self.avatars_dir.glob("*.jpg") if path.is_file()),
            key=lambda path: (
                path.stem in referenced,
                self._avatar_last_used(index.get(path.stem)),
            ),
        )
        changed = False
        for path in candidates:
            path.unlink(missing_ok=True)
            index.pop(path.stem, None)
            changed = True
            if self._media_usage_bytes_sync() + incoming <= limit:
                break
        if changed:
            self._atomic_json_write(index_path, index, keep_backup=False)

    def _referenced_author_ids_sync(self) -> set[str]:
        result: set[str] = set()
        for path in self.groups_dir.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                records = [
                    QuoteRecord.from_dict(raw) for raw in payload.get("records", [])
                ]
            except (
                OSError,
                UnicodeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ):
                continue
            for record in records:
                if record.author and record.author.user_id:
                    result.add(record.author.user_id)
                self._collect_reply_author_ids(record.reply, result)
                for node in record.nodes:
                    if node.author.user_id:
                        result.add(node.author.user_id)
                    self._collect_reply_author_ids(node.reply, result)
        return result

    @staticmethod
    def _avatar_last_used(value: Any) -> float:
        if not isinstance(value, dict):
            return 0
        try:
            return float(value.get("last_used", 0))
        except (TypeError, ValueError):
            return 0

    async def referenced_author_ids(self) -> set[str]:
        """汇总全部记录及回复快照中仍被引用的 QQ 号。"""
        result: set[str] = set()
        for group_id in await self.list_groups():
            for record in await self.records(group_id):
                if record.author and record.author.user_id:
                    result.add(record.author.user_id)
                self._collect_reply_author_ids(record.reply, result)
                for node in record.nodes:
                    if node.author.user_id:
                        result.add(node.author.user_id)
                    self._collect_reply_author_ids(node.reply, result)
        return result

    @classmethod
    def _collect_reply_author_ids(cls, reply: Any, result: set[str]) -> None:
        if reply is None:
            return
        if reply.author.user_id:
            result.add(reply.author.user_id)
        cls._collect_reply_author_ids(reply.reply, result)

    async def cleanup_group_orphans(self, group_id: str) -> None:
        """清理一次失败收录可能留下的未引用图片。"""
        async with self._maintenance_lock, self._lock(group_id):
            document = await asyncio.to_thread(self._load_document_sync, group_id)
            records = [QuoteRecord.from_dict(raw) for raw in document["records"]]
            await self._cleanup_unreferenced_images_unlocked(group_id, records)

    async def _cleanup_unreferenced_images_unlocked(
        self,
        group_id: str,
        records: list[QuoteRecord],
    ) -> None:
        referenced = {
            (self.root / segment.path).resolve()
            for record in records
            for segment in record.image_segments()
            if segment.path
        }
        directory = self.images_dir / str(group_id)

        def cleanup() -> None:
            if not directory.exists():
                return
            for path in directory.iterdir():
                if path.is_file() and path.resolve() not in referenced:
                    path.unlink(missing_ok=True)

        await asyncio.to_thread(cleanup)

    async def _append_audit(
        self,
        group_id: str,
        records: list[QuoteRecord],
        deleted_by: str,
        source: str,
        limit: int,
    ) -> None:
        def append() -> None:
            entries: list[dict[str, Any]] = []
            if self.audit_path.exists():
                try:
                    loaded = json.loads(self.audit_path.read_text(encoding="utf-8-sig"))
                    if isinstance(loaded, list):
                        entries = loaded
                except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                    entries = []
            timestamp = datetime.now(UTC).isoformat()
            entries.extend(
                {
                    "group_id": group_id,
                    "record_id": record.id,
                    "content_hash": record.content_hash,
                    "deleted_by": deleted_by,
                    "deleted_at": timestamp,
                    "source": source,
                }
                for record in records
            )
            self._atomic_json_write(
                self.audit_path,
                entries[-limit:],
                keep_backup=True,
            )

        await asyncio.to_thread(append)

    async def audit_entries(self, limit: int = 200) -> list[dict[str, Any]]:
        """读取最近删除审计。"""

        def read() -> list[dict[str, Any]]:
            if not self.audit_path.exists():
                return []
            value = json.loads(self.audit_path.read_text(encoding="utf-8-sig"))
            return value[-limit:] if isinstance(value, list) else []

        return await asyncio.to_thread(read)

    async def list_groups(self) -> list[str]:
        """列出已有群数据文件。"""
        return await asyncio.to_thread(
            lambda: sorted(path.stem for path in self.groups_dir.glob("*.json"))
        )

    async def export_zip(self, include_settings: dict[str, Any]) -> Path:
        """创建带清单的可移植 ZIP 备份。"""
        async with self._maintenance_lock:
            return await asyncio.to_thread(self._export_zip_sync, include_settings)

    def _export_zip_sync(self, include_settings: dict[str, Any]) -> Path:
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.backups_dir / f"iconic-quotes-{stamp}-{uuid.uuid4().hex[:6]}.zip"
        files = [
            path
            for directory in (self.groups_dir, self.images_dir)
            for path in directory.rglob("*")
            if path.is_file() and not path.name.endswith(".bak")
        ]
        manifest = {
            "format": "astrbot_plugin_iconic_quotes_backup",
            "schema_version": SCHEMA_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "files": {
                path.relative_to(self.root).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in files
            },
            "settings": include_settings,
        }
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )
            for path in files:
                archive.write(path, path.relative_to(self.root).as_posix())
        return target

    async def import_zip(
        self,
        archive_path: Path,
        *,
        max_records: int | dict[str, int],
        max_media_bytes: int,
        max_image_bytes: int = 100 * 1024 * 1024,
    ) -> dict[str, Any]:
        """严格校验后以哈希去重方式合并官方备份。"""
        async with self._maintenance_lock:
            try:
                return await asyncio.to_thread(
                    self._import_zip_sync,
                    archive_path,
                    max_records,
                    max_media_bytes,
                    max_image_bytes,
                    True,
                )
            except StorageError:
                raise
            except (
                OSError,
                KeyError,
                TypeError,
                ValueError,
                zipfile.BadZipFile,
            ) as exc:
                raise StorageError("备份文件损坏或格式无效") from exc

    async def inspect_zip(
        self,
        archive_path: Path,
        *,
        max_records: int | dict[str, int],
        max_media_bytes: int,
        max_image_bytes: int = 100 * 1024 * 1024,
    ) -> dict[str, Any]:
        """完整预检 ZIP，但不写入任何记录或图片。"""
        async with self._maintenance_lock:
            try:
                return await asyncio.to_thread(
                    self._import_zip_sync,
                    archive_path,
                    max_records,
                    max_media_bytes,
                    max_image_bytes,
                    False,
                )
            except StorageError:
                raise
            except (
                OSError,
                KeyError,
                TypeError,
                ValueError,
                zipfile.BadZipFile,
            ) as exc:
                raise StorageError("备份文件损坏或格式无效") from exc

    def _import_zip_sync(
        self,
        archive_path: Path,
        max_records: int | dict[str, int],
        max_media_bytes: int,
        max_image_bytes: int,
        commit: bool,
    ) -> dict[str, Any]:
        added = duplicates = conflicts = missing_images = 0
        merged_documents: dict[str, dict[str, Any]] = {}
        original_documents: dict[str, dict[str, Any]] = {}
        required_images: set[str] = set()
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > 100_000:
                raise StorageError("备份文件数量超过限制")
            unpacked_limit = min(max_media_bytes + 512 * 1024 * 1024, 4 * 1024**3)
            if sum(info.file_size for info in infos) > unpacked_limit:
                raise StorageError("备份解压后的总大小超过安全限制")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise StorageError("备份包含重复文件名")
            for info in infos:
                path = Path(info.filename)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or info.file_size > 100 * 1024 * 1024
                    or (
                        info.file_size > 10 * 1024 * 1024
                        and info.file_size > max(1, info.compress_size) * 200
                    )
                ):
                    raise StorageError("备份包含不安全的文件路径或超大文件")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != "astrbot_plugin_iconic_quotes_backup":
                raise StorageError("不是本插件导出的备份")
            declared = manifest.get("files")
            if not isinstance(declared, dict):
                raise StorageError("备份清单无效")
            actual_files = {info.filename for info in infos if not info.is_dir()}
            if actual_files != {"manifest.json", *declared.keys()}:
                raise StorageError("备份文件与清单不一致")
            payloads: dict[str, bytes] = {}
            for name, expected_hash in declared.items():
                path = Path(name)
                if (
                    not isinstance(name, str)
                    or path.is_absolute()
                    or ".." in path.parts
                    or not (
                        name.startswith("images/")
                        or (name.startswith("groups/") and name.endswith(".json"))
                    )
                ):
                    raise StorageError(f"备份清单包含不允许的路径: {name}")
                data = archive.read(name)
                if hashlib.sha256(data).hexdigest() != expected_hash:
                    raise StorageError(f"备份文件哈希不匹配: {name}")
                payloads[name] = data
            image_payloads = {
                name: data
                for name, data in payloads.items()
                if name.startswith("images/")
            }
            if any(len(data) > max_image_bytes for data in image_payloads.values()):
                raise StorageError("备份包含超过当前单张上限的图片")
            current_usage = sum(
                path.stat().st_size
                for path in self.images_dir.rglob("*")
                if path.is_file()
            )
            group_names = sorted(
                name
                for name in payloads
                if name.startswith("groups/") and name.endswith(".json")
            )
            for name in group_names:
                incoming = json.loads(payloads[name])
                group_id = Path(name).stem
                group_limit = (
                    max_records.get(group_id, max_records.get("*", 5000))
                    if isinstance(max_records, dict)
                    else max_records
                )
                self._validate_document(incoming, group_id)
                current = self._load_document_sync(group_id)
                original_documents[group_id] = json.loads(json.dumps(current))
                self._upgrade_document(incoming)
                self._upgrade_document(current)
                hashes = {raw.get("content_hash") for raw in current["records"]}
                ids = {
                    raw.get("id"): raw.get("content_hash") for raw in current["records"]
                }
                for raw in incoming["records"]:
                    if raw.get("content_hash") in hashes:
                        duplicates += 1
                        continue
                    if raw.get("id") in ids:
                        conflicts += 1
                        continue
                    if len(current["records"]) >= group_limit:
                        raise StorageError(f"群 {group_id} 导入后会超过记录上限")
                    record = QuoteRecord.from_dict(raw)
                    for segment in record.image_segments():
                        relative = segment.path or ""
                        expected = segment.sha256 or ""
                        image_path = Path(relative)
                        if (
                            not relative.startswith(f"images/{group_id}/")
                            or image_path.is_absolute()
                            or ".." in image_path.parts
                            or len(expected) != 64
                            or image_path.stem.casefold() != expected.casefold()
                        ):
                            raise StorageError("记录图片路径或哈希字段无效")
                        required_images.add(relative)
                        data = image_payloads.get(relative)
                        if data is None:
                            existing = self.root / relative
                            if not existing.is_file():
                                missing_images += 1
                                continue
                            data = existing.read_bytes()
                        if hashlib.sha256(data).hexdigest() != expected:
                            raise StorageError(f"记录图片哈希不匹配: {relative}")
                        identify_image(data)
                    current["records"].append(raw)
                    hashes.add(raw.get("content_hash"))
                    ids[raw.get("id")] = raw.get("content_hash")
                    added += 1
                merged_documents[group_id] = current
            image_payloads = {
                name: data
                for name, data in image_payloads.items()
                if name in required_images
            }
            new_image_bytes = sum(
                len(data)
                for name, data in image_payloads.items()
                if not (self.root / name).exists()
            )
            if current_usage + new_image_bytes > max_media_bytes:
                raise StorageError("导入会超过全局媒体容量")
        result = {
            "added": added,
            "duplicates": duplicates,
            "conflicts": conflicts,
            "missing_images": missing_images,
            "image_bytes": new_image_bytes,
            "settings": manifest.get("settings") or {},
        }
        if not commit:
            return result
        if missing_images:
            raise StorageError(f"备份缺少 {missing_images} 张被记录引用的图片")
        created_images: list[Path] = []
        written_groups: list[str] = []
        try:
            for name, data in image_payloads.items():
                target = self.root / name
                if not target.exists():
                    self._write_bytes_once(target, data)
                    created_images.append(target)
            for group_id, document in merged_documents.items():
                self._atomic_json_write(self._group_path(group_id), document)
                written_groups.append(group_id)
        except Exception:
            for group_id in reversed(written_groups):
                original = original_documents[group_id]
                path = self._group_path(group_id)
                if (
                    original["records"]
                    or path.with_suffix(path.suffix + ".bak").exists()
                ):
                    with contextlib.suppress(Exception):
                        self._atomic_json_write(path, original, keep_backup=False)
                else:
                    path.unlink(missing_ok=True)
            for path in created_images:
                path.unlink(missing_ok=True)
            raise
        return result

    async def migrate_to(self, new_subdir: str) -> tuple[Path, Path, Path]:
        """复制校验数据后切换根目录，并保留带时间戳旧目录。"""
        new_root = safe_storage_path(self.data_root, new_subdir)
        if new_root == self.root:
            raise StorageError("新旧存储路径相同")
        if new_root in self.root.parents or self.root in new_root.parents:
            raise StorageError("新旧存储目录不能互相包含")
        async with self._maintenance_lock:
            old_root = self.root
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            backup_root = old_root.with_name(
                f"{old_root.name}.backup-{stamp}-{uuid.uuid4().hex[:6]}"
            )
            await asyncio.to_thread(self._migrate_sync, old_root, new_root, backup_root)
            self.root = new_root
            self.groups_dir = new_root / "groups"
            self.images_dir = new_root / "images"
            self.avatars_dir = new_root / "avatars"
            self.backups_dir = new_root / "backups"
            self.audit_path = new_root / "audit.json"
            self._broken_groups.clear()
            return new_root, backup_root, old_root

    async def rollback_migration(
        self,
        old_root: Path,
        new_root: Path,
        backup_root: Path,
    ) -> None:
        """配置保存失败时恢复迁移前目录和服务路径。"""
        async with self._maintenance_lock:
            await asyncio.to_thread(
                self._rollback_migration_sync,
                old_root,
                new_root,
                backup_root,
            )
            self.root = old_root
            self.groups_dir = old_root / "groups"
            self.images_dir = old_root / "images"
            self.avatars_dir = old_root / "avatars"
            self.backups_dir = old_root / "backups"
            self.audit_path = old_root / "audit.json"
            self._broken_groups.clear()

    def _migrate_sync(self, old_root: Path, new_root: Path, backup_root: Path) -> None:
        if new_root.exists() and any(new_root.iterdir()):
            raise StorageError("目标存储目录不是空目录")
        temporary = new_root.with_name(
            f".{new_root.name}.migrating-{uuid.uuid4().hex[:8]}"
        )
        try:
            if temporary.exists():
                shutil.rmtree(temporary)
            shutil.copytree(old_root, temporary)
            for group_file in (temporary / "groups").glob("*.json"):
                value = json.loads(group_file.read_text(encoding="utf-8-sig"))
                self._validate_document(value, group_file.stem)
            for image in (temporary / "images").rglob("*"):
                if image.is_file():
                    identify_image(image.read_bytes())
            new_root.parent.mkdir(parents=True, exist_ok=True)
            if new_root.exists():
                new_root.rmdir()
            os.replace(temporary, new_root)
            os.replace(old_root, backup_root)
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary)
            if backup_root.exists() and not old_root.exists():
                os.replace(backup_root, old_root)
            raise

    @staticmethod
    def _rollback_migration_sync(
        old_root: Path,
        new_root: Path,
        backup_root: Path,
    ) -> None:
        if old_root.exists():
            raise StorageError("无法回滚迁移：原存储目录已被占用")
        if not backup_root.is_dir():
            raise StorageError("无法回滚迁移：旧目录备份不存在")
        if new_root.is_dir():
            shutil.rmtree(new_root)
        elif new_root.exists():
            raise StorageError("无法回滚迁移：新路径不是目录")
        os.replace(backup_root, old_root)

    def resolve_media_path(self, relative_path: str) -> Path:
        """解析已保存媒体路径并确保不会越界。"""
        resolved = (self.root / relative_path).resolve()
        if self.root not in resolved.parents or not resolved.is_file():
            raise StorageError("媒体文件不存在或路径无效")
        return resolved
