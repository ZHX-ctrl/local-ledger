"""Category deletion regressions using synthetic data in temporary folders."""

from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ledger_app.service import Ledger, LedgerError


class CategoryDeleteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.ledger = self.open_ledger()
        self.request_counter = 0
        self.meal = next(c["id"] for c in self.state()["categories"] if c["name"] == "三餐")
        for currency in ("CNY", "SGD"):
            self.command(
                "snapshot.save",
                {"currency": currency, "balance": "100.00", "cutoff": "2026-08-31"},
            )

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def open_ledger(self) -> Ledger:
        return Ledger(
            self.root,
            fx_resolver=lambda day: {
                "status": "resolved", "rate": "5.00", "quote_date": day,
                "source": "synthetic test source",
            },
        )

    def state(self, **filters: object) -> dict[str, object]:
        return self.ledger.state({"month": "2026-09", **filters})

    def command(
        self, action: str, payload: dict[str, object], request_id: str | None = None,
    ) -> dict[str, object]:
        self.request_counter += 1
        return self.ledger.dispatch(action, payload, request_id or f"category-test-{self.request_counter}")

    def entry(self, **changes: object) -> dict[str, object]:
        return {
            "date": "2026-09-02", "kind": "expense", "currency": "CNY",
            "amount": "10.00", "name": "synthetic lunch", "category_id": self.meal,
            **changes,
        }

    def assert_error(self, code: str, action: str, payload: dict[str, object]) -> None:
        with self.assertRaises(LedgerError) as caught:
            self.command(action, payload)
        self.assertEqual(caught.exception.code, code)

    def test_used_category_disappears_without_changing_history_summary_or_wallets(self) -> None:
        self.command("entry.save", self.entry())
        self.command("entry.save", self.entry(name="synthetic snack", currency="SGD", amount="2.00"))
        before = self.state()
        self.assertEqual(self.command("category.delete", {"id": self.meal}), {"deleted": True})
        after = self.state()
        self.assertNotIn(self.meal, [c["id"] for c in after["categories"]])
        self.assertEqual(after["historical_categories"], [
            {"id": self.meal, "name": "三餐", "active": False, "deleted": True},
        ])
        for field in ("entries", "summary", "wallets", "snapshots", "exchanges", "internal_transfers"):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(self.state(category_id=self.meal)["entries"], before["entries"])
        # Historical options remain available even when the current list has no entries.
        self.assertEqual(self.state(month="2026-08")["historical_categories"], after["historical_categories"])
        export = self.command("export.csv", {"month": "2026-09"})
        with (self.root / "exports" / export["name"]).open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual([row["category"] for row in csv.DictReader(stream)], ["三餐", "三餐"])

    def test_unused_active_and_inactive_categories_can_be_deleted(self) -> None:
        for active in (True, False):
            with self.subTest(active=active):
                category = self.command("category.save", {"name": f"synthetic unused {active}", "active": active})["category"]
                self.command("category.delete", {"id": category["id"]})
                state = self.state()
                self.assertNotIn(category["id"], [c["id"] for c in state["categories"]])
                self.assertNotIn(category["id"], [c["id"] for c in state["historical_categories"]])

    def test_inactive_used_category_remains_a_historical_option(self) -> None:
        entry = self.command("entry.save", self.entry())["entry"]
        self.command("category.save", {"id": self.meal, "active": False})
        self.command("category.delete", {"id": self.meal})
        self.assertEqual(self.state()["entries"], [entry])
        self.assertEqual(self.state()["historical_categories"][0]["id"], self.meal)
        self.command("entry.delete", {"id": entry["id"]})
        self.assertEqual(self.state()["historical_categories"], [])

    def test_existing_entry_may_keep_deleted_category_but_other_entries_cannot_select_it(self) -> None:
        old = self.command("entry.save", self.entry())["entry"]
        other_category = self.command("category.save", {"name": "synthetic other"})["category"]
        other = self.command("entry.save", self.entry(name="synthetic second", category_id=other_category["id"]))["entry"]
        self.command("category.delete", {"id": self.meal})
        for edit in ({"id": old["id"], "note": "synthetic edit"}, {"id": old["id"], "category_id": self.meal, "amount": "12.00"}):
            result = self.command("entry.save", edit)["entry"]
            self.assertEqual(result["category_id"], self.meal)
            self.assertEqual(result["category_name"], "三餐")
        before = self.state()
        self.assert_error("category_deleted", "entry.save", self.entry(name="synthetic new"))
        self.assert_error("category_deleted", "entry.save", {"id": other["id"], "category_id": self.meal})
        self.assert_error("category_required", "entry.save", self.entry(category_id=None, category_name="三餐"))
        self.assertEqual(self.state(), before)
        self.command("entry.save", {"id": old["id"], "category_id": other_category["id"]})
        self.assertEqual(self.state()["historical_categories"], [])
        self.assert_error("category_deleted", "entry.save", {"id": old["id"], "category_id": self.meal})

    def test_deleted_category_cannot_be_renamed_or_reactivated(self) -> None:
        self.command("entry.save", self.entry())
        self.command("category.delete", {"id": self.meal})
        before = self.state()
        for change in ({"name": "synthetic renamed"}, {"active": True}, {"active": False}, {}):
            with self.subTest(change=change):
                self.assert_error("category_deleted", "category.save", {"id": self.meal, **change})
                self.assertEqual(self.state(), before)

    def test_recreating_same_name_gives_new_identity_and_keeps_old_display_name(self) -> None:
        old = self.command("entry.save", self.entry())["entry"]
        self.command("category.delete", {"id": self.meal})
        replacement = self.command("category.save", {"name": "三餐"})["category"]
        self.assertNotEqual(replacement["id"], self.meal)
        new = self.command("entry.save", self.entry(name="synthetic replacement", category_id=None, category_name="三餐"))["entry"]
        self.assertEqual(new["category_id"], replacement["id"])
        self.command("category.save", {"id": replacement["id"], "name": "synthetic replacement renamed"})
        state = self.state()
        self.assertEqual(next(e for e in state["entries"] if e["id"] == old["id"]), old)
        self.assertEqual(state["historical_categories"][0]["name"], "三餐")
        self.command("category.delete", {"id": replacement["id"]})
        again = self.command("category.save", {"name": "三餐"})["category"]
        self.assertNotIn(again["id"], (self.meal, replacement["id"]))

    def test_reserved_internal_name_key_is_not_a_valid_category_name(self) -> None:
        self.command("category.delete", {"id": self.meal})
        internal_key = self.ledger.db.execute("SELECT name_key FROM categories WHERE id=?", (self.meal,)).fetchone()[0]
        self.assert_error("invalid_input", "category.save", {"name": internal_key})
        self.assert_error("invalid_input", "entry.save", self.entry(category_id=None, category_name=internal_key))

    def test_repeated_delete_and_request_retries_are_idempotent(self) -> None:
        payload = {"id": self.meal}
        result = self.command("category.delete", payload, "synthetic-delete-retry")
        before = self.state()
        with patch.object(self.ledger.backups, "ensure_daily", side_effect=OSError("synthetic failure")):
            self.assertEqual(self.command("category.delete", payload, "synthetic-delete-retry"), result)
        self.assertEqual(self.command("category.delete", payload), result)
        self.assertEqual(self.state(), before)
        with self.assertRaises(LedgerError) as caught:
            self.command("category.delete", {"id": self.meal + 1}, "synthetic-delete-retry")
        self.assertEqual(caught.exception.code, "request_conflict")
        self.assert_error("not_found", "category.delete", {"id": 999999})
        self.assert_error("invalid_id", "category.delete", {"id": 0})

    def test_backup_failure_leaves_category_and_request_available_for_retry(self) -> None:
        self.command("entry.save", self.entry())
        before = self.state()
        with patch.object(self.ledger.backups, "ensure_daily", side_effect=OSError("synthetic failure")):
            with self.assertRaises(LedgerError) as caught:
                self.command("category.delete", {"id": self.meal}, "synthetic-backup-retry")
        self.assertEqual(caught.exception.code, "backup_failed")
        self.assertEqual(self.state(), before)
        self.assertEqual(self.command("category.delete", {"id": self.meal}, "synthetic-backup-retry"), {"deleted": True})

    def test_restart_keeps_deleted_state_old_names_and_recreated_categories(self) -> None:
        old = self.command("entry.save", self.entry())["entry"]
        self.command("category.delete", {"id": self.meal})
        self.command("category.save", {"name": "三餐"})
        before = self.state()
        self.ledger.close()
        self.ledger = self.open_ledger()
        self.assertEqual(self.state(), before)
        self.assert_error("category_deleted", "category.save", {"id": self.meal, "active": True})
        self.assertEqual(self.command("entry.save", {"id": old["id"], "note": "synthetic after restart"})["entry"]["category_name"], "三餐")

    def test_real_archives_restore_deleted_metadata_and_predelete_state(self) -> None:
        self.command("entry.save", self.entry())
        before = self.state()
        predelete_archive = self.command("backup.create", {})["name"]
        self.command("category.delete", {"id": self.meal}, "synthetic-restored-delete")
        deleted = self.state()
        deleted_archive = self.command("backup.create", {})["name"]
        replacement = self.command("category.save", {"name": "三餐"})["category"]
        self.command("entry.save", self.entry(name="synthetic later entry", category_id=replacement["id"]))
        self.command("backup.restore", {"name": deleted_archive, "confirm": True})
        self.assertEqual(self.state(), deleted)
        self.assertEqual(self.command("category.delete", {"id": self.meal}, "synthetic-restored-delete"), {"deleted": True})
        self.assert_error("category_deleted", "category.save", {"id": self.meal, "active": True})
        self.command("backup.restore", {"name": predelete_archive, "confirm": True})
        self.assertEqual(self.state(), before)
        self.assertEqual(self.ledger.db.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(self.ledger.db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
