import base64
import contextlib
import hashlib
import http.client
import io
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

from tg_triage import admin
from tg_triage.__main__ import main
from tg_triage.config import ConfigError, load_admin_config, load_config
from tg_triage.store import Store
from tg_triage.ticket_templates import load_templates
from tests.helpers import EXAMPLE, FAKE_HASH


NEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS classification_model_calls (
  run_id INTEGER NOT NULL, seq INTEGER NOT NULL,
  link TEXT NOT NULL,
  model TEXT, started_at REAL NOT NULL,
  latency_ms INTEGER NOT NULL DEFAULT 0, timeout_s REAL, finish_reason TEXT,
  prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_tokens INTEGER, cached_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_text TEXT,
  content_text TEXT,
  tool_calls_json TEXT,
  error_class TEXT, schema_error_kind TEXT,
  PRIMARY KEY (run_id, seq));
ALTER TABLE classification_tool_calls ADD COLUMN result_json TEXT;
ALTER TABLE classification_tool_calls ADD COLUMN started_at REAL;
ALTER TABLE classification_tool_calls ADD COLUMN args_full_json TEXT;
"""
PASSWORD = "test password"
NOW = 1_800_000_000.0
CHAT = -1001234567890
URL = "https://github.com/owner/repo/issues/42"
SCRIPT = "<script>alert('hello')</script>"
BASE_SCHEMA = """
CREATE TABLE classification_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
    turn_key TEXT NOT NULL, topic_id TEXT, phase TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT 'live', batch_fingerprint TEXT NOT NULL,
    attempt_no INTEGER, chat_id INTEGER NOT NULL, message_ids TEXT NOT NULL,
    id_space TEXT NOT NULL, image_ids TEXT NOT NULL DEFAULT '[]', rules_version TEXT,
    rules_fingerprint TEXT NOT NULL, provider TEXT, model TEXT, effort TEXT,
    model_calls INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
    cache_hit_tokens INTEGER NOT NULL DEFAULT 0, reasoning_tokens INTEGER,
    finish_reason TEXT, cost REAL NOT NULL DEFAULT 0, fallback INTEGER NOT NULL DEFAULT 0,
    fallback_reason TEXT, error_class TEXT, circuit_state TEXT NOT NULL DEFAULT 'none',
    retry_at REAL, proposal_json TEXT, reasoning_summary TEXT, final_state TEXT,
    decision_rule TEXT, outcomes_json TEXT, template TEXT, ticket_ref TEXT, error TEXT);
CREATE TABLE classification_tool_calls (
    run_id INTEGER NOT NULL, seq INTEGER NOT NULL, tool TEXT NOT NULL, args_json TEXT,
    result_bytes INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT, PRIMARY KEY (run_id, seq));
CREATE TABLE messages (
    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, received_at TEXT NOT NULL,
    sender_name TEXT NOT NULL, text TEXT NOT NULL, decision TEXT NOT NULL,
    issue_url TEXT, chat_username TEXT, PRIMARY KEY (chat_id, message_id));
"""


def _hash_file(path):
    salt = b"test-salt-for-admin"
    digest = hashlib.scrypt(PASSWORD.encode(), salt=salt, n=1024, r=8, p=1, dklen=32)
    path.write_bytes(b"$".join((b"scrypt$1024$8$1", base64.b64encode(salt), base64.b64encode(digest))))
    path.chmod(0o600)


class AdminServiceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db_path = self.root / "admin #test.sqlite3"
        self.hash_path = self.root / "passwd"
        _hash_file(self.hash_path)
        self.db = sqlite3.connect(self.db_path, isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.executescript(BASE_SCHEMA)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "INSERT INTO messages VALUES (?, 1, '2025-01-01T01:00:00+01:00', 'Sender', ?, 'created', ?, 'triage')",
            (CHAT, SCRIPT, URL),
        )
        self.now = NOW
        self.server = admin.AdminServer(("127.0.0.1", 0), self.db_path, self.hash_path,
                                        clock=lambda: self.now, session_hours=1)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05})
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def add_run(self, **overrides):
        values = dict(created_at=self.now, turn_key="turn", phase="assistant_live", batch_fingerprint="batch",
                      chat_id=CHAT, message_ids="[1]", id_space="message", rules_fingerprint="rules",
                      final_state="HOLD", error_class="infra")
        values.update(overrides)
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        return self.db.execute(f"INSERT INTO classification_runs ({columns}) VALUES ({placeholders})",
                               tuple(values.values())).lastrowid

    def request(self, path, *, method="GET", cookie=None, body=None, headers=None, stream=False):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        headers = dict(headers or {})
        if cookie:
            headers["Cookie"] = cookie
        if body is not None:
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            if stream and response.status == 200:
                self.addCleanup(response.close)
                self.addCleanup(connection.close)
                return connection, response
            with response:
                return response.status, response.headers, response.read()
        finally:
            if not stream or "response" not in locals() or response.status != 200:
                connection.close()

    def login(self, password=PASSWORD):
        return self.request("/api/login", method="POST", body={"password": password})

    def cookie(self):
        status, headers, body = self.login()
        self.assertEqual((status, body), (204, b""))
        cookie = headers["Set-Cookie"]
        self.assertRegex(cookie, r"^la_sid=[a-f0-9]{64}; HttpOnly; SameSite=Strict; Path=/$")
        return cookie.split(";", 1)[0]

    def assert_headers(self, headers):
        for name, value in admin.SECURITY_HEADERS.items():
            self.assertEqual(headers[name], value)

    def test_auth_login_logout_expiry_and_security_headers(self):
        for path in ("/api/me", "/api/runs", "/api/runs/1", "/api/stream", "/api/unknown"):
            status, headers, body = self.request(path)
            self.assertEqual((status, json.loads(body)), (401, {"error": "unauthorized"}))
            self.assert_headers(headers)
        status, headers, body = self.login("wrong")
        self.assertEqual((status, json.loads(body)), (401, {"error": "invalid"}))
        self.assert_headers(headers)
        cookie = self.cookie()
        status, headers, body = self.request("/api/me", cookie=cookie)
        self.assertEqual((status, json.loads(body)), (200, {"ok": True, "server_time": NOW, "poll_ms": 1000}))
        self.assert_headers(headers)
        status, headers, _ = self.request("/api/logout", method="POST", cookie=cookie)
        self.assertEqual(status, 204)
        self.assertIn("Max-Age=0", headers["Set-Cookie"])
        self.assertEqual(self.request("/api/me", cookie=cookie)[0], 401)
        cookie = self.cookie()
        self.now += 3600
        self.assertEqual(self.request("/api/me", cookie=cookie)[0], 401)

    def test_lockout_is_per_ip_and_expires_after_ten_minutes(self):
        for _ in range(4):
            self.assertEqual(self.login("wrong")[0], 401)
        status, _, body = self.login("wrong")
        self.assertEqual((status, json.loads(body)), (429, {"error": "locked", "retry_after": 600}))
        sid, retry = self.server.sessions.login(PASSWORD.encode(), "100.64.0.2")
        self.assertEqual(retry, 0)
        self.assertTrue(self.server.sessions.valid(sid))
        self.now += 599.25
        status, _, body = self.login()
        self.assertEqual((status, json.loads(body)), (429, {"error": "locked", "retry_after": 1}))
        self.now += 0.75
        self.cookie()

    def test_global_failure_backoff_allows_valid_password(self):
        sessions = self.server.sessions
        for index in range(admin.GLOBAL_FAILURE_LIMIT):
            sid, retry = sessions.login(b"wrong", f"100.64.0.{index + 1}")
            self.assertIsNone(sid)
        self.assertEqual(retry, 600)
        self.assertEqual(sessions.login(b"wrong", "100.64.1.1"), (None, 600))
        sid, retry = sessions.login(PASSWORD.encode(), "100.64.1.1")
        self.assertEqual(retry, 0)
        self.assertTrue(sessions.valid(sid))

    def test_host_allowlist_applies_before_routing_and_keeps_origin_check(self):
        headers = {"Host": "evil.com:8787", "Origin": "http://evil.com:8787"}
        for method, path in (("GET", "/"), ("POST", "/api/login"), ("GET", "/api/stream")):
            with self.subTest(method=method, path=path):
                status, response_headers, body = self.request(path, method=method, headers=headers)
                self.assertEqual((status, json.loads(body)), (403, {"error": "forbidden"}))
                self.assert_headers(response_headers)
        host = f"localhost:{self.server.server_port}"
        self.assertEqual(self.request("/", headers={"Host": host})[0], 200)
        self.assertEqual(self.request("/api/login", method="POST", body={"password": PASSWORD},
                                      headers={"Host": host, "Origin": "http://evil.com:8787"})[0], 403)

    def test_static_whitelist_with_and_without_resources(self):
        package = self.root / "tg_triage"
        static = package / "admin_static"
        static.mkdir(parents=True)
        routes = {"/": "index.html", "/static/app.css": "app.css", "/static/app.js": "app.js"}
        with patch.object(admin.resources, "files", return_value=package):
            self.assertEqual(self.request("/")[0], 404)
            for path, filename in routes.items():
                (static / filename).write_bytes(filename.encode())
                status, headers, body = self.request(path)
                self.assertEqual((status, body), (200, filename.encode()))
                self.assert_headers(headers)
            self.assertEqual(self.request("/static/../passwd")[0], 404)

    def test_database_connection_rejects_writes_and_requests_preserve_mtime(self):
        run_id = self.add_run()
        before = self.db_path.stat().st_mtime_ns
        with admin._read_database(self.db_path) as db:
            self.assertEqual(db.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                db.execute("DELETE FROM classification_runs")
            db.execute("PRAGMA query_only=OFF")
            with self.assertRaisesRegex(sqlite3.OperationalError, "readonly"):
                db.execute("DELETE FROM classification_runs")
        cookie = self.cookie()
        self.assertEqual(self.request("/api/runs", cookie=cookie)[0], 200)
        self.assertEqual(self.request(f"/api/runs/{run_id}", cookie=cookie)[0], 200)
        self.assertEqual(before, self.db_path.stat().st_mtime_ns)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM classification_runs").fetchone()[0], 1)

    def test_delete_journal_busy_returns_503_with_retry_after(self):
        self.assertEqual(self.db.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
        cookie = self.cookie()
        self.db.execute("BEGIN EXCLUSIVE")
        try:
            for path in ("/api/runs", "/api/runs/1", "/api/stream"):
                with self.subTest(path=path):
                    start = time.monotonic()
                    status, headers, body = self.request(path, cookie=cookie)
                    self.assertLess(time.monotonic() - start, 0.25)
                    self.assertEqual((status, headers["Retry-After"], json.loads(body)),
                                     (503, "1", {"error": "unavailable"}))
        finally:
            self.db.execute("ROLLBACK")

    def test_delete_journal_admin_poll_and_stream_allow_writer_commits(self):
        self.assertEqual(self.db.execute("PRAGMA journal_mode=DELETE").fetchone()[0], "delete")
        run_id = self.add_run(latency_ms=0)
        cookie = self.cookie()
        commits, failures = [], []
        active = threading.Event()
        polled_during_writes = threading.Event()
        poll = admin._Handler._poll

        def observe_poll(handler, *args):
            if active.is_set():
                polled_during_writes.set()
            return poll(handler, *args)

        def write():
            try:
                with contextlib.closing(sqlite3.connect(self.db_path, timeout=0.45)) as db:
                    active.set()
                    for latency in range(1, 61):
                        db.execute("UPDATE classification_runs SET latency_ms=? WHERE run_id=?", (latency, run_id))
                        start = time.monotonic()
                        db.commit()
                        commits.append(time.monotonic() - start)
                        time.sleep(0.02)
            except sqlite3.Error as error:
                failures.append(error)
            finally:
                active.clear()

        with patch.object(admin, "POLL_SECONDS", 0.01), patch.object(admin._Handler, "_poll", observe_poll):
            connection, response = self.request("/api/stream", cookie=cookie, stream=True)
            self.assertEqual(self.event(response)[0], "hello")
            writer = threading.Thread(target=write)
            writer.start()
            try:
                self.assertTrue(polled_during_writes.wait(2), "SSE did not poll during writes")
                for _ in range(20):
                    for path in ("/api/runs", f"/api/runs/{run_id}"):
                        self.assertIn(self.request(path, cookie=cookie)[0], (200, 503))
            finally:
                response.close()
                connection.close()
                writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(len(commits), 60)
        print(f"DELETE journal: {len(commits)} writer commits, max {max(commits) * 1000:.2f} ms", flush=True)
        self.assertLess(max(commits), 0.5)

    def test_runs_list_filters_and_pagination(self):
        first = self.add_run()
        second = self.add_run(provider="google", model="Gemini-3", final_state="READY", error_class=None)
        third = self.add_run(provider="openai", model="GPT-6", final_state="READY", error_class="timeout")
        fourth = self.add_run(chat_id=-123, model="GPT-6", final_state="READY", error_class=None)
        cookie = self.cookie()

        def results(query):
            status, _, body = self.request("/api/runs?" + query, cookie=cookie)
            self.assertEqual(status, 200)
            return json.loads(body)

        page = results("limit=2")
        self.assertEqual([row["run_id"] for row in page["runs"]], [fourth, third])
        self.assertEqual(page["next_before"], third)
        page = results(f"limit=2&before={third}")
        self.assertEqual([row["run_id"] for row in page["runs"]], [second, first])
        self.assertIsNone(page["next_before"])
        self.assertTrue(page["runs"][1]["in_progress"])
        for query, expected in (
            ("state=READY", [fourth, third, second]),
            ("model=gPt", [fourth, third]),
            (f"chat={CHAT}", [third, second, first]),
            ("errors=1", [third, first]),
            (f"state=READY&model=gPt&chat={CHAT}&errors=1", [third]),
            ("model=%25", []),
        ):
            with self.subTest(query=query):
                self.assertEqual([row["run_id"] for row in results(query)["runs"]], expected)

    def test_detail_old_schema_and_plain_json_message_text(self):
        run_id = self.add_run(proposal_json='[{"intent":"bug"}]', outcomes_json='[{"final_state":"READY"}]')
        self.db.execute("INSERT INTO classification_tool_calls VALUES (?, 1, 'lookup', '{}', 25, 8, NULL)", (run_id,))
        cookie = self.cookie()
        status, headers, body = self.request(f"/api/runs/{run_id}", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        data = json.loads(body)
        self.assertEqual(set(data), {"run", "inputs", "steps"})
        self.assertEqual(data["run"]["chat_label"], "triage")
        self.assertEqual(data["run"]["ticket_url"], URL)
        self.assertEqual(data["run"]["tool_calls"], 1)
        self.assertEqual(data["run"]["proposal"], [{"intent": "bug"}])
        self.assertEqual(data["run"]["outcomes"], [{"final_state": "READY"}])
        self.assertEqual(data["inputs"], [dict(chat_id=CHAT, message_id=1, received_at=1735689600.0,
                                               sender_name="Sender", text=SCRIPT, decision="created", issue_url=URL)])
        self.assertEqual(data["steps"], [dict(kind="tool", seq=1, started_at=None, tool="lookup",
                                              args={}, result=None, result_bytes=25, latency_ms=8, error=None)])
        status, _, body = self.request(f"/api/runs/{run_id + 1}", cookie=cookie)
        self.assertEqual((status, json.loads(body)), (404, {"error": "not_found"}))

    def test_detail_new_schema_interleaving_and_full_fields(self):
        self.db.executescript(NEW_SCHEMA)
        run_id = self.add_run(model_calls=2, tokens_in=23, reasoning_tokens=7, cache_hit_tokens=3)
        self.db.execute(
            """INSERT INTO classification_model_calls
               (run_id, seq, link, started_at, reasoning_text, content_text, tool_calls_json)
               VALUES (?, 1, 'gemini', ?, 'thinking', 'raw content', '[{"id":"1","name":"lookup","arguments":{}}]'),
                      (?, 2, 'gpt', ?, 'more thinking', 'another response', ?)""",
            (run_id, NOW, run_id, NOW + 2, '[{"id":"2"' + "…[truncated]"),
        )
        self.db.execute(
            """INSERT INTO classification_tool_calls
               (run_id, seq, tool, args_json, result_json, started_at, args_full_json)
               VALUES (?, 1, 'lookup', '{"reduced":true}', '{"found":true}', ?, '{"full":true}'),
                      (?, 2, 'lookup', '{"reduced":true}', 'plain result', ?, 'invalid JSON')""",
            (run_id, NOW, run_id, NOW + 1),
        )
        status, _, body = self.request(f"/api/runs/{run_id}", cookie=self.cookie())
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(set(data["run"]), set(admin.SUMMARY_FIELDS + admin.DETAIL_FIELDS) | {
            "proposal", "outcomes", "chat_label", "message_count", "image_count",
            "tool_calls", "ticket_url", "in_progress",
        })
        self.assertEqual(data["run"]["tokens_in"], 23)
        self.assertEqual(data["run"]["reasoning_tokens"], 7)
        self.assertEqual(data["run"]["cache_hit_tokens"], 3)
        self.assertEqual([(step["kind"], step["seq"]) for step in data["steps"]],
                         [("model", 1), ("tool", 1), ("tool", 2), ("model", 2)])
        model, first, second, truncated = data["steps"]
        self.assertEqual(model["reasoning_text"], "thinking")
        self.assertEqual(model["content_text"], "raw content")
        self.assertEqual(model["tool_calls"], [{"id": "1", "name": "lookup", "arguments": {}}])
        self.assertEqual((first["args"], first["result"]), ({"full": True}, {"found": True}))
        self.assertEqual((second["args"], second["result"]), ({"reduced": True}, "plain result"))
        self.assertEqual(truncated["tool_calls"], '[{"id":"2"' + "…[truncated]")

    def test_topic_ref_returns_refs_and_never_looks_up_message_ids(self):
        run_id = self.add_run(id_space="topic_ref", message_ids="[1,42]", topic_id="topic")
        status, _, body = self.request(f"/api/runs/{run_id}", cookie=self.cookie())
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["inputs"], [])
        self.assertEqual(data["run"]["message_refs"], [1, 42])
        self.assertIsNone(data["run"]["ticket_url"])

    def event(self, response):
        fields = {}
        while True:
            line = response.readline()
            self.assertTrue(line, "SSE closed without an event")
            if line.startswith(b":"):
                continue
            if line == b"\n":
                if fields:
                    return fields["event"], int(fields["id"]), json.loads(fields["data"])
                continue
            name, value = line.decode().rstrip("\n").split(": ", 1)
            fields[name] = value

    def test_sse_hello_created_updated_and_last_event_id_resume(self):
        baseline = self.add_run()
        cookie = self.cookie()
        connection, response = self.request("/api/stream", cookie=cookie, stream=True)
        self.assertEqual(response.headers["Content-Type"], "text/event-stream")
        self.assert_headers(response.headers)
        self.assertEqual(self.event(response), ("hello", baseline, {"server_time": NOW}))
        start = time.monotonic()
        run_id = self.add_run()
        event, cursor, data = self.event(response)
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual((event, cursor, data["run_id"]), ("run.created", run_id, run_id))
        self.db.execute("UPDATE classification_runs SET provider='gemini', model_calls=1 WHERE run_id=?", (baseline,))
        event, cursor, data = self.event(response)
        self.assertEqual((event, cursor, data["run_id"], data["provider"]),
                         ("run.updated", run_id, baseline, "gemini"))
        response.close()
        connection.close()
        missed = self.add_run()
        _, resumed = self.request("/api/stream", cookie=cookie, headers={"Last-Event-ID": str(cursor)}, stream=True)
        self.assertEqual(self.event(resumed), ("hello", cursor, {"server_time": NOW}))
        event, cursor, data = self.event(resumed)
        self.assertEqual((event, cursor, data["run_id"]), ("run.created", missed, missed))

    def test_fifth_sse_stream_returns_503(self):
        cookie = self.cookie()
        for _ in range(4):
            _, response = self.request("/api/stream", cookie=cookie, stream=True)
            self.assertEqual(self.event(response)[0], "hello")
        status, headers, _ = self.request("/api/stream", cookie=cookie)
        self.assertEqual(status, 503)
        self.assert_headers(headers)

    def test_sse_max_lifetime_closes_connection_and_releases_slot(self):
        cookie = self.cookie()
        with patch.object(admin, "STREAM_SECONDS", 0.15):
            _, response = self.request("/api/stream", cookie=cookie, stream=True)
            self.assertEqual(self.event(response)[0], "hello")
            self.assertEqual(response.read(), b"")
        for _ in range(4):
            self.assertTrue(self.server.streams.acquire(timeout=1))
            self.addCleanup(self.server.streams.release)


class AdminCommandTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.config = self.root / "listener.toml"
        self.hash_file = self.root / "passwd"
        self.enterContext(patch.dict(os.environ, {"TG_API_ID": "123", "TG_API_HASH": FAKE_HASH}, clear=True))
        self.enterContext(patch("os.umask"))

    def write_config(self, text):
        self.config.write_text(text, encoding="utf-8")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = main(list(args))
        return status, out.getvalue(), err.getvalue()

    def test_config_rejects_wildcard_public_and_accepts_loopback_tailscale(self):
        for host in ("0.0.0.0", "8.8.8.8", "100.128.0.1", "localhost", "::1"):
            self.write_config(f'[admin]\nbind = "{host}:8787"\n')
            with self.subTest(host=host), self.assertRaisesRegex(ConfigError, "admin.bind"):
                load_admin_config(self.config)
        for host in ("127.0.0.1", "100.81.90.91"):
            self.write_config(f'[admin]\nbind = "{host}:8787"\nsession_hours = 72\n')
            self.assertEqual(load_admin_config(self.config).address, (host, 8787))
        for field in ('password_hash_file = "relative"', 'session_hours = 73', 'session_hours = true'):
            self.write_config(f"[admin]\n{field}\n")
            with self.subTest(field=field), self.assertRaises(ConfigError):
                load_admin_config(self.config)

    def test_admin_missing_hash_refuses_start_with_fixed_message(self):
        for section in ("", '[admin]\n', f'[admin]\npassword_hash_file = "{self.hash_file.as_posix()}"\n'):
            self.write_config('[storage]\ndb_path = "db.sqlite3"\n' + section)
            self.assertEqual(self.run_cli("admin", "--config", str(self.config)),
                             (2, "", "ConfigError: admin: password hash unavailable\n"))

    def test_check_config_unchanged_without_admin_and_reports_section_when_present(self):
        source = EXAMPLE.read_text(encoding="utf-8")
        self.write_config(source)
        expected = load_config(self.config).redacted()
        expected["assistant"]["pack_fingerprint"] = None
        expected["templates"] = {"fingerprint": load_templates().fingerprint}
        status, out, err = self.run_cli("check-config", "--config", str(self.config))
        self.assertEqual((status, err), (0, ""))
        self.assertEqual(out, json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
        self.write_config(source + f'\n[admin]\npassword_hash_file = "{self.hash_file.as_posix()}"\n')
        for configured in (False, True):
            if configured:
                _hash_file(self.hash_file)
            status, out, err = self.run_cli("check-config", "--config", str(self.config))
            self.assertEqual((status, err), (0, ""))
            expected["admin"] = {"bind": "127.0.0.1:8787", "configured": configured}
            self.assertEqual(json.loads(out), json.loads(json.dumps(expected)))

    def test_bot_run_ignores_admin_config_and_never_imports_admin(self):
        self.write_config(EXAMPLE.read_text(encoding="utf-8") + '\n[admin]\nbind = "invalid"\n')
        with patch.dict("sys.modules", {"tg_triage.admin": None}), \
                patch("tg_triage.__main__.validate_data_directories"), \
                patch("tg_triage.__main__.ProcessLock", return_value=contextlib.nullcontext()), \
                patch("tg_triage.__main__.Store", side_effect=lambda path: Store(":memory:")), \
                patch("tg_triage.telegram_io.run", new_callable=AsyncMock) as run:
            status, _, err = self.run_cli("run", "--config", str(self.config))
        self.assertEqual((status, err), (0, ""))
        run.assert_awaited_once()

    def test_admin_passwd_prompts_twice_refuses_bad_input_and_never_prints_hash(self):
        for pair in (("", ""), ("first", "second")):
            with patch.object(admin.getpass, "getpass", side_effect=pair) as prompt:
                status, out, err = self.run_cli("admin-passwd", "--out", str(self.hash_file))
            self.assertEqual(prompt.call_count, 2)
            self.assertEqual((status, out), (2, ""))
            self.assertIn("passwords must be non-empty and match", err)
            self.assertFalse(self.hash_file.exists())
        gid = self.root.stat().st_gid
        for scenario in ("no_group", "allowed", "denied"):
            with self.subTest(scenario=scenario):
                path = self.root / scenario
                lookup = Mock(return_value=SimpleNamespace(gr_gid=gid),
                              side_effect=KeyError if scenario == "no_group" else None)
                system = SimpleNamespace(
                    name="posix", O_WRONLY=os.O_WRONLY, O_CREAT=os.O_CREAT, O_EXCL=os.O_EXCL,
                    O_BINARY=getattr(os, "O_BINARY", 0), open=Mock(wraps=os.open), close=os.close,
                    write=os.write, fstat=os.fstat,
                    fchown=Mock(side_effect=PermissionError if scenario == "denied" else None),
                    fchmod=Mock(wraps=getattr(os, "fchmod", None)),
                )
                with patch.dict("sys.modules", {"grp": SimpleNamespace(getgrnam=lookup)}), \
                        patch.object(admin, "os", system), \
                        patch.object(admin.getpass, "getpass", side_effect=[PASSWORD, PASSWORD]) as prompt:
                    result = self.run_cli("admin-passwd", "--out", str(path))
                    self.assertEqual(prompt.call_count, 2)
                    lookup.assert_called_once_with("tglistener")
                    self.assertEqual(system.open.call_args.args[-1], 0o600)
                    if scenario == "no_group":
                        system.fchown.assert_not_called()
                    else:
                        system.fchown.assert_called_once_with(ANY, -1, gid)
                    if scenario == "denied":
                        self.assertEqual(result, (2, "", "ConfigError: admin-passwd: cannot give group "
                                                  "tglistener access; run with sudo\n"))
                        system.fchmod.assert_not_called()
                        self.assertFalse(path.exists())
                        continue
                    self.assertEqual(result, (0, "", ""))
                    mode = 0o600 if scenario == "no_group" else 0o640
                    if scenario == "no_group":
                        system.fchmod.assert_not_called()
                    else:
                        system.fchmod.assert_called_once_with(ANY, mode)
                    if os.name != "nt":
                        self.assertEqual(path.stat().st_mode & 0o777, mode)
                        self.assertEqual(path.stat().st_gid, gid)
                    salt, n, r, p, digest = admin._read_password_hash(path)
                    self.assertEqual(hashlib.scrypt(PASSWORD.encode(), salt=salt, n=n, r=r, p=p,
                                                    dklen=32, maxmem=128 * 1024**2), digest)

    def test_admin_passwd_removes_partially_written_file(self):
        write = os.write
        with patch.object(admin.getpass, "getpass", side_effect=[PASSWORD, PASSWORD]), \
                patch.object(admin.os, "write", side_effect=lambda fd, data: write(fd, data[:4])):
            self.assertEqual(self.run_cli("admin-passwd", "--out", str(self.hash_file)),
                             (2, "", "ConfigError: admin-passwd: cannot write password hash file\n"))
        self.assertFalse(self.hash_file.exists())
