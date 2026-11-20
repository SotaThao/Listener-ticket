"""Observational lifecycle for the paced Telegram worker's collection windows."""

import logging
import sqlite3

LOG = logging.getLogger(__name__)


def init_store(db, *, recover_pending):
    db.execute("""CREATE TABLE IF NOT EXISTS collection_batches (
        batch_id TEXT PRIMARY KEY, chat_id INTEGER NOT NULL, chat_label TEXT,
        started_at REAL NOT NULL, status TEXT NOT NULL
        CHECK(status IN ('collecting','queued','processing','finished','interrupted'))
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS collection_batch_messages (
        batch_id TEXT NOT NULL, message_id INTEGER NOT NULL, ordinal INTEGER NOT NULL,
        PRIMARY KEY(batch_id,message_id)
    )""")
    db.execute("""CREATE INDEX IF NOT EXISTS idx_collection_batches_display
        ON collection_batches((status IN ('collecting','queued','processing')) DESC,
            started_at DESC,batch_id DESC)""")
    if recover_pending:
        db.execute("""UPDATE collection_batches SET status='interrupted'
            WHERE status IN ('collecting','queued','processing')""")


def persist(store, events):
    """Persist ordered observer events without joining a pipeline transaction."""
    if not store._lock.acquire(timeout=0.05):
        return False
    try:
        db = store.connection
        if db.in_transaction:
            return False
        busy_ms = db.execute("PRAGMA busy_timeout").fetchone()[0]
        # Do not hold the Store lock for its ordinary multi-second busy timeout.
        limited_ms = min(busy_ms, 50)
        db.execute(f"PRAGMA busy_timeout={limited_ms}")
        try:
            _persist_events(db, events)
        finally:
            db.execute(f"PRAGMA busy_timeout={busy_ms}")
        return True
    except sqlite3.Error as error:
        LOG.debug("Collection accounting will retry exception=%s", type(error).__name__)
        return False
    finally:
        store._lock.release()


def _persist_events(db, events):
    with db:
        for batch_id, status, payload in events:
            if status == 'discard':
                db.execute("DELETE FROM collection_batch_messages WHERE batch_id=?", (batch_id,))
                db.execute("DELETE FROM collection_batches WHERE batch_id=?", (batch_id,))
            elif payload is None:
                db.execute("""UPDATE collection_batches SET status=? WHERE batch_id=?
                    AND status IN ('collecting','queued','processing')""", (status, batch_id))
            else:
                chat_id, label, started_at, message_id, ordinal = payload
                db.execute("""INSERT INTO collection_batches
                    (batch_id,chat_id,chat_label,started_at,status) VALUES (?,?,?,?,'collecting')
                    ON CONFLICT(batch_id) DO UPDATE
                    SET chat_label=COALESCE(collection_batches.chat_label,excluded.chat_label)""",
                    (batch_id, chat_id, label, started_at))
                db.execute("""INSERT OR IGNORE INTO collection_batch_messages
                    (batch_id,message_id,ordinal) VALUES (?,?,?)""", (batch_id, message_id, ordinal))


def prune(db, cutoff):
    """Drop batch metadata in the same age window as classification accounting."""
    expired = """SELECT batch_id FROM collection_batches WHERE started_at<?
        AND status IN ('finished','interrupted')"""
    db.execute(f"DELETE FROM collection_batch_messages WHERE batch_id IN ({expired})", (cutoff,))
    db.execute(f"DELETE FROM collection_batches WHERE batch_id IN ({expired})", (cutoff,))
