"""Read-only HTTP service for the Listener admin UI."""

import base64
import getpass
import hashlib
import hmac
import ipaddress
import json
import math
import os
import secrets
import select
import socket
import sqlite3
import threading
import time
import warnings
from collections import deque
from contextlib import contextmanager, suppress
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .config import ConfigError, _validate_admin_secret_stat


POLL_SECONDS = 1
PING_SECONDS = 15
LOCK_SECONDS = 600
GLOBAL_FAILURE_LIMIT = 50
STREAM_SECONDS = 600
UPDATE_WINDOW = 1800
PAGE_SIZE = 50
MAX_INPUTS = 1000
MAX_STEPS_PER_KIND = 256
MAX_SQLITE_ID = 2**63 - 1
MAX_LOGIN_BYTES = 8192
SUMMARY_FIELDS = (
    "run_id created_at phase mode chat_id provider model model_calls latency_ms "
    "final_state decision_rule template ticket_ref error_class fallback_reason attempt_no"
).split()
DETAIL_FIELDS = (
    "tokens_in tokens_out reasoning_tokens cache_hit_tokens finish_reason retry_at "
    "circuit_state batch_fingerprint topic_id id_space"
).split()
UPDATE_FIELDS = "final_state model_calls provider error_class ticket_ref latency_ms".split()
MODEL_FIELDS = (
    "seq started_at link model latency_ms timeout_s finish_reason prompt_tokens "
    "completion_tokens reasoning_tokens reasoning_text content_text error_class schema_error_kind"
).split()
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


def _scrypt(password, salt, n, r, p):
    return hashlib.scrypt(password, salt=salt, n=n, r=r, p=p, dklen=32, maxmem=128 * 1024**2)


def _read_password_hash(path):
    try:
        if path is None or not path.is_file():
            raise ValueError()
        with path.open("rb") as stream:
            _validate_admin_secret_stat(os.fstat(stream.fileno()))
            encoded = stream.read(1025).strip()
        if len(encoded) > 1024:
            raise ValueError()
        scheme, n, r, p, salt, digest = encoded.split(b"$")
        n, r, p = int(n), int(r), int(p)
        salt, digest = (base64.b64decode(value, validate=True) for value in (salt, digest))
        if (scheme != b"scrypt" or not 2 <= n <= 2**20 or n & (n - 1)
                or not 1 <= r <= 32 or not 1 <= p <= 16
                or 128 * n * r >= 128 * 1024**2 or n * r * p > 2**20
                or not 8 <= len(salt) <= 64 or len(digest) != 32):
            raise ValueError()
        return salt, n, r, p, digest
    except (OSError, ValueError, KeyError):
        raise ConfigError("admin: password hash unavailable") from None


def write_password_hash(path):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Admin password: ")
            confirmation = getpass.getpass("Confirm admin password: ")
    except (EOFError, getpass.GetPassWarning):
        raise ConfigError("admin-passwd: a private terminal is required") from None
    if not password or password != confirmation:
        raise ConfigError("admin-passwd: passwords must be non-empty and match")
    salt = secrets.token_bytes(16)
    n, r, p = 2**16, 8, 1
    digest = _scrypt(password.encode("utf-8"), salt, n, r, p)
    encoded = b"$".join((f"scrypt${n}${r}${p}".encode("ascii"), base64.b64encode(salt),
                         base64.b64encode(digest))) + b"\n"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            try:
                if os.write(fd, encoded) != len(encoded):
                    raise OSError("incomplete password hash write")
                if os.name != "nt":
                    import grp
                    try:
                        group = grp.getgrnam("tglistener")
                    except KeyError:
                        group = None
                    if group is not None:
                        try:
                            os.fchown(fd, -1, group.gr_gid)
                        except PermissionError:
                            raise ConfigError("admin-passwd: cannot give group tglistener access; run with sudo") from None
                        os.fchmod(fd, 0o640)
            finally:
                os.close(fd)
        except BaseException:
            Path(path).unlink(missing_ok=True)
            raise
    except OSError:
        raise ConfigError("admin-passwd: cannot write password hash file") from None


class _Sessions:
    def __init__(self, password_hash, lifetime, clock):
        self.password_hash = password_hash
        self.lifetime = lifetime
        self.clock = clock
        self.sessions = {}
        self.failures = {}
        self.recent_failures = deque(maxlen=GLOBAL_FAILURE_LIMIT)
        self.lock = threading.Lock()
        self.login_lock = threading.Lock()

    def login(self, password, source):
        with self.login_lock:
            now = self.clock()
            cutoff = now - LOCK_SECONDS
            self.failures = {ip: attempts for ip, attempts in self.failures.items() if attempts[-1] > cutoff}
            failures = self.failures.get(source, deque())
            if len(failures) == 5:
                return None, math.ceil(failures[-1] + LOCK_SECONDS - now)
            while failures and failures[0] <= cutoff:
                failures.popleft()
            while self.recent_failures and self.recent_failures[0] <= cutoff:
                self.recent_failures.popleft()
            salt, n, r, p, digest = self.password_hash
            if password and hmac.compare_digest(_scrypt(password, salt, n, r, p), digest):
                with self.lock:
                    now = self.clock()
                    self.sessions = {sid: expiry for sid, expiry in self.sessions.items() if expiry > now}
                    sid = secrets.token_hex(32)
                    self.sessions[sid] = now + self.lifetime
                self.failures.pop(source, None)
                return sid, 0
            now = self.clock()
            failures.append(now)
            self.failures[source] = failures
            self.recent_failures.append(now)
            if len(failures) == 5:
                return None, LOCK_SECONDS
            # Apply the aggregate retry signal only after checking the password.
            if len(self.recent_failures) == GLOBAL_FAILURE_LIMIT:
                return None, max(1, math.ceil(self.recent_failures[0] + LOCK_SECONDS - now))
            return None, 0

    def valid(self, sid):
        with self.lock:
            expiry = self.sessions.get(sid)
            if expiry is None:
                return False
            if expiry <= self.clock():
                del self.sessions[sid]
                return False
            return True

    def logout(self, sid):
        with self.lock:
            self.sessions.pop(sid, None)


@contextmanager
def _read_database(path):
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=0)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db
    finally:
        db.close()


def _parse_json(value, *, invalid=None):
    try:
        return json.loads(value) if value is not None else None
    except (ValueError, RecursionError):
        return invalid


def _list_json(value):
    decoded = _parse_json(value)
    return decoded if isinstance(decoded, list) else []


def _columns(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _messages(db, run, ids, *, include_text=False):
    if run["id_space"] == "topic_ref":
        return []
    ids = list(dict.fromkeys(value for value in ids[:MAX_INPUTS]
                             if type(value) is int and 0 < value <= MAX_SQLITE_ID))
    if not ids:
        return []
    columns = _columns(db, "messages")
    if not columns:
        return []
    label = "chat_username" if "chat_username" in columns else "NULL AS chat_username"
    fields = "message_id, issue_url, " + label
    if include_text:
        fields += ", chat_id, received_at, sender_name, text, decision"
    placeholders = ",".join("?" for _ in ids)
    rows = db.execute(f"SELECT {fields} FROM messages WHERE chat_id=? AND message_id IN ({placeholders})",
                      (run["chat_id"], *ids))
    by_id = {row["message_id"]: dict(row) for row in rows}
    return [by_id[value] for value in ids if value in by_id]


def _https_url(value):
    if not isinstance(value, str):
        return False
    try:
        parts = urlsplit(value)
        return parts.scheme == "https" and bool(parts.hostname) and parts.username is None
    except ValueError:
        return False


def _summary(db, run, now, *, ids=None, messages=None):
    if ids is None:
        ids = _list_json(run["message_ids"])
    if messages is None:
        messages = _messages(db, run, ids)
    result = {key: run[key] for key in SUMMARY_FIELDS}
    tool_count = db.execute("SELECT COUNT(*) FROM classification_tool_calls WHERE run_id=?", (run["run_id"],))
    result.update(
        chat_label=next((row["chat_username"] for row in messages if row["chat_username"]), None),
        message_count=len(ids),
        image_count=len(_list_json(run["image_ids"])),
        tool_calls=tool_count.fetchone()[0],
        ticket_url=next((row["issue_url"] for row in messages if _https_url(row["issue_url"])), None),
        in_progress=(run["phase"].startswith("assistant_") and run["provider"] is None
                     and run["model_calls"] == 0 and run["error_class"] == "infra"
                     and now - 600 <= run["created_at"] <= now),
    )
    return result


def _bounded_int(value, minimum=1, maximum=MAX_SQLITE_ID):
    if len(value) > 20:
        raise ValueError()
    number = int(value)
    if not minimum <= number <= maximum:
        raise ValueError()
    return number


def _runs(db, query, now):
    params = parse_qs(query, keep_blank_values=True, max_num_fields=16)
    predicates, values = [], []
    if "before" in params:
        predicates.append("run_id < ?")
        values.append(_bounded_int(params["before"][0]))
    if "state" in params:
        predicates.append("final_state = ?")
        values.append(params["state"][0])
    if "model" in params:
        predicates.append("instr(lower(model), lower(?)) > 0")
        values.append(params["model"][0])
    if "chat" in params:
        predicates.append("chat_id = ?")
        values.append(_bounded_int(params["chat"][0], minimum=-(2**63)))
    if params.get("errors") == ["1"]:
        predicates.append("(error_class IS NOT NULL OR error IS NOT NULL)")
    limit = _bounded_int(params.get("limit", [str(PAGE_SIZE)])[0])
    limit = min(limit, PAGE_SIZE)
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    rows = db.execute(
        f"SELECT * FROM classification_runs{where} ORDER BY run_id DESC LIMIT ?", (*values, limit + 1)
    ).fetchall()
    return {
        "runs": [_summary(db, row, now) for row in rows[:limit]],
        "next_before": rows[limit - 1]["run_id"] if len(rows) > limit else None,
    }


def _step_rows(db, run_id):
    models = db.execute(
        "SELECT * FROM classification_model_calls WHERE run_id=? ORDER BY seq LIMIT ?",
        (run_id, MAX_STEPS_PER_KIND),
    ).fetchall() if _columns(db, "classification_model_calls") else []
    tools = db.execute(
        "SELECT * FROM classification_tool_calls WHERE run_id=? ORDER BY seq LIMIT ?",
        (run_id, MAX_STEPS_PER_KIND),
    ).fetchall()
    return models, tools


def _steps(models, tools):
    steps = []
    for row in models:
        raw_calls = row["tool_calls_json"]
        steps.append(dict({key: row[key] for key in MODEL_FIELDS},
                          kind="model", tool_calls=_parse_json(raw_calls, invalid=raw_calls)))
    for row in tools:
        tool = dict(row)
        full_args = tool.get("args_full_json")
        raw_result = tool.get("result_json")
        args = _parse_json(tool["args_json"])
        if full_args is not None:
            args = _parse_json(full_args, invalid=args)
        result = _parse_json(raw_result, invalid=raw_result)
        steps.append(dict(kind="tool", seq=tool["seq"], started_at=tool.get("started_at"),
                          tool=tool["tool"], args=args,
                          result=result, result_bytes=tool["result_bytes"],
                          latency_ms=tool["latency_ms"], error=tool["error"]))
    return sorted(steps, key=lambda step: (
        step["started_at"] is None, step["started_at"] or 0, step["kind"] != "model", step["seq"]
    ))


def _timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return (dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)).timestamp()


def _detail(db, run_id, now):
    row = db.execute("SELECT * FROM classification_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    ids = _list_json(row["message_ids"])
    messages = _messages(db, row, ids, include_text=True)
    run = _summary(db, row, now, ids=ids, messages=messages)
    run.update({key: row[key] for key in DETAIL_FIELDS})
    if row["id_space"] == "topic_ref":
        run["message_refs"] = ids
    return run, messages, row["proposal_json"], row["outcomes_json"], _step_rows(db, run_id)


def _format_detail(detail):
    run, messages, proposal, outcomes, step_rows = detail
    run.update(proposal=_parse_json(proposal), outcomes=_parse_json(outcomes))
    for message in messages:
        message.pop("chat_username")
        message["received_at"] = _timestamp(message["received_at"])
    return {"run": run, "inputs": messages, "steps": _steps(*step_rows)}


def _recent_signatures(db, cursor, now):
    return {
        row["run_id"]: tuple(row[key] for key in UPDATE_FIELDS)
        for row in db.execute(
            f"SELECT run_id, {', '.join(UPDATE_FIELDS)} FROM classification_runs "
            "WHERE created_at >= ? AND run_id <= ?",
            (now - UPDATE_WINDOW, cursor),
        )
    }


def _static_file(name):
    return resources.files(__package__).joinpath("admin_static", name).read_bytes()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class AdminServer(ThreadingHTTPServer):
    def __init__(self, address, db_path, password_hash_file, *, session_hours=12, clock=time.time):
        host = ipaddress.IPv4Address(address[0])
        if not (host.is_loopback or host in ipaddress.IPv4Network("100.64.0.0/10")):
            raise ConfigError("admin.bind: expected a loopback or Tailscale IPv4 address and port")
        self.db_path = db_path
        self.clock = clock
        self.sessions = _Sessions(_read_password_hash(password_hash_file), session_hours * 3600, clock)
        self.streams = threading.BoundedSemaphore(4)
        super().__init__(address, _Handler)
        port = self.server_address[1]
        self.allowed_hosts = {f"{name}:{port}" for name in (str(host), "127.0.0.1", "localhost")}

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address

    def handle_error(self, request, client_address):
        # A handler exception can include private data. Do not emit a traceback.
        pass


class _Handler(BaseHTTPRequestHandler):
    def parse_request(self):
        if not super().parse_request():
            return False
        hosts = self.headers.get_all("Host", [])
        if len(hosts) == 1 and hosts[0].lower() in self.server.allowed_hosts:
            return True
        self.close_connection = True
        self._json(403, {"error": "forbidden"})
        return False

    def log_message(self, format, *args):
        pass

    def end_headers(self):
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        super().end_headers()

    def send_error(self, code, message=None, explain=None):
        if code == 501 and self._path().startswith("/api/") and not self._authorized():
            return
        self._json(code, {"error": "not_found" if code == 404 else "bad_request"})

    def _respond(self, status, body=b"", content_type=None, *, cookie=None, retry_after=None):
        self.send_response(status)
        if content_type:
            self.send_header("Content-Type", content_type)
        if status != 204:
            self.send_header("Content-Length", str(len(body)))
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _json(self, status, value, **headers):
        self._respond(status, _json_bytes(value), "application/json; charset=utf-8", **headers)

    def _unavailable(self):
        self._json(503, {"error": "unavailable"}, retry_after=1)

    def _sid(self):
        cookies = SimpleCookie()
        try:
            cookies.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        return cookies["la_sid"].value if "la_sid" in cookies else None

    def _path(self):
        return self.path.partition("?")[0]

    def _authorized(self):
        if self.server.sessions.valid(self._sid()):
            return True
        self._json(401, {"error": "unauthorized"})
        return False

    def do_POST(self):
        path = self._path()
        if path in ("/api/login", "/api/logout"):
            origin = self.headers.get("Origin")
            if origin is not None and origin != "http://" + self.headers.get("Host", ""):
                self._json(403, {"error": "forbidden"})
                return
            if path == "/api/login":
                self._login()
            else:
                self.server.sessions.logout(self._sid())
                self._respond(204, cookie="la_sid=; Max-Age=0; HttpOnly; SameSite=Strict; Path=/")
        elif not path.startswith("/api/") or self._authorized():
            self._json(404, {"error": "not_found"})

    def _login(self):
        password = None
        try:
            size = _bounded_int(self.headers.get("Content-Length", ""), maximum=MAX_LOGIN_BYTES)
            value = json.loads(self.rfile.read(size).decode("utf-8"))
            if isinstance(value, dict) and isinstance(value.get("password"), str):
                password = value["password"].encode("utf-8")
        except (ValueError, RecursionError):
            pass
        sid, retry = self.server.sessions.login(password, self.client_address[0])
        if retry:
            self._json(429, {"error": "locked", "retry_after": retry}, retry_after=retry)
        elif sid is None:
            self._json(401, {"error": "invalid"})
        else:
            self._respond(204, cookie=f"la_sid={sid}; HttpOnly; SameSite=Strict; Path=/")

    def do_GET(self):
        path = self._path()
        if path in STATIC:
            filename, content_type = STATIC[path]
            try:
                body = _static_file(filename)
            except OSError:
                self._json(404, {"error": "not_found"})
            else:
                self._respond(200, body, content_type)
            return
        if path.startswith("/api/") and not self._authorized():
            return
        if path == "/api/me":
            self._json(200, {"ok": True, "server_time": self.server.clock(), "poll_ms": 1000})
            return
        if path == "/api/stream":
            self._stream()
            return
        if path != "/api/runs" and not path.startswith("/api/runs/"):
            self._json(404, {"error": "not_found"})
            return
        try:
            with _read_database(self.server.db_path) as db:
                now = self.server.clock()
                if path == "/api/runs":
                    value = _runs(db, self.path.partition("?")[2], now)
                else:
                    identifier = path.removeprefix("/api/runs/")
                    value = _detail(db, _bounded_int(identifier), now) if identifier.isascii() and identifier.isdecimal() else None
        except ValueError:
            self._json(400 if path == "/api/runs" else 404,
                       {"error": "bad_request" if path == "/api/runs" else "not_found"})
            return
        except sqlite3.Error:
            self._unavailable()
            return
        if value is None:
            self._json(404, {"error": "not_found"})
        else:
            self._json(200, value if path == "/api/runs" else _format_detail(value))

    def _event(self, name, value, cursor):
        self.wfile.write(f"id: {cursor}\nevent: {name}\ndata: ".encode("ascii") + _json_bytes(value) + b"\n\n")
        self.wfile.flush()

    def _stream(self):
        if not self.server.streams.acquire(blocking=False):
            self._unavailable()
            return
        try:
            with suppress(OSError):
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for name, value in (("TCP_KEEPIDLE", 10), ("TCP_KEEPINTVL", 5), ("TCP_KEEPCNT", 3),
                                ("TCP_USER_TIMEOUT", 30_000)):
                if hasattr(socket, name):
                    with suppress(OSError):
                        self.connection.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
            self._stream_events()
        except OSError:
            pass
        finally:
            self.server.streams.release()

    def _stream_events(self):
        deadline = time.monotonic() + STREAM_SECONDS
        sid = self._sid()
        try:
            resume = _bounded_int(self.headers.get("Last-Event-ID", ""), minimum=0)
        except ValueError:
            resume = None
        now = self.server.clock()
        try:
            with _read_database(self.server.db_path) as db:
                latest = db.execute("SELECT COALESCE(MAX(run_id), 0) FROM classification_runs").fetchone()[0]
                cursor = latest if resume is None else min(resume, latest)
                known = _recent_signatures(db, cursor, now)
        except sqlite3.Error:
            self._unavailable()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self._event("hello", {"server_time": now}, cursor)
        next_ping = time.monotonic() + PING_SECONDS
        while self.server.sessions.valid(sid):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            # This response owns the connection. Readability indicates a close
            # or another request, and either means this stream should end.
            if select.select([self.connection], [], [], min(POLL_SECONDS, remaining))[0]:
                return
            if time.monotonic() >= deadline or not self.server.sessions.valid(sid):
                return
            now = self.server.clock()
            try:
                with _read_database(self.server.db_path) as db:
                    events, next_known = self._poll(db, cursor, known, now)
            except sqlite3.Error:
                # Headers are already sent. Reconnect with Last-Event-ID.
                return
            for event, summary in events:
                if time.monotonic() >= deadline or not self.server.sessions.valid(sid):
                    return
                if event == "run.created":
                    cursor = summary["run_id"]
                self._event(event, summary, cursor)
            known = next_known
            if time.monotonic() >= next_ping:
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                next_ping = time.monotonic() + PING_SECONDS

    def _poll(self, db, cursor, known, now):
        new = db.execute(
            "SELECT * FROM classification_runs WHERE run_id > ? ORDER BY run_id LIMIT ?", (cursor, PAGE_SIZE)
        ).fetchall()
        next_cursor = new[-1]["run_id"] if new else cursor
        signatures = _recent_signatures(db, next_cursor, now)
        events = [("run.created", _summary(db, row, now)) for row in new]
        changed = (run_id for run_id, signature in signatures.items()
                   if run_id <= cursor and run_id in known and signature != known[run_id])
        # Bound the work per poll without losing deferred changes.
        for index, run_id in enumerate(changed):
            if index >= PAGE_SIZE:
                signatures[run_id] = known[run_id]
                continue
            row = db.execute("SELECT * FROM classification_runs WHERE run_id=?", (run_id,)).fetchone()
            events.append(("run.updated", _summary(db, row, now)))
        return events, signatures


def serve(config, db_path):
    if config is None:
        raise ConfigError("admin: password hash unavailable")
    with AdminServer(config.address, db_path, config.password_hash_file,
                     session_hours=config.session_hours) as server:
        server.serve_forever()
