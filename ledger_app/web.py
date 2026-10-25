"""Small, loopback-only HTTP adapter for the ledger service.

The web layer deliberately contains no accounting logic.  It authenticates the
browser session, validates the request envelope, and delegates state changes to
``Ledger.dispatch``.  Keeping that boundary narrow makes it possible to run
the HTTP tests with a tiny in-memory ledger while the SQLite service is being
developed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


MAX_COMMAND_BYTES = 12 * 1024 * 1024
MAX_REQUEST_ID = 160
SESSION_COOKIE = "ledger_session"
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}
_SESSION_TTL = 12 * 60 * 60
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,100}$")
_SESSION_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
_PHOTO_ID_RE = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})\.(?:jpg|jpeg|png|webp)$",
    re.IGNORECASE,
)
_EXPORT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\.csv$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_TAB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_MAX_TABS = 50
_MUTATION_CONFLICT_CODES = {
    "duplicate",
    "duplicate_confirmation_required",
    "duplicate_requires_confirmation",
    "request_conflict",
    "conflict",
    "already_exists",
    "restore_confirmation_required",
}


class _Server(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the one process-wide Ledger instance."""

    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _safe_error(code: str, message: str, details: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": str(code), "message": str(message)}
    if details is not None:
        # Details are part of the public contract.  Avoid allowing an arbitrary
        # exception object to escape into json or into a response log.
        try:
            json.dumps(details, ensure_ascii=False)
            error["details"] = details
        except (TypeError, ValueError):
            error["details"] = None
    return error


def _status_for_error(code: str) -> int:
    lowered = str(code).lower()
    if lowered in _MUTATION_CONFLICT_CODES or "duplicate" in lowered or "conflict" in lowered:
        return 409
    if lowered in {"forbidden", "unauthorized", "csrf", "session_required", "origin_rejected", "host_rejected", "fetch_metadata_rejected"}:
        return 403
    return 400


def _parse_host(value: str | None) -> tuple[str, int | None] | None:
    """Parse a Host header without accepting userinfo, paths, or IPv6 aliases."""

    if not value or len(value) > 255 or any(ch in value for ch in " /\\\t\r\n"):
        return None
    # urlsplit handles the optional port and rejects malformed bracketed hosts.
    try:
        parsed = urlsplit("//" + value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
        return None
    hostname = hostname.lower().rstrip(".")
    if hostname not in _LOCAL_HOSTS:
        return None
    return hostname, port


def _parse_origin(value: str | None) -> tuple[str, int | None] | None:
    if not value or len(value) > 255:
        return None
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme != "http" or not hostname or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
        return None
    hostname = hostname.lower().rstrip(".")
    if hostname not in _LOCAL_HOSTS:
        return None
    # An explicit slash is harmless for browser Origins but is not emitted by
    # browsers.  Treat it as an invalid origin to keep comparison exact.
    if parsed.path == "/":
        return None
    return hostname, port


def _cookie_value(header: str | None, name: str) -> str | None:
    if not header:
        return None
    for part in header.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == name:
            return value.strip()
    return None


def _root_for_ledger(ledger: Any) -> Path | None:
    for attribute in ("root", "private_root", "data_root"):
        value = getattr(ledger, attribute, None)
        if value:
            try:
                return Path(value)
            except (TypeError, ValueError):
                return None
    return None


def _within(child: Path, parent: Path) -> bool:
    try:
        child.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


class LedgerRequestHandler(BaseHTTPRequestHandler):
    """Authenticated GET/POST routes for the local ledger app."""

    protocol_version = "HTTP/1.1"
    # BaseHTTPRequestHandler logs request lines by default.  A request URL can
    # contain a user-entered search term, so the web app intentionally logs no
    # request payloads or URLs.
    def log_message(self, _format: str, *args: Any) -> None:  # pragma: no cover - policy guard
        return

    @property
    def server_state(self) -> _Server:
        return self.server  # type: ignore[return-value]

    def _common_headers(self) -> None:
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _send_bytes(self, status: int, body: bytes, content_type: str = "application/octet-stream", extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, value: Any, cookie: str | None = None) -> None:
        body = _json_bytes(value)
        self.send_response(status)
        self._common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def _failure(self, status: int, code: str, message: str, details: Any = None) -> None:
        self._send_json(status, {"ok": False, "error": _safe_error(code, message, details)})

    def _host_and_fetch_ok(self) -> bool:
        host_info = _parse_host(self.headers.get("Host"))
        if host_info is None:
            self._failure(403, "host_rejected", "仅允许通过本机地址访问。")
            return False
        _hostname, host_port = host_info
        if host_port is not None and host_port != int(self.server.server_port):
            self._failure(403, "host_rejected", "请求地址与当前本地服务不一致。")
            return False
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site in {"cross-site", "cross-origin"}:
            self._failure(403, "fetch_metadata_rejected", "已拒绝跨站请求。")
            return False
        origin = self.headers.get("Origin")
        if origin:
            origin_info = _parse_origin(origin)
            if origin_info is None:
                self._failure(403, "origin_rejected", "请求来源不是本机服务。")
                return False
            origin_host, origin_port = origin_info
            if origin_host != _hostname or origin_port != int(self.server.server_port):
                self._failure(403, "origin_rejected", "请求来源与本机页面不一致。")
                return False
        return True

    def _expected_origin(self) -> str | None:
        info = _parse_host(self.headers.get("Host"))
        if info is None:
            return None
        host, port = info
        actual_port = port or int(self.server.server_port)
        return f"http://{host}:{actual_port}"

    def _session_cookie(self, sid: str) -> str:
        signature = hmac.new(self.server_state.session_secret, sid.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{sid}.{signature}"

    def _new_session(self) -> tuple[str, dict[str, Any], str]:
        sid = secrets.token_urlsafe(32)
        session = {"csrf": secrets.token_urlsafe(32), "created": time.time(), "last_seen": time.time()}
        self.server_state.sessions[sid] = session
        cookie = f"{SESSION_COOKIE}={self._session_cookie(sid)}; Path=/; HttpOnly; SameSite=Strict"
        return sid, session, cookie

    def _current_session(self, create: bool = False) -> tuple[str, dict[str, Any], str | None] | None:
        raw = _cookie_value(self.headers.get("Cookie"), SESSION_COOKIE)
        if raw:
            sid, separator, signature = raw.partition(".")
            if not separator or not _SESSION_ID_RE.fullmatch(sid) or not _SESSION_SIGNATURE_RE.fullmatch(signature):
                sid = ""
                expected = ""
            else:
                expected = hmac.new(self.server_state.session_secret, sid.encode("ascii"), hashlib.sha256).hexdigest()
            session = self.server_state.sessions.get(sid)
            if (
                sid
                and hmac.compare_digest(signature, expected)
                and session
                and time.time() - float(session.get("created", 0)) <= _SESSION_TTL
            ):
                session["last_seen"] = time.time()
                return sid, session, None
        if create:
            return self._new_session()
        return None

    def _require_session(self) -> tuple[str, dict[str, Any]] | None:
        current = self._current_session(False)
        if current is None:
            self._failure(403, "session_required", "请先打开本机账本页面。")
            return None
        return current[0], current[1]

    def _require_command_auth(self) -> tuple[str, dict[str, Any]] | None:
        if not self._host_and_fetch_ok():
            return None
        origin = self.headers.get("Origin")
        expected = self._expected_origin()
        if not origin or not expected or origin != expected:
            self._failure(403, "origin_rejected", "写入请求必须来自当前本机页面。")
            return None
        current = self._require_session()
        if current is None:
            return None
        csrf = self.headers.get("X-CSRF-Token")
        if not csrf or not hmac.compare_digest(str(csrf), str(current[1].get("csrf", ""))):
            self._failure(403, "csrf", "写入凭据无效，请刷新页面后重试。")
            return None
        return current

    def _tracked(self, handler: Any) -> None:
        # In-flight requests keep an auto-exiting server alive until they finish.
        state = self.server_state
        with state.lock:
            state.inflight += 1
            state.last_activity = time.monotonic()
        try:
            handler()
        finally:
            with state.lock:
                state.inflight -= 1
                state.last_activity = time.monotonic()

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        # Launcher readiness probes are not ledger use.
        if urlsplit(self.path).path == "/api/health":
            self._handle_get()
            return
        self._tracked(self._handle_get)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        self._tracked(self._handle_post)

    def _handle_get(self) -> None:
        if not self._host_and_fetch_ok():
            return
        parsed = urlsplit(self.path)
        route = parsed.path
        if route == "/api/health":
            # Launcher readiness only: no ledger data or session credentials.
            self._send_json(200, {"ok": True, "app": "local-ledger",
                                  "instance_id": self.server_state.instance_id})
            return
        if route == "/api/session":
            current = self._current_session(True)
            assert current is not None
            sid, session, new_cookie = current
            result = {"ok": True, "csrf": session["csrf"], "auto_exit": bool(self.server_state.auto_exit)}
            self._send_json(200, result, f"{SESSION_COOKIE}={self._session_cookie(sid)}; Path=/; HttpOnly; SameSite=Strict" if new_cookie else None)
            return
        if route == "/api/state":
            if self._require_session() is None:
                return
            try:
                filters = self._filters(parsed.query)
                result = self.server_state.ledger.state(filters=filters)
                self._send_json(200, {"ok": True, "result": result})
            except Exception as exc:  # service errors are kept inside the JSON envelope
                self._send_ledger_exception(exc)
            return
        if route == "/api/backups":
            if self._require_session() is None:
                return
            try:
                manager = getattr(self.server_state.ledger, "backups", None)
                if manager is None:
                    manager = getattr(self.server_state.ledger, "backup_manager", None)
                result = manager.list() if manager is not None else []
                self._send_json(200, {"ok": True, "result": result})
            except Exception as exc:
                self._send_ledger_exception(exc)
            return
        if route.startswith("/api/photos/"):
            if self._require_session() is None:
                return
            self._serve_photo(unquote(route[len("/api/photos/"):]))
            return
        if route.startswith("/api/exports/"):
            if self._require_session() is None:
                return
            self._serve_export(unquote(route[len("/api/exports/"):]))
            return
        static = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/fonts/InterVariable.woff2": ("fonts/InterVariable.woff2", "font/woff2"),
        }
        if route in static:
            filename, content_type = static[route]
            path = Path(__file__).resolve().parent / "static" / filename
            try:
                body = path.read_bytes()
            except OSError:
                self._failure(500, "static_unavailable", "页面资源暂时不可用。")
                return
            self._send_bytes(200, body, content_type)
            return
        self._failure(404, "not_found", "请求地址不存在。")

    def _handle_post(self) -> None:
        route = self.path.split("?", 1)[0]
        if route == "/api/presence":
            self._handle_presence()
            return
        if route != "/api/command":
            # Authenticate even for a malformed write route so it cannot be
            # used as a cross-site probing endpoint.
            if self._require_command_auth() is None:
                return
            self._failure(404, "not_found", "请求地址不存在。")
            return
        if self._require_command_auth() is None:
            return
        body = self._read_json_body()
        if body is None:
            return
        action = body.get("action")
        payload = body.get("payload", {})
        request_id = body.get("request_id")
        if not isinstance(action, str) or not action or len(action) > 100 or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}", action):
            self._failure(400, "invalid_action", "缺少有效操作名称。")
            return
        if not isinstance(payload, dict):
            self._failure(400, "invalid_payload", "操作参数必须是对象。")
            return
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            self._failure(400, "invalid_request_id", "缺少有效请求编号。")
            return
        try:
            result = self.server_state.ledger.dispatch(action, payload, request_id)
            self._send_json(200, {"ok": True, "result": result})
        except Exception as exc:
            self._send_ledger_exception(exc)

    def _handle_presence(self) -> None:
        """Record that a ledger page is open (heartbeat) or was just closed."""
        if self._require_command_auth() is None:
            return
        body = self._read_json_body()
        if body is None:
            return
        tab_id = body.get("tab_id")
        closing = body.get("closing", False)
        if not isinstance(tab_id, str) or not _TAB_ID_RE.fullmatch(tab_id) or not isinstance(closing, bool):
            self._failure(400, "invalid_presence", "页面在线信息无效。")
            return
        state = self.server_state
        with state.lock:
            if closing:
                state.tabs.pop(tab_id, None)
            elif tab_id in state.tabs or len(state.tabs) < _MAX_TABS:
                state.tabs[tab_id] = time.monotonic()
        self._send_json(200, {"ok": True})

    def _read_json_body(self) -> dict[str, Any] | None:
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._failure(400, "invalid_content_type", "请求必须使用 JSON。")
            return None
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if length < 0:
            self._failure(400, "invalid_content_length", "缺少有效请求长度。")
            return None
        if length > MAX_COMMAND_BYTES:
            self.close_connection = True
            self._failure(400, "request_too_large", "请求内容超过 12 MiB 限制。")
            return None
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                self._failure(400, "invalid_body", "请求内容不完整。")
                return None
            value = json.loads(raw.decode("utf-8"), parse_constant=self._reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            self._failure(400, "invalid_json", "请求不是有效 JSON。")
            return None
        if not isinstance(value, dict):
            self._failure(400, "invalid_json", "请求 JSON 必须是对象。")
            return None
        return value

    @staticmethod
    def _reject_json_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant: {value}")

    def _filters(self, query: str) -> dict[str, Any]:
        values = parse_qs(query, keep_blank_values=True, strict_parsing=False)
        allowed = {"month", "date_from", "date_to", "category_id", "currency", "amount", "q"}
        unknown = set(values) - allowed
        if unknown:
            raise _RequestError("invalid_filter", "包含不支持的筛选条件。")
        result: dict[str, Any] = {}
        for key in allowed:
            items = values.get(key)
            if not items:
                continue
            if len(items) != 1:
                raise _RequestError("invalid_filter", "筛选条件不能重复。")
            value = items[0]
            if len(value) > 80:
                raise _RequestError("invalid_filter", "筛选条件过长。")
            if value != "":
                result[key] = value
        if "category_id" in result:
            try:
                category_id = int(result["category_id"])
            except (TypeError, ValueError):
                raise _RequestError("invalid_filter", "类别编号无效。") from None
            if category_id <= 0:
                raise _RequestError("invalid_filter", "类别编号无效。")
            result["category_id"] = category_id
        if "currency" in result and result["currency"] not in {"CNY", "SGD"}:
            raise _RequestError("invalid_filter", "币种无效。")
        return result

    def _send_ledger_exception(self, exc: Exception) -> None:
        if isinstance(exc, _RequestError):
            self._failure(400, exc.code, exc.message, exc.details)
            return
        code = getattr(exc, "code", "internal_error")
        message = getattr(exc, "message", None) or ("账本操作失败。" if code != "internal_error" else "服务暂时无法完成请求。")
        details = getattr(exc, "details", None)
        status = _status_for_error(str(code))
        # Do not expose traceback or arbitrary exception text.  LedgerError's
        # public message/details are intentionally the only service details.
        self._failure(status, str(code), str(message), details)

    def _serve_photo(self, photo_id: str) -> None:
        if not _PHOTO_ID_RE.fullmatch(photo_id):
            self._failure(404, "not_found", "附件不存在。")
            return
        root = _root_for_ledger(self.server_state.ledger)
        if root is None:
            self._failure(404, "not_found", "附件不存在。")
            return
        try:
            try:
                from .attachments import read_photo  # type: ignore
            except ImportError:
                read_photo = None
            if read_photo is not None:
                body, mime = read_photo(root, photo_id)
            else:
                reader = getattr(self.server_state.ledger, "read_photo", None)
                if reader is None:
                    raise ValueError("photo unavailable")
                body, mime = reader(photo_id)
            if not isinstance(body, (bytes, bytearray)):
                raise ValueError("photo invalid")
            content_type = str(mime or "application/octet-stream")
            if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                raise ValueError("photo mime invalid")
            self._send_bytes(200, bytes(body), content_type)
        except (OSError, ValueError, TypeError):
            self._failure(404, "not_found", "附件不存在。")

    def _serve_export(self, name: str) -> None:
        if not _EXPORT_RE.fullmatch(name) or ".." in name:
            self._failure(404, "not_found", "导出文件不存在。")
            return
        root = _root_for_ledger(self.server_state.ledger)
        if root is None:
            self._failure(404, "not_found", "导出文件不存在。")
            return
        path = root / "exports" / name
        if not _within(path, root / "exports") or path.is_symlink() or not path.is_file():
            self._failure(404, "not_found", "导出文件不存在。")
            return
        try:
            body = path.read_bytes()
        except OSError:
            self._failure(404, "not_found", "导出文件不存在。")
            return
        self._send_bytes(200, body, "text/csv; charset=utf-8", {"Content-Disposition": f'attachment; filename="{name}"'})


class _RequestError(Exception):
    def __init__(self, code: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def make_server(ledger: Any, port: int = 8765) -> ThreadingHTTPServer:
    """Create a loopback-only ledger HTTP server.

    The caller owns the returned server and should call ``serve_forever`` and
    later ``shutdown``/``server_close``.  ``port=0`` is intentionally passed
    through so tests can reserve an ephemeral port safely.
    """

    if not isinstance(port, int) or port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")
    server = _Server(("127.0.0.1", port), LedgerRequestHandler)
    server.instance_id = secrets.token_hex(16)
    server.ledger = ledger
    server.sessions: dict[str, dict[str, Any]] = {}
    server.session_secret = secrets.token_bytes(32)
    server.lock = threading.RLock()
    server.auto_exit = False
    server.tabs: dict[str, float] = {}
    server.inflight = 0
    server.last_activity = time.monotonic()
    return server


class IdleExit:
    """Decide when a shortcut-started server may stop because no page is open.

    Open pages heartbeat through /api/presence and say goodbye when closed.
    A page that vanishes without a goodbye expires after ``tab_timeout``.
    After the last page leaves, ``grace`` covers a reload; if no page ever
    arrives, ``startup_timeout`` applies.  A long gap between checks means
    the computer slept, so pages get a fresh chance to heartbeat.
    """

    def __init__(self, server: Any, *, clock: Any = time.monotonic, tab_timeout: float = 180,
                 grace: float = 15, startup_timeout: float = 300, sleep_gap: float = 60) -> None:
        self.server = server
        self.clock = clock
        self.tab_timeout = tab_timeout
        self.grace = grace
        self.startup_timeout = startup_timeout
        self.sleep_gap = sleep_gap
        self.last_check: float | None = None
        self.seen_tab = False

    def should_exit(self) -> bool:
        now = self.clock()
        state = self.server
        with state.lock:
            if self.last_check is not None and now - self.last_check > self.sleep_gap:
                for tab_id in state.tabs:
                    state.tabs[tab_id] = now
                state.last_activity = max(state.last_activity, now)
            self.last_check = now
            for tab_id, seen in list(state.tabs.items()):
                if now - seen > self.tab_timeout:
                    del state.tabs[tab_id]
            if state.tabs:
                self.seen_tab = True
                return False
            if state.inflight:
                return False
            wait = self.grace if self.seen_tab else self.startup_timeout
            return now - state.last_activity >= wait

    def run(self, sleep: Any = time.sleep, interval: float = 5) -> None:
        while not self.should_exit():
            sleep(interval)
        self.server.shutdown()


__all__ = ["IdleExit", "LedgerRequestHandler", "make_server"]
