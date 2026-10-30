"""Blind bounded critic mechanics; fake transport does not prove semantic judgment."""
import base64
import copy
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from tg_triage.ingress_model import parse_ingress, parse_critic
from tg_triage.pipeline import Pipeline
from tg_triage.provider import CloudProvider
from tg_triage.store import Store
from tg_triage.thinking import CURRENT, Turn, request_text_bytes
from tests.helpers import config, message, NOW, FakeDownloader, FakeUploader, JPG
from tests.test_analysis_reply_contract import AuditedGitHub, Router
from tests.test_analysis_task_contract import wire_task
from tests.test_document_content_contract import new_wire

SENTINEL = 'CRITIC_PRIVATE_SENTINEL_41a8'


def reviews(*verdicts):
    return {'reviews': [{'group_index': index, 'verdict': verdict}
                        for index, verdict in enumerate(verdicts)]}


def proposals():
    first = new_wire([10, 11])
    first['groups'].extend(wire_task(ids=(12,))['groups'])
    return first


class IngressCriticContractTests(TestCase):
    def cfg(self):
        cfg = replace(config('live'), autonomous_enabled=True, thinking_enabled=True)
        return replace(cfg, model=replace(cfg.model, provider='openai', name='test', think=False,
            cloud_consent=True, cloud_image_consent=True, base_url='https://api.openai.com/v1'))

    def store(self, path=':memory:'):
        store = Store(path)
        self.addCleanup(store.close)
        return store

    def transport(self, outputs, before=None):
        responses = iter(outputs)
        requests = []
        def complete(*args, **kwargs):
            requests.append(copy.deepcopy(args[3]))
            if before:
                before(len(requests))
            return next(responses)
        self.enterContext(patch('tg_triage.ai_transport.read_key', return_value='fake'))
        self.enterContext(patch('tg_triage.thinking_transport.complete', side_effect=complete))
        return requests

    def invoke(self, first, critic, sources=None, candidates=None, *, history=None, images=None,
               before=None, clock=None):
        sources = sources or [message(10, has_image=True), message(11), message(12)]
        store = self.store()
        turn = Turn(store, 'critic:test', 'critic:root', sources[0].message_id,
                    **({'clock': clock} if clock else {}))
        token = CURRENT.set(turn)
        self.addCleanup(CURRENT.reset, token)
        requests = self.transport([first, critic, reviews('approve', 'approve')], before)
        model = CloudProvider(self.cfg())
        result = model.analyze_ingress(sources, candidates or [], read_context=history or [], images=images or {})
        return result, requests, turn, store

    def build(self, outputs, *, path=':memory:', sender=None, before=None):
        requests = self.transport(outputs, before)
        p = Pipeline(self.cfg(), self.store(path), CloudProvider(self.cfg()), AuditedGitHub(),
                     downloader=FakeDownloader({10: (JPG, '10.jpg')}), uploader=FakeUploader(), notice_sender=sender)
        return p, requests

    def feed(self, pipeline):
        pipeline.store.enqueue(message(11))
        pipeline.store.enqueue(message(12))
        return pipeline.process(message(10, has_image=True))

    def assert_approved(self, expected, actual, candidate=None):
        # The public host validator adds a resolved candidate object to the parsed group.
        self.assertEqual(expected, {key: actual[key] for key in expected})
        self.assertEqual(set(expected) | {'candidate'}, set(actual))
        self.assertEqual(candidate, actual['candidate'])

    def test_parser_accepts_exact_coverage_in_any_order_including_valid_reject(self):
        payload = {'reviews': [{'group_index': 1, 'verdict': 'reject'},
                               {'group_index': 0, 'verdict': 'approve'}]}
        self.assertEqual(['approve', 'reject'], parse_critic(json.dumps(payload), 2))
        self.assertEqual(['context'], parse_critic(json.dumps(reviews('context')), 1))

    def test_parser_rejects_missing_duplicate_foreign_or_malformed_group_indices(self):
        malformed = [[], [{'group_index': 0, 'verdict': 'approve'}],
                     [{'group_index': 0, 'verdict': 'approve'}] * 2]
        for index in (-1, 2, True, False, 0.0, '0', None, 10000000000000000000):
            malformed.append([{'group_index': index, 'verdict': 'approve'},
                              {'group_index': 1, 'verdict': 'approve'}])
        for rows in malformed:
            with self.subTest(rows=rows):
                self.assertIsNone(parse_critic(json.dumps({'reviews': rows}), 2))

    def test_parser_rejects_all_extra_authority_prose_and_nonenum_verdicts(self):
        values = [None, '', SENTINEL, [], {}, {'reviews': [], SENTINEL: SENTINEL}]
        for verdict in ('NEW', 'Approve', 'merge', True, 1, None, {'reasoning_content': SENTINEL}):
            values.append({'reviews': [{'group_index': 0, 'verdict': verdict}]})
        for extra in ('message_ids', 'candidate_id', 'analysis', 'task', 'question', SENTINEL):
            values.append({'reviews': [{'group_index': 0, 'verdict': 'approve', extra: SENTINEL}]})
        for value in values:
            with self.subTest(value=repr(value)[:80]):
                self.assertIsNone(parse_critic(json.dumps(value), 1))

    def test_approve_preserves_first_canonical_groups_and_task_without_regeneration(self):
        first = proposals()
        expected = parse_ingress(json.dumps(first), require_analyze_task=True)
        result, requests, turn, _ = self.invoke(first, reviews('approve', 'approve'))
        self.assertEqual(len(expected), len(result))
        for original, final in zip(expected, result):
            self.assert_approved(original, final)
        self.assertEqual(2, len(requests))
        self.assertEqual(2, turn.calls)
        self.assertEqual(expected[0]['analysis'].details, result[0]['analysis'].details)
        self.assertEqual(expected[1]['task'], result[1]['task'])

    def test_approve_preserves_existing_exact_candidate_and_question(self):
        first = new_wire([10])
        first['groups'][0].update(action='CLARIFY', candidate_id='c1', question='Màn hình nào?')
        first['groups'].extend(wire_task(ids=(12,))['groups'])
        old = message(1, received_at=NOW - timedelta(minutes=2))
        candidates = [{'candidate_id': 'c1', 'pending': True, 'exact_reply': True,
                       'reply_source_ids': [10], 'messages': [old]}]
        sources = [message(10, reply_to_id=1), message(12, has_image=True)]
        expected = parse_ingress(json.dumps(first), require_analyze_task=True)
        result, requests, _, _ = self.invoke(first, reviews('approve', 'approve'), sources, candidates)
        self.assert_approved(expected[0], result[0], candidates[0])
        self.assert_approved(expected[1], result[1])
        self.assertEqual('c1', result[0]['candidate_id'])
        self.assertEqual('Màn hình nào?', result[0]['question'])
        self.assertEqual(2, len(requests))

    def test_context_lowers_whole_group_to_fixed_silent_chat_and_keeps_other_group_exact(self):
        first = proposals()
        first['groups'][0]['analysis']['title'] = 'UNPROVEN_SMTP_FAILURE'
        expected = parse_ingress(json.dumps(first), require_analyze_task=True)
        result, requests, _, _ = self.invoke(first, reviews('context', 'approve'))
        self.assertIsNotNone(result)
        context = result[0]
        self.assertEqual([10, 11], context['message_ids'])
        self.assertEqual('CHAT', context['action'])
        self.assertIsNone(context['candidate_id'])
        self.assertEqual('', context['question'])
        self.assertEqual('noise', context['analysis'].verdict)
        self.assertEqual('chat', context['analysis'].category)
        self.assertFalse(context['analysis'].actionable)
        self.assertIsNone(context['analysis'].classification)
        self.assertEqual((10, 11), context['analysis'].key_message_ids)
        self.assertTrue(all(key not in context for key in ('task', 'answer', 'options', 'needs_choice')))
        self.assert_approved(expected[1], result[1])
        self.assertNotIn('UNPROVEN_SMTP_FAILURE', repr(context))
        self.assertEqual(2, len(requests))

    def test_context_downgrade_produces_no_telegram_reply_or_github_effect(self):
        notices = []
        p, requests = self.build([proposals(), reviews('context', 'context')],
                                sender=lambda c, m, t: notices.append((m, t)) or 901)
        self.feed(p)
        self.assertEqual(2, len(requests))
        self.assertEqual([], notices)
        self.assertEqual([], p.github.calls)
        self.assertEqual([], p.github.mutations)
        self.assertEqual([], p.attachments.uploader.uploads)
        for mid in (10, 11, 12):
            self.assertEqual('chat', p.store.get(message().chat_id, mid)['decision'])

    def test_reject_atomically_blocks_all_proposals_before_plans_or_external_effects(self):
        notices = []
        p = None
        observed_before_second = []
        def before(count):
            if count == 2:
                observed_before_second.append([p.store.autonomous_plan(message().chat_id, mid) for mid in (10, 11, 12)])
                self.assertEqual([], p.github.calls)
                self.assertEqual([], notices)
        p, requests = self.build([proposals(), reviews('approve', 'reject')],
                                sender=lambda c, m, t: notices.append(m) or 901, before=before)
        self.feed(p)
        self.assertEqual([[None, None, None]], observed_before_second)
        self.assertEqual(2, len(requests))
        self.assertEqual([10], notices)
        self.assertEqual('semantic_review_rejected', p.classifier.last_ingress_outcome)
        self.assertEqual([], p.github.calls)
        self.assertEqual([], p.github.mutations)
        for mid in (10, 11, 12):
            self.assertEqual('pending_review', p.store.get(message().chat_id, mid)['decision'])
            self.assertNotEqual('NEW', p.store.autonomous_plan(message().chat_id, mid)['action'])

    def test_bad_critic_coverage_and_unknown_prose_fail_whole_batch_without_third_call(self):
        malformed = {'reviews': [{'group_index': 0, 'verdict': 'approve', SENTINEL: SENTINEL}]}
        p, requests = self.build([proposals(), malformed, reviews('approve', 'approve')])
        self.feed(p)
        self.assertEqual(2, len(requests))
        self.assertEqual([], p.github.calls)
        self.assertTrue(all(p.store.get(message().chat_id, mid)['decision'] == 'pending_review' for mid in (10, 11, 12)))
        self.assertNotIn(SENTINEL, '\n'.join(p.store.connection.iterdump()))

    def test_invalid_first_partition_cannot_be_rewritten_by_critic_into_valid_authority(self):
        first = proposals()
        first['groups'][0]['message_ids'].append(999)
        result, requests, turn, _ = self.invoke(first, reviews('approve', 'approve'))
        self.assertIsNone(result)
        self.assertLessEqual(len(requests), 2)
        self.assertEqual(len(requests), turn.calls)
        # A producer repair may use slot2; it must never be mistaken for a critic authority upgrade.
        self.assertFalse(any('PROPOSED_ROUTES_FOR_REVIEW' in json.dumps(messages) for messages in requests))

    def test_full_untrusted_canonical_proposals_are_user_data_not_system_instructions(self):
        first = proposals()
        first['groups'][0]['analysis']['details']['work'] = SENTINEL
        result, requests, _, _ = self.invoke(first, reviews('approve', 'approve'))
        self.assertIsNotNone(result)
        second = requests[1]
        self.assertNotIn(SENTINEL, json.dumps([m for m in second if m['role'] != 'user']))
        user = next(m for m in second if m['role'] == 'user')
        packet = json.loads(user['content'])
        offered = packet['PROPOSED_ROUTES_FOR_REVIEW']
        self.assertEqual([0, 1], [row['group_index'] for row in offered])
        self.assertEqual([10, 11], offered[0]['message_ids'])
        self.assertEqual(SENTINEL, offered[0]['analysis']['details']['work'])
        self.assertEqual(first['groups'][1]['task'], offered[1]['task'])
        self.assertLessEqual(request_text_bytes({'messages': second}), 8000)

    def test_unfittable_full_proposals_fail_closed_without_truncating_or_partial_execution(self):
        first = {'groups': []}
        sources = []
        for mid in range(1, 21):
            wire = new_wire([mid])['groups'][0]
            wire['analysis']['details'].update(work='W' * 500, scope_in='S' * 500,
                                               scope_out='X' * 500, done=['D' * 200] * 10)
            first['groups'].append(wire)
            sources.append(message(mid, text='Current request', has_image=mid == 1))
        result, requests, _, _ = self.invoke(first, reviews(*(['approve'] * 20)), sources)
        self.assertIsNone(result)
        self.assertEqual(1, len(requests))

    def test_critic_cannot_reset_shared_deadline_or_acquire_third_call(self):
        now = [0.0]
        def before(count):
            if count == 1:
                now[0] = 41.0
        result, requests, turn, _ = self.invoke(proposals(), reviews('approve', 'approve'),
                                               before=before, clock=lambda: now[0])
        self.assertIsNone(result)
        self.assertEqual(1, len(requests))
        self.assertEqual(1, turn.calls)

    def test_cached_critic_rejection_is_immutable_and_restart_does_not_notify_again(self):
        folder = self.enterContext(TemporaryDirectory())
        path = str(Path(folder) / 'rejected.sqlite3')
        notices = []
        p, _ = self.build([proposals(), reviews('approve', 'reject')], path=path,
                          sender=lambda c, m, t: notices.append(m) or 901)
        self.feed(p)
        plans = [copy.deepcopy(p.store.autonomous_plan(message().chat_id, mid)) for mid in (10, 11, 12)]
        p.store.close()
        resumed = Pipeline(self.cfg(), self.store(path), Router(), AuditedGitHub(),
                           notice_sender=lambda c, m, t: notices.append(m) or 902)
        for mid in (10, 11, 12):
            resumed.process(message(mid))
        resumed.close_due_cases()
        self.assertEqual([10], notices)
        self.assertEqual([], resumed.classifier.calls)
        self.assertEqual([], resumed.github.calls)
        self.assertEqual(plans, [resumed.store.autonomous_plan(message().chat_id, mid) for mid in (10, 11, 12)])

    def test_approved_unknown_post_is_not_created_again_or_recriticized_after_restart(self):
        folder = self.enterContext(TemporaryDirectory())
        path = str(Path(folder) / 'ambiguous.sqlite3')
        p, requests = self.build([proposals(), reviews('approve', 'context')], path=path)
        p.github.error = OSError('POST result unknown')
        self.feed(p)
        self.assertEqual(2, len(requests))
        self.assertEqual(1, len(p.github.calls))
        plans = [copy.deepcopy(p.store.autonomous_plan(message().chat_id, mid)) for mid in (10, 11, 12)]
        p.store.close()
        resumed = Pipeline(self.cfg(), self.store(path), Router(), AuditedGitHub())
        for mid in (10, 11, 12):
            resumed.process(message(mid))
        resumed.close_due_cases()
        self.assertEqual([], resumed.classifier.calls)
        self.assertEqual([], resumed.github.calls)
        self.assertEqual(plans, [resumed.store.autonomous_plan(message().chat_id, mid) for mid in (10, 11, 12)])

    def test_full_valid_proposals_reclaim_history_without_losing_current_scope_or_draft_fields(self):
        pages = ['home', '/sms-consent', '/privacy-policy', '/terms-of-service',
                 '/muchie-lash-brows', '/bitcoin-nail-bars', '/nail-world']
        footer = ('Add a real public contact link in the shared footer, never hidden in an image or button. '
                  'Apply the same legal contact information on every shared footer, including ' +
                  ', '.join(pages) + '. Verify all seven page footers use the same current information.')
        sources = [message(10, text=footer, has_image=True),
                   message(11, text='Current secondary request ' * 5),
                   message(12, text='Independent current task ' * 5)]
        first = new_wire([10, 11])
        first['groups'].extend(new_wire([12])['groups'])
        for group in first['groups']:
            group['analysis']['details'].update(work='W' * 350, scope_in='I' * 350,
                scope_out='O' * 350, reason_context='R' * 350, done=['D' * 190, 'E' * 190])
        history = [message(i, text='Earlier history ' * 300, received_at=NOW - timedelta(minutes=1))
                   for i in range(1, 9)]
        expected = parse_ingress(json.dumps(first), require_analyze_task=True)
        result, requests, turn, _ = self.invoke(first, reviews('approve', 'approve'), sources, history=history)
        self.assertIsNotNone(result)
        self.assertEqual(2, len(requests))
        self.assertEqual(2, turn.calls)
        packets = []
        for messages in requests:
            self.assertLessEqual(request_text_bytes({'messages': messages}), 8000)
            packet = json.loads(next(m['content'] for m in messages if m['role'] == 'user'))
            packets.append(packet)
            text_column = packet['source_columns'].index('text')
            current = {row[0]: row[text_column] for row in packet['sources']}
            self.assertEqual([10, 11, 12], packet['NEW_SOURCE_IDS'])
            self.assertEqual(footer, current[10])
            self.assertTrue(all(page in current[10] for page in pages))
        self.assertEqual(8, len(packets[0]['READ_ONLY_CONTEXT']))
        self.assertLess(len(packets[1]['READ_ONLY_CONTEXT']), 8)
        self.assertTrue(packets[1]['read_context_truncated'])
        offered = packets[1]['PROPOSED_ROUTES_FOR_REVIEW']
        for index, row in enumerate(offered):
            self.assertEqual(index, row['group_index'])
            self.assertEqual(first['groups'][index]['analysis']['details'], row['analysis']['details'])
            self.assert_approved(expected[index], result[index])
