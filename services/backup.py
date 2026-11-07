"""ZIP 备份：导出 v4，导入 v4 与 1.x 旧版（v1–v3），预检后再确认写入。

v4 结构::

    manifest.json        格式、版本、文件哈希清单、可迁移配置
    records.jsonl        每行一条记录
    media/<sha256>.<ext> 被引用的图片与贴纸

导入按群内内容哈希去重合并，不覆盖现有记录。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ..models import QuoteRecord
from ..utils.hashing import calculate_record_hash
from ..utils.timefmt import now_iso
from ..utils.validation import identify_image
from .legacy_import import LegacyFormatError, parse_document
from .media import MediaInfo, MediaStore
from .storage import QuoteStorage, StorageError

FORMAT = "astrbot_plugin_iconic_quotes_backup"
BACKUP_VERSION = 4
MAX_ENTRIES = 100_000
MAX_ENTRY_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 4 * 1024**3
HASH_CHUNK_BYTES = 1024 * 1024
HEADER_BYTES = 16
MEDIA_PREFIXES = ("media/", "images/")
AUTO_PREFIX = "auto-iconic-quotes-"
MANUAL_PREFIX = "iconic-quotes-"
# 手动导出的文件已经下载到管理员手里，服务器上只留最近几份兜底。
MANUAL_KEEP = 3


@dataclass(slots=True, frozen=True)
class ArchiveMedia:
    """压缩包内的一个媒体文件：只保留校验结果，内容写入时才读取。"""

    member: str
    size: int
    digest: str
    header: bytes


@dataclass(slots=True)
class ImportPlan:
    """预检结果：待写入记录、待写入媒体与统计。"""

    records: list[QuoteRecord] = field(default_factory=list)
    media: dict[str, ArchiveMedia] = field(default_factory=dict)
    added: int = 0
    duplicates: int = 0
    conflicts: int = 0
    missing_images: int = 0
    image_bytes: int = 0
    settings: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "duplicates": self.duplicates,
            "conflicts": self.conflicts,
            "missing_images": self.missing_images,
            "image_bytes": self.image_bytes,
            "settings": self.settings,
        }


def _safe_name(name: str) -> bool:
    path = PurePosixPath(name)
    return (
        bool(name)
        and not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in name
    )


def _hash_member(archive: zipfile.ZipFile, name: str) -> tuple[str, bytes]:
    """流式计算压缩包成员的 SHA-256，并取文件头用于识别格式。"""
    digest = hashlib.sha256()
    header = b""
    with archive.open(name) as stream:
        while chunk := stream.read(HASH_CHUNK_BYTES):
            if not header:
                header = chunk[:HEADER_BYTES]
            digest.update(chunk)
    return digest.hexdigest(), header


class BackupService:
    """导出与导入；所有写入都通过 QuoteStorage 的单事务完成。"""

    def __init__(self, storage: QuoteStorage, media: MediaStore, backups_dir: Path):
        self.storage = storage
        self.media = media
        self.backups_dir = backups_dir
        self._lock = asyncio.Lock()

    # 导出 -------------------------------------------------------------

    async def export(self, settings: dict[str, Any], *, auto: bool = False) -> Path:
        async with self._lock:
            rows = await self.storage.export_rows()
            media = await self.storage.media_rows()
            prefix = AUTO_PREFIX if auto else MANUAL_PREFIX
            target = await asyncio.to_thread(
                self._export_sync, rows, media, settings, prefix
            )
            if not auto:
                await asyncio.to_thread(self._prune, MANUAL_PREFIX, MANUAL_KEEP)
            return target

    def _prune(self, prefix: str, keep: int) -> int:
        """按文件名中的时间保留最新的 keep 个；删不掉（如正在下载）的留到下次。"""
        files = sorted(self.backups_dir.glob(f"{prefix}*.zip"), reverse=True)
        removed = 0
        for path in files[keep:]:
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def prune_auto(self, keep: int) -> int:
        """只清理自动备份；手动导出的文件有自己的保留数量。"""
        return self._prune(AUTO_PREFIX, keep)

    def sweep(self) -> None:
        """启动时清理：待确认的导入随进程结束已失效，手动导出超量的也一并删除。"""
        if not self.backups_dir.is_dir():
            return
        for path in self.backups_dir.glob(".import-*.zip"):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)
        self._prune(MANUAL_PREFIX, MANUAL_KEEP)

    def _export_sync(
        self,
        rows: list[tuple[str, str]],
        media: dict[str, dict[str, Any]],
        settings: dict[str, Any],
        prefix: str = MANUAL_PREFIX,
    ) -> Path:
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.backups_dir / f"{prefix}{stamp}-{uuid.uuid4().hex[:6]}.zip"
        records_blob = (
            ("\n".join(payload for _, payload in rows) + "\n").encode() if rows else b""
        )
        files: dict[str, str] = {
            "records.jsonl": hashlib.sha256(records_blob).hexdigest()
        }
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("records.jsonl", records_blob)
            for sha256, row in sorted(media.items()):
                path = self.media.path(sha256, row["mime"])
                if not path.is_file():
                    continue
                name = f"media/{path.name}"
                data = path.read_bytes()
                files[name] = hashlib.sha256(data).hexdigest()
                archive.writestr(name, data)
            archive.writestr(
                "manifest.json",
                json.dumps(
                    {
                        "format": FORMAT,
                        "schema_version": BACKUP_VERSION,
                        "created_at": now_iso(),
                        "files": files,
                        "settings": settings,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return target

    # 导入 -------------------------------------------------------------

    async def inspect(
        self, archive: Path, settings: dict[str, Any], limits: dict[str, int]
    ) -> ImportPlan:
        """完整预检，不写入任何数据。"""
        async with self._lock:
            return await self._plan(archive, settings, limits)

    async def commit(
        self, archive: Path, settings: dict[str, Any], limits: dict[str, int]
    ) -> ImportPlan:
        """重新预检后写入；缺图或任何校验失败时整体不写入。"""
        async with self._lock:
            plan = await self._plan(archive, settings, limits)
            if plan.missing_images:
                raise StorageError(f"备份缺少 {plan.missing_images} 张被记录引用的图片")
            written: list[MediaInfo] = []
            infos: dict[str, MediaInfo] = {}
            try:
                await asyncio.to_thread(
                    self._write_media, archive, plan.media, infos, written
                )
                await self.storage.import_records(
                    plan.records,
                    infos.values(),
                    lambda group_id: limits.get(group_id, limits["*"]),
                )
            except BaseException:
                for info in written:
                    await asyncio.to_thread(self.media.delete, info.sha256, info.mime)
                raise
            return plan

    def _write_media(
        self,
        archive: Path,
        entries: dict[str, ArchiveMedia],
        infos: dict[str, MediaInfo],
        written: list[MediaInfo],
    ) -> None:
        """逐个读出并写入媒体，内存中同时最多只有一张图片。"""
        with zipfile.ZipFile(archive) as source:
            for sha256, entry in entries.items():
                info, created = self.media.write(source.read(entry.member))
                if created:
                    written.append(info)
                if info.sha256 != sha256:
                    raise StorageError("记录图片哈希不匹配")
                infos[sha256] = info

    async def _plan(
        self, archive: Path, settings: dict[str, Any], limits: dict[str, int]
    ) -> ImportPlan:
        try:
            records, archived, manifest_settings = await asyncio.to_thread(
                self._read_archive, archive
            )
        except StorageError:
            raise
        except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
            raise StorageError("备份文件损坏或格式无效") from exc
        plan = ImportPlan(settings=manifest_settings)
        max_image = settings["max_image_mb"] * 1024 * 1024
        if any(entry.size > max_image for entry in archived.values()):
            raise StorageError("备份包含超过当前单张上限的图片")
        by_group: dict[str, list[QuoteRecord]] = {}
        for record in records:
            by_group.setdefault(record.group_id, []).append(record)
        existing_ids = await self.storage.all_record_ids()
        for group_id, items in sorted(by_group.items()):
            hashes, _, count = await self.storage.existing_keys(group_id)
            limit = limits.get(group_id, limits["*"])
            for record in items:
                if record.content_hash in hashes:
                    plan.duplicates += 1
                    continue
                if record.id in existing_ids:
                    plan.conflicts += 1
                    continue
                if count >= limit:
                    raise StorageError(f"群 {group_id} 导入后会超过记录上限")
                plan.records.append(record)
                hashes.add(record.content_hash)
                existing_ids.add(record.id)
                count += 1
        plan.added = len(plan.records)
        needed: dict[str, str] = {}
        for record in plan.records:
            for segment in record.image_segments():
                if segment.sha256:
                    needed.setdefault(segment.sha256, segment.mime or "")
        known = await self.storage.media_rows(needed)
        for sha256, mime in needed.items():
            entry = archived.get(sha256)
            if entry is None:
                row = known.get(sha256)
                if (
                    row is None
                    or row["missing"]
                    or not await asyncio.to_thread(
                        self.media.quick_ok, sha256, row["mime"], row["size"]
                    )
                ):
                    plan.missing_images += 1
                continue
            if entry.digest != sha256:
                raise StorageError("记录图片哈希不匹配")
            try:
                _, actual = identify_image(entry.header)
            except ValueError as exc:
                raise StorageError("备份包含无法识别的图片") from exc
            if mime and actual != mime:
                raise StorageError("记录图片类型与登记不一致")
            plan.media[sha256] = entry
            if sha256 not in known:
                plan.image_bytes += entry.size
        usage = await self.storage.media_usage()
        if usage + plan.image_bytes > settings["max_media_mb"] * 1024 * 1024:
            raise StorageError("导入会超过全局媒体容量")
        return plan

    def _read_archive(
        self, archive_path: Path
    ) -> tuple[list[QuoteRecord], dict[str, ArchiveMedia], dict[str, Any]]:
        """读取并校验 ZIP；返回记录、按 sha256 索引的媒体条目与可迁移配置。

        只有清单和记录文本进入内存，媒体流式校验，写入时才逐个读取。
        """
        with zipfile.ZipFile(archive_path) as archive:
            infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(infos) > MAX_ENTRIES:
                raise StorageError("备份文件数量超过限制")
            if sum(info.file_size for info in infos) > MAX_TOTAL_BYTES:
                raise StorageError("备份解压后的总大小超过安全限制")
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                raise StorageError("备份包含重复文件名")
            for info in infos:
                if (
                    not _safe_name(info.filename)
                    or info.file_size > MAX_ENTRY_BYTES
                    or (
                        info.file_size > 10 * 1024 * 1024
                        and info.file_size > max(1, info.compress_size) * 200
                    )
                ):
                    raise StorageError("备份包含不安全的文件路径或超大文件")
            if "manifest.json" not in names:
                raise StorageError("不是本插件导出的备份")
            manifest = json.loads(archive.read("manifest.json"))
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
                raise StorageError("不是本插件导出的备份")
            declared = manifest.get("files")
            if not isinstance(declared, dict) or not all(
                isinstance(name, str) and _safe_name(name) for name in declared
            ):
                raise StorageError("备份清单无效")
            if set(names) != {"manifest.json", *declared}:
                raise StorageError("备份文件与清单不一致")
            payloads: dict[str, bytes] = {}
            members: dict[str, ArchiveMedia] = {}
            for info in infos:
                name = info.filename
                if name == "manifest.json":
                    continue
                digest, header = _hash_member(archive, name)
                if digest != declared[name]:
                    raise StorageError(f"备份文件哈希不匹配: {name}")
                if name.startswith(MEDIA_PREFIXES):
                    members[name] = ArchiveMedia(name, info.file_size, digest, header)
                else:
                    payloads[name] = archive.read(name)
        version = manifest.get("schema_version")
        settings = (
            manifest.get("settings")
            if isinstance(manifest.get("settings"), dict)
            else {}
        )
        if version == BACKUP_VERSION:
            records, media = self._v4(payloads, members)
        elif version in {1, 2, 3}:
            records, media = self._legacy(payloads, members)
        else:
            raise StorageError(
                "备份由更高版本插件创建，请先升级插件"
                if isinstance(version, int) and version > BACKUP_VERSION
                else f"不支持的备份版本: {version}"
            )
        return records, media, settings

    @staticmethod
    def _v4(
        payloads: dict[str, bytes], members: dict[str, ArchiveMedia]
    ) -> tuple[list[QuoteRecord], dict[str, ArchiveMedia]]:
        if any(name != "records.jsonl" for name in payloads) or any(
            not name.startswith("media/") for name in members
        ):
            raise StorageError("备份清单包含不允许的路径")
        records: list[QuoteRecord] = []
        seen: set[str] = set()
        for line in payloads.get("records.jsonl", b"").decode("utf-8").splitlines():
            if not line.strip():
                continue
            record = QuoteRecord.from_dict(json.loads(line))
            if (
                not record.group_id.isdigit()
                or record.id in seen
                or not record.has_content()
                or calculate_record_hash(record) != record.content_hash
            ):
                raise StorageError("备份记录格式或内容哈希无效")
            seen.add(record.id)
            records.append(record)
        media = {
            PurePosixPath(name).stem.lower(): entry for name, entry in members.items()
        }
        return records, media

    @staticmethod
    def _legacy(
        payloads: dict[str, bytes], members: dict[str, ArchiveMedia]
    ) -> tuple[list[QuoteRecord], dict[str, ArchiveMedia]]:
        if any(not name.startswith("images/") for name in members):
            raise StorageError("备份清单包含不允许的路径")
        records: list[QuoteRecord] = []
        image_paths: dict[str, str] = {}
        for name, data in sorted(payloads.items()):
            if not (name.startswith("groups/") and name.endswith(".json")):
                raise StorageError(f"备份清单包含不允许的路径: {name}")
            group_id = PurePosixPath(name).stem
            if not group_id.isdigit():
                raise StorageError("群号格式无效")
            try:
                parsed = parse_document(data, group_id)
            except LegacyFormatError as exc:
                raise StorageError(f"群 {group_id} 的数据无效：{exc}") from exc
            for sha256, path in parsed.image_paths.items():
                if (
                    not path.startswith(f"images/{group_id}/")
                    or PurePosixPath(path).stem.lower() != sha256
                ):
                    raise StorageError("记录图片路径或哈希字段无效")
            records.extend(parsed.records)
            image_paths.update(parsed.image_paths)
        media = {
            sha256: members[path]
            for sha256, path in image_paths.items()
            if path in members
        }
        return records, media
