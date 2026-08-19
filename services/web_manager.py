"""AstrBot Plugin Pages 的后台管理 API。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import mimetypes
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api.web import (
    PluginUploadFile,
    error_response,
    file_response,
    json_response,
    request,
)

from ..models import QuoteRecord
from ..utils.hashing import normalize_search
from ..utils.validation import validate_numeric_id
from .settings import DEFAULTS, SettingsService
from .storage import QuoteStorage, StorageError


class WebManager:
    """向已登录 Dashboard 用户提供受校验的管理能力。"""

    def __init__(
        self,
        storage: QuoteStorage,
        settings: SettingsService,
        renderer: Any,
        config: Any,
    ):
        self.storage = storage
        self.settings = settings
        self.renderer = renderer
        self.config = config
        self._pending_imports: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _authenticated() -> bool:
        return bool(request.username)

    async def stats(self):
        """返回各群数量及全局磁盘占用。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        groups = await self.storage.list_groups()
        rows = []
        for group_id in groups:
            values = self.settings.for_group(group_id)
            info = await self.storage.info(group_id, values["max_records_per_group"])
            rows.append({"group_id": group_id, **info})
        return json_response(
            {
                "groups": rows,
                "media_bytes": await self.storage.media_usage_bytes(),
                "storage_root": str(self.storage.root),
            }
        )

    async def records(self):
        """分页查询完整记录元数据。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        try:
            group_id = validate_numeric_id(request.query.get("group_id"), "群号")
            page = max(1, request.query.get("page", 1, type=int))
            page_size = min(100, max(1, request.query.get("page_size", 20, type=int)))
            record_type = str(request.query.get("type", "") or "")
            has_image = str(request.query.get("has_image", "") or "").casefold()
            health = str(request.query.get("health", "") or "").casefold()
            needle = normalize_search(str(request.query.get("q", "") or ""))
            records = await self.storage.records(group_id)
            if record_type in {"message", "forward"}:
                records = [record for record in records if record.type == record_type]
            if has_image in {"true", "false"}:
                expected = has_image == "true"
                records = [
                    record
                    for record in records
                    if bool(record.image_segments()) == expected
                ]
            if needle:
                records = [
                    record for record in records if self._matches(record, needle)
                ]
            if health in {"healthy", "broken"}:
                expected = health == "healthy"
                checked = []
                for record in records:
                    if await self.storage.record_is_healthy(record) == expected:
                        checked.append(record)
                records = checked
            records.sort(key=lambda item: item.recorded_at, reverse=True)
            start = (page - 1) * page_size
            page_records = records[start : start + page_size]
            items = []
            for record in page_records:
                item = record.to_dict()
                item["broken"] = not await self.storage.record_is_healthy(record)
                items.append(item)
            return json_response(
                {
                    "items": items,
                    "total": len(records),
                    "page": page,
                    "page_size": page_size,
                }
            )
        except (ValueError, StorageError) as exc:
            return error_response(str(exc))

    @staticmethod
    def _matches(record: QuoteRecord, needle: str) -> bool:
        values = [record.id, record.searchable_text()]
        if record.author:
            values.extend([record.author.user_id or "", record.author.nickname])
        for node in record.nodes:
            values.extend([node.author.user_id or "", node.author.nickname])
        return any(needle in normalize_search(value) for value in values)

    async def delete(self):
        """从后台按 ID 精确批量删除。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        payload = await request.json(default={})
        try:
            if not isinstance(payload, dict):
                raise TypeError("请求格式无效")
            group_id = validate_numeric_id(payload.get("group_id"), "群号")
            raw_ids = payload.get("record_ids")
            if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 100:
                raise ValueError("每次必须选择 1 到 100 条记录")
            record_ids = {str(value) for value in raw_ids}
            deleted = await self.storage.delete_ids(
                group_id,
                record_ids,
                deleted_by=f"dashboard:{request.username}",
                source="page",
                audit_limit=self.settings.global_settings()["audit_limit"],
            )
            return json_response({"deleted": deleted})
        except (TypeError, ValueError, StorageError) as exc:
            return error_response(str(exc))

    async def audit(self):
        """返回最近删除审计。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        limit = min(1000, max(1, request.query.get("limit", 200, type=int)))
        return json_response({"items": await self.storage.audit_entries(limit)})

    async def get_config(self):
        """返回管理页可编辑的当前配置。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        return json_response(self.settings.global_settings())

    async def save_config(self):
        """保存非迁移类配置，并立即热更新。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        payload = await request.json(default={})
        try:
            if not isinstance(payload, dict):
                raise TypeError("请求格式无效")
            if "storage_subdir" in payload and payload[
                "storage_subdir"
            ] != self.config.get("storage_subdir"):
                raise ValueError("存储路径只能通过迁移操作修改")
            values = self.settings.update_from_page(payload)
            await self._save_config()
            return json_response(values)
        except (TypeError, ValueError) as exc:
            return error_response(str(exc))

    async def export(self):
        """下载不包含环境路径的完整 ZIP 备份。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        settings = self.settings.global_settings()
        portable = {key: settings[key] for key in DEFAULTS if key != "storage_subdir"}
        path = await self.storage.export_zip(portable)
        return file_response(
            path,
            filename=path.name,
            content_type="application/zip",
        )

    async def import_backup(self):
        """上传并完整预检 ZIP，返回一次性确认令牌。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        files = await request.files()
        upload = files.get("file")
        if not isinstance(upload, PluginUploadFile):
            return error_response("缺少备份文件")
        if upload.content_length and upload.content_length > 1024 * 1024 * 1024:
            return error_response("备份文件不能超过 1 GB")
        temporary: Path | None = None
        try:
            self._prune_pending_imports()
            self.storage.backups_dir.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(
                dir=self.storage.backups_dir,
                prefix=".import-",
                suffix=".zip",
            )
            # mkstemp 的句柄必须先关闭，再交由框架异步写入。
            os.close(fd)
            temporary = Path(name)
            await upload.save(temporary)
            settings = self.settings.global_settings()
            result = await self.storage.inspect_zip(
                temporary,
                max_records=self._record_limits(settings),
                max_media_bytes=settings["max_media_mb"] * 1024 * 1024,
                max_image_bytes=settings["max_image_mb"] * 1024 * 1024,
            )
            portable = result.get("settings") or {}
            if portable:
                probe = SettingsService(dict(self.config))
                result["settings"] = probe.update_from_page(portable)
            if result["missing_images"]:
                temporary.unlink(missing_ok=True)
                temporary = None
                return json_response(result)
            token = uuid.uuid4().hex
            self._pending_imports[token] = {
                "owner": request.username,
                "path": temporary,
                "expires_at": time.monotonic() + 600,
                "result": result,
            }
            temporary = None
            return json_response({"token": token, **result})
        except (OSError, TypeError, ValueError, StorageError) as exc:
            return error_response(str(exc))
        finally:
            await upload.close()
            if temporary:
                temporary.unlink(missing_ok=True)

    async def commit_import(self):
        """确认并执行已预检的 ZIP 合并。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        payload = await request.json(default={})
        token = str(payload.get("token") or "") if isinstance(payload, dict) else ""
        pending = self._pending_imports.pop(token, None)
        if (
            not pending
            or pending["owner"] != request.username
            or pending["expires_at"] < time.monotonic()
        ):
            if pending:
                Path(pending["path"]).unlink(missing_ok=True)
            return error_response("导入确认已失效，请重新上传")
        archive_path = Path(pending["path"])
        try:
            settings = self.settings.global_settings()
            result = await self.storage.import_zip(
                archive_path,
                max_records=self._record_limits(settings),
                max_media_bytes=settings["max_media_mb"] * 1024 * 1024,
                max_image_bytes=settings["max_image_mb"] * 1024 * 1024,
            )
            if bool(payload.get("restore_settings")) and result.get("settings"):
                self.settings.update_from_page(result["settings"])
                await self._save_config()
            return json_response(result)
        except (OSError, TypeError, ValueError, StorageError) as exc:
            return error_response(str(exc))
        finally:
            archive_path.unlink(missing_ok=True)

    def _prune_pending_imports(self) -> None:
        now = time.monotonic()
        for token, pending in list(self._pending_imports.items()):
            if pending["expires_at"] >= now:
                continue
            Path(pending["path"]).unlink(missing_ok=True)
            self._pending_imports.pop(token, None)

    async def close(self) -> None:
        """插件停止时清理尚未确认的上传文件。"""
        for pending in self._pending_imports.values():
            with contextlib.suppress(OSError):
                Path(pending["path"]).unlink(missing_ok=True)
        self._pending_imports.clear()

    @staticmethod
    def _record_limits(settings: dict[str, Any]) -> dict[str, int]:
        limits = {"*": int(settings["max_records_per_group"])}
        for group_id, override in settings.get("group_overrides", {}).items():
            if "max_records_per_group" in override:
                limits[str(group_id)] = int(override["max_records_per_group"])
        return limits

    async def migrate(self):
        """显式迁移已有数据并保存新相对路径。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        payload = await request.json(default={})
        try:
            if not isinstance(payload, dict):
                raise TypeError("请求格式无效")
            new_subdir = str(payload.get("storage_subdir") or "").strip()
            new_root, backup_root, old_root = await self.storage.migrate_to(new_subdir)
            old_value = self.config.get("storage_subdir")
            try:
                self.config["storage_subdir"] = new_subdir
                await self._save_config()
            except Exception:
                self.config["storage_subdir"] = old_value
                await self.storage.rollback_migration(
                    old_root,
                    new_root,
                    backup_root,
                )
                raise
            return json_response(
                {"storage_root": str(new_root), "backup_root": str(backup_root)}
            )
        except (OSError, TypeError, ValueError, StorageError) as exc:
            return error_response(str(exc))

    async def media(self, relative_path: str):
        """通过认证后的插件路由读取单张存档图片。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        try:
            if not relative_path.startswith("images/"):
                raise StorageError("只能读取群典图片")
            path = self.storage.resolve_media_path(relative_path)
            return file_response(
                path,
                filename=path.name,
                content_type=mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
            )
        except StorageError as exc:
            return error_response(str(exc), status_code=404)

    async def media_data(self):
        """返回管理页面预览所需的短期 data URL。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        try:
            relative_path = str(request.query.get("path") or "")
            if not relative_path.startswith("images/"):
                raise StorageError("只能读取群典图片")
            path = self.storage.resolve_media_path(relative_path)
            data = await asyncio.to_thread(path.read_bytes)
            if len(data) > 20 * 1024 * 1024:
                raise StorageError("图片过大，无法在页面中预览")
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            return json_response(
                {"data_url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}
            )
        except StorageError as exc:
            return error_response(str(exc), status_code=404)

    async def preview(self):
        """把指定普通记录渲染为卡片预览数据。"""
        if not self._authenticated():
            return error_response("未登录 Dashboard", status_code=401)
        payload = await request.json(default={})
        paths: list[str] = []
        try:
            group_id = validate_numeric_id(payload.get("group_id"), "群号")
            record_id = str(payload.get("record_id") or "")
            record = next(
                item
                for item in await self.storage.records(group_id)
                if item.id == record_id
            )
            if record.type != "message":
                raise ValueError("合并转发不生成卡片预览")
            if record.has_native_segments():
                raise ValueError("包含表情、贴纸或回复快照的记录不生成 CSS 卡片")
            paths = await self.renderer.card_paths(
                record, self.settings.for_group(group_id)
            )
            images = []
            for path_value in paths:
                path = Path(path_value)
                data = await asyncio.to_thread(path.read_bytes)
                mime = mimetypes.guess_type(path.name)[0] or "image/png"
                images.append(f"data:{mime};base64,{base64.b64encode(data).decode()}")
            return json_response({"images": images})
        except (StopIteration, ValueError, StorageError) as exc:
            return error_response(
                "记录不存在" if isinstance(exc, StopIteration) else str(exc)
            )
        except Exception:  # noqa: BLE001 - 渲染后端异常类型不稳定，页面只返回通用错误。
            return error_response("卡片预览生成失败")
        finally:
            for path_value in paths:
                with contextlib.suppress(OSError):
                    Path(path_value).unlink(missing_ok=True)

    async def _save_config(self) -> None:
        saver = getattr(self.config, "save_config_async", None)
        if callable(saver):
            await saver()
            return
        self.config.save_config()
