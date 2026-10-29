"""插件可视化视图的管理接口：鉴权、参数校验与错误转换集中在 endpoint 装饰器。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import functools
import os
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    file_response,
    json_response,
    request,
)

from ..models import QuoteRecord, remove_forward_node
from ..utils.hashing import calculate_record_hash, normalize_search
from ..utils.validation import validate_numeric_id
from .avatar_cache import AvatarCacheService
from .backup import BackupService
from .media import MediaStore
from .members import MemberService
from .operations import QuoteOperations
from .renderer import CardRenderer
from .settings import SettingsService
from .storage import QuoteStorage, StorageError

IMPORT_TOKEN_SECONDS = 600
MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
MAX_PREVIEW_BYTES = 20 * 1024 * 1024

Handler = Callable[..., Awaitable[Any]]


class ApiError(ValueError):
    """带 HTTP 状态码的接口错误。"""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def endpoint(func: Handler) -> Handler:
    """统一校验 Dashboard 登录，并把业务异常转换为错误响应。"""

    @functools.wraps(func)
    async def wrapper(self: WebApi, *args: Any, **kwargs: Any) -> Any:
        if not request.username:
            return error_response("未登录 Dashboard", status_code=401)
        try:
            result = await func(self, *args, **kwargs)
        except ApiError as exc:
            return error_response(str(exc), status_code=exc.status_code)
        except (StorageError, TypeError, ValueError, OSError) as exc:
            return error_response(str(exc))
        except Exception:  # noqa: BLE001 - 接口边界不向页面暴露未知异常细节。
            logger.exception("群典：管理接口 %s 失败", func.__name__)
            return error_response("操作失败，请查看 AstrBot 日志", status_code=500)
        return result if not isinstance(result, dict) else json_response(result)

    return wrapper


def _alias_targets(record: QuoteRecord) -> list[str]:
    """别名允许覆盖的作者：转发节点与回复快照，不含普通消息作者。"""
    names = [node.author.nickname for node in record.iter_nodes()]
    names += [reply.author.nickname for reply in record.iter_replies()]
    return [normalize_search(name) for name in names]


class WebApi:
    """向已登录 Dashboard 用户提供受校验的管理能力。"""

    def __init__(
        self,
        *,
        config: Any,
        settings: SettingsService,
        storage: QuoteStorage,
        media: MediaStore,
        avatars: AvatarCacheService,
        members: MemberService,
        backups: BackupService,
        cards: CardRenderer,
        operations: QuoteOperations,
        data_root: Path,
    ):
        self.config = config
        self.settings = settings
        self.storage = storage
        self.media = media
        self.avatars = avatars
        self.members = members
        self.backups = backups
        self.cards = cards
        self.operations = operations
        self.data_root = data_root
        self._pending_imports: dict[str, dict[str, Any]] = {}
        self._config_lock = asyncio.Lock()
        self._integrity_lock = asyncio.Lock()

    def routes(self) -> list[tuple[str, Handler, list[str], str]]:
        return [
            ("stats", self.stats, ["GET"], "群典统计"),
            ("records", self.records, ["GET"], "浏览群典"),
            ("records/delete", self.delete, ["POST"], "删除群典"),
            ("records/nodes/delete", self.delete_node, ["POST"], "删除合并转发节点"),
            ("audit", self.audit, ["GET"], "删除审计"),
            ("config", self.get_config, ["GET"], "读取配置"),
            ("config/save", self.save_config, ["POST"], "保存配置"),
            ("aliases/preview", self.preview_aliases, ["POST"], "预览作者别名影响"),
            ("backup/export", self.export, ["GET"], "导出备份"),
            ("backup/import", self.import_backup, ["POST"], "预检导入备份"),
            ("backup/import/commit", self.commit_import, ["POST"], "确认导入备份"),
            ("preview", self.preview, ["POST"], "预览卡片"),
            ("media-data", self.media_data, ["GET"], "预览群典图片"),
            ("avatar-data", self.avatar_data, ["GET"], "预览本地头像"),
            ("avatars/stats", self.avatar_stats, ["GET"], "头像缓存统计"),
            ("avatars/cleanup", self.cleanup_avatars, ["POST"], "清理无引用头像"),
            ("avatars/clear", self.clear_avatars, ["POST"], "清空头像缓存"),
            ("integrity/check", self.integrity_check, ["POST"], "媒体完整性检查"),
        ]

    async def close(self) -> None:
        """插件停止时清理尚未确认的上传文件。"""
        for pending in self._pending_imports.values():
            with contextlib.suppress(OSError):
                Path(pending["path"]).unlink(missing_ok=True)
        self._pending_imports.clear()

    @staticmethod
    async def _json() -> dict[str, Any]:
        payload = await request.json(default=None)
        if not isinstance(payload, dict):
            raise ApiError("请求格式无效")
        return payload

    # 统计与记录 -------------------------------------------------------

    @endpoint
    async def stats(self) -> dict[str, Any]:
        groups = await self.storage.summaries()
        for item in groups:
            item["limit"] = self.settings.for_group(item["group_id"])[
                "max_records_per_group"
            ]
        avatars = await self.avatars.stats()
        return {
            "groups": groups,
            "media_bytes": await self.storage.media_usage(),
            "media_limit": self.settings.global_settings()["max_media_mb"]
            * 1024
            * 1024,
            "avatar_count": avatars["count"],
            "avatar_bytes": avatars["bytes"],
            "storage_root": str(self.data_root),
        }

    @endpoint
    async def records(self) -> dict[str, Any]:
        query = request.query
        group_id = validate_numeric_id(query.get("group_id"), "群号")
        page = max(1, query.get("page", 1, type=int))
        page_size = min(100, max(1, query.get("page_size", 20, type=int)))
        has_image = str(query.get("has_image", "") or "").casefold()
        health = str(query.get("health", "") or "").casefold()
        rows, total = await self.storage.page(
            group_id,
            record_type=str(query.get("type", "") or ""),
            has_image={"true": True, "false": False}.get(has_image),
            healthy={"healthy": True, "broken": False}.get(health),
            needle=normalize_search(str(query.get("q", "") or "")),
            page=page,
            page_size=page_size,
        )
        return {
            "items": [
                {**record.to_dict(), "broken": not healthy} for record, healthy in rows
            ],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @endpoint
    async def delete(self) -> dict[str, Any]:
        payload = await self._json()
        group_id = validate_numeric_id(payload.get("group_id"), "群号")
        raw_ids = payload.get("record_ids")
        if not isinstance(raw_ids, list) or not 1 <= len(raw_ids) <= 100:
            raise ApiError("每次必须选择 1 到 100 条记录")
        deleted, orphans = await self.storage.delete_records(
            group_id,
            {str(value) for value in raw_ids},
            deleted_by=f"dashboard:{request.username}",
            source="page",
            audit_limit=self.settings.global_settings()["audit_limit"],
        )
        await self.operations.delete_files(orphans)
        return {"deleted": deleted}

    @endpoint
    async def delete_node(self) -> dict[str, Any]:
        payload = await self._json()
        group_id = validate_numeric_id(payload.get("group_id"), "群号")
        record_id = str(payload.get("record_id") or "").strip()
        expected = str(payload.get("content_hash") or "").strip()
        path = payload.get("node_path")
        if not record_id or not expected:
            raise ApiError("记录 ID 或内容哈希缺失")
        if (
            not isinstance(path, list)
            or not 1 <= len(path) <= 21
            or len(path) % 2 == 0
            or any(not isinstance(i, int) or isinstance(i, bool) or i < 0 for i in path)
        ):
            raise ApiError("节点路径无效")
        record = await self.storage.get(group_id, record_id)
        if record is None or record.type != "forward":
            raise ApiError("合并转发记录不存在，请刷新页面后重试")
        if record.content_hash != expected:
            raise ApiError("记录内容已发生变化，请刷新页面后重试")
        try:
            removed = remove_forward_node(record.nodes, path)
        except IndexError as exc:
            raise ApiError(f"{exc}，请刷新页面后重试") from exc
        record.source_forward_id = None
        updated: QuoteRecord | None = None
        if record.nodes:
            record.content_hash = calculate_record_hash(record)
            record.identity_incomplete = record.has_unknown_authors()
            updated = record
        orphans = await self.storage.replace_record(
            updated,
            group_id=group_id,
            record_id=record_id,
            expected_hash=expected,
            deleted_by=f"dashboard:{request.username}",
            node_path=path,
            removed_nodes=removed,
            audit_limit=self.settings.global_settings()["audit_limit"],
        )
        await self.operations.delete_files(orphans)
        item = None
        if updated is not None:
            item = {
                **updated.to_dict(),
                "broken": not await self.storage.is_healthy(record_id),
            }
        return {
            "record_deleted": updated is None,
            "removed_nodes": removed,
            "record": item,
        }

    @endpoint
    async def audit(self) -> dict[str, Any]:
        limit = min(1000, max(1, request.query.get("limit", 200, type=int)))
        return {"items": await self.storage.audit_entries(limit)}

    # 配置 -------------------------------------------------------------

    @endpoint
    async def get_config(self) -> dict[str, Any]:
        return self.settings.global_settings()

    @endpoint
    async def save_config(self) -> dict[str, Any]:
        return await self.commit_config(await self._json())

    async def commit_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """串行保存：群级数据写 SQLite，全局配置写宿主；任一失败都恢复两边旧值。"""
        async with self._config_lock:
            values = self.settings.validate_update(payload)
            previous = self.settings.global_settings()
            host_before = copy.deepcopy(dict(self.config))
            await self.storage.save_group_settings(
                values["group_overrides"], values["author_aliases"]
            )
            try:
                self.config.update(SettingsService.host_values(values))
                await self._save_host_config()
            except BaseException:
                self.config.clear()
                self.config.update(host_before)
                with contextlib.suppress(Exception):
                    await self.storage.save_group_settings(
                        previous["group_overrides"], previous["author_aliases"]
                    )
                raise
            self.settings.publish(values)
            return values

    async def _save_host_config(self) -> None:
        saver = getattr(self.config, "save_config_async", None)
        # 写盘不能被取消到一半：等待完成后再发布或回滚。
        task = asyncio.ensure_future(
            saver() if callable(saver) else asyncio.to_thread(self.config.save_config)
        )
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    @endpoint
    async def preview_aliases(self) -> dict[str, Any]:
        """保存别名前只返回受影响记录与节点数量，不泄露正文。"""
        payload = await self._json()
        candidate = SettingsService.validate_aliases(payload.get("author_aliases", {}))
        current = self.settings.global_settings()["author_aliases"]
        affected_records = affected_nodes = 0
        for group_id in sorted(set(current) | set(candidate)):
            old = MemberService.alias_map(group_id, {"author_aliases": current})
            new = MemberService.alias_map(group_id, {"author_aliases": candidate})
            for record in await self.storage.group_records(group_id):
                changed = sum(
                    old.get(name) != new.get(name) for name in _alias_targets(record)
                )
                affected_nodes += changed
                affected_records += changed > 0
        return {"affected_records": affected_records, "affected_nodes": affected_nodes}

    # 备份 -------------------------------------------------------------

    def _limits(self, settings: dict[str, Any]) -> dict[str, int]:
        limits = {"*": int(settings["max_records_per_group"])}
        for group_id, override in settings["group_overrides"].items():
            if "max_records_per_group" in override:
                limits[group_id] = int(override["max_records_per_group"])
        return limits

    @endpoint
    async def export(self) -> Any:
        path = await self.backups.export(self.settings.global_settings())
        return file_response(path, filename=path.name, content_type="application/zip")

    @endpoint
    async def import_backup(self) -> dict[str, Any]:
        upload = (await request.files()).get("file")
        if not isinstance(upload, PluginUploadFile):
            raise ApiError("缺少备份文件")
        temporary: Path | None = None
        try:
            if upload.content_length and upload.content_length > MAX_UPLOAD_BYTES:
                raise ApiError("备份文件不能超过 1 GB")
            self._prune_imports()
            self.backups.backups_dir.mkdir(parents=True, exist_ok=True)
            # 不使用上传文件名落盘；mkstemp 的句柄先关闭再交由框架写入。
            fd, name = tempfile.mkstemp(
                dir=self.backups.backups_dir, prefix=".import-", suffix=".zip"
            )
            os.close(fd)
            temporary = Path(name)
            await upload.save(temporary)
            settings = self.settings.global_settings()
            plan = await self.backups.inspect(
                temporary, settings, self._limits(settings)
            )
            result = plan.summary()
            if plan.settings:
                result["settings"] = self.settings.validate_update(plan.settings)
            aliases = (
                result["settings"].get("author_aliases", {})
                if result["settings"]
                else {}
            )
            result["alias_count"] = sum(
                len(v) for targets in aliases.values() for v in targets.values()
            )
            if plan.missing_images:
                return result
            token = uuid.uuid4().hex
            self._pending_imports[token] = {
                "owner": request.username,
                "path": temporary,
                "expires_at": time.monotonic() + IMPORT_TOKEN_SECONDS,
            }
            temporary = None
            return {"token": token, **result}
        finally:
            await upload.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @endpoint
    async def commit_import(self) -> dict[str, Any]:
        payload = await self._json()
        pending = self._pending_imports.pop(str(payload.get("token") or ""), None)
        if (
            not pending
            or pending["owner"] != request.username
            or pending["expires_at"] < time.monotonic()
        ):
            if pending:
                Path(pending["path"]).unlink(missing_ok=True)
            raise ApiError("导入确认已失效，请重新上传")
        archive = Path(pending["path"])
        try:
            settings = self.settings.global_settings()
            plan = await self.backups.commit(archive, settings, self._limits(settings))
            result = plan.summary()
            aliases = plan.settings.get("author_aliases", {}) if plan.settings else {}
            alias_count = sum(
                len(v) for targets in aliases.values() for v in targets.values()
            )
            if payload.get("restore_settings") and plan.settings:
                try:
                    await self.commit_config(plan.settings)
                except Exception:  # noqa: BLE001 - 记录已导入，配置失败须反馈部分完成。
                    logger.exception("群典：记录已导入，配置恢复失败")
                    result["settings_restore_failed"] = True
                    result["settings_warning"] = (
                        "记录已导入，配置恢复失败，原配置仍然有效。"
                    )
                    return result
                result["aliases_skipped"] = 0
            else:
                result["aliases_skipped"] = alias_count
            return result
        finally:
            archive.unlink(missing_ok=True)

    def _prune_imports(self) -> None:
        now = time.monotonic()
        for token, pending in list(self._pending_imports.items()):
            if pending["expires_at"] < now:
                Path(pending["path"]).unlink(missing_ok=True)
                self._pending_imports.pop(token, None)

    # 媒体与预览 -------------------------------------------------------

    @endpoint
    async def media_data(self) -> dict[str, Any]:
        sha256 = str(request.query.get("sha256") or "").strip().lower()
        row = (await self.storage.media_rows([sha256])).get(sha256)
        if row is None:
            raise ApiError("图片不存在", status_code=404)
        if row["size"] > MAX_PREVIEW_BYTES:
            raise ApiError("图片过大，无法在页面中预览")
        try:
            return {
                "data_url": await asyncio.to_thread(
                    self.media.data_url, sha256, row["mime"]
                )
            }
        except OSError as exc:
            raise ApiError("图片文件缺失", status_code=404) from exc

    @endpoint
    async def avatar_data(self) -> dict[str, Any]:
        user_id = validate_numeric_id(request.query.get("user_id"), "QQ 号")
        return {"data_url": await self.avatars.cached_data_url(user_id)}

    @endpoint
    async def preview(self) -> dict[str, Any]:
        payload = await self._json()
        group_id = validate_numeric_id(payload.get("group_id"), "群号")
        record = await self.storage.get(group_id, str(payload.get("record_id") or ""))
        if record is None:
            raise ApiError("记录不存在")
        if record.type != "message":
            raise ApiError("合并转发不生成卡片预览")
        if record.has_native_segments():
            raise ApiError("包含表情、贴纸或回复快照的记录不生成 CSS 卡片")
        paths: list[str] = []
        try:
            try:
                paths = await self.cards.card_paths(
                    record, self.settings.for_group(group_id)
                )
            except Exception as exc:
                raise ApiError("卡片预览生成失败") from exc
            images = []
            for path in paths:
                data = await asyncio.to_thread(Path(path).read_bytes)
                images.append(
                    f"data:image/png;base64,{base64.b64encode(data).decode()}"
                )
            return {"images": images}
        finally:
            for path in paths:
                with contextlib.suppress(OSError):
                    Path(path).unlink(missing_ok=True)

    @endpoint
    async def avatar_stats(self) -> dict[str, Any]:
        return await self.avatars.stats()

    @endpoint
    async def cleanup_avatars(self) -> dict[str, Any]:
        return await self.avatars.cleanup_unreferenced()

    @endpoint
    async def clear_avatars(self) -> dict[str, Any]:
        return await self.avatars.clear()

    @endpoint
    async def integrity_check(self) -> dict[str, Any]:
        """逐个重新计算媒体 SHA-256，刷新缺失标记。"""
        if self._integrity_lock.locked():
            raise ApiError("完整性检查正在进行")
        async with self._integrity_lock:
            rows = await self.storage.media_rows()

            def verify() -> dict[str, bool]:
                return {
                    sha: not self.media.verify(sha, row["mime"])
                    for sha, row in rows.items()
                }

            states = await asyncio.to_thread(verify)
            await self.storage.set_media_state(states, verified=True)
            summaries = await self.storage.summaries()
        return {
            "checked": len(states),
            "missing": sum(states.values()),
            "restored": sum(
                1
                for sha, missing in states.items()
                if not missing and rows[sha]["missing"]
            ),
            "broken_records": sum(item["broken"] for item in summaries),
        }
