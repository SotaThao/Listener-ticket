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
from urllib.parse import parse_qs, unquote, urlsplit

from .assistant_contract import PHASE_DECIDE
from .config import ConfigError, _validate_admin_secret_stat


POLL_SECONDS = 1
PING_SECONDS = 15
LOCK_SECONDS = 600
GLOBAL_FAILURE_LIMIT = 50
STREAM_SECONDS = 600
UPDATE_WINDOW = 1800
PAGE_SIZE = 50
THREAD_ROUND_LIMIT = 50
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
RULE_NAMES = {
    "F1": "Nhờ review PR/issue", "F2": "Giao việc nội bộ", "F3": "Tin chỉ có link",
    "F4": "Tin xã giao", "F5": "Tin đùa", "F6": "Trả lời tin xã giao",
    "R1": "Lệnh hỏi", "R2": "Lệnh trống", "R3": "Ảnh không có chữ", "R4": "Lỗi xử lý",
    "R5": "Xã giao trong ticket", "R6": "Bổ sung ticket đang mở", "R7": "Tin trò chuyện",
    "R8": "Câu hỏi không kèm việc", "R9": "Báo lỗi đủ dữ kiện", "R10": "Công việc đủ dữ kiện",
    "R11": "Góp ý đủ dữ kiện", "R12": "Thiếu dữ kiện", "R13": "Chưa xác định yêu cầu",
    "I1": "Câu hỏi có việc", "I2": "Ý định từ trợ lý", "I3": "Ý định từ lệnh",
}
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


def _thread_key(columns):
    return "COALESCE(log_thread_id, run_id)" if "log_thread_id" in columns else "run_id"


def _authority(phase):
    return "Trợ lý" if phase == PHASE_DECIDE or phase.startswith("assistant_") else "Code"


def _messages(db, run, ids, *, include_text=False):
    if run["id_space"] == "topic_ref":
        return _indexed_messages(db, run, include_text=include_text)
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


def _indexed_messages(db, run, *, include_text=True):
    columns = _columns(db, "messages")
    if not columns or not _columns(db, "classification_run_sources"):
        return []
    label = "m.chat_username" if "chat_username" in columns else "NULL AS chat_username"
    fields = "s.message_id,m.issue_url," + label
    if include_text:
        fields += ",s.chat_id,m.received_at,m.sender_name,m.text,m.decision"
        if run["id_space"] == "topic_ref":
            fields += ",s.input_id AS input_ref"
    return [dict(source) for source in db.execute(f"""
        SELECT {fields}
        FROM classification_run_sources s
        JOIN messages m ON m.chat_id=s.chat_id AND m.message_id=s.message_id
        WHERE s.run_id=? ORDER BY s.ordinal LIMIT ?""", (run["run_id"], MAX_INPUTS))]


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


def _thread_summary(db, latest, now, key):
    thread_id = dict(latest).get("log_thread_id") or latest["run_id"]
    round_count, updated_at = db.execute(
        f"SELECT COUNT(*), MAX(created_at) FROM classification_runs WHERE {key}=?", (thread_id,)
    ).fetchone()
    # Message IDs and topic references are distinct namespaces.
    message_count = db.execute(
        f"""SELECT COUNT(*) FROM (
            SELECT DISTINCT chat_id, id_space, item.value
            FROM classification_runs, json_each(CASE
                WHEN json_valid(message_ids) THEN
                    CASE WHEN json_type(message_ids)='array' THEN message_ids ELSE '[]' END
                ELSE '[]' END) AS item
            WHERE {key}=? AND item.type IN ('integer','text'))""", (thread_id,)
    ).fetchone()[0]
    result = _summary(db, latest, now)
    result.update(run_id=thread_id, latest_run_id=latest["run_id"], round_count=round_count,
                  message_count=message_count, updated_at=updated_at,
                  decided_by=_authority(latest["phase"]))
    if not result["ticket_ref"]:
        ticket = db.execute(
            f"SELECT ticket_ref FROM classification_runs WHERE {key}=? AND ticket_ref<>'' "
            "ORDER BY run_id DESC LIMIT 1", (thread_id,)
        ).fetchone()
        result["ticket_ref"] = ticket[0] if ticket else None
    return result


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
    key = _thread_key(_columns(db, "classification_runs"))
    # Filter the current round of each thread. Apply the cursor after grouping so
    # older rounds cannot reappear as separate entries on the next page.
    rows = db.execute(
        f"SELECT * FROM classification_runs{where}"
        f"{' AND' if where else ' WHERE'} run_id IN (SELECT MAX(run_id) FROM classification_runs GROUP BY {key})"
        " ORDER BY run_id DESC LIMIT ?", (*values, limit + 1)
    ).fetchall()
    return {
        "runs": [_thread_summary(db, row, now, key) for row in rows[:limit]],
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


def _round_detail(db, row, now, *, messages=None):
    ids = _list_json(row["message_ids"])
    if messages is None:
        messages = _messages(db, row, ids, include_text=True)
    run = _summary(db, row, now, ids=ids, messages=messages)
    run.update({key: row[key] for key in DETAIL_FIELDS})
    if row["id_space"] == "topic_ref":
        run["message_refs"] = ids
    if "code_precheck_json" in row.keys():
        run["code_precheck"] = _parse_json(row["code_precheck_json"])
    return run, messages, row["proposal_json"], row["outcomes_json"], _step_rows(db, row["run_id"])


def _detail(db, run_id, now):
    key = _thread_key(_columns(db, "classification_runs"))
    rows = db.execute(
        f"SELECT * FROM classification_runs WHERE {key}=COALESCE("
        f"(SELECT {key} FROM classification_runs WHERE run_id=?), ?) ORDER BY run_id DESC LIMIT ?",
        (run_id, run_id, THREAD_ROUND_LIMIT + 1),
    ).fetchall()
    if rows:
        return ([_round_detail(db, row, now) for row in reversed(rows[:THREAD_ROUND_LIMIT])],
                len(rows) > THREAD_ROUND_LIMIT)
    return None


_BATCH_SUMMARY = """SELECT b.batch_id,b.chat_id,b.chat_label,b.started_at,b.status,
    (SELECT COUNT(*) FROM collection_batch_messages m WHERE m.batch_id=b.batch_id) AS message_count
    FROM collection_batches b"""


def _batches(db):
    if not _columns(db, "collection_batches"):
        return {"batches": []}
    by_activity = _BATCH_SUMMARY + """
        WHERE (b.status IN ('collecting','queued','processing')) = ?
        ORDER BY b.started_at DESC,b.batch_id DESC"""
    batches = [dict(row) for row in db.execute(by_activity, (1,))]
    batches.extend(dict(row) for row in db.execute(by_activity + " LIMIT ?", (0, PAGE_SIZE)))
    return {"batches": batches}


def _batch_detail(db, batch_id):
    if not batch_id or len(batch_id) > 128 or not _columns(db, "collection_batches"):
        return None
    batch = db.execute(_BATCH_SUMMARY + " WHERE b.batch_id=?", (batch_id,)).fetchone()
    if batch is None:
        return None
    chat_id = batch["chat_id"]
    rows = db.execute("""SELECT bm.message_id,m.received_at,m.sender_name,m.text,m.decision
        FROM collection_batch_messages bm
        LEFT JOIN messages m ON m.chat_id=? AND m.message_id=bm.message_id
        WHERE bm.batch_id=? ORDER BY bm.ordinal""", (chat_id, batch_id))
    messages = {}
    for row in rows:
        message = dict(row, run_ids=[])
        message["received_at"] = _timestamp(message["received_at"])
        messages[row["message_id"]] = message
    if _columns(db, "classification_run_sources"):
        # Drive the lookup from batch members so polling never scans a chat's history.
        links = db.execute("""SELECT DISTINCT bm.message_id,s.run_id
            FROM collection_batch_messages bm
            CROSS JOIN classification_run_sources s ON s.chat_id=? AND s.message_id=bm.message_id
            JOIN classification_runs r ON r.run_id=s.run_id
            WHERE bm.batch_id=? ORDER BY s.run_id""", (chat_id, batch_id))
        for row in links:
            messages[row["message_id"]]["run_ids"].append(row["run_id"])
    return {"batch": dict(batch), "messages": list(messages.values())}


def _exact_round(db, run_id, now):
    columns = _columns(db, "classification_runs")
    if not columns:
        return None
    row = db.execute("SELECT * FROM classification_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return None
    thread = row["log_thread_id"] if "log_thread_id" in columns else None
    key = _thread_key(columns)
    round_index = db.execute(
        f"SELECT COUNT(*) FROM classification_runs WHERE {key}=? AND run_id<=?",
        (thread if thread is not None else run_id, run_id),
    ).fetchone()[0]
    inputs = _indexed_messages(db, row)
    return {**_format_round(_round_detail(db, row, now, messages=inputs or None)), "round_index": round_index}


def _format_round(detail):
    run, messages, proposal, outcomes, step_rows = detail
    run.update(proposal=_parse_json(proposal), outcomes=_parse_json(outcomes))
    if "code_precheck" in run or run["decision_rule"] or any(
        isinstance(group, dict) and group.get("rule")
        for group in (run["outcomes"] if isinstance(run["outcomes"], list) else ())
    ):
        _present_rules(run)
    for message in messages:
        message.pop("chat_username")
        message["received_at"] = _timestamp(message["received_at"])
    return {"run": run, "inputs": messages, "steps": _steps(*step_rows)}


def _rule_label(code, text=None, *, name=None):
    if not isinstance(code, str) or not code:
        return None
    return f"{code} · {name or RULE_NAMES.get(code, 'Quy tắc xử lý')} · {text or 'Không có nguyên văn trong nhật ký'}"


def _present_rules(run):
    trace = run.get("code_precheck")
    trace = trace if isinstance(trace, dict) else {}
    decided = trace.get("decided_rule")
    checks = trace.get("checks")
    checks = [check for check in checks if isinstance(check, dict) and isinstance(check.get("rule"), str)
              ] if isinstance(checks, list) else []
    match = next((check for check in checks if check["rule"] == decided), {})
    name = match.get("name") or (RULE_NAMES.get(decided, "Quy tắc xử lý") if isinstance(decided, str) else None)
    rule_text = trace.get("rule_text")
    reason = trace.get("reason") or "Chưa ghi lý do"
    if trace.get("handled_by_code") is True:
        rule = f"{decided} · {name}" if name else "Chưa ghi luật"
        run["precheck_summary"] = f"Code đã quyết — {rule}: {reason}"
        run["precheck_rule"] = _rule_label(decided, rule_text, name=name)
    elif trace.get("handled_by_code") is False:
        run["precheck_summary"] = f"Code không xử lý được — Lý do: {reason} ⇒ Gửi lên trợ lý"
    run["precheck_checks"] = [
        dict(check, rule_display=_rule_label(check["rule"],
             rule_text if check["rule"] == decided else check.get("rule_text"), name=check.get("name")))
        for check in checks
    ]
    main_rule = run["decision_rule"]
    outcomes = run["outcomes"]
    groups = [group for group in outcomes if isinstance(group, dict)] if isinstance(outcomes, list) else []
    matching = next((group for group in groups if group.get("rule") == main_rule and group.get("rule_text")), {})
    run["rule_display"] = _rule_label(main_rule, matching.get("rule_text") or (rule_text if main_rule == decided else None),
                                      name=name if main_rule == decided else None)
    run["outcome_rule_displays"] = [
        _rule_label(group.get("rule"), group.get("rule_text"),
                    name=name if group.get("rule") == decided else None) for group in groups
    ]


def _format_detail(detail):
    items, truncated = detail
    rounds = [_format_round(item) for item in items]
    latest = rounds[-1]
    if len(rounds) == 1 and "code_precheck" not in latest["run"]:
        return latest  # Preserve the historical response on a legacy database.
    return {**latest, "rounds": rounds, "truncated": truncated}


def _recent_signatures(db, cursor, now):
    key = _thread_key(_columns(db, "classification_runs"))
    return {
        row["run_id"]: tuple(row[field] for field in (*UPDATE_FIELDS, "thread_id"))
        for row in db.execute(
            f"SELECT run_id, {', '.join(UPDATE_FIELDS)}, {key} AS thread_id FROM classification_runs "
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
        route, _, identifier = path.removeprefix("/api/").partition("/")
        if not path.startswith("/api/") or route not in ("runs", "batches", "rounds"):
            self._json(404, {"error": "not_found"})
            return
        try:
            with _read_database(self.server.db_path) as db:
                now = self.server.clock()
                if path == "/api/runs":
                    value = _runs(db, self.path.partition("?")[2], now)
                elif path == "/api/batches":
                    value = _batches(db)
                elif route == "batches":
                    value = _batch_detail(db, unquote(identifier))
                elif identifier.isascii() and identifier.isdecimal():
                    run_id = _bounded_int(identifier)
                    if route == "rounds":
                        value = _exact_round(db, run_id, now)
                    else:
                        detail = _detail(db, run_id, now)
                        value = _format_detail(detail) if detail is not None else None
                else:
                    value = None
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
            self._json(200, value)

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
            for event, summary, event_cursor in events:
                if time.monotonic() >= deadline or not self.server.sessions.valid(sid):
                    return
                cursor = event_cursor
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
        key = _thread_key(_columns(db, "classification_runs"))
        events = []
        for row in new:
            summary = _thread_summary(db, row, now, key)
            earlier = db.execute(
                f"SELECT 1 FROM classification_runs WHERE {key}=? AND run_id<? LIMIT 1",
                (summary["run_id"], row["run_id"]),
            ).fetchone()
            event = "run.updated" if earlier else "run.created"
            events.append((event, summary, row["run_id"]))
        changed = (run_id for run_id, signature in signatures.items()
                   if run_id <= cursor and run_id in known and signature != known[run_id])
        # Bound the work per poll without losing deferred changes.
        for index, run_id in enumerate(changed):
            if index >= PAGE_SIZE:
                signatures[run_id] = known[run_id]
                continue
            thread_id = signatures[run_id][-1]
            row = db.execute(
                f"SELECT * FROM classification_runs WHERE {key}=? ORDER BY run_id DESC LIMIT 1",
                (thread_id,),
            ).fetchone()
            summary = _thread_summary(db, row, now, key)
            if thread_id != known[run_id][-1] and known[run_id][-1] == run_id:
                summary["replaced_thread_id"] = run_id
            events.append(("run.updated", summary, next_cursor))
        return events, signatures


def serve(config, db_path):
    if config is None:
        raise ConfigError("admin: password hash unavailable")
    with AdminServer(config.address, db_path, config.password_hash_file,
                     session_hours=config.session_hours) as server:
        server.serve_forever()
