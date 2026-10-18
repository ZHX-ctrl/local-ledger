"""Synthetic tests for the private attachment and backup boundary."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import struct
import subprocess
import tempfile
import unittest
import uuid
import zlib
from threading import RLock
import zipfile

from ledger_app.attachments import MAX_PHOTO_BYTES, read_photo, save_photo
from ledger_app.backup import BackupError, BackupManager


def tiny_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\x10\x80\x80"))
        + chunk(b"IEND", b"")
    )


def make_database(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    schema = Path(__file__).parents[1] / "ledger_app" / "schema.sql"
    connection.executescript(schema.read_text(encoding="utf-8"))
    connection.execute(
        "INSERT INTO categories(id,name,name_key,active) VALUES (1,'虚构类别','虚构类别',1)"
    )
    connection.execute("INSERT INTO meta(key,value) VALUES ('state','before')")
    connection.commit()
    return connection


class AttachmentTests(unittest.TestCase):
    def test_jpeg_trailer_is_removed_without_changing_image_stream(self) -> None:
        # The APP segment contains EOI bytes that are metadata, not the end.
        jpeg = b"\xff\xd8\xff\xe0\x00\x06ab\xff\xd9\xff\xda\x00\x02\x12\xff\x00\x34\xff\xd9"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for trailer in (b"", b"synthetic-app-trailer-123", b"<script>ignored</script>"):
                photo_id = save_photo(root, jpeg + trailer)
                self.assertEqual(read_photo(root, photo_id), (jpeg, "image/jpeg"))
            for bad in (jpeg[:-2], b"\xff\xd8\xff\xe0\xff\xff\xff\xd9", jpeg + b"x" * MAX_PHOTO_BYTES):
                with self.assertRaises(ValueError):
                    save_photo(root, bad)

    def test_save_read_and_reject_unsafe_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photo_id = save_photo(root, tiny_png())
            self.assertRegex(photo_id, r"^[0-9a-f-]{36}\.png$")
            body, mime = read_photo(root, photo_id)
            self.assertEqual(body, tiny_png())
            self.assertEqual(mime, "image/png")

            for bad in (b"<svg></svg>", b"<html>unsafe</html>", b"GIF89a"):
                with self.assertRaises(ValueError):
                    save_photo(root, bad)
            with self.assertRaises(ValueError):
                read_photo(root, "../outside.png")
            with self.assertRaises(ValueError):
                read_photo(root, "not-a-uuid.png")

            # A syntactically correct PNG with a corrupted CRC must not be
            # accepted merely because the magic bytes are present.
            corrupt = bytearray(tiny_png())
            corrupt[29] ^= 0x01
            with self.assertRaises(ValueError):
                save_photo(root, bytes(corrupt))

            with self.assertRaises(ValueError):
                save_photo(root, b"\x89PNG\r\n\x1a\n" + b"x" * (MAX_PHOTO_BYTES + 1))

    def test_read_rejects_symlink_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            photo_id = save_photo(root, tiny_png())
            target = root / "photos" / photo_id
            outside = root / "outside.png"
            outside.write_bytes(tiny_png())
            target.unlink()
            try:
                target.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("this Windows runner does not permit symlink creation")
            with self.assertRaises(ValueError):
                read_photo(root, photo_id)


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = make_database(self.root / "ledger.sqlite3")
        self.manager = BackupManager(self.root, self.db, RLock())

    def tearDown(self) -> None:
        self.db.close()
        self.temporary.cleanup()

    @unittest.skipUnless(os.name == "nt", "Windows ACL regression")
    def test_restore_keeps_parent_access_for_desktop_user(self) -> None:
        # Only synthetic data: give an additional principal read access so
        # the test distinguishes inherited ledger ACLs from owner-only
        # tempfile ACLs, without needing a second account or credentials.
        subprocess.run(
            ["icacls", str(self.root), "/grant", "*S-1-5-32-545:(OI)(CI)(RX)"],
            check=True, capture_output=True,
        )
        photo_id = save_photo(self.root, tiny_png())

        def assert_reader_access(path: Path) -> None:
            script = (
                "$ErrorActionPreference = 'Stop'; "
                "$a = Get-Acl -LiteralPath $env:LEDGER_TEST_ACL_PATH; "
                "@($a.Access | Where-Object { "
                "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value "
                "-eq 'S-1-5-32-545' }).Count"
            )
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                env={
                    **{k: v for k, v in os.environ.items() if k.casefold() != "psmodulepath"},
                    "LEDGER_TEST_ACL_PATH": str(path),
                },
                check=True, capture_output=True, text=True,
            )
            self.assertGreater(int(result.stdout.strip()), 0, "ledger reader lost access: " + result.stderr)

        assert_reader_access(self.root / "photos")
        archive = self.manager.create()
        self.manager.restore(archive)
        assert_reader_access(self.root / "photos")
        assert_reader_access(self.root / "photos" / photo_id)
        self.assertEqual(read_photo(self.root, photo_id), (tiny_png(), "image/png"))

    def test_archive_manifest_and_restore_preserve_photos(self) -> None:
        photo_id = save_photo(self.root, tiny_png())
        self.db.execute("UPDATE meta SET value='backup-state' WHERE key='state'")
        self.db.commit()
        archive_name = self.manager.create()
        archive_path = self.root / "backups" / archive_name
        self.assertTrue(archive_path.is_file())
        with zipfile.ZipFile(archive_path) as archive:
            names = set(archive.namelist())
            self.assertIn("manifest.json", names)
            self.assertIn("db/ledger.sqlite3", names)
            self.assertIn(f"photos/{photo_id}", names)
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(manifest["database"]["sha256"], hashlib.sha256(archive.read("db/ledger.sqlite3")).hexdigest())

        self.db.execute("UPDATE meta SET value='changed-after-backup' WHERE key='state'")
        self.db.commit()
        (self.root / "photos" / photo_id).unlink()
        self.manager.restore(archive_name)
        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0], "backup-state")
        self.assertEqual(read_photo(self.root, photo_id), (tiny_png(), "image/png"))
        kinds = {item["kind"] for item in self.manager.list()}
        self.assertIn("manual", kinds)
        self.assertIn("pre-restore", kinds)

    def test_tampered_manifest_is_rejected_without_switching_state(self) -> None:
        archive_name = self.manager.create()
        source = self.root / "backups" / archive_name
        tampered_name = "tampered.zip"
        tampered = self.root / "backups" / tampered_name
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(tampered, "w") as replacement:
            for info in original.infolist():
                body = original.read(info)
                if info.filename == "manifest.json":
                    manifest = json.loads(body)
                    manifest["database"]["sha256"] = "0" * 64
                    body = json.dumps(manifest, sort_keys=True).encode()
                replacement.writestr(info, body)
        before = self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0]
        with self.assertRaises(BackupError):
            self.manager.restore(tampered_name)
        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0], before)
        self.assertFalse(any(path.name.startswith(".photos-before-restore-") for path in self.root.iterdir()))

    def test_traversal_symlink_duplicate_and_corrupt_archives_are_rejected(self) -> None:
        photo_id = save_photo(self.root, tiny_png())
        archive_name = self.manager.create()
        source = self.root / "backups" / archive_name
        before = self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0]
        before_photo = read_photo(self.root, photo_id)

        def copy_entries(destination: Path, extra: tuple[str, bytes, int] | None = None) -> None:
            with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as replacement:
                for info in original.infolist():
                    replacement.writestr(info, original.read(info))
                if extra is not None:
                    name, body, external_attr = extra
                    info = zipfile.ZipInfo(name)
                    info.external_attr = external_attr
                    replacement.writestr(info, body)

        traversal = self.root / "backups" / "traversal.zip"
        copy_entries(traversal, ("../escape.txt", b"escape", (stat.S_IFREG | 0o600) << 16))
        with self.assertRaises(BackupError):
            self.manager.restore(traversal.name)
        self.assertFalse((self.root / "escape.txt").exists())

        symlink_member = self.root / "backups" / "zip-symlink.zip"
        symlink_info = ("photos/link.png", b"target", (stat.S_IFLNK | 0o777) << 16)
        copy_entries(symlink_member, symlink_info)
        with self.assertRaises(BackupError):
            self.manager.restore(symlink_member.name)

        duplicate = self.root / "backups" / "duplicate.zip"
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(duplicate, "w") as replacement:
            for info in original.infolist():
                replacement.writestr(info, original.read(info))
            replacement.writestr("manifest.json", original.read("manifest.json"))
        with self.assertRaises(BackupError):
            self.manager.restore(duplicate.name)

        corrupt_db = self.root / "backups" / "corrupt-db.zip"
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(corrupt_db, "w") as replacement:
            for info in original.infolist():
                body = original.read(info)
                if info.filename == "db/ledger.sqlite3":
                    body = body[:-1] + bytes([body[-1] ^ 0xFF])
                replacement.writestr(info, body)
        with self.assertRaises(BackupError):
            self.manager.restore(corrupt_db.name)

        corrupt_zip = self.root / "backups" / "corrupt.zip"
        corrupt_zip.write_bytes(b"this is not a zip archive")
        with self.assertRaises(BackupError):
            self.manager.restore(corrupt_zip.name)
        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0], before)
        self.assertEqual(read_photo(self.root, photo_id), before_photo)

    def test_restore_rolls_back_live_db_and_photos_after_injected_failure(self) -> None:
        photo_id = save_photo(self.root, tiny_png())
        self.db.execute("UPDATE meta SET value='archive-state' WHERE key='state'")
        self.db.commit()
        archive_name = self.manager.create()
        self.db.execute("UPDATE meta SET value='current-state' WHERE key='state'")
        self.db.commit()
        before_photo = read_photo(self.root, photo_id)

        def fail_before_photo_switch(_staged: Path) -> Path:
            raise BackupError("injected photo switch failure")

        original_swap = self.manager._swap_photos
        self.manager._swap_photos = fail_before_photo_switch  # type: ignore[method-assign]
        try:
            with self.assertRaises(BackupError):
                self.manager.restore(archive_name)
        finally:
            self.manager._swap_photos = original_swap  # type: ignore[method-assign]

        self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0], "current-state")
        self.assertEqual(read_photo(self.root, photo_id), before_photo)
        self.assertTrue(any(item["kind"] == "pre-restore" for item in self.manager.list()))
        self.assertFalse(any(path.name.startswith(".photos-before-restore-") for path in self.root.iterdir()))

    def test_daily_backup_is_once_per_day_and_retains_thirty(self) -> None:
        first = self.manager.ensure_daily()
        self.assertIsInstance(first, str)
        self.assertIsNone(self.manager.ensure_daily())
        for _ in range(31):
            self.manager.create(kind="daily")
        daily = [item for item in self.manager.list() if item["kind"] == "daily"]
        self.assertEqual(len(daily), 30)
        # Manual and recovery archives are outside automatic retention.
        self.assertEqual(len([item for item in self.manager.list() if item["kind"] == "manual"]), 0)

    def test_incompatible_live_schema_cannot_be_archived(self) -> None:
        self.db.execute("PRAGMA user_version=99")
        self.db.commit()
        with self.assertRaises(BackupError):
            self.manager.create()

    def test_same_version_missing_column_or_constraint_is_rejected_before_restore(self) -> None:
        valid_archive = self.manager.create()
        source = self.root / "backups" / valid_archive

        def archive_with_database(name: str, mutate: callable) -> str:
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "fake.sqlite3"
                fake = make_database(path)
                mutate(fake)
                fake.commit()
                fake.close()
                database_bytes = path.read_bytes()
            destination = self.root / "backups" / name
            with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as replacement:
                manifest = json.loads(original.read("manifest.json"))
                manifest["database"]["size"] = len(database_bytes)
                manifest["database"]["sha256"] = hashlib.sha256(database_bytes).hexdigest()
                for info in original.infolist():
                    body = original.read(info)
                    if info.filename == "manifest.json":
                        body = json.dumps(manifest, sort_keys=True).encode()
                    elif info.filename == "db/ledger.sqlite3":
                        body = database_bytes
                    replacement.writestr(info, body)
            return name

        def remove_column(fake: sqlite3.Connection) -> None:
            fake.execute("DROP TABLE entries")
            fake.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, date TEXT NOT NULL)")

        def remove_constraint(fake: sqlite3.Connection) -> None:
            fake.execute("DROP TABLE entries")
            fake.execute(
                """CREATE TABLE entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    date TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    category_id INTEGER NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    photo_id TEXT,
                    fx_status TEXT NOT NULL,
                    rate TEXT,
                    quote_date TEXT,
                    fx_source TEXT,
                    fx_reason TEXT,
                    cny_cents INTEGER,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            fake.execute("CREATE INDEX entries_date_idx ON entries(date, id)")
            fake.execute("CREATE INDEX entries_active_date_idx ON entries(deleted, date, id)")
            fake.execute("CREATE INDEX entries_category_idx ON entries(category_id, deleted)")

        def add_foreign_key_violation(fake: sqlite3.Connection) -> None:
            fake.execute("PRAGMA foreign_keys=OFF")
            fake.execute(
                """INSERT INTO entries(
                    date,kind,currency,amount_cents,name,category_id,note,photo_id,fx_status,
                    rate,quote_date,fx_source,fx_reason,cny_cents,deleted,created_at,updated_at
                ) VALUES ('2026-09-12','expense','CNY',100,'孤立条目',999,'',NULL,'resolved',
                          NULL,NULL,NULL,NULL,100,0,'2026-09-12T00:00:00+08:00','2026-09-12T00:00:00+08:00')"""
            )

        before = self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0]
        for filename, mutate in (
            ("same-version-missing-column.zip", remove_column),
            ("same-version-missing-check.zip", remove_constraint),
            ("same-version-fk-violation.zip", add_foreign_key_violation),
        ):
            candidate = archive_with_database(filename, mutate)
            with self.assertRaises(BackupError):
                self.manager.restore(candidate)
            self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0], before)

    def test_schema_signature_rejects_extra_trigger_and_changed_check_literal(self) -> None:
        photo_id = save_photo(self.root, tiny_png())
        source_name = self.manager.create()
        source = self.root / "backups" / source_name
        before = self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0]
        before_photo = read_photo(self.root, photo_id)

        def malicious_archive(name: str, mutate: callable) -> str:
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "malicious.sqlite3"
                fake = make_database(path)
                mutate(fake)
                fake.commit()
                fake.close()
                database_bytes = path.read_bytes()
            destination = self.root / "backups" / name
            with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w") as replacement:
                manifest = json.loads(original.read("manifest.json"))
                manifest["database"]["size"] = len(database_bytes)
                manifest["database"]["sha256"] = hashlib.sha256(database_bytes).hexdigest()
                for info in original.infolist():
                    body = original.read(info)
                    if info.filename == "manifest.json":
                        body = json.dumps(manifest, sort_keys=True).encode()
                    elif info.filename == "db/ledger.sqlite3":
                        body = database_bytes
                    replacement.writestr(info, body)
            return name

        def add_extra_trigger(fake: sqlite3.Connection) -> None:
            fake.execute(
                "CREATE TRIGGER sqliteXevil AFTER INSERT ON entries BEGIN SELECT 1; END"
            )

        def change_check_literal(fake: sqlite3.Connection) -> None:
            current = fake.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='entries'"
            ).fetchone()[0]
            changed = current.replace("currency IN ('CNY', 'SGD')", "currency IN ('cny', 'SGD')")
            self.assertNotEqual(changed, current)
            fake.execute("PRAGMA writable_schema=ON")
            fake.execute(
                "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='entries'",
                (changed,),
            )
            fake.execute("PRAGMA writable_schema=OFF")

        for filename, mutate in (
            ("extra-trigger.zip", add_extra_trigger),
            ("changed-check-literal.zip", change_check_literal),
        ):
            candidate = malicious_archive(filename, mutate)
            with self.assertRaises(BackupError):
                self.manager.restore(candidate)
            self.assertEqual(self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0], before)
            self.assertEqual(read_photo(self.root, photo_id), before_photo)


if __name__ == "__main__":
    unittest.main()
