"""Live grant leases and router-only fence regressions; no external I/O."""
import json
from contextlib import closing
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tg_triage import assistant, rules_intake
from tg_triage.__main__ import _load_decision_rules
from tg_triage.assistant_extractor import AssistantExtractor, batch_fingerprint, LIVE_GRANT_LEASE_S
from tg_triage.assistant_loop import AttemptTracker
from tg_triage.config import AssistantConfig
from tg_triage.pipeline import Pipeline
from tg_triage.rules_extraction import parse_grouped_facts
from tg_triage.store import Store
from tests.helpers import config, message, NOW
from tests.test_assistant_integration import Clock
from tests.test_assistant_turn import Breaker, Chat, PROPOSAL, reply, Reader
from tg_triage.assistant_tools import ToolBox


class AssistantRound5Tests(unittest.TestCase):
    def runtime(self):
        clock = Clock()
        settings = SimpleNamespace(enabled=True, mode='live', router_key='test',
            max_calls_per_minute=20, daily_cap=0, queue_max=200)
        cfg = SimpleNamespace(assistant=settings, model=SimpleNamespace(provider='ollama', cloud_consent=False))
        return AssistantExtractor(cfg, None, '', chat=Mock(), clock=clock, wall_clock=clock), clock

    def test_unclaimed_ready_is_revalidated_for_every_departure_and_peek(self):
        for fate in ('escalated', 'done', 'unknown', 'topic_closed', 'missing_source', 'unread_document', 'never_returns'):
            with self.subTest(fate=fate):
                runtime, clock = self.runtime()
                runtime.attempts.begin('A'); runtime.attempts.finish('A', 'infra'); runtime._queue_live('A', 'infra')
                clock.advance(30)
                self.assertFalse(runtime.waiting([], fingerprint='B')[1])
                self.assertEqual(runtime._live_ready, 'A')
                if fate == 'escalated':
                    clock.advance(86400 - 30)  # A expires; B has 30s left to claim.
                elif fate == 'done':
                    runtime.attempts.begin('A'); runtime.attempts.finish('A', 'ok')
                    clock.advance(20)
                elif fate == 'unknown':
                    del runtime.attempts._batches['A']
                    clock.advance(20)
                else:
                    # Closed/missing/unread topics never re-enter the extraction seam.
                    clock.advance(LIVE_GRANT_LEASE_S)
                runtime.waiting([], fingerprint='B', admit=False)
                self.assertIsNone(runtime._live_ready)
                self.assertTrue(runtime.waiting([], fingerprint='B')[1])
                self.assertNotIn('B', runtime._live_waiting)
                if fate not in ('escalated', 'done', 'unknown'):
                    self.assertIn('A', runtime._live_waiting)  # Requeued, not lost.
                runtime.chat.assert_not_called()

    def test_never_admitted_wait_expires_without_failure_or_deadline_reset(self):
        for blocked in ('fifo', 'global_cooldown'):
            with self.subTest(blocked=blocked):
                runtime, clock = self.runtime()
                if blocked == 'fifo':
                    runtime._outage = True
                    for breaker in runtime.breakers.values(): breaker.record_failure('auth')
                    self.assertEqual(runtime.waiting([], fingerprint='B'), ('WAIT_INFRA', False))
                else:
                    runtime._blocked_until = 200000
                    self.assertEqual(runtime.waiting([], fingerprint='B', admit=False), ('WAIT_INFRA', False))
                    self.assertNotIn('B', runtime._live_waiting)
                first = runtime.attempts._batches['B'].first_infra
                for _ in range(50):
                    clock.advance(3600)
                    status, _ = runtime.waiting([], fingerprint='B', admit=False)
                self.assertEqual(status, 'ESCALATED')
                self.assertEqual(runtime.attempts._batches['B'].first_infra, first)
                self.assertEqual(runtime.attempts._batches['B'].failures, 0)
                runtime.chat.assert_not_called()

    def test_closed_ready_topic_releases_real_pipeline_queue_within_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'live.db'
            with closing(Store(path, clock=lambda: NOW)) as store:
                clock = Clock()
                settings = replace(config(), db_path=str(path), autonomous_enabled=True, decision_rules_enabled=True,
                    assistant=AssistantConfig(enabled=True, mode='live', router_key='test'))
                from tests.test_assistant_integration import Provider
                pipeline = Pipeline(settings, store, Provider(), Mock(), rules_snapshot=_load_decision_rules(),
                                    assistant_pack=SimpleNamespace(text='Loaded pack'))
                runtime = AssistantExtractor(settings, store, '', chat=Mock(side_effect=ConnectionRefusedError()), clock=clock, wall_clock=clock)
                pipeline.assistant_runtime = store.assistant_runtime = runtime
                a, b = message(42, text='POS paper jam', has_image=True), message(43, text='POS paper jam', has_image=True)
                pipeline.process(a)
                clock.advance(30); pipeline.process(b)
                afp, bfp = batch_fingerprint([a]), batch_fingerprint([b])
                self.assertEqual(runtime._live_ready, afp)
                pipeline.rules_pending.close(f'batch:{a.chat_id}:42', 'CLOSED')
                clock.advance(LIVE_GRANT_LEASE_S)
                self.assertTrue(runtime.waiting([b])[1])
                self.assertEqual(runtime.attempts.status(bfp), 'WAIT_INFRA')
                self.assertEqual(runtime.chat.call_count, 2)

    def test_queue_only_timeout_uses_existing_foreground_escalation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'live.db'
            with closing(Store(path, clock=lambda: NOW)) as store:
                settings = replace(config(), db_path=str(path), autonomous_enabled=True, decision_rules_enabled=True,
                    telegram=replace(config().telegram, ops_chat_id=-1009876543210),
                    assistant=AssistantConfig(enabled=True, mode='live', router_key='test'))
                from tests.test_assistant_integration import Provider
                pipeline = Pipeline(settings, store, Provider(), Mock(), rules_snapshot=_load_decision_rules(),
                                    assistant_pack=SimpleNamespace(text='Loaded pack'))
                runtime, clock = self.runtime()
                runtime.config, runtime.settings, runtime.store = settings, settings.assistant, store
                pipeline.assistant_runtime = store.assistant_runtime = runtime
                runtime._outage = True
                for breaker in runtime.breakers.values(): breaker.record_failure('auth')
                source = message(42, text='POS paper jam', has_image=True)
                pipeline.process(source)
                fp = batch_fingerprint([source]); topic = f'batch:{source.chat_id}:42'
                self.assertEqual(runtime.attempts.status(fp), 'WAIT_INFRA')
                self.assertEqual(store.connection.execute('SELECT attempts FROM rules_topics WHERE topic_id=?', (topic,)).fetchone()[0], 0)
                clock.advance(86400)
                with patch.object(rules_intake, '_send_ops', return_value=True) as notice, patch.object(
                        rules_intake, '_escalate', wraps=rules_intake._escalate) as escalate:
                    rules_intake._assistant_wait(pipeline, [source], topic)
                self.assertEqual(runtime.attempts.status(fp), 'ESCALATED')
                self.assertEqual(store.connection.execute('SELECT status FROM rules_topics WHERE topic_id=?', (topic,)).fetchone()[0], 'ESCALATED')
                escalate.assert_called_once(); notice.assert_called_once()
                runtime.chat.assert_not_called()

    def test_router_accepts_only_one_whole_fence_and_deepseek_is_unchanged(self):
        snapshot = assistant._snapshot()
        raw = json.dumps(PROPOSAL)
        for content in (raw, ' \n```json\n' + raw + '\n```\n ', '```\n' + raw + '\n```'):
            with self.subTest(content=content[:12]):
                chat = Chat(reply(content))
                result = self.turn(chat)
                self.assertEqual(result.outcome, 'ok'); self.assertEqual(result.model_calls, 1)
        rejected = ['Here is JSON: ' + raw, '```json\n' + raw + '\n```\n```json\n' + raw + '\n```',
                    '```json\n```\n' + raw + '\n```\n```', 'before\n```json\n' + raw + '\n```',
                    '```python\n' + raw + '\n```']
        for content in rejected:
            with self.subTest(content=content[:12]):
                result = self.turn(Chat(*[reply(content) for _ in range(3)]))
                self.assertEqual(result.outcome, 'failed'); self.assertEqual(result.model_calls, 3)
        fenced = '```json\n' + raw + '\n```'
        self.assertIsNone(parse_grouped_facts(fenced, snapshot, [1]))
        for value, expected in ((PROPOSAL, 'ok'), (fenced, 'failed')):
            fallback = Mock(return_value=value)
            result = self.turn(Chat(ConnectionRefusedError(), ConnectionRefusedError()), deepseek=fallback)
            self.assertEqual(result.outcome, expected); fallback.assert_called_once()

    def turn(self, chat, deepseek=None):
        return assistant.run_turn(sources_payload=[dict(message_id=1, text='POS', has_image=False)], images={},
            pack_text='pack', links=[assistant.Link('gemini', 'ag/fake', 75), assistant.Link('gpt', 'cx/fake', 65)],
            chat=chat, toolbox=ToolBox(Reader(), Reader()),
            breakers={n: Breaker() for n in ('gemini', 'gpt', 'deepseek')}, redact=lambda s:s,
            deepseek=deepseek, images_carry_data=False)

    def test_queue_wait_tracker_retains_failures_and_backoff(self):
        clock = Clock(); tracker = AttemptTracker(clock=clock)
        tracker.begin('B'); tracker.finish('B', 'failed')
        self.assertEqual(tracker.wait_infra('B'), 'WAIT_INFRA')
        self.assertFalse(tracker.due('B'))
        self.assertEqual(tracker._batches['B'].failures, 1)
        clock.advance(120); self.assertTrue(tracker.due('B'))
        clock.advance(86400); self.assertEqual(tracker.status('B'), 'ESCALATED')

    def test_schema_rejection_classifications_do_not_return_content(self):
        for text, expected in [('```json\n{}\n```', 'fenced'), ('private prose {}', 'prose'),
                               ('{private invalid', 'invalid_json'), ('{}', 'invalid_shape'), (None, 'invalid_json')]:
            self.assertEqual(assistant.schema_rejection_kind(text), expected)
