"""Transactional business service for the local two-currency ledger.

The HTTP and CLI layers intentionally remain thin.  This module owns the
database representation, validation, request idempotency, wallet arithmetic,
monthly reporting, and the immutable exchange-rate quote attached to each
SGD entry.
"""

from __future__ import annotations

import base64
import csv
import datetime as dt
from decimal import Decimal
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import uuid
from typing import Any, Callable

from .money import MoneyError, convert_cents, format_cents, parse_amount, parse_rate, rate_text
from .backup import backup_failure_reason
from .fx import expected_quote_date


LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MONTH_RE = re.compile(r"^[0-9]{4}-[0-9]{2}$")
_REQUEST_RE = re.compile(r"^[^\x00\r\n]{1,200}$")
_PHOTO_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\.(?:jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)
_CURRENCIES = {"CNY", "SGD"}
_KINDS = {"expense", "income"}
_CATEGORY_DELETED_PREFIX = "category.deleted."
_ENTRY_PHOTOS_PREFIX = "entry.photos."
_MAX_ENTRY_PHOTOS = 20
_DEFAULT_CATEGORIES = (
    "三餐",
    "饮料",
    "零食",
    "水果",
    "日用品",
    "房租",
    "交通",
    "旅行",
    "医疗",
    "学习",
    "娱乐",
    "话费",
    "其他",
)


class LedgerError(Exception):
    """A safe, JSON-serializable business error."""

    def __init__(self, code: str, message: str, details: Any = None):
        self.code = str(code)
        self.message = str(message)
        self.details = details
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _now() -> str:
    return dt.datetime.now(LOCAL_TZ).isoformat(timespec="seconds")


def _today() -> dt.date:
    return dt.datetime.now(LOCAL_TZ).date()


def _month_now() -> str:
    day = _today()
    return f"{day.year:04d}-{day.month:02d}"


def _month_bounds(month: str) -> tuple[str, str]:
    try:
        first = dt.date.fromisoformat(month + "-01")
    except ValueError as exc:
        raise LedgerError("invalid_month", "月份必须为 YYYY-MM") from exc
    if first.year < 1 or first.month > 12:  # pragma: no cover - fromisoformat handles it
        raise LedgerError("invalid_month", "月份必须为 YYYY-MM")
    if first.month == 12:
        next_month = dt.date(first.year + 1, 1, 1)
    else:
        next_month = dt.date(first.year, first.month + 1, 1)
    return first.isoformat(), (next_month - dt.timedelta(days=1)).isoformat()


def _safe_text(value: Any, field: str, *, allow_empty: bool = True, limit: int = 5000) -> str:
    if not isinstance(value, str):
        raise LedgerError("invalid_input", f"{field}必须是文本")
    value = value.strip()
    if not allow_empty and not value:
        raise LedgerError("invalid_input", f"{field}不能为空")
    if len(value) > limit or "\x00" in value:
        raise LedgerError("invalid_input", f"{field}长度或内容不合法")
    return value


def _parse_date(value: Any, field: str = "日期", *, allow_future: bool = False) -> str:
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise LedgerError("invalid_date", f"{field}必须是 YYYY-MM-DD")
    try:
        day = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerError("invalid_date", f"{field}不是有效日期") from exc
    if not allow_future and day > _today():
        raise LedgerError("future_date", f"{field}不能晚于今天")
    return day.isoformat()


def _parse_month(value: Any) -> str:
    if not isinstance(value, str) or not _MONTH_RE.fullmatch(value):
        raise LedgerError("invalid_month", "月份必须为 YYYY-MM")
    try:
        dt.date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise LedgerError("invalid_month", "月份不是有效日期") from exc
    return value


def _parse_currency(value: Any, field: str = "币种") -> str:
    if value not in _CURRENCIES:
        raise LedgerError("invalid_currency", f"{field}仅支持 CNY 或 SGD")
    return str(value)


def _parse_kind(value: Any) -> str:
    if value not in _KINDS:
        raise LedgerError("invalid_kind", "类型仅支持 expense 或 income")
    return str(value)


def _parse_id(value: Any, field: str = "id") -> int:
    if isinstance(value, bool):
        raise LedgerError("invalid_id", f"{field}必须是正整数")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise LedgerError("invalid_id", f"{field}必须是正整数") from exc
    if result <= 0 or str(value).strip() != str(result):
        raise LedgerError("invalid_id", f"{field}必须是正整数")
    return result


def _parse_money(value: Any, *, signed: bool = False, field: str = "金额") -> int:
    try:
        return parse_amount(value, signed=signed)
    except MoneyError as exc:
        raise LedgerError("invalid_amount", f"{field}必须是两位小数的金额") from exc


def _parse_filter_amount(value: Any) -> int | None:
    """Normalize a search amount without changing the strict write API."""
    if value is None:
        return None
    message = "筛选金额必须是大于 0 且最多两位小数的数字"
    if not isinstance(value, str):
        raise LedgerError("invalid_amount", message)
    value = value.strip()
    if not value:
        return None
    if len(value) > 32 or not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", value):
        raise LedgerError("invalid_amount", message)
    whole, _, fraction = value.partition(".")
    try:
        return parse_amount(f"{whole}.{fraction.ljust(2, '0')}")
    except MoneyError as exc:
        raise LedgerError("invalid_amount", message) from exc


class Ledger:
    """One process-owned, serialized ledger database."""

    def __init__(self, root: Path, fx_resolver: Callable[[str], dict[str, Any]] | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        for dirname in ("photos", "backups", "exports"):
            (self.root / dirname).mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "ledger.sqlite3"
        self.db = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.lock = threading.RLock()
        self._fx_resolver = fx_resolver
        self._closed = False
        self._initialize_schema()
        self.backups = self._make_backup_manager()

    def _initialize_schema(self) -> None:
        current_version = int(self.db.execute("PRAGMA user_version").fetchone()[0])
        if current_version not in (0, 1, 2):
            self.db.close()
            self._closed = True
            raise LedgerError("schema_incompatible", "账本数据库版本不兼容")
        schema_path = Path(__file__).with_name("schema.sql")
        with schema_path.open("r", encoding="utf-8") as fh:
            schema = fh.read()
        with self.db:
            self.db.executescript(schema)
            count = self.db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
            if count == 0:
                self.db.executemany(
                    "INSERT INTO categories(name, name_key, active) VALUES (?, ?, 1)",
                    [(name, name.casefold()) for name in _DEFAULT_CATEGORIES],
                )
            self.db.execute("PRAGMA user_version=2")

    def _make_backup_manager(self) -> Any:
        try:
            module = importlib.import_module(".backup", __package__)
            manager = getattr(module, "BackupManager")
            return manager(self.root, self.db, self.lock)
        except Exception as exc:
            self.db.close()
            self._closed = True
            if isinstance(exc, LedgerError):
                raise
            raise LedgerError("backup_unavailable", "备份组件尚未就绪，无法安全启动账本") from exc

    def close(self) -> None:
        with self.lock:
            if not self._closed:
                self.db.close()
                self._closed = True

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ----- public reads -------------------------------------------------

    def state(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        filters = {} if filters is None else filters
        if not isinstance(filters, dict):
            raise LedgerError("invalid_filters", "筛选条件必须是对象")
        with self.lock:
            return self._state_locked(filters)

    def backups_list(self) -> list[dict[str, Any]]:
        """Compatibility helper; the public contract also exposes backups.list()."""
        with self.lock:
            try:
                return self.backups.list()
            except LedgerError:
                raise
            except Exception as exc:
                raise LedgerError("backup_failed", "无法读取备份列表") from exc

    # ----- dispatch and idempotency ------------------------------------

    def dispatch(self, action: str, payload: dict[str, Any] | None, request_id: str) -> dict[str, Any]:
        if not isinstance(action, str) or not action:
            raise LedgerError("invalid_action", "缺少操作类型")
        if not isinstance(payload, dict):
            raise LedgerError("invalid_payload", "payload必须是对象")
        if not isinstance(request_id, str) or not _REQUEST_RE.fullmatch(request_id):
            raise LedgerError("invalid_request_id", "request_id必须是非空文本")
        known_actions = {
            "entry.save",
            "entry.delete",
            "snapshot.save",
            "exchange.save",
            "exchange.delete",
            "internal_transfer.save",
            "internal_transfer.delete",
            "category.save",
            "category.delete",
            "photo.save",
            "fx.refresh",
            "backup.create",
            "backup.restore",
            "export.csv",
        }
        if action not in known_actions:
            raise LedgerError("unknown_action", f"不支持的操作：{action}")
        try:
            request_material = _json_dumps({"action": action, "payload": payload})
        except (TypeError, ValueError) as exc:
            raise LedgerError("invalid_payload", "payload必须是可序列化的 JSON 对象") from exc
        request_hash = hashlib.sha256(request_material.encode("utf-8")).hexdigest()

        if action == "fx.refresh":
            return self._dispatch_fx_refresh(payload, request_id, request_hash)

        with self.lock:
            cached = self._get_request(request_id, request_hash)
            if cached is not None:
                return cached
            if action == "backup.restore":
                return self._dispatch_restore_locked(payload, request_id, request_hash)
            if action == "backup.create":
                return self._dispatch_backup_create_locked(request_id, request_hash)
            self.prepare_daily_backup()

            try:
                self.db.execute("BEGIN IMMEDIATE")
                if action == "entry.save":
                    result = self._entry_save(payload)
                elif action == "entry.delete":
                    result = self._entry_delete(payload)
                elif action == "snapshot.save":
                    result = self._snapshot_save(payload)
                elif action == "exchange.save":
                    result = self._exchange_save(payload)
                elif action == "exchange.delete":
                    result = self._exchange_delete(payload)
                elif action == "internal_transfer.save":
                    result = self._internal_transfer_save(payload)
                elif action == "internal_transfer.delete":
                    result = self._internal_transfer_delete(payload)
                elif action == "category.save":
                    result = self._category_save(payload)
                elif action == "category.delete":
                    result = self._category_delete(payload)
                elif action == "photo.save":
                    result = self._photo_save(payload)
                elif action == "export.csv":
                    result = self._export_csv(payload)
                else:  # guarded by known_actions above
                    raise LedgerError("unknown_action", f"不支持的操作：{action}")
                self._remember_request(request_id, request_hash, result)
                self.db.commit()
                return result
            except LedgerError:
                self.db.rollback()
                raise
            except sqlite3.IntegrityError as exc:
                self.db.rollback()
                raise LedgerError("storage_error", "账本写入未完成") from exc
            except Exception as exc:
                self.db.rollback()
                raise LedgerError("storage_error", "账本写入未完成") from exc

    def _dispatch_backup_create_locked(self, request_id: str, request_hash: str) -> dict[str, Any]:
        """Create a manual archive before opening a SQLite write transaction.

        ``BackupManager`` uses ``Connection.backup``; starting an IMMEDIATE
        transaction first can hold a write lock while the snapshot waits for
        itself.  The archive operation is serialized by the same RLock, and
        only the small idempotency-record write runs in a transaction.
        """

        try:
            name = self._backup_create()
            result = {"name": name}
            self.db.execute("BEGIN IMMEDIATE")
            self._remember_request(request_id, request_hash, result)
            self.db.commit()
            return result
        except LedgerError:
            try:
                self.db.rollback()
            except sqlite3.Error:
                pass
            raise
        except Exception as exc:
            try:
                self.db.rollback()
            except sqlite3.Error:
                pass
            raise LedgerError("backup_failed", "创建备份失败") from exc

    def _get_request(self, request_id: str, request_hash: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT request_hash, result_json FROM requests WHERE request_id=?", (request_id,)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise LedgerError("request_conflict", "同一 request_id 对应了不同内容")
        try:
            value = json.loads(row["result_json"])
        except (TypeError, json.JSONDecodeError) as exc:  # pragma: no cover - defensive database corruption guard
            raise LedgerError("storage_error", "历史请求结果无法读取") from exc
        if not isinstance(value, dict):  # pragma: no cover
            raise LedgerError("storage_error", "历史请求结果格式错误")
        return value

    def _remember_request(self, request_id: str, request_hash: str, result: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO requests(request_id, request_hash, result_json, created_at) VALUES (?, ?, ?, ?)",
            (request_id, request_hash, _json_dumps(result), _now()),
        )

    def _dispatch_restore_locked(self, payload: dict[str, Any], request_id: str, request_hash: str) -> dict[str, Any]:
        cached = self._get_request(request_id, request_hash)
        if cached is not None:
            return cached
        if payload.get("confirm") is not True:
            raise LedgerError("confirmation_required", "恢复备份必须明确 confirm=true")
        name = self._safe_archive_name(payload.get("name"))
        try:
            self.backups.ensure_daily()
            # BackupManager owns the live-connection/file rollback protocol.
            self.backups.restore(name)
            self.db.execute("BEGIN IMMEDIATE")
            result = {"restored": True}
            self._remember_request(request_id, request_hash, result)
            self.db.commit()
            return result
        except LedgerError:
            try:
                self.db.rollback()
            except sqlite3.Error:
                pass
            raise
        except Exception as exc:
            try:
                self.db.rollback()
            except sqlite3.Error:
                pass
            raise LedgerError("backup_failed", "恢复失败，现有账本应保持不变") from exc

    # ----- exchange-rate refresh --------------------------------------

    def _dispatch_fx_refresh(self, payload: dict[str, Any], request_id: str, request_hash: str) -> dict[str, Any]:
        if payload:
            raise LedgerError("invalid_payload", "fx.refresh不接受参数")
        # Snapshot request state briefly, then perform potentially slow network
        # calls without holding the process-wide ledger lock.  At most ten
        # unique dates are attempted per invocation; the remainder stays
        # pending for a later run.
        with self.lock:
            cached = self._get_request(request_id, request_hash)
            if cached is not None:
                return cached
            rows = self.db.execute(
                "SELECT DISTINCT date FROM entries WHERE deleted=0 AND fx_status='pending' AND currency='SGD' ORDER BY date LIMIT 10"
            ).fetchall()
            dates = [str(row[0]) for row in rows]
            # The dashboard shows the latest official SGD->CNY quote even
            # when there is no transaction dated today.  Include today in
            # every refresh without sending any ledger details.
            # ECB publishes today's quote only in the European afternoon, so
            # also ask for the previous business day; until then that is the
            # latest official quote the dashboard can show.
            today = _today().isoformat()
            previous = expected_quote_date(_today() - dt.timedelta(days=1)).isoformat()
            for extra in (previous, today):
                if extra not in dates:
                    dates = [extra, *dates[:9]]
        resolved: dict[str, dict[str, Any]] = {}
        for date_str in dates:
            record = self._call_rate_resolver(date_str)
            resolved[date_str] = record
            # A common offline failure is independent of the requested date;
            # stop after its first attempt instead of queueing ten sequential
            # network timeouts during startup.  Date-specific "not published"
            # responses continue so a single refresh can fill other dates.
            reason = str(record.get("reason", "")).casefold() if isinstance(record, dict) else ""
            if any(token in reason for token in ("无法联网", "断网", "offline", "network", "timeout", "超时", "连接")):
                break

        with self.lock:
            cached = self._get_request(request_id, request_hash)
            if cached is not None:
                return cached
            self.prepare_daily_backup()
            try:
                self.db.execute("BEGIN IMMEDIATE")
                updated = 0
                for date_str, record in resolved.items():
                    # A concurrent entry.save may have resolved and cached
                    # this date while the network call was in flight.  Keep
                    # that authoritative cached quote instead of downgrading
                    # it to a later pending response.
                    cached_rate = self.db.execute(
                        "SELECT status,rate,quote_date,source,reason FROM fx_cache WHERE consumption_date=?",
                        (date_str,),
                    ).fetchone()
                    if cached_rate is not None and cached_rate["status"] == "resolved":
                        cached_decimal = parse_rate(cached_rate["rate"])
                        normalized = {
                            "status": "resolved",
                            "rate": rate_text(cached_decimal),
                            "quote_date": cached_rate["quote_date"],
                            "source": cached_rate["source"] or "ECB via Frankfurter v2",
                            "_rate_decimal": cached_decimal,
                        }
                    else:
                        normalized = self._normalize_rate_record(date_str, record)
                    if normalized["status"] == "resolved":
                        self._cache_rate(date_str, normalized)
                        affected = self.db.execute(
                            "SELECT id, amount_cents FROM entries WHERE deleted=0 AND currency='SGD' AND date=? AND fx_status='pending'",
                            (date_str,),
                        ).fetchall()
                        for row in affected:
                            self.db.execute(
                                """UPDATE entries SET fx_status='resolved', rate=?, quote_date=?, fx_source=?,
                                   fx_reason=NULL, cny_cents=?, updated_at=?
                                   WHERE id=? AND fx_status='pending'""",
                                (
                                    normalized["rate"],
                                    normalized["quote_date"],
                                    normalized["source"],
                                    convert_cents(int(row["amount_cents"]), normalized["rate"]),
                                    _now(),
                                    int(row["id"]),
                                ),
                            )
                            updated += 1
                    else:
                        self._cache_rate(date_str, normalized)
                        self.db.execute(
                            "UPDATE entries SET fx_reason=?, updated_at=? WHERE deleted=0 AND currency='SGD' AND date=? AND fx_status='pending'",
                            (normalized.get("reason", "等待汇率"), _now(), date_str),
                        )
                pending = int(
                    self.db.execute("SELECT COUNT(*) FROM entries WHERE deleted=0 AND fx_status='pending'").fetchone()[0]
                )
                result = {"updated": updated, "pending": pending}
                self._remember_request(request_id, request_hash, result)
                self.db.commit()
                return result
            except LedgerError:
                self.db.rollback()
                raise
            except Exception as exc:
                self.db.rollback()
                raise LedgerError("storage_error", "汇率更新未完成") from exc

    def _call_rate_resolver(self, date_str: str) -> dict[str, Any]:
        try:
            resolver = self._fx_resolver
            if resolver is None:
                try:
                    module = importlib.import_module("ledger_app.fx")
                except ImportError:
                    module = importlib.import_module("fx")
                resolver = getattr(module, "resolve_rate")
            record = resolver(date_str)
            if not isinstance(record, dict):
                return {"status": "pending", "reason": "汇率响应格式错误，等待重试"}
            return record
        except Exception:
            return {"status": "pending", "reason": "暂时无法联网取得汇率，稍后重试"}

    def _normalize_rate_record(self, date_str: str, record: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(record, dict) or record.get("status") != "resolved":
            reason = "等待汇率"
            if isinstance(record, dict) and isinstance(record.get("reason"), str) and record["reason"].strip():
                reason = _safe_text(record["reason"], "reason", limit=300)
            return {"status": "pending", "reason": reason}
        try:
            rate = parse_rate(record.get("rate"))
            quote_date = _parse_date(record.get("quote_date"), "报价日期")
            consumption = dt.date.fromisoformat(date_str)
            quote = dt.date.fromisoformat(quote_date)
            if quote > consumption or quote > _today():
                raise ValueError("future quote")
            source = _safe_text(record.get("source", "ECB via Frankfurter v2"), "汇率来源", allow_empty=False, limit=300)
        except (LedgerError, MoneyError, ValueError, TypeError) as exc:
            return {"status": "pending", "reason": "汇率数据未通过校验，等待重试"}
        return {
            "status": "resolved",
            "rate": rate_text(rate),
            "quote_date": quote_date,
            "source": source,
            "_rate_decimal": rate,
        }

    def _cache_rate(self, date_str: str, record: dict[str, Any]) -> None:
        if record["status"] == "resolved":
            self.db.execute(
                """INSERT INTO fx_cache(consumption_date,status,rate,quote_date,source,reason,updated_at)
                   VALUES (?, 'resolved', ?, ?, ?, NULL, ?)
                   ON CONFLICT(consumption_date) DO UPDATE SET status='resolved', rate=excluded.rate,
                   quote_date=excluded.quote_date, source=excluded.source, reason=NULL, updated_at=excluded.updated_at""",
                (date_str, record["rate"], record["quote_date"], record["source"], _now()),
            )
        else:
            self.db.execute(
                """INSERT INTO fx_cache(consumption_date,status,rate,quote_date,source,reason,updated_at)
                   VALUES (?, 'pending', NULL, NULL, NULL, ?, ?)
                   ON CONFLICT(consumption_date) DO UPDATE SET status='pending', rate=NULL,
                   quote_date=NULL, source=NULL, reason=excluded.reason, updated_at=excluded.updated_at""",
                (date_str, record.get("reason", "等待汇率"), _now()),
            )

    def _rate_for_date(self, date_str: str) -> dict[str, Any]:
        cached = self.db.execute("SELECT * FROM fx_cache WHERE consumption_date=?", (date_str,)).fetchone()
        if cached is not None and cached["status"] == "resolved":
            rate = parse_rate(cached["rate"])
            return {
                "status": "resolved",
                "rate": rate_text(rate),
                "quote_date": cached["quote_date"],
                "source": cached["source"] or "ECB via Frankfurter v2",
                "_rate_decimal": rate,
            }
        record = self._normalize_rate_record(date_str, self._call_rate_resolver(date_str))
        self._cache_rate(date_str, record)
        return record

    # ----- entry mutations ---------------------------------------------

    def _entry_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        existing = None
        entry_id = None
        if "id" in payload and payload.get("id") is not None:
            entry_id = _parse_id(payload.get("id"))
            existing = self.db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
            if existing is None or int(existing["deleted"]):
                raise LedgerError("not_found", "记录不存在")

        date_str = _parse_date(payload.get("date", existing["date"] if existing else None))
        kind = _parse_kind(payload.get("kind", existing["kind"] if existing else None))
        currency = _parse_currency(payload.get("currency", existing["currency"] if existing else None))
        old_amount = format_cents(existing["amount_cents"]) if existing else None
        amount_cents = _parse_money(payload.get("amount", old_amount), field="金额")
        name = _safe_text(payload.get("name", existing["name"] if existing else None), "名称", allow_empty=False, limit=200)
        category_id = self._entry_category_id(payload, existing)
        note = _safe_text(payload.get("note", existing["note"] if existing else ""), "备注", limit=5000)
        photo_ids = self._entry_photos_from_payload(payload, existing)
        photo_id = photo_ids[0] if photo_ids else None

        # Entries may be recorded before a wallet baseline exists.  In that
        # state the entry and monthly statistics are still authoritative, but
        # the wallet card remains "待初始化" until the user supplies a
        # snapshot.  A later snapshot anchors the balance by its cutoff date.

        quote: dict[str, Any]
        if currency == "CNY":
            quote = {
                "status": "resolved",
                "rate": "1",
                "quote_date": date_str,
                "source": "原币 CNY",
                "_rate_decimal": Decimal("1"),
                "cny_cents": amount_cents,
            }
        elif (
            existing is not None
            and existing["currency"] == currency
            and existing["date"] == date_str
            and existing["fx_status"] == "resolved"
        ):
            rate = parse_rate(existing["rate"])
            quote = {
                "status": "resolved",
                "rate": rate_text(rate),
                "quote_date": existing["quote_date"],
                "source": existing["fx_source"] or "ECB via Frankfurter v2",
                "_rate_decimal": rate,
                "cny_cents": convert_cents(amount_cents, rate),
            }
        elif (
            existing is not None
            and existing["currency"] == currency
            and existing["date"] == date_str
            and existing["fx_status"] == "pending"
        ):
            quote = {"status": "pending", "reason": existing["fx_reason"] or "等待汇率", "cny_cents": None}
        else:
            quote = self._rate_for_date(date_str)
            if quote["status"] == "resolved":
                quote["cny_cents"] = convert_cents(amount_cents, quote["_rate_decimal"])
            else:
                quote["cny_cents"] = None

        if self._duplicate_entry_exists(
            date_str, kind, currency, amount_cents, name, entry_id
        ) and payload.get("confirm_duplicate") is not True:
            raise LedgerError("duplicate_suspected", "发现相同收支；如确实是两笔记录，请 confirm_duplicate=true")

        now = _now()
        if existing is None:
            cur = self.db.execute(
                """INSERT INTO entries(date,kind,currency,amount_cents,name,category_id,note,photo_id,
                   fx_status,rate,quote_date,fx_source,fx_reason,cny_cents,deleted,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
                (
                    date_str,
                    kind,
                    currency,
                    amount_cents,
                    name,
                    category_id,
                    note,
                    photo_id,
                    quote["status"],
                    quote.get("rate"),
                    quote.get("quote_date"),
                    quote.get("source"),
                    quote.get("reason"),
                    quote.get("cny_cents"),
                    now,
                    now,
                ),
            )
            entry_id = int(cur.lastrowid)
        else:
            self.db.execute(
                """UPDATE entries SET date=?,kind=?,currency=?,amount_cents=?,name=?,category_id=?,note=?,photo_id=?,
                   fx_status=?,rate=?,quote_date=?,fx_source=?,fx_reason=?,cny_cents=?,updated_at=? WHERE id=?""",
                (
                    date_str,
                    kind,
                    currency,
                    amount_cents,
                    name,
                    category_id,
                    note,
                    photo_id,
                    quote["status"],
                    quote.get("rate"),
                    quote.get("quote_date"),
                    quote.get("source"),
                    quote.get("reason"),
                    quote.get("cny_cents"),
                    now,
                    entry_id,
                ),
            )
        # Keep the first attachment in the legacy column. The ordered list is
        # in existing metadata so old schema-validated backups remain usable.
        # Both writes commit (or roll back) with the request result.
        self.db.execute(
            "INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"{_ENTRY_PHOTOS_PREFIX}{entry_id}", _json_dumps(photo_ids)),
        )
        row = self.db.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        return {"entry": self._entry_dict(row)}

    def _normalize_photo_ids(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            raise LedgerError("photo_invalid", "photo_ids 必须是照片标识数组")
        if len(value) > _MAX_ENTRY_PHOTOS:
            raise LedgerError("photo_invalid", f"每笔记录最多关联 {_MAX_ENTRY_PHOTOS} 张照片")
        photo_ids: list[str] = []
        for item in value:
            photo_id = self._validate_photo_id(item)
            if photo_id is None:
                raise LedgerError("photo_invalid", "照片标识数组不能包含空值")
            if photo_id not in photo_ids:
                photo_ids.append(photo_id)
        return photo_ids

    def _entry_photo_ids(self, row: sqlite3.Row) -> list[str]:
        stored = self.db.execute(
            "SELECT value FROM meta WHERE key=?", (f"{_ENTRY_PHOTOS_PREFIX}{row['id']}",)
        ).fetchone()
        try:
            if stored is None:
                photo_id = self._validate_photo_id(row["photo_id"])
                return [photo_id] if photo_id is not None else []
            return self._normalize_photo_ids(json.loads(stored["value"]))
        except (LedgerError, TypeError, json.JSONDecodeError) as exc:
            raise LedgerError("storage_error", "记录的照片关联无法读取") from exc

    def _entry_photos_from_payload(self, payload: dict[str, Any], existing: sqlite3.Row | None) -> list[str]:
        previous = self._entry_photo_ids(existing) if existing is not None else []
        if "photo_ids" in payload:
            photo_ids = self._normalize_photo_ids(payload["photo_ids"])
            if "photo_id" in payload:
                legacy_id = self._validate_photo_id(payload["photo_id"])
                if legacy_id != (photo_ids[0] if photo_ids else None):
                    raise LedgerError("photo_invalid", "photo_id 与 photo_ids 的首张照片不一致")
        elif "photo_id" in payload:
            photo_id = self._validate_photo_id(payload["photo_id"])
            photo_ids = [photo_id] if photo_id is not None else []
        else:
            return previous

        # Revalidate newly attached local files through the same bounded,
        # path-safe reader used to serve them. Existing associations remain
        # editable when an attachment is temporarily unavailable.
        from .attachments import read_photo

        for photo_id in photo_ids:
            if photo_id not in previous:
                try:
                    read_photo(self.root, photo_id)
                except (ValueError, OSError) as exc:
                    raise LedgerError("photo_invalid", "新增关联的照片不存在、不可读取或内容无效") from exc
        return photo_ids

    def _entry_category_id(self, payload: dict[str, Any], existing: sqlite3.Row | None) -> int:
        has_category = "category_id" in payload or "category_name" in payload
        if not has_category and existing is not None:
            return int(existing["category_id"])
        if "category_id" in payload and payload.get("category_id") is not None:
            category_id = _parse_id(payload.get("category_id"), "category_id")
        elif isinstance(payload.get("category_name"), str):
            key = _safe_text(payload["category_name"], "类别名称", limit=100).casefold()
            rows = self.db.execute("SELECT * FROM categories WHERE name_key=?", (key,)).fetchall()
            if len(rows) != 1:
                raise LedgerError("category_required", "类别缺失或已停用，请明确选择类别")
            category_id = int(rows[0]["id"])
        else:
            raise LedgerError("category_required", "请明确选择类别")
        row = self.db.execute("SELECT id,active FROM categories WHERE id=?", (category_id,)).fetchone()
        if row is None:
            raise LedgerError("category_required", "所选类别不存在")
        if self._category_is_deleted(category_id):
            if existing is not None and int(existing["category_id"]) == category_id:
                return category_id
            raise LedgerError("category_deleted", "新记录不能使用已删除类别")
        if not int(row["active"]):
            if existing is not None and int(existing["category_id"]) == category_id:
                return category_id
            raise LedgerError("category_inactive", "新记录不能使用已停用类别")
        return category_id

    def _duplicate_entry_exists(
        self,
        date_str: str,
        kind: str,
        currency: str,
        amount_cents: int,
        name: str,
        exclude_id: int | None,
    ) -> bool:
        sql = """SELECT 1 FROM entries WHERE deleted=0 AND date=? AND kind=? AND currency=?
                 AND amount_cents=? AND name=?"""
        args: list[Any] = [date_str, kind, currency, amount_cents, name]
        if exclude_id is not None:
            sql += " AND id<>?"
            args.append(exclude_id)
        return self.db.execute(sql + " LIMIT 1", args).fetchone() is not None

    def _entry_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        entry_id = _parse_id(payload.get("id"))
        row = self.db.execute("SELECT deleted FROM entries WHERE id=?", (entry_id,)).fetchone()
        if row is None:
            raise LedgerError("not_found", "记录不存在")
        if not int(row["deleted"]):
            self.db.execute("UPDATE entries SET deleted=1,updated_at=? WHERE id=?", (_now(), entry_id))
        return {"deleted": True}

    # ----- snapshots, wallets, exchanges -------------------------------

    def _wallet_initialized(self, currency: str) -> bool:
        return self.db.execute("SELECT 1 FROM snapshots WHERE currency=? LIMIT 1", (currency,)).fetchone() is not None

    def _snapshot_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        currency = _parse_currency(payload.get("currency"))
        balance = _parse_money(payload.get("balance"), signed=True, field="余额")
        cutoff = _parse_date(payload.get("cutoff"), "截止日期")
        note = _safe_text(payload.get("note", ""), "备注", limit=5000)
        cur = self.db.execute(
            "INSERT INTO snapshots(currency,balance_cents,cutoff,note,created_at) VALUES (?,?,?,?,?)",
            (currency, balance, cutoff, note, _now()),
        )
        snapshot_id = int(cur.lastrowid)
        row = self.db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return {"snapshot": self._snapshot_dict(row)}

    def _exchange_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        existing = None
        exchange_id = None
        if "id" in payload and payload.get("id") is not None:
            exchange_id = _parse_id(payload.get("id"))
            existing = self.db.execute("SELECT * FROM exchanges WHERE id=?", (exchange_id,)).fetchone()
            if existing is None or int(existing["deleted"]):
                raise LedgerError("not_found", "换汇记录不存在")
        date_str = _parse_date(payload.get("date", existing["date"] if existing else None))
        from_currency = _parse_currency(payload.get("from_currency", existing["from_currency"] if existing else None), "转出币种")
        to_currency = _parse_currency(payload.get("to_currency", existing["to_currency"] if existing else None), "到账币种")
        if from_currency == to_currency:
            raise LedgerError("invalid_exchange", "换汇两侧必须是不同币种")
        from_amount = _parse_money(
            payload.get("from_amount", format_cents(existing["from_amount_cents"]) if existing else None),
            field="转出金额",
        )
        to_amount = _parse_money(
            payload.get("to_amount", format_cents(existing["to_amount_cents"]) if existing else None),
            field="到账金额",
        )
        note = _safe_text(payload.get("note", existing["note"] if existing else ""), "备注", limit=5000)
        if not self._wallet_initialized(from_currency) or not self._wallet_initialized(to_currency):
            raise LedgerError("wallet_uninitialized", "请先初始化换汇涉及的两个钱包")
        if self._duplicate_exchange_exists(
            date_str, from_currency, from_amount, to_currency, to_amount, note, exchange_id
        ) and payload.get("confirm_duplicate") is not True:
            raise LedgerError("duplicate_suspected", "发现相同换汇；如确实是两笔记录，请 confirm_duplicate=true")
        now = _now()
        if existing is None:
            cur = self.db.execute(
                """INSERT INTO exchanges(date,from_currency,from_amount_cents,to_currency,to_amount_cents,note,deleted,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,0,?,?)""",
                (date_str, from_currency, from_amount, to_currency, to_amount, note, now, now),
            )
            exchange_id = int(cur.lastrowid)
        else:
            self.db.execute(
                """UPDATE exchanges SET date=?,from_currency=?,from_amount_cents=?,to_currency=?,to_amount_cents=?,note=?,updated_at=?
                   WHERE id=?""",
                (date_str, from_currency, from_amount, to_currency, to_amount, note, now, exchange_id),
            )
        row = self.db.execute("SELECT * FROM exchanges WHERE id=?", (exchange_id,)).fetchone()
        return {"exchange": self._exchange_dict(row)}

    def _duplicate_exchange_exists(
        self,
        date_str: str,
        from_currency: str,
        from_amount: int,
        to_currency: str,
        to_amount: int,
        note: str,
        exclude_id: int | None,
    ) -> bool:
        sql = """SELECT 1 FROM exchanges WHERE deleted=0 AND date=? AND from_currency=? AND from_amount_cents=?
                 AND to_currency=? AND to_amount_cents=? AND note=?"""
        args: list[Any] = [date_str, from_currency, from_amount, to_currency, to_amount, note]
        if exclude_id is not None:
            sql += " AND id<>?"
            args.append(exclude_id)
        return self.db.execute(sql + " LIMIT 1", args).fetchone() is not None

    def _exchange_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        exchange_id = _parse_id(payload.get("id"))
        row = self.db.execute("SELECT deleted FROM exchanges WHERE id=?", (exchange_id,)).fetchone()
        if row is None:
            raise LedgerError("not_found", "换汇记录不存在")
        if not int(row["deleted"]):
            self.db.execute("UPDATE exchanges SET deleted=1,updated_at=? WHERE id=?", (_now(), exchange_id))
        return {"deleted": True}

    def _internal_transfer_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        existing = None
        transfer_id = None
        if "id" in payload and payload.get("id") is not None:
            transfer_id = _parse_id(payload.get("id"))
            existing = self.db.execute(
                "SELECT * FROM internal_transfers WHERE id=?", (transfer_id,)
            ).fetchone()
            if existing is None or int(existing["deleted"]):
                raise LedgerError("not_found", "内部转账记录不存在")
        date_str = _parse_date(payload.get("date", existing["date"] if existing else None))
        currency = _parse_currency(payload.get("currency", existing["currency"] if existing else None))
        old_amount = format_cents(existing["amount_cents"]) if existing else None
        amount_cents = _parse_money(payload.get("amount", old_amount), field="金额")
        from_account = _safe_text(
            payload.get("from_account", existing["from_account"] if existing else None),
            "转出账户",
            allow_empty=False,
            limit=200,
        )
        to_account = _safe_text(
            payload.get("to_account", existing["to_account"] if existing else None),
            "转入账户",
            allow_empty=False,
            limit=200,
        )
        if from_account.casefold() == to_account.casefold():
            raise LedgerError("invalid_transfer", "转出账户与转入账户不能相同")
        note = _safe_text(payload.get("note", existing["note"] if existing else ""), "备注", limit=5000)
        sql = """SELECT 1 FROM internal_transfers WHERE deleted=0 AND date=? AND currency=?
                 AND amount_cents=? AND from_account=? AND to_account=?"""
        args: list[Any] = [date_str, currency, amount_cents, from_account, to_account]
        if transfer_id is not None:
            sql += " AND id<>?"
            args.append(transfer_id)
        duplicate = self.db.execute(sql + " LIMIT 1", args).fetchone() is not None
        if duplicate and payload.get("confirm_duplicate") is not True:
            raise LedgerError("duplicate_suspected", "发现相同内部转账；如确实是两笔记录，请 confirm_duplicate=true")
        now = _now()
        if existing is None:
            cur = self.db.execute(
                """INSERT INTO internal_transfers(
                       date,currency,amount_cents,from_account,to_account,note,deleted,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,0,?,?)""",
                (date_str, currency, amount_cents, from_account, to_account, note, now, now),
            )
            transfer_id = int(cur.lastrowid)
        else:
            self.db.execute(
                """UPDATE internal_transfers SET date=?,currency=?,amount_cents=?,from_account=?,
                   to_account=?,note=?,updated_at=? WHERE id=?""",
                (date_str, currency, amount_cents, from_account, to_account, note, now, transfer_id),
            )
        row = self.db.execute("SELECT * FROM internal_transfers WHERE id=?", (transfer_id,)).fetchone()
        return {"internal_transfer": self._internal_transfer_dict(row)}

    def _internal_transfer_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        transfer_id = _parse_id(payload.get("id"))
        row = self.db.execute("SELECT deleted FROM internal_transfers WHERE id=?", (transfer_id,)).fetchone()
        if row is None:
            raise LedgerError("not_found", "内部转账记录不存在")
        if not int(row["deleted"]):
            self.db.execute(
                "UPDATE internal_transfers SET deleted=1,updated_at=? WHERE id=?",
                (_now(), transfer_id),
            )
        return {"deleted": True}

    # ----- categories and attachments ----------------------------------

    def _category_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        category_id = None
        existing = None
        if "id" in payload and payload.get("id") is not None:
            category_id = _parse_id(payload.get("id"))
            existing = self.db.execute("SELECT * FROM categories WHERE id=?", (category_id,)).fetchone()
            if existing is None:
                raise LedgerError("not_found", "类别不存在")
            if self._category_is_deleted(category_id):
                raise LedgerError("category_deleted", "已删除类别不能修改或重新启用，请新建类别")
        name = _safe_text(payload.get("name", existing["name"] if existing else None), "类别名称", allow_empty=False, limit=100)
        active = payload.get("active", bool(existing["active"]) if existing else True)
        if not isinstance(active, bool):
            raise LedgerError("invalid_input", "active必须是布尔值")
        key = name.casefold()
        duplicate = self.db.execute("SELECT id FROM categories WHERE name_key=?", (key,)).fetchone()
        if duplicate is not None and (category_id is None or int(duplicate["id"]) != category_id):
            raise LedgerError("category_exists", "已有同名类别")
        if existing is None:
            cur = self.db.execute("INSERT INTO categories(name,name_key,active) VALUES (?,?,?)", (name, key, int(active)))
            category_id = int(cur.lastrowid)
        else:
            self.db.execute("UPDATE categories SET name=?,name_key=?,active=? WHERE id=?", (name, key, int(active), category_id))
        row = self.db.execute("SELECT id,name,active FROM categories WHERE id=?", (category_id,)).fetchone()
        return {"category": {"id": int(row["id"]), "name": row["name"], "active": bool(row["active"])}}

    def _category_is_deleted(self, category_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM meta WHERE key=?", (f"{_CATEGORY_DELETED_PREFIX}{category_id}",)
        ).fetchone() is not None

    def _category_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        category_id = _parse_id(payload.get("id"))
        row = self.db.execute("SELECT id FROM categories WHERE id=?", (category_id,)).fetchone()
        if row is None:
            raise LedgerError("not_found", "类别不存在")
        if not self._category_is_deleted(category_id):
            # Keep the row and its display name for historical joins. A NUL
            # cannot occur in a validated category name, so this internal key
            # safely releases the original name for a new category.
            self.db.execute(
                "UPDATE categories SET active=0,name_key=? WHERE id=?",
                (f"\x00deleted-category:{category_id}", category_id),
            )
            self.db.execute(
                "INSERT INTO meta(key,value) VALUES (?,?)",
                (f"{_CATEGORY_DELETED_PREFIX}{category_id}", _now()),
            )
        return {"deleted": True}

    def _photo_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        encoded = payload.get("content_base64")
        if not isinstance(encoded, str) or not encoded or len(encoded) > 12 * 1024 * 1024:
            raise LedgerError("photo_invalid", "照片数据无效或超过大小限制")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise LedgerError("photo_invalid", "照片数据不是有效 Base64") from exc
        if len(data) > 8 * 1024 * 1024:
            raise LedgerError("photo_invalid", "照片不能超过 8 MiB")
        try:
            module = importlib.import_module(".attachments", __package__)
            save_photo = getattr(module, "save_photo")
            photo_id = save_photo(self.root, data)
        except LedgerError:
            raise
        except Exception as exc:
            raise LedgerError("photo_invalid", "照片格式或内容不被支持") from exc
        return {"photo_id": self._validate_photo_id(photo_id)}

    def _validate_photo_id(self, value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not _PHOTO_RE.fullmatch(value):
            raise LedgerError("photo_invalid", "照片标识无效")
        return value

    # ----- backup/export -----------------------------------------------

    def prepare_daily_backup(self) -> None:
        """Check the daily backup at startup and before every business write."""
        with self.lock:
            try:
                self.backups.ensure_daily()
            except LedgerError:
                raise
            except Exception as exc:
                raise LedgerError(
                    "backup_failed", "写入前备份失败，账本未改变：" + backup_failure_reason(exc)
                ) from exc

    def _safe_archive_name(self, value: Any) -> str:
        if not isinstance(value, str) or not value or Path(value).name != value or ".." in value or len(value) > 200:
            raise LedgerError("invalid_backup", "备份名称无效")
        return value

    def _backup_create(self) -> str:
        try:
            return str(self.backups.create(kind="manual"))
        except LedgerError:
            raise
        except Exception as exc:
            raise LedgerError("backup_failed", "创建备份失败：" + backup_failure_reason(exc)) from exc

    def _export_csv(self, payload: dict[str, Any]) -> dict[str, Any]:
        # Use the same server-side filtered view as state, then write the
        # generated file atomically under the private exports directory.
        filters = payload.get("filters", payload)
        if not isinstance(filters, dict):
            raise LedgerError("invalid_filters", "导出筛选条件必须是对象")
        state = self._state_locked(filters)
        name = f"ledger-{uuid.uuid4().hex}.csv"
        target = self.root / "exports" / name
        temp = self.root / "exports" / f".{name}.{uuid.uuid4().hex}.tmp"
        headers = [
            "id",
            "date",
            "kind",
            "currency",
            "amount",
            "name",
            "category",
            "note",
            "fx_status",
            "rate",
            "quote_date",
            "cny_amount",
        ]
        try:
            with temp.open("w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh, lineterminator="\n")
                writer.writerow(headers)
                for entry in state["entries"]:
                    writer.writerow(
                        [
                            entry["id"],
                            entry["date"],
                            entry["kind"],
                            entry["currency"],
                            self._csv_safe(entry["amount"]),
                            self._csv_safe(entry["name"]),
                            self._csv_safe(entry["category_name"]),
                            self._csv_safe(entry["note"]),
                            entry["fx_status"],
                            entry["rate"] or "",
                            entry["quote_date"] or "",
                            self._csv_safe(entry["cny_amount"] or ""),
                        ]
                    )
                for transfer in state["internal_transfers"]:
                    writer.writerow(
                        [
                            transfer["id"],
                            transfer["date"],
                            "internal_transfer",
                            transfer["currency"],
                            self._csv_safe(transfer["amount"]),
                            self._csv_safe(f'{transfer["from_account"]} → {transfer["to_account"]}'),
                            "",
                            self._csv_safe(transfer["note"]),
                            "",
                            "",
                            "",
                            "",
                        ]
                    )
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp, target)
        except OSError as exc:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise LedgerError("export_failed", "CSV导出失败") from exc
        return {"name": name}

    @staticmethod
    def _csv_safe(value: Any) -> Any:
        if value is None:
            return ""
        text = str(value)
        if text.startswith(("=", "+", "-", "@")):
            return "'" + text
        return text

    # ----- state/reporting ---------------------------------------------

    def _state_locked(self, filters: dict[str, Any]) -> dict[str, Any]:
        month = _parse_month(filters.get("month", _month_now()))
        date_from, date_to = self._list_date_bounds(filters, month)
        amount_cents = _parse_filter_amount(filters.get("amount"))
        currency = None
        conditions = ["e.deleted=0"]
        params: list[Any] = []
        if date_from is not None:
            conditions.append("e.date>=?")
            params.append(date_from)
        if date_to is not None:
            conditions.append("e.date<=?")
            params.append(date_to)
        if "category_id" in filters and filters.get("category_id") not in (None, ""):
            category_id = _parse_id(filters.get("category_id"), "category_id")
            conditions.append("e.category_id=?")
            params.append(category_id)
        if "currency" in filters and filters.get("currency") not in (None, ""):
            currency = _parse_currency(filters.get("currency"))
            conditions.append("e.currency=?")
            params.append(currency)
        if amount_cents is not None:
            conditions.append("e.amount_cents=?")
            params.append(amount_cents)
        if "q" in filters and filters.get("q") not in (None, ""):
            q = _safe_text(filters.get("q"), "搜索词", limit=200)
            conditions.append("(e.name LIKE ? OR e.note LIKE ? OR c.name LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        where = " AND ".join(conditions)
        rows = self.db.execute(
            f"""SELECT e.*, c.name AS category_name FROM entries e JOIN categories c ON c.id=e.category_id
                WHERE {where} ORDER BY e.date DESC, e.id DESC""",
            params,
        ).fetchall()

        ex_conditions = ["deleted=0"]
        ex_params: list[Any] = []
        if date_from is not None:
            ex_conditions.append("date>=?")
            ex_params.append(date_from)
        if date_to is not None:
            ex_conditions.append("date<=?")
            ex_params.append(date_to)
        if amount_cents is not None:
            if currency is not None:
                ex_conditions.append(
                    "((from_currency=? AND from_amount_cents=?) OR (to_currency=? AND to_amount_cents=?))"
                )
                ex_params.extend([currency, amount_cents, currency, amount_cents])
            else:
                ex_conditions.append("(from_amount_cents=? OR to_amount_cents=?)")
                ex_params.extend([amount_cents, amount_cents])
        exchanges = self.db.execute(
            f"SELECT * FROM exchanges WHERE {' AND '.join(ex_conditions)} ORDER BY date DESC,id DESC", ex_params
        ).fetchall()
        transfer_conditions = ["deleted=0"]
        transfer_params: list[Any] = []
        if date_from is not None:
            transfer_conditions.append("date>=?")
            transfer_params.append(date_from)
        if date_to is not None:
            transfer_conditions.append("date<=?")
            transfer_params.append(date_to)
        if "category_id" in filters and filters.get("category_id") not in (None, ""):
            transfer_conditions.append("0=1")
        if "currency" in filters and filters.get("currency") not in (None, ""):
            transfer_conditions.append("currency=?")
            transfer_params.append(currency)
        if amount_cents is not None:
            transfer_conditions.append("amount_cents=?")
            transfer_params.append(amount_cents)
        if "q" in filters and filters.get("q") not in (None, ""):
            transfer_q = _safe_text(filters.get("q"), "搜索词", limit=200)
            transfer_conditions.append("(from_account LIKE ? OR to_account LIKE ? OR note LIKE ?)")
            transfer_like = f"%{transfer_q}%"
            transfer_params.extend([transfer_like, transfer_like, transfer_like])
        internal_transfers = self.db.execute(
            f"SELECT * FROM internal_transfers WHERE {' AND '.join(transfer_conditions)} ORDER BY date DESC,id DESC",
            transfer_params,
        ).fetchall()
        snapshots = self.db.execute("SELECT * FROM snapshots ORDER BY cutoff DESC,id DESC").fetchall()
        categories = self.db.execute(
            """SELECT c.id,c.name,c.active FROM categories c
               WHERE NOT EXISTS (SELECT 1 FROM meta m WHERE m.key=? || c.id)
               ORDER BY c.id""",
            (_CATEGORY_DELETED_PREFIX,),
        ).fetchall()
        historical_categories = self.db.execute(
            """SELECT c.id,c.name FROM categories c
               WHERE EXISTS (SELECT 1 FROM meta m WHERE m.key=? || c.id)
                 AND EXISTS (SELECT 1 FROM entries e WHERE e.category_id=c.id AND e.deleted=0)
               ORDER BY c.id""",
            (_CATEGORY_DELETED_PREFIX,),
        ).fetchall()
        wallets = [self._wallet_dict(currency) for currency in ("CNY", "SGD")]
        summary = self._summary(month)
        return {
            "wallets": wallets,
            "categories": [{"id": int(row["id"]), "name": row["name"], "active": bool(row["active"])} for row in categories],
            "historical_categories": [
                {"id": int(row["id"]), "name": row["name"], "active": False, "deleted": True}
                for row in historical_categories
            ],
            "entries": [self._entry_dict(row) for row in rows],
            "exchanges": [self._exchange_dict(row) for row in exchanges],
            "internal_transfers": [self._internal_transfer_dict(row) for row in internal_transfers],
            "snapshots": [self._snapshot_dict(row) for row in snapshots],
            "today_rate": self._today_rate_dict(),
            "summary": summary,
        }

    def _today_rate_dict(self) -> dict[str, Any]:
        """Return the latest cached official quote available as of today."""

        today = _today().isoformat()
        row = self.db.execute(
            """SELECT rate,quote_date,source FROM fx_cache
               WHERE status='resolved' AND quote_date<=?
               ORDER BY quote_date DESC,updated_at DESC LIMIT 1""",
            (today,),
        ).fetchone()
        if row is None:
            return {"status": "pending", "as_of_date": today}
        try:
            rate = rate_text(parse_rate(row["rate"]))
        except MoneyError:
            return {"status": "pending", "as_of_date": today}
        return {
            "status": "resolved",
            "rate": rate,
            "quote_date": row["quote_date"],
            "source": row["source"] or "ECB via Frankfurter v2",
            "as_of_date": today,
        }

    def _list_date_bounds(self, filters: dict[str, Any], month: str) -> tuple[str | None, str | None]:
        has_from = filters.get("date_from") not in (None, "")
        has_to = filters.get("date_to") not in (None, "")
        if has_from:
            date_from = _parse_date(filters["date_from"], "起始日期")
        else:
            date_from = None
        if has_to:
            date_to = _parse_date(filters["date_to"], "结束日期")
        else:
            date_to = None
        if not has_from and not has_to:
            return _month_bounds(month)
        if date_from and date_to and date_from > date_to:
            raise LedgerError("invalid_date_range", "起始日期不能晚于结束日期")
        return date_from, date_to

    def _summary(self, month: str) -> dict[str, Any]:
        first, last = _month_bounds(month)
        rows = self.db.execute(
            """SELECT e.*, c.name AS category_name FROM entries e JOIN categories c ON c.id=e.category_id
               WHERE e.deleted=0 AND e.date>=? AND e.date<=? ORDER BY e.id""",
            (first, last),
        ).fetchall()
        expense = 0
        income = 0
        original = {
            "expense": {"CNY": 0, "SGD": 0},
            "income": {"CNY": 0, "SGD": 0},
        }
        pending = 0
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            kind = str(row["kind"])
            currency = str(row["currency"])
            original[kind][currency] += int(row["amount_cents"])
            if row["fx_status"] == "pending":
                pending += 1
            cents = row["cny_cents"]
            if cents is None:
                cents = 0
            if kind == "expense":
                expense += int(cents)
                bucket = grouped.setdefault(
                    str(row["category_name"]),
                    {"amount": 0, "pending_count": 0, "CNY": 0, "SGD": 0},
                )
                bucket["amount"] += int(cents)
                bucket[currency] += int(row["amount_cents"])
                if row["fx_status"] == "pending":
                    bucket["pending_count"] += 1
            else:
                income += int(cents)
        category_summary = [
            {
                "name": name,
                "amount": format_cents(values["amount"]),
                "amounts": {"CNY": format_cents(values["CNY"]), "SGD": format_cents(values["SGD"])},
                "pending_count": values["pending_count"],
            }
            for name, values in sorted(grouped.items(), key=lambda item: item[0])
        ]
        return {
            "month": month,
            "expense_cny": format_cents(expense),
            "income_cny": format_cents(income),
            "expense_by_currency": {
                "CNY": format_cents(original["expense"]["CNY"]),
                "SGD": format_cents(original["expense"]["SGD"]),
            },
            "income_by_currency": {
                "CNY": format_cents(original["income"]["CNY"]),
                "SGD": format_cents(original["income"]["SGD"]),
            },
            "pending_count": pending,
            "categories": category_summary,
        }

    def _wallet_dict(self, currency: str) -> dict[str, Any]:
        snapshot = self.db.execute(
            "SELECT * FROM snapshots WHERE currency=? ORDER BY cutoff DESC,id DESC LIMIT 1", (currency,)
        ).fetchone()
        if snapshot is None:
            return {"currency": currency, "initialized": False, "balance": None, "cutoff": None, "negative": False}
        cutoff = snapshot["cutoff"]
        balance = int(snapshot["balance_cents"])
        row = self.db.execute(
            """SELECT COALESCE(SUM(CASE WHEN kind='income' THEN amount_cents ELSE -amount_cents END),0)
               FROM entries WHERE deleted=0 AND currency=? AND date>?""",
            (currency, cutoff),
        ).fetchone()
        balance += int(row[0] or 0)
        exchanges = self.db.execute(
            """SELECT COALESCE(SUM(CASE WHEN from_currency=? THEN -from_amount_cents ELSE 0 END),0)
               + COALESCE(SUM(CASE WHEN to_currency=? THEN to_amount_cents ELSE 0 END),0)
               FROM exchanges WHERE deleted=0 AND date>?""",
            (currency, currency, cutoff),
        ).fetchone()
        balance += int(exchanges[0] or 0)
        return {
            "currency": currency,
            "initialized": True,
            "balance": format_cents(balance),
            "cutoff": cutoff,
            "negative": balance < 0,
        }

    # ----- row serializers ---------------------------------------------

    def _entry_dict(self, row: sqlite3.Row | None) -> dict[str, Any]:
        assert row is not None
        photo_ids = self._entry_photo_ids(row)
        return {
            "id": int(row["id"]),
            "date": row["date"],
            "kind": row["kind"],
            "currency": row["currency"],
            "amount": format_cents(row["amount_cents"]),
            "name": row["name"],
            "category_id": int(row["category_id"]),
            "category_name": row["category_name"] if "category_name" in row.keys() else self._category_name(int(row["category_id"])),
            "note": row["note"],
            "photo_id": photo_ids[0] if photo_ids else None,
            "photo_ids": photo_ids,
            "fx_status": row["fx_status"],
            "rate": row["rate"],
            "quote_date": row["quote_date"],
            "cny_amount": format_cents(row["cny_cents"]),
        }

    def _category_name(self, category_id: int) -> str | None:
        row = self.db.execute("SELECT name FROM categories WHERE id=?", (category_id,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _snapshot_dict(row: sqlite3.Row | None) -> dict[str, Any]:
        assert row is not None
        return {
            "id": int(row["id"]),
            "currency": row["currency"],
            "balance": format_cents(row["balance_cents"]),
            "cutoff": row["cutoff"],
            "note": row["note"],
        }

    @staticmethod
    def _exchange_dict(row: sqlite3.Row | None) -> dict[str, Any]:
        assert row is not None
        return {
            "id": int(row["id"]),
            "date": row["date"],
            "from_currency": row["from_currency"],
            "from_amount": format_cents(row["from_amount_cents"]),
            "to_currency": row["to_currency"],
            "to_amount": format_cents(row["to_amount_cents"]),
            "note": row["note"],
        }

    @staticmethod
    def _internal_transfer_dict(row: sqlite3.Row | None) -> dict[str, Any]:
        assert row is not None
        return {
            "id": int(row["id"]),
            "date": row["date"],
            "currency": row["currency"],
            "amount": format_cents(row["amount_cents"]),
            "from_account": row["from_account"],
            "to_account": row["to_account"],
            "note": row["note"],
        }
