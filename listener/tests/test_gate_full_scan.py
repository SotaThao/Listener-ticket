"""Startup scans full real SQLite ledgers; offline reports retain their budgets."""
import hashlib
import json
import tracemalloc
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from contextlib import contextmanager

from tg_triage.legacy_preflight import MAX_JSON_BYTES, MAX_ROWS, REGISTRY, _category, inspect
from tg_triage.rules_pending_store import RulesHalt, RulesPendingStore, check_before_recovery, _proven_closed_chat_sources, local_binding_blockers, readonly_plan_id
from tg_triage.store import Store
from tests.helpers import message


class GateFullScanTests(unittest.TestCase):
    TABLES = ('rules_decisions', 'triage_topic_events')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'gate.sqlite3'

    def populate(self, count, *, bad_table=None, position=None, value=None, payload='{}', invalid=False):
        store = Store(self.db)
        try:
            RulesPendingStore(store)
            store.enable_topic_registry()
            with store.connection:
                store.connection.executemany(
                    "INSERT INTO rules_decisions(topic_id,recorded_at,state,intent_rule,decision_rule,"
                    "fingerprint,present_sources) VALUES(?,'now',?,'I2','R9','fingerprint',?)",
                    ((f'topic-{i}', 'unknown' if invalid and i == position else 'CHAT',
                      value if bad_table == 'rules_decisions' and i == position else payload)
                     for i in range(1, count + 1)))
                store.connection.executemany(
                    "INSERT INTO triage_topic_events(topic_id,version,recorded_at,event,metadata) "
                    "VALUES(?,1,'now','test',?)",
                    ((f'topic-{i}', value if bad_table == 'triage_topic_events' and i == position else payload)
                     for i in range(1, count + 1)))
        finally:
            store.close()

    def assessment(self, table, limit=None):
        connection = sqlite3.connect(self.db)
        try:
            return _category(connection, table, REGISTRY[table], limit)
        finally:
            connection.close()

    @contextmanager
    def fresh_database(self):
        previous = self.db
        with tempfile.TemporaryDirectory(dir=self.temp.name) as directory:
            self.db = Path(directory) / 'gate.sqlite3'
            try:
                yield
            finally:
                self.db = previous

    def test_gate_passes_1000_1001_and_10000_rows_per_table(self):
        for count in (1000, 1001, 10000):
            with self.subTest(count=count), self.fresh_database():
                self.populate(count)
                started = time.perf_counter()
                check_before_recovery(self.db)
                elapsed = time.perf_counter() - started
                if count == 10000:
                    print(f'Gate full scan: {count} rows/table x 2 tables in {elapsed:.3f}s', flush=True)
                    self.assertLess(elapsed, 20.0)
                for table in self.TABLES:
                    row = self.assessment(table)
                    self.assertEqual(count, row['total'])
                    self.assertTrue(row['validation_complete'])
                    self.assertFalse(row['truncated'])
                    self.assertEqual([], row['codes'])

    def test_gate_halts_on_bad_json_and_types_at_1500_and_10000(self):
        for table in self.TABLES:
            for position in (1500, 10000):
                for value in ('{broken', '42', b'{}'):
                    with self.subTest(table=table, position=position, value=value), self.fresh_database():
                        self.populate(position, bad_table=table, position=position, value=value)
                        with self.assertRaisesRegex(RulesHalt, 'unknown schema, identifier, state or record; Owner inspection required'):
                            check_before_recovery(self.db)
                        row = self.assessment(table)
                        self.assertEqual(1, row['invalid'])
                        self.assertEqual(['MALFORMED_RECORD'], row['codes'])
                        self.assertFalse(row['truncated'])

    def test_gate_halts_on_oversized_json_beyond_1000(self):
        value = json.dumps({'padding': 'x' * MAX_JSON_BYTES})
        self.assertGreater(len(value.encode('utf8')), MAX_JSON_BYTES)
        for table in self.TABLES:
            with self.subTest(table=table), self.fresh_database():
                self.populate(1500, bad_table=table, position=1500, value=value)
                with self.assertRaisesRegex(RulesHalt, 'unknown schema, identifier, state or record; Owner inspection required'):
                    check_before_recovery(self.db)
                row = self.assessment(table)
                self.assertEqual(1, row['invalid'])
                self.assertEqual(['MALFORMED_RECORD'], row['codes'])

    def test_malformed_and_unknown_status_count_row_once(self):
        self.populate(1500, bad_table='rules_decisions', position=1500, value='{broken', invalid=True)
        with self.assertRaisesRegex(RulesHalt, 'unknown schema, identifier, state or record; Owner inspection required'):
            check_before_recovery(self.db)
        row = self.assessment('rules_decisions')
        self.assertEqual(1, row['invalid'])
        self.assertEqual(['UNKNOWN_STATUS', 'MALFORMED_RECORD'], row['codes'])

    def test_gate_ignores_cumulative_budget_but_report_keeps_byte_cap(self):
        payload = json.dumps({'padding': 'x' * 10000})
        self.assertGreater(900 * len(payload.encode('utf8')), 8 * 1024 * 1024)
        self.populate(900, payload=payload)
        check_before_recovery(self.db)
        report = inspect(self.db, limit=MAX_ROWS)
        for table in self.TABLES:
            self.assertTrue(self.assessment(table)['validation_complete'])
            row = report['inventory'][table]
            self.assertTrue(row['truncated'])
            self.assertFalse(row['validation_complete'])
            self.assertEqual(0, row['invalid'])
            self.assertEqual(['VALIDATION_TRUNCATED'], row['codes'])

    def test_report_keeps_row_cap_and_codes(self):
        self.populate(1001)
        check_before_recovery(self.db)
        report = inspect(self.db, limit=MAX_ROWS)
        self.assertEqual('UNKNOWN', report['recovery_gate'])
        self.assertEqual(['SCHEMA_OR_VALIDATION_UNKNOWN'], report['recovery_codes'])
        for table in self.TABLES:
            row = report['inventory'][table]
            self.assertEqual(1001, row['total'])
            self.assertEqual(0, row['invalid'])
            self.assertTrue(row['truncated'])
            self.assertFalse(row['validation_complete'])
            self.assertEqual(['VALIDATION_TRUNCATED'], row['codes'])

    def test_report_rejects_unlimited_or_invalid_limits(self):
        self.populate(1)
        for limit in (None, 0, MAX_ROWS + 1, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                inspect(self.db, limit=limit)

    def populate_closed_chat(self, count, *, closed=True, proof_positions=None):
        """Production schema with unowned pending sources and late CHAT proofs."""
        positions = (count - 1,) if proof_positions is None else proof_positions
        store = Store(self.db)
        expected = set()
        try:
            RulesPendingStore(store)
            plans = {}
            for index, position in enumerate(positions):
                pending = message(2 * index + 1)
                chat = message(2 * index + 2)
                store.enqueue(pending)
                store.enqueue(chat)
                topic = f'closed-chat-{index}'
                sources = [[1, pending.chat_id, pending.message_id], [2, chat.chat_id, chat.message_id]]
                expected.add((pending.chat_id, pending.message_id))
                plans[position] = json.dumps({
                    'state': 'CHAT', 'rule': 'R_chat', 'pending_topic': topic,
                    'pending_snapshot': {'status': 'OPEN', 'last_at': 'now', 'asked': '{}',
                                         'fallback_asked': 0, 'sources': sources},
                    'source_keys': [[chat.chat_id, chat.message_id]],
                })
                with store.connection:
                    store.connection.execute(
                        "UPDATE messages SET decision='pending_review' WHERE chat_id=? AND message_id=?",
                        (pending.chat_id, pending.message_id))
                    store.connection.execute(
                        "UPDATE messages SET decision='chat',reason='R_chat' WHERE chat_id=? AND message_id=?",
                        (chat.chat_id, chat.message_id))
                    # EXPIRED remains unowned but is settled by a separate startup
                    # exception. ESCALATED without a notice isolates a broken
                    # CLOSED proof and must remain unsupported legacy pending.
                    store.connection.execute(
                        'INSERT INTO rules_topics(topic_id,source_chat_id,last_at,status) VALUES(?,?,?,?)',
                        (topic, pending.chat_id, 'now', 'CLOSED' if closed else 'ESCALATED'))
                    store.connection.executemany(
                        'INSERT INTO rules_sources(topic_id,ref,chat_id,message_id) VALUES(?,?,?,?)',
                        ((topic, *source) for source in sources))
                    store.connection.execute(
                        "INSERT INTO rules_decisions(topic_id,recorded_at,state,intent_rule,decision_rule,"
                        "fingerprint,present_sources) VALUES(?,'now','CHAT','I2','R_chat','fp','{}')", (topic,))
            with store.connection:
                store.connection.execute("INSERT INTO rules_batches VALUES('history','fp','now')")
                store.connection.executemany(
                    "INSERT INTO rules_batch_groups(batch_id,ordinal,plan,status) VALUES('history',?,?,'DONE')",
                    ((ordinal, plans.get(ordinal, '{}')) for ordinal in range(count)))
        finally:
            store.close()
        return expected

    def test_gate_passes_with_closed_chat_proof_beyond_1000_done_groups(self):
        for count in (1001, 5000):
            with self.subTest(count=count), self.fresh_database():
                expected = self.populate_closed_chat(count)
                check_before_recovery(self.db)
                connection = sqlite3.connect(self.db)
                try:
                    self.assertEqual(expected, _proven_closed_chat_sources(connection))
                    self.assertEqual(count, connection.execute('SELECT count(*) FROM rules_batch_groups').fetchone()[0])
                finally:
                    connection.close()

    def test_gate_halts_when_late_closed_chat_proof_is_broken(self):
        for count in (1001, 5000):
            with self.subTest(count=count), self.fresh_database():
                self.populate_closed_chat(count, closed=False)
                with self.assertRaisesRegex(RulesHalt, 'unsupported legacy pending messages; Owner inspection required'):
                    check_before_recovery(self.db)

    def test_closed_chat_cursor_continues_after_nested_queries_on_same_connection(self):
        expected = self.populate_closed_chat(5000, proof_positions=(1000, 4999))
        connection = sqlite3.connect(self.db)
        try:
            # Each proof performs nested queries using this same connection;
            # finding the second proves that the outer cursor resumes afterward.
            self.assertEqual(expected, _proven_closed_chat_sources(connection))
            self.assertEqual(2, len(expected))
        finally:
            connection.close()
        check_before_recovery(self.db)

    def test_report_binding_budget_remains_capped_with_production_schema(self):
        self.populate_closed_chat(1001)
        check_before_recovery(self.db)
        report = inspect(self.db, limit=MAX_ROWS)
        self.assertIn('BINDING_VALIDATION_TRUNCATED', report['binding_codes'])

    @staticmethod
    def plan_digest(plan):
        return hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()).hexdigest()

    def populate_related(self, count, *, all_receipts=False):
        """Completed direct-recurrence ledgers, with plans/receipts in insertion order."""
        store = Store(self.db)
        try:
            RulesPendingStore(store)
            selected = range(1, count + 1) if all_receipts else (count,)
            chat = message().chat_id
            for mid in selected:
                store.enqueue(message(mid))
            with store.connection:
                store.connection.execute("UPDATE messages SET decision='chat'")
                store.connection.execute("INSERT INTO rules_batches VALUES('related','fp','now')")
                store.connection.executemany(
                    "INSERT INTO rules_batch_groups VALUES('related',?,?,'DONE')",
                    ((i - 1, json.dumps({'state': 'RELATED', 'source_keys': [[chat, i]]}))
                     for i in range(1, count + 1)))
                for mid in selected:
                    plan = {'state': 'RELATED', 'source_keys': [[chat, mid]]}
                    keys = json.dumps(plan['source_keys'])
                    store.connection.execute(
                        'INSERT INTO rules_related_receipts VALUES(?,?,?,?,?,?)',
                        (chat, 'sandbox/triage', 10000 + mid, self.plan_digest(plan), keys, keys))
                    store.connection.execute(
                        "INSERT INTO rules_related_episodes(chat_id,repo,parent_number,source_id,answer_id,status,issue_number) "
                        "VALUES(?,'sandbox/triage',?,?,?,'done',202)", (chat, 10000 + mid, mid, mid))
                    store.connection.execute(
                        "INSERT INTO rules_lifecycle_answers VALUES(?,?,?,'recurrence','done')", (chat, mid, mid))
        finally:
            store.close()

    def test_gate_passes_production_receipt_linked_to_plan_beyond_1000(self):
        for count in (1001, 5000):
            with self.subTest(count=count), self.fresh_database():
                self.populate_related(count)
                check_before_recovery(self.db)
                connection = sqlite3.connect(self.db)
                try:
                    self.assertEqual([], local_binding_blockers(connection, limit=None))
                    self.assertIn('BINDING_VALIDATION_TRUNCATED', local_binding_blockers(connection))
                finally:
                    connection.close()
                report = inspect(self.db, limit=MAX_ROWS)
                self.assertIn('BINDING_VALIDATION_TRUNCATED', report['binding_codes'])

    def test_gate_checks_receipts_and_lifecycle_answers_beyond_1000(self):
        self.populate_related(1001, all_receipts=True)
        check_before_recovery(self.db)
        connection = sqlite3.connect(self.db)
        try:
            with connection:
                connection.execute('UPDATE rules_related_receipts SET plan_hash=? WHERE parent_number=11001', ('0' * 64,))
            self.assertEqual(['RELATED_RECEIPT_MISMATCH'], local_binding_blockers(connection, limit=None))
        finally:
            connection.close()
        with self.assertRaisesRegex(RulesHalt, 'invalid local ownership or receipt binding; Owner inspection required'):
            check_before_recovery(self.db)

    def test_gate_binding_byte_budget_is_off_and_full_plans_are_not_retained(self):
        store = Store(self.db)
        try:
            RulesPendingStore(store)
            with store.connection:
                store.connection.execute("INSERT INTO rules_batches VALUES('large','fp','now')")
                store.connection.executemany(
                    "INSERT INTO rules_batch_groups VALUES('large',?,?,'DONE')",
                    ((i, json.dumps({'source_keys': [], 'ordinal': i, 'padding': 'x' * 16000}))
                     for i in range(1001)))
            tracemalloc.start()
            try:
                self.assertEqual([], local_binding_blockers(store.connection, limit=None))
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertLess(peak, 8 * 1024 * 1024)
            self.assertIn('BINDING_VALIDATION_TRUNCATED', local_binding_blockers(store.connection))
        finally:
            store.close()
        check_before_recovery(self.db)

    def test_gate_binding_generator_reports_sqlite_errors_during_iteration(self):
        self.populate_closed_chat(3)
        connection = sqlite3.connect(self.db)
        try:
            def stored_plan(ordinal, raw):
                if ordinal == 2:
                    raise sqlite3.OperationalError('fixture cursor fault')
                return raw
            connection.create_function('stored_plan', 2, stored_plan)
            # SQLite fails after the first row while stepping the cursor, rather
            # than at execute(). The generator must translate this error too.
            with connection:
                connection.execute('ALTER TABLE rules_batch_groups RENAME TO saved_groups')
                connection.execute("CREATE VIEW rules_batch_groups AS SELECT batch_id,ordinal,"
                                   "stored_plan(ordinal,plan) AS plan,"
                                   "status FROM saved_groups")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            tables.add('rules_batch_groups')  # Include the deliberate fault-injection view.
            self.assertIn('BINDING_SCHEMA_UNKNOWN', local_binding_blockers(connection, tables, limit=None))
        finally:
            connection.close()

    def populate_lifecycle(self, count):
        store = Store(self.db)
        chat = message().chat_id
        ops = -1009876543210
        try:
            RulesPendingStore(store)
            for mid in range(1, count + 1):
                store.enqueue(message(mid))
                store.enqueue(message(mid, chat_id=ops, text='yes', reply_to_id=100000 + mid))
            with store.connection:
                store.connection.execute(
                    "UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=?", (chat,))
                store.connection.execute("UPDATE messages SET decision='chat' WHERE chat_id=?", (ops,))
                store.connection.execute("INSERT INTO rules_batches VALUES('lifecycle','fp','now')")
                for mid in range(1, count + 1):
                    topic = f'question-{mid}'
                    plan = {'state': 'LIFECYCLE_QUESTION', 'pending_topic': topic, 'source_keys': [[chat, mid]],
                            'targets': [{'chat_id': chat, 'reply_message_id': mid, 'number': 101, 'reply_to_id': mid}],
                            'lifecycle_question': 'question', 'lifecycle_evidence_answer': 'yes',
                            'lifecycle_recurrence_answer': 'no'}
                    store.connection.execute("INSERT INTO rules_batch_groups VALUES('lifecycle',?,?,'DONE')", (mid - 1, json.dumps(plan)))
                    store.connection.execute(
                        "INSERT INTO rules_topics(topic_id,source_chat_id,last_at,status) VALUES(?,?,'now','CLOSED')", (topic, chat))
                    store.connection.execute('INSERT INTO rules_lifecycle_questions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                                             (chat, mid, topic, 101, 'sandbox/triage',
                                              readonly_plan_id('lifecycle', mid - 1, 'fp', plan),
                                              'question', 'fp', 'yes', 'no', 'lifecycle', mid - 1))
                    store.connection.execute("INSERT INTO rules_notices VALUES(?,1,?,'question','sent',?)", (topic, ops, 100000 + mid))
                    store.connection.execute("INSERT INTO rules_lifecycle_answers VALUES(?,?,?,'evidence','done')", (chat, mid, mid))
        finally:
            store.close()

    def test_gate_streams_lifecycle_questions_answers_and_notices_beyond_1000(self):
        self.populate_lifecycle(1001)
        check_before_recovery(self.db)
        report = inspect(self.db, limit=MAX_ROWS)
        self.assertIn('LIFECYCLE_VALIDATION_TRUNCATED', report['binding_codes'])
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual([], local_binding_blockers(connection, limit=None))
            with connection:
                connection.execute("UPDATE messages SET text='wrong' WHERE chat_id=-1009876543210 AND message_id=1001")
            self.assertEqual(['LIFECYCLE_ANSWER_MISMATCH'], local_binding_blockers(connection, limit=None))
            with connection:
                connection.execute("UPDATE messages SET text='yes' WHERE chat_id=-1009876543210 AND message_id=1001")
                connection.execute("UPDATE rules_lifecycle_questions SET question='wrong' WHERE source_id=1001")
            self.assertIn('LIFECYCLE_QUESTION_MISMATCH', local_binding_blockers(connection, limit=None))
        finally:
            connection.close()
        with self.assertRaisesRegex(RulesHalt, 'invalid local ownership or receipt binding; Owner inspection required'):
            check_before_recovery(self.db)

    def populate_analysis_reviews(self, count, *, padded=False):
        from tg_triage.rules_analysis import VERSION, digest, init_store
        from tg_triage.rules_intake import _source_evidence_id
        from tg_triage.store import _to_message
        store = Store(self.db)
        chat = message().chat_id
        try:
            RulesPendingStore(store)
            init_store(store.connection)
            for mid in range(1, count + 1):
                store.enqueue(message(mid))
            with store.connection:
                store.connection.execute("UPDATE messages SET decision='chat',reason='semantic_analyze'")
                store.connection.execute("INSERT INTO rules_batches VALUES('review','fp','now')")
                for mid in range(1, count + 1):
                    token = f'r-{mid}' + ('x' * 6500 if padded else '')
                    receipt = dict(version=VERSION, fingerprint='fp', batch_id='review', ordinal=mid - 1,
                                   task={}, source_ids=[mid], chat_id=chat, documents=[], context={},
                                   sources={str(mid): _source_evidence_id(_to_message(store.get(chat, mid)))})
                    if padded:
                        receipt['padding'] = 'y' * 3000
                    checksum = digest(receipt)
                    plan = dict(state='ANALYZE', source_keys=[[chat, mid]], task={},
                                analysis_origin=['review', mid - 1], document_reads=[],
                                analysis_review=dict(version=VERSION, review_id=token, receipt_hash=checksum))
                    store.connection.execute('INSERT INTO rules_analysis_reviews VALUES(?,?,?,?,?)',
                                             (token, chat, mid, checksum, json.dumps(receipt)))
                    store.connection.execute('INSERT INTO rules_analysis_review_members VALUES(?,?,?)', (chat, mid, token))
                    store.connection.execute("INSERT INTO rules_batch_groups VALUES('review',?,?,'DONE')", (mid - 1, json.dumps(plan)))
        finally:
            store.close()

    def test_gate_streams_analysis_reviews_beyond_1000_and_keeps_mismatch_code(self):
        self.populate_analysis_reviews(1001)
        check_before_recovery(self.db)
        connection = sqlite3.connect(self.db)
        try:
            self.assertEqual([], local_binding_blockers(connection, limit=None))
            with connection:
                connection.execute("UPDATE rules_analysis_reviews SET receipt='{}' WHERE review_id='r-1001'")
            self.assertEqual(['ANALYSIS_RECEIPT_MISMATCH'], local_binding_blockers(connection, limit=None))
        finally:
            connection.close()
        with self.assertRaisesRegex(RulesHalt, 'invalid local ownership or receipt binding; Owner inspection required'):
            check_before_recovery(self.db)

    def test_binding_sql_guard_still_rejects_oversized_plan_beyond_1000(self):
        self.populate_closed_chat(1001)
        connection = sqlite3.connect(self.db)
        try:
            with connection:
                connection.execute('UPDATE rules_batch_groups SET plan=? WHERE ordinal=1000',
                                   (json.dumps({'padding': 'x' * MAX_JSON_BYTES}),))
            self.assertEqual(['MALFORMED_BINDING_RECORD'], local_binding_blockers(connection, limit=None))
        finally:
            connection.close()
        with self.assertRaisesRegex(RulesHalt, 'invalid local ownership or receipt binding; Owner inspection required'):
            check_before_recovery(self.db)

    def test_report_keeps_recovery_codes_when_only_binding_byte_budget_truncates(self):
        self.populate_analysis_reviews(900, padded=True)
        check_before_recovery(self.db)
        report = inspect(self.db, limit=MAX_ROWS)
        self.assertTrue(report['inspection']['present_ledgers_validated'])
        self.assertEqual(['BINDING_VALIDATION_TRUNCATED'], report['binding_codes'])
        self.assertEqual('HOLD', report['recovery_gate'])
        self.assertEqual(['RECOVERY_REQUIRES_RECONCILIATION'], report['recovery_codes'])
