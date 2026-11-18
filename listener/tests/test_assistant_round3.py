"""Terminal foreground reconciliation and link-health regressions for #2917."""
import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tg_triage import config as config_module, rules_intake
from tg_triage.__main__ import _load_decision_rules
from tg_triage.assistant import TurnResult
from tg_triage.assistant_extractor import AssistantExtractor, batch_fingerprint
from tg_triage.config import AssistantConfig, ConfigError, load_config
from tg_triage.decision_rules import RulesSnapshot
from tg_triage.models import Decision
from tg_triage.pipeline import Pipeline
from tg_triage.router_transport import RouterError, ToolCall
from tg_triage.store import Store
from tests.helpers import config, message, NOW, EXAMPLE, FAKE_HASH
from tests.test_assistant_integration import Clock, Provider, reply, proposal


class AssistantRound3Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.snapshot = _load_decision_rules()
        self.clock = Clock()
        self.serial = 0

    def make(self, enabled=True, provider=None, chat=None, snapshot=None):
        self.serial += 1
        path = self.root / f'{self.serial}.db'
        store = Store(path, clock=lambda: NOW); self.addCleanup(store.close)
        settings = replace(config(), db_path=str(path), autonomous_enabled=True, decision_rules_enabled=True,
            telegram=replace(config().telegram, ops_chat_id=-1009876543210),
            assistant=AssistantConfig(enabled=enabled, mode='live', router_key='test' if enabled else None))
        github = Mock()
        github.lifecycle_snapshot.return_value = dict(number=42, repo=settings.gh_repo,
            state='open', observed_at=NOW.isoformat())
        pipeline = Pipeline(settings, store, provider or Provider(), github,
            rules_snapshot=snapshot or self.snapshot, timer=lambda: 0,
            assistant_pack=SimpleNamespace(text='Loaded pack'))
        runtime = None
        if enabled:
            runtime = AssistantExtractor(settings, store, 'Loaded pack', chat=chat,
                clock=self.clock, wall_clock=self.clock, ops_notice=Mock())
            pipeline.assistant_runtime = store.assistant_runtime = runtime
        return pipeline, runtime

    def test_done_foreground_partition_rejection_escalates_like_disabled(self):
        class PartitionProvider(Provider):
            def _ask(self, body, parse, attempts=1):
                self.calls += 1
                ids = json.loads(body['messages'][1]['content'])['source_ids']
                return parse(json.dumps({'groups': [proposal([mid])['groups'][0] for mid in ids]}))
        summaries = []
        for enabled in (False, True):
            provider = PartitionProvider()
            data = {'groups': [proposal([mid])['groups'][0] for mid in (41, 42)]}
            pipeline, runtime = self.make(enabled, provider, Mock(return_value=reply(data)))
            sources = [message(41, text='POS kẹt giấy'), message(42, text='POS kẹt giấy', reply_to_id=41)]
            for source in sources: pipeline.store.enqueue(source)
            topic = 'batch:' + str(sources[0].chat_id) + ':41'
            attempts = []
            with patch('tg_triage.rules_intake._send_ops', return_value=True) as notice, patch(
                    'tg_triage.rules_intake._escalate', wraps=rules_intake._escalate) as escalate:
                result = rules_intake._process_batch(pipeline, sources)
                self.assertEqual(result.reason, 'rules_reply_partition_conflict')
                for index in range(5):
                    if index: pipeline.close_due_cases()
                    row = pipeline.store.connection.execute('SELECT attempts,status FROM rules_topics WHERE topic_id=?', (topic,)).fetchone()
                    attempts.append(row['attempts'])
                self.assertEqual(row['status'], 'ESCALATED')
                self.assertEqual(escalate.call_count, 1)
                self.assertEqual(notice.call_args.args[3], 'escalation')
            self.assertEqual(attempts, [1, 2, 3, 4, 4])
            if enabled:
                self.assertEqual(runtime.attempts.status(batch_fingerprint(sources)), 'DONE')
                self.assertEqual(runtime.chat.call_count, 1)
                self.assertEqual(provider.calls, 3)  # DONE reuses the existing extractor.
            summaries.append((attempts, row['status']))
        self.assertEqual(*summaries)

    def test_escalated_source_reply_refusal_spends_fourth_attempt_like_disabled(self):
        directory = Path(__file__).resolve().parents[1]
        core = RulesSnapshot(directory / 'rules', directory / 'templates' / 'github-ticket')
        class FailingProvider(Provider):
            def _ask(self, body, parse, attempts=1):
                self.calls += 1
                return None
        summaries = []
        for enabled in (False, True):
            provider = FailingProvider()
            pipeline, runtime = self.make(enabled, provider, Mock(return_value=reply('bad facts')), core)
            target = message(40, text='original ticket')
            source = message(42, text='POS kẹt giấy', reply_to_id=40)
            pipeline.store.enqueue(target)
            pipeline.store.finish(target, Decision('issue', 'rule', issue_number=42,
                issue_url='https://github.com/' + pipeline.config.gh_repo + '/issues/42'))
            pipeline.store.enqueue(source)
            topic = str(source.chat_id) + ':42'
            attempts = []
            with patch('tg_triage.rules_intake._send_ops', return_value=True) as notice, patch(
                    'tg_triage.rules_intake._escalate', wraps=rules_intake._escalate) as escalate:
                rules_intake._process_source_reply(pipeline, source)
                for index in range(5):
                    if index:
                        self.clock.advance(120 if index == 1 else 600)
                        pipeline.close_due_cases()
                    row = pipeline.store.connection.execute('SELECT attempts,status FROM rules_topics WHERE topic_id=?', (topic,)).fetchone()
                    attempts.append(row['attempts'])
                self.assertEqual(row['status'], 'ESCALATED')
                self.assertEqual(escalate.call_count, 1)
                self.assertEqual(notice.call_args.args[3], 'escalation')
            self.assertEqual(attempts, [1, 2, 3, 4, 4])
            if enabled: self.assertEqual(runtime.chat.call_count, 9)
            else: self.assertEqual(provider.calls, 4)
            summaries.append((attempts, row['status']))
        self.assertEqual(*summaries)

    def test_slow_tool_loop_spends_attempt_without_poisoning_breakers(self):
        counter = [0]
        def chat(model, messages, **kwargs):
            counter[0] += 1
            return reply(calls=[ToolCall(str(counter[0]), 'get_rule', json.dumps({'rule_id':'R' + str(counter[0])}))])
        pipeline, runtime = self.make(chat=chat)
        source = message(42, text='POS kẹt giấy', has_image=True)
        def slow_rule(rule_id):
            self.clock.advance(20)
            return {'rule': rule_id}
        with patch('tg_triage.assistant_local.LocalReader.get_rule', side_effect=slow_rule), patch.object(
                runtime, '_deepseek', return_value=Mock(side_effect=AssertionError('no image fallback'))):
            result = pipeline.process(source)
        self.assertEqual(result.reason, 'rules_hold')
        self.assertEqual(counter[0], 8)
        self.assertEqual(runtime.attempts.status(batch_fingerprint([source])), 'RETRY')
        self.assertEqual(pipeline.store.connection.execute('SELECT attempts FROM rules_topics').fetchone()[0], 1)
        self.assertTrue(all(b.state == 'closed' and not b._failures for b in runtime.breakers.values()))
        runtime.ops_notice.assert_not_called()

    def test_connection_refused_and_upstream_429_remain_infra_without_attempts(self):
        for failure in (ConnectionRefusedError(), RouterError('rate_limited')):
            with self.subTest(kind=type(failure).__name__):
                pipeline, runtime = self.make(chat=Mock(side_effect=failure))
                source = message(42, text='POS kẹt giấy')
                pipeline.process(source)
                self.assertEqual(runtime.chat.call_count, 2)
                self.assertEqual(runtime.attempts.status(batch_fingerprint([source])), 'WAIT_INFRA')
                self.assertEqual(pipeline.store.connection.execute('SELECT attempts FROM rules_topics').fetchone()[0], 0)
                self.assertTrue(all(b.state == 'closed' for b in runtime.breakers.values()))
                for _ in range(2):
                    self.clock.advance(20); pipeline.close_due_cases()
                self.assertEqual(runtime.chat.call_count, 6)
                self.assertEqual(runtime.attempts.status(batch_fingerprint([source])), 'WAIT_INFRA')
                self.assertEqual(pipeline.store.connection.execute('SELECT attempts FROM rules_topics').fetchone()[0], 0)
                self.assertEqual(runtime.breakers['gemini'].state, 'open')
                self.assertEqual(runtime.breakers['gpt'].state, 'open')
                runtime.ops_notice.assert_not_called()

    def test_late_deepseek_reply_spends_attempt_without_health_failure(self):
        from tg_triage.config import ModelConfig
        pipeline, runtime = self.make(chat=Mock(side_effect=ConnectionRefusedError()))
        runtime.config = replace(runtime.config, model=ModelConfig(provider='deepseek', name='deepseek-flash',
            think=False, cloud_consent=True, base_url='https://api.deepseek.com', fallback='none'))
        source = message(42, text='POS kẹt giấy')
        def late(*args, **kwargs):
            self.clock.advance(41)
            return proposal()
        with patch('tg_triage.assistant_extractor.read_key', return_value='test'), patch(
                'tg_triage.thinking_transport.complete', side_effect=late) as deepseek:
            result = pipeline.process(source)
        self.assertEqual(result.reason, 'rules_hold')
        self.assertEqual((runtime.chat.call_count, deepseek.call_count), (2, 1))
        self.assertEqual(runtime.attempts.status(batch_fingerprint([source])), 'RETRY')
        self.assertEqual(pipeline.store.connection.execute('SELECT attempts FROM rules_topics').fetchone()[0], 1)
        self.assertEqual(runtime.breakers['deepseek'].state, 'closed')
        self.assertFalse(runtime.breakers['deepseek']._failures)

    def test_split_failed_part_takes_precedence_over_infra(self):
        pipeline, runtime = self.make(chat=Mock(side_effect=AssertionError('fake parts')))
        source = message(42, text='POS kẹt giấy')
        def evaluate(snapshot, provider, sources, *, extract, **kwargs):
            for _ in range(2):
                extract(provider, snapshot, sources, {}, lambda raw: None,
                        images={}, documents=[], read_context=())
            return None
        results = [TurnResult('failed', error_class='schema'), TurnResult('infra', infra=True)]
        with patch.object(runtime, 'run', side_effect=results), patch.object(runtime, '_refund') as refund:
            self.assertIsNone(runtime.evaluate(evaluate, self.snapshot, pipeline.classifier, [source]))
        self.assertEqual(runtime.attempts.status(batch_fingerprint([source])), 'RETRY')
        self.assertEqual(runtime.attempts._batches[batch_fingerprint([source])].failures, 1)
        refund.assert_not_called()

    def test_disabled_ignores_bad_router_key_file_without_reading_it(self):
        if not EXAMPLE.is_file(): self.skipTest('config example absent in distribution')
        secret = 'do-not-print-router-secret'
        bad = self.root / 'router-key'; bad.write_text(secret, encoding='utf8'); os.chmod(bad, 0o644)
        settings = self.root / 'settings.toml'
        original = config_module._assistant_token
        for path in (str(bad), str(self.root / 'absent'), 'relative-unreadable-key'):
            settings.write_text(EXAMPLE.read_text(encoding='utf8') + '\nrouter_key_file = ' + json.dumps(path), encoding='utf8')
            def read_token(value):
                if value == path: raise AssertionError('disabled must not read router key')
                return original(value)
            with patch('tg_triage.config._assistant_token', side_effect=read_token):
                loaded = load_config(settings, {'TG_API_ID':'1','TG_API_HASH':FAKE_HASH})
            self.assertFalse(loaded.assistant.enabled)
            self.assertIsNone(loaded.assistant.router_key)
            public = json.dumps(loaded.redacted())
            self.assertNotIn(secret, public)
            self.assertNotIn(path, public)


if __name__ == '__main__': unittest.main()
