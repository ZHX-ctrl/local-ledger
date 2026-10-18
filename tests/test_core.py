"""Focused core-service tests using only synthetic ledger data."""

from __future__ import annotations

import csv
import tempfile
import errno
from pathlib import Path
import unittest
from unittest.mock import patch

from ledger_app.service import Ledger, LedgerError
from ledger_app.backup import _fail


class _FakeBackups:
    """Keep core tests independent of archive I/O and network timing."""

    def __init__(self) -> None:
        self.created: list[str] = []

    def ensure_daily(self) -> None:
        return None

    def create(self, kind: str = "manual") -> str:
        name = f"{kind}-test.zip"
        self.created.append(name)
        return name

    def list(self) -> list[dict[str, object]]:
        return []

    def restore(self, name: str) -> None:
        return None


class CoreLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.calls: list[str] = []
        self.rate_mode = "resolved"

        def resolver(day: str) -> dict[str, str]:
            self.calls.append(day)
            if self.rate_mode == "pending":
                return {"status": "pending", "reason": "synthetic offline"}
            return {
                "status": "resolved",
                "rate": "5.271",
                "quote_date": day,
                "source": "synthetic test source",
            }

        self.ledger = Ledger(Path(self.temp.name), fx_resolver=resolver)
        self.ledger.backups = _FakeBackups()
        self._request_counter = 0
        for currency in ("CNY", "SGD"):
            self.command(
                "snapshot.save",
                {"currency": currency, "balance": "100.00", "cutoff": "2026-08-31"},
            )
        self.meal = next(c["id"] for c in self.ledger.state()["categories"] if c["name"] == "三餐")
        self.drink = next(c["id"] for c in self.ledger.state()["categories"] if c["name"] == "饮料")

    def tearDown(self) -> None:
        self.ledger.close()
        self.temp.cleanup()

    def command(self, action: str, payload: dict[str, object], request_id: str | None = None) -> dict[str, object]:
        self._request_counter += 1
        return self.ledger.dispatch(action, payload, request_id or f"request-{self._request_counter}")

    def entry(self, **changes: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "date": "2026-09-02",
            "kind": "expense",
            "currency": "CNY",
            "amount": "10.00",
            "name": "synthetic lunch",
            "category_id": self.meal,
        }
        payload.update(changes)
        return payload

    def test_backup_failure_explains_cause_without_writing_or_leaking(self) -> None:
        before = self.ledger.state()
        cases = [
            (PermissionError(errno.EACCES, "secret amount and name", "private-filename"), "权限"),
            (OSError(errno.ENOSPC, "secret amount and name"), "磁盘空间不足"),
            (_fail("备份数据库版本不兼容。"), "版本不兼容"),
        ]
        for cause, expected in cases:
            wrapped = _fail("创建备份失败。")
            wrapped.__cause__ = cause
            with self.subTest(expected=expected), patch.object(self.ledger.backups, "ensure_daily", side_effect=wrapped):
                with self.assertRaises(LedgerError) as caught:
                    self.command("entry.save", self.entry(), "retry-after-backup-failure")
                self.assertIn(expected, caught.exception.message)
                self.assertNotIn("secret", caught.exception.message)
                self.assertNotIn("private-filename", caught.exception.message)
                self.assertEqual(self.ledger.state(), before)
        result = self.command("entry.save", self.entry(), "retry-after-backup-failure")
        self.assertGreater(result["entry"]["id"], 0)
        self.assertEqual(self.command("entry.save", self.entry(), "retry-after-backup-failure"), result)

    def test_manual_backup_and_startup_check_explain_errors(self) -> None:
        failure = PermissionError(errno.EACCES, "secret")
        with patch.object(self.ledger.backups, "create", side_effect=failure):
            with self.assertRaises(LedgerError) as caught:
                self.command("backup.create", {})
            self.assertIn("权限", caught.exception.message)
        with patch.object(self.ledger.backups, "ensure_daily", side_effect=failure):
            with self.assertRaises(LedgerError) as caught:
                self.ledger.prepare_daily_backup()
            self.assertIn("权限", caught.exception.message)

    def test_fx_quote_is_immutable_and_wallet_uses_cutoff_anchor(self) -> None:
        first = self.command("entry.save", self.entry(currency="SGD", amount="2.00"))["entry"]
        self.assertEqual(first["cny_amount"], "10.54")
        self.assertEqual(self.calls, ["2026-09-02"])

        edited = self.command(
            "entry.save",
            self.entry(id=first["id"], currency="SGD", amount="3.00"),
        )["entry"]
        self.assertEqual(edited["cny_amount"], "15.81")
        self.assertEqual(self.calls, ["2026-09-02"], "amount-only edits reuse the stored quote")

        moved = self.command(
            "entry.save",
            self.entry(id=first["id"], currency="SGD", amount="3.00", date="2026-09-03"),
        )["entry"]
        self.assertEqual(moved["quote_date"], "2026-09-03")
        self.assertEqual(self.calls, ["2026-09-02", "2026-09-03"])

        # A newer snapshot anchors the actual wallet.  Backfilling an older
        # month remains visible in the report but is already included in the
        # captured balance and therefore does not deduct it a second time.
        self.command(
            "snapshot.save",
            {"currency": "SGD", "balance": "80.00", "cutoff": "2026-09-05", "note": "synthetic anchor"},
        )
        self.command("entry.save", self.entry(currency="SGD", amount="4.00", date="2026-09-02", name="backfill"))
        state = self.ledger.state({"month": "2026-09"})
        wallet = next(w for w in state["wallets"] if w["currency"] == "SGD")
        self.assertEqual(wallet["balance"], "80.00")
        self.assertEqual(state["summary"]["expense_cny"], "36.89")
        self.assertEqual(state["summary"]["expense_by_currency"], {"CNY": "0.00", "SGD": "7.00"})
        self.assertEqual(state["today_rate"]["rate"], "5.271")
        self.assertEqual(state["today_rate"]["quote_date"], "2026-09-03")

        # A transaction entered later for an older date must call the resolver
        # with the transaction date, never the day on which it was entered.
        historical = self.command(
            "entry.save",
            self.entry(currency="SGD", amount="1.00", date="2026-08-20", name="historical expense"),
        )["entry"]
        self.assertEqual(self.calls[-1], "2026-08-20")
        self.assertEqual(historical["quote_date"], "2026-08-20")

    def test_wallet_updates_each_currency_after_save_edit_and_delete(self) -> None:
        def balances() -> dict[str, str]:
            return {wallet["currency"]: wallet["balance"] for wallet in self.ledger.state()["wallets"]}

        self.assertEqual(balances(), {"CNY": "100.00", "SGD": "100.00"})
        self.rate_mode = "pending"
        sgd = self.command("entry.save", self.entry(currency="SGD", amount="7.50"))["entry"]
        self.assertEqual(sgd["fx_status"], "pending")
        self.assertEqual(balances(), {"CNY": "100.00", "SGD": "92.50"})

        cny = self.command("entry.save", self.entry(currency="CNY", amount="12.30"))["entry"]
        self.assertEqual(balances(), {"CNY": "87.70", "SGD": "92.50"})
        self.command("entry.save", self.entry(id=sgd["id"], currency="SGD", amount="8.00"))
        self.assertEqual(balances(), {"CNY": "87.70", "SGD": "92.00"})
        self.command("entry.delete", {"id": cny["id"]})
        self.assertEqual(balances(), {"CNY": "100.00", "SGD": "92.00"})

        self.command("entry.save", self.entry(currency="CNY", kind="income", amount="3.25", name="synthetic income"))
        self.assertEqual(balances(), {"CNY": "103.25", "SGD": "92.00"})
        self.command("entry.save", self.entry(currency="SGD", amount="4.00", date="2026-08-20", name="synthetic backfill"))
        self.assertEqual(balances(), {"CNY": "103.25", "SGD": "92.00"})

    def test_pending_fx_refreshes_and_cache_is_reused(self) -> None:
        self.rate_mode = "pending"
        pending = self.command("entry.save", self.entry(currency="SGD", amount="2.00"))["entry"]
        self.assertEqual(pending["fx_status"], "pending")
        self.assertIsNone(pending["cny_amount"])
        self.assertEqual(self.ledger.state({"month": "2026-09"})["summary"]["pending_count"], 1)

        self.rate_mode = "resolved"
        refreshed = self.command("fx.refresh", {})
        self.assertEqual(refreshed, {"updated": 1, "pending": 0})
        resolved = self.ledger.state({"month": "2026-09"})["entries"][0]
        self.assertEqual(resolved["cny_amount"], "10.54")

        calls_after_refresh = len(self.calls)
        second = self.command("entry.save", self.entry(currency="SGD", amount="1.00", name="cached"))["entry"]
        self.assertEqual(second["cny_amount"], "5.27")
        self.assertEqual(len(self.calls), calls_after_refresh, "resolved dates come from fx_cache")

    def test_entry_can_be_recorded_before_wallet_initialization(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            pending_ledger = Ledger(Path(root), fx_resolver=lambda day: {
                "status": "resolved",
                "rate": "5.271",
                "quote_date": day,
                "source": "synthetic test source",
            })
            pending_ledger.backups = _FakeBackups()
            try:
                category = next(
                    item["id"] for item in pending_ledger.state()["categories"] if item["name"] == "其他"
                )
                saved = pending_ledger.dispatch(
                    "entry.save",
                    {
                        "date": "2026-09-19",
                        "kind": "expense",
                        "currency": "CNY",
                        "amount": "99.97",
                        "name": "synthetic yoga mat",
                        "category_id": category,
                    },
                    "before-wallet-snapshot",
                )
                self.assertEqual(saved["entry"]["amount"], "99.97")
                self.assertFalse(next(w for w in pending_ledger.state()["wallets"] if w["currency"] == "CNY")["initialized"])
                self.assertEqual(len(pending_ledger.state({"month": "2026-09"})["entries"]), 1)
            finally:
                pending_ledger.close()

    def test_idempotency_duplicate_confirmation_and_atomic_errors(self) -> None:
        payload = self.entry(name="same expense")
        one = self.ledger.dispatch("entry.save", payload, "same-request")
        again = self.ledger.dispatch("entry.save", payload, "same-request")
        self.assertEqual(one, again)
        self.assertEqual(len(self.ledger.state({"month": "2026-09"})["entries"]), 1)

        with self.assertRaises(LedgerError) as duplicate:
            self.command("entry.save", payload)
        self.assertEqual(duplicate.exception.code, "duplicate_suspected")
        self.command("entry.save", dict(payload, confirm_duplicate=True))
        self.assertEqual(len(self.ledger.state({"month": "2026-09"})["entries"]), 2)

        with self.assertRaises(LedgerError) as conflict:
            self.ledger.dispatch("entry.save", self.entry(amount="11.00"), "same-request")
        self.assertEqual(conflict.exception.code, "request_conflict")

        before = self.ledger.state({"month": "2026-09"})
        with self.assertRaises(LedgerError):
            self.command("entry.save", self.entry(amount="not-money"))
        self.assertEqual(len(self.ledger.state({"month": "2026-09"})["entries"]), len(before["entries"]))

        entry_id = before["entries"][0]["id"]
        self.command("entry.delete", {"id": entry_id})
        self.command("entry.delete", {"id": entry_id})
        self.assertEqual(len(self.ledger.state({"month": "2026-09"})["entries"]), 1)

    def test_income_duplicates_require_confirmation_and_kind_isolated(self) -> None:
        income = self.entry(kind="income", amount="21.00", name="same transfer", note="first")
        saved = self.command("entry.save", income)["entry"]

        # Date, type, currency, amount, and name identify a suspected repeat;
        # changing the note or category does not bypass the confirmation.
        with self.assertRaises(LedgerError) as duplicate:
            self.command(
                "entry.save",
                self.entry(
                    kind="income",
                    amount="21.00",
                    name="same transfer",
                    note="second note",
                    category_id=self.drink,
                ),
            )
        self.assertEqual(duplicate.exception.code, "duplicate_suspected")

        # Updating the same row excludes its own ID from duplicate detection.
        edited = self.command(
            "entry.save",
            dict(income, id=saved["id"], note="edited note", category_id=self.drink),
        )["entry"]
        self.assertEqual(edited["id"], saved["id"])

        # An expense with otherwise identical fields is a different kind and
        # can be inserted without a duplicate confirmation.
        expense = self.command(
            "entry.save",
            self.entry(kind="expense", amount="21.00", name="same transfer", note="expense"),
        )["entry"]
        self.assertNotEqual(expense["id"], saved["id"])

    def test_exchange_categories_filters_and_formula_safe_export(self) -> None:
        custom = self.command("category.save", {"name": "测试类别"})["category"]
        formula = self.command(
            "entry.save",
            self.entry(name="=synthetic", category_id=custom["id"], note="@memo"),
        )["entry"]
        self.command("category.save", {"id": custom["id"], "active": False})
        # Historical records remain editable when their inactive category is
        # left unchanged; selecting it for a new record is rejected.
        self.command(
            "entry.save",
            self.entry(id=formula["id"], amount="11.00", name="=synthetic", category_id=custom["id"]),
        )
        with self.assertRaises(LedgerError) as inactive:
            self.command("entry.save", self.entry(name="new", category_id=custom["id"]))
        self.assertEqual(inactive.exception.code, "category_inactive")

        self.command(
            "exchange.save",
            {
                "date": "2026-09-03",
                "from_currency": "CNY",
                "from_amount": "20.00",
                "to_currency": "SGD",
                "to_amount": "3.00",
                "note": "synthetic exchange",
            },
        )
        with self.assertRaises(LedgerError) as duplicate:
            self.command(
                "exchange.save",
                {
                    "date": "2026-09-03",
                    "from_currency": "CNY",
                    "from_amount": "20.00",
                    "to_currency": "SGD",
                    "to_amount": "3.00",
                    "note": "synthetic exchange",
                },
            )
        self.assertEqual(duplicate.exception.code, "duplicate_suspected")

        filtered = self.ledger.state({"month": "2026-09", "currency": "CNY", "q": "synthetic"})
        self.assertTrue(filtered["entries"])
        self.assertEqual(filtered["summary"]["month"], "2026-09")

        export = self.command("export.csv", {"month": "2026-09"})["name"]
        text = (Path(self.temp.name) / "exports" / str(export)).read_text(encoding="utf-8-sig")
        self.assertIn("'=synthetic", text)
        self.assertIn("'@memo", text)

    def test_internal_transfer_is_neutral_filterable_and_auditable(self) -> None:
        before = self.ledger.state({"month": "2026-09"})
        saved = self.command(
            "internal_transfer.save",
            {
                "date": "2026-09-07",
                "currency": "SGD",
                "amount": "1001.00",
                "from_account": "other account",
                "to_account": "DBS",
                "note": "synthetic neutral transfer",
            },
        )["internal_transfer"]
        after = self.ledger.state({"month": "2026-09", "currency": "SGD", "q": "DBS"})
        self.assertEqual(after["internal_transfers"], [saved])
        self.assertEqual(after["wallets"], before["wallets"])
        self.assertEqual(after["summary"], before["summary"])

        with self.assertRaises(LedgerError) as duplicate:
            self.command(
                "internal_transfer.save",
                {
                    "date": "2026-09-07",
                    "currency": "SGD",
                    "amount": "1001.00",
                    "from_account": "other account",
                    "to_account": "DBS",
                },
            )
        self.assertEqual(duplicate.exception.code, "duplicate_suspected")

        export = self.command("export.csv", {"month": "2026-09"})["name"]
        text = (Path(self.temp.name) / "exports" / str(export)).read_text(encoding="utf-8-sig")
        self.assertIn("internal_transfer", text)
        self.assertIn("other account", text)

        self.command("internal_transfer.delete", {"id": saved["id"]})
        self.assertEqual(self.ledger.state({"month": "2026-09"})["internal_transfers"], [])

    def test_amount_filter_matches_original_cents_and_composes_with_other_filters(self) -> None:
        matching = self.command(
            "entry.save", self.entry(currency="SGD", amount="6.00", name="synthetic match")
        )["entry"]
        income = self.command(
            "entry.save", self.entry(currency="SGD", amount="6.00", kind="income", name="synthetic income")
        )["entry"]
        cny = self.command("entry.save", self.entry(amount="6.00", name="synthetic CNY"))["entry"]
        for amount in ("6.01", "16.00"):
            self.command("entry.save", self.entry(currency="SGD", amount=amount, name=f"synthetic {amount}"))
        drink = self.command(
            "entry.save", self.entry(currency="SGD", amount="6.00", category_id=self.drink, name="other drink")
        )["entry"]
        previous = self.command(
            "entry.save", self.entry(currency="SGD", amount="6.00", date="2026-08-20", name="synthetic history")
        )["entry"]
        baseline = self.ledger.state({"month": "2026-09"})
        calls_before = list(self.calls)
        expected_sgd = {matching["id"], income["id"], drink["id"]}
        for amount in ("6", "6.0", "6.00", " 6.00 "):
            with self.subTest(amount=amount):
                state = self.ledger.state({"month": "2026-09", "currency": "SGD", "amount": amount})
                self.assertEqual({entry["id"] for entry in state["entries"]}, expected_sgd)
                self.assertEqual(state["summary"], baseline["summary"])
                self.assertEqual(state["wallets"], baseline["wallets"])
        both = self.ledger.state({"month": "2026-09", "amount": "6"})
        self.assertEqual({entry["id"] for entry in both["entries"]}, expected_sgd | {cny["id"]})
        combined = self.ledger.state({
            "month": "2026-09", "date_from": "2026-09-02", "date_to": "2026-09-02",
            "category_id": self.meal, "currency": "SGD", "amount": "6", "q": "match",
        })
        self.assertEqual(combined["entries"], [matching])
        # Explicit list dates can reach outside the selected summary month.
        history = self.ledger.state({
            "month": "2026-09", "date_from": "2026-08-01", "date_to": "2026-08-31", "amount": "6",
        })
        self.assertEqual(history["entries"], [previous])
        self.assertEqual(history["summary"], baseline["summary"])
        self.assertEqual(matching["cny_amount"], "31.63")
        self.assertEqual(self.ledger.state({"month": "2026-09", "amount": "31.63"})["entries"], [])
        self.assertEqual(self.calls, calls_before, "searches must never fetch rates")
        for empty in (None, "", " "):
            self.assertEqual(self.ledger.state({"month": "2026-09", "amount": empty}), baseline)

    def test_amount_filter_rejects_invalid_values_for_state_and_export(self) -> None:
        invalid_values = (
            "0", "-6", "6.001", "6e0", "NaN", "1,000", "6.", "90000000000000000.01",
            "9" * 64, 6, 6.0, True, [], {},
        )
        for amount in invalid_values:
            with self.subTest(amount=amount):
                with self.assertRaises(LedgerError) as state_error:
                    self.ledger.state({"month": "2026-09", "amount": amount})
                self.assertEqual(state_error.exception.code, "invalid_amount")
                with self.assertRaises(LedgerError) as export_error:
                    self.command("export.csv", {"filters": {"month": "2026-09", "amount": amount}})
                self.assertEqual(export_error.exception.code, "invalid_amount")
        self.assertEqual(list((Path(self.temp.name) / "exports").iterdir()), [])

    def test_amount_filter_applies_to_internal_transfers_and_csv(self) -> None:
        entry = self.command("entry.save", self.entry(currency="SGD", amount="6.00"))["entry"]
        self.command("entry.save", self.entry(currency="SGD", amount="6.01", name="synthetic unmatched"))
        transfers = []
        for currency, amount in (("SGD", "6.00"), ("SGD", "16.00"), ("CNY", "6.00")):
            transfers.append(self.command("internal_transfer.save", {
                "date": "2026-09-02", "currency": currency, "amount": amount,
                "from_account": "synthetic source", "to_account": "synthetic target",
            })["internal_transfer"])
        filters = {"month": "2026-09", "currency": "SGD", "amount": "6"}
        state = self.ledger.state(filters)
        self.assertEqual(state["entries"], [entry])
        self.assertEqual(state["internal_transfers"], [transfers[0]])
        self.assertEqual(self.ledger.state({**filters, "category_id": self.meal})["internal_transfers"], [])
        for payload in (filters, {"filters": filters}):
            exported = self.command("export.csv", payload)["name"]
            with (Path(self.temp.name) / "exports" / exported).open(encoding="utf-8-sig", newline="") as fh:
                rows = list(csv.DictReader(fh))
            self.assertEqual([(row["kind"], row["id"]) for row in rows], [
                ("expense", str(entry["id"])), ("internal_transfer", str(transfers[0]["id"])),
            ])
            self.assertTrue(all(row["currency"] == "SGD" and row["amount"] == "6.00" for row in rows))

    def test_exchange_amount_filter_matches_amount_and_currency_on_same_side(self) -> None:
        exchanges = []
        for from_amount, to_amount in (("30.00", "6.00"), ("6.00", "1.00"), ("36.00", "7.00")):
            exchanges.append(self.command("exchange.save", {
                "date": "2026-09-02", "from_currency": "CNY", "from_amount": from_amount,
                "to_currency": "SGD", "to_amount": to_amount, "note": "synthetic exchange",
            })["exchange"])
        baseline = self.ledger.state({"month": "2026-09"})
        both = self.ledger.state({"month": "2026-09", "amount": "6"})
        self.assertEqual({item["id"] for item in both["exchanges"]}, {item["id"] for item in exchanges[:2]})
        for currency, expected in (("SGD", exchanges[0]), ("CNY", exchanges[1])):
            state = self.ledger.state({"month": "2026-09", "currency": currency, "amount": "6"})
            self.assertEqual(state["exchanges"], [expected])
            self.assertEqual(state["summary"], baseline["summary"])
            self.assertEqual(state["wallets"], baseline["wallets"])

    def test_reopen_preserves_database_state(self) -> None:
        self.command("entry.save", self.entry(name="persisted"))
        root = Path(self.temp.name)
        self.ledger.close()
        reopened = Ledger(root, fx_resolver=lambda day: {"status": "pending", "reason": "unused"})
        try:
            entries = reopened.state({"month": "2026-09"})["entries"]
            self.assertEqual([entry["name"] for entry in entries], ["persisted"])
        finally:
            reopened.close()


if __name__ == "__main__":
    unittest.main()
