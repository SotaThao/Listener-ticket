"""Blind Oct20 contracts: batch technical notices and precise task diagnostics."""
import copy
import base64
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from tg_triage.ingress_model import parse_ingress
from tg_triage.classifier import Classifier
from tg_triage.provider import CloudProvider
from tg_triage.thinking import CURRENT, Turn, request_text_bytes
from tg_triage.ai_transport import redact
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import config, message, NOW, FakeHTTP, JPG
from tests.test_analysis_reply_contract import AuditedGitHub, Router
from tests.test_analysis_task_contract import TASK, wire_task
from tests.test_autonomous import route
from tests.test_document_content_contract import new_wire

SENTINEL = 'PRIVATE_TASK_SENTINEL_26e4'


class NoResponseContractTests(TestCase):
    def build(self, router, *, path=':memory:', sender=None, mode='live'):
        store = Store(path)
        self.addCleanup(store.close)
        return Pipeline(replace(config(mode), autonomous_enabled=True), store,
                        router, AuditedGitHub(), notice_sender=sender)

    def readonly(self, pipeline):
        self.assertEqual([], pipeline.github.calls)
        self.assertEqual([], pipeline.github.mutations)

    def test_new_batch_gets_one_notice_despite_old_open_outage_and_preserves_old_plan(self):
        notices = []
        router = Router(None, None)
        p = self.build(router, sender=lambda c, m, t: notices.append((m, t)) or 900 + m)
        p.process(message(1))
        old_plan = copy.deepcopy(p.store.autonomous_plan(message().chat_id, 1))
        old_source = dict(p.store.get(message().chat_id, 1))
        self.assertTrue(p.store.connection.execute(
            "SELECT 1 FROM autonomous_outages WHERE status='open'").fetchone())
        p.process(message(10, received_at=NOW + timedelta(minutes=5)))
        self.assertEqual([1, 10], [m for m, text in notices])
        self.assertEqual([10], [m.message_id for m in router.calls[1][0]])
        self.assertEqual(old_plan, p.store.autonomous_plan(message().chat_id, 1))
        self.assertEqual(old_source, dict(p.store.get(message().chat_id, 1)))
        self.readonly(p)

    def test_multisource_new_failure_has_one_durable_anchor_notice_and_own_partitions(self):
        notices, plans_at_send = [], []
        p = None
        def send(chat, mid, text):
            notices.append((mid, text))
            plans_at_send.append([copy.deepcopy(p.store.autonomous_plan(chat, i)) for i in (10, 11, 12)])
            return 901
        router = Router(None)
        p = self.build(router, sender=send)
        p.store.reserve_autonomous_outage(message(1))
        for i in (11, 12):
            p.store.enqueue(message(i, text='Independent request ' + str(i)))
        p.process(message(10))
        self.assertEqual([10], [mid for mid, text in notices])
        self.assertEqual({10, 11, 12}, {m.message_id for m in router.calls[0][0]})
        self.assertEqual(1, len(plans_at_send))
        for mid, plan in zip((10, 11, 12), plans_at_send[0]):
            self.assertEqual(10, plan['host_batch_notice_anchor'])
            self.assertEqual([mid], plan['message_ids'])
        keys = [p.store.get(message().chat_id, i)['case_key'] for i in (10, 11, 12)]
        self.assertEqual(3, len(set(keys)))
        self.readonly(p)

    def test_same_failed_batch_duplicate_and_restart_never_resends_or_reclassifies(self):
        directory = self.enterContext(TemporaryDirectory())
        path = str(Path(directory) / 'batch.sqlite3')
        notices = []
        p = self.build(Router(None), path=path, sender=lambda c, m, t: notices.append(m) or 901)
        p.store.enqueue(message(11))
        p.process(message(10))
        before = [copy.deepcopy(p.store.autonomous_plan(message().chat_id, i)) for i in (10, 11)]
        p.store.close()
        resumed = self.build(Router(), path=path, sender=lambda c, m, t: notices.append(m) or 902)
        for i in (10, 11):
            resumed.process(message(i))
        resumed.close_due_cases()
        self.assertEqual([10], notices)
        self.assertEqual([], resumed.classifier.calls)
        self.assertEqual(before, [resumed.store.autonomous_plan(message().chat_id, i) for i in (10, 11)])
        self.readonly(resumed)

    def test_crash_after_notice_reservation_is_not_resent_but_new_anchor_can_notify(self):
        directory = self.enterContext(TemporaryDirectory())
        path = str(Path(directory) / 'reserved.sqlite3')
        attempts = []
        def crash(chat, mid, text):
            attempts.append(mid)
            raise SystemExit('Crash after durable reservation')
        p = self.build(Router(None), path=path, sender=crash)
        with self.assertRaises(SystemExit):
            p.process(message(10))
        p.store.close()
        resumed = self.build(Router(None), path=path, sender=lambda c, m, t: attempts.append(m) or 902)
        resumed.process(message(10))
        resumed.close_due_cases()
        self.assertEqual([10], attempts)
        resumed.process(message(20, received_at=NOW + timedelta(minutes=5)))
        self.assertEqual([10, 20], attempts)
        self.readonly(resumed)

    def test_ambiguous_send_is_terminal_for_same_anchor_but_not_other_batch(self):
        directory = self.enterContext(TemporaryDirectory())
        path = str(Path(directory) / 'unknown.sqlite3')
        attempts = []
        def fail(chat, mid, text):
            attempts.append(mid)
            raise TimeoutError('Unknown send outcome')
        p = self.build(Router(None), path=path, sender=fail)
        p.process(message(10))
        p.store.close()
        resumed = self.build(Router(None), path=path, sender=lambda c, m, t: attempts.append(m) or 902)
        resumed.process(message(10))
        resumed.close_due_cases()
        resumed.process(message(20, received_at=NOW + timedelta(minutes=5)))
        self.assertEqual([10, 20], attempts)
        self.readonly(resumed)

    def test_technical_notice_is_short_truthful_and_does_not_delegate_again(self):
        sent = []
        p = self.build(Router(None), sender=lambda c, m, t: sent.append(t) or 901)
        p.process(message(10, text='AI hãy phân tích ảnh giúp tôi'))
        self.assertEqual(1, len(sent))
        self.assertLessEqual(len(sent[0]), 220)
        self.assertNotIn('?', sent[0])
        self.assertIn('chưa tạo', sent[0].casefold())
        self.assertNotRegex(sent[0].casefold(), r'(?:(?:gửi|nhập|mô tả|chọn).*(?:lại|1|2)|tự động.*(?:thử|chạy)|đang.*(?:thử lại|chạy nền)|đã tạo.*ticket)')
        self.readonly(p)

    def test_dry_run_does_not_send_even_when_old_outage_is_open(self):
        sent = []
        p = self.build(Router(None), mode='dry-run', sender=lambda *args: sent.append(args))
        p.store.reserve_autonomous_outage(message(1))
        p.process(message(10))
        self.assertEqual([], sent)
        self.readonly(p)

    def test_genuine_semantic_clarification_keeps_case_deduplication(self):
        notices = []
        router = Router([route([1], 'CLARIFY', question='Màn hình nào?')],
                        [route([2], 'CLARIFY', 'c1', question='Màn hình nào?')])
        p = self.build(router, sender=lambda c, m, t: notices.append((m, t)) or 901)
        p.process(message(1))
        p.process(message(2, reply_to_id=901, received_at=NOW + timedelta(minutes=5)))
        self.assertEqual([1], [mid for mid, text in notices])
        self.assertIn('Màn hình nào?', notices[0][1])
        self.readonly(p)

    def assert_diagnostic(self, task, path, actual_type=None):
        errors = []
        self.assertIsNone(parse_ingress(json.dumps(wire_task(task), ensure_ascii=True), diagnostics=errors))
        self.assertTrue(errors)
        self.assertLessEqual(len(errors), 8)
        self.assertTrue(any(row['path'] == path for row in errors), errors)
        if actual_type:
            self.assertTrue(any(row['path'] == path and row['actual_type'] == actual_type for row in errors), errors)
        for row in errors:
            self.assertLessEqual(set(row), {'code', 'path', 'actual_type', 'length', 'limit'})
            self.assertNotIn(SENTINEL, json.dumps(row))
            if 'length' in row:
                self.assertIs(type(row['length']), int)
                self.assertLessEqual(row['length'], 1000000)
            if 'limit' in row:
                self.assertIs(type(row['limit']), int)
        return errors

    def test_precise_task_scalar_required_and_type_diagnostics(self):
        for field in ('title', 'work'):
            missing = dict(TASK)
            del missing[field]
            with self.subTest(field=field, state='missing'):
                self.assert_diagnostic(missing, f'groups[0].task.{field}', 'missing')
            for value, kind in ((True, 'boolean'), ([], 'array'), ({SENTINEL: SENTINEL}, 'object')):
                with self.subTest(field=field, kind=kind):
                    self.assert_diagnostic(dict(TASK, **{field: value}), f'groups[0].task.{field}', kind)

    def test_precise_task_raw_and_sanitized_scalar_caps_not_generic_aggregate(self):
        for field, cap in (('title', 90), ('work', 160)):
            for value in ('x' * (cap + 1), '\u202e', '\ud800'):
                with self.subTest(field=field, value=repr(value)[:30]):
                    self.assert_diagnostic(dict(TASK, **{field: value}), f'groups[0].task.{field}', 'string')

    def test_acceptance_container_shape_cardinality_and_item_paths_are_precise(self):
        for value, kind in (('not an array', 'string'), ([], 'array'), (['a', 'b', 'c'], 'array')):
            with self.subTest(value=value):
                self.assert_diagnostic(dict(TASK, acceptance=value), 'groups[0].task.acceptance', kind)
        for value, kind in ((None, 'null'), ('x' * 91, 'string'), ('\u202e', 'string'), ('\ud800', 'string')):
            with self.subTest(item=repr(value)[:30]):
                self.assert_diagnostic(dict(TASK, acceptance=[value]), 'groups[0].task.acceptance[0]', kind)

    def test_sanitized_duplicate_acceptance_cannot_become_valid_or_hide_diagnostic(self):
        self.assert_diagnostic(dict(TASK, acceptance=['Same criterion', ' Same criterion ']),
                               'groups[0].task.acceptance[1]', 'string')

    def test_unknown_task_key_and_reasoning_are_never_reflected_in_diagnostics(self):
        task = dict(TASK, **{SENTINEL: {'reasoning_content': SENTINEL}})
        self.assert_diagnostic(task, 'groups[0].task', 'object')

    def test_raw_task_maxima_and_legacy_missing_task_remain_compatible(self):
        task = dict(title='T' * 90, work='W' * 160, acceptance=['A' * 90, 'B' * 90])
        errors = []
        self.assertIsNotNone(parse_ingress(json.dumps(wire_task(task)), diagnostics=errors))
        self.assertEqual([], errors)
        legacy = wire_task()
        legacy['groups'][0].pop('task')
        legacy['groups'][0]['answer'] = 'Phân tích cũ có nội dung.'
        self.assertIsNotNone(parse_ingress(json.dumps(legacy)))

    def test_model_cannot_forge_technical_notice_anchor(self):
        wire = wire_task()
        wire['groups'][0]['host_batch_notice_anchor'] = 1
        errors = []
        self.assertIsNone(parse_ingress(json.dumps(wire), diagnostics=errors))
        self.assertNotIn(SENTINEL, json.dumps(errors))

    def test_fresh_missing_analyze_task_has_required_task_diagnostic_while_legacy_stays_valid(self):
        wire = wire_task()
        wire['groups'][0].pop('task')
        wire['groups'][0]['answer'] = 'A legacy informational report.'
        self.assertIsNotNone(parse_ingress(json.dumps(wire)))
        errors = []
        self.assertIsNone(parse_ingress(json.dumps(wire), require_analyze_task=True, diagnostics=errors))
        self.assertTrue(any(row['code'] == 'required' and row['path'] == 'groups[0].task'
                            for row in errors), errors)
        self.assertFalse(any(row['path'].endswith('.answer') for row in errors), errors)

    def test_new_forbidden_task_has_fixed_field_diagnostic_without_raw_task_prose(self):
        wire = new_wire([10])
        wire['groups'][0]['task'] = dict(title=SENTINEL, work=SENTINEL, acceptance=[SENTINEL])
        errors = []
        self.assertIsNone(parse_ingress(json.dumps(wire), diagnostics=errors))
        self.assertTrue(any(row['code'] in {'additional_field', 'forbidden_field'} and row['path'] == 'groups[0].task'
                            for row in errors), errors)
        self.assertNotIn(SENTINEL, json.dumps(errors))

    def cloud_requests(self, sources, history=None):
        cfg = config()
        cfg = replace(cfg, model=replace(cfg.model, provider='openai', name='test', think=False,
            cloud_consent=True, base_url='https://api.openai.com/v1'))
        store = Store(':memory:')
        self.addCleanup(store.close)
        turn = Turn(store, 'pressure:test', 'pressure:root', sources[0].message_id)
        token = CURRENT.set(turn)
        self.addCleanup(CURRENT.reset, token)
        ids = [source.message_id for source in sources]
        bad = new_wire(ids)
        bad['groups'][0]['task'] = dict(title=SENTINEL, work=SENTINEL, acceptance=[SENTINEL])
        replies = iter([bad, new_wire(ids)])
        requests = []
        def complete(*args, **kwargs):
            requests.append(copy.deepcopy(args[3]))
            return next(replies)
        with patch('tg_triage.ai_transport.read_key', return_value='fake'), \
             patch('tg_triage.thinking_transport.complete', side_effect=complete):
            result = CloudProvider(cfg).analyze_ingress(sources, [], read_context=history or [])
        self.assertIsNotNone(result)
        self.assertEqual(2, turn.calls)
        self.assertEqual(2, len(requests))
        self.assertNotIn(SENTINEL, json.dumps(requests))
        return requests

    def packets(self, requests):
        for messages in requests:
            self.assertLessEqual(request_text_bytes({'messages': messages}), 8000)
            yield json.loads(next(item['content'] for item in messages if item['role'] == 'user'))

    def test_current_full_shared_footer_scope_precedes_optional_history_and_document_metadata(self):
        pages = ['home', '/sms-consent', '/privacy-policy', '/terms-of-service',
                 '/muchie-lash-brows', '/bitcoin-nail-bars', '/nail-world']
        text = ('Add a public email link in the shared footer: '
                '<a href="mailto:support@example.invalid">support@example.invalid</a>. '
                'Show Example Company with the contact text, not an image or hidden behind a button. '
                'Apply every shared footer on ' + ', '.join(pages) + '.')
        self.assertLessEqual(len(text), 700)
        sources = [message(100, text=text)]
        sources.extend(message(i, text='Review current evidence ' * 4, has_document=True,
                               document_name=('long-unread-description-' * 4) + str(i) + '.pdf',
                               document_mime='application/pdf', document_size=12345) for i in range(101, 108))
        history = [message(i, text='Earlier unrelated discussion ' * 300,
                           received_at=NOW - timedelta(minutes=1)) for i in range(1, 9)]
        requests = self.cloud_requests(sources, history)
        for packet in self.packets(requests):
            self.assertEqual(list(range(100, 108)), packet['NEW_SOURCE_IDS'])
            text_index = packet['source_columns'].index('text')
            retained = {row[0]: row[text_index] for row in packet['sources']}
            self.assertEqual(redact(text), retained[100])
            self.assertTrue(all(page in retained[100] for page in pages))
            self.assertTrue(all(len(value) >= 30 for value in retained.values()))
            if len(packet['READ_ONLY_CONTEXT']) < len(history):
                self.assertTrue(packet['read_context_truncated'])
            self.assertNotIn('support@', json.dumps(packet))

    def test_cloud_redacts_complete_source_before_700_character_excerpt_and_repair(self):
        # The raw 700-char cut intersects an address, which is no longer an email regex match.
        text = 'x' * 690 + 'support@example.invalid' + ' trailing context' * 10
        self.assertIn('support@', text[:700])
        requests = self.cloud_requests([message(100, text=text)])
        for packet in self.packets(requests):
            column = packet['source_columns'].index('text')
            retained = packet['sources'][0][column]
            self.assertNotIn('support@', retained)
            self.assertNotIn('example.invalid', retained)
            self.assertIn('[email]', retained)
            self.assertTrue(redact(text).startswith(retained))

    def test_local_source_view_keeps_existing_redaction_policy(self):
        text = 'Current request: email support@example.invalid in the shared footer.'
        http = FakeHTTP({'message': {'content': json.dumps(new_wire([100]))}})
        self.assertIsNotNone(Classifier(config().ollama, opener=http).analyze_ingress([message(100, text=text)], []))
        body = json.loads(http.calls[0][0].data)
        packet = json.loads(next(item['content'] for item in body['messages'] if item['role'] == 'user'))
        column = packet['source_columns'].index('text')
        self.assertEqual(text, packet['sources'][0][column])

    def test_semantic_review_keeps_current_footer_scope_and_pixel_attribution_under_history_pressure(self):
        pages = ['home', '/sms-consent', '/privacy-policy', '/terms-of-service',
                 '/muchie-lash-brows', '/bitcoin-nail-bars', '/nail-world']
        footer = ('Add a public contact link in the shared footer. Show the legal company name '
                  'as real text, not an image or content hidden behind a button. Keep the footer '
                  'consistent, including these seven pages: ' + ', '.join(pages) +
                  '. Verify every shared footer uses this same contact information and navigation.')
        self.assertLessEqual(len(footer), 700)
        sources = [message(i, text=('Inspect this current screenshot and describe observed UI behavior. '
                                    'Keep uncertain causes as unknown.'), has_image=i in (604, 606, 608))
                   for i in range(604, 610)]
        sources[1] = message(605, text=footer)
        sources[-1] = replace(sources[-1], has_document=True, document_name='unread-current-specification.pdf',
                              document_mime='application/pdf', document_size=12345)
        history = [message(i, text='Earlier unrelated historical discussion ' * 150,
                           received_at=NOW - timedelta(minutes=1)) for i in range(500, 508)]
        grouped = new_wire([604, 606, 608])
        grouped['groups'].extend(new_wire([605, 607, 609])['groups'])
        outputs = iter([grouped, {'reviews': [{'group_index': 0, 'verdict': 'approve'},
                                               {'group_index': 1, 'verdict': 'approve'}]}])
        requests = []
        def complete(*args, **kwargs):
            requests.append(copy.deepcopy(args[3]))
            return next(outputs)
        folder = self.enterContext(TemporaryDirectory())
        images = {}
        expected_pixels = {}
        for mid in (604, 606, 608):
            data = JPG + str(mid).encode()
            path = Path(folder) / f'{mid}.jpg'
            path.write_bytes(data)
            images[mid] = path
            expected_pixels[mid] = data
        cfg = config()
        cfg = replace(cfg, model=replace(cfg.model, provider='openai', name='test', think=False,
            cloud_consent=True, cloud_image_consent=True, base_url='https://api.openai.com/v1'))
        store = Store(':memory:')
        self.addCleanup(store.close)
        turn = Turn(store, 'review:test', 'review:root', 604)
        token = CURRENT.set(turn)
        self.addCleanup(CURRENT.reset, token)
        with patch('tg_triage.ai_transport.read_key', return_value='fake'), \
             patch('tg_triage.thinking_transport.complete', side_effect=complete):
            result = CloudProvider(cfg).analyze_ingress(sources, [], images=images, read_context=history)
        self.assertIsNotNone(result)
        self.assertEqual(2, len(requests))
        self.assertEqual(2, turn.calls)
        for index, messages in enumerate(requests):
            self.assertLessEqual(request_text_bytes({'messages': messages}), 8000)
            user = next(item for item in messages if item['role'] == 'user')
            parts = user['content']
            packet = json.loads(next(part['text'] for part in parts if part['type'] == 'text'))
            self.assertEqual(list(range(604, 610)), packet['NEW_SOURCE_IDS'])
            retained = {row[0]: dict(zip(packet['source_columns'], row)) for row in packet['sources']}
            self.assertEqual(footer, retained[605]['text'])
            self.assertTrue(all(page in retained[605]['text'] for page in pages))
            self.assertEqual(bool(index), 'PROPOSED_ROUTES_FOR_REVIEW' in packet)
            image_ids = [row['message_id'] for row in packet['images_order']]
            self.assertEqual([604, 606, 608], image_ids)
            pixel_parts = [part for part in parts if part['type'] == 'image_url']
            self.assertEqual([expected_pixels[mid] for mid in image_ids],
                             [base64.b64decode(part['image_url']['url'].split(',', 1)[1]) for part in pixel_parts])
            for mid in image_ids:
                self.assertTrue(retained[mid]['has_image'])
                self.assertTrue(retained[mid]['pixels_available'])
            self.assertLessEqual(len(packet['READ_ONLY_CONTEXT']), len(history))
            if index and len(packet['READ_ONLY_CONTEXT']) < len(history):
                self.assertTrue(packet['read_context_truncated'])
