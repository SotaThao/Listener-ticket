"""2178: 24 000-byte cloud request budget, <=5 sources per call, split into <=4 parts, ops notice on overflow."""
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tg_triage.pacing import RELEASED_SOURCES
from tg_triage.pipeline import Pipeline
from tg_triage.rules_extraction import partition_and_extract, split_and_extract
from tg_triage.rules_intake import evaluate_batch
from tg_triage.store import Store
from tg_triage.thinking import (CLOUD_REQUEST_BUDGET_BYTES, CURRENT, LOCAL_REQUEST_BUDGET_BYTES, RULES_MAX_PARTS,
                                Turn, request_text_bytes)
from tests.helpers import config, message
from tests.test_rules_bootstrap import RulesBootstrapTests

VI = 'Máy in hóa đơn ở quầy số hai không in được, khách đang chờ, nhờ bên kỹ thuật xem giúp. '


def vi(chars):
    return (VI * (chars // len(VI) + 1))[:chars]


class Cloud:
    """Stub with the cloud budget; answers every call with one group per source and records the calls."""
    settings = SimpleNamespace(model='test')
    think = False
    request_budget_bytes = CLOUD_REQUEST_BUDGET_BYTES

    def __init__(self):
        self.calls = []
        self.sizes = []

    def _ask(self, body, parse, attempts):
        ids = json.loads(body['messages'][1]['content'])['source_ids']
        self.calls.append(ids)
        self.sizes.append(request_text_bytes(body))
        return parse(json.dumps({'groups': [{'message_ids': [i], 'facts': {}} for i in ids]}))


class SplitTests(RulesBootstrapTests):
    def test_ten_thousand_vietnamese_chars_is_one_call_not_a_hold(self):
        snapshot = self.snapshot()
        provider = Cloud()
        groups = split_and_extract(provider, snapshot, [message(1, text=vi(10000))])
        self.assertIsNotNone(groups)
        self.assertEqual(len(provider.calls), 1)
        self.assertGreater(provider.sizes[0], LOCAL_REQUEST_BUDGET_BYTES)
        self.assertLessEqual(provider.sizes[0], CLOUD_REQUEST_BUDGET_BYTES)

    def test_provider_without_budget_attribute_keeps_eight_thousand(self):
        class NoBudget(Cloud):
            def __getattribute__(self, name):
                if name == 'request_budget_bytes': raise AttributeError(name)
                return super().__getattribute__(name)
        plain = NoBudget()
        self.assertIsNone(partition_and_extract(plain, self.snapshot(), [message(1, text=vi(10000))]))
        self.assertEqual(plain.calls, [])

    def run_split(self, sources, provider=None, turn=None, **kwargs):
        provider = provider or Cloud()
        turn = turn or Turn(Store(':memory:'), 'rules:t', 't', 1)
        self.addCleanup(turn.store.close)
        token = CURRENT.set(turn)
        try:
            groups = split_and_extract(provider, self.snapshot(), sources, **kwargs)
        finally:
            CURRENT.reset(token)
        return groups, provider, turn

    def test_whole_batch_that_fits_is_one_call_whatever_the_message_count(self):
        for count, chars in ((5, 2000), (12, 300), (30, 40)):
            with self.subTest(count=count):
                groups, provider, turn = self.run_split([message(i, text=vi(chars)) for i in range(1, count + 1)])
                self.assertEqual(provider.calls, [list(range(1, count + 1))])
                self.assertEqual(len(groups), count)
                self.assertIsNone(turn.reason)

    def test_batch_over_budget_is_split_by_bytes_keeping_reply_chains_together(self):
        sources = [message(i, text=vi(3000), reply_to_id={2: 1, 12: 2, 11: 10}.get(i)) for i in range(1, 13)]
        groups, provider, turn = self.run_split(sources)
        self.assertGreaterEqual(len(provider.calls), 2)
        self.assertLessEqual(len(provider.calls), RULES_MAX_PARTS)
        self.assertTrue(all(size <= CLOUD_REQUEST_BUDGET_BYTES for size in provider.sizes))
        self.assertEqual(sorted(i for ids in provider.calls for i in ids), list(range(1, 13)))
        for chain in ({1, 2, 12}, {10, 11}):
            self.assertEqual(sum(chain <= set(ids) for ids in provider.calls), 1)
        self.assertEqual(sorted(i for ids, _ in groups for i in ids), list(range(1, 13)))
        self.assertEqual((turn.max_calls, turn.deadline - turn.started), (2 * len(provider.calls) + 1, 40 + 20 * (len(provider.calls) - 1)))
        self.assertIsNone(turn.reason)

    def test_too_many_parts_or_unfittable_component_overflow_and_set_reason(self):
        for name, sources in (('five parts', [message(i, text=vi(9000)) for i in range(1, 11)]),
                              ('one component', [message(i, text=vi(5000), reply_to_id=i - 1 if i > 1 else None) for i in range(1, 8)]),
                              ('one message', [message(1, text=vi(30000))])):
            with self.subTest(name=name):
                groups, provider, turn = self.run_split(sources)
                self.assertIsNone(groups)
                self.assertEqual(provider.calls, [])
                self.assertEqual(turn.reason, 'rules_request_too_large')

    def test_long_reply_chain_that_fits_is_one_call(self):
        chain = [message(i, text='ok', reply_to_id=i - 1 if i > 1 else None) for i in range(1, 8)]
        groups, provider, _ = self.run_split(chain)
        self.assertEqual((len(groups), len(provider.calls)), (7, 1))

    def test_allow_mixed_is_one_call_or_overflow_never_split(self):
        groups, provider, turn = self.run_split([message(i, text='cần sửa POS') for i in range(1, 8)], allow_mixed=True)
        self.assertEqual((len(groups), provider.calls), (7, [[1, 2, 3, 4, 5, 6, 7]]))
        groups, provider, turn = self.run_split([message(i, text=vi(9000)) for i in range(1, 5)], allow_mixed=True)
        self.assertIsNone(groups)
        self.assertEqual((provider.calls, turn.reason), ([], 'rules_request_too_large'))

    def test_spent_turn_budget_is_never_reported_as_too_large(self):
        # nested pending re-evaluation: the first evaluate already spent a call
        turn = Turn(Store(':memory:'), 'rules:t', 't', 1)
        turn.calls = 1
        groups, provider, turn = self.run_split([message(i, text='ok') for i in range(1, 7)], turn=turn, allow_mixed=True)
        self.assertEqual((len(groups), len(provider.calls), turn.reason), (6, 1, None))
        # a batch that really needs a split but cannot extend the budget is an ordinary hold
        spent = Turn(Store(':memory:'), 'rules:t', 't', 1)
        spent.calls = 1
        groups, provider, spent = self.run_split([message(i, text=vi(3000)) for i in range(1, 13)], turn=spent)
        self.assertIsNone(groups)
        self.assertEqual((provider.calls, spent.reason), ([], None))

    def test_part_failure_holds_whole_batch_without_overflow_reason(self):
        class FailSecond(Cloud):
            def _ask(self, body, parse, attempts):
                result = super()._ask(body, parse, attempts)
                return result if len(self.calls) == 1 else None
        turn = Turn(Store(':memory:'), 'rules:t', 't', 1)
        self.addCleanup(turn.store.close)
        token = CURRENT.set(turn)
        provider = FailSecond()
        try:
            result = evaluate_batch(self.snapshot(), provider, [message(i, text=vi(3000)) for i in range(1, 13)])
        finally:
            CURRENT.reset(token)
        self.assertIsNone(result)
        self.assertEqual(len(provider.calls), 2)
        self.assertIsNone(turn.reason)


class TurnBudgetTests(unittest.TestCase):
    def turn(self):
        store = Store(':memory:'); self.addCleanup(store.close)
        return Turn(store, 'rules:t', 't', 1)

    def test_configure_rules_parts_buys_calls_and_time(self):
        for parts, calls, seconds in ((1, 3, 40), (2, 5, 60), (4, 9, 100)):
            turn = self.turn()
            self.assertTrue(turn.configure_rules_parts(parts))
            self.assertEqual((turn.max_calls, turn.deadline - turn.started), (calls, seconds))

    def test_configure_rules_parts_guards(self):
        turn = self.turn()
        self.assertTrue(turn.configure_rules_parts(3))
        self.assertFalse(turn.configure_rules_parts(3))        # never twice
        self.assertFalse(self.turn().configure_rules_parts(5))  # beyond four parts
        self.assertFalse(self.turn().configure_rules_parts(0))
        late = self.turn(); late.calls = 1
        self.assertFalse(late.configure_rules_parts(2))         # only before the first call
        self.assertEqual(late.max_calls, 2)
        documents = self.turn()
        self.assertTrue(documents.configure_document_work(2, {'m': 1}))
        self.assertFalse(documents.configure_rules_parts(2))    # document work owns the budget
        self.assertEqual(documents.max_calls, 6)
        rules = self.turn()
        self.assertTrue(rules.configure_rules_parts(2))
        self.assertFalse(rules.configure_document_work(2, {'m': 1}))

    def test_single_part_may_still_be_extended_before_first_call(self):
        turn = self.turn()
        self.assertTrue(turn.configure_rules_parts(1))
        self.assertTrue(turn.configure_rules_parts(3))
        self.assertEqual(turn.max_calls, 7)

    def test_attempt_enforces_the_limit_it_is_given(self):
        turn = self.turn()
        body = {'messages': [{'role': 'user', 'content': 'x' * 9000}], 'format': {}}
        with self.assertRaises(Exception):
            turn.attempt(body)
        self.assertEqual(turn.reason, 'context_budget_exhausted')
        self.assertGreater(turn.attempt(body, limit=CLOUD_REQUEST_BUDGET_BYTES), 0)


class ProviderBudgetTests(RulesBootstrapTests):
    def cloud_provider(self):
        from tg_triage.provider import CloudProvider
        cfg = replace(config(), model=replace(config().model, provider='deepseek', cloud_consent=True, fallback='ollama'))
        return CloudProvider(cfg)

    def body(self, size):
        return {'model': 'm', 'stream': False, 'think': False, 'options': {}, 'format': {'type': 'object'},
                'messages': [{'role': 'system', 'content': 'x' * size}, {'role': 'user', 'content': '{}'}]}

    def test_budgets_per_provider(self):
        from tg_triage.provider import OllamaProvider
        self.assertEqual(self.cloud_provider().request_budget_bytes, CLOUD_REQUEST_BUDGET_BYTES)
        self.assertEqual(self.cloud_provider()._fallback.request_budget_bytes, LOCAL_REQUEST_BUDGET_BYTES)
        self.assertEqual(OllamaProvider(config().ollama).request_budget_bytes, LOCAL_REQUEST_BUDGET_BYTES)

    def test_fallback_is_not_called_for_a_body_over_local_budget(self):
        provider = self.cloud_provider()
        called = []
        provider._fallback._ask = lambda *a, **k: called.append(a) or 'local'
        with patch.object(provider, '_ask_cloud', return_value=None):
            self.assertIsNone(provider._ask(self.body(12000), lambda raw: raw))
            self.assertEqual(called, [])
            self.assertEqual(provider._ask(self.body(3000), lambda raw: raw), 'local')
            self.assertEqual(len(called), 1)

    def test_ollama_primary_is_still_limited_to_eight_thousand_bytes(self):
        from tg_triage.provider import OllamaProvider
        provider = OllamaProvider(config().ollama)
        store = Store(':memory:'); self.addCleanup(store.close)
        turn = Turn(store, 'rules:t', 't', 1)
        token = CURRENT.set(turn)
        try:
            self.assertIsNone(provider._ask(self.body(12000), lambda raw: raw, attempts=1))
        finally:
            CURRENT.reset(token)
        self.assertEqual((turn.reason, turn.calls), ('context_budget_exhausted', 0))

    def settings(self, **model):
        base = config()
        return replace(base, model=replace(base.model, provider='deepseek', name='test', cloud_consent=True,
                                           fallback='ollama', **model))

    def run_local_route(self, text, *, settings, error=None, images=None, has_image=False):
        from tg_triage.provider import CloudProvider
        from tg_triage.ai_transport import ProviderError
        from tests.helpers import FakeHTTP
        http = FakeHTTP({'message': {'content': json.dumps({'groups': [{'message_ids': [1], 'facts': {}}]})}})
        provider = CloudProvider(settings, opener=http)
        store = Store(':memory:'); self.addCleanup(store.close)
        turn = Turn(store, 'rules:t', 't', 1)
        token = CURRENT.set(turn)
        try:
            with patch('tg_triage.ai_transport.read_key', return_value='k'), \
                 patch('tg_triage.thinking_transport.complete', side_effect=error or ProviderError('timeout')) as cloud:
                groups = split_and_extract(provider, self.snapshot(), [message(1, text=text, has_image=has_image)], images=images)
        finally:
            CURRENT.reset(token)
        return groups, http, cloud, turn

    def test_cloud_timeout_falls_back_to_ollama_with_a_body_rebuilt_at_eight_thousand(self):
        for chars in (300, 1000, 1250):
            with self.subTest(chars=chars):
                groups, http, cloud, turn = self.run_local_route(vi(chars), settings=self.settings())
                self.assertIsNotNone(groups)
                self.assertEqual((cloud.call_count, len(http.calls)), (1, 1))
                sent = json.loads(http.calls[0][0].data)
                self.assertLessEqual(request_text_bytes(sent), LOCAL_REQUEST_BUDGET_BYTES)

    def test_image_without_cloud_image_consent_is_processed_by_ollama(self):
        from tests.helpers import PNG
        pixels = self.root / 'p.png'; pixels.write_bytes(PNG)
        groups, http, cloud, turn = self.run_local_route(vi(370), settings=self.settings(cloud_image_consent=False),
                                                         images={1: pixels}, has_image=True)
        self.assertIsNotNone(groups)
        self.assertEqual((cloud.call_count, len(http.calls)), (0, 1))

    def test_local_only_when_still_too_big_at_eight_thousand_is_refused(self):
        groups, http, cloud, turn = self.run_local_route(vi(9000), settings=self.settings())
        self.assertIsNone(groups)   # cloud timed out and a 9 000-char body cannot be rebuilt below 8 000
        self.assertEqual(len(http.calls), 0)


class OverflowIntakeTests(RulesBootstrapTests):
    def pipeline(self, provider):
        store = Store(':memory:'); self.addCleanup(store.close)
        settings = replace(config(), mode='live', gh_repo='sandbox/triage', autonomous_enabled=True, decision_rules_enabled=True,
                           telegram=replace(config().telegram, ops_chat_id=-1009876543210))
        sent = []
        from tests.helpers import FakeGitHub
        github = FakeGitHub()
        pipeline = Pipeline(settings, store, provider, github, rules_snapshot=self.snapshot(),
                            notice_sender=lambda *args: sent.append(args) or 70 + len(sent))
        return pipeline, store, sent, github

    def provider(self):
        class Provider(Cloud):
            pass
        return Provider()

    def assert_escalated_once(self, pipeline, store, sent, github, provider, result):
        self.assertEqual(result.reason, 'rules_request_too_large')
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], -1009876543210)
        self.assertIn('quá dài', sent[0][2])
        self.assertIn('24000', sent[0][2])
        self.assertNotIn('{{', sent[0][2])
        self.assertEqual(github.calls, [])
        self.assertEqual([r[0] for r in store.connection.execute('SELECT status FROM rules_topics')], ['ESCALATED'])
        pipeline.close_due_cases(); pipeline.close_due_cases(); pipeline.close_due_cases(); pipeline.close_due_cases()
        self.assertEqual(provider.calls, [])
        self.assertEqual(len(sent), 1)
        self.assertTrue(all(row[0] == -1009876543210 for row in sent))   # no text in the source group

    def test_single_thirty_thousand_char_message_is_reported_once_to_ops(self):
        provider = self.provider()
        pipeline, store, sent, github = self.pipeline(provider)
        result = pipeline.process(message(42, text=vi(30000), chat_title='Nhóm POS'))
        self.assert_escalated_once(pipeline, store, sent, github, provider, result)

    def test_batch_that_cannot_be_split_into_four_parts_is_reported_once_to_ops(self):
        provider = self.provider()
        pipeline, store, sent, github = self.pipeline(provider)
        sources = [message(100 + i, text=vi(9000), chat_title='Nhóm POS') for i in range(10)]
        for m in sources: store.enqueue(m)
        token = RELEASED_SOURCES.set(frozenset((m.chat_id, m.message_id) for m in sources))
        try: result = pipeline.process(sources[0])
        finally: RELEASED_SOURCES.reset(token)
        self.assert_escalated_once(pipeline, store, sent, github, provider, result)

    def test_many_short_messages_are_one_call_and_no_notice(self):
        provider = self.provider()
        pipeline, store, sent, github = self.pipeline(provider)
        sources = [message(100 + i, text='POS kẹt giấy', chat_title='Nhóm POS') for i in range(21)]
        for m in sources: store.enqueue(m)
        token = RELEASED_SOURCES.set(frozenset((m.chat_id, m.message_id) for m in sources))
        try: pipeline.process(sources[0])
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(provider.calls[0]), 21)
        self.assertNotIn('quá dài', ' '.join(str(a[2]) for a in sent))


if __name__ == '__main__':
    unittest.main()
