"""Ordered entry attachments, using only generated photos and temporary ledgers."""

from __future__ import annotations

import base64
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zlib

from ledger_app.attachments import read_photo
from ledger_app.service import Ledger, LedgerError


def tiny_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\x10\x80\x80"))
        + chunk(b"IEND", b"")
    )


class EntryPhotoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fx_calls: list[str] = []
        self.ledger = Ledger(self.root, fx_resolver=self.resolve_rate)
        self.request_number = 0
        self.category = self.ledger.state()["categories"][0]["id"]
        for currency in ("CNY", "SGD"):
            self.command("snapshot.save", {"currency": currency, "balance": "100.00", "cutoff": "2026-08-31"})

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def resolve_rate(self, day: str) -> dict[str, str]:
        self.fx_calls.append(day)
        return {"status": "resolved", "rate": "5.25", "quote_date": day, "source": "synthetic test quote"}

    def command(self, action: str, payload: dict, request_id: str | None = None) -> dict:
        self.request_number += 1
        return self.ledger.dispatch(action, payload, request_id or f"synthetic-photos-{self.request_number}")

    def photo(self) -> str:
        return self.command("photo.save", {"content_base64": base64.b64encode(tiny_png()).decode("ascii")})["photo_id"]

    def entry_payload(self, **changes) -> dict:
        return {
            "date": "2026-09-02", "kind": "expense", "currency": "SGD", "amount": "2.00",
            "name": "synthetic purchase", "category_id": self.category, **changes,
        }

    def state(self) -> dict:
        return self.ledger.state({"month": "2026-09"})

    def stored_ids(self, entry_id: int) -> list[str] | None:
        row = self.ledger.db.execute("SELECT value FROM meta WHERE key=?", (f"entry.photos.{entry_id}",)).fetchone()
        return json.loads(row[0]) if row else None

    def assert_invalid(self, payload: dict) -> None:
        before = self.state()
        with self.assertRaises(LedgerError) as caught:
            self.command("entry.save", payload)
        self.assertEqual(caught.exception.code, "photo_invalid")
        self.assertEqual(self.state(), before)

    def test_multiple_photos_edit_remove_and_clear_without_changing_finances(self) -> None:
        photos = [self.photo() for _ in range(3)]
        entry = self.command("entry.save", self.entry_payload(photo_ids=photos[:2]))["entry"]
        before = self.state()
        quote = {key: entry[key] for key in ("amount", "rate", "quote_date", "cny_amount", "fx_status")}
        for expected in ([photos[1], photos[0], photos[2]], [photos[2], photos[0]], []):
            entry = self.command("entry.save", {"id": entry["id"], "photo_ids": expected})["entry"]
            self.assertEqual(entry["photo_ids"], expected)
            self.assertEqual(entry["photo_id"], expected[0] if expected else None)
            self.assertEqual(self.stored_ids(entry["id"]), expected)
            legacy = self.ledger.db.execute("SELECT photo_id FROM entries WHERE id=?", (entry["id"],)).fetchone()[0]
            self.assertEqual(legacy, entry["photo_id"])
            self.assertEqual({key: entry[key] for key in quote}, quote)
            self.assertEqual(self.state()["wallets"], before["wallets"])
            self.assertEqual(self.state()["summary"], before["summary"])
        self.assertEqual(self.fx_calls, ["2026-09-02"])
        # Removing an association never deletes an uploaded original.
        for photo in photos:
            self.assertEqual(read_photo(self.root, photo), (tiny_png(), "image/png"))

    def test_duplicate_ids_keep_the_first_occurrence_order(self) -> None:
        first, second = self.photo(), self.photo()
        entry = self.command("entry.save", self.entry_payload(photo_ids=[second, first, second, first]))["entry"]
        self.assertEqual(entry["photo_ids"], [second, first])
        self.assertEqual(entry["photo_id"], second)

    def test_legacy_single_photo_replaces_all_and_null_clears(self) -> None:
        first, second, third = self.photo(), self.photo(), self.photo()
        entry = self.command("entry.save", self.entry_payload(photo_id=first))["entry"]
        self.assertEqual(entry["photo_ids"], [first])
        self.command("entry.save", {"id": entry["id"], "photo_ids": [first, second]})
        replaced = self.command("entry.save", {"id": entry["id"], "photo_id": third})["entry"]
        self.assertEqual(replaced["photo_ids"], [third])
        cleared = self.command("entry.save", {"id": entry["id"], "photo_id": None})["entry"]
        self.assertEqual(cleared["photo_ids"], [])
        self.assertIsNone(cleared["photo_id"])

    def test_both_fields_accept_consistent_first_photo_and_reject_conflict(self) -> None:
        first, second = self.photo(), self.photo()
        entry = self.command("entry.save", self.entry_payload(photo_ids=[first, second], photo_id=first))["entry"]
        for legacy in (second, None):
            self.assert_invalid({"id": entry["id"], "photo_ids": [first, second], "photo_id": legacy})
        self.assert_invalid({"id": entry["id"], "photo_ids": [], "photo_id": first})
        cleared = self.command("entry.save", {"id": entry["id"], "photo_ids": [], "photo_id": None})["entry"]
        self.assertEqual(cleared["photo_ids"], [])

    def test_omission_preserves_all_even_when_existing_files_are_missing(self) -> None:
        first, second = self.photo(), self.photo()
        entry = self.command("entry.save", self.entry_payload(photo_ids=[first, second]))["entry"]
        (self.root / "photos" / first).unlink()
        edited = self.command("entry.save", {"id": entry["id"], "note": "synthetic edit"})["entry"]
        self.assertEqual(edited["photo_ids"], [first, second])
        # An explicit retained list/reorder also need not re-open old files.
        reordered = self.command("entry.save", {"id": entry["id"], "photo_ids": [second, first]})["entry"]
        self.assertEqual(reordered["photo_ids"], [second, first])
        retained = self.command("entry.save", {"id": entry["id"], "photo_id": first})["entry"]
        self.assertEqual(retained["photo_ids"], [first])
        self.command("entry.save", {"id": entry["id"], "photo_ids": []})
        # Re-adding a removed association is new and must pass file validation.
        self.assert_invalid({"id": entry["id"], "photo_ids": [first]})

    def test_invalid_array_values_and_path_ids_leave_entry_unchanged(self) -> None:
        first = self.photo()
        entry = self.command("entry.save", self.entry_payload(photo_ids=[first]))["entry"]
        for invalid in (None, first, {}, True, [None], [17], [False], [""], ["../outside.png"], [str(self.root / first)]):
            with self.subTest(invalid=invalid):
                self.assert_invalid({"id": entry["id"], "photo_ids": invalid})
        for invalid in ("../outside.png", 1, ""):
            self.assert_invalid({"id": entry["id"], "photo_id": invalid})

    def test_missing_corrupt_and_wrong_extension_new_photos_are_rejected(self) -> None:
        missing = f"{uuid.uuid4()}.png"
        self.assert_invalid(self.entry_payload(photo_ids=[missing]))
        self.assert_invalid(self.entry_payload(photo_id=missing))
        corrupt = self.photo()
        (self.root / "photos" / corrupt).write_bytes(b"not an image")
        self.assert_invalid(self.entry_payload(photo_ids=[corrupt]))
        mislabeled = f"{uuid.uuid4()}.jpg"
        (self.root / "photos" / mislabeled).write_bytes(tiny_png())
        self.assert_invalid(self.entry_payload(photo_ids=[mislabeled]))

    def test_new_symlink_photo_is_rejected_when_supported(self) -> None:
        photo = f"{uuid.uuid4()}.png"
        outside = self.root / "outside.png"
        outside.write_bytes(tiny_png())
        try:
            (self.root / "photos" / photo).symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("this Windows runner does not permit symlink creation")
        # Avoid the backup layer rejecting the synthetic symlink first, so
        # this specifically exercises the entry-association boundary.
        with patch.object(self.ledger.backups, "ensure_daily"):
            self.assert_invalid(self.entry_payload(photo_ids=[photo]))

    def test_twenty_photo_limit_applies_before_deduplication(self) -> None:
        photos = [self.photo() for _ in range(20)]
        entry = self.command("entry.save", self.entry_payload(photo_ids=photos))["entry"]
        self.assertEqual(entry["photo_ids"], photos)
        self.assert_invalid({"id": entry["id"], "photo_ids": [*photos, self.photo()]})
        self.assert_invalid({"id": entry["id"], "photo_ids": [photos[0]] * 21})

    def test_request_replay_returns_same_photos_without_duplicate_entry_or_wallet_change(self) -> None:
        photos = [self.photo(), self.photo()]
        payload = self.entry_payload(photo_ids=photos)
        result = self.command("entry.save", payload, "synthetic-create-replay")
        before = self.state()
        self.assertEqual(self.command("entry.save", payload, "synthetic-create-replay"), result)
        self.assertEqual(self.state(), before)
        edit = {"id": result["entry"]["id"], "photo_ids": list(reversed(photos))}
        updated = self.command("entry.save", edit, "synthetic-edit-replay")
        (self.root / "photos" / photos[0]).unlink()
        self.assertEqual(self.command("entry.save", edit, "synthetic-edit-replay"), updated)
        with self.assertRaises(LedgerError) as caught:
            self.command("entry.save", {**edit, "photo_ids": []}, "synthetic-edit-replay")
        self.assertEqual(caught.exception.code, "request_conflict")
        self.assertEqual(self.state()["wallets"], before["wallets"])
        self.assertEqual(len(self.state()["entries"]), 1)

    def test_associations_and_entry_changes_roll_back_with_failed_request(self) -> None:
        first, second = self.photo(), self.photo()
        entry = self.command("entry.save", self.entry_payload(photo_ids=[first]))["entry"]
        before = self.state()
        edit = {"id": entry["id"], "photo_ids": [second, first], "amount": "3.00"}
        with patch.object(self.ledger, "_remember_request", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(LedgerError) as caught:
                self.command("entry.save", edit, "synthetic-atomic-retry")
        self.assertEqual(caught.exception.code, "storage_error")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.stored_ids(entry["id"]), [first])
        self.assertIsNone(self.ledger.db.execute("SELECT 1 FROM requests WHERE request_id='synthetic-atomic-retry'").fetchone())
        retried = self.command("entry.save", edit, "synthetic-atomic-retry")["entry"]
        self.assertEqual(retried["photo_ids"], [second, first])
        self.assertEqual(retried["amount"], "3.00")

    def test_create_failure_does_not_leave_entry_or_metadata(self) -> None:
        photo = self.photo()
        with patch.object(self.ledger, "_remember_request", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(LedgerError):
                self.command("entry.save", self.entry_payload(photo_ids=[photo]))
        self.assertEqual(self.state()["entries"], [])
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'entry.photos.%'").fetchone()[0], 0)

    def test_multiple_photos_survive_restart_and_backup_restore(self) -> None:
        photos = [self.photo(), self.photo()]
        entry = self.command("entry.save", self.entry_payload(photo_ids=photos))["entry"]
        original = self.state()
        self.ledger.close()
        self.ledger = Ledger(self.root, fx_resolver=self.resolve_rate)
        self.assertEqual(self.state(), original)
        backup = self.command("backup.create", {})["name"]
        self.command("entry.save", {"id": entry["id"], "photo_ids": [], "amount": "4.00"})
        (self.root / "photos" / photos[1]).unlink()
        self.command("backup.restore", {"name": backup, "confirm": True})
        self.assertEqual(self.state(), original)
        self.assertEqual(self.stored_ids(entry["id"]), photos)
        for photo in photos:
            self.assertEqual(read_photo(self.root, photo), (tiny_png(), "image/png"))

    def test_old_single_photo_backup_has_fallback_and_can_be_edited_after_restore(self) -> None:
        first, second = self.photo(), self.photo()
        entry = self.command("entry.save", self.entry_payload(photo_id=first))["entry"]
        # Simulate the prior version's representation in this temporary DB.
        with self.ledger.db:
            self.ledger.db.execute("DELETE FROM meta WHERE key=?", (f"entry.photos.{entry['id']}",))
        self.assertIsNone(self.stored_ids(entry["id"]))
        self.assertEqual(self.state()["entries"][0]["photo_ids"], [first])
        backup = self.command("backup.create", {})["name"]
        self.command("entry.save", {"id": entry["id"], "photo_ids": [second, first]})
        self.command("backup.restore", {"name": backup, "confirm": True})
        self.assertIsNone(self.stored_ids(entry["id"]))
        self.assertEqual(self.state()["entries"][0]["photo_ids"], [first])
        edited = self.command("entry.save", {"id": entry["id"], "note": "synthetic legacy edit"})["entry"]
        self.assertEqual(edited["photo_ids"], [first])
        self.assertEqual(self.stored_ids(entry["id"]), [first])


if __name__ == "__main__":
    unittest.main()
