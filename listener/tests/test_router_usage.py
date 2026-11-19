from concurrent.futures import ThreadPoolExecutor
import contextlib
from importlib import import_module
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest.mock import patch

from tg_triage.assistant_contract import PHASE_DECIDE
from tg_triage.router_transport import RouterError, RouterReply
from tg_triage.store import Store
from tests import helpers


SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
SECRET = 'secret-from-usageHistory-apiKey'
ROUTER_URL = 'http://127.0.0.1:20128/v1'
TIMESTAMP = '2026-11-19T15:17:14.671Z'


@unittest.skipUnless((SCRIPTS / 'router_usage.py').is_file(), 'scripts absent in deploy stage')
class RouterUsageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch.object(sys, 'path', [str(SCRIPTS), *sys.path]):
            cls.usage = import_module('router_usage')
            cls.p8 = import_module('eval-assistant-p8')
            cls.probe = import_module('probe-prompt-cache')
            cls.replay = import_module('replay-assistant-decide')

    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.db = self.directory / 'billing #1.sqlite'
        self.key = self.directory / 'key'
        self.key.write_text('test-router-key', encoding='utf-8')
        self.key.chmod(0o600)
        self.output = self.directory / 'result.jsonl'
        self.report = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.errors = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.enterContext(patch.object(self.usage, 'DEFAULT_DB', self.directory / 'missing.sqlite'))
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executescript("""
                CREATE TABLE usageHistory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
                    provider TEXT, model TEXT, connectionId TEXT, apiKey TEXT, endpoint TEXT,
                    promptTokens INTEGER DEFAULT 0, completionTokens INTEGER DEFAULT 0,
                    cost REAL DEFAULT 0, status TEXT, tokens TEXT, meta TEXT
                );
                CREATE INDEX idx_uh_ts ON usageHistory(timestamp);
                CREATE INDEX idx_uh_provider ON usageHistory(provider);
                CREATE INDEX idx_uh_model ON usageHistory(model);
                CREATE INDEX idx_uh_conn ON usageHistory(connectionId);
            """)

    def tearDown(self):
        artifact = self.output.read_text(encoding='utf-8') if self.output.exists() else ''
        self.assertNotIn(SECRET, self.report.getvalue() + self.errors.getvalue() + artifact)

    def add_usage(self, model, *, prompt=100, cached=60, cost=.00125, status='ok', tokens=None):
        if tokens is None:
            tokens = json.dumps(dict(prompt_tokens=prompt, completion_tokens=7, total_tokens=prompt + 7,
                                     cached_tokens=cached, cache_creation_input_tokens=4, reasoning_tokens=3))
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("""
                INSERT INTO usageHistory (timestamp, provider, model, apiKey, promptTokens,
                                          completionTokens, cost, status, tokens)
                VALUES (?, 'test-provider', ?, ?, ?, 7, ?, ?, ?)
            """, (TIMESTAMP, model, SECRET, prompt, cost, status, tokens))

    def test_reads_ordered_named_fields_and_ignores_invalid_tokens(self):
        reader = self.usage.RouterUsage(self.db)
        self.assertEqual(reader.since_id(), 0)
        self.add_usage('old')
        before = reader.since_id()
        self.add_usage('gemini')
        self.add_usage('gpt', tokens='{invalid')
        self.add_usage('other', tokens='null')
        fresh, invalid, missing = reader.rows_after(before)
        self.assertEqual(fresh, dict(id=2, timestamp=TIMESTAMP, provider='test-provider', model='gemini',
                                     status='ok', promptTokens=100, completionTokens=7, cost=.00125,
                                     cached_tokens=60, cache_creation_input_tokens=4, reasoning_tokens=3))
        self.assertEqual((invalid['id'], missing['id']), (3, 4))
        self.assertIsNone(invalid['cached_tokens'])
        self.assertIsNone(missing['cached_tokens'])
        self.assertNotIn(SECRET, json.dumps([fresh, invalid, missing]))

    def test_attributes_by_order_and_rejects_wrong_model_or_row_count(self):
        window = self.usage.UsageWindow(self.usage.RouterUsage(self.db), settle_s=0)
        send = window.wrap(lambda model, cached: self.add_usage(model.split('/')[1], cached=cached))
        window.start()
        send('ag/gemini', 11)
        send('ag/gemini', 22)
        send('cx/gpt', 33)
        result = window.finish()
        self.assertEqual(result['usage_source'], 'router_db')
        self.assertEqual([call['cached_tokens'] for call in result['router_calls']], [11, 22, 33])
        self.assertEqual(result['router_usage']['cache_ratio'], .22)

        window.start()
        send('ag/gemini', 11)
        self.add_usage('gemini')
        self.assertEqual(window.finish(), {'usage_source': 'ambiguous', 'reason': 'foreign_row'})

        window.start()
        window.wrap(lambda model: self.add_usage('gpt'))('ag/gemini')
        self.assertEqual(window.finish(), {'usage_source': 'ambiguous', 'reason': 'model_mismatch'})

        window.start()
        window.wrap(lambda model: None)('ag/gemini')
        self.assertEqual(window.finish(), {'usage_source': 'ambiguous', 'reason': 'late_or_missing_row'})

    def test_batch_waits_for_delayed_completion_rows(self):
        window = self.usage.UsageWindow(self.usage.RouterUsage(self.db), settle_s=1.5)
        with ThreadPoolExecutor(max_workers=1) as executor:
            tracked = window.wrap(lambda model: executor.submit(self.insert_delayed_usage, model))
            tracked('ag/gemini').result()
            pending = tracked('cx/gpt')
            result = window.finish()
            pending.result()
        self.assertEqual(result['usage_source'], 'router_db')
        self.assertEqual([call['model'] for call in result['router_calls']], ['ag/gemini', 'cx/gpt'])
        self.assertEqual(result['router_usage']['prompt_tokens'], 200)

    def test_row_past_deadline_cannot_be_billed_to_next_same_model_call(self):
        reader = self.usage.RouterUsage(self.db)
        window = self.usage.UsageWindow(reader, settle_s=.02)
        release_row = Event()

        def insert_first_row():
            if release_row.wait(timeout=2):
                self.add_usage('gemini', prompt=111)

        with ThreadPoolExecutor(max_workers=1) as executor:
            delayed = window.wrap(lambda model: executor.submit(insert_first_row))('ag/gemini')
            self.assertEqual(window.finish(), {'usage_source': 'ambiguous', 'reason': 'late_or_missing_row'})

            def next_request(model):
                release_row.set()
                delayed.result(timeout=2)

            window.start()
            window.wrap(next_request)('ag/gemini')
            self.assertEqual(window.finish(), {'usage_source': 'ambiguous', 'reason': 'foreign_row'})

        # The second row appeared between windows. A fresh snapshot excludes it.
        self.add_usage('gemini', prompt=222)
        window.start()
        window.wrap(lambda model: self.add_usage('gemini', prompt=333))('ag/gemini')
        self.assertEqual(window.finish()['router_usage']['prompt_tokens'], 333)

    def test_read_only_connection_and_protected_output(self):
        reader = self.usage.RouterUsage(self.db)
        self.add_usage('gemini')
        original = self.db.read_bytes(), self.db.stat().st_mtime_ns
        with reader._connect() as conn:
            self.assertEqual(conn.execute('PRAGMA query_only').fetchone()[0], 1)
            # URI mode must still reject writes even if query_only is cleared.
            conn.execute('PRAGMA query_only=OFF')
            with self.assertRaisesRegex(sqlite3.OperationalError, 'readonly'):
                conn.execute('DELETE FROM usageHistory')
        with self.assertRaises(self.usage.RouterUsageError):
            self.usage.open_usage(self.db, output=self.db)
        with self.assertRaises(self.usage.RouterUsageError):
            self.usage.open_usage(self.db, output=str(self.db) + '-wal')
        self.assertEqual(reader.rows_after(0)[0]['model'], 'gemini')
        self.assertEqual((self.db.read_bytes(), self.db.stat().st_mtime_ns), original)

    def test_batch_matches_completions_and_logged_errors_in_order(self):
        window = self.usage.UsageWindow(self.usage.RouterUsage(self.db))

        def send(model, status):
            if status:
                self.add_usage(model.split('/')[1], status=status)
            if status != 'ok':
                raise RouterError('transient')

        tracked = window.wrap(send)
        with self.assertRaises(RouterError):
            tracked('ag/gemini', None)
        tracked('ag/gemini', 'ok')
        with self.assertRaises(RouterError):
            tracked('cx/gpt', 'error')
        tracked('cx/gpt', 'ok')

        result = window.finish()
        self.assertEqual(result['usage_source'], 'router_db')
        self.assertEqual([call['usage_source'] for call in result['router_calls']],
                         ['none', 'router_db', 'router_db', 'router_db'])
        self.assertEqual([call.get('status') for call in result['router_calls']], [None, 'ok', 'error', 'ok'])
        self.assertEqual(result['router_usage']['prompt_tokens'], 300)

        window.start()
        for status in ('error', None):
            with self.assertRaises(RouterError):
                tracked('cx/gpt', status)
        self.assertEqual(window.finish(), {'usage_source': 'ambiguous', 'reason': 'foreign_row'})

    def test_cli_uses_default_db_only_when_readable(self):
        self.assertIsNone(self.usage.open_usage(self.usage.DEFAULT_DB))
        with patch.object(self.usage, 'DEFAULT_DB', self.db):
            with patch.object(self.probe.router_transport, 'chat', side_effect=self.probe_chat):
                self.assertEqual(self.probe.main(['--router-url', ROUTER_URL, '--key-file', str(self.key)]), 0)
        self.assert_report(400, 240, .005)

    def probe_chat(self, url, key, model, messages, **kwargs):
        self.add_usage(model.split('/')[1])
        content = json.dumps(dict(contract='decide/v1', groups=[dict(state='CHAT', template=None)]))
        return RouterReply(content, (), 'stop', 170, 7, None, 0, model, 1)

    def insert_delayed_usage(self, model, **usage):
        time.sleep(.3)
        self.add_usage(model.split('/')[1], **usage)

    def assert_report(self, prompt, cached, cost):
        report = self.report.getvalue()
        self.assertRegex(report, rf'Router-billed prompt tokens\s+{prompt}\b')
        self.assertRegex(report, rf'Router cached tokens\s+{cached}\b')
        self.assertRegex(report, r'Router cache ratio\s+60\.0%')
        self.assertRegex(report, r'Router cost \(USD\)\s+' + r'\$' + f'{cost:.9f}'.replace('.', r'\.'))
        self.assertIn('response', report)
        self.assertIn('usage_source: router_db', report)

    def assert_artifact(self, source, *, response_prompt=170):
        row = json.loads(self.output.read_text(encoding='utf-8'))
        self.assertEqual(row['usage_source'], source)
        self.assertEqual(row['tokens_in'], response_prompt)
        self.assertEqual(row['cached_tokens'], 0)
        if source == 'router_db':
            self.assertEqual(row['router_usage'], dict(prompt_tokens=100, cached_tokens=60,
                                                       cache_ratio=.6, cost_usd=.00125))
            self.assertEqual(row['router_calls'][0]['completion_tokens'], 7)
        else:
            self.assertNotIn('router_usage', row)

    def clear_report(self):
        artifact = self.output.read_text(encoding='utf-8') if self.output.exists() else ''
        self.assertNotIn(SECRET, self.report.getvalue() + self.errors.getvalue() + artifact)
        self.report.seek(0)
        self.report.truncate()

    def test_probe_cli_reports_billing_and_keeps_disabled_output(self):
        args = ['--router-url', ROUTER_URL, '--key-file', str(self.key)]
        with patch.object(self.probe.router_transport, 'chat', side_effect=self.probe_chat):
            self.assertEqual(self.probe.main([*args, '--router-usage-db', str(self.db)]), 0)
            self.assert_report(400, 240, .005)
            self.clear_report()
            self.assertEqual(self.probe.main(args), 0)
        self.assertIn('Input tokens\t680\t', self.report.getvalue())
        self.assertNotIn('Router-billed', self.report.getvalue())
        self.assertNotIn('(response)', self.report.getvalue())

    def test_probe_compare_attributes_four_delayed_gemini_rows_and_no_gpt_rows(self):
        metrics = {}
        run_probe = self.probe.run_probe
        gemini_usage = iter(((27407, 0), (27407, 25361), (8645, 6604), (8645, 6609)))
        inserts = []

        def chat(url, key, model, messages, **kwargs):
            if model.startswith('cx/'):
                self.probe.router_transport._status_error(503, b'The usage limit has been reached')
            prompt, cached = next(gemini_usage)
            inserts.append(executor.submit(self.insert_delayed_usage, model, prompt=prompt, cached=cached))
            content = json.dumps(dict(contract='decide/v1', groups=[dict(state='CHAT', template=None)]))
            return RouterReply(content, (), 'stop', prompt + 2000, 7, None, 0, model, 1)

        def capture(**options):
            metrics[options['pack_mode']] = options['metrics_out']
            return run_probe(**options)

        with (ThreadPoolExecutor(max_workers=1) as executor,
              patch.object(self.probe, 'run_probe', side_effect=capture),
              patch.object(self.probe.router_transport, 'chat', side_effect=chat)):
            self.assertEqual(self.probe.main(['--router-url', ROUTER_URL, '--key-file', str(self.key),
                                              '--router-usage-db', str(self.db), '--compare']), 1)
            for insert in inserts:
                insert.result()
        for rows in metrics.values():
            self.assertEqual([row['usage_source'] for row in rows], ['router_db', 'router_db', 'none', 'none'])
        self.assertEqual([row['router_usage']['cached_tokens'] for row in metrics['full'][:2]], [0, 25361])
        self.assertEqual([row['router_usage']['cached_tokens'] for row in metrics['core'][:2]], [6604, 6609])
        report = self.report.getvalue()
        self.assertIn('Router-billed prompt tokens\t54814\t17290', report)
        self.assertIn('Router cached tokens\t25361\t13213', report)
        self.assertIn('Router cost (USD)\t$0.002500000\t$0.002500000', report)
        self.assertIn('Ambiguous measurements\t0\t0', report)

    def test_probe_records_timeout_without_row_and_attributes_logged_error(self):
        calls = 0

        def chat(url, key, model, messages, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError()
            if calls == 2:
                self.add_usage(model.split('/')[1], status='error', cached=0)
                raise RouterError('transient')
            return self.probe_chat(url, key, model, messages, **kwargs)

        rows = []
        self.assertEqual(self.probe.run_probe(router_url=ROUTER_URL, key_file=self.key, chat=chat,
                                             metrics_out=rows, router_usage_db=self.db), 1)
        self.assertEqual([row['usage_source'] for row in rows], ['none', 'router_db', 'router_db', 'router_db'])
        self.assertEqual(rows[1]['router_calls'][0]['status'], 'error')
        self.assertEqual(rows[1]['router_usage']['cost_usd'], .00125)
        self.assertIn('Router status: ["error"]', self.report.getvalue())

    def test_probe_flags_concurrent_row_only_for_affected_call(self):
        calls = 0

        def chat(url, key, model, messages, **kwargs):
            nonlocal calls
            calls += 1
            reply = self.probe_chat(url, key, model, messages, **kwargs)
            if calls == 2:
                self.add_usage('foreign', prompt=900, cost=42)
            return reply

        rows = []
        self.assertEqual(self.probe.run_probe(router_url=ROUTER_URL, key_file=self.key, chat=chat,
                                             metrics_out=rows, router_usage_db=self.db), 0)
        self.assertEqual([row['usage_source'] for row in rows],
                         ['router_db', 'ambiguous', 'router_db', 'router_db'])
        self.assertNotIn(SECRET, json.dumps(rows))
        self.p8.print_pack_comparison({'full': rows})
        self.assertIn('usage_source: ambiguous', self.report.getvalue())
        self.assertIn('Ambiguous measurements\t1', self.report.getvalue())
        self.assertIn('Router cost (USD)\t$0.003750000', self.report.getvalue())

    def test_eval_cli_reports_billing_and_keeps_disabled_output(self):
        snapshot = self.p8._load_decision_rules()
        case = self.p8.cases(snapshot)[0]

        def chat(url, key, model, messages, **kwargs):
            self.add_usage(model.split('/')[1])
            facts = {name: dict(value=value, source_ids=[1]) for name, value in
                     [('intent', 'bug'), ('object', 'máy in POS'), ('observed', 'không in được receipt')]}
            content = json.dumps(dict(groups=[dict(message_ids=[1], facts=facts)]))
            return RouterReply(content, (), 'stop', 170, 7, None, 0, model, 1)

        args = ['--router-url', ROUTER_URL, '--key-file', str(self.key), '--output', str(self.output)]
        with (patch.object(self.p8, 'cases', return_value=(case,)),
              patch.object(self.p8.router_transport, 'chat', side_effect=chat)):
            self.assertEqual(self.p8.main([*args, '--router-usage-db', str(self.db)]), 0)
            self.assert_report(100, 60, .00125)
            self.assert_artifact('router_db')
            self.clear_report()
            self.assertEqual(self.p8.main(args), 0)
        self.assert_artifact('response')
        self.assertIn('Tokens in / out                170 / 7', self.report.getvalue())
        self.assertNotIn('Router-billed', self.report.getvalue())
        self.assertNotIn('(response)', self.report.getvalue())

    def test_replay_cli_reports_billing_and_keeps_disabled_output(self):
        source = self.directory / 'listener-copy.sqlite'
        store = Store(str(source))
        try:
            message = helpers.message()
            store.enqueue(message)
            with store.connection:
                store.connection.execute("""
                    INSERT INTO classification_runs
                        (created_at, turn_key, phase, batch_fingerprint, chat_id, message_ids,
                         id_space, rules_fingerprint, final_state)
                    VALUES (0, 'fixture', 'extract', 'batch', ?, '[1]', 'message', 'rules', 'CHAT')
                """, (message.chat_id,))
        finally:
            store.close()

        def factory(config, store, pack, *, chat=None):
            class FakeRuntime:
                def decide(runtime, job):
                    model = config.assistant.models[0]
                    if chat is None:
                        reply = self.probe_chat(ROUTER_URL, 'fixture-key', model, [])
                    else:
                        reply = chat(model, [])
                    with store.connection:
                        run_id = store.connection.execute("""
                            INSERT INTO classification_runs
                                (created_at, turn_key, phase, batch_fingerprint, chat_id, message_ids,
                                 id_space, rules_fingerprint, model_calls)
                            VALUES (0, 'fixture', ?, 'batch', ?, '[1]', 'message', 'rules', 1)
                        """, (PHASE_DECIDE, job['chat_id'])).lastrowid
                        store.connection.execute("""
                            INSERT INTO classification_model_calls
                                (run_id, seq, link, model, started_at, prompt_tokens, completion_tokens, cached_tokens)
                            VALUES (?, 1, 'gemini', ?, 0, ?, ?, ?)
                        """, (run_id, model, reply.prompt_tokens, reply.completion_tokens, reply.cached_tokens))
                    return [], run_id, 'done'
            return FakeRuntime()

        args = ['--db', str(source), '--config', 'unused.toml', '--out', str(self.output)]
        replay = self.replay.run_replay
        with (patch.object(self.p8, 'offline_config', return_value=helpers.config()),
              patch.object(self.replay, 'run_replay', side_effect=lambda **kw: replay(**kw, runtime_factory=factory)),
              patch.object(self.replay.router_transport, 'chat', side_effect=self.probe_chat)):
            self.assertEqual(self.replay.main([*args, '--router-usage-db', str(self.db)]), 0)
            self.assert_report(100, 60, .00125)
            self.assert_artifact('router_db')
            self.clear_report()
            self.assertEqual(self.replay.main(args), 0)
        self.assert_artifact('response')
        self.assertIn('Tokens in / out                170 / 7', self.report.getvalue())
        self.assertNotIn('Router-billed', self.report.getvalue())
        self.assertNotIn('(response)', self.report.getvalue())
