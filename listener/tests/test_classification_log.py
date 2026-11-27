"""Offline attempt accounting, privacy, behavior parity and read-only reporting."""
import contextlib
import asyncio
import io
import json
import sqlite3
import socket
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tg_triage import classification_log as log
from tg_triage.__main__ import _load_decision_rules, main
from tg_triage.config import AssistantConfig, ConfigError
from tg_triage.decision_rules import RulesSnapshot
from tg_triage.legacy_preflight import REGISTRY, inspect
from tg_triage.pipeline import Pipeline
from tg_triage.rules_intake import evaluate_batch, _process_batch, sweep
from tg_triage.rules_pending_store import RulesPendingStore, check_before_recovery
from tg_triage.store import Store
from tg_triage.thinking import CURRENT, Turn
from tests import test_usage_fallback as fallback
from tests.helpers import load_with_test_secrets
from tests.helpers import Clock, EXAMPLE, FakeGitHub, FakeHTTP, config, message


def payload(ids=(42,), marker='POS'):
    return json.dumps({'groups': [{'message_ids': list(ids), 'facts': {
        'intent': {'value': 'bug', 'source_ids': [ids[0]]},
        'object': {'value': marker, 'source_ids': [ids[0]]},
        'observed': {'value': 'kẹt giấy', 'source_ids': [ids[0]]}}}]})


class ClassificationLogTests(fallback.ProviderBase):
    def setUp(self):
        super().setUp()
        self.snapshot = _load_decision_rules()
        self.clock = Clock(datetime.now(timezone.utc))
        self.store.clock = self.clock
        self.socket_connect = socket.socket.connect
        patch('socket.socket.connect', side_effect=AssertionError('unexpected network')).start()

    def provider(self, upstream, http=None, **model):
        provider = super().provider(upstream, http, **model)
        # Active rules Turns use the deadline adapter; intercept its boundary too.
        patch('tg_triage.thinking_transport.complete',
              lambda *args, **kwargs: fallback.REAL_COMPLETE(*args, transport=upstream, **kwargs)).start()
        return provider

    @contextlib.contextmanager
    def turn(self, store=None, key='rules:test', topic='test'):
        turn = Turn(store or self.store, key, topic, 42)
        token = CURRENT.set(turn)
        try:
            yield turn
        finally:
            CURRENT.reset(token)

    def runs(self, store=None):
        return [dict(row) for row in (store or self.store).connection.execute(
            'SELECT * FROM classification_runs ORDER BY run_id')]

    def evaluate(self, provider, sources=None, **kwargs):
        with self.turn():
            return evaluate_batch(self.snapshot, provider, sources or [message(42, 'POS kẹt giấy')], **kwargs)

    def hosted(self, content=None, **kwargs):
        return self.provider(fallback.Upstream(fallback.deepseek_envelope(content or payload())), **kwargs)

    def add_run(self, *, age=0, state='CHAT', **kwargs):
        with self.turn(), log.run(self.snapshot, [message(42)]) as attempt:
            attempt.values.update(created_at=self.clock().timestamp() - age * 86400, **kwargs)
            attempt.result = ((tuple([message(42)]), self.snapshot.prefilter_decision('F4')),)
            if state != 'CHAT':
                attempt.result = ((tuple([message(42)]), replace(attempt.result[0][1], state=state)),)
        return self.runs()[-1]['run_id']

    def test_schema_new_old_reopen_preserves_all_old_tables(self):
        path = Path(self.tmp.name) / 'old.sqlite3'
        store = Store(path, recover_pending=False)
        store.enqueue(message(42, 'PRIVATE-old'))
        store.record_model_usage(1, 'ollama', 'm', 'primary', 'ok', 10)
        store.connection.execute('DROP TABLE classification_tool_calls')
        store.connection.execute('DROP TABLE classification_runs')
        store.connection.commit()
        tables = [r[0] for r in store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        def old_bytes(connection):
            return {name: json.dumps([tuple(row) for row in connection.execute('SELECT * FROM "' + name + '"')],
                                     sort_keys=True).encode() for name in tables}
        before = old_bytes(store.connection)
        store.close()
        for _ in range(2):
            store = Store(path, recover_pending=False)
            self.assertEqual(old_bytes(store.connection), before)
            self.assertEqual(len(self.runs(store)), 0)
            columns = {r[1]: r for r in store.connection.execute('PRAGMA table_info(classification_runs)')}
            self.assertEqual(columns['chat_id'][2:4], ('INTEGER', 1))
            self.assertEqual(len(list(store.connection.execute('PRAGMA foreign_key_list(classification_tool_calls)'))), 0)
            index_names = {row['name'] for row in store.connection.execute('PRAGMA index_list(classification_runs)')}
            self.assertEqual(index_names, {
                'idx_classification_created', 'idx_classification_batch', 'idx_classification_topic',
                'idx_classification_chat_run', 'idx_classification_thread',
            })
            store.close()

    def test_one_run_totals_equal_actual_model_usage(self):
        pending = RulesPendingStore(self.store)
        pending.create('test', message().chat_id, 42)
        self.assertEqual(pending.reserve_attempt('test', 1), 1)
        result = self.evaluate(self.hosted())
        self.assertEqual(result[0][1].state, 'READY')
        row, = self.runs()
        usage = self.store.model_usage_since(0)
        self.assertEqual((row['model_calls'], row['tokens_in'], row['tokens_out'], row['cache_hit_tokens']),
                         (len(usage), sum(r[5] for r in usage), sum(r[6] for r in usage), sum(r[7] for r in usage)))
        self.assertAlmostEqual(row['cost'], sum(r[8] for r in usage))
        self.assertEqual((row['turn_key'], row['topic_id'], row['phase'], row['attempt_no']),
                         ('rules:test', 'test', 'extract', 1))
        self.assertEqual((row['template'], row['mode'], row['circuit_state']), ('bug.md', 'live', 'none'))
        for name in ('reasoning_tokens', 'finish_reason', 'fallback_reason', 'error_class', 'retry_at',
                     'effort', 'reasoning_summary', 'rules_version', 'ticket_ref'):
            self.assertIsNone(row[name], name)
        self.assertEqual(row['rules_fingerprint'], self.snapshot.fingerprint)
        self.assertEqual(row['created_at'], self.clock().timestamp())
        self.assertEqual(row['chat_id'], message().chat_id)
        self.assertEqual(json.loads(row['message_ids']), [42])
        self.assertGreaterEqual(row['latency_ms'], 0)
        self.assertIsNone(row['error'])

    def test_real_provider_fallback_records_both_calls(self):
        provider = self.provider(fallback.Upstream(fallback.ProviderError('rate_limited')),
                                 FakeHTTP({'message': {'content': payload()}, 'prompt_eval_count': 410, 'eval_count': 77}))
        self.assertEqual(self.evaluate(provider)[0][1].state, 'READY')
        row, = self.runs()
        usage = self.store.model_usage_since(0)
        self.assertEqual((row['fallback'], row['model_calls'], row['error']), (1, 2, 'rate_limited'))
        self.assertEqual(row['tokens_in'], sum(r[5] for r in usage))
        self.assertEqual(row['tokens_out'], sum(r[6] for r in usage))
        self.assertEqual(row['cost'], sum(r[8] for r in usage))
        self.assertIsNone(row['fallback_reason'])

    def test_proposal_never_stores_content_or_task_text(self):
        marker = 'PRIVATE-classification-2914'
        result = self.evaluate(self.hosted(payload(marker=marker)), [message(42, marker + ' kẹt giấy')])
        self.assertIsNotNone(result)
        row, = self.runs()
        self.assertNotIn(marker, json.dumps(row))
        proposal, = json.loads(row['proposal_json'])
        self.assertEqual(proposal['fields']['object'], [42])
        self.assertEqual(proposal['intent'], 'bug')
        with self.turn(), log.run(self.snapshot, [message(42)], {42: Path(marker)}) as attempt:
            log.note_proposal([([42], {'intent': {'value': 'unknown', 'source_ids': [42]},
                'task': {'title': marker}, 'lifecycle': {'kind': 'unclear'},
                'media_ids': {'value': [42, marker], 'source_ids': [42, marker]},
                marker: {'source_ids': [42], 'value': marker}})], self.snapshot)
            attempt.result = result
        row = self.runs()[-1]
        self.assertNotIn(marker, json.dumps(row))
        proposal, = json.loads(row['proposal_json'])
        self.assertEqual((proposal['intent'], proposal['has_task'], proposal['lifecycle_kind']), ('other', True, 'unclear'))
        self.assertEqual(json.loads(row['image_ids']), [42])
        self.assertEqual(proposal['media_ids'], [42])

    def test_prefilter_and_early_decision_use_zero_calls(self):
        for text in ('https://github.com/org/repo', '/question sao?', 'cảm ơn'):
            with self.subTest(text=text):
                result = self.evaluate(object(), [message(42, text)])
                row = self.runs()[-1]
                self.assertEqual((result[0][1].state, row['final_state'], row['model_calls']), ('CHAT', 'CHAT', 0))
        self.assertEqual(self.store.model_usage_since(0), [])
        now = self.clock()
        with self.turn(), patch.object(self.store, 'clock', side_effect=[now, now + timedelta(milliseconds=125)]):
            evaluate_batch(self.snapshot, object(), [message(42, '/question sao?')])
        self.assertEqual(self.runs()[-1]['latency_ms'], 125)

    def test_frozen_store_clock_produces_identical_run_rows(self):
        rows = []
        for elapsed in (0.004, 0.051):
            store = Store(':memory:', clock=self.clock)
            self.addCleanup(store.close)
            # Host timer jitter must not affect a run using the frozen store clock.
            with self.turn(store), patch('time.perf_counter', side_effect=[100, 100 + elapsed]):
                result = evaluate_batch(self.snapshot, object(), [message(42, '/question sao?')])
            self.assertEqual(result[0][1].state, 'CHAT')
            row, = self.runs(store)
            rows.append(row)
        self.assertEqual(rows[0], rows[1])
        self.assertEqual(rows[0]['latency_ms'], 0)

    def test_batch_pending_merge_records_ref_topic_instead_of_batch_root(self):
        root = Path(__file__).resolve().parents[1]
        snapshot = RulesSnapshot(root / 'rules', root / 'templates/github-ticket', extensions=False)
        class Provider:
            settings = SimpleNamespace(model='test')
            think = False
            supports_vision = False
            def __init__(self):
                self.calls = []
            def _ask(self, body, parse, attempts):
                ids = json.loads(body['messages'][1]['content'])['source_ids']
                self.calls.append(tuple(ids))
                if ids == [10]:
                    groups = [{'message_ids': ids, 'facts': {'intent': {'value': 'bug', 'source_ids': ids}}}]
                elif set(ids) == {11, 12}:
                    groups = [{'message_ids': [mid], 'facts': {
                        'intent': {'value': 'bug' if mid == 11 else 'chat', 'source_ids': [mid]}}} for mid in ids]
                else:
                    groups = [{'message_ids': ids, 'facts': {
                        'intent': {'value': 'bug', 'source_ids': [1]},
                        'object': {'value': 'POS', 'source_ids': [2]},
                        'observed': {'value': 'kẹt giấy', 'source_ids': [2]}}}]
                return parse(json.dumps({'groups': groups}))
        provider = Provider()
        settings = replace(config(), autonomous_enabled=True, decision_rules_enabled=True,
                           telegram=replace(config().telegram, ops_chat_id=-1009876543210))
        pipeline = Pipeline(settings, self.store, provider, rules_snapshot=snapshot)
        original = message(10, 'bị lỗi')
        self.assertEqual(pipeline.process(original).decision, 'pending_review')
        topic = f'{original.chat_id}:10'
        incoming = message(11, 'POS kẹt giấy', reply_to_id=10)
        other = message(12, 'cảm ơn')
        self.store.enqueue(incoming)
        self.store.enqueue(other)
        self.assertEqual(_process_batch(pipeline, [incoming, other]).decision, 'dry_run_issue')
        self.assertEqual(provider.calls, [(10,), (11, 12), (1, 2)])
        row, = [row for row in self.runs() if row['phase'] == 'pending_merge']
        self.assertEqual(row['turn_key'], f'rules:batch:{original.chat_id}:11')
        self.assertEqual(row['topic_id'], topic)
        self.assertEqual(row['id_space'], 'topic_ref')
        self.assertEqual(json.loads(row['message_ids']), [1, 2])
        mapped = self.store.connection.execute(
            "SELECT input_id,chat_id,message_id FROM classification_run_sources WHERE run_id=? ORDER BY ordinal",
            (row["run_id"],),
        )
        self.assertEqual([(1, original.chat_id, 10), (2, incoming.chat_id, 11)],
                         [tuple(link) for link in mapped])
        refs = [replace(original, message_id=1), replace(incoming, message_id=2, reply_to_id=1)]
        with self.turn(topic=topic), log.run(snapshot, refs, phase='pending_merge'):
            pass
        self.assertEqual(row['batch_fingerprint'], self.runs()[-1]['batch_fingerprint'])

    def test_bad_model_output_holds_and_turn_error_takes_priority(self):
        provider = self.provider(fallback.Upstream(fallback.deepseek_envelope('{}'), fallback.deepseek_envelope('{}')),
                                 fallback='none')
        self.assertIsNone(self.evaluate(provider))
        row, = self.runs()
        self.assertEqual((row['final_state'], row['error']), ('HOLD', 'invalid_output'))
        with self.turn() as turn:
            turn.reason = 'decision_deadline_exhausted'
            with log.run(self.snapshot, [message(42)]):
                pass
        self.assertEqual(self.runs()[-1]['error'], 'decision_deadline_exhausted')

    def test_pending_and_confirmation_reference_space_and_multi_outcomes(self):
        for options, phase in (({'allow_mixed': True}, 'pending_merge'),
                               ({'confirm_followup': True}, 'followup_confirm')):
            with self.subTest(phase=phase):
                result = self.evaluate(self.hosted(), **options)
                self.assertIsNotNone(result)
                row = self.runs()[-1]
                self.assertEqual((row['id_space'], row['phase']), ('topic_ref', phase))
                if phase == 'followup_confirm':
                    self.assertIsNone(row['attempt_no'])
        content = json.dumps({'groups': [{'message_ids': [i], 'facts': {'intent': {'value': 'chat', 'source_ids': [i]}}}
                                          for i in (41, 42)]})
        result = self.evaluate(self.hosted(content), [message(41), message(42)])
        self.assertEqual(len(result), 2)
        row = self.runs()[-1]
        self.assertEqual(row['final_state'], 'MULTI')
        self.assertIsNone(row['decision_rule'])
        self.assertEqual(len(json.loads(row['outcomes_json'])), 2)

    def test_no_current_classic_and_unread_document_create_no_runs(self):
        self.assertIsNotNone(evaluate_batch(self.snapshot, object(), [message(42, '/question sao?')]))
        with self.turn(key='classic:test'):
            evaluate_batch(self.snapshot, object(), [message(42, '/question sao?')])
        pipeline = Pipeline(replace(config(), decision_rules_enabled=True, autonomous_enabled=True), self.store,
                            object(), rules_snapshot=self.snapshot)
        pipeline.process(message(43, 'đọc nội dung trong file này', has_document=True,
                                 document_name='proof.txt', document_mime='text/plain'))
        self.assertEqual(self.runs(), [])

    def test_real_pipeline_early_decisions_are_logged_once_without_model_or_startup_warning(self):
        for text, rule in (('ok', 'F4'), ('cảm ơn', 'F4'), ('/question sao?', 'R1')):
            with self.subTest(text=text):
                store = Store(':memory:', clock=self.clock)
                self.addCleanup(store.close)
                settings = replace(config(), autonomous_enabled=True, decision_rules_enabled=True)
                pipeline = Pipeline(settings, store, object(), rules_snapshot=self.snapshot)
                self.assertFalse(store.connection.in_transaction)
                with self.assertNoLogs('tg_triage.classification_log', level='WARNING'):
                    log.maintain(store)
                    self.assertFalse(store.connection.in_transaction)
                    source = message(42, text, mentions_listener=False)
                    store.enqueue(source)
                    self.assertFalse(store.connection.in_transaction)
                    result = pipeline.process(source)
                self.assertEqual((result.decision, result.reason), ('chat', rule))
                row, = self.runs(store)
                self.assertEqual((row['phase'], row['final_state'], row['decision_rule'], row['model_calls']),
                                 ('early', 'CHAT', rule, 0))
                self.assertEqual(row['topic_id'], f'{source.chat_id}:42')
                self.assertEqual(row['turn_key'], 'rules:' + row['topic_id'])
                self.assertEqual(json.loads(row['message_ids']), [42])
                for name in ('proposal_json', 'outcomes_json', 'provider', 'model'):
                    self.assertIsNone(row[name])
                self.assertEqual(store.model_usage_since(0), [])
                self.assertFalse(getattr(store, '_classification_pending', None))
                sweep(pipeline)
                self.assertIsNone(pipeline.process(source))
                self.assertEqual(self.runs(store), [row])

    def test_early_logging_preserves_decision_state_and_current_scope_and_isolates_failure(self):
        observations = []
        for enabled in (False, True):
            store = Store(':memory:', clock=self.clock)
            self.addCleanup(store.close)
            settings = replace(config(), autonomous_enabled=True, decision_rules_enabled=True)
            github = FakeGitHub()
            pipeline = Pipeline(settings, store, object(), github, rules_snapshot=self.snapshot)
            source = message(42, '/question PRIVATE-marker', mentions_listener=False)
            record = log.record_decision
            with patch.object(log, 'record_decision', record if enabled else lambda *a, **kw: None):
                result = pipeline.process(source)
            state = {name: [tuple(row) for row in store.connection.execute('SELECT * FROM ' + name)]
                     for name in ('rules_topics', 'messages', 'rules_decisions', 'thinking_decisions')}
            observations.append((result, github.calls, state))
            if enabled:
                row, = self.runs(store)
                self.assertNotIn('PRIVATE', json.dumps(row))
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(observations[1][1], [])
        source = message(43, '/question sao?')
        pipeline = Pipeline(settings, self.store, object(), rules_snapshot=self.snapshot)
        self.store.connection.execute('DROP TABLE classification_runs')
        with self.assertLogs('tg_triage.classification_log', level='WARNING'):
            result = pipeline.process(source)
        self.assertEqual((result.decision, result.reason), ('chat', 'R1'))
        with self.turn() as existing:
            log.init_store(self.store.connection)
            log.record_decision(self.store, self.snapshot, 'early:44', [message(44, 'PRIVATE-text')],
                                self.snapshot.prefilter_decision('F4'))
            self.assertIs(CURRENT.get(), existing)

    def test_failures_are_isolated_and_body_exceptions_propagate(self):
        expected = evaluate_batch(self.snapshot, object(), [message(42, '/question sao?')])
        before_usage = self.store.model_usage_since(0)
        real = self.store.connection
        class RaisingConnection:
            in_transaction = False
            def execute(self, *args):
                raise sqlite3.OperationalError('PRIVATE-error-text')
        with self.turn(), patch.object(self.store, 'connection', RaisingConnection()):
            with self.assertLogs('tg_triage.classification_log', level='WARNING') as captured:
                actual = evaluate_batch(self.snapshot, object(), [message(42, '/question sao?')])
            self.assertEqual(actual, expected)
            self.assertNotIn('PRIVATE', str(captured.output))
        real.execute('BEGIN')
        self.assertEqual(self.evaluate(object(), [message(42, '/question sao?')]), expected)
        self.assertTrue(real.in_transaction)
        self.assertEqual(len(self.store._classification_pending), 1)
        real.rollback()
        real.execute('DROP TABLE classification_runs')
        with self.assertLogs('tg_triage.classification_log', level='WARNING'):
            self.assertEqual(self.evaluate(object(), [message(42, '/question sao?')]), expected)
        self.assertEqual(self.store.model_usage_since(0), before_usage)
        log.init_store(real)
        with self.turn(), patch('tg_triage.rules_intake._evaluate_batch', side_effect=RuntimeError('body')):
            with self.assertRaisesRegex(RuntimeError, 'body'):
                evaluate_batch(self.snapshot, object(), [message(42)])
        # A failing classification INSERT must not affect real provider accounting.
        real.execute('DROP TABLE classification_runs')
        with self.assertLogs('tg_triage.classification_log', level='WARNING'):
            self.assertEqual(self.evaluate(self.hosted())[0][1].state, 'READY')
        self.assertEqual(len(self.store.model_usage_since(0)), 1)

    def test_defensive_implicit_transaction_preserves_caller_commit_and_rollback(self):
        expected = evaluate_batch(self.snapshot, object(), [message(42, '/question sao?')])
        for finish in ('commit', 'rollback'):
            with self.subTest(finish=finish):
                store = Store(':memory:', clock=self.clock)
                self.addCleanup(store.close)
                pending = RulesPendingStore(store)
                pending.create('test', message().chat_id, 42)
                pending.reserve_attempt('test', 1)
                store.enqueue(message(42, 'PRIVATE-source'))
                before_usage = store.model_usage_since(0)
                store.connection.execute("UPDATE messages SET sender_name='PRIVATE-uncommitted'")
                self.assertTrue(store.connection.in_transaction)
                statements = []
                store.connection.set_trace_callback(statements.append)
                with self.turn(store), patch('tg_triage.rules_intake._evaluate_batch', return_value=expected):
                    actual = evaluate_batch(self.snapshot, object(), [message(42, '/question sao?')])
                self.assertIs(actual, expected)
                self.assertEqual(self.runs(store), [])
                self.assertEqual(len(store._classification_pending), 1)
                self.assertTrue(store.connection.in_transaction)
                self.assertEqual(store.get(message().chat_id, 42)['sender_name'], 'PRIVATE-uncommitted')
                self.assertFalse(log.flush(store))
                self.assertFalse(any(sql.strip().upper().startswith(('COMMIT', 'ROLLBACK')) for sql in statements))
                # The queued attempt number is frozen before later caller changes.
                store.connection.execute('UPDATE rules_topics SET attempts=2')
                getattr(store.connection, finish)()
                self.assertFalse(store.connection.in_transaction)
                self.assertEqual(store.get(message().chat_id, 42)['sender_name'] == 'PRIVATE-uncommitted', finish == 'commit')
                self.assertTrue(log.flush(store))
                self.assertTrue(log.flush(store))
                row, = self.runs(store)
                self.assertEqual((row['attempt_no'], row['final_state']), (1, 'CHAT'))
                self.assertNotIn('PRIVATE', json.dumps(row))
                self.assertEqual(len(store._classification_pending), 0)
                self.assertEqual(store.model_usage_since(0), before_usage)

    def test_next_safe_write_flushes_fifo_and_queues_are_per_store(self):
        other = Store(':memory:', clock=self.clock)
        self.addCleanup(other.close)
        self.store.enqueue(message(42))
        self.store.connection.execute("UPDATE messages SET sender_name='caller'")
        self.evaluate(object(), [message(42, '/question sao?')])
        self.store.connection.commit()
        with self.turn(other):
            evaluate_batch(self.snapshot, object(), [message(99, '/question sao?')])
        self.assertEqual(self.runs(), [])
        self.assertEqual([json.loads(r['message_ids']) for r in self.runs(other)], [[99]])
        self.evaluate(object(), [message(43, '/question sao?')])
        self.assertEqual([json.loads(r['message_ids']) for r in self.runs()], [[42], [43]])
        self.assertTrue(log.flush(self.store))
        self.assertEqual(len(self.runs()), 2)

    def test_uncommitted_caller_transaction_is_untouched_by_maintenance(self):
        settings = replace(config(), autonomous_enabled=True, decision_rules_enabled=True)
        statements = []
        self.store.connection.set_trace_callback(statements.append)
        Pipeline(settings, self.store, object(), rules_snapshot=self.snapshot)
        self.assertTrue(any(sql.startswith('UPDATE autonomous_confirmations') for sql in statements))
        self.assertFalse(self.store.connection.in_transaction)
        source = message(42)
        self.store.enqueue(source)
        self.store.connection.execute("UPDATE messages SET sender_name='uncommitted'")
        self.assertTrue(self.store.connection.in_transaction)
        statements.clear()
        with self.assertNoLogs('tg_triage.classification_log', level='WARNING'):
            log.maintain(self.store)
        self.assertTrue(self.store.connection.in_transaction)
        self.assertFalse(any(sql.strip().upper().startswith(('COMMIT', 'ROLLBACK')) for sql in statements))
        self.store.connection.rollback()
        self.assertEqual(source.sender_name, self.store.get(source.chat_id, source.message_id)['sender_name'])
        log.maintain(self.store)
        self.assertFalse(self.store.connection.in_transaction)

    def test_body_exception_in_caller_transaction_is_preserved_and_hold_is_queued(self):
        self.store.enqueue(message(42))
        self.store.connection.execute("UPDATE messages SET sender_name='PRIVATE-caller'")
        failure = RuntimeError('PRIVATE-body-error')
        with self.turn(), patch('tg_triage.rules_intake._evaluate_batch', side_effect=failure):
            with self.assertRaises(RuntimeError) as raised:
                evaluate_batch(self.snapshot, object(), [message(42)])
        self.assertIs(raised.exception, failure)
        self.assertTrue(self.store.connection.in_transaction)
        self.assertEqual(self.runs(), [])
        self.store.connection.rollback()
        self.assertTrue(log.flush(self.store))
        row, = self.runs()
        self.assertEqual((row['final_state'], row['error']), ('HOLD', 'rules_extraction_failed'))
        self.assertNotIn('PRIVATE', json.dumps(row))

    def test_maintenance_and_sweep_flush_even_within_hourly_retention_interval(self):
        settings = replace(config(), autonomous_enabled=True, decision_rules_enabled=True)
        pipeline = Pipeline(settings, self.store, object(), rules_snapshot=self.snapshot)
        self.store.connection.commit()
        self.store.enqueue(message(42))
        log.maintain(self.store)
        maintained_at = self.store._classification_maintained_at
        for index, maintain in enumerate((log.maintain, lambda store: sweep(pipeline)), 42):
            with self.subTest(index=index):
                self.store.connection.execute("UPDATE messages SET sender_name='caller'")
                self.evaluate(object(), [message(index, '/question sao?')])
                log.maintain(self.store)
                self.assertTrue(self.store.connection.in_transaction)
                self.assertEqual(len(self.store._classification_pending), 1)
                self.store.connection.commit()
                maintain(self.store)
                self.assertEqual(len(self.store._classification_pending), 0)
                self.assertEqual(self.store._classification_maintained_at, maintained_at)
        self.assertEqual([json.loads(r['message_ids']) for r in self.runs()], [[42], [43]])

    def test_pending_queue_is_bounded_drops_oldest_and_rate_limits_counted_warnings(self):
        self.store.enqueue(message(42))
        self.store.connection.execute("UPDATE messages SET sender_name='caller'")
        with self.assertLogs('tg_triage.classification_log', level='WARNING') as captured:
            with self.turn():
                for index in range(1, 505):
                    evaluate_batch(self.snapshot, object(), [message(index, '/question sao?')])
            self.assertEqual(len(self.store._classification_pending), 500)
            self.assertEqual(len(captured.records), 1)
            self.assertIn('dropped=1', captured.output[0])
            self.clock.now += timedelta(seconds=60)
            self.evaluate(object(), [message(505, '/question sao?')])
            self.assertEqual(len(captured.records), 2)
            self.assertIn('dropped=4', captured.output[1])
        self.assertTrue(self.store.connection.in_transaction)
        self.store.connection.rollback()
        self.assertTrue(log.flush(self.store))
        self.assertEqual([json.loads(r['message_ids'])[0] for r in self.runs()], list(range(6, 506)))

    def test_poison_constraint_row_is_dropped_and_later_rows_and_retention_continue(self):
        old = self.add_run(age=91)
        log.record_tool_call(self.store, old, 1, 'lookup')
        self.store.enqueue(message(42))
        self.store.connection.execute("UPDATE messages SET sender_name='caller'")
        self.evaluate(object(), [message(42, '/question sao?')])
        self.evaluate(object(), [message(43, '/question sao?')])
        self.evaluate(object(), [message(44, '/question sao?')])
        self.store.connection.commit()
        self.store.connection.execute('''CREATE TEMP TRIGGER fail_classification BEFORE INSERT ON classification_runs
            WHEN NEW.message_ids='[43]' BEGIN SELECT RAISE(ABORT,'PRIVATE-error-text'); END''')
        with self.assertLogs('tg_triage.classification_log', level='WARNING') as captured:
            log.maintain(self.store)
        self.assertEqual(len(captured.records), 1)
        self.assertIn('IntegrityError', captured.output[0])
        self.assertIn('dropped=1', captured.output[0])
        self.assertNotIn('PRIVATE', str(captured.output))
        self.assertEqual([json.loads(r['message_ids']) for r in self.runs()], [[42], [44]])
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM classification_tool_calls').fetchone()[0], 0)
        self.assertEqual(len(self.store._classification_pending), 0)
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(self.store.get(message().chat_id, 42)['sender_name'], 'caller')
        self.store.connection.execute('DROP TRIGGER fail_classification')
        self.assertTrue(log.flush(self.store))
        self.assertTrue(log.flush(self.store))
        self.assertEqual([json.loads(r['message_ids']) for r in self.runs()], [[42], [44]])

    def test_defensive_binding_poison_drops_only_bad_rows_with_rate_limited_counts(self):
        self.store.enqueue(message(42))
        self.store.connection.execute("UPDATE messages SET sender_name='caller'")
        with self.turn():
            for index in range(42, 45):
                with log.run(self.snapshot, [message(index)]) as attempt:
                    if index != 44:
                        attempt.values['cost'] = object()  # Real SQLite binding failure.
        self.store.connection.commit()
        with self.assertLogs('tg_triage.classification_log', level='WARNING') as captured:
            self.assertTrue(log.flush(self.store))
            self.assertEqual(len(captured.records), 1)
            self.assertIn('ProgrammingError', captured.output[0])
            self.assertIn('dropped=1', captured.output[0])
            self.clock.now += timedelta(seconds=60)
            log.flush(self.store)
        self.assertEqual(len(captured.records), 2)
        self.assertIn('dropped=1', captured.output[1])
        self.assertEqual([json.loads(r['message_ids']) for r in self.runs()], [[44]])

    def test_storage_flush_failure_keeps_queue_without_blocking_retention_or_backfill(self):
        self.add_run(age=91)
        ready = self.add_run(state='READY')
        self.store.enqueue(message(42))
        with self.store.connection:
            self.store.connection.execute("UPDATE messages SET issue_url='https://github.com/sandbox/triage/issues/42'")
        self.store.connection.execute("UPDATE messages SET sender_name='caller'")
        self.evaluate(object(), [message(43, '/question sao?')])
        self.store.connection.commit()
        def authorizer(action, table, *args):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == 'classification_runs' else sqlite3.SQLITE_OK
        self.store.connection.set_authorizer(authorizer)
        try:
            with self.assertLogs('tg_triage.classification_log', level='WARNING'):
                log.maintain(self.store)
            row, = self.runs()
            self.assertEqual(row['run_id'], ready)
            self.assertEqual(row['ticket_ref'], 'https://github.com/sandbox/triage/issues/42')
            self.assertEqual(len(self.store._classification_pending), 1)
        finally:
            self.store.connection.set_authorizer(None)
        self.assertTrue(log.flush(self.store))
        self.assertEqual(len(self.runs()), 2)

    def test_defensive_deferred_notice_is_rate_limited_and_reports_queue_length(self):
        self.store.enqueue(message(42))
        self.store.connection.execute("UPDATE messages SET sender_name='PRIVATE-caller'")
        with self.assertLogs('tg_triage.classification_log', level='INFO') as captured:
            self.evaluate(object(), [message(42, '/question sao?')])
            self.evaluate(object(), [message(43, '/question sao?')])
            self.assertEqual(len(captured.records), 1)
            self.assertIn('queued=1', captured.output[0])
            self.clock.now += timedelta(seconds=60)
            self.evaluate(object(), [message(44, '/question sao?')])
        self.assertEqual(len(captured.records), 2)
        self.assertIn('queued=3', captured.output[1])
        self.assertNotIn('PRIVATE', str(captured.output))
        self.assertTrue(self.store.connection.in_transaction)
        self.store.connection.rollback()
        log.flush(self.store)
        self.assertEqual(len(self.runs()), 3)

    def test_shutdown_flushes_safe_queue_and_cannot_raise_or_commit_caller_transaction(self):
        for finish in ('commit', 'rollback', 'open', 'flush_raises'):
            with self.subTest(finish=finish):
                path = Path(self.tmp.name) / (finish + '.sqlite3')
                settings = replace(config(), db_path=str(path), autonomous_enabled=True, decision_rules_enabled=True)
                stores = []
                async def transport(settings, pipeline):
                    store = pipeline.store
                    stores.append(store)
                    source = message(42, 'PRIVATE-source')
                    store.enqueue(source)
                    store.connection.execute("UPDATE messages SET sender_name='PRIVATE-caller'")
                    log.record_decision(store, self.snapshot, 'early:42', [source], self.snapshot.prefilter_decision('F4'))
                    self.assertEqual(self.runs(store), [])
                    if finish != 'open':
                        getattr(store.connection, 'rollback' if finish == 'flush_raises' else finish)()
                flush = log.flush
                # Windows creates a local self-pipe while constructing its loop.
                # Restore the network guard before running any application code.
                with patch('socket.socket.connect', self.socket_connect):
                    loop = asyncio.new_event_loop()
                with asyncio.Runner(loop_factory=lambda: loop) as runner, \
                     patch('tg_triage.__main__.asyncio.run', new=runner.run), \
                     patch('tg_triage.__main__.load_config', return_value=settings), \
                     patch('tg_triage.__main__._build_classifier', return_value=object()), \
                     patch('tg_triage.__main__.validate_data_directories'), \
                     patch('tg_triage.telegram_io.run', new=AsyncMock(side_effect=transport)), \
                     patch.object(log, 'flush', side_effect=RuntimeError('PRIVATE-error') if finish == 'flush_raises' else flush), \
                     self.assertNoLogs('tg_triage.classification_log', level='ERROR'):
                    self.assertEqual(main(['run']), 0)
                self.assertEqual(len(stores), 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    stores[0].connection.execute('SELECT 1')
                with contextlib.closing(sqlite3.connect(path)) as connection:
                    rows = connection.execute('SELECT final_state,model_calls,proposal_json FROM classification_runs').fetchall()
                    self.assertEqual(rows, [('CHAT', 0, None)] if finish in ('commit', 'rollback') else [])
                    name = connection.execute('SELECT sender_name FROM messages').fetchone()[0]
                    self.assertEqual(name == 'PRIVATE-caller', finish == 'commit')

    def test_defensive_pipeline_batch_preserves_run_after_uncommitted_dml_during_evaluation(self):
        store = self.store
        states = []
        class Provider:
            settings = SimpleNamespace(model='test')
            think = False
            supports_vision = False
            def _ask(self, body, parse, attempts):
                # Defensive fault injection, not the observed production cause.
                # Keep real rules extraction and accounting intact.
                store.connection.execute("UPDATE messages SET sender_name='PRIVATE-caller'")
                states.append(store.connection.in_transaction)
                return parse(payload((41, 42)))
        settings = replace(config(), autonomous_enabled=True, decision_rules_enabled=True)
        pipeline = Pipeline(settings, store, Provider(), rules_snapshot=self.snapshot)
        sources = [message(41, 'POS'), message(42, 'POS')]
        for source in sources:
            store.enqueue(source)
        original = log._Attempt.write
        def observe(attempt):
            result = original(attempt)
            states.append(store.connection.in_transaction)
            self.assertEqual(self.runs(), [])
            self.assertEqual(len(store._classification_pending), 1)
            return result
        with patch.object(log._Attempt, 'write', observe):
            result = _process_batch(pipeline, sources)
        self.assertEqual(result.decision, 'dry_run_issue')
        self.assertEqual(states, [True, True])
        self.assertFalse(store.connection.in_transaction)
        # Existing flow owns its later commits; periodic maintenance flushes next.
        sweep(pipeline)
        row, = self.runs()
        self.assertEqual((row['final_state'], row['attempt_no'], row['template']), ('READY', 1, 'bug.md'))
        self.assertEqual(json.loads(row['message_ids']), [41, 42])
        self.assertNotIn('PRIVATE', json.dumps(row))
        sweep(pipeline)
        self.assertEqual(len(self.runs()), 1)

    def test_pipeline_batch_ab_has_identical_decisions_github_and_state(self):
        observations = []
        for enabled in (False, True):
            store = Store(':memory:', clock=self.clock)
            self.addCleanup(store.close)
            class Provider:
                settings = SimpleNamespace(model='test')
                think = False
                supports_vision = False
                def _ask(self, body, parse, attempts):
                    return parse(payload((41, 42)))
            settings = replace(config(), mode='live', autonomous_enabled=True, decision_rules_enabled=True,
                               telegram=replace(config().telegram, ops_chat_id=-1009876543210))
            github = FakeGitHub()
            pipeline = Pipeline(settings, store, Provider(), github, rules_snapshot=self.snapshot,
                                notice_sender=lambda *args: 77)
            sources = [message(41, 'POS kẹt giấy'), message(42, 'POS bị lỗi')]
            for source in sources:
                store.enqueue(source)
            original = log.run
            @contextlib.contextmanager
            def disabled(*args, **kwargs):
                yield None
            with patch('tg_triage.classification_log.run', original if enabled else disabled):
                decision = _process_batch(pipeline, sources)
            state = {name: [tuple(r) for r in store.connection.execute('SELECT * FROM ' + name)]
                     for name in ('rules_topics', 'messages')}
            observations.append((decision, github.calls, state))
            if enabled:
                row, = self.runs(store)
                self.assertEqual((row['template'], row['attempt_no'], row['final_state']), ('bug.md', 1, 'READY'))
            else:
                self.assertEqual(self.runs(store), [])
        self.assertEqual(observations[0], observations[1])
        self.assertEqual(len(observations[1][1]), 1)

    def test_retention_deletes_children_first_and_is_hourly(self):
        old = self.add_run(age=91)
        self.add_run(age=89)
        self.add_run()
        log.record_tool_call(self.store, old, 1, 'lookup', '{"message_id":42}', 8, 4)
        log.maintain(self.store, 90)
        self.assertEqual(len(self.runs()), 2)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM classification_tool_calls').fetchone()[0], 0)
        self.add_run(age=91)
        log.maintain(self.store, 90)
        self.assertEqual(len(self.runs()), 3)
        self.clock.now += timedelta(hours=1)
        log.maintain(self.store, 90)
        self.assertEqual(len(self.runs()), 2)

    def test_custom_retention_from_sweep_keeps_model_usage_untouched(self):
        self.add_run(age=31)
        self.add_run(age=29)
        self.store.record_model_usage(self.clock().timestamp() - 89 * 86400, 'ollama', 'm', 'primary', 'ok')
        before = self.store.model_usage_since(0)
        settings = replace(config(), autonomous_enabled=True, decision_rules_enabled=True, assistant=AssistantConfig(30))
        pipeline = Pipeline(settings, self.store, object(), rules_snapshot=self.snapshot)
        self.store.connection.commit()
        sweep(pipeline)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.store.model_usage_since(0), before)

    def test_retention_batches_leave_no_orphan_tool_calls(self):
        source = message(42)
        with self.turn():
            for index in range(5002):
                with log.run(self.snapshot, [source]) as attempt:
                    if index < 5001:
                        # Vary ages so created_at order differs from run_id order.
                        attempt.values['created_at'] = self.clock().timestamp() - (91 + index % 3) * 86400
        rows = self.runs()
        keep = rows[-1]['run_id']
        for row in rows:
            log.record_tool_call(self.store, row['run_id'], 1, 'lookup')
        log.maintain(self.store)
        self.assertEqual([row['run_id'] for row in self.runs()], [keep])
        self.assertEqual([keep], [row[0] for row in self.store.connection.execute(
            "SELECT run_id FROM classification_run_sources"
        )])
        calls = self.store.connection.execute('SELECT run_id FROM classification_tool_calls').fetchall()
        self.assertEqual([row[0] for row in calls], [keep])
        orphans = self.store.connection.execute('''SELECT count(*) FROM classification_tool_calls c
            LEFT JOIN classification_runs r USING(run_id) WHERE r.run_id IS NULL''').fetchone()[0]
        self.assertEqual(orphans, 0)

    def test_topic_reference_fingerprints_are_scoped_to_topic(self):
        sources = [message(1), message(2)]
        for topic, phase, members in (
                ('topic-a', 'pending_merge', sources), ('topic-b', 'pending_merge', sources),
                ('topic-a', 'followup_confirm', list(reversed(sources))),
                ('topic-a', 'extract', sources), ('topic-b', 'extract', list(reversed(sources)))):
            with self.turn(topic=topic), log.run(self.snapshot, members, phase=phase):
                pass
        rows = self.runs()
        self.assertEqual(rows[0]['id_space'], 'topic_ref')
        self.assertNotEqual(rows[0]['batch_fingerprint'], rows[1]['batch_fingerprint'])
        self.assertEqual(rows[0]['batch_fingerprint'], rows[2]['batch_fingerprint'])
        self.assertNotEqual(rows[0]['batch_fingerprint'], rows[3]['batch_fingerprint'])
        self.assertEqual(rows[3]['batch_fingerprint'], rows[4]['batch_fingerprint'])

    def test_invalid_token_counts_do_not_abort_call_metadata(self):
        with self.turn(), log.run(self.snapshot, [message(42)]):
            for value in (None, -1, True, 1.5, '8'):
                log.note_call('deepseek', 'm', 'fallback', 'timeout',
                              {'prompt_tokens': value, 'completion_tokens': 2, 'cache_hit_tokens': None},
                              cost=0.125)
        row, = self.runs()
        self.assertEqual((row['tokens_in'], row['tokens_out'], row['cache_hit_tokens']), (0, 10, 0))
        self.assertEqual((row['model_calls'], row['cost'], row['fallback'], row['error']),
                         (5, 0.625, 1, 'timeout'))

    def test_sweep_without_assistant_config_uses_default_retention(self):
        self.add_run(age=91)
        self.add_run(age=89)
        settings = replace(config(), autonomous_enabled=True, decision_rules_enabled=True)
        pipeline = Pipeline(settings, self.store, object(), rules_snapshot=self.snapshot)
        pipeline.config = SimpleNamespace(**{name: value for name, value in vars(settings).items()
                                             if name != 'assistant'})
        self.store.connection.commit()
        sweep(pipeline)
        rows = self.runs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['created_at'], self.clock().timestamp() - 89 * 86400)

    def test_tool_args_project_structure_and_ids_without_private_text(self):
        run_id = self.add_run()
        marker = 'PRIVATE-tool-arguments'
        args = {'message_id': 42, 'chat_id': message().chat_id, 'source_ids': [1, 2],
                'text': marker, 'query': {'text': marker}, 'items': [marker],
                'limit': 10, marker: marker}
        for seq, value in enumerate((json.dumps(args), args, marker, json.dumps(marker)), 1):
            log.record_tool_call(self.store, run_id, seq, 'lookup', value)
        rows = [dict(row) for row in self.store.connection.execute('SELECT * FROM classification_tool_calls ORDER BY seq')]
        self.assertEqual(len(rows), 4)
        self.assertNotIn(marker, json.dumps(rows))
        for row in rows[:2]:
            projected = json.loads(row['args_json'])['arguments']
            self.assertEqual(projected['message_id']['id'], 42)
            self.assertEqual(projected['chat_id']['id'], message().chat_id)
            self.assertEqual(projected['source_ids']['ids'], [1, 2])
            self.assertEqual(projected['text'], {'type': 'string', 'length': len(marker)})
            self.assertEqual(projected['query'], {'type': 'object', 'length': 1})
            self.assertEqual(projected['items'], {'type': 'array', 'length': 1})
            self.assertNotIn('id', projected['limit'])
        for row in rows[2:]:
            self.assertEqual(json.loads(row['args_json']), {'type': 'string', 'length': len(marker)})

    def test_ticket_ref_backfill_is_recent_single_message_space_only(self):
        self.store.enqueue(message(42))
        with self.store.connection:
            self.store.connection.execute('UPDATE messages SET issue_url=?', ('https://github.com/sandbox/triage/issues/42',))
        ready = self.add_run(state='READY')
        for options in ({'age': 8}, {'id_space': 'topic_ref'}, {'state': 'CHAT'}, {'outcomes_json': '[{}]'}):
            self.add_run(**dict({'state': 'READY'}, **options))
        log.maintain(self.store)
        rows = self.runs()
        self.assertEqual(rows[0]['run_id'], ready)
        self.assertEqual(rows[0]['ticket_ref'], 'https://github.com/sandbox/triage/issues/42')
        self.assertTrue(all(row['ticket_ref'] is None for row in rows[1:]))
        # The maintenance write never updates source messages.
        self.assertEqual(self.store.get(message().chat_id, 42)['decision'], 'queued')

    def test_daily_summary_local_day_read_only_and_old_database(self):
        path = Path(self.tmp.name) / 'daily.sqlite3'
        store = Store(path)
        midnight = datetime.now(timezone.utc).replace(hour=18, minute=30, second=0, microsecond=0) - timedelta(days=2)
        ts = midnight.timestamp()
        store.record_model_usage(ts, 'deepseek', 'm', 'primary', 'ok', 10, 4, 3, cost=0.25)
        store.record_model_usage(ts + 1, 'deepseek', 'm', 'primary', 'timeout', 2, 0, 0, cost=0.1)
        with self.turn(store), log.run(self.snapshot, [message(42)]) as attempt:
            attempt.values.update(created_at=ts, model_calls=2, reasoning_tokens=7)
        store.close()
        before = path.read_bytes()
        reader = Store(path, read_only=True)
        try:
            report = log.daily_summary(reader.connection, 30, '+07:00')
            row, = report['model_usage']
            self.assertEqual((row['day'], row['calls'], row['ok'], row['error']),
                             ((midnight + timedelta(hours=7)).date().isoformat(), 2, 1, 1))
            self.assertEqual((row['prompt_tokens'], row['completion_tokens'], row['cache_hit_tokens']), (12, 4, 3))
            self.assertAlmostEqual(row['cost'], 0.35)
            run, = report['classification_runs']
            self.assertEqual((run['runs'], run['runs_with_model_calls'], run['reasoning_tokens']), (1, 1, 7))
            utc = log.daily_summary(reader.connection, 30, '+00:00')
            self.assertEqual(utc['model_usage'][0]['day'], midnight.date().isoformat())
        finally:
            reader.close()
        self.assertEqual(before, path.read_bytes())
        old = sqlite3.connect(path)
        old.execute('DROP TABLE classification_tool_calls')
        old.execute('DROP TABLE classification_runs')
        old.commit()
        old.close()
        before = path.read_bytes()
        reader = Store(path, read_only=True)
        self.assertEqual(log.daily_summary(reader.connection)['classification_runs'], [])
        reader.close()
        self.assertEqual(before, path.read_bytes())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['usage-daily', '--db', str(path)]), 0)
        self.assertEqual(before, path.read_bytes())

    def test_cli_json_and_config_defaults_bounds_and_example(self):
        path = Path(self.tmp.name) / 'cli.sqlite3'
        Store(path).close()
        before = path.read_bytes()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(['usage-daily', '--db', str(path)]), 0)
        self.assertEqual(json.loads(out.getvalue())['tz'], '+07:00')
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(load_with_test_secrets(EXAMPLE).assistant.retention_days, 90)
        text = EXAMPLE.read_text(encoding='utf-8')
        settings = Path(self.tmp.name) / 'listener.toml'
        settings.write_text(text.replace('[assistant]\n# Retention for classification runs and their tool-call metadata only.\nretention_days = 90', ''), encoding='utf-8')
        self.assertEqual(load_with_test_secrets(settings).assistant.retention_days, 90)
        for value in ('0', '-1', '"x"', 'true', '3651'):
            settings.write_text(text.replace('retention_days = 90', 'retention_days = ' + value), encoding='utf-8')
            with self.subTest(value=value), self.assertRaises(ConfigError):
                load_with_test_secrets(settings)
        settings.write_text(text.replace('retention_days = 90', 'retention_days = 30'), encoding='utf-8')
        self.assertEqual(load_with_test_secrets(settings).assistant.retention_days, 30)

    def test_startup_gate_1001_runs_supported_and_tool_roundtrip(self):
        path = Path(self.tmp.name) / 'gate.sqlite3'
        store = Store(path)
        # Use the public writer rather than duplicating the new schema/INSERT contract.
        with self.turn(store):
            for _ in range(1001):
                with log.run(self.snapshot, [message(42)]):
                    pass
        row = self.runs(store)[0]
        log.record_tool_call(store, row['run_id'], 1, 'lookup', '{"message_id":42}', 13, 9, 'timeout')
        call = tuple(store.connection.execute('''SELECT run_id, seq, tool, args_json, result_bytes, latency_ms, error
            FROM classification_tool_calls''').fetchone())
        self.assertEqual(call[:3], (row['run_id'], 1, 'lookup'))
        self.assertEqual(json.loads(call[3])['arguments']['message_id'], {'type': 'integer', 'id': 42})
        self.assertEqual(call[4:], (13, 9, 'timeout'))
        store.close()
        before = path.read_bytes()
        check_before_recovery(path)
        report = inspect(path)
        for name in ('classification_runs', 'classification_tool_calls'):
            self.assertEqual(report['inventory'][name]['schema'], 'SUPPORTED')
            self.assertTrue(report['inventory'][name]['validation_complete'])
            self.assertEqual(REGISTRY[name]['json_fields'], ())
            self.assertIsNone(REGISTRY[name]['status'])
            self.assertEqual(REGISTRY[name]['enums'], {})
        self.assertEqual(report['inventory']['classification_runs']['total'], 1001)
        self.assertEqual(before, path.read_bytes())
