"""Round-two regressions for the shared runtime boundary; all I/O is fake."""
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tg_triage import classification_log
from tg_triage.__main__ import _load_decision_rules
from tg_triage.assistant_extractor import AssistantExtractor, batch_fingerprint, SHUTDOWN_JOIN_S
from tg_triage.config import AssistantConfig, ConfigError, load_config
from tg_triage.pipeline import Pipeline
from tg_triage.router_transport import RouterError
from tg_triage.rules_intake import _evaluate_batch, evaluate_batch
from tg_triage.store import Store
from tg_triage.thinking import decision_turn
from tests.helpers import config, message, EXAMPLE, FAKE_HASH
from tests.test_assistant_integration import Clock, Provider, reply, proposal


class AssistantReviewFixTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root / 'review.db'); self.addCleanup(self.store.close)
        self.snapshot = _load_decision_rules()
        self.clock = Clock()
        self.settings = replace(config(), db_path=str(self.root / 'review.db'),
            autonomous_enabled=True, decision_rules_enabled=True,
            telegram=replace(config().telegram, ops_chat_id=-1009876543210),
            assistant=AssistantConfig(enabled=True, router_key='test'))

    def pipeline(self, mode, chat):
        settings = replace(self.settings, assistant=replace(self.settings.assistant, mode=mode))
        pipeline = Pipeline(settings, self.store, Provider(), Mock(), rules_snapshot=self.snapshot,
                            notice_sender=Mock(), notice_editor=Mock(), reactor=Mock(),
                            assistant_pack=SimpleNamespace(text='Loaded pack'))
        runtime = AssistantExtractor(settings, self.store, 'Loaded pack', chat=chat,
                                     clock=self.clock, wall_clock=self.clock, ops_notice=Mock())
        pipeline.assistant_runtime = self.store.assistant_runtime = runtime
        return pipeline, runtime

    def test_live_image_infra_holds_without_attempts_escalation_or_deepseek(self):
        for kind in ('transient', 'invalid_endpoint'):
            with self.subTest(kind=kind):
                source = message(42 if kind == 'transient' else 43, text='POS kẹt giấy', has_image=True)
                chat = Mock(side_effect=RouterError(kind))
                pipeline, runtime = self.pipeline('live', chat)
                fallback = Mock(side_effect=AssertionError('images must not reach DeepSeek'))
                with patch.object(runtime, '_deepseek', return_value=fallback), patch(
                        'tg_triage.rules_intake._escalate', side_effect=AssertionError('infra escalation')):
                    result = pipeline.process(source)
                    self.assertEqual(result.reason, 'rules_hold')
                    for delay in (20, 120, 600):
                        self.clock.advance(delay)
                        pipeline.close_due_cases()
                self.assertEqual(runtime.attempts.status(batch_fingerprint([source])), 'WAIT_INFRA')
                self.assertEqual(runtime.attempts._batches[batch_fingerprint([source])].failures, 0)
                row = self.store.connection.execute('SELECT attempts FROM rules_topics WHERE source_chat_id=? AND topic_id=?',
                    (source.chat_id, 'batch:' + str(source.chat_id) + ':' + str(source.message_id))).fetchone()
                self.assertEqual(row[0], 0)
                runtime.ops_notice.assert_not_called()
                fallback.assert_not_called()
                self.assertEqual(chat.call_count, 6 if kind == 'transient' else 8)
                pipeline.rules_pending.close('batch:' + str(source.chat_id) + ':' + str(source.message_id), 'CLOSED')

    def test_shadow_image_infra_real_worker_keeps_wait_infra_and_zero_attempts(self):
        source = message(42, text='POS kẹt giấy', has_image=True)
        pipeline, runtime = self.pipeline('shadow', Mock(side_effect=ConnectionError()))
        fallback = Mock(side_effect=AssertionError('image fallback'))
        with patch.object(runtime, '_deepseek', return_value=fallback), patch(
                'tg_triage.rules_intake._escalate', side_effect=AssertionError('infra escalation')):
            pipeline.process(source)
            runtime.drain(); runtime._worker.join(timeout=5)
            self.assertFalse(runtime._worker.is_alive())
        self.assertEqual(runtime.attempts.status(batch_fingerprint([source])), 'WAIT_INFRA')
        self.assertEqual(runtime.attempts._batches[batch_fingerprint([source])].failures, 0)
        self.assertTrue(all(r[0] == 0 for r in self.store.connection.execute('SELECT attempts FROM rules_topics')))
        self.assertEqual(runtime.chat.call_count, 2)
        runtime.ops_notice.assert_not_called(); fallback.assert_not_called()

    def test_image_model_failure_still_spends_attempt_in_both_modes(self):
        for mode, kind, mid in (('live', 'schema', 42), ('shadow', 'schema', 43),
                                ('live', 'truncated', 44), ('shadow', 'truncated', 45)):
            with self.subTest(mode=mode, kind=kind):
                source = message(mid, text='POS kẹt giấy', has_image=True, chat_id=-1001234567800-mid)
                chat = Mock(side_effect=RouterError('truncated')) if kind == 'truncated' else Mock(return_value=reply('invalid facts'))
                pipeline, runtime = self.pipeline(mode, chat)
                fallback = Mock(side_effect=AssertionError('image fallback'))
                with patch.object(runtime, '_deepseek', return_value=fallback):
                    pipeline.process(source)
                    if mode == 'shadow':
                        runtime.drain(); runtime._worker.join(timeout=5)
                self.assertEqual(runtime.attempts.status(batch_fingerprint([source])), 'RETRY')
                self.assertEqual(runtime.attempts._batches[batch_fingerprint([source])].failures, 1)
                if mode == 'live':
                    self.assertEqual(self.store.connection.execute('SELECT attempts FROM rules_topics WHERE topic_id=?',
                        ('batch:' + str(source.chat_id) + ':' + str(mid),)).fetchone()[0], 1)
                self.assertEqual(runtime.chat.call_count, 3)
                fallback.assert_not_called()

    def test_image_all_breakers_open_marks_infra_without_model_or_deepseek(self):
        from tg_triage.rules_extraction import partition_and_extract, parse_grouped_facts
        for mode in ('shadow', 'live'):
            with self.subTest(mode=mode):
                _, runtime = self.pipeline(mode, Mock(side_effect=AssertionError('open breaker')))
                for breaker in runtime.breakers.values(): breaker.record_failure('auth')
                sources = [message(42, text='POS kẹt giấy', has_image=True)]
                bodies = []
                partition_and_extract(Provider(), self.snapshot, sources, body_out=bodies)
                job = dict(sources=sources, provider=Provider(), snapshot=self.snapshot, body=bodies[0],
                    parse=lambda raw: parse_grouped_facts(raw, self.snapshot, [42]),
                    data=dict(images={}, documents=[], read_context=()))
                fallback = Mock(side_effect=AssertionError('image fallback'))
                with patch.object(runtime, '_deepseek', return_value=fallback):
                    result = runtime.run(job)
                self.assertEqual((result.outcome, result.infra, result.model_calls), ('hold', True, 0))
                self.assertEqual(runtime.attempts.status(batch_fingerprint(sources)), 'WAIT_INFRA')
                fallback.assert_not_called(); runtime.chat.assert_not_called()

    def test_topic_refs_colliding_with_real_ids_bypass_assistant_in_both_modes(self):
        for mid in (1, 2): self.store.enqueue(message(mid, text='UNRELATED REAL MESSAGE'))
        refs = [message(1, text='POS kẹt giấy'), message(2, text='bổ sung cùng chủ đề')]
        for mode in ('shadow', 'live'):
            with self.subTest(mode=mode):
                _, runtime = self.pipeline(mode, Mock(side_effect=AssertionError('no assistant for refs')))
                provider = Provider()
                current = SimpleNamespace(values=dict(id_space='topic_ref', batch_fingerprint='ref-namespace', topic_id='topic'))
                token = classification_log.CURRENT.set(current)
                try:
                    with patch('tg_triage.assistant_extractor.LocalReader', side_effect=AssertionError('no ref reader')):
                        result = runtime.evaluate(_evaluate_batch, self.snapshot, provider, refs, allow_mixed=True)
                finally:
                    classification_log.CURRENT.reset(token)
                self.assertIsNotNone(result)
                self.assertEqual(provider.calls, 1)
                runtime.chat.assert_not_called()
                self.assertEqual(len(runtime.queue), 0)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM classification_runs WHERE phase LIKE 'assistant_%'").fetchone()[0], 0)

    def test_evaluate_seam_paces_reserved_single_reply_and_ops_paths(self):
        def chat(model, messages, **kwargs):
            packet = json.loads(messages[1]['content'].split('UNTRUSTED BATCH DATA:\n')[1])
            return reply(proposal([r['message_id'] for r in packet['sources']]))
        pipeline, runtime = self.pipeline('live', Mock(side_effect=chat))
        sources = [message(mid, text='POS kẹt giấy') for mid in (41, 42, 43)]
        options = ({}, {'reply_target_for': lambda _: 'ticket_open'}, {'asked_counts': {}})
        for source in sources:
            runtime._queue_live(batch_fingerprint([source]), 'infra')
            self.store.enqueue(source)

        def invoke(source, kwargs):
            topic = 'review:' + str(source.message_id)
            pipeline.rules_pending.create(topic, source.chat_id, source.message_id)
            attempt = pipeline.rules_pending.reserve_attempt(topic, 3)
            self.assertIsNotNone(attempt)
            with decision_turn(pipeline, 'rules:' + topic, topic, source.message_id):
                result = evaluate_batch(self.snapshot, pipeline.classifier, [source], **kwargs)
            pipeline.rules_pending.finish_attempt(topic, attempt, success=result is not None)
            return result

        self.assertIsNotNone(invoke(sources[0], options[0]))
        self.assertIsNone(invoke(sources[1], options[1]))
        self.assertIsNone(invoke(sources[2], options[2]))
        self.assertEqual(runtime.chat.call_count, 1)
        self.assertTrue(all(r[0] == 0 for r in self.store.connection.execute('SELECT attempts FROM rules_topics')))
        self.clock.advance(20)
        self.assertIsNotNone(invoke(sources[1], options[1]))
        self.assertIsNone(invoke(sources[2], options[2]))
        self.assertEqual(runtime.chat.call_count, 2)
        self.clock.advance(20)
        self.assertIsNotNone(invoke(sources[2], options[2]))
        self.assertEqual(runtime.chat.call_count, 3)
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM classification_runs WHERE phase='assistant_live' AND error_class='rate_limited'").fetchone()[0], 0)

    def test_shutdown_join_is_bounded_and_late_worker_does_not_touch_closed_store(self):
        entered, release = threading.Event(), threading.Event()
        def chat(*args, **kwargs):
            entered.set(); release.wait(3); return reply()
        pipeline, runtime = self.pipeline('shadow', chat)
        pipeline.process(message(42, text='POS kẹt giấy'))
        runtime.drain()
        try:
            self.assertTrue(entered.wait(2))
            started = time.monotonic()
            with patch('tg_triage.assistant_extractor.SHUTDOWN_JOIN_S', .02), self.assertLogs(
                    'tg_triage.assistant_extractor', level='WARNING') as logs:
                runtime.close()
                self.assertLess(time.monotonic() - started, 1)
                self.store.close()
                release.set(); runtime._worker.join(timeout=3)
            self.assertEqual(len(logs.output), 1)
            self.assertIn('continuing shutdown', logs.output[0])
        finally:
            release.set(); runtime._worker.join(timeout=3)
        self.assertFalse(runtime._worker.is_alive())
        self.assertEqual(len(runtime.queue), 0)
        unit = Path(__file__).resolve().parents[2] / 'systemd' / 'tg-triage.service'
        if not unit.is_file(): self.skipTest('systemd unit absent in distribution')
        self.assertIn('TimeoutStopSec=150', unit.read_text(encoding='utf8'))
        self.assertEqual(SHUTDOWN_JOIN_S, 5)

    def test_shadow_missing_key_warns_once_without_exposing_path_or_key(self):
        settings = replace(self.settings, assistant=replace(self.settings.assistant, router_key=None,
            router_key_file=str(self.root / 'private-path')))
        with self.assertLogs('tg_triage.assistant_extractor', level='WARNING') as logs:
            runtime = AssistantExtractor(settings, self.store, 'Loaded pack')
            runtime.close()
        self.assertEqual(len(logs.output), 1)
        self.assertIn('router key is not configured', logs.output[0])
        self.assertNotIn('private-path', logs.output[0])

    def test_live_missing_key_config_fails_closed_shadow_stays_nonfatal(self):
        if not EXAMPLE.is_file(): self.skipTest('config example absent in distribution')
        path = self.root / 'settings.toml'
        missing = json.dumps(str(self.root / 'private-key-path'))
        text = EXAMPLE.read_text(encoding='utf8')
        for mode in ('shadow', 'live'):
            path.write_text(text + '\nenabled = true\nmode = "' + mode + '"\nrouter_key_file = ' + missing, encoding='utf8')
            if mode == 'shadow':
                self.assertIsNone(load_config(path, {'TG_API_ID':'1', 'TG_API_HASH':FAKE_HASH}).assistant.router_key)
            else:
                with self.assertRaisesRegex(ConfigError, '^assistant: live mode requires a configured router key$'):
                    load_config(path, {'TG_API_ID':'1', 'TG_API_HASH':FAKE_HASH})


if __name__ == '__main__': unittest.main()
