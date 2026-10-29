"""SQLite 持久化：连接、迁移、记录、媒体引用、头像索引、审计与群级设置。

所有方法在线程中执行（``asyncio.to_thread``），单连接由线程锁串行化；
写操作使用短 ``BEGIN IMMEDIATE`` 事务，网络、渲染和文件下载都在事务外完成。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, TypeVar

from ..models import QuoteRecord
from ..utils.hashing import normalize_search
from ..utils.timefmt import now_iso
from .media import MediaInfo

T = TypeVar("T")
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "resources" / "migrations"
_HEALTHY = """NOT EXISTS (
    SELECT 1 FROM record_media rm JOIN media m ON m.sha256 = rm.sha256
    WHERE rm.record_id = r.id AND m.missing = 1)"""


class StorageError(RuntimeError):
    """可向用户归类说明的存储错误。"""


class DuplicateQuoteError(StorageError):
    """当前群已经存在相同内容哈希。"""

    def __init__(self, record: QuoteRecord):
        super().__init__("该群典已存在")
        self.record = record


def record_index(record: QuoteRecord) -> dict[str, Any]:
    """计算记录的查询索引：参与者、可纠正昵称与正文。"""
    involved: set[str] = set()
    if record.type == "message" and record.author and record.author.user_id:
        involved.add(record.author.user_id)
    involved.update(
        node.author.user_id for node in record.iter_nodes() if node.author.user_id
    )
    users = {author.user_id for author in record.iter_authors() if author.user_id}
    names = {
        normalize_search(author.nickname)
        for author in record.iter_authors()
        if author is not record.author and author.nickname
    }
    names.discard("")
    return {
        "participants": [
            (
                user_id,
                int(user_id in involved),
                normalize_search(record.authored_text(user_id)),
            )
            for user_id in sorted(users)
        ],
        "names": sorted(names),
        "search_text": normalize_search(record.searchable_text()),
    }


class QuoteStorage:
    """群典 SQLite 仓储。"""

    def __init__(self, database_path: Path):
        self.database_path = database_path
        self._connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # 生命周期 ---------------------------------------------------------

    async def open(self) -> None:
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.database_path,
            timeout=5,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA journal_mode=WAL")
            # WAL 下 NORMAL 不会损坏数据库；只有断电时可能丢失最后几次提交。
            connection.execute("PRAGMA synchronous=NORMAL")
            problems = connection.execute("PRAGMA quick_check").fetchall()
            if len(problems) != 1 or problems[0][0] != "ok":
                raise StorageError("群典数据库完整性检查未通过，已停止初始化")
            self._connection = connection
            self._migrate()
        except Exception:
            connection.close()
            self._connection = None
            raise

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def _db(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StorageError("群典数据库尚未打开")
        return self._connection

    @contextlib.contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            db = self._db()
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.rollback()
                raise
            else:
                try:
                    db.commit()
                except sqlite3.Error as exc:
                    db.rollback()
                    raise StorageError(f"数据库提交失败：{exc}") from exc

    @contextlib.contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._db()

    async def _run(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        try:
            return await asyncio.to_thread(func, *args, **kwargs)
        except sqlite3.Error as exc:
            raise StorageError(f"数据库操作失败：{exc}") from exc

    def _migrate(self) -> None:
        db = self._db()
        db.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL, checksum TEXT NOT NULL)"
        )
        applied = {
            int(row["version"]): row["checksum"]
            for row in db.execute("SELECT version, checksum FROM schema_migrations")
        }
        scripts = sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))
        known = {int(path.name[:3]) for path in scripts}
        if applied and max(applied) > max(known, default=0):
            raise StorageError("数据库由更高版本插件创建，请先升级插件")
        for path in scripts:
            version = int(path.name[:3])
            sql = path.read_text("utf-8")
            checksum = hashlib.sha256(sql.encode()).hexdigest()
            if version in applied:
                if applied[version] != checksum:
                    raise StorageError(f"迁移脚本 {path.name} 已被修改")
                continue
            # executescript 会先提交，迁移脚本自身不含事务语句，这里显式包裹。
            with self._lock:
                db.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split_sql(sql):
                        db.execute(statement)
                    db.execute(
                        "INSERT INTO schema_migrations VALUES (?, ?, ?)",
                        (version, now_iso(), checksum),
                    )
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise

    async def backup_to(self, target: Path) -> None:
        """使用 SQLite backup API 生成一致性快照。"""

        def run() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            with self._read() as db, contextlib.closing(sqlite3.connect(target)) as out:
                db.backup(out)

        await self._run(run)

    # 元数据 -----------------------------------------------------------

    async def meta(self, key: str) -> str | None:
        def run() -> str | None:
            with self._read() as db:
                row = db.execute(
                    "SELECT value FROM meta WHERE key = ?", (key,)
                ).fetchone()
            return row["value"] if row else None

        return await self._run(run)

    async def set_meta(self, key: str, value: str) -> None:
        def run() -> None:
            with self._tx() as db:
                db.execute(
                    "INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

        await self._run(run)

    # 记录写入 ---------------------------------------------------------

    @staticmethod
    def _insert_record(
        db: sqlite3.Connection, record: QuoteRecord, media: dict[str, MediaInfo]
    ) -> None:
        index = record_index(record)
        db.execute(
            "INSERT INTO records (id, group_id, type, content_hash, author_id, recorded_at,"
            " search_text, has_media, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.group_id,
                record.type,
                record.content_hash,
                record.author.user_id if record.author else None,
                record.recorded_at,
                index["search_text"],
                int(bool(record.media_hashes())),
                json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":")),
            ),
        )
        db.executemany(
            "INSERT INTO record_participants VALUES (?, ?, ?, ?)",
            [(record.id, *row) for row in index["participants"]],
        )
        db.executemany(
            "INSERT INTO record_names VALUES (?, ?)",
            [(record.id, name) for name in index["names"]],
        )
        now = now_iso()
        for sha256 in record.media_hashes():
            info = media.get(sha256)
            if info is not None:
                db.execute(
                    "INSERT INTO media (sha256, mime, size, created_at, verified_at)"
                    " VALUES (?, ?, ?, ?, ?) ON CONFLICT(sha256) DO UPDATE SET missing = 0,"
                    " verified_at = excluded.verified_at",
                    (sha256, info.mime, info.size, now, now),
                )
            elif not db.execute(
                "SELECT 1 FROM media WHERE sha256 = ?", (sha256,)
            ).fetchone():
                raise StorageError("记录引用了未登记的媒体")
            db.execute("INSERT INTO record_media VALUES (?, ?)", (record.id, sha256))

    def _row_record(self, row: sqlite3.Row) -> QuoteRecord:
        return QuoteRecord.from_dict(json.loads(row["payload"]))

    async def add(
        self,
        record: QuoteRecord,
        max_records: int,
        media: Iterable[MediaInfo] = (),
    ) -> None:
        """原子添加记录；同群内容哈希重复或达到条数上限时拒绝。"""
        media_map = {info.sha256: info for info in media}

        def run() -> None:
            with self._tx() as db:
                existing = db.execute(
                    "SELECT payload FROM records WHERE group_id = ? AND content_hash = ?",
                    (record.group_id, record.content_hash),
                ).fetchone()
                if existing:
                    raise DuplicateQuoteError(self._row_record(existing))
                (count,) = db.execute(
                    "SELECT COUNT(*) FROM records WHERE group_id = ?",
                    (record.group_id,),
                ).fetchone()
                if count >= max_records:
                    raise StorageError("当前群的群典条数已达到上限")
                self._insert_record(db, record, media_map)

        await self._run(run)

    async def import_records(
        self,
        records: list[QuoteRecord],
        media: Iterable[MediaInfo],
        limits: Callable[[str], int],
    ) -> None:
        """单事务写入已预检的导入记录；任一冲突则整体回滚。"""
        media_map = {info.sha256: info for info in media}

        def run() -> None:
            try:
                with self._tx() as db:
                    counts: dict[str, int] = {}
                    for record in records:
                        if record.group_id not in counts:
                            (counts[record.group_id],) = db.execute(
                                "SELECT COUNT(*) FROM records WHERE group_id = ?",
                                (record.group_id,),
                            ).fetchone()
                        if counts[record.group_id] >= limits(record.group_id):
                            raise StorageError(
                                f"群 {record.group_id} 导入后会超过记录上限"
                            )
                        self._insert_record(db, record, media_map)
                        counts[record.group_id] += 1
            except sqlite3.IntegrityError as exc:
                raise StorageError("导入期间数据已发生变化，请重新预检") from exc

        await self._run(run)

    @staticmethod
    def _audit(
        db: sqlite3.Connection, limit: int, entries: list[tuple[Any, ...]]
    ) -> None:
        db.executemany(
            "INSERT INTO audit_log (group_id, record_id, content_hash, deleted_by,"
            " deleted_at, source, node_path, removed_nodes, record_deleted)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            entries,
        )
        db.execute(
            "DELETE FROM audit_log WHERE id <= (SELECT MAX(id) FROM audit_log) - ?",
            (limit,),
        )

    @staticmethod
    def _orphan_media(
        db: sqlite3.Connection, candidates: set[str]
    ) -> list[tuple[str, str]]:
        """删除不再被引用的媒体行，返回需删除文件的 (sha256, mime)。"""
        result: list[tuple[str, str]] = []
        for sha256 in candidates:
            if db.execute(
                "SELECT 1 FROM record_media WHERE sha256 = ?", (sha256,)
            ).fetchone():
                continue
            row = db.execute(
                "SELECT mime FROM media WHERE sha256 = ?", (sha256,)
            ).fetchone()
            if row:
                db.execute("DELETE FROM media WHERE sha256 = ?", (sha256,))
                result.append((sha256, row["mime"]))
        return result

    async def delete_records(
        self,
        group_id: str,
        record_ids: set[str],
        *,
        deleted_by: str,
        source: str,
        expected_hashes: dict[str, str] | None = None,
        audit_limit: int = 10_000,
    ) -> tuple[int, list[tuple[str, str]]]:
        """按 ID 删除并写审计；返回删除数量与待删除的孤儿媒体文件。"""

        def run() -> tuple[int, list[tuple[str, str]]]:
            with self._tx() as db:
                marks = ",".join("?" * len(record_ids))
                rows = db.execute(
                    f"SELECT id, content_hash FROM records WHERE group_id = ? AND id IN ({marks})",
                    (group_id, *record_ids),
                ).fetchall()
                if len(rows) != len(record_ids) or (
                    expected_hashes
                    and any(
                        expected_hashes.get(row["id"]) != row["content_hash"]
                        for row in rows
                    )
                ):
                    raise StorageError("待删除记录已发生变化，请重新执行删除命令")
                shas = {
                    row["sha256"]
                    for row in db.execute(
                        f"SELECT sha256 FROM record_media WHERE record_id IN ({marks})",
                        tuple(record_ids),
                    )
                }
                db.execute(
                    f"DELETE FROM records WHERE id IN ({marks})", tuple(record_ids)
                )
                now = now_iso()
                self._audit(
                    db,
                    audit_limit,
                    [
                        (
                            group_id,
                            row["id"],
                            row["content_hash"],
                            deleted_by,
                            now,
                            source,
                            None,
                            None,
                            None,
                        )
                        for row in rows
                    ],
                )
                return len(rows), self._orphan_media(db, shas)

        if not record_ids:
            return 0, []
        return await self._run(run)

    async def replace_record(
        self,
        record: QuoteRecord | None,
        *,
        group_id: str,
        record_id: str,
        expected_hash: str,
        deleted_by: str,
        node_path: list[int],
        removed_nodes: int,
        audit_limit: int,
    ) -> list[tuple[str, str]]:
        """节点删除：用新内容替换记录（record 为 None 表示整条删除）。"""

        def run() -> list[tuple[str, str]]:
            with self._tx() as db:
                row = db.execute(
                    "SELECT content_hash FROM records WHERE group_id = ? AND id = ?",
                    (group_id, record_id),
                ).fetchone()
                if row is None or row["content_hash"] != expected_hash:
                    raise StorageError("记录内容已发生变化，请刷新页面后重试")
                shas = {
                    item["sha256"]
                    for item in db.execute(
                        "SELECT sha256 FROM record_media WHERE record_id = ?",
                        (record_id,),
                    )
                }
                db.execute("DELETE FROM records WHERE id = ?", (record_id,))
                if record is not None:
                    if db.execute(
                        "SELECT 1 FROM records WHERE group_id = ? AND content_hash = ?",
                        (group_id, record.content_hash),
                    ).fetchone():
                        raise StorageError(
                            "删除节点后内容与现有群典重复，请改为删除整条记录"
                        )
                    self._insert_record(db, record, {})
                self._audit(
                    db,
                    audit_limit,
                    [
                        (
                            group_id,
                            record_id,
                            expected_hash,
                            deleted_by,
                            now_iso(),
                            "page_node",
                            json.dumps(node_path),
                            removed_nodes,
                            int(record is None),
                        )
                    ],
                )
                return self._orphan_media(db, shas)

        return await self._run(run)

    # 记录读取 ---------------------------------------------------------

    async def get(self, group_id: str, record_id: str) -> QuoteRecord | None:
        def run() -> QuoteRecord | None:
            with self._read() as db:
                row = db.execute(
                    "SELECT payload FROM records WHERE group_id = ? AND id = ?",
                    (group_id, record_id),
                ).fetchone()
            return self._row_record(row) if row else None

        return await self._run(run)

    async def load(self, record_ids: list[str]) -> list[QuoteRecord]:
        """按给定顺序加载记录。"""

        def run() -> list[QuoteRecord]:
            result: dict[str, QuoteRecord] = {}
            with self._read() as db:
                for start in range(0, len(record_ids), 500):
                    chunk = record_ids[start : start + 500]
                    marks = ",".join("?" * len(chunk))
                    for row in db.execute(
                        f"SELECT id, payload FROM records WHERE id IN ({marks})", chunk
                    ):
                        result[row["id"]] = self._row_record(row)
            return [result[item] for item in record_ids if item in result]

        return await self._run(run)

    async def candidates(
        self,
        group_id: str,
        *,
        user_id: str | None = None,
        names: Iterable[str] = (),
        include_broken: bool = False,
    ) -> tuple[list[str], int]:
        """候选记录 ID（按收录时间）与媒体缺失的记录数；默认只返回健康记录。

        指定用户时，按存档 QQ 或可被纠正到该用户的昵称取超集，调用方再做身份纠正与精确过滤。
        """
        names = sorted(set(names))

        def run() -> tuple[list[str], int]:
            where = "r.group_id = ?"
            params: list[Any] = [group_id]
            if user_id is not None:
                marks = ",".join("?" * len(names))
                where += (
                    " AND (EXISTS (SELECT 1 FROM record_participants p WHERE p.record_id = r.id"
                    " AND p.user_id = ?)"
                    + (
                        f" OR EXISTS (SELECT 1 FROM record_names n WHERE n.record_id = r.id"
                        f" AND n.name_norm IN ({marks}))"
                        if names
                        else ""
                    )
                    + ")"
                )
                params.extend([user_id, *names])
            with self._read() as db:
                rows = db.execute(
                    f"SELECT r.id, {_HEALTHY} AS healthy FROM records r WHERE {where}"
                    " ORDER BY r.recorded_at, r.id",
                    params,
                ).fetchall()
            healthy = [row["id"] for row in rows if row["healthy"]]
            broken = len(rows) - len(healthy)
            return ([row["id"] for row in rows] if include_broken else healthy), broken

        return await self._run(run)

    async def group_records(self, group_id: str) -> list[QuoteRecord]:
        """整群记录，仅用于低频管理操作（别名影响预览、导出）。"""

        def run() -> list[QuoteRecord]:
            with self._read() as db:
                rows = db.execute(
                    "SELECT payload FROM records WHERE group_id = ? ORDER BY recorded_at, id",
                    (group_id,),
                ).fetchall()
            return [self._row_record(row) for row in rows]

        return await self._run(run)

    async def search(self, group_id: str, needle: str) -> list[QuoteRecord]:
        """按规范化正文子串检索。"""

        def run() -> list[QuoteRecord]:
            with self._read() as db:
                rows = db.execute(
                    "SELECT payload FROM records WHERE group_id = ? AND instr(search_text, ?) > 0"
                    " ORDER BY recorded_at, id",
                    (group_id, needle),
                ).fetchall()
            return [self._row_record(row) for row in rows]

        return await self._run(run)

    async def page(
        self,
        group_id: str,
        *,
        record_type: str = "",
        has_image: bool | None = None,
        healthy: bool | None = None,
        needle: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[tuple[QuoteRecord, bool]], int]:
        """管理页分页：返回（记录, 是否健康）列表与总数，按收录时间倒序。"""

        def run() -> tuple[list[tuple[QuoteRecord, bool]], int]:
            where = ["r.group_id = ?"]
            params: list[Any] = [group_id]
            if record_type in {"message", "forward"}:
                where.append("r.type = ?")
                params.append(record_type)
            if has_image is not None:
                where.append("r.has_media = ?")
                params.append(int(has_image))
            if healthy is not None:
                where.append(_HEALTHY if healthy else f"NOT {_HEALTHY}")
            if needle:
                where.append(
                    "(instr(lower(r.id), ?) > 0 OR instr(r.search_text, ?) > 0"
                    " OR EXISTS (SELECT 1 FROM record_participants p WHERE p.record_id = r.id"
                    " AND instr(p.user_id, ?) > 0)"
                    " OR EXISTS (SELECT 1 FROM record_names n WHERE n.record_id = r.id"
                    " AND instr(n.name_norm, ?) > 0)"
                    " OR instr(lower(json_extract(r.payload, '$.author.nickname')), ?) > 0)"
                )
                params.extend([needle] * 5)
            clause = " AND ".join(where)
            with self._read() as db:
                (total,) = db.execute(
                    f"SELECT COUNT(*) FROM records r WHERE {clause}", params
                ).fetchone()
                rows = db.execute(
                    f"SELECT r.payload, {_HEALTHY} AS healthy FROM records r WHERE {clause}"
                    " ORDER BY r.recorded_at DESC, r.id DESC LIMIT ? OFFSET ?",
                    (*params, page_size, (page - 1) * page_size),
                ).fetchall()
            return [
                (self._row_record(row), bool(row["healthy"])) for row in rows
            ], total

        return await self._run(run)

    async def is_healthy(self, record_id: str) -> bool:
        def run() -> bool:
            with self._read() as db:
                row = db.execute(
                    f"SELECT {_HEALTHY} AS healthy FROM records r WHERE r.id = ?",
                    (record_id,),
                ).fetchone()
            return bool(row and row["healthy"])

        return await self._run(run)

    # 统计 -------------------------------------------------------------

    _SUMMARY = f"""
        SELECT r.group_id AS group_id,
               COUNT(*) AS total,
               SUM(r.type = 'message') AS messages,
               SUM(r.type = 'forward') AS forwards,
               SUM(r.has_media) AS with_images,
               SUM(NOT {_HEALTHY}) AS broken,
               MIN(r.recorded_at) AS earliest,
               MAX(r.recorded_at) AS latest
        FROM records r"""

    @staticmethod
    def _image_bytes(db: sqlite3.Connection, group_id: str) -> int:
        (value,) = db.execute(
            "SELECT COALESCE(SUM(size), 0) FROM media WHERE sha256 IN ("
            "SELECT rm.sha256 FROM record_media rm JOIN records r ON r.id = rm.record_id"
            " WHERE r.group_id = ?)",
            (group_id,),
        ).fetchone()
        return int(value)

    @staticmethod
    def _summary_row(row: sqlite3.Row | None, group_id: str) -> dict[str, Any]:
        if row is None or not row["total"]:
            return {
                "group_id": group_id, "total": 0, "messages": 0, "forwards": 0,
                "with_images": 0, "broken": 0, "earliest": None, "latest": None,
            }  # fmt: skip
        return {
            "group_id": row["group_id"],
            "total": int(row["total"]),
            "messages": int(row["messages"] or 0),
            "forwards": int(row["forwards"] or 0),
            "with_images": int(row["with_images"] or 0),
            "broken": int(row["broken"] or 0),
            "earliest": row["earliest"],
            "latest": row["latest"],
        }

    async def info(self, group_id: str) -> dict[str, Any]:
        """当前群统计，不返回正文。"""

        def run() -> dict[str, Any]:
            with self._read() as db:
                row = db.execute(
                    self._SUMMARY + " WHERE r.group_id = ? GROUP BY r.group_id",
                    (group_id,),
                ).fetchone()
                result = self._summary_row(row, group_id)
                result["image_bytes"] = self._image_bytes(db, group_id)
            return result

        return await self._run(run)

    async def summaries(self) -> list[dict[str, Any]]:
        """全部群的统计，一次查询完成。"""

        def run() -> list[dict[str, Any]]:
            with self._read() as db:
                rows = db.execute(
                    self._SUMMARY + " GROUP BY r.group_id ORDER BY r.group_id"
                ).fetchall()
                result = [self._summary_row(row, row["group_id"]) for row in rows]
                for item in result:
                    item["image_bytes"] = self._image_bytes(db, item["group_id"])
            return result

        return await self._run(run)

    async def count(self, group_id: str) -> int:
        def run() -> int:
            with self._read() as db:
                (value,) = db.execute(
                    "SELECT COUNT(*) FROM records WHERE group_id = ?", (group_id,)
                ).fetchone()
            return int(value)

        return await self._run(run)

    async def existing_keys(self, group_id: str) -> tuple[set[str], set[str], int]:
        """导入预检用：当前群已有的内容哈希、记录 ID 与条数。"""

        def run() -> tuple[set[str], set[str], int]:
            with self._read() as db:
                rows = db.execute(
                    "SELECT id, content_hash FROM records WHERE group_id = ?",
                    (group_id,),
                ).fetchall()
            return (
                {row["content_hash"] for row in rows},
                {row["id"] for row in rows},
                len(rows),
            )

        return await self._run(run)

    async def all_record_ids(self) -> set[str]:
        def run() -> set[str]:
            with self._read() as db:
                return {row["id"] for row in db.execute("SELECT id FROM records")}

        return await self._run(run)

    async def export_rows(self) -> list[tuple[str, str]]:
        """导出用：全部记录的 (group_id, payload JSON)。"""

        def run() -> list[tuple[str, str]]:
            with self._read() as db:
                return [
                    (row["group_id"], row["payload"])
                    for row in db.execute(
                        "SELECT group_id, payload FROM records ORDER BY group_id, recorded_at, id"
                    )
                ]

        return await self._run(run)

    # 媒体 -------------------------------------------------------------

    async def media_usage(self) -> int:
        """群典媒体与头像缓存的合计字节数。"""

        def run() -> int:
            with self._read() as db:
                (media,) = db.execute(
                    "SELECT COALESCE(SUM(size), 0) FROM media"
                ).fetchone()
                (avatars,) = db.execute(
                    "SELECT COALESCE(SUM(size), 0) FROM avatars"
                ).fetchone()
            return int(media) + int(avatars)

        return await self._run(run)

    async def media_rows(
        self, hashes: Iterable[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """媒体登记信息；hashes 为 None 时返回全部。"""
        wanted = None if hashes is None else sorted(set(hashes))

        def run() -> dict[str, dict[str, Any]]:
            with self._read() as db:
                if wanted is None:
                    rows = db.execute("SELECT * FROM media").fetchall()
                else:
                    rows = []
                    for start in range(0, len(wanted), 500):
                        chunk = wanted[start : start + 500]
                        rows.extend(
                            db.execute(
                                f"SELECT * FROM media WHERE sha256 IN ({','.join('?' * len(chunk))})",
                                chunk,
                            )
                        )
            return {row["sha256"]: dict(row) for row in rows}

        return await self._run(run)

    async def set_media_state(
        self, states: dict[str, bool], *, verified: bool = False
    ) -> None:
        """批量更新媒体缺失标记；verified 时同时刷新校验时间。"""

        def run() -> None:
            now = now_iso()
            with self._tx() as db:
                for sha256, missing in states.items():
                    if verified:
                        db.execute(
                            "UPDATE media SET missing = ?, verified_at = ? WHERE sha256 = ?",
                            (int(missing), now, sha256),
                        )
                    else:
                        db.execute(
                            "UPDATE media SET missing = ? WHERE sha256 = ?",
                            (int(missing), sha256),
                        )

        if states:
            await self._run(run)

    async def referenced_user_ids(self) -> set[str]:
        """全部记录中出现过的 QQ（含回复作者），用于头像清理。"""

        def run() -> set[str]:
            with self._read() as db:
                return {
                    row["user_id"]
                    for row in db.execute(
                        "SELECT DISTINCT user_id FROM record_participants"
                    )
                }

        return await self._run(run)

    # 头像索引 ---------------------------------------------------------

    async def avatar_rows(self) -> dict[str, dict[str, Any]]:
        def run() -> dict[str, dict[str, Any]]:
            with self._read() as db:
                return {
                    row["user_id"]: dict(row)
                    for row in db.execute("SELECT * FROM avatars")
                }

        return await self._run(run)

    async def avatar_upsert(self, rows: dict[str, tuple[float, float, int]]) -> None:
        """写入头像索引：user_id → (fetched_at, last_used, size)。"""

        def run() -> None:
            with self._tx() as db:
                db.executemany(
                    "INSERT INTO avatars VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET"
                    " fetched_at = excluded.fetched_at, last_used = excluded.last_used,"
                    " size = excluded.size",
                    [(user_id, *values) for user_id, values in rows.items()],
                )

        if rows:
            await self._run(run)

    async def avatar_delete(self, user_ids: Iterable[str]) -> None:
        ids = list(user_ids)

        def run() -> None:
            with self._tx() as db:
                db.executemany(
                    "DELETE FROM avatars WHERE user_id = ?", [(item,) for item in ids]
                )

        if ids:
            await self._run(run)

    # 审计 -------------------------------------------------------------

    async def audit_entries(self, limit: int = 200) -> list[dict[str, Any]]:
        def run() -> list[dict[str, Any]]:
            with self._read() as db:
                rows = db.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
            result = []
            for row in rows:
                item = {
                    key: row[key]
                    for key in (
                        "group_id",
                        "record_id",
                        "content_hash",
                        "deleted_by",
                        "deleted_at",
                        "source",
                    )
                }
                if row["node_path"] is not None:
                    item["node_path"] = json.loads(row["node_path"])
                    item["removed_nodes"] = row["removed_nodes"]
                    item["record_deleted"] = bool(row["record_deleted"])
                result.append(item)
            return result

        return await self._run(run)

    async def import_audit(self, entries: list[dict[str, Any]]) -> None:
        """旧版 audit.json 迁移。"""

        def run() -> None:
            with self._tx() as db:
                db.executemany(
                    "INSERT INTO audit_log (group_id, record_id, content_hash, deleted_by,"
                    " deleted_at, source, node_path, removed_nodes, record_deleted)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            str(item.get("group_id") or ""),
                            str(item.get("record_id") or ""),
                            str(item.get("content_hash") or ""),
                            str(item.get("deleted_by") or ""),
                            str(item.get("deleted_at") or ""),
                            str(item.get("source") or ""),
                            json.dumps(item["node_path"])
                            if isinstance(item.get("node_path"), list)
                            else None,
                            item.get("removed_nodes"),
                            None
                            if item.get("record_deleted") is None
                            else int(bool(item["record_deleted"])),
                        )
                        for item in entries
                        if isinstance(item, dict)
                    ],
                )

        if entries:
            await self._run(run)

    # 群级设置 ---------------------------------------------------------

    async def group_settings(
        self,
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, list[str]]]]:
        """读取群级覆盖与作者别名。"""

        def run() -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, list[str]]]]:
            with self._read() as db:
                overrides = {
                    row["group_id"]: json.loads(row["value"])
                    for row in db.execute("SELECT group_id, value FROM group_overrides")
                }
                aliases: dict[str, dict[str, list[str]]] = {}
                for row in db.execute(
                    "SELECT group_id, user_id, alias FROM author_aliases ORDER BY group_id, position"
                ):
                    aliases.setdefault(row["group_id"], {}).setdefault(
                        row["user_id"], []
                    ).append(row["alias"])
            return overrides, aliases

        return await self._run(run)

    async def save_group_settings(
        self,
        overrides: dict[str, dict[str, Any]],
        aliases: dict[str, dict[str, list[str]]],
    ) -> None:
        """整体替换群级覆盖与作者别名（调用方已校验）。"""

        def run() -> None:
            now = now_iso()
            with self._tx() as db:
                db.execute("DELETE FROM group_overrides")
                db.executemany(
                    "INSERT INTO group_overrides VALUES (?, ?, ?)",
                    [
                        (group_id, json.dumps(value, ensure_ascii=False), now)
                        for group_id, value in overrides.items()
                        if value
                    ],
                )
                db.execute("DELETE FROM author_aliases")
                rows = []
                for group_id, targets in aliases.items():
                    position = 0
                    for user_id, values in targets.items():
                        for alias in values:
                            rows.append(
                                (
                                    group_id,
                                    user_id,
                                    alias,
                                    normalize_search(alias),
                                    position,
                                )
                            )
                            position += 1
                db.executemany(
                    "INSERT INTO author_aliases VALUES (?, ?, ?, ?, ?)", rows
                )

        await self._run(run)


def _split_sql(script: str) -> list[str]:
    """按分号拆分迁移脚本；脚本中不使用触发器等含内部分号的语句。"""
    lines = [line for line in script.splitlines() if not line.strip().startswith("--")]
    return [
        statement.strip()
        for statement in "\n".join(lines).split(";")
        if statement.strip()
    ]
