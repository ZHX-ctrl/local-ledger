"""Contract tests for the loopback web adapter.

These tests use only fabricated in-memory data.  They intentionally do not
depend on the SQLite service so the HTTP boundary can be checked while the
core implementation evolves.
"""

from __future__ import annotations

import http.client
import json
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from typing import Any

from ledger_app.web import IdleExit, make_server


class _Backups:
    def list(self) -> list[dict[str, Any]]:
        return [{"name": "manual-test.zip", "created": "2026-09-12T00:00:00+08:00", "kind": "manual", "size": 123}]


class MockLedger:
    """Small Ledger-shaped fake for web tests and early local development."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.backups = _Backups()
        self.calls: list[tuple[str, dict[str, Any], str]] = []

    def state(self, filters: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "wallets": [
                {"currency": "CNY", "initialized": True, "balance": "100.00", "cutoff": "2026-08-31", "negative": False},
                {"currency": "SGD", "initialized": False, "balance": "0.00", "cutoff": None, "negative": False},
            ],
            "categories": [{"id": 1, "name": "三餐", "active": True}],
            "entries": [],
            "exchanges": [],
            "snapshots": [],
            "today_rate": {"status": "resolved", "rate": "5.271", "quote_date": "2026-09-18", "source": "synthetic", "as_of_date": "2026-09-20"},
            "summary": {"month": (filters or {}).get("month", "2026-09"), "expense_cny": "0.00", "income_cny": "0.00", "expense_by_currency": {"CNY": "0.00", "SGD": "0.00"}, "income_by_currency": {"CNY": "0.00", "SGD": "0.00"}, "pending_count": 0, "categories": []},
        }

    def dispatch(self, action: str, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
        self.calls.append((action, payload, request_id))
        return {"action": action, "accepted": True}


class WebContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = MockLedger(Path(self.temp.name))
        self.server = make_server(self.ledger, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = f"127.0.0.1:{self.server.server_port}"
        self.cookie = ""
        self.csrf = ""

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, method: str, path: str, body: Any = None, *, origin: str | None = None, cookie: str | None = None, csrf: str | None = None, site: str | None = "same-origin", content_type: str = "application/json") -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        headers = {"Host": self.host}
        if site is not None:
            headers["Sec-Fetch-Site"] = site
        if origin is not None:
            headers["Origin"] = origin
        if cookie is not None:
            headers["Cookie"] = cookie
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        raw = None
        if body is not None:
            raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(len(raw))
        connection.request(method, path, body=raw, headers=headers)
        response = connection.getresponse()
        result = (response.status, {key.lower(): value for key, value in response.getheaders()}, response.read())
        connection.close()
        return result

    def open_session(self) -> None:
        status, headers, body = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        value = json.loads(body)
        self.assertTrue(value["ok"])
        self.csrf = value["csrf"]
        cookie_header = headers["set-cookie"]
        self.cookie = cookie_header.split(";", 1)[0]

    def test_static_bootstrap_has_security_headers_and_allowlist(self) -> None:
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("双币账本", body.decode("utf-8"))
        self.assertIn("form-action 'self'", headers["content-security-policy"])
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        status, headers, body = self.request("GET", "/fonts/InterVariable.woff2")
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "font/woff2")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertGreater(len(body), 48)
        self.assertEqual(body[:4], b"wOF2")
        self.assertEqual(int.from_bytes(body[8:12], "big"), len(body))
        for route in (
            "/CONTRACT.md",
            "/%2e%2e/CONTRACT.md",
            "/fonts/",
            "/fonts/OFL.txt",
            "/fonts/other.woff2",
            "/fonts/../app.js",
            "/fonts/%2e%2e/app.js",
        ):
            with self.subTest(route=route):
                status, _headers, _body = self.request("GET", route)
                self.assertEqual(status, 404)

    def test_session_cookie_and_sensitive_read_require_authentication(self) -> None:
        status, _headers, _body = self.request("GET", "/api/state")
        self.assertEqual(status, 403)
        self.open_session()
        status, headers, body = self.request("GET", "/api/state", cookie=self.cookie)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_loopback_host_and_fetch_metadata_are_enforced(self) -> None:
        status, _headers, _body = self.request("GET", "/", site="cross-site")
        self.assertEqual(status, 403)
        status, _headers, _body = self.request("GET", "/fonts/InterVariable.woff2", site="cross-site")
        self.assertEqual(status, 403)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request("GET", "/", headers={"Host": "example.invalid", "Sec-Fetch-Site": "same-origin"})
        response = connection.getresponse()
        self.assertEqual(response.status, 403)
        response.read()
        connection.close()

    def test_command_requires_exact_origin_csrf_and_dispatches(self) -> None:
        self.open_session()
        command = {"action": "category.save", "payload": {"name": "测试", "active": True}, "request_id": "req-web-1"}
        status, _headers, _body = self.request("POST", "/api/command", command, cookie=self.cookie, csrf=self.csrf)
        self.assertEqual(status, 403)
        status, _headers, _body = self.request("POST", "/api/command", command, cookie=self.cookie, csrf="bad", origin=f"http://{self.host}")
        self.assertEqual(status, 403)
        status, _headers, body = self.request("POST", "/api/command", command, cookie=self.cookie, csrf=self.csrf, origin=f"http://{self.host}")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.ledger.calls[0][0], "category.save")

    def test_foreign_origin_is_rejected_for_sensitive_gets(self) -> None:
        self.open_session()
        status, _headers, _body = self.request("GET", "/api/state", cookie=self.cookie, origin="http://evil.example")
        self.assertEqual(status, 403)
        status, _headers, _body = self.request("GET", "/api/backups", cookie=self.cookie, origin="https://127.0.0.1:" + str(self.server.server_port))
        self.assertEqual(status, 403)

    def test_json_non_finite_values_and_photo_route_are_rejected(self) -> None:
        self.open_session()
        raw = b'{"action":"entry.save","payload":{"amount":NaN},"request_id":"nan-1"}'
        status, _headers, _body = self.request("POST", "/api/command", raw, cookie=self.cookie, csrf=self.csrf, origin=f"http://{self.host}")
        self.assertEqual(status, 400)
        status, _headers, _body = self.request("GET", "/api/photos/%2e%2e%2fCONTRACT.md", cookie=self.cookie)
        self.assertEqual(status, 404)

    def test_invalid_command_body_is_a_client_error(self) -> None:
        self.open_session()
        status, _headers, _body = self.request("POST", "/api/command", b"not-json", cookie=self.cookie, csrf=self.csrf, origin=f"http://{self.host}")
        self.assertEqual(status, 400)
        status, _headers, _body = self.request("POST", "/api/command", {"action": "entry.save", "payload": {}}, cookie=self.cookie, csrf=self.csrf, origin=f"http://{self.host}")
        self.assertEqual(status, 400)

    def test_presence_requires_auth_and_tracks_tabs(self) -> None:
        self.open_session()
        beat = {"tab_id": "tab-0001-abcd", "closing": False}
        status, _headers, _body = self.request("POST", "/api/presence", beat, cookie=self.cookie, origin=f"http://{self.host}")
        self.assertEqual(status, 403)
        self.assertEqual(self.server.tabs, {})
        status, _headers, _body = self.request("POST", "/api/presence", beat, cookie=self.cookie, csrf=self.csrf, origin=f"http://{self.host}")
        self.assertEqual(status, 200)
        self.assertIn("tab-0001-abcd", self.server.tabs)
        status, _headers, _body = self.request("POST", "/api/presence", {"tab_id": "../x", "closing": False}, cookie=self.cookie, csrf=self.csrf, origin=f"http://{self.host}")
        self.assertEqual(status, 400)
        status, _headers, _body = self.request("POST", "/api/presence", dict(beat, closing=True), cookie=self.cookie, csrf=self.csrf, origin=f"http://{self.host}")
        self.assertEqual(status, 200)
        self.assertEqual(self.server.tabs, {})
        self.assertEqual(self.server.inflight, 0)

    def test_server_stops_after_page_says_goodbye(self) -> None:
        self.open_session()
        idle = IdleExit(self.server, grace=0.3, startup_timeout=30)
        watcher = threading.Thread(target=idle.run, kwargs={"interval": 0.05}, daemon=True)
        beat = {"tab_id": "tab-0002-abcd", "closing": False}
        self.request("POST", "/api/presence", beat, cookie=self.cookie, csrf=self.csrf, origin=f"http://{self.host}")
        watcher.start()
        time.sleep(0.6)
        self.assertTrue(self.thread.is_alive(), "an open page keeps the server running")
        self.request("POST", "/api/presence", dict(beat, closing=True), cookie=self.cookie, csrf=self.csrf, origin=f"http://{self.host}")
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())


class IdleExitTests(unittest.TestCase):
    def make(self) -> tuple[Any, IdleExit, list[float]]:
        server = types.SimpleNamespace(lock=threading.RLock(), tabs={}, inflight=0, last_activity=0.0)
        clock = [0.0]
        return server, IdleExit(server, clock=lambda: clock[0]), clock

    def test_exits_after_last_page_closes_with_reload_grace(self) -> None:
        server, idle, clock = self.make()
        server.tabs["tab"] = 0.0
        clock[0] = 30
        self.assertFalse(idle.should_exit())
        del server.tabs["tab"]
        server.last_activity = 30
        clock[0] = 40
        self.assertFalse(idle.should_exit(), "a reload gets a short grace period")
        clock[0] = 46
        self.assertTrue(idle.should_exit())

    def test_waits_for_first_page_and_inflight_requests(self) -> None:
        server, idle, clock = self.make()
        clock[0] = 299
        self.assertFalse(idle.should_exit())
        server.inflight = 1
        clock[0] = 301
        self.assertFalse(idle.should_exit())
        server.inflight = 0
        self.assertTrue(idle.should_exit())

    def test_silent_page_expires_but_sleep_gap_resets(self) -> None:
        server, idle, clock = self.make()
        server.tabs["tab"] = 0.0
        for now in range(5, 180, 5):
            clock[0] = now
            self.assertFalse(idle.should_exit())
        clock[0] = 4000  # computer slept for an hour
        self.assertFalse(idle.should_exit())
        self.assertEqual(server.tabs["tab"], 4000)
        for now in range(4005, 4190, 5):
            clock[0] = now
            idle.should_exit()
        self.assertEqual(server.tabs, {})
        clock[0] = 4200
        self.assertTrue(idle.should_exit())


if __name__ == "__main__":
    unittest.main()
