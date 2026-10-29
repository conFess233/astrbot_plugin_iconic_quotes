"""1.x JSON 数据（Schema v1–v3）的解析，以及首次启动时迁移到 SQLite。

旧版每群一个 ``groups/<群号>.json``，图片位于 ``images/<群号>/<sha256>.<扩展名>``。
迁移只读旧目录，成功后把旧目录改名保留为备份，不删除任何旧文件。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

from ..models import QuoteRecord
from ..utils.hashing import calculate_record_hash
from ..utils.timefmt import error_reason
from .media import MediaInfo, MediaStore
from .storage import QuoteStorage, StorageError

LEGACY_VERSIONS = {1, 2, 3}
MIGRATED_META = "legacy_migrated_from"


class LegacyFormatError(ValueError):
    """旧版数据格式无效。"""


@dataclass(slots=True)
class LegacyRecords:
    """解析后的旧记录与其引用的图片相对路径。"""

    records: list[QuoteRecord] = field(default_factory=list)
    image_paths: dict[str, str] = field(default_factory=dict)


def _collect_media_paths(value: Any, result: dict[str, str]) -> None:
    if isinstance(value, dict):
        sha256, path = value.get("sha256"), value.get("path")
        if isinstance(sha256, str) and isinstance(path, str):
            result[sha256.lower()] = path
        for item in value.values():
            _collect_media_paths(item, result)
    elif isinstance(value, list):
        for item in value:
            _collect_media_paths(item, result)


def parse_document(raw: bytes, group_id: str) -> LegacyRecords:
    """校验旧版群文档并把内容哈希升级到当前算法。"""
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LegacyFormatError("群典 JSON 无法解析") from exc
    if not isinstance(document, dict):
        raise LegacyFormatError("群典 JSON 根节点不是对象")
    version = document.get("schema_version")
    if version not in LEGACY_VERSIONS:
        raise LegacyFormatError(f"不支持的数据版本: {version}")
    if str(document.get("group_id")) != str(group_id):
        raise LegacyFormatError("群典 JSON 的群号与文件名不一致")
    if not isinstance(document.get("records"), list):
        raise LegacyFormatError("群典 JSON 缺少记录列表")
    result = LegacyRecords()
    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for raw_record in document["records"]:
        if not isinstance(raw_record, dict):
            raise LegacyFormatError("群典记录格式无效")
        try:
            record = QuoteRecord.from_dict(raw_record)
        except (KeyError, TypeError, ValueError) as exc:
            raise LegacyFormatError(f"群典记录格式无效：{exc}") from exc
        if record.group_id != str(group_id):
            raise LegacyFormatError("记录群号与所在文件不一致")
        if not record.id or record.id in seen_ids:
            raise LegacyFormatError("记录 ID 为空或重复")
        if calculate_record_hash(record, schema_version=version) != record.content_hash:
            raise LegacyFormatError("记录内容与哈希不一致")
        if not record.has_content():
            raise LegacyFormatError("记录缺少作者或内容")
        record.content_hash = calculate_record_hash(record)
        seen_ids.add(record.id)
        # v1/v2 升级后的哈希理论上可能重合，按先到先得保留一条。
        if record.content_hash in seen_hashes:
            continue
        seen_hashes.add(record.content_hash)
        result.records.append(record)
        _collect_media_paths(raw_record, result.image_paths)
    return result


def load_group_file(path: Path) -> LegacyRecords:
    """读取旧群文件；主文件损坏时尝试同名 .bak。"""
    errors: list[str] = []
    for candidate in (path, path.with_suffix(path.suffix + ".bak")):
        if not candidate.is_file():
            continue
        try:
            return parse_document(candidate.read_bytes(), path.stem)
        except (OSError, LegacyFormatError) as exc:
            errors.append(f"{candidate.name}: {exc}")
    raise LegacyFormatError("；".join(errors) or "文件不存在")


def store_media(
    media: MediaStore,
    records: list[QuoteRecord],
    read: Callable[[str, str], bytes | None],
) -> tuple[dict[str, MediaInfo], dict[str, MediaInfo]]:
    """把记录引用的图片写入新媒体目录。

    read(sha256, mime) 返回旧图片字节或 None。返回（已写入, 缺失）两组媒体信息；
    缺失的媒体仍然登记，标记为 missing，记录照常导入。
    """
    stored: dict[str, MediaInfo] = {}
    missing: dict[str, MediaInfo] = {}
    for record in records:
        for segment in record.image_segments():
            sha256 = segment.sha256
            if not sha256 or sha256 in stored or sha256 in missing:
                continue
            data = read(sha256, segment.mime or "")
            info = None
            if data is not None:
                try:
                    candidate, _ = media.write(data)
                    if candidate.sha256 == sha256:
                        info = candidate
                except ValueError:
                    info = None
            if info is None:
                missing[sha256] = MediaInfo(
                    sha256, segment.mime or "image/png", max(1, segment.size or 1)
                )
            else:
                stored[sha256] = info
    return stored, missing


class LegacyMigrator:
    """首次启动时把旧 JSON 目录迁入 SQLite。"""

    def __init__(self, storage: QuoteStorage, media: MediaStore, avatars_dir: Path):
        self.storage = storage
        self.media = media
        self.avatars_dir = avatars_dir

    @staticmethod
    def find_root(candidates: list[Path]) -> Path | None:
        for root in candidates:
            if (root / "groups").is_dir() and any((root / "groups").glob("*.json")):
                return root
        return None

    async def migrate(self, candidates: list[Path]) -> Path | None:
        """返回旧目录改名后的备份路径；没有旧数据或已迁移时返回 None。"""
        if await self.storage.meta(MIGRATED_META):
            return None
        root = self.find_root(candidates)
        if root is None:
            return None
        if await self.storage.all_record_ids():
            logger.warning("群典：数据库已有记录，跳过旧目录迁移；旧目录=%s", root)
            await self.storage.set_meta(MIGRATED_META, f"skipped:{root}")
            return None
        started = time.monotonic()
        records: list[QuoteRecord] = []
        image_paths: dict[str, str] = {}
        failed: list[str] = []
        for path in sorted((root / "groups").glob("*.json")):
            if not path.stem.isdigit():
                continue
            try:
                parsed = await asyncio.to_thread(load_group_file, path)
            except LegacyFormatError as exc:
                failed.append(path.stem)
                logger.warning(
                    "群典：旧群数据无法迁移，已跳过；群=%s，原因=%s", path.stem, exc
                )
                continue
            records.extend(parsed.records)
            image_paths.update(parsed.image_paths)

        def read(sha256: str, _mime: str) -> bytes | None:
            relative = image_paths.get(sha256)
            if not relative:
                return None
            target = (root / relative).resolve()
            if root.resolve() not in target.parents or not target.is_file():
                return None
            return target.read_bytes()

        stored, missing = await asyncio.to_thread(
            store_media, self.media, records, read
        )
        await self.storage.import_records(
            records, [*stored.values(), *missing.values()], lambda _group: 1 << 31
        )
        await self.storage.set_media_state({sha: True for sha in missing})
        await self._migrate_audit(root)
        await self._migrate_avatars(root)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = root.with_name(f"{root.name}.migrated-{stamp}")
        try:
            await asyncio.to_thread(os.replace, root, backup)
        except OSError as exc:
            # 改名失败不影响已经提交的迁移；记录元数据防止重复导入。
            logger.warning(
                "群典：旧目录改名失败，请手动备份；原因=%s", error_reason(exc)
            )
            backup = root
        await self.storage.set_meta(MIGRATED_META, str(backup))
        logger.info(
            "群典：旧数据迁移完成；记录=%s，媒体=%s，缺失媒体=%s，跳过群=%s，耗时=%.1fs，旧目录备份=%s",
            len(records),
            len(stored),
            len(missing),
            ",".join(failed) or "无",
            time.monotonic() - started,
            backup,
        )
        return backup

    async def _migrate_audit(self, root: Path) -> None:
        path = root / "audit.json"
        try:
            entries = json.loads(path.read_text("utf-8-sig")) if path.is_file() else []
        except (OSError, UnicodeError, json.JSONDecodeError):
            logger.warning("群典：旧删除审计无法读取，已跳过")
            return
        if isinstance(entries, list):
            try:
                await self.storage.import_audit(entries)
            except StorageError as exc:
                logger.warning("群典：旧删除审计迁移失败；原因=%s", error_reason(exc))

    async def _migrate_avatars(self, root: Path) -> None:
        source = root / "avatars"
        if not source.is_dir():
            return
        try:
            index = json.loads((source / "index.json").read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            index = {}
        rows: dict[str, tuple[float, float, int]] = {}

        def copy() -> None:
            self.avatars_dir.mkdir(parents=True, exist_ok=True)
            for path in source.glob("*.jpg"):
                if not path.stem.isdigit() or not path.is_file():
                    continue
                target = self.avatars_dir / path.name
                if not target.exists():
                    target.write_bytes(path.read_bytes())
                item = index.get(path.stem) if isinstance(index, dict) else None
                item = item if isinstance(item, dict) else {}
                try:
                    rows[path.stem] = (
                        float(item.get("fetched_at", 0)),
                        float(item.get("last_used", 0)),
                        target.stat().st_size,
                    )
                except (TypeError, ValueError):
                    rows[path.stem] = (0.0, 0.0, target.stat().st_size)

        await asyncio.to_thread(copy)
        await self.storage.avatar_upsert(rows)
