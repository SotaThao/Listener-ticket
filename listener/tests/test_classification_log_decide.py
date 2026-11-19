"""Migration, thread lookup and propose compatibility for classification runs."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tg_triage import classification_log as log
from tg_triage.assistant_extractor import batch_fingerprint
from tg_triage.store import Store
from tg_triage.thinking import CURRENT, Turn
from tests.helpers import NOW, message


# The classification_runs table from 028e2cb, including original defaults.
LEGACY_SCHEMA = '''CREATE TABLE classification_runs (
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
    decision_rule TEXT, outcomes_json TEXT, template TEXT, ticket_ref TEXT, error TEXT)'''


class ClassificationLogDecideTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.snapshot = SimpleNamespace(fingerprint='rules-hash')
        self.decision = SimpleNamespace(state='CHAT', rule='F3', intent='chat')
        self.precheck = dict(handled_by_code=True, checks=[], decided_rule='F3',
                             rule_text='Chỉ có đường dẫn.', handoff=None, reason='Khớp F3')

    def store(self, name):
        store = Store(self.directory / name, clock=lambda: NOW)
        self.addCleanup(store.close)
        return store

    def record(self, store, mid, *, chat_id=None, topic=None, **kwargs):
        source = message(mid) if chat_id is None else message(mid, chat_id=chat_id)
        log.record_decision(store, self.snapshot, topic or f'topic:{mid}', [source], self.decision, **kwargs)
        return store.connection.execute('SELECT max(run_id) FROM classification_runs').fetchone()[0]

    def test_additive_migration_on_028e2cb_classification_database(self):
        path = self.directory / 'legacy.db'
        with sqlite3.connect(path) as legacy:
            legacy.execute(LEGACY_SCHEMA)
            columns = [row[1] for row in legacy.execute('PRAGMA table_info(classification_runs)')]
            legacy.execute('''INSERT INTO classification_runs
                (created_at, turn_key, phase, batch_fingerprint, chat_id, message_ids, id_space,
                 rules_fingerprint, final_state, decision_rule, outcomes_json)
                 VALUES (1,'rules:old','extract','batch',-1,'[42]','message','rules','CHAT','F3',?)''',
                ('[{"rule":"F3"}]',))
            original = legacy.execute('SELECT * FROM classification_runs').fetchone()
        legacy.close()
        store = self.store(path.name)
        with store.connection:
            log.init_store(store.connection)
        migrated, = store.connection.execute('SELECT * FROM classification_runs')
        self.assertEqual(tuple(migrated.keys()), (*columns, 'code_precheck_json', 'log_thread_id'))
        self.assertEqual(tuple(migrated), (*original, None, None))
        lookup = store.connection.execute('EXPLAIN QUERY PLAN SELECT run_id FROM classification_runs '
                                          'WHERE chat_id=? ORDER BY run_id DESC LIMIT 500', (-1,)).fetchall()
        thread = store.connection.execute('EXPLAIN QUERY PLAN SELECT COUNT(*) FROM classification_runs '
                                          'WHERE COALESCE(log_thread_id,run_id)=?', (1,)).fetchall()
        self.assertIn('idx_classification_chat_run', lookup[0][3])
        self.assertIn('idx_classification_thread', thread[0][3])
        self.assertEqual(self.record(store, 43, code_precheck=self.precheck), 2)
        self.assertEqual(store.connection.execute(
            'SELECT log_thread_id FROM classification_runs WHERE run_id=2').fetchone()[0], 2)

    def test_find_thread_latest_or_match_and_set_thread_once(self):
        store = self.store('threads.db')
        chat_id = message().chat_id
        old = self.record(store, 42, topic='topic')
        root = self.record(store, 43, code_precheck=self.precheck)
        latest = self.record(store, 44, topic='topic', log_thread_id=root)
        self.record(store, 42, chat_id=-77, topic='topic', log_thread_id=900)
        url = 'https://github.com/sandbox/triage/issues/17'
        with store.connection:
            store.connection.execute('UPDATE classification_runs SET ticket_ref=? WHERE run_id=?',
                                     (url, latest))
        self.assertEqual(log.find_thread(store, chat_id, reply_to_ids=[42]), old)
        self.assertEqual(log.find_thread(store, chat_id, reply_to_ids=[42], topic_id='topic'), root)
        self.assertEqual(log.find_thread(store, chat_id, ticket_ref=url), root)
        self.assertEqual(log.find_thread(store, chat_id, ticket_ref=17), root)
        self.assertIsNone(log.find_thread(store, chat_id, ticket_ref=7))
        self.assertEqual(log.find_thread(store, -77, topic_id='topic'), 900)
        self.assertIsNone(log.find_thread(store, -99, reply_to_ids=[42], topic_id='topic', ticket_ref=url))
        self.assertIsNone(log.find_thread(store, chat_id))
        log.set_thread(store, old, root)
        log.set_thread(store, old, 999)
        log.set_thread(store, latest, 999)
        self.assertEqual(log.find_thread(store, chat_id, reply_to_ids=[42]), root)
        self.assertEqual(log.find_thread(store, chat_id, ticket_ref=url), root)
        log.set_thread(store, root, 900)
        self.assertEqual(log.find_thread(store, chat_id, reply_to_ids=[43]), root)
        leaf = self.record(store, 45, code_precheck=self.precheck)
        log.set_thread(store, leaf, root)
        self.assertEqual(log.find_thread(store, chat_id, reply_to_ids=[45]), root)

    def test_find_thread_limits_rows_and_age(self):
        store = self.store('bounded.db')
        chat = message().chat_id
        expired = self.record(store, 1, topic='expired')
        older = self.record(store, 2, topic='older')
        sql = """INSERT INTO classification_runs
                 (created_at,turn_key,phase,batch_fingerprint,chat_id,message_ids,id_space,rules_fingerprint)
                 VALUES (?, 'turn', 'early', 'batch', ?, ?, 'message', 'rules')"""
        with store.connection:
            store.connection.execute('UPDATE classification_runs SET created_at=? WHERE run_id=?',
                                     (NOW.timestamp() - 31 * 86400, expired))
            store.connection.executemany(sql, [(NOW.timestamp(), chat, json.dumps([mid]))
                                              for mid in range(3, 3 + log.THREAD_LOOKUP_LIMIT)])
        latest = 2 + log.THREAD_LOOKUP_LIMIT
        self.assertIsNone(log.find_thread(store, chat, reply_to_ids=[2], topic_id='older'))
        self.assertEqual(log.find_thread(store, chat, reply_to_ids=[3]), older + 1)
        self.assertEqual(log.find_thread(store, chat, reply_to_ids=[latest]), latest)
        with store.connection:
            store.connection.execute('DELETE FROM classification_runs WHERE run_id>?', (older,))
        self.assertIsNone(log.find_thread(store, chat, reply_to_ids=[1], topic_id='expired'))

    def test_run_records_precheck_and_rule_text_in_deferred_transaction(self):
        store = self.store('deferred.db')
        source = message(42)
        turn = Turn(store, 'rules:root', 'root', source.message_id)
        token = CURRENT.set(turn)
        try:
            with store.connection:
                store.connection.execute('BEGIN')
                with log.run(self.snapshot, [source], code_precheck=self.precheck, topic_id='named') as attempt:
                    attempt.result = (([source], self.decision),)
                self.assertEqual(store.connection.execute('SELECT count(*) FROM classification_runs').fetchone()[0], 0)
            self.assertTrue(log.flush(store))
        finally:
            CURRENT.reset(token)
        row, = store.connection.execute('SELECT * FROM classification_runs')
        self.assertEqual((row['topic_id'], row['log_thread_id']), ('named', row['run_id']))
        self.assertEqual(json.loads(row['code_precheck_json']), self.precheck)
        outcome, = json.loads(row['outcomes_json'])
        self.assertEqual(outcome['rule_text'], self.precheck['rule_text'])
        self.assertEqual((outcome['final_state'], outcome['decision_rule']), ('CHAT', 'F3'))

    def test_propose_ab_byte_identity(self):
        """Compare the same fixed-clock decision with omitted and explicit NULL arguments."""
        serialized = []
        for explicit in (False, True):
            store = self.store(f'propose-{explicit}.db')
            options = dict(code_precheck=None, log_thread_id=None) if explicit else {}
            self.record(store, 42, **options)
            source = message(43)
            token = CURRENT.set(Turn(store, 'rules:root', 'root', source.message_id))
            try:
                with log.run(self.snapshot, [source], **options) as attempt:
                    attempt.result = (([source], self.decision),)
            finally:
                CURRENT.reset(token)
            rows = [dict(row) for row in store.connection.execute('SELECT * FROM classification_runs ORDER BY run_id')]
            self.assertEqual(rows[0]['batch_fingerprint'], batch_fingerprint([message(42)]))
            for row in rows:
                self.assertIsNone(row['code_precheck_json'])
                self.assertIsNone(row['log_thread_id'])
                self.assertIsNone(row['outcomes_json'])
            serialized.append(json.dumps(rows, sort_keys=True).encode())
        self.assertEqual(*serialized)
