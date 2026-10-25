# Implementation contract

Python stdlib only, package `ledger_app`. All amounts strings with 2 decimals at interfaces, integer cents in DB; Decimal rates. Dates ISO YYYY-MM-DD, local UTC+8. Private root defaults PROJECT/private; tests use temporary directories.

## Core
`from ledger_app.service import Ledger, LedgerError`
`Ledger(root: pathlib.Path, fx_resolver=None)` creates root DB `ledger.sqlite3`, directories; `fx_resolver(date_str)` returns rate record dict or pending (see below). `ledger.db` sqlite connection check_same_thread=False; `ledger.lock` RLock. All operations serialized in process. Only server owns service; CLI uses HTTP to avoid competing processes. LedgerError has `code`, `message`, optional `details` JSON-safe. `close()` closes connection.

`ledger.dispatch(action, payload, request_id)` returns dict; all business mutations use request id and transaction-idempotent result. Same id+different data conflict. Daily backup before mutation. Failed mutations rollback. Distinct identical expense/income/exchange requires payload `confirm_duplicate: true`. Request hashes do not contain request_id. `ledger.state(filters={})` returns:
```
{wallets:[{currency,initialized,balance,cutoff,negative}],categories:[{id,name,active}],
 historical_categories:[{id,name,active:false,deleted:true}],
 entries:[{id,date,kind,currency,amount,name,category_id,category_name,note,photo_id,photo_ids,fx_status,rate,quote_date,cny_amount}],
 exchanges:[{id,date,from_currency,from_amount,to_currency,to_amount,note}],
 internal_transfers:[{id,date,currency,amount,from_account,to_account,note}],
 snapshots:[{id,currency,balance,cutoff,note}],
 today_rate:{status,rate?,quote_date?,source?,as_of_date},
 summary:{month,expense_cny,income_cny,expense_by_currency:{CNY,SGD},income_by_currency:{CNY,SGD},pending_count,categories:[{name,amount,amounts:{CNY,SGD},pending_count}]}}
```
filters: month YYYY-MM (default current UTC+8 month), date_from/date_to, category_id, currency, q, amount. Optional amount is a leading-digit prefix over the original-currency amount formatted as 0.00 (6 matches 6.00, 6.80, 60.00, 650.00 but not 16.50; 6.00 matches only 6.00; leading zeros ignored), never the reference converted amount. Empty amount imposes no restriction. It combines with existing entry and internal-transfer filters; for exchanges it matches an actual transfer side, with currency and amount on the same side if currency is provided. Summary month is independent of narrower list filters, labeled clearly in UI. Entries list defaults selected month. categories returns active and inactive. Stable IDs positive integers. photo_id nullable safe filename. Snapshot cutoff whole date inclusive; latest date then ID anchors wallet. Future dates disallowed; no initial balances invented. Entries may be saved before a snapshot; wallet cards remain uninitialized until a snapshot exists. Exchange writes require initialized involved wallets.

Actions (payload keys):
- entry.save: optional id (edit), date, kind expense|income, currency CNY|SGD, amount, name, category_id (required or high-confidence name classification), note default '', photo_ids default []. photo_ids is an ordered array of at most 20 uploaded photo IDs; duplicate IDs are removed while preserving order. New photo associations must be readable validated local attachments. Edits preserve all prior photos if both photo fields are omitted; [] clears associations without deleting photo files. Legacy photo_id replaces the selection with one photo (null clears); if both fields appear, photo_id must match the first photo_ids item or null for an empty list. Responses always include photo_ids and a compatible photo_id (first or null). Ordered lists are stored under meta key entry.photos.<id> within the entry transaction; old single-photo entries fall back to entries.photo_id. SQLite schema and existing backups remain compatible. Missing/ambiguous category => category_required. Entries store immutable resolved FX quote; date change invalidates, amount-only edit reuses quote. Entries can be recorded before wallet snapshots; balances remain pending until initialized.
- entry.delete: id (soft delete).
- snapshot.save: currency, balance (may be signed), cutoff YYYY-MM-DD, note. Both initial and correction, retain audit.
- exchange.save: optional id, date, from_currency, from_amount, to_currency, to_amount, note. exchange.delete: id.
- internal_transfer.save: optional id, date, currency, amount, from_account, to_account, note. internal_transfer.delete: id. Internal transfers are audit-only and never alter wallet balances or income/expense summaries.
- category.save: optional id, name, active boolean default true. Historical usage persists; editing old entry with its unchanged inactive category permitted.
- category.delete: id -> {deleted:true}. Records a deletion marker in meta (category.deleted.<id>), sets active=false, and reserves an internal name_key while preserving the original display name and foreign-key row. No schema change. categories excludes deleted rows; historical_categories contains deleted categories referenced by nondeleted entries, independent of entry filters. Old entries may retain the same deleted category_id; new entries or reassignment cannot use it (category_deleted). Deleted categories cannot be renamed/reactivated; a same-name category may be created with a new ID. Existing backup, transaction and request-id protections apply.
- photo.save: content_base64 -> {photo_id}; server limits <= 12 MiB JSON, decoded photo <=8 MiB.
- fx.refresh: {} retry pending transaction dates and refresh today's dashboard SGD→CNY quote, then return {updated,pending}; cache resolved rates by consumption date, untouched historical resolved rows. No wallet effect. Daily backup before changes.
- backup.create: {} -> {name}; backup.restore: {name,confirm:true} -> {restored:true}; backup.list read via separate `ledger.backups.list()`.
- export.csv: filters same as state -> {name}; use sanitized formula-safe CSV in private/exports.

Successful normal mutations return {entry|exchange|snapshot|category|photo_id|...} or {deleted:true}; HTTP wrapper adds ok:true/result. All actions audited/idempotent except backup and export may use file-level result record after success. Repeated delete remains safe. Tables: categories, entries, exchanges, snapshots, requests, meta, fx_cache. PRAGMA user_version=2. No need independent entry_fx table; fields may live on entries. State reads never network.

## Storage contract
`attachments.save_photo(root:Path, data:bytes) -> str` generated uuid.ext in root/photos, validates JPEG/PNG/WebP signatures+bounded structure, excludes SVG/HTML, 8 MiB max. `attachments.read_photo(root, photo_id) -> (bytes,mime)` validates simple UUID filename and path containment/symlinks. Raise ValueError on invalid. No untrusted original filename stored.
`BackupManager(root:Path, db:sqlite3.Connection, lock:RLock)` methods `ensure_daily()` before first business write, `create(kind='manual')->str`, `list()->list[dict{name,created,kind,size}]`, `restore(name)->None`. Archives DB+photos manifest; schema check PRAGMA user_version=2 and core tables; reject unsafe zip members, symlinks, oversized archive, checksum mismatch. All photos (including orphans) may be included. restore uses live db connection, must backup current state before switch; rollback file+DB state on failure. Daily retention 30 auto only. Manager uses same RLock. Never own its own connection to live DB other than supplied one. UI restores only selected existing archive; no arbitrary upload.

## FX contract
`fx.resolve_rate(date_str)->dict`: {status:'resolved',rate:'5.271',quote_date:'2026-08-10',source:'ECB via Frankfurter v2'} or {status:'pending',reason:'...'} (no amount to resolver). Same schema for injected test resolver. Use target closing calendar weekends/Jan1/GoodFriday/EasterMonday/May1/Dec25/26. Expected actual quote date exactly latest calendar business day <= requested, not arbitrary stale data. Weekday lacking its exact expected quote stays pending. Exact current-day quotes may resolve; absent stays pending. Never future quote, strict source endpoint and currency/schema. `fx.convert(amount_minor,rate)->int` ROUND_HALF_UP.

## HTTP/UI contract
`web.make_server(ledger,port=8765)->ThreadingHTTPServer` bound only 127.0.0.1, port=0 supported tests. Stdlib.
- GET /api/session -> {ok:true,csrf:...}, creates HttpOnly SameSite=Strict signed/unguessable session cookie; validate local Host, Origin if provided and Fetch-Metadata for all reads including bootstrap. No CORS.
- GET /api/state?<filters> -> {ok:true,result:<state>}; GET /api/backups -> {ok:true,result:[...]}; require session for sensitive reads.
- POST /api/command JSON {action,payload,request_id} -> {ok:true,result:...}; exact same Origin, session and X-CSRF-Token mandatory. Errors {ok:false,error:{code,message,details}} with 400/409/403.
- GET /api/photos/<id> mapped attachment only, authenticated. GET /api/exports/<safe-name> authenticated downloads generated CSV only. Never general filesystem serving. Only /, /app.js, /styles.css, /fonts/InterVariable.woff2 static allowlist.
- Headers CSP self only (img blob/data if needed local previews), nosniff, no-referrer, frame deny, cache no-store. Do not log payloads or URLs. HTML/JS use textContent, not untrusted innerHTML.
- UI polished Chinese local personal app, calm cream/teal style, responsive sidebar/nav, dashboard, entry editing/filtering, wallets/snapshots, exchange edit/delete, categories, photos, backup/restore, CSV; all via API above. Error/toast shown, duplicate confirmation explicit, destructive delete/restore confirmation. No default real entries or balances. Monetary calculations on server only. UI form amount/name/category order with date/currency clear. Month defaults UTC+8.

## CLI contract
`python run_ledger.py` starts and opens default browser; `--no-browser`, `--port`, `--data-dir` supported. Reject duplicate active server/data lock. `python -m ledger_app.cli state ...` / `command --action ... --request-id ... --file <private JSON>` talks to running server, uses session+Origin. Never claim saved before committed response; logs don't contain credentials. Natural language handled by Codex with structured file into private/work, not cloud API. Full README and project AGENTS on safe conversational operation.
