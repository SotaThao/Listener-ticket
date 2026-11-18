"""Offline production-seam checks; no router account or writable tools required."""
import json
import tempfile
import threading
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tg_triage.__main__ import _load_decision_rules
from tg_triage.assistant_extractor import AssistantExtractor
from tg_triage.config import AssistantConfig, ConfigError, load_config
from tg_triage.pipeline import Pipeline
from tg_triage.router_transport import RouterError, RouterReply, ToolCall
from tg_triage.rules_extraction import split_and_extract
from tg_triage.rules_intake import _evaluate_batch
from tg_triage.store import Store
from tests.helpers import config, message, EXAMPLE, FAKE_HASH, NOW


def proposal(ids=(42,)):
    return {'groups': [{'message_ids': list(ids), 'facts': {
        name: {'value': value, 'source_ids': [ids[0]]} for name, value in
        (('intent', 'bug'), ('object', 'POS'), ('observed', 'kẹt giấy'))}}]}


def reply(value=None, calls=()):
    return RouterReply(json.dumps(proposal() if value is None else value), tuple(calls),
                       'stop', 12, 4, 2, 3, 'fake', 1)


class Provider:
    settings = SimpleNamespace(model='fake')
    think = False
    request_budget_bytes = 24000
    supports_vision = False
    def __init__(self):
        self.calls = 0
    def _ask(self, body, parse, attempts=1):
        self.calls += 1
        ids = json.loads(body['messages'][1]['content'])['source_ids']
        return parse(json.dumps(proposal(ids)))


class Clock:
    def __init__(self): self.now = 0
    def __call__(self): return self.now
    def advance(self, value): self.now += value


class AssistantIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'triage.db'
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.snapshot = _load_decision_rules()
        self.clock = Clock()
        self.settings = replace(config(), db_path=str(self.path), autonomous_enabled=True,
            decision_rules_enabled=True, assistant=AssistantConfig(enabled=True, router_key='test'),
            telegram=replace(config().telegram, ops_chat_id=-1009876543210))

    def runtime(self, chat, mode='shadow', **changes):
        settings = replace(self.settings, assistant=replace(self.settings.assistant, mode=mode, **changes))
        return AssistantExtractor(settings, self.store, 'Loaded assistant pack', chat=chat,
                                  clock=self.clock, wall_clock=self.clock)

    def job(self, runtime, sources=None):
        sources = sources or [message(42, text='POS kẹt giấy')]
        bodies = []
        from tg_triage.rules_extraction import partition_and_extract, parse_grouped_facts
        provider = Provider()
        partition_and_extract(provider, self.snapshot, sources, body_out=bodies)
        ids = [m.message_id for m in sources]
        return dict(provider=provider, snapshot=self.snapshot, sources=sources, body=bodies[0],
                    parse=lambda raw: parse_grouped_facts(raw, self.snapshot, ids),
                    data=dict(images={}, documents=[], read_context=()))

    def test_default_config_ab_byte_identity_decisions_and_runs(self):
        """Actual unchanged production keys vs explicit assistant-off, fixed clocks."""
        results = []
        for explicit in (False, True):
            store = Store(Path(self.temp.name) / ('ab' + str(explicit) + '.db'), clock=lambda: NOW)
            try:
                settings = replace(self.settings, assistant=AssistantConfig() if not explicit else
                                   AssistantConfig(enabled=False, mode='live'))
                github, telegram = Mock(), Mock()
                pipeline = Pipeline(settings, store, Provider(), github, rules_snapshot=self.snapshot, timer=lambda: 0,
                                    notice_sender=telegram)
                with patch('tg_triage.router_transport.chat', side_effect=AssertionError('disabled')) as chat, patch('time.monotonic', return_value=0):
                    decision = pipeline.process(message(42, text='POS kẹt giấy'))
                    pipeline.close_due_cases()
                self.assertIsNone(pipeline.assistant_runtime)
                chat.assert_not_called()
                rows = [dict(r) for r in store.connection.execute('SELECT * FROM classification_runs')]
                results.append(json.dumps(dict(decision=asdict(decision), runs=rows), sort_keys=True))
            finally: store.close()
        self.assertEqual(*results)

    def test_shadow_committed_decision_zero_side_effects_all_other_tables(self):
        chat = Mock(side_effect=[reply(calls=[ToolCall('read', 'get_messages', '{"ids":[42]}')]), reply()])
        github, sender, editor, reactor, uploader = (Mock() for _ in range(5))
        writers = (github, sender, editor, reactor, uploader)
        pipeline = Pipeline(self.settings, self.store, Provider(), github, rules_snapshot=self.snapshot,
                            notice_sender=sender, notice_editor=editor, reactor=reactor, uploader=uploader,
                            assistant_pack=SimpleNamespace(text='Loaded pack'))
        runtime = pipeline.assistant_runtime
        runtime.chat = chat
        result = pipeline.process(message(42, text='POS kẹt giấy'))
        self.assertEqual((result.decision, pipeline.classifier.calls, chat.call_count), ('dry_run_issue', 1, 0))
        for writer in writers: writer.reset_mock()
        before = {r[0]: list(self.store.connection.execute('SELECT * FROM "' + r[0] + '"'))
                  for r in self.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if r[0] not in ('classification_runs', 'classification_tool_calls', 'sqlite_sequence')}
        runtime.drain()
        runtime._worker.join(timeout=5)
        self.assertFalse(runtime._worker.is_alive())
        for writer in writers: self.assertEqual(writer.mock_calls, [])
        for table, rows in before.items():
            self.assertEqual(list(self.store.connection.execute('SELECT * FROM "' + table + '"')), rows, table)
        row = self.store.connection.execute("SELECT * FROM classification_runs WHERE phase='assistant_shadow'").fetchone()
        self.assertEqual((row['final_state'], row['template'], row['model_calls']), ('READY', 'bug.md', 2))
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM classification_tool_calls').fetchone()[0], 1)

    def test_live_happy_path_through_pipeline_seam(self):
        pipeline = Pipeline(replace(self.settings, assistant=replace(self.settings.assistant, mode='live')),
                            self.store, Provider(), rules_snapshot=self.snapshot,
                            assistant_pack=SimpleNamespace(text='Loaded pack'))
        pipeline.assistant_runtime.chat = Mock(return_value=reply())
        result = pipeline.process(message(42, text='POS kẹt giấy'))
        self.assertEqual((result.decision, result.reason), ('dry_run_issue', 'R9'))
        self.assertEqual(pipeline.classifier.calls, 0)
        self.assertEqual(pipeline.assistant_runtime.chat.call_count, 1)

    def test_500_continuous_opens_each_link_and_infra_spends_no_attempts(self):
        chat = Mock(side_effect=RouterError('transient'))
        runtime = self.runtime(chat)
        job = self.job(runtime)
        for _ in range(3):
            result = runtime.run(job)
            self.assertEqual((result.outcome, result.model_calls), ('infra', 2))
            self.clock.advance(20)
        self.assertEqual(chat.call_count, 6)
        self.assertEqual(runtime.breakers['gemini'].state, 'open')
        self.assertEqual(runtime.breakers['gpt'].state, 'open')
        result = runtime.run(job)
        self.assertEqual((result.outcome, result.model_calls), ('infra', 0))
        self.assertEqual(runtime.attempts.status(next(iter(runtime.attempts._batches))), 'WAIT_INFRA')

    def test_401_one_call_per_link_and_shadow_auth_never_sends_telegram(self):
        chat = Mock(side_effect=RouterError('auth'))
        runtime = self.runtime(chat)
        runtime.ops_notice = Mock()
        with self.assertLogs('tg_triage.assistant_extractor', level='WARNING'):
            result = runtime.run(self.job(runtime))
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(result.outcome, 'failed')
        self.assertEqual(runtime.breakers['gemini'].state, 'open')
        runtime.ops_notice.assert_not_called()

    def test_hang_late_reply_rejected_and_bounded_timeouts(self):
        timeouts = []
        def chat(model, messages, **kw):
            timeouts.append(kw['timeout'])
            self.clock.advance(kw['timeout'] + 1)
            return reply()
        runtime = self.runtime(chat)
        result = runtime.run(self.job(runtime))
        self.assertEqual((result.outcome, result.model_calls), ('failed', 2))
        self.assertEqual([b.state for b in runtime.breakers.values()], ['closed'] * 3)
        self.assertEqual(timeouts, [60, 60])

    def test_invalid_json_twice_then_gpt_one_repair(self):
        chat = Mock(side_effect=[reply('bad'), reply('bad'), reply()])
        runtime = self.runtime(chat)
        result = runtime.run(self.job(runtime))
        self.assertEqual((result.outcome, result.model_calls, result.link), ('ok', 3, 'gpt'))
        self.assertEqual(chat.call_count, 3)

    def test_duplicate_tool_call_forces_proposal_single_toolbox(self):
        call = ToolCall('a', 'get_rule', '{"rule_id":"R9"}')
        chat = Mock(side_effect=[reply(calls=[call]), reply(calls=[call]), reply()])
        runtime = self.runtime(chat)
        result = runtime.run(self.job(runtime))
        self.assertEqual((result.outcome, result.model_calls, result.tool_calls), ('ok', 3, 1))
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM classification_tool_calls').fetchone()[0], 1)
        self.assertEqual(chat.call_args.kwargs['tools'], ())

    def test_daily_cap_zero_off_and_enabled_limits_actual_calls(self):
        chat = Mock(return_value=reply())
        runtime = self.runtime(chat, daily_cap=1)
        self.assertEqual(runtime.run(self.job(runtime)).model_calls, 1)
        other = self.job(runtime, [message(43, text='POS kẹt giấy')])
        result = runtime.run(other)
        self.assertEqual((result.outcome, result.model_calls), ('infra', 0))
        self.assertEqual(chat.call_count, 1)
        status, due = runtime.waiting(other['sources'])
        self.assertEqual((status, due), ('WAIT_INFRA', False))
        self.clock.advance(20)
        self.assertFalse(runtime.waiting(other['sources'])[1])

    def test_daily_cap_does_not_strand_half_open_probe(self):
        def chat(model, messages, **kwargs):
            packet = json.loads(messages[1]['content'].split('UNTRUSTED BATCH DATA:\n')[1])
            return reply(proposal([r['message_id'] for r in packet['sources']]))
        runtime = self.runtime(Mock(side_effect=chat), daily_cap=1)
        runtime.run(self.job(runtime))
        for b in runtime.breakers.values(): b.record_failure('auth')
        self.clock.advance(3600)
        job = self.job(runtime, [message(43, text='POS kẹt giấy')])
        self.assertEqual(runtime.run(job).model_calls, 0)
        self.clock.advance(61200 - self.clock())
        result = runtime.run(job)
        self.assertEqual((result.outcome, result.model_calls), ('ok', 1))
        self.assertEqual(runtime.chat.call_count, 2)

    def test_50_queued_batches_open_then_closed_drain_one_per_20_seconds(self):
        chat = Mock(side_effect=lambda model, messages, **kw: reply(proposal([
            json.loads(messages[1]['content'].split('UNTRUSTED BATCH DATA:\n')[1])['sources'][0]['message_id']])))
        runtime = self.runtime(chat)
        for n in range(1, 51):
            self.assertTrue(runtime.enqueue(self.job(runtime, [message(n, text='POS kẹt giấy')]), {}))
        for breaker in runtime.breakers.values(): breaker.record_failure('auth')
        runtime.drain(background=False)
        self.assertEqual(chat.call_count, 0)
        for breaker in runtime.breakers.values(): breaker.record_success()
        runtime.drain(background=False)
        runtime.drain(background=False)
        self.assertEqual(chat.call_count, 1)
        for n in range(2, 51):
            self.clock.advance(20)
            runtime.drain(background=False)
            self.assertEqual(chat.call_count, n)
        self.assertEqual(len(runtime.queue), 0)

    def test_live_50_infra_batches_recovery_also_drains_one_per_20_seconds(self):
        def chat(model, messages, **kwargs):
            packet = json.loads(messages[1]['content'].split('UNTRUSTED BATCH DATA:\n')[1])
            return reply(proposal([r['message_id'] for r in packet['sources']]))
        runtime = self.runtime(Mock(side_effect=chat), mode='live')
        batches = [[message(n, text='POS kẹt giấy')] for n in range(1, 51)]
        for breaker in runtime.breakers.values(): breaker.record_failure('auth')
        for sources in batches:
            self.assertIsNone(runtime.evaluate(_evaluate_batch, self.snapshot, Provider(), sources))
        self.assertEqual(runtime.chat.call_count, 0)
        self.assertEqual(len(runtime.queue), 50)
        for breaker in runtime.breakers.values(): breaker.record_success()
        self.clock.advance(20)
        for index, sources in enumerate(batches):
            self.assertTrue(runtime.waiting(sources)[1])
            self.assertIsNotNone(runtime.evaluate(_evaluate_batch, self.snapshot, Provider(), sources))
            self.assertEqual(runtime.chat.call_count, index + 1)
            if index < 49:
                self.assertFalse(runtime.waiting(batches[index + 1])[1])
                self.clock.advance(20)
        self.assertEqual(len(runtime.queue), 0)

    def test_live_new_arrival_waits_durably_and_cannot_burst_recovery(self):
        pipeline = Pipeline(self.settings, self.store, Provider(), rules_snapshot=self.snapshot)
        runtime = self.runtime(Mock(side_effect=RouterError('transient')), mode='live')
        pipeline.assistant_runtime = self.store.assistant_runtime = runtime
        pipeline.process(message(42, text='POS kẹt giấy'))
        pipeline.process(message(43, text='POS kẹt giấy'))
        self.assertEqual(runtime.chat.call_count, 2)
        rows = self.store.connection.execute('SELECT phase,attempts FROM rules_topics').fetchall()
        self.assertEqual([tuple(r) for r in rows], [('HOLD', 0), ('HOLD', 0)])
        def chat(model, messages, **kwargs):
            packet = json.loads(messages[1]['content'].split('UNTRUSTED BATCH DATA:\n')[1])
            return reply(proposal([r['message_id'] for r in packet['sources']]))
        runtime.chat.side_effect = chat
        self.clock.advance(20)
        pipeline.close_due_cases()
        self.assertEqual(runtime.chat.call_count, 3)
        self.clock.advance(20)
        pipeline.close_due_cases()
        self.assertEqual(runtime.chat.call_count, 4)

    def test_image_failure_holds_without_deepseek(self):
        import base64
        from tests.helpers import JPG
        runtime = self.runtime(Mock(side_effect=RouterError('transient')))
        job = self.job(runtime)
        job['sources'] = [replace(job['sources'][0], has_image=True)]
        packet = json.loads(job['body']['messages'][1]['content'])
        packet['images_order'] = [42]
        packet['sources'][0]['has_image'] = True
        packet['sources'][0]['pixels_available'] = True
        job['body']['messages'][1]['content'] = json.dumps(packet)
        job['body']['messages'][1]['images'] = [base64.b64encode(JPG).decode('ascii')]
        with patch.object(runtime, '_deepseek', return_value=Mock(side_effect=AssertionError('no image fallback'))) as factory:
            result = runtime.run(job)
        self.assertEqual((result.outcome, result.model_calls), ('hold', 2))
        factory.return_value.assert_not_called()
        parts = runtime.chat.call_args.args[1][1]['content']
        self.assertTrue(next(p['image_url']['url'] for p in parts if p['type'] == 'image_url').startswith('data:image/jpeg;'))

    def test_same_topic_arrival_during_turn_no_parallel_attempt(self):
        entered, release = threading.Event(), threading.Event()
        def chat(*args, **kwargs):
            entered.set(); release.wait(3); return reply()
        runtime = self.runtime(chat)
        job = self.job(runtime)
        thread = threading.Thread(target=lambda: runtime.run(job))
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertIsNone(runtime.run(job))
            self.assertIsNone(runtime.run(self.job(runtime, [message(43, text='POS kẹt giấy')])) )
        finally:
            release.set(); thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.store.connection.execute('SELECT sum(model_calls) FROM classification_runs').fetchone()[0], 1)

    def test_rate_limit_queue_bounds_and_missing_key_no_network(self):
        chat = Mock(side_effect=RouterError('transient'))
        runtime = self.runtime(chat, router_key=None)
        result = runtime.run(self.job(runtime))
        self.assertEqual((result.outcome, result.model_calls), ('infra', 0))
        chat.assert_not_called()
        runtime = self.runtime(Mock(return_value=reply()), max_calls_per_minute=1, queue_max=1)
        self.assertTrue(runtime.enqueue(self.job(runtime), {}))
        self.assertFalse(runtime.enqueue(self.job(runtime, [message(43)]), {}))
        runtime.drain(background=False)
        result = runtime.run(self.job(runtime, [message(43)]))
        self.assertEqual((result.outcome, result.model_calls), ('infra', 0))

    def test_extract_hook_default_and_injected_shape(self):
        provider = Provider()
        sources = [message(42, text='POS kẹt giấy')]
        normal = split_and_extract(provider, self.snapshot, sources)
        fake = Mock(side_effect=lambda p, s, src, body, parse, **kw: parse(json.dumps(proposal())))
        injected = split_and_extract(provider, self.snapshot, sources, extract=fake)
        self.assertEqual(normal, injected)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(fake.call_count, 1)

    def test_redaction_reuses_cloud_primitive_preserving_long_source_ids(self):
        captured = []
        mid = 1234567890
        def chat(model, messages, **kw):
            captured.append(json.dumps(messages))
            return reply(proposal([mid]))
        runtime = self.runtime(chat)
        result = runtime.run(self.job(runtime, [message(mid, text='POS lỗi, gọi 0901234567')]))
        self.assertEqual(result.outcome, 'ok')
        self.assertIn(str(mid), captured[0])
        self.assertNotIn('0901234567', captured[0])
        self.assertIn('[phone]', captured[0])

    def test_deepseek_callback_one_request_24000_bytes_remaining_deadline(self):
        from tg_triage.config import ModelConfig
        from tg_triage.thinking import request_text_bytes
        import os
        key = Path(self.temp.name) / 'deepseek-key'
        key.write_text(json.dumps({'key': 'test', 'scope': 'deepseek|https://api.deepseek.com'}))
        os.chmod(key, 0o600)
        runtime = self.runtime(Mock(side_effect=RouterError('transient')))
        runtime.config = replace(runtime.config, model=ModelConfig(provider='deepseek', name='deepseek-flash',
            think=False, base_url='https://api.deepseek.com', key_file=str(key), cloud_consent=True, fallback='none'))
        sent = []
        def complete(provider, base, model, messages, schema, **kw):
            sent.append((messages, schema, kw))
            kw['usage_sink']({'prompt_tokens': 17, 'completion_tokens': 5})
            return proposal()
        with patch('tg_triage.thinking_transport.complete', side_effect=complete) as transport:
            result = runtime.run(self.job(runtime))
        self.assertEqual((result.outcome, result.model_calls, transport.call_count), ('ok', 3, 1))
        self.assertLessEqual(request_text_bytes(dict(messages=sent[0][0], format=sent[0][1])), 24000)
        self.assertEqual(sent[0][2]['timeout'], 40)
        self.assertNotIn('tools', sent[0][2])

    def test_deepseek_auth_uses_transport_code_and_opens_breaker(self):
        from agentfleet_common.ai_transport import ProviderError
        from tg_triage.config import ModelConfig
        runtime = self.runtime(Mock(side_effect=RouterError('transient')))
        runtime.config = replace(runtime.config, model=ModelConfig(provider='deepseek', cloud_consent=True))
        with patch('tg_triage.assistant_extractor.read_key', return_value='test'), patch(
                'tg_triage.thinking_transport.complete', side_effect=ProviderError('auth_failed')) as complete:
            with self.assertLogs('tg_triage.assistant_extractor', level='WARNING'):
                result = runtime.run(self.job(runtime))
        self.assertEqual((result.model_calls, complete.call_count), (3, 1))
        self.assertEqual(runtime.breakers['deepseek'].state, 'open')

    def test_terminal_shadow_fingerprint_is_removed_from_queue(self):
        from tg_triage.assistant_extractor import batch_fingerprint
        runtime = self.runtime(Mock(return_value=reply()))
        job = self.job(runtime)
        fp = batch_fingerprint(job['sources'])
        self.assertTrue(runtime.enqueue(job, {}))
        runtime.attempts.begin(fp)
        runtime.attempts.finish(fp, 'infra')
        self.clock.advance(86400)
        runtime.drain(background=False)
        self.assertEqual(runtime.attempts.status(fp), 'ESCALATED')
        self.assertEqual(len(runtime.queue), 0)
        self.assertNotIn(fp, runtime._jobs)

    def test_live_infra_refunds_existing_pending_attempt_and_backoff(self):
        pipeline = Pipeline(replace(self.settings, assistant=replace(self.settings.assistant, mode='live')),
                            self.store, Provider(), rules_snapshot=self.snapshot,
                            assistant_pack=SimpleNamespace(text='Loaded pack'))
        runtime = pipeline.assistant_runtime
        runtime.chat = Mock(side_effect=RouterError('transient'))
        result = pipeline.process(message(42, text='POS kẹt giấy'))
        self.assertEqual(result.reason, 'rules_hold')
        topic = self.store.connection.execute('SELECT attempts,attempt_reserved FROM rules_topics').fetchone()
        self.assertEqual(tuple(topic), (0, 0))
        pipeline.close_due_cases()
        self.assertEqual(runtime.chat.call_count, 2)

    def test_shadow_sweep_returns_while_model_waits(self):
        entered, release = threading.Event(), threading.Event()
        def chat(*a, **kw):
            entered.set(); release.wait(3); return reply()
        pipeline = Pipeline(self.settings, self.store, Provider(), rules_snapshot=self.snapshot,
                            assistant_pack=SimpleNamespace(text='Loaded pack'))
        pipeline.assistant_runtime.chat = chat
        pipeline.process(message(42, text='POS kẹt giấy'))
        started = time.monotonic()
        pipeline.close_due_cases()
        elapsed = time.monotonic() - started
        try:
            self.assertTrue(entered.wait(2))
            self.assertLess(elapsed, .5)
        finally:
            release.set()
            pipeline.assistant_runtime._worker.join(3)

    def test_uncommitted_shadow_job_cannot_start_before_publish(self):
        chat = Mock(return_value=reply())
        runtime = self.runtime(chat)
        self.assertTrue(runtime.enqueue(self.job(runtime), {}, ready=False))
        runtime.drain(background=False)
        chat.assert_not_called()
        runtime.publish()
        runtime.drain(background=False)
        self.assertEqual(chat.call_count, 1)

    def test_live_failed_json_backoffs_then_escalates_with_nine_calls(self):
        pipeline = Pipeline(self.settings, self.store, Provider(), rules_snapshot=self.snapshot)
        runtime = self.runtime(Mock(return_value=reply('bad')), mode='live')
        pipeline.assistant_runtime = self.store.assistant_runtime = runtime
        pipeline.process(message(42, text='POS kẹt giấy'))
        self.assertEqual(runtime.chat.call_count, 3)
        pipeline.close_due_cases()
        self.assertEqual(runtime.chat.call_count, 3)
        self.clock.advance(120)
        pipeline.close_due_cases()
        self.assertEqual(runtime.chat.call_count, 6)
        self.clock.advance(600)
        pipeline.close_due_cases()
        pipeline.close_due_cases()
        self.assertEqual(runtime.chat.call_count, 9)
        status = self.store.connection.execute('SELECT status FROM rules_topics').fetchone()[0]
        self.assertEqual(status, 'ESCALATED')

    def test_live_split_parts_share_one_batch_attempt_and_deadline(self):
        from tg_triage.assistant_extractor import batch_fingerprint
        def chat(model, messages, **kwargs):
            packet = json.loads(messages[1]['content'].split('UNTRUSTED BATCH DATA:\n')[1])
            return reply(proposal([r['message_id'] for r in packet['sources']]))
        runtime = self.runtime(Mock(side_effect=chat), mode='live')
        sources = [message(n, text='POS kẹt giấy ' + 'dữ liệu ' * 700) for n in range(1, 9)]
        result = runtime.evaluate(_evaluate_batch, self.snapshot, Provider(), sources)
        self.assertIsNotNone(result)
        self.assertEqual(sum(len(members) for members, _ in result), 8)
        self.assertGreater(runtime.chat.call_count, 1)
        self.assertLessEqual(runtime.chat.call_count, 4)
        self.assertEqual(runtime.attempts.status(batch_fingerprint(sources)), 'DONE')


class AssistantConfigIntegrationTests(unittest.TestCase):
    def load(self, extra):
        if not EXAMPLE.is_file(): self.skipTest('config example absent in distribution')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text(EXAMPLE.read_text(encoding='utf8') + '\n' + extra, encoding='utf8')
            return load_config(path, {'TG_API_ID': '1', 'TG_API_HASH': FAKE_HASH})

    def test_defaults_and_existing_fields_preserved_no_key_disclosure(self):
        settings = self.load('')
        self.assertFalse(settings.assistant.enabled)
        public = settings.redacted()['assistant']
        self.assertEqual(public['retention_days'], 90)
        self.assertEqual(public['mode'], 'shadow')
        self.assertFalse(public['router_key_configured'])
        self.assertNotIn('router_key_file', public)
        self.assertNotIn('router_key', public)

    def test_missing_key_is_unavailable_not_startup_error(self):
        with tempfile.TemporaryDirectory() as directory:
            value = json.dumps(str(Path(directory) / 'absent-key'))
            self.assertIsNone(self.load('router_key_file = ' + value).assistant.router_key)

    def test_endpoint_models_bounds_and_types(self):
        for extra in ('enabled = 1', 'mode = "typo"', 'enabled = true\nrouter_key_file = "relative"',
                      'router_base_url = "https://evil.invalid/v1"', 'max_calls_per_minute = 21',
                      'queue_max = 201', 'turn_deadline_s = 181', 'daily_cap = -1',
                      'models = ["oc/test", "cx/test"]', 'models = ["mmf/test", "cx/test"]',
                      'models = ["ag/claude-test", "cx/test"]'):
            with self.subTest(extra=extra), self.assertRaises(ConfigError): self.load(extra)


if __name__ == '__main__': unittest.main()
