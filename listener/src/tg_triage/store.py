"""Durable message deduplication, result storage and owner CSV export."""

import csv
import os
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import IncomingMessage
from .followup_store import FollowupStoreMixin
from .topic_store import TopicStoreMixin
from .autonomy_store import AutonomyStoreMixin
from .config import ConfigError

USAGE_RETENTION_DAYS = 90
GITHUB_AMBIGUOUS_ERROR = "GitHub có thể đã tạo issue — kiểm tra repo trước khi tạo lại"

COLUMNS = (
    "chat_id", "message_id", "received_at", "sender_name", "text", "tier",
    "type", "confidence", "title", "summary", "priority", "decision",
    "issue_number", "issue_url", "error", "processed_at", "latency_ms",
    "github_called_at", "chat_username", "attempts", "case_key", "group_key", "reason",
    "mentions_listener", "model_reason",
)

# Decisions that end a case. Everything else (queued, classifying, in_case...) is transient.
# 12.11: `chat` (talk with no work for the team) and `pending_review` (looks like work but was not
# called directly, or the model was unsure) never reach GitHub; both are exported and reconciled.
TERMINAL_DECISIONS = frozenset((
    "noise", "chat", "pending_review", "issue", "needs_human", "dry_run_issue",
    "dry_run_needs_human", "rate_limited", "error"))
CASE_STATUS = {"noise": "noise", "chat": "chat", "pending_review": "pending_review",
               "rate_limited": "error", "error": "error"}
# A window that ends in one of these never takes late messages as follow-ups (no issue to follow).
NO_TICKET_DECISIONS = frozenset(("noise", "chat", "pending_review"))
# Columns added after the first release; each is added with a safe ALTER.
CASE_MESSAGE_COLUMNS = (
    ("case_key", "TEXT"), ("sender_id", "INTEGER"), ("reply_to_id", "INTEGER"),
    ("is_forward", "INTEGER NOT NULL DEFAULT 0"), ("has_image", "INTEGER NOT NULL DEFAULT 0"),
    ("image_size", "INTEGER"), ("image_mime", "TEXT"),
    # 12.14: document attachment metadata (declared size / MIME / file name; never the content).
    ("has_document", "INTEGER NOT NULL DEFAULT 0"), ("document_size", "INTEGER"),
    ("document_mime", "TEXT"), ("document_name", "TEXT"),
    # 12.9: the topic group a message belongs to, and why a message was set aside as noise.
    ("group_key", "TEXT"), ("reason", "TEXT"),
    # 12.10: reactions this account already placed on the message (heart on receipt / OK hand
    # on issue), so a message never gets the same reaction twice, restarts included.
    ("reacted_received", "INTEGER NOT NULL DEFAULT 0"), ("reacted_ok", "INTEGER NOT NULL DEFAULT 0"),
    # 12.11: Telegram says this message tagged the listener account (a direct call).
    ("mentions_listener", "INTEGER NOT NULL DEFAULT 0"),
    # 12.12: the model's own short reason for its verdict, for the reviewer (never sent to GitHub).
    ("model_reason", "TEXT"),
)
# 12.11: columns added to `reconciliation` after its first release.
RECONCILIATION_COLUMNS = (("chat", "INTEGER NOT NULL DEFAULT 0"),
                          ("pending_review", "INTEGER NOT NULL DEFAULT 0"))
REACTION_COLUMNS = {"received": "reacted_received", "ok": "reacted_ok"}
RECONCILIATION_ERROR = "Đối soát lệch: số tin không khớp trạng thái đã ghi, cần người xem"


@dataclass(frozen=True)
class Reconciliation:
    """Per-window tally (12.9, 12.11): received must equal
    issue + noise + needs_human + chat + pending_review."""

    case_key: str
    received: int
    issue: int
    noise: int
    needs_human: int
    failed: int
    groups: int
    issues_created: int
    status: str
    chat: int = 0
    pending_review: int = 0

    @property
    def accounted(self) -> int:
        return self.issue + self.noise + self.needs_human + self.chat + self.pending_review

    @property
    def balanced(self) -> bool:
        return self.received == self.accounted


@dataclass(frozen=True)
class CaseAssignment:
    case_key: str
    first_message_id: int
    created: bool
    followup: bool
    message_count: int


def utc_now():
    return datetime.now(timezone.utc)


class Store(FollowupStoreMixin, TopicStoreMixin, AutonomyStoreMixin):
    """One service owns a database; a lock allows sequential thread offloading."""

    def __init__(self, path, *, clock=utc_now, recover_pending=True, read_only=False):
        self._read_only = read_only
        self.clock = clock
        self._lock = threading.RLock()
        self._usage_pruned_at = 0.0
        if read_only:
            try:
                uri = Path(path).resolve().as_uri() + "?mode=ro"
                self.connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
                self.connection.row_factory = sqlite3.Row
            except (OSError, sqlite3.Error):
                raise ConfigError("Cannot open database for read-only export; check that it exists") from None
            return
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        if str(path) != ":memory:":
            Path(path).chmod(0o600)
        with self.connection:
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    received_at TEXT NOT NULL, sender_name TEXT NOT NULL,
                    text TEXT NOT NULL, tier TEXT NOT NULL,
                    type TEXT, confidence REAL, title TEXT, summary TEXT, priority TEXT,
                    decision TEXT NOT NULL, issue_number INTEGER, issue_url TEXT,
                    error TEXT, processed_at TEXT, latency_ms INTEGER,
                    github_called_at TEXT, chat_username TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, message_id)
                )
            """)
            existing = {row[1] for row in self.connection.execute("PRAGMA table_info(messages)")}
            for name in ("github_called_at", "chat_username"):
                if name not in existing:
                    self.connection.execute(f"ALTER TABLE messages ADD COLUMN {name} TEXT")
            if "attempts" not in existing:
                self.connection.execute("ALTER TABLE messages ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
            for name, definition in CASE_MESSAGE_COLUMNS:
                if name not in existing:
                    self.connection.execute(f"ALTER TABLE messages ADD COLUMN {name} {definition}")
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_key TEXT NOT NULL UNIQUE,
                    chat_id INTEGER NOT NULL, first_message_id INTEGER NOT NULL,
                    status TEXT NOT NULL, has_command INTEGER NOT NULL DEFAULT 0,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL, last_message_at TEXT NOT NULL, closed_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, github_called_at TEXT,
                    issue_number INTEGER, issue_url TEXT, decision TEXT, error TEXT
                )
            """)
            self.connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_case ON messages(case_key)")
            self.connection.execute("CREATE INDEX IF NOT EXISTS idx_messages_group ON messages(group_key)")
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_key TEXT NOT NULL UNIQUE,
                    case_key TEXT NOT NULL,
                    chat_id INTEGER NOT NULL, first_message_id INTEGER NOT NULL,
                    message_count INTEGER NOT NULL,
                    status TEXT NOT NULL, decision TEXT, tier TEXT, type TEXT, reason TEXT,
                    issue_number INTEGER, issue_url TEXT, github_called_at TEXT, error TEXT,
                    created_at TEXT NOT NULL, closed_at TEXT
                )
            """)
            self.connection.execute("CREATE INDEX IF NOT EXISTS idx_groups_case ON groups(case_key)")
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS reconciliation (
                    case_key TEXT PRIMARY KEY,
                    received INTEGER NOT NULL, issue INTEGER NOT NULL, noise INTEGER NOT NULL,
                    needs_human INTEGER NOT NULL, failed INTEGER NOT NULL,
                    groups INTEGER NOT NULL, issues_created INTEGER NOT NULL,
                    balanced INTEGER NOT NULL, status TEXT NOT NULL, checked_at TEXT NOT NULL,
                    chat INTEGER NOT NULL DEFAULT 0, pending_review INTEGER NOT NULL DEFAULT 0
                )
            """)
            tallied = {row[1] for row in self.connection.execute("PRAGMA table_info(reconciliation)")}
            for name, definition in RECONCILIATION_COLUMNS:
                if name not in tallied:
                    self.connection.execute(f"ALTER TABLE reconciliation ADD COLUMN {name} {definition}")
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_key TEXT NOT NULL, message_id INTEGER NOT NULL,
                    asset_name TEXT NOT NULL, status TEXT NOT NULL, called_at TEXT NOT NULL
                )
            """)
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS group_notices (
                    chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL,
                    reserved_at TEXT NOT NULL, finished_at TEXT, notice_message_id INTEGER,
                    PRIMARY KEY (chat_id, message_id)
                )
            """)
            # Model-call accounting: counts, cost and timing only, never message text, images or keys.
            self.connection.execute("""
                CREATE TABLE IF NOT EXISTS model_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
                    role TEXT NOT NULL, outcome TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_hit_tokens INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
                    cost REAL NOT NULL DEFAULT 0
                )
            """)
            self.connection.execute("CREATE INDEX IF NOT EXISTS idx_model_usage_ts ON model_usage(ts)")
            if "github_called_at" not in existing:
                # Old ambiguous errors may already have created issues. Reserve
                # conservatively during the migration rather than reset quota.
                self.connection.execute("""
                    UPDATE messages SET github_called_at = COALESCE(processed_at, received_at)
                    WHERE decision IN ('issue', 'needs_human', 'error', 'pending')
                """)
            self.init_followups(recover_pending)
            from .thinking import init_store
            init_store(self)
            if recover_pending:
                self._recover_cases()
                self.connection.execute("""
                    UPDATE messages SET decision = 'error',
                    error = 'Interrupted processing; review before any manual retry',
                    processed_at = ?, latency_ms = COALESCE(latency_ms, 0)
                    WHERE decision = 'pending'
                """, (self.clock().isoformat(),))
                self.connection.execute("""
                    UPDATE messages SET decision='error', error='Lặp lỗi khi xử lý, cần người xem',
                    type=NULL, confidence=NULL, title=NULL, summary=NULL, priority=NULL,
                    processed_at=? WHERE decision IN ('queued', 'classifying') AND attempts >= 2
                """, (self.clock().isoformat(),))
                self.connection.execute("""
                    UPDATE messages SET decision='queued' WHERE decision='classifying' AND attempts < 2
                """)

    def reserve_group_notice(self, chat_id, message_id, kind):
        """Commit the only transport attempt before sending; reserved is never retried."""
        with self._lock, self.connection:
            cursor = self.connection.execute("""
                INSERT OR IGNORE INTO group_notices
                (chat_id, message_id, kind, status, reserved_at)
                VALUES (?, ?, ?, 'reserved', ?)
            """, (chat_id, message_id, kind, self.clock().isoformat()))
            return cursor.rowcount == 1

    def finish_group_notice(self, chat_id, message_id, status, notice_message_id=None):
        if status not in ("sent", "failed"):
            raise ValueError("Invalid group notice status")
        with self._lock, self.connection:
            self.connection.execute("""
                UPDATE group_notices SET status=?, finished_at=?, notice_message_id=?
                WHERE chat_id=? AND message_id=? AND status='reserved'
            """, (status, self.clock().isoformat(), notice_message_id, chat_id, message_id))

    def group_notice_reply_target(self, chat_id, reply_to_id):
        """Map replies to our outgoing notice back to the group's original message."""
        with self._lock:
            episode=self.connection.execute('''SELECT e.anchor_message_id FROM group_notices n
                JOIN followup_jobs j ON j.chat_id=n.chat_id AND j.message_id=n.message_id
                JOIN followup_episodes e ON e.job_id=j.id AND e.chat_id=n.chat_id
                WHERE n.chat_id=? AND n.notice_message_id=? AND n.status='sent'
                ORDER BY e.created_at DESC LIMIT 1''',(chat_id,reply_to_id)).fetchone()
            if episode:
                return episode[0]
            row = self.connection.execute("""
                SELECT message_id FROM group_notices
                WHERE chat_id=? AND notice_message_id=? AND status='sent'
            """, (chat_id, reply_to_id)).fetchone()
            if row:
                return row[0]
            if getattr(self,'_autonomy_enabled',False):
                return self.autonomy_notice_reply_target(chat_id,reply_to_id)
            return reply_to_id

    def record_model_usage(self, ts, provider, model, role, outcome, prompt_tokens=0,
                           completion_tokens=0, cache_hit_tokens=0, latency_ms=0, cost=0.0):
        """One row per model call (errors and fallbacks included). Old rows are pruned after 90 days."""
        def count(value):
            return value if type(value) is int and value >= 0 else 0
        with self._lock, self.connection:
            self.connection.execute(
                "INSERT INTO model_usage (ts, provider, model, role, outcome, prompt_tokens,"
                " completion_tokens, cache_hit_tokens, latency_ms, cost) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (float(ts), str(provider)[:200], str(model)[:200], str(role)[:20], str(outcome)[:60],
                 count(prompt_tokens), count(completion_tokens), count(cache_hit_tokens),
                 count(latency_ms), float(cost)))
            if float(ts) - self._usage_pruned_at >= 3600:
                self._usage_pruned_at = float(ts)
                self.connection.execute("DELETE FROM model_usage WHERE ts < ?",
                                        (float(ts) - USAGE_RETENTION_DAYS * 86400,))

    def model_usage_since(self, since):
        """Rows with ts >= since, oldest first, as plain tuples (no message content exists here)."""
        with self._lock:
            return [tuple(row) for row in self.connection.execute(
                "SELECT ts, provider, model, role, outcome, prompt_tokens, completion_tokens,"
                " cache_hit_tokens, cost FROM model_usage WHERE ts >= ? ORDER BY ts, id", (float(since),))]

    def close(self):
        with self._lock:
            self.connection.close()

    def get(self, chat_id, message_id):
        with self._lock:
            return self.connection.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id)).fetchone()

    def claim(self, message) -> bool:
        """Compatibility name for durable queued ingress, without a side effect."""
        return self.enqueue(message)

    def enqueue(self, message) -> bool:
        with self._lock, self.connection:
            cursor = self.connection.execute("""
                INSERT OR IGNORE INTO messages
                (chat_id, message_id, received_at, sender_name, text, tier, decision, chat_username,
                 sender_id, reply_to_id, is_forward, has_image, image_size, image_mime,
                 mentions_listener, has_document, document_size, document_mime, document_name)
                VALUES (?, ?, ?, ?, ?, 'none', 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (message.chat_id, message.message_id, message.received_at.isoformat(),
                  message.sender_name, message.text, message.chat_username,
                  message.sender_id, message.reply_to_id, int(bool(message.is_forward)),
                  int(bool(message.has_image)), message.image_size, message.image_mime,
                  int(bool(message.mentions_listener)), int(bool(message.has_document)),
                  message.document_size, message.document_mime, message.document_name))
            return cursor.rowcount == 1

    def reaction_done(self, chat_id, message_id, kind) -> bool:
        """True when reaction `kind` ("received" heart / "ok" OK hand) was already placed (12.10)."""
        column = REACTION_COLUMNS[kind]
        with self._lock:
            row = self.connection.execute(
                f"SELECT {column} FROM messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id)).fetchone()
        return row is not None and bool(row[0])

    def mark_reaction(self, chat_id, message_id, kind) -> None:
        """Record that reaction `kind` was successfully placed on the message (12.10)."""
        column = REACTION_COLUMNS[kind]
        with self._lock, self.connection:
            self.connection.execute(
                f"UPDATE messages SET {column} = 1 WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id))

    def update_metadata(self, message):
        with self._lock, self.connection:
            self.connection.execute("""
                UPDATE messages SET sender_name=?, chat_username=?
                WHERE chat_id=? AND message_id=? AND decision='queued'
            """, (message.sender_name, message.chat_username, message.chat_id, message.message_id))

    def start_classifying(self, message) -> bool:
        self.enqueue(message)
        with self._lock, self.connection:
            cursor = self.connection.execute("""
                UPDATE messages SET decision='classifying', attempts=attempts+1
                WHERE chat_id=? AND message_id=? AND decision='queued' AND attempts < 2
            """, (message.chat_id, message.message_id))
            return cursor.rowcount == 1

    def queued_messages(self):
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM messages WHERE decision='queued' ORDER BY received_at, chat_id, message_id"
            ).fetchall()
        return [_to_message(row) for row in rows]

    def reserve_github_call(self, message, limit) -> bool:
        """Atomically commit quota consumption immediately before the HTTP call.

        For a message that belongs to a topic group (12.9) the reservation is taken for that
        group: only the group's first message carries `github_called_at`, so one issue consumes
        one unit of quota and a group can be reserved only once; the window is stamped too, so
        a restart never re-plans a window that may already have posted. A message of a case
        without groups reserves the whole case, as before.
        """
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                if self.count_recent_issues() >= limit:
                    self.connection.rollback()
                    return False
                now = self.clock().isoformat()
                row = self.connection.execute(
                    "SELECT case_key, group_key FROM messages WHERE chat_id = ? AND message_id = ?",
                    (message.chat_id, message.message_id)).fetchone()
                if row is not None and row["group_key"] is not None:
                    claimed = self.connection.execute("""
                        UPDATE groups SET github_called_at=?, status='analyzing'
                        WHERE group_key=? AND status='planned' AND github_called_at IS NULL
                    """, (now, row["group_key"]))
                    if claimed.rowcount != 1:
                        self.connection.rollback()
                        return False
                    self.connection.execute("""
                        UPDATE cases SET github_called_at=COALESCE(github_called_at, ?)
                        WHERE case_key=? AND status='analyzing'
                    """, (now, row["case_key"]))
                elif row is not None and row["case_key"] is not None:
                    claimed = self.connection.execute("""
                        UPDATE cases SET github_called_at=?
                        WHERE case_key=? AND status='analyzing' AND github_called_at IS NULL
                    """, (now, row["case_key"]))
                    if claimed.rowcount != 1:
                        self.connection.rollback()
                        return False
                cursor = self.connection.execute("""
                    UPDATE messages SET decision='pending', github_called_at=?
                    WHERE chat_id=? AND message_id=? AND decision IN ('classifying', 'in_case')
                """, (now, message.chat_id, message.message_id))
                self.connection.commit()
                return cursor.rowcount == 1
            except Exception:
                self.connection.rollback()
                raise

    def finish(self, message, result):
        """Store a result on a message. A terminal result also settles the rest of its group
        (12.9; messages and group row) or, for a message of a case without groups, the case."""
        classification = result.classification
        fields = ((classification.type, classification.confidence, classification.title,
                   classification.summary, classification.priority)
                  if classification is not None else (None,) * 5)
        with self._lock, self.connection:
            self.connection.execute("""
                UPDATE messages SET tier = ?, type = ?, confidence = ?, title = ?,
                summary = ?, priority = ?, decision = ?, issue_number = ?, issue_url = ?,
                error = ?, processed_at = ?, latency_ms = ?, reason = ?, model_reason = ?
                WHERE chat_id = ? AND message_id = ?
            """, (result.tier, *fields, result.decision, result.issue_number, result.issue_url,
                  result.error, self.clock().isoformat(), result.latency_ms, result.reason,
                  result.model_reason, message.chat_id, message.message_id))
            row = self.connection.execute(
                "SELECT case_key, group_key FROM messages WHERE chat_id = ? AND message_id = ?",
                (message.chat_id, message.message_id)).fetchone()
            if row is not None and result.decision in TERMINAL_DECISIONS:
                if row["group_key"] is not None:
                    self._finish_group(row["group_key"], message, result, fields)
                elif row["case_key"] is not None:
                    self._finish_case(row["case_key"], message, result, fields)
                self.project_topics(message.chat_id, row["case_key"])

    def _finish_group(self, group_key, message, result, fields):
        """Close one topic group and mirror its outcome onto the group's unfinished messages."""
        now = self.clock().isoformat()
        self.connection.execute("""
            UPDATE groups SET status = ?, decision = ?, tier = ?, type = ?, reason = ?,
            issue_number = ?, issue_url = ?, error = ?, closed_at = ? WHERE group_key = ?
        """, (CASE_STATUS.get(result.decision, "done"), result.decision, result.tier, fields[0],
              result.reason, result.issue_number, result.issue_url, result.error, now, group_key))
        self.connection.execute("""
            UPDATE messages SET tier = ?, type = ?, confidence = ?, title = ?, summary = ?,
            priority = ?, decision = ?, issue_number = ?, issue_url = ?, error = ?,
            processed_at = ?, latency_ms = ?, reason = ?, model_reason = ?
            WHERE group_key = ? AND NOT (chat_id = ? AND message_id = ?)
            AND decision IN ('in_case', 'classifying', 'pending')
        """, (result.tier, *fields, result.decision, result.issue_number, result.issue_url,
              result.error, now, result.latency_ms, result.reason, result.model_reason, group_key,
              message.chat_id, message.message_id))

    def _finish_case(self, case_key, message, result, fields):
        """Close the case and mirror the outcome onto its other unfinished messages."""
        now = self.clock().isoformat()
        self.connection.execute("""
            UPDATE cases SET status = ?, decision = ?, issue_number = ?, issue_url = ?,
            error = ?, closed_at = ? WHERE case_key = ?
        """, (CASE_STATUS.get(result.decision, "done"), result.decision, result.issue_number,
              result.issue_url, result.error, now, case_key))
        self.connection.execute("""
            UPDATE messages SET tier = ?, type = ?, confidence = ?, title = ?, summary = ?,
            priority = ?, decision = ?, issue_number = ?, issue_url = ?, error = ?,
            processed_at = ?, latency_ms = ?, model_reason = ?
            WHERE case_key = ? AND NOT (chat_id = ? AND message_id = ?)
            AND decision IN ('in_case', 'classifying', 'pending')
        """, (result.tier, *fields, result.decision, result.issue_number, result.issue_url,
              result.error, now, result.latency_ms, result.model_reason, case_key,
              message.chat_id, message.message_id))

    def fail_message(self, message, exception_name):
        """Quarantine and flag any durable GitHub reservation for manual review.

        A message whose case is being analyzed takes the whole case with it, so no
        sibling stays `in_case` or `pending` forever.
        """
        with self._lock, self.connection:
            self.connection.execute("""
                UPDATE messages SET decision='error', type=NULL, confidence=NULL,
                title=NULL, summary=NULL, priority=NULL,
                error=CASE WHEN github_called_at IS NOT NULL THEN ? || ' (' || ? || ')'
                           ELSE ? END, processed_at=?
                WHERE chat_id=? AND message_id=?
            """, (GITHUB_AMBIGUOUS_ERROR, exception_name, exception_name, self.clock().isoformat(),
                  message.chat_id, message.message_id))
            row = self.get(message.chat_id, message.message_id)
            if row is not None and row["group_key"] is not None:
                # 12.9: one failing group never takes the window's other groups with it.
                return self._fail_group_locked(row["group_key"], exception_name)
            if row is not None and row["case_key"] is not None:
                error = self._fail_case_locked(row["case_key"], exception_name, message)
                if error is not None:
                    return error
            return row["error"] if row is not None else exception_name

    def _fail_group_locked(self, group_key, exception_name):
        group = self.connection.execute(
            "SELECT * FROM groups WHERE group_key = ?", (group_key,)).fetchone()
        error = (f"{GITHUB_AMBIGUOUS_ERROR} ({exception_name})"
                 if group["github_called_at"] is not None else exception_name)
        now = self.clock().isoformat()
        self.connection.execute(
            "UPDATE groups SET status='error', decision='error', error=?, closed_at=? WHERE group_key=?",
            (error, now, group_key))
        self.connection.execute("""
            UPDATE messages SET decision='error', type=NULL, confidence=NULL, title=NULL,
            summary=NULL, priority=NULL, error=?, processed_at=?
            WHERE group_key=? AND decision IN ('in_case', 'classifying', 'pending')
        """, (error, now, group_key))
        return error

    def fail_case(self, case_key, exception_name):
        """Quarantine a case that has no triggering message (periodic close path)."""
        with self._lock, self.connection:
            return self._fail_case_locked(case_key, exception_name, None)

    def _fail_case_locked(self, case_key, exception_name, trigger):
        case = self._case(case_key)
        if case is None or case["status"] != "analyzing":
            return None
        error = (f"{GITHUB_AMBIGUOUS_ERROR} ({exception_name})"
                 if case["github_called_at"] is not None else exception_name)
        self._mark_case_error(case_key, error, trigger)
        return error

    def _mark_case_error(self, case_key, error, trigger=None):
        now = self.clock().isoformat()
        self.connection.execute(
            "UPDATE cases SET status='error', error=?, closed_at=? WHERE case_key=?",
            (error, now, case_key))
        trigger_id = trigger.message_id if trigger is not None else None
        self.connection.execute("""
            UPDATE groups SET status='error', error=?, closed_at=?
            WHERE case_key=? AND status IN ('planned', 'analyzing')
        """, (error, now, case_key))
        self.connection.execute("""
            UPDATE messages SET decision='error', type=NULL, confidence=NULL, title=NULL,
            summary=NULL, priority=NULL, error=?, processed_at=?
            WHERE case_key=? AND (decision IN ('in_case', 'pending', 'classifying') OR message_id=?)
        """, (error, now, case_key, trigger_id))

    # ---- cases (Assistant definition 12.1 - 12.3) -------------------------------------

    def _case(self, case_key):
        return self.connection.execute(
            "SELECT * FROM cases WHERE case_key = ?", (case_key,)).fetchone()

    def get_case(self, case_key):
        with self._lock:
            return self._case(case_key)

    def _recover_cases(self):
        """Never call GitHub twice for a case; re-analyze only when no call was made."""
        rows = self.connection.execute(
            "SELECT case_key, github_called_at, attempts FROM cases WHERE status='analyzing'"
        ).fetchall()
        for row in rows:
            if row["github_called_at"] is not None:
                self._mark_case_error(row["case_key"], GITHUB_AMBIGUOUS_ERROR + " (Interrupted)")
            elif row["attempts"] >= 2:
                self._mark_case_error(row["case_key"], "Lặp lỗi khi xử lý, cần người xem")
            else:
                self.connection.execute(
                    "UPDATE cases SET status='open' WHERE case_key=?", (row["case_key"],))
                # No group reached GitHub, so the whole plan is discarded and made again.
                self.connection.execute("DELETE FROM groups WHERE case_key=?", (row["case_key"],))
                self.connection.execute("""
                    UPDATE messages SET decision='in_case', group_key=NULL, reason=NULL, model_reason=NULL
                    WHERE case_key=? AND decision NOT IN ('error', 'followup')
                """, (row["case_key"],))

    def assign_case(self, message, *, window_minutes, max_messages, is_command=False, fresh=False) -> CaseAssignment:
        """Attach a message to exactly one case, atomically and idempotently.

        Rules (12.1): a reply joins the case of the replied message; otherwise, when
        the window is enabled, the latest case of the chat whose last message is within
        `window_minutes` and that has the same sender (including forwards).
        A command never joins a finished case as a follow-up (it is an explicit new
        request) unless it is a direct reply. Full cases accept nothing more.
        """
        with self._lock, self.connection:
            row = self.get(message.chat_id, message.message_id)
            if row is not None and row["case_key"] is not None:
                case = self._case(row["case_key"])
                return CaseAssignment(case["case_key"], case["first_message_id"], False,
                                      row["decision"] == "followup", case["message_count"])
            notice = self._notice_followup(message) if not fresh else None
            if notice is not None:
                return notice
            target = self._find_case(message, window_minutes, max_messages, is_command) if not fresh else None
            created = target is None
            if created:
                key = f"{message.chat_id}:{message.message_id}"
                self.connection.execute("""
                    INSERT INTO cases (case_key, chat_id, first_message_id, status,
                                       created_at, last_message_at)
                    VALUES (?, ?, ?, 'open', ?, ?)
                """, (key, message.chat_id, message.message_id,
                      message.received_at.isoformat(), message.received_at.isoformat()))
                target = self._case(key)
            followup = target["status"] == "done"
            last = max(_aware(datetime.fromisoformat(target["last_message_at"])),
                       _aware(message.received_at))
            self.connection.execute("""
                UPDATE cases SET message_count = message_count + 1, last_message_at = ?,
                has_command = CASE WHEN ? THEN 1 ELSE has_command END WHERE case_key = ?
            """, (last.isoformat(), int(bool(is_command)), target["case_key"]))
            if followup:
                number, url = target["issue_number"], target["issue_url"]
                if message.reply_to_id is not None:
                    # A window can hold several issues (12.9): follow the one that was replied to.
                    replied = self.connection.execute("""
                        SELECT issue_number, issue_url FROM messages
                        WHERE chat_id = ? AND message_id = ? AND case_key = ?
                        AND issue_number IS NOT NULL
                    """, (message.chat_id, message.reply_to_id, target["case_key"])).fetchone()
                    if replied is not None:
                        number, url = replied["issue_number"], replied["issue_url"]
                self.connection.execute("""
                    UPDATE messages SET case_key = ?, decision = 'followup', tier = 'none',
                    issue_number = ?, issue_url = ?, processed_at = ?
                    WHERE chat_id = ? AND message_id = ?
                """, (target["case_key"], number, url,
                      self.clock().isoformat(), message.chat_id, message.message_id))
            else:
                self.connection.execute("""
                    UPDATE messages SET case_key = ?, decision = 'in_case'
                    WHERE chat_id = ? AND message_id = ?
                """, (target["case_key"], message.chat_id, message.message_id))
            return CaseAssignment(target["case_key"], target["first_message_id"], created,
                                  followup, target["message_count"] + 1)

    def _notice_followup(self, message):
        """A reply to a sent clarification is support data, never a fresh GitHub attempt.

        Ingress maps outgoing notice ids to original ids. Accept either form here so
        durable replay and direct store callers retain the same safe association.
        This explicit notice-backed path bypasses normal case size/status gates.
        """
        if message.reply_to_id is None:
            return None
        source = self.connection.execute("""
            SELECT m.case_key, m.group_key, m.issue_number, m.issue_url,
                   c.first_message_id, c.message_count, c.last_message_at
            FROM group_notices n
            JOIN messages m ON m.chat_id=n.chat_id AND m.message_id=n.message_id
            JOIN cases c ON c.case_key=m.case_key
            WHERE n.chat_id=? AND n.status='sent' AND n.kind!='context_pending'
              AND (n.message_id=? OR n.notice_message_id=?)
        """, (message.chat_id, message.reply_to_id, message.reply_to_id)).fetchone()
        if source is None:
            return None
        last = max(_aware(datetime.fromisoformat(source["last_message_at"])),
                   _aware(message.received_at))
        self.connection.execute("""
            UPDATE cases SET message_count=message_count+1, last_message_at=?
            WHERE case_key=?
        """, (last.isoformat(), source["case_key"]))
        self.connection.execute("""
            UPDATE messages SET case_key=?, group_key=?, decision='followup', tier='none',
                                issue_number=?, issue_url=?, processed_at=?
            WHERE chat_id=? AND message_id=?
        """, (source["case_key"], source["group_key"], source["issue_number"], source["issue_url"],
              self.clock().isoformat(), message.chat_id, message.message_id))
        return CaseAssignment(source["case_key"], source["first_message_id"], False, True,
                              source["message_count"] + 1)

    def pending_context_source(self, message):
        """Exact same-chat pending clarification, never a ticket/foreign root permission."""
        if message.reply_to_id is None:
            return None
        with self._lock:
            row=self.connection.execute('''SELECT m.* FROM messages m JOIN group_notices n
                ON n.chat_id=m.chat_id AND n.message_id=m.message_id AND n.status='sent'
                AND n.kind='context_pending'
                WHERE m.chat_id=? AND m.decision='pending_review' AND m.issue_number IS NULL AND m.issue_url IS NULL
                AND m.reason='vision_partition_unresolved'
                AND (m.message_id=? OR n.notice_message_id=?) LIMIT 1''',
                (message.chat_id,message.reply_to_id,message.reply_to_id)).fetchone()
            return dict(row) if row else None

    def _find_case(self, message, window_minutes, max_messages, is_command):
        if message.reply_to_id is not None:
            target = self.connection.execute("""
                SELECT c.* FROM cases c JOIN messages m ON m.case_key = c.case_key
                WHERE m.chat_id = ? AND m.message_id = ?
            """, (message.chat_id, message.reply_to_id)).fetchone()
            if (target is not None and target["status"] in ("open", "done")
                    and target["message_count"] < max_messages):
                return target
        if window_minutes <= 0:
            return None
        cutoff = _aware(message.received_at) - timedelta(minutes=window_minutes)
        candidates = self.connection.execute("""
            SELECT * FROM cases WHERE chat_id = ? AND status IN ('open', 'done')
            AND message_count < ?
        """, (message.chat_id, max_messages)).fetchall()
        candidates = sorted(candidates, key=lambda case: (
            _aware(datetime.fromisoformat(case["last_message_at"])), case["id"]), reverse=True)
        for case in candidates:
            if _aware(datetime.fromisoformat(case["last_message_at"])) < cutoff:
                continue
            if is_command and case["status"] != "open":
                continue
            if message.sender_id is not None and self.connection.execute(
                    "SELECT 1 FROM messages WHERE case_key = ? AND sender_id = ? LIMIT 1",
                    (case["case_key"], message.sender_id)).fetchone():
                return case
        return None

    def begin_analysis(self, case_key) -> bool:
        """Win the right to analyze a case; only one caller can ever succeed per attempt."""
        with self._lock, self.connection:
            cursor = self.connection.execute("""
                UPDATE cases SET status='analyzing', attempts=attempts+1
                WHERE case_key=? AND status='open'
            """, (case_key,))
            return cursor.rowcount == 1

    def case_messages(self, case_key):
        with self._lock:
            rows = self.connection.execute("""
                SELECT * FROM messages WHERE case_key = ? AND decision != 'error'
                ORDER BY message_id
            """, (case_key,)).fetchall()
        return [_to_message(row) for row in rows]

    # ---- topic groups and reconciliation (Assistant definition 12.9) -------------------

    def plan_groups(self, case_key, groups):
        """Persist the partition of a window: `groups` = [(group_key, [message ids])].

        All or nothing. A message already owned by a group, or an id outside the window, aborts
        the plan, so one message can never end up in two issues.
        """
        with self._lock, self.connection:
            case = self._case(case_key)
            if case is None:
                raise ValueError("unknown case")
            now = self.clock().isoformat()
            for group_key, ids in groups:
                ids = sorted(ids)
                self.connection.execute("""
                    INSERT INTO groups (group_key, case_key, chat_id, first_message_id, message_count,
                                        status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'planned', ?)
                """, (group_key, case_key, case["chat_id"], ids[0], len(ids), now))
                for message_id in ids:
                    cursor = self.connection.execute("""
                        UPDATE messages SET group_key = ?
                        WHERE chat_id = ? AND message_id = ? AND case_key = ? AND group_key IS NULL
                    """, (group_key, case["chat_id"], message_id, case_key))
                    if cursor.rowcount != 1:
                        raise ValueError("message not available for this group")

            self.project_topics(case["chat_id"], case_key)

    def get_group(self, group_key):
        with self._lock:
            return self.connection.execute(
                "SELECT * FROM groups WHERE group_key = ?", (group_key,)).fetchone()

    def case_groups(self, case_key):
        with self._lock:
            return self.connection.execute(
                "SELECT * FROM groups WHERE case_key = ? ORDER BY first_message_id",
                (case_key,)).fetchall()

    def reconcile(self, case_key) -> Reconciliation:
        """Tally one window and store the result (12.9); the status says whether it may be `done`."""
        with self._lock, self.connection:
            row = self.connection.execute("""
                SELECT COUNT(*) AS received,
                       COALESCE(SUM(decision IN ('issue', 'dry_run_issue')), 0) AS issue,
                       COALESCE(SUM(decision = 'noise'), 0) AS noise,
                       COALESCE(SUM(decision = 'chat'), 0) AS chat,
                       COALESCE(SUM(decision = 'pending_review'), 0) AS pending_review,
                       COALESCE(SUM(decision IN ('needs_human', 'dry_run_needs_human')), 0) AS needs_human,
                       COALESCE(SUM(decision IN ('error', 'rate_limited')), 0) AS failed
                FROM messages WHERE case_key = ?
            """, (case_key,)).fetchone()
            groups = self.connection.execute("""
                SELECT COUNT(*) AS total, COALESCE(SUM(issue_number IS NOT NULL), 0) AS created
                FROM groups WHERE case_key = ?
            """, (case_key,)).fetchone()
            value = Reconciliation(case_key, row["received"], row["issue"], row["noise"],
                                   row["needs_human"], row["failed"], groups["total"],
                                   groups["created"], "pending", row["chat"], row["pending_review"])
            value = replace(value, status="done" if value.balanced and not value.failed else "error")
            self.connection.execute("""
                INSERT OR REPLACE INTO reconciliation
                (case_key, received, issue, noise, needs_human, failed, groups, issues_created,
                 balanced, status, checked_at, chat, pending_review)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (case_key, value.received, value.issue, value.noise, value.needs_human, value.failed,
                  value.groups, value.issues_created, int(value.balanced), value.status,
                  self.clock().isoformat(), value.chat, value.pending_review))
            return value

    def close_case(self, case_key, result, latency_ms) -> Reconciliation:
        """Close a window after all its groups ran: `done`/`noise` only when the tally balances."""
        value = self.reconcile(case_key)
        with self._lock, self.connection:
            if value.status != "done":
                status = "error"
                error = result.error or RECONCILIATION_ERROR
            else:
                # No issue to follow up on: such a window stays closed to late "follow-up" messages.
                status = result.decision if result.decision in NO_TICKET_DECISIONS else "done"
                error = None
            self.connection.execute("""
                UPDATE cases SET status = ?, decision = ?, issue_number = ?, issue_url = ?,
                error = ?, closed_at = ? WHERE case_key = ?
            """, (status, result.decision, result.issue_number, result.issue_url, error,
                  self.clock().isoformat(), case_key))
            self.connection.execute("""
                UPDATE messages SET latency_ms = ?
                WHERE case_key = ? AND decision NOT IN ('error', 'followup', 'in_case', 'queued')
            """, (latency_ms, case_key))
        return replace(value, status=status)

    def due_cases(self, now, window_minutes, max_messages):
        """Open cases that must be analyzed: window expired, full, or command-driven."""
        with self._lock:
            rows = self.connection.execute(
                "SELECT * FROM cases WHERE status = 'open' ORDER BY id").fetchall()
        deadline = timedelta(minutes=max(window_minutes, 0))
        return [row["case_key"] for row in rows
                if row["has_command"] or row["message_count"] >= max_messages
                or _aware(datetime.fromisoformat(row["last_message_at"])) + deadline <= _aware(now)]

    # ---- attachment quota (Assistant definition 12.8) ----------------------------------

    def reserve_attachment_call(self, case_key, message_id, asset_name, limit):
        """Commit one `gh release upload` before it runs; return its row id or None."""
        with self._lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                if self.count_recent_attachments() >= limit:
                    self.connection.rollback()
                    return None
                cursor = self.connection.execute("""
                    INSERT INTO attachments (case_key, message_id, asset_name, status, called_at)
                    VALUES (?, ?, ?, 'uploading', ?)
                """, (case_key, message_id, asset_name, self.clock().isoformat()))
                self.connection.commit()
                return cursor.lastrowid
            except Exception:
                self.connection.rollback()
                raise

    def finish_attachment(self, call_id, status):
        with self._lock, self.connection:
            self.connection.execute(
                "UPDATE attachments SET status = ? WHERE id = ?", (status, call_id))

    def count_recent_attachments(self, now=None) -> int:
        now = self.clock() if now is None else now
        with self._lock:
            row = self.connection.execute("""
                SELECT COUNT(*) FROM attachments WHERE julianday(called_at) >= julianday(?)
            """, ((now - timedelta(minutes=60)).isoformat(),)).fetchone()
            return row[0]

    def count_recent_issues(self, now=None) -> int:
        now = self.clock() if now is None else now
        with self._lock:
            row = self.connection.execute("""
                SELECT COUNT(*) FROM messages WHERE github_called_at IS NOT NULL
                AND julianday(github_called_at) >= julianday(?)
            """, ((now - timedelta(minutes=60)).isoformat(),)).fetchone()
            return row[0] + self.followup_create_count(now)

    def export_csv(self, out):
        with self._lock:
            existing = {row[1] for row in self.connection.execute("PRAGMA table_info(messages)")}
            # Read-only export must also support a database from an older release.
            columns = [name if name in existing else "NULL AS " + name for name in COLUMNS]
            rows = self.connection.execute(
                "SELECT " + ", ".join(columns) + " FROM messages ORDER BY chat_id, message_id"
            ).fetchall()
        descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as stream:
            Path(out).chmod(0o600)
            writer = csv.writer(stream)
            writer.writerow((*COLUMNS, "owner_label", "owner_should_create"))
            for row in rows:
                writer.writerow((*(_csv_safe(value) for value in row), "", ""))


def _csv_safe(value):
    if value is None:
        return ""
    text = str(value)
    unsafe = text.startswith(("\t", "\r")) or text.lstrip().startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠"))
    return "'" + text if unsafe else text


def _aware(value):
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _to_message(row):
    return IncomingMessage(
        row["chat_id"], row["message_id"], datetime.fromisoformat(row["received_at"]),
        row["sender_name"], row["text"], row["chat_username"],
        sender_id=row["sender_id"], reply_to_id=row["reply_to_id"],
        is_forward=bool(row["is_forward"]), has_image=bool(row["has_image"]),
        image_size=row["image_size"], image_mime=row["image_mime"],
        mentions_listener=bool(row["mentions_listener"]),
        has_document=bool(row["has_document"]), document_size=row["document_size"],
        document_mime=row["document_mime"], document_name=row["document_name"])
