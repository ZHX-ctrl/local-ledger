"""Consistent local SQLite and photo backups.

Backups are intentionally a private, application-owned format.  The restore
entry point accepts a basename that already exists in ``root/backups``; it does
not accept an arbitrary upload or filesystem path.  Archive validation happens
before any live database or photo directory is changed.
"""

from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Iterable

from .attachments import (
    MAX_PHOTO_BYTES,
    _photo_filename,
    _validate_image,
)


ARCHIVE_VERSION = 1
ARCHIVE_DIRNAME = "backups"
DB_ARCHIVE_PATH = "db/ledger.sqlite3"
MANIFEST_PATH = "manifest.json"
PHOTO_ARCHIVE_PREFIX = "photos/"
# These are safety ceilings for an archive supplied by the local restore UI.
# A photo is already limited to 8 MiB; the larger aggregate bound leaves room
# for a useful local history while stopping accidental ZIP bombs.
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
MAX_DB_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_COMPRESSION_RATIO = 200
MAX_AUTO_BACKUPS = 30

LOCAL_TZ = timezone(timedelta(hours=8))
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_KINDS = frozenset({"manual", "daily", "pre-restore"})


class BackupError(ValueError):
    """A backup is invalid, incompatible, or cannot be safely restored."""


@dataclass(frozen=True)
class _ArchiveStage:
    root: Path
    manifest: dict[str, Any]

    @property
    def db(self) -> Path:
        return self.root / "db" / "ledger.sqlite3"

    @property
    def photos(self) -> Path:
        return self.root / "photos"


def _fail(message: str) -> BackupError:
    error = BackupError(message)
    # Only our fixed messages are safe to show; never expose raw OS paths,
    # SQL text, or exception messages originating in user files.
    error.safe_message = message
    return error


def backup_failure_reason(error: Exception) -> str:
    """Describe a failure without including paths or financial contents."""
    messages = []
    seen = set()
    current = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, BackupError) and getattr(current, "safe_message", None):
            messages.append(current.safe_message.rstrip("。"))
        if isinstance(current, OSError):
            code = getattr(current, "winerror", None)
            if code in (32, 33):
                messages.append("备份所需文件正被其他程序占用，请关闭占用程序后重试")
            elif current.errno in (errno.EACCES, errno.EPERM) or code == 5:
                messages.append("没有读取账本或写入备份目录的权限，请检查文件权限及安全软件拦截")
            elif current.errno == errno.ENOSPC or code == 112:
                messages.append("磁盘空间不足，请释放空间后重试")
            elif current.errno == errno.ENOENT:
                messages.append("备份所需的文件或目录不存在")
            elif current.errno is not None:
                messages.append(f"文件操作失败（系统错误码 {int(current.errno)}）")
        if isinstance(current, sqlite3.Error):
            code = getattr(current, "sqlite_errorcode", None)
            if code is not None:
                messages.append(f"数据库检查或快照失败（SQLite 错误码 {int(code)}）")
        current = current.__cause__
    return "；".join(dict.fromkeys(messages)) or "备份检查出现未分类错误，请保留现场以便排查"


def _now() -> datetime:
    return datetime.now(LOCAL_TZ)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise _fail("无法读取备份文件。") from exc
    return digest.hexdigest()


def _safe_private_dir(path: Path, *, create: bool) -> Path:
    path = Path(path)
    if path.is_symlink():
        raise _fail("备份目录不能是符号链接。")
    if not path.exists():
        if not create:
            raise _fail("备份目录不存在。")
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise _fail("备份路径不是目录。")
    return path


def _canonical_schema_sql(sql: Any) -> str | None:
    if sql is None:
        return None
    if not isinstance(sql, str):
        return str(sql)
    # Keep SQL text and string literals byte-for-byte comparable after the
    # harmless outer whitespace SQLite may retain.  Case-folding here would
    # turn a changed CHECK literal such as 'CNY' into the reference 'cny'.
    return sql.strip()


def _schema_signature(connection: sqlite3.Connection) -> tuple[tuple[str, str, str | None, str | None], ...]:
    """Return the structural signature derived from the checked-in schema.

    The reference is executed in memory from ``schema.sql`` on every process
    start.  This keeps the backup validator aligned with the actual migration
    source and avoids maintaining a second hand-written list of columns and
    constraints in this module.
    """

    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT GLOB 'sqlite_*'
        ORDER BY type, name
        """
    ).fetchall()
    return tuple(
        (
            str(row[0]),
            str(row[1]),
            None if row[2] is None else str(row[2]),
            _canonical_schema_sql(row[3]),
        )
        for row in rows
    )


def _reference_schema_signature() -> tuple[tuple[str, str, str | None, str | None], ...]:
    reference: sqlite3.Connection | None = None
    try:
        reference = sqlite3.connect(":memory:")
        schema_path = Path(__file__).with_name("schema.sql")
        try:
            schema = schema_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise _fail("无法读取账本数据库结构定义。") from exc
        try:
            reference.executescript(schema)
        except sqlite3.Error as exc:
            raise _fail("账本数据库结构定义无效。") from exc
        return _schema_signature(reference)
    finally:
        if reference is not None:
            reference.close()


# The signature comparison above is the source of truth.  Keep the final
# runtime check small and independent of table-specific hand-written lists.
def _check_schema(connection: sqlite3.Connection) -> None:
    try:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != 2:
            raise _fail("备份数据库版本不兼容。")
        actual = _schema_signature(connection)
        expected = _reference_schema_signature()
        if actual != expected:
            raise _fail("备份数据库表、列、索引或约束与当前版本不兼容。")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    except BackupError:
        raise
    except sqlite3.Error as exc:
        raise _fail("备份数据库结构或完整性无法读取。") from exc
    if violations:
        raise _fail("备份数据库包含外键违规数据。")


def _validate_sqlite_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise _fail("备份数据库文件无效。")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise _fail("备份数据库文件无法检查。") from exc
    if size <= 0 or size > MAX_DB_BYTES:
        raise _fail("备份数据库大小超出限制。")
    try:
        with path.open("rb") as stream:
            if stream.read(16) != b"SQLite format 3\x00":
                raise _fail("备份文件不是 SQLite 数据库。")
    except OSError as exc:
        raise _fail("备份数据库文件无法读取。") from exc

    connection: sqlite3.Connection | None = None
    try:
        # Read-only mode prevents validation from creating or changing sidecar
        # files in the staged restore directory.
        uri = f"file:{path.absolute().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only = ON")
        _check_schema(connection)
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or str(result[0]).lower() != "ok":
            raise _fail("备份数据库完整性校验失败。")
    except BackupError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise _fail("备份数据库无法打开或校验。") from exc
    finally:
        if connection is not None:
            connection.close()


def _safe_member_name(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name:
        raise _fail("备份压缩包包含非法文件名。")
    # Zip names are always interpreted as POSIX paths.  Backslashes are
    # rejected as well because Windows would treat them as separators.
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise _fail("备份压缩包包含绝对路径。")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise _fail("备份压缩包包含目录穿越路径。")
    normalized = "/".join(path.parts)
    if normalized != name:
        raise _fail("备份压缩包包含未规范化路径。")
    return normalized


def _validate_zip_infos(infos: Iterable[zipfile.ZipInfo], archive_size: int) -> list[zipfile.ZipInfo]:
    if archive_size <= 0 or archive_size > MAX_ARCHIVE_BYTES:
        raise _fail("备份压缩包大小超出限制。")
    infos = list(infos)
    if not infos or len(infos) > MAX_ARCHIVE_MEMBERS:
        raise _fail("备份压缩包成员数量无效。")

    result: list[zipfile.ZipInfo] = []
    seen: set[str] = set()
    total_uncompressed = 0
    for info in infos:
        name = _safe_member_name(info.filename)
        key = name.casefold()
        if key in seen:
            raise _fail("备份压缩包包含重复文件。")
        seen.add(key)
        if info.is_dir() or name.endswith("/"):
            raise _fail("备份压缩包不允许目录成员。")
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode):
            raise _fail("备份压缩包不允许符号链接。")
        if info.flag_bits & 0x1:
            raise _fail("备份压缩包不允许加密成员。")
        if info.file_size < 0 or info.file_size > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise _fail("备份压缩包成员大小超出限制。")
        if name == MANIFEST_PATH and info.file_size > MAX_MANIFEST_BYTES:
            raise _fail("备份清单大小超出限制。")
        if name == DB_ARCHIVE_PATH and info.file_size > MAX_DB_BYTES:
            raise _fail("备份数据库大小超出限制。")
        if name.startswith(PHOTO_ARCHIVE_PREFIX) and info.file_size > MAX_PHOTO_BYTES:
            raise _fail("备份照片大小超出限制。")
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise _fail("备份压缩包解压后大小超出限制。")
        if info.file_size and info.compress_size == 0:
            raise _fail("备份压缩包压缩信息无效。")
        if info.compress_size and info.file_size > 1024 * 1024:
            if info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                raise _fail("备份压缩包压缩比例异常。")
        result.append(info)
    return result


def _manifest_hash(value: Any) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise _fail("备份清单包含无效哈希。")
    return value


def _manifest_size(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _fail("备份清单包含无效文件大小。")
    return value


def _read_json_manifest(data: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail("备份清单不是有效 JSON。") from exc
    if not isinstance(parsed, dict) or parsed.get("version") != ARCHIVE_VERSION:
        raise _fail("备份清单版本不兼容。")
    kind = parsed.get("kind")
    if kind not in _ALLOWED_KINDS:
        raise _fail("备份清单类型无效。")
    created = parsed.get("created")
    if not isinstance(created, str) or not created:
        raise _fail("备份清单缺少创建时间。")

    database = parsed.get("database")
    # ``db`` is accepted as a read-only compatibility spelling for an early
    # development archive; archives produced here always use ``database``.
    if database is None:
        database = parsed.get("db")
    if not isinstance(database, dict):
        raise _fail("备份清单缺少数据库信息。")
    if database.get("path") != DB_ARCHIVE_PATH:
        raise _fail("备份清单数据库路径无效。")
    _manifest_hash(database.get("sha256"))
    _manifest_size(database.get("size"))

    photos = parsed.get("photos")
    if not isinstance(photos, list):
        raise _fail("备份清单照片信息无效。")
    seen: set[str] = set()
    normalized_photos: list[dict[str, Any]] = []
    for item in photos:
        if not isinstance(item, dict):
            raise _fail("备份清单照片信息无效。")
        path = item.get("path")
        if not isinstance(path, str) or not path.startswith(PHOTO_ARCHIVE_PREFIX):
            raise _fail("备份清单照片路径无效。")
        filename = path[len(PHOTO_ARCHIVE_PREFIX) :]
        try:
            _photo_filename(filename)
        except ValueError as exc:
            raise _fail("备份清单照片名称无效。") from exc
        if path != f"{PHOTO_ARCHIVE_PREFIX}{filename}":
            raise _fail("备份清单照片路径未规范化。")
        key = path.casefold()
        if key in seen:
            raise _fail("备份清单包含重复照片。")
        seen.add(key)
        normalized = {
            "path": path,
            "size": _manifest_size(item.get("size")),
            "sha256": _manifest_hash(item.get("sha256")),
        }
        if "mime" in item:
            if not isinstance(item["mime"], str) or item["mime"] not in {
                "image/jpeg",
                "image/png",
                "image/webp",
            }:
                raise _fail("备份清单照片类型无效。")
            normalized["mime"] = item["mime"]
        normalized_photos.append(normalized)
    parsed["database"] = {
        "path": DB_ARCHIVE_PATH,
        "size": _manifest_size(database.get("size")),
        "sha256": _manifest_hash(database.get("sha256")),
    }
    parsed["photos"] = normalized_photos
    return parsed


def _extract_member(zf: zipfile.ZipFile, info: zipfile.ZipInfo, stage_root: Path) -> None:
    relative = PurePosixPath(_safe_member_name(info.filename))
    target = stage_root.joinpath(*relative.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with zf.open(info, "r") as source, target.open("xb") as destination:
            while True:
                block = source.read(1024 * 1024)
                if not block:
                    break
                written += len(block)
                if written > info.file_size or written > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise _fail("备份成员解压后大小异常。")
                destination.write(block)
            destination.flush()
            os.fsync(destination.fileno())
    except BackupError:
        raise
    except (OSError, EOFError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise _fail("备份压缩包成员无法安全解压。") from exc
    if written != info.file_size:
        raise _fail("备份压缩包成员大小校验失败。")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass


def _stage_archive(archive: Path, stage_root: Path) -> _ArchiveStage:
    try:
        archive_size = archive.stat().st_size
    except OSError as exc:
        raise _fail("备份不存在或无法读取。") from exc
    if archive.is_symlink() or not archive.is_file():
        raise _fail("备份路径无效。")

    try:
        with zipfile.ZipFile(archive, "r") as zf:
            infos = _validate_zip_infos(zf.infolist(), archive_size)
            names = {_safe_member_name(info.filename) for info in infos}
            if MANIFEST_PATH not in names or DB_ARCHIVE_PATH not in names:
                raise _fail("备份压缩包缺少清单或数据库。")
            for info in infos:
                _extract_member(zf, info, stage_root)
    except BackupError:
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise _fail("备份压缩包无法打开。") from exc

    manifest_path = stage_root / MANIFEST_PATH
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise _fail("备份清单无法读取。") from exc
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise _fail("备份清单大小超出限制。")
    manifest = _read_json_manifest(manifest_bytes)

    actual_names = {
        _safe_member_name(path.relative_to(stage_root).as_posix())
        for path in stage_root.rglob("*")
        if path.is_file()
    }
    expected_names = {MANIFEST_PATH, DB_ARCHIVE_PATH}
    expected_names.update(item["path"] for item in manifest["photos"])
    if actual_names != expected_names:
        raise _fail("备份压缩包成员与清单不一致。")

    database = manifest["database"]
    db_path = stage_root / DB_ARCHIVE_PATH
    if db_path.stat().st_size != database["size"] or _sha256(db_path) != database["sha256"]:
        raise _fail("备份数据库校验和不匹配。")
    _validate_sqlite_file(db_path)

    photos_root = stage_root / "photos"
    photos_root.mkdir(parents=True, exist_ok=True)
    for item in manifest["photos"]:
        path = stage_root / PurePosixPath(item["path"])
        if path.is_symlink() or not path.is_file():
            raise _fail("备份照片文件无效。")
        if path.stat().st_size != item["size"] or _sha256(path) != item["sha256"]:
            raise _fail("备份照片校验和不匹配。")
        try:
            extension, mime = _validate_image(path.read_bytes())
        except ValueError as exc:
            raise _fail("备份照片格式校验失败。") from exc
        expected_extension = path.suffix.lower()
        if extension != expected_extension and not (extension == ".jpg" and expected_extension == ".jpeg"):
            raise _fail("备份照片扩展名与内容不匹配。")
        if item.get("mime") is not None and item["mime"] != mime:
            raise _fail("备份照片类型与清单不匹配。")

    return _ArchiveStage(stage_root, manifest)


class BackupManager:
    """Manage private backups while sharing the service's lock and database."""

    def __init__(self, root: Path, db: sqlite3.Connection, lock: RLock):
        self.root = Path(root)
        self.db = db
        self.lock = lock
        self.backup_dir = self.root / ARCHIVE_DIRNAME
        _safe_private_dir(self.root, create=True)
        _safe_private_dir(self.backup_dir, create=True)

    def _archive_path(self, name: str) -> Path:
        if not isinstance(name, str) or not name or name in {".", ".."}:
            raise _fail("备份名称无效。")
        if "\\" in name or "/" in name or "\x00" in name:
            raise _fail("备份名称不能包含路径。")
        if Path(name).name != name or not name.lower().endswith(".zip"):
            raise _fail("备份名称无效。")
        path = self.backup_dir / name
        try:
            path.resolve(strict=False).relative_to(self.backup_dir.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise _fail("备份路径越界。") from exc
        if path.is_symlink() or not path.is_file():
            raise _fail("所选备份不存在。")
        return path

    def _snapshot_live_db(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        target: sqlite3.Connection | None = None
        try:
            target = sqlite3.connect(str(destination))
            self.db.backup(target)
            target.commit()
        except (sqlite3.Error, OSError) as exc:
            raise _fail("无法创建一致的数据库快照。") from exc
        finally:
            if target is not None:
                target.close()
        _validate_sqlite_file(destination)

    def _snapshot_photos(self, destination: Path) -> list[dict[str, Any]]:
        destination.mkdir(parents=True, exist_ok=True)
        source = self.root / "photos"
        if source.is_symlink() or not source.is_dir():
            if not source.exists() and not source.is_symlink():
                return []
            raise _fail("照片目录无效。")
        records: list[dict[str, Any]] = []
        try:
            entries = sorted(source.iterdir(), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise _fail("照片目录无法读取。") from exc
        for entry in entries:
            if entry.is_symlink() or not entry.is_file():
                raise _fail("照片目录包含非法成员。")
            try:
                filename = _photo_filename(entry.name)
            except ValueError as exc:
                raise _fail("照片目录包含非法文件名。") from exc
            try:
                size = entry.stat().st_size
            except OSError as exc:
                raise _fail("照片文件无法检查。") from exc
            if size <= 0 or size > MAX_PHOTO_BYTES:
                raise _fail("照片文件大小无效。")
            try:
                data = entry.read_bytes()
                extension, mime = _validate_image(data)
            except (OSError, ValueError) as exc:
                raise _fail("照片文件格式校验失败。") from exc
            expected_extension = Path(filename).suffix.lower()
            if extension != expected_extension and not (extension == ".jpg" and expected_extension == ".jpeg"):
                raise _fail("照片扩展名与内容不匹配。")
            target = destination / filename
            try:
                with target.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(target, 0o600)
            except OSError as exc:
                raise _fail("无法暂存照片。") from exc
            records.append(
                {
                    "path": f"{PHOTO_ARCHIVE_PREFIX}{filename}",
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "mime": mime,
                }
            )
        return records

    def _new_archive_name(self, kind: str) -> str:
        stamp = _now().strftime("%Y%m%dT%H%M%S%f%z")
        return f"backup-{stamp}-{kind}-{uuid.uuid4().hex[:12]}.zip"

    def _create_unlocked(self, kind: str) -> str:
        if kind not in _ALLOWED_KINDS:
            raise _fail("备份类型无效。")
        _check_schema(self.db)
        _safe_private_dir(self.backup_dir, create=True)
        archive_name = self._new_archive_name(kind)
        archive_path = self.backup_dir / archive_name
        temporary_archive = self.backup_dir / f".{archive_name}.{uuid.uuid4().hex}.tmp"
        try:
            with tempfile.TemporaryDirectory(prefix=".backup-stage-", dir=str(self.backup_dir)) as stage_name:
                stage = Path(stage_name)
                db_stage = stage / "db" / "ledger.sqlite3"
                self._snapshot_live_db(db_stage)
                photos_stage = stage / "photos"
                photo_records = self._snapshot_photos(photos_stage)
                manifest = {
                    "version": ARCHIVE_VERSION,
                    "created": _now().isoformat(timespec="seconds"),
                    "kind": kind,
                    "database": {
                        "path": DB_ARCHIVE_PATH,
                        "size": db_stage.stat().st_size,
                        "sha256": _sha256(db_stage),
                    },
                    "photos": photo_records,
                }
                manifest_bytes = json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                    raise _fail("备份清单大小超出限制。")

                with zipfile.ZipFile(
                    temporary_archive,
                    mode="w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                    allowZip64=False,
                ) as archive:
                    manifest_info = zipfile.ZipInfo(MANIFEST_PATH)
                    manifest_info.external_attr = (stat.S_IFREG | 0o600) << 16
                    archive.writestr(manifest_info, manifest_bytes)
                    archive.write(db_stage, DB_ARCHIVE_PATH)
                    for item in photo_records:
                        archive.write(stage / PurePosixPath(item["path"]), item["path"])
            try:
                archive_size = temporary_archive.stat().st_size
            except OSError as exc:
                raise _fail("备份压缩包无法检查。") from exc
            if archive_size <= 0 or archive_size > MAX_ARCHIVE_BYTES:
                raise _fail("备份压缩包大小超出限制。")
            with temporary_archive.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary_archive, archive_path)
            self._prune_daily_unlocked()
            return archive_name
        except BackupError:
            try:
                temporary_archive.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except (OSError, sqlite3.Error, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            try:
                temporary_archive.unlink(missing_ok=True)
            except OSError:
                pass
            raise _fail("创建备份失败。") from exc

    def _manifest_header(self, path: Path) -> tuple[str, str] | None:
        try:
            with zipfile.ZipFile(path, "r") as archive:
                info = archive.getinfo(MANIFEST_PATH)
                if info.file_size > MAX_MANIFEST_BYTES:
                    return None
                manifest = _read_json_manifest(archive.read(info))
                return str(manifest["created"]), str(manifest["kind"])
        except (OSError, KeyError, zipfile.BadZipFile, BackupError, RuntimeError):
            return None

    def _prune_daily_unlocked(self) -> None:
        daily: list[tuple[str, float, Path]] = []
        try:
            paths = list(self.backup_dir.glob("*.zip"))
        except OSError:
            return
        for path in paths:
            if path.is_symlink() or not path.is_file():
                continue
            header = self._manifest_header(path)
            if header is None or header[1] != "daily":
                continue
            try:
                created = datetime.fromisoformat(header[0]).timestamp()
            except (TypeError, ValueError, OverflowError):
                try:
                    created = path.stat().st_mtime
                except OSError:
                    continue
            daily.append((header[0], created, path))
        daily.sort(key=lambda item: (item[1], item[0], item[2].name), reverse=True)
        for _created, _timestamp, path in daily[MAX_AUTO_BACKUPS:]:
            try:
                path.unlink()
            except OSError:
                # A concurrent reader should not make the successful backup
                # fail; the next daily run can retry retention.
                continue

    def ensure_daily(self) -> str | None:
        """Create one automatic backup for today's local UTC+8 date.

        ``None`` means today's automatic backup already exists.  A caller that
        only needs the pre-write side effect can ignore the return value.
        """

        with self.lock:
            today = _now().date()
            for item in self.list():
                if item["kind"] != "daily":
                    continue
                try:
                    created = datetime.fromisoformat(item["created"]).astimezone(LOCAL_TZ)
                except (TypeError, ValueError, OverflowError):
                    continue
                if created.date() == today:
                    return None
            return self._create_unlocked("daily")

    def create(self, kind: str = "manual") -> str:
        with self.lock:
            return self._create_unlocked(kind)

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            _safe_private_dir(self.backup_dir, create=True)
            records: list[dict[str, Any]] = []
            try:
                paths = sorted(self.backup_dir.glob("*.zip"), key=lambda path: path.name)
            except OSError as exc:
                raise _fail("无法列出备份。") from exc
            for path in paths:
                if path.is_symlink() or not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                header = self._manifest_header(path)
                if header is None:
                    created, kind = "", "unknown"
                else:
                    created, kind = header
                records.append({"name": path.name, "created": created, "kind": kind, "size": size})
            records.sort(key=lambda item: (item["created"] or "", item["name"]), reverse=True)
            return records

    def _restore_db_from_file(self, source_path: Path) -> None:
        source: sqlite3.Connection | None = None
        try:
            source = sqlite3.connect(f"file:{source_path.absolute().as_posix()}?mode=ro", uri=True)
            source.execute("PRAGMA query_only = ON")
            self.db.rollback()
            source.backup(self.db)
            self.db.commit()
        except (sqlite3.Error, OSError) as exc:
            raise _fail("切换账本数据库失败。") from exc
        finally:
            if source is not None:
                source.close()
        _check_schema(self.db)

    def _swap_photos(self, staged: Path) -> Path | None:
        target = self.root / "photos"
        if target.exists() and target.is_symlink():
            raise _fail("现有照片目录不能是符号链接。")
        if target.exists() and not target.is_dir():
            raise _fail("现有照片路径不是目录。")
        staged = Path(staged)
        if staged.is_symlink() or not staged.is_dir():
            raise _fail("暂存照片目录无效。")
        rollback = self.root / f".photos-before-restore-{uuid.uuid4().hex}"
        ready = None
        moved_old = False
        try:
            if os.name == "nt":
                # Windows keeps a moved directory's ACL. Restore staging is
                # deliberately owner-only (tempfile, mode 0700); moving it
                # straight into the ledger can exclude the desktop user when
                # the restore ran under a sandbox account. Create fresh files
                # beside the live directory so they inherit the ledger ACL.
                ready = self.root / f".photos-ready-{uuid.uuid4().hex}"
                ready.mkdir()
                for photo in staged.iterdir():
                    if photo.is_symlink() or not photo.is_file():
                        raise _fail("暂存照片目录包含非法成员。")
                    shutil.copyfile(photo, ready / photo.name)
                staged = ready
            if target.exists():
                os.replace(target, rollback)
                moved_old = True
            os.replace(staged, target)
            return rollback if moved_old else None
        except OSError as exc:
            if moved_old and rollback.exists() and not target.exists():
                try:
                    os.replace(rollback, target)
                except OSError:
                    pass
            raise _fail("切换照片目录失败。") from exc
        finally:
            if ready is not None and ready.exists():
                shutil.rmtree(ready, ignore_errors=True)

    def _rollback_photos(self, rollback: Path | None) -> None:
        target = self.root / "photos"
        try:
            if target.exists() or target.is_symlink():
                if target.is_symlink() or target.is_file():
                    target.unlink()
                else:
                    shutil.rmtree(target)
            if rollback is not None and rollback.exists():
                os.replace(rollback, target)
        except OSError as exc:
            raise _fail("照片回滚失败。") from exc

    def restore(self, name: str) -> None:
        """Validate and restore one existing archive.

        A ``pre-restore`` archive and a temporary SQLite snapshot are created
        before switching the live connection.  If either database or photo
        replacement fails, both are restored from those snapshots and the
        recovery archive remains in ``backups``.
        """

        with self.lock:
            archive = self._archive_path(name)
            stage_path = Path(tempfile.mkdtemp(prefix=".restore-stage-", dir=str(self.root)))
            db_before = stage_path / "current.sqlite3"
            rollback_photos: Path | None = None
            database_attempted = False
            try:
                staged = _stage_archive(archive, stage_path / "target")
                # This is deliberately done only after target validation, so a
                # corrupt archive cannot create a stream of recovery backups.
                self._create_unlocked("pre-restore")
                self._snapshot_live_db(db_before)
                try:
                    database_attempted = True
                    self._restore_db_from_file(staged.db)
                    rollback_photos = self._swap_photos(staged.photos)
                except Exception as original:
                    rollback_errors: list[BaseException] = []
                    if rollback_photos is not None:
                        try:
                            self._rollback_photos(rollback_photos)
                        except Exception as rollback_error:  # pragma: no cover - defensive path
                            rollback_errors.append(rollback_error)
                    if database_attempted:
                        try:
                            self._restore_db_from_file(db_before)
                        except Exception as rollback_error:  # pragma: no cover - defensive path
                            rollback_errors.append(rollback_error)
                    if rollback_errors:
                        raise _fail("恢复失败且自动回滚也失败，请使用恢复前备份。") from original
                    raise
                if rollback_photos is not None and rollback_photos.exists():
                    try:
                        shutil.rmtree(rollback_photos)
                    except OSError:
                        # The restored state is already live and the
                        # pre-restore archive is the durable recovery copy.
                        pass
            finally:
                shutil.rmtree(stage_path, ignore_errors=True)


__all__ = [
    "ARCHIVE_VERSION",
    "BackupError",
    "BackupManager",
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCHIVE_UNCOMPRESSED_BYTES",
]
