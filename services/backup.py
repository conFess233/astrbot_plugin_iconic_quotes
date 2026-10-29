"""ZIP 备份：导出 v4，导入 v4 与 1.x 旧版（v1–v3），预检后再确认写入。

v4 结构::

    manifest.json        格式、版本、文件哈希清单、可迁移配置
    records.jsonl        每行一条记录
    media/<sha256>.<ext> 被引用的图片与贴纸

导入按群内内容哈希去重合并，不覆盖现有记录。
"""

from __future__ import annotations

import asyncio
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


@dataclass(slots=True)
class ImportPlan:
    """预检结果：待写入记录、待写入媒体与统计。"""

    records: list[QuoteRecord] = field(default_factory=list)
    media: dict[str, bytes] = field(default_factory=dict)
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


class BackupService:
    """导出与导入；所有写入都通过 QuoteStorage 的单事务完成。"""

    def __init__(self, storage: QuoteStorage, media: MediaStore, backups_dir: Path):
        self.storage = storage
        self.media = media
        self.backups_dir = backups_dir
        self._lock = asyncio.Lock()

    # 导出 -------------------------------------------------------------

    async def export(self, settings: dict[str, Any]) -> Path:
        async with self._lock:
            rows = await self.storage.export_rows()
            media = await self.storage.media_rows()
            return await asyncio.to_thread(self._export_sync, rows, media, settings)

    def _export_sync(
        self,
        rows: list[tuple[str, str]],
        media: dict[str, dict[str, Any]],
        settings: dict[str, Any],
    ) -> Path:
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.backups_dir / f"iconic-quotes-{stamp}-{uuid.uuid4().hex[:6]}.zip"
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
                for data in plan.media.values():
                    info, created = await asyncio.to_thread(self.media.write, data)
                    infos[info.sha256] = info
                    if created:
                        written.append(info)
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

    async def _plan(
        self, archive: Path, settings: dict[str, Any], limits: dict[str, int]
    ) -> ImportPlan:
        try:
            records, payloads, manifest_settings = await asyncio.to_thread(
                self._read_archive, archive
            )
        except StorageError:
            raise
        except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as exc:
            raise StorageError("备份文件损坏或格式无效") from exc
        plan = ImportPlan(settings=manifest_settings)
        max_image = settings["max_image_mb"] * 1024 * 1024
        if any(len(data) > max_image for data in payloads.values()):
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
            data = payloads.get(sha256)
            if data is None:
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
            if hashlib.sha256(data).hexdigest() != sha256:
                raise StorageError("记录图片哈希不匹配")
            try:
                _, actual = identify_image(data)
            except ValueError as exc:
                raise StorageError("备份包含无法识别的图片") from exc
            if mime and actual != mime:
                raise StorageError("记录图片类型与登记不一致")
            plan.media[sha256] = data
            if sha256 not in known:
                plan.image_bytes += len(data)
        usage = await self.storage.media_usage()
        if usage + plan.image_bytes > settings["max_media_mb"] * 1024 * 1024:
            raise StorageError("导入会超过全局媒体容量")
        return plan

    def _read_archive(
        self, archive_path: Path
    ) -> tuple[list[QuoteRecord], dict[str, bytes], dict[str, Any]]:
        """读取并校验 ZIP；返回记录、按 sha256 索引的媒体字节与可迁移配置。"""
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
            for name, expected in declared.items():
                data = archive.read(name)
                if hashlib.sha256(data).hexdigest() != expected:
                    raise StorageError(f"备份文件哈希不匹配: {name}")
                payloads[name] = data
        version = manifest.get("schema_version")
        settings = (
            manifest.get("settings")
            if isinstance(manifest.get("settings"), dict)
            else {}
        )
        if version == BACKUP_VERSION:
            records, media = self._v4(payloads)
        elif version in {1, 2, 3}:
            records, media = self._legacy(payloads)
        else:
            raise StorageError(
                "备份由更高版本插件创建，请先升级插件"
                if isinstance(version, int) and version > BACKUP_VERSION
                else f"不支持的备份版本: {version}"
            )
        return records, media, settings

    @staticmethod
    def _v4(payloads: dict[str, bytes]) -> tuple[list[QuoteRecord], dict[str, bytes]]:
        if any(
            name != "records.jsonl" and not name.startswith("media/")
            for name in payloads
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
            PurePosixPath(name).stem.lower(): data
            for name, data in payloads.items()
            if name.startswith("media/")
        }
        return records, media

    @staticmethod
    def _legacy(
        payloads: dict[str, bytes],
    ) -> tuple[list[QuoteRecord], dict[str, bytes]]:
        records: list[QuoteRecord] = []
        image_paths: dict[str, str] = {}
        for name, data in sorted(payloads.items()):
            if name.startswith("images/"):
                continue
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
            sha256: payloads[path]
            for sha256, path in image_paths.items()
            if path in payloads
        }
        return records, media
