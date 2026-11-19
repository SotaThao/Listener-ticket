"""Offline checks for admin capture through the production assistant wiring."""
import io
import json
import sqlite3
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

from tg_triage import classification_log, router_transport
from tg_triage.__main__ import _load_decision_rules
from tg_triage.assistant_extractor import AssistantExtractor
from tg_triage.config import AssistantConfig, ModelConfig
from tg_triage.pipeline import Pipeline
from tg_triage.rules_extraction import parse_grouped_facts, partition_and_extract
from tg_triage.store import Store
from tests.helpers import NOW, config, message
from tests.test_assistant_integration import Clock, Provider, proposal, reply


PHONE = '0901234567'
OLD_TOOL_SCHEMA = '''CREATE TABLE classification_tool_calls (
    run_id INTEGER NOT NULL, seq INTEGER NOT NULL, tool TEXT NOT NULL, args_json TEXT,
    result_bytes INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
    error TEXT, PRIMARY KEY(run_id, seq))'''


class AdminCaptureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'capture.db'
        self.store = Store(self.path, clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.snapshot = _load_decision_rules()
        self.clock = Clock()
        self.settings = replace(config(), db_path=str(self.path), autonomous_enabled=True,
                                decision_rules_enabled=True,
                                assistant=AssistantConfig(enabled=True, router_key='fake-router-key'))

    def runtime(self, chat, **settings):
        return AssistantExtractor(replace(self.settings, **settings), self.store, 'Assistant pack',
                                  chat=chat, clock=self.clock,
                                  wall_clock=lambda: NOW.timestamp() + self.clock())

    def job(self):
        provider = Provider()
        sources = [message(42, text='POS jammed')]
        bodies = []
        partition_and_extract(provider, self.snapshot, sources, body_out=bodies)
        return dict(provider=provider, snapshot=self.snapshot, sources=sources, body=bodies[0],
                    parse=lambda raw: parse_grouped_facts(raw, self.snapshot, [42]),
                    data=dict(images={}, documents=[], read_context=()))

    def rows(self, table):
        return [dict(row) for row in self.store.connection.execute(
            f'SELECT * FROM classification_{table} ORDER BY run_id, seq')]

    def test_capture_truncates_before_redaction_without_store_lock(self):
        capture = classification_log.capture_text
        bounded = classification_log.bounded_text
        oversized = '{"query":"\\u0030' + PHONE[1:] + 'x' * (4 * 1024 * 1024) + '"}'

        def redact_unlocked(value):
            self.assertFalse(self.store._lock._is_owned())
            if value is not None:
                self.assertLessEqual(len(value.encode('utf-8')), 16384)
            return capture(value)

        def bound_unlocked(*args):
            self.assertFalse(self.store._lock._is_owned())
            return bounded(*args)

        with patch.object(classification_log, 'capture_text', side_effect=redact_unlocked) as redact, \
                patch.object(classification_log, 'bounded_text', side_effect=bound_unlocked) as bound:
            classification_log.record_tool_call(self.store, 1, 1, 'search_tickets', args_full_json=oversized)
            classification_log.record_model_call(self.store, 1, 1, link='gemini', started_at=0,
                                                 content_text='x' * (4 * 1024 * 1024))
        redact.assert_called_once()
        self.assertGreaterEqual(bound.call_count, 6)
        tools = self.rows('tool_calls')
        models = self.rows('model_calls')
        self.assertEqual(tools[0]['args_full_json'], '{"query":' + router_transport.CAPTURE_TRUNCATION)
        self.assertEqual(len(models[0]['content_text'].encode('utf-8')), 65536)

    def test_two_tools_and_json_repair_capture_order_and_redaction_in_shadow_and_live(self):
        first_args = '{ "query": "call ' + PHONE + '" }'
        second_args = '{"query":"also \\u0030' + PHONE[1:] + '"}'
        calls = [router_transport.ToolCall('a', 'search_tickets', first_args),
                 router_transport.ToolCall('b', 'search_tickets', second_args)]
        rejected = '{"detail": "call ' + PHONE + '"}'
        replies = [replace(reply(calls=[call]), content=None) for call in calls]
        replies.extend((replace(reply(), content=rejected), reply()))

        for mode in ('shadow', 'live'):
            with self.subTest(mode=mode):
                sent = []
                responses = iter(replies)

                def chat(model, messages, **kw):
                    sent.append(messages)
                    self.clock.advance(1)
                    return replace(next(responses), reasoning_text=f'Step {len(sent)} call {PHONE}')

                def search(**kwargs):
                    self.clock.advance(1)
                    return [f'Call {PHONE}']

                runtime = self.runtime(chat, assistant=replace(self.settings.assistant, mode=mode))
                job = self.job()
                started_at = NOW.timestamp() + self.clock()
                with patch('tg_triage.assistant_extractor.GitHubReader') as reader:
                    reader.return_value.search_tickets.side_effect = search
                    result = runtime.run(job)
                    self.assertEqual(reader.return_value.search_tickets.call_count, 2)
                self.assertEqual((result.outcome, result.model_calls, result.tool_calls), ('ok', 4, 2))
                self.assertIn('Repair once', sent[-1][-1]['content'])
                models = [r for r in self.rows('model_calls') if r['run_id'] == job['run_id']]
                tools = [r for r in self.rows('tool_calls') if r['run_id'] == job['run_id']]
                self.assertEqual([r['seq'] for r in models], [1, 2, 3, 4])
                self.assertEqual([r['link'] for r in models], ['gemini'] * 4)
                self.assertEqual([r['started_at'] - started_at for r in models], [0, 2, 4, 5])
                self.assertEqual([r['reasoning_text'] for r in models],
                                 [f'Step {n} call [phone]' for n in range(1, 5)])
                self.assertEqual([r['content_text'] for r in models],
                                 [None, None, rejected.replace(PHONE, '[phone]'), replies[-1].content])
                self.assertEqual([r['schema_error_kind'] for r in models], [None, None, 'invalid_shape', None])
                self.assertEqual([r['error_class'] for r in models], [None, None, 'schema', None])
                self.assertEqual(json.loads(models[0]['tool_calls_json']),
                    [dict(id='a', name='search_tickets', arguments=first_args.replace(PHONE, '[phone]'))])
                self.assertEqual([r['seq'] for r in tools], [1, 2])
                self.assertEqual([r['started_at'] - started_at for r in tools], [1, 3])
                self.assertEqual([json.loads(r['args_full_json']) for r in tools],
                                 [{'query': 'call [phone]'}, {'query': 'also [phone]'}])
                self.assertEqual([r['result_json'] for r in tools],
                                 [m['content'] for m in sent[-1] if m['role'] == 'tool'])
                serialized = json.dumps((models, tools))
                self.assertIn('[phone]', serialized)
                self.assertNotIn(PHONE, serialized)
                self.assertNotIn(PHONE[1:], serialized)

    def test_failed_router_calls_and_deepseek_fallback(self):
        settings = ModelConfig(provider='deepseek', name='deepseek-fallback', cloud_consent=True)
        for fallback_error in (None, 'auth_failed', 'invalid_json', 'invalid_shape'):
            with self.subTest(fallback_error=fallback_error):
                from agentfleet_common.ai_transport import ProviderError
                runtime = self.runtime(Mock(side_effect=[router_transport.RouterError('transient'),
                                                          router_transport.RouterError('auth')]), model=settings)

                def complete(*args, **kw):
                    if fallback_error in ('auth_failed', 'invalid_json'):
                        raise ProviderError(fallback_error)
                    kw['usage_sink'](dict(prompt_tokens=17, completion_tokens=5, cache_hit_tokens=3))
                    return {'rejected': PHONE} if fallback_error else proposal()

                job = self.job()
                with patch('tg_triage.assistant_extractor.read_key', return_value='fake-key'), patch(
                        'tg_triage.thinking_transport.complete', side_effect=complete) as transport:
                    result = runtime.run(job)
                self.assertEqual(result.model_calls, 3)
                transport.assert_called_once()
                rows = [r for r in self.rows('model_calls') if r['run_id'] == job['run_id']]
                self.assertEqual([(r['seq'], r['link']) for r in rows], [(1, 'gemini'), (2, 'gpt'), (3, 'deepseek')])
                self.assertEqual([r['error_class'] for r in rows[:2]], ['transient', 'auth'])
                self.assertEqual([r['content_text'] for r in rows[:2]], [None, None])
                fallback = rows[-1]
                self.assertEqual((fallback['model'], fallback['timeout_s'], fallback['reasoning_text']),
                                 ('deepseek-fallback', 40, None))
                if fallback_error == 'auth_failed':
                    self.assertEqual((fallback['error_class'], fallback['content_text']), ('auth', None))
                elif fallback_error == 'invalid_json':
                    self.assertEqual((fallback['error_class'], fallback['schema_error_kind'], fallback['content_text']),
                                     ('schema', 'invalid_json', None))
                else:
                    self.assertEqual((fallback['prompt_tokens'], fallback['completion_tokens'],
                                      fallback['cached_tokens']), (17, 5, 3))
                    if fallback_error:
                        self.assertEqual(fallback['schema_error_kind'], 'invalid_shape')
                        self.assertEqual(json.loads(fallback['content_text']), {'rejected': '[phone]'})
                    else:
                        self.assertEqual(json.loads(fallback['content_text']), proposal())

    def test_router_bounds_70_kb_utf8_reasoning_without_changing_request(self):
        messages = [{'role': 'user', 'content': 'hello'}]
        for thinking in ('あ' * 23334, None, {'unexpected': True}):
            with self.subTest(type=type(thinking)):
                response = io.BytesIO(json.dumps({'choices': [{'message': {
                    'content': 'answer', 'reasoning_content': thinking}}]}).encode())
                response.status = 200
                opener = Mock(spec=[], return_value=response)
                result = router_transport.chat('http://127.0.0.1:8080/v1', 'fake-router-key',
                                               'test-model', messages, timeout=3, opener=opener)
                self.assertEqual(json.loads(opener.call_args.args[0].data),
                                 dict(model='test-model', messages=messages, stream=False))
                if isinstance(thinking, str):
                    self.assertLessEqual(len(result.reasoning_text.encode('utf-8')), 65536)
                    self.assertGreater(len(result.reasoning_text.encode('utf-8')), 65533)
                    self.assertTrue(result.reasoning_text.endswith('\u2026[truncated]'))
                    self.assertTrue(thinking.startswith(result.reasoning_text.split('\u2026')[0]))
                    self.runtime(Mock(return_value=replace(reply(), reasoning_text=result.reasoning_text))).run(self.job())
                    self.assertEqual(self.rows('model_calls')[0]['reasoning_text'], result.reasoning_text)
                else:
                    self.assertIsNone(result.reasoning_text)

    def test_router_error_excludes_credentials_and_response_text(self):
        key = 'fake-router-key'
        malformed = io.BytesIO(json.dumps({'choices': [{'message': {'reasoning_content': key}}]}).encode())
        malformed.status = 200
        for opener in (Mock(spec=[], return_value=malformed), Mock(spec=[], side_effect=RuntimeError(key))):
            with self.assertRaisesRegex(router_transport.RouterError, '^schema$') as error:
                router_transport.chat('http://127.0.0.1:8080/v1', key, 'test-model', [], timeout=3, opener=opener)
            self.assertTrue(error.exception.__suppress_context__)

    def test_capture_writer_errors_do_not_break_turn_or_log_payloads(self):
        for table in ('classification_model_calls', 'classification_tool_calls'):
            self.store.connection.execute(f'''CREATE TEMP TRIGGER fail_{table} BEFORE INSERT ON {table}
                BEGIN SELECT RAISE(ABORT, 'fake-router-key private-text'); END''')
        call = router_transport.ToolCall('a', 'get_rule', '{"rule_id":"R9"}')
        runtime = self.runtime(Mock(side_effect=[reply(calls=[call]), reply()]))
        with self.assertLogs('tg_triage.classification_log', level='WARNING') as logs:
            result = runtime.run(self.job())
        self.assertEqual((result.outcome, result.model_calls, result.tool_calls), ('ok', 2, 1))
        self.assertEqual(len(logs.output), 3)
        self.assertNotIn('fake-router-key', '\n'.join(logs.output))
        self.assertNotIn('private-text', '\n'.join(logs.output))
        self.assertEqual(self.rows('model_calls'), [])
        self.assertEqual(self.rows('tool_calls'), [])

    def test_retention_deletes_model_and_tool_rows_of_expired_runs(self):
        job = self.job()
        call = router_transport.ToolCall('a', 'get_rule', '{"rule_id":"R9"}')
        self.runtime(Mock(side_effect=[reply(calls=[call]), reply()])).run(job)
        expired = job['run_id']
        with self.store.connection:
            self.store.connection.execute('UPDATE classification_runs SET created_at=? WHERE run_id=?',
                                          (NOW.timestamp() - 91 * 86400, expired))
        self.runtime(Mock(return_value=reply())).run(job)
        classification_log.maintain(self.store, retention_days=90)
        self.assertEqual([r['run_id'] for r in self.rows('model_calls')], [job['run_id']])
        self.assertEqual(self.rows('tool_calls'), [])
        self.assertIsNone(self.store.connection.execute('SELECT 1 FROM classification_runs WHERE run_id=?',
                                                       (expired,)).fetchone())

    def test_disabled_assistant_ab_byte_identity_and_no_capture_rows(self):
        results = []
        for explicit in (False, True):
            path = self.root / f'ab-{explicit}.db'
            store = Store(path, clock=lambda: NOW)
            self.addCleanup(store.close)
            settings = replace(self.settings, db_path=str(path), assistant=(
                AssistantConfig(enabled=False, mode='live') if explicit else AssistantConfig()))
            pipeline = Pipeline(settings, store, Provider(), Mock(), rules_snapshot=self.snapshot, timer=lambda: 0)
            with patch('tg_triage.router_transport.chat') as chat, patch('time.monotonic', return_value=0):
                decision = pipeline.process(message(42, text='POS jammed'))
                pipeline.close_due_cases()
            chat.assert_not_called()
            self.assertIsNone(pipeline.assistant_runtime)
            for table in ('classification_model_calls', 'classification_tool_calls'):
                self.assertEqual(store.connection.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0)
            runs = [dict(r) for r in store.connection.execute('SELECT * FROM classification_runs ORDER BY run_id')]
            self.assertTrue(runs)
            self.assertTrue(all(not r['phase'].startswith('assistant_') for r in runs))
            results.append(json.dumps(dict(decision=asdict(decision), runs=runs), sort_keys=True).encode())
        self.assertEqual(*results)

    def test_migrates_0ae89c6_tool_schema_preserving_rows_and_reopening(self):
        path = self.root / 'legacy.db'
        old = sqlite3.connect(path)
        old_row = (1, 1, 'get_rule', '{"type":"object"}', 9, 3, None)
        try:
            with old:
                old.execute(OLD_TOOL_SCHEMA)
                old.execute('INSERT INTO classification_tool_calls VALUES (?,?,?,?,?,?,?)', old_row)
        finally:
            old.close()
        for _ in range(2):
            store = Store(path)
            try:
                row = store.connection.execute('SELECT * FROM classification_tool_calls').fetchone()
                self.assertEqual(tuple(row), (*old_row, None, None, None))
                self.assertEqual(row.keys()[-3:], ['result_json', 'started_at', 'args_full_json'])
                self.assertIsNotNone(store.connection.execute('''SELECT name FROM sqlite_master
                    WHERE name='classification_model_calls' AND type='table' ''').fetchone())
            finally:
                store.close()
