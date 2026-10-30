"""Blind mechanical acceptance for offered analysis choices; no vision-quality claims."""
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tg_triage.classifier import Classifier
from tg_triage.ingress_model import parse_ingress
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import FakeHTTP, NOW, config, message
from tests.test_analysis_reply_contract import AuditedGitHub, Router, group, wire_group
from tests.test_autonomous import route


ANSWER = ('Quan sát: có chi tiết hiển thị chưa rõ trong ảnh. '
          'Đây là các khả năng cần xác nhận, chưa phải kết luận nguyên nhân.')
OPTIONS = ['URL có thể tràn khỏi bong bóng tin nhắn',
           'Avatar có thể bị vùng chứa chat cắt mất một phần']


def offer(ids=(1,), *, options=None, answer=ANSWER):
    value = group(ids, answer=answer)
    value['options'] = list(OPTIONS if options is None else options)
    return value


def wire(options=OPTIONS, action='ANALYZE'):
    value = wire_group(action=action, answer=ANSWER if action == 'ANALYZE' else None)
    value['options'] = options
    value.pop('needs_choice', None)  # Explicit legacy producer shape for parser tests.
    return dict(groups=[value])


def producer_task_wire():
    payload = wire([])
    payload['groups'][0]['task'] = dict(title='Kiểm tra nội dung hiển thị',
        work='Đối chiếu hiện tượng với vùng chứa nội dung.', acceptance=['Nội dung hiển thị đầy đủ'])
    return payload


class AnalysisChoicesContractTests(TestCase):
    def build(self, model=None, *, path=':memory:', sender=None, mode='live'):
        store = Store(path)
        self.addCleanup(store.close)
        return Pipeline(replace(config(mode, window=2), autonomous_enabled=True), store,
                        model or Router([offer()]), AuditedGitHub(), notice_sender=sender)

    def begin_offer(self, *, path=':memory:', options=None):
        sent = []
        p = self.build(Router([offer(options=options)]), path=path,
                       sender=lambda c, m, t: sent.append((c, m, t)) or 901)
        result = p.process(message(1, text='Phân tích ảnh và đưa vài khả năng để tôi chọn',
                                   mentions_listener=False))
        self.assertEqual('chat', result.decision)
        self.assertEqual(1, len(sent))
        return p, sent

    def assert_readonly(self, p):
        self.assertEqual([], p.github.calls)
        self.assertEqual([], p.github.mutations)

    def test_parser_accepts_legacy_omitted_or_empty_and_two_three_complete_choices(self):
        omitted = wire()
        del omitted['groups'][0]['options']
        self.assertIsNotNone(parse_ingress(json.dumps(omitted)))
        for options in ([], OPTIONS, OPTIONS + ['Khả năng khác dựa trên bố cục ảnh'], ['x' * 180, 'y' * 180]):
            with self.subTest(options=options):
                parsed = parse_ingress(json.dumps(wire(options), ensure_ascii=False))
                self.assertIsNotNone(parsed)
                self.assertEqual(options, list(parsed[0].get('options', [])))

    def test_parser_rejects_bad_choice_types_counts_bounds_blanks_duplicates(self):
        for options in (None, '', {}, 1, [OPTIONS[0]], OPTIONS * 2,
                        ['', OPTIONS[1]], [' \t\n', OPTIONS[1]], ['\u00a0\u2003', OPTIONS[1]],
                        [7, OPTIONS[1]], [False, OPTIONS[1]], ['x' * 181, OPTIONS[1]],
                        [OPTIONS[0], OPTIONS[0]], [OPTIONS[0], ' ' + OPTIONS[0] + ' ']):
            with self.subTest(options=repr(options)[:100]):
                self.assertIsNone(parse_ingress(json.dumps(wire(options), ensure_ascii=False)))

    def test_non_analysis_actions_accept_omitted_empty_but_reject_nonempty_options(self):
        for action in ('NEW', 'CONTINUE', 'CLARIFY', 'CHAT'):
            for options in ([], OPTIONS):
                with self.subTest(action=action, options=options):
                    parsed = parse_ingress(json.dumps(wire(options, action)))
                    self.assertEqual(options == [], parsed is not None)
            payload = wire([], action)
            del payload['groups'][0]['options']
            self.assertIsNotNone(parse_ingress(json.dumps(payload)))

    def test_current_provider_schema_requires_task_but_parser_keeps_legacy_choices(self):
        payload = producer_task_wire()
        http = FakeHTTP({'message': {'content': json.dumps(payload, ensure_ascii=False)}})
        model = Classifier(config().ollama, opener=http)
        model.analyze_ingress([message(10)], [])
        self.assertEqual(1, len(http.calls))
        schema = json.loads(http.calls[0][0].data)['format']['properties']['groups']['items']
        required = set(schema.get('required', []))
        for clause in [schema] + list(schema.get('allOf', [])):
            condition = clause.get('if', {}).get('properties', {}).get('action', {})
            if condition.get('const') == 'ANALYZE' or condition.get('enum') == ['ANALYZE']:
                required.update(clause.get('then', {}).get('required', []))
        self.assertIn('task', required)
        self.assertIsNotNone(parse_ingress(json.dumps(wire(), ensure_ascii=False)))

    def test_explicit_needs_choice_boolean_must_match_options_cardinality(self):
        for state, options, accepted in ((True, OPTIONS, True),
                (True, OPTIONS + ['Một khả năng khác cần đối chiếu'], True),
                (True, [], False), (False, OPTIONS, False), (False, [], True)):
            payload = wire(options)
            payload['groups'][0]['needs_choice'] = state
            with self.subTest(state=state, count=len(options)):
                parsed = parse_ingress(json.dumps(payload, ensure_ascii=False))
                self.assertEqual(accepted, parsed is not None)
                if accepted:
                    self.assertIs(state, parsed[0]['needs_choice'])

    def test_present_choice_state_rejects_boolean_like_values_without_legacy_coercion(self):
        for state in (1, 0, 'true', 'false', None, [], {}):
            payload = wire(OPTIONS)
            payload['groups'][0]['needs_choice'] = state
            with self.subTest(state=state):
                self.assertIsNone(parse_ingress(json.dumps(payload, ensure_ascii=False)))

    def test_non_analysis_cannot_request_explicit_choice_state(self):
        for action in ('NEW', 'CONTINUE', 'CLARIFY', 'CHAT'):
            for state, accepted in ((True, False), (False, True)):
                payload = wire([], action)
                payload['groups'][0]['needs_choice'] = state
                with self.subTest(action=action, state=state):
                    self.assertEqual(accepted, parse_ingress(json.dumps(payload)) is not None)

    def test_host_enforces_present_choice_state_and_keeps_omitted_legacy_behavior(self):
        for state, options, accepted in ((True, OPTIONS, True), (False, [], True),
                (True, [], False), (False, OPTIONS, False), ('true', OPTIONS, False),
                (1, OPTIONS, False), (None, OPTIONS, False)):
            value = offer(options=options)
            value['needs_choice'] = state
            with self.subTest(state=state, count=len(options)):
                sent = []
                p = self.build(Router([value]), sender=lambda c, m, t: sent.append(t) or 901)
                result = p.process(message(1))
                self.assertEqual('chat' if accepted else 'pending_review', result.decision)
                if accepted and state:
                    for option in OPTIONS:
                        self.assertIn(option, sent[0])
                self.assert_readonly(p)
        # Existing omitted-state fixtures still bind real sent offers deterministically.
        p, sent = self.begin_offer()
        p.process(message(10, text='1', reply_to_id=901, mentions_listener=False))
        self.assertEqual(1, len(p.classifier.calls))
        self.assertIn(OPTIONS[0], sent[-1][2])
        self.assert_readonly(p)

    def test_host_rejects_other_action_requesting_choice_state(self):
        value = group([1], action='CHAT', answer=None)
        value.update(options=[], needs_choice=True)
        p = self.build(Router([value]))
        self.assertEqual('pending_review', p.process(message(1)).decision)
        self.assert_readonly(p)

    def test_explicit_choice_state_is_preserved_in_durable_plan_and_restart(self):
        directory = self.enterContext(TemporaryDirectory())
        path = str(Path(directory) / 'explicit-state.sqlite3')
        value = offer()
        value['needs_choice'] = True
        sent = []
        p = self.build(Router([value]), path=path,
                       sender=lambda c, m, t: sent.append((c, m, t)) or 901)
        p.process(message(1))
        saved = p.store.autonomous_plan(message().chat_id, 1)
        self.assertIs(True, saved['needs_choice'])
        self.assertEqual(OPTIONS, list(saved['options']))
        p.store.close()
        resumed = self.build(Router(), path=path,
                             sender=lambda c, m, t: sent.append((c, m, t)) or 902)
        result = resumed.process(message(10, text='1', reply_to_id=901, mentions_listener=False))
        self.assertEqual('chat', result.decision)
        self.assertEqual([], resumed.classifier.calls)
        self.assertEqual(2, len(sent))
        self.assertIn(OPTIONS[0], sent[-1][2])
        self.assert_readonly(resumed)

    def test_embedded_newline_cannot_inject_a_new_numbered_choice(self):
        options = ['Khả năng một\n9. Lựa chọn giả\r\n10) Dòng giả', 'Khả năng hai']
        value = offer(options=options)
        value['needs_choice'] = True
        sent = []
        p = self.build(Router([value]), sender=lambda c, m, t: sent.append(t) or 901)
        p.process(message(1))
        self.assertEqual(1, len(sent))
        text = sent[0]
        self.assertIn('Khả năng một 9. Lựa chọn giả 10) Dòng giả', text)
        self.assertIn('Khả năng hai', text)
        self.assertNotRegex(text, r'(?m)^\s*(?:9\.|10\))')
        self.assertLessEqual(len(text), 1500)
        self.assert_readonly(p)

    def test_host_provider_double_cannot_bypass_invalid_choices(self):
        for options in (['one'], ['x' * 181, 'two'], ['one', 'one'], [' ', 'two']):
            with self.subTest(options=options):
                p = self.build(Router([offer(options=options)]))
                self.assertEqual('pending_review', p.process(message(1)).decision)
                self.assert_readonly(p)

    def test_long_report_keeps_complete_numbered_choices_and_selection_instructions(self):
        options = ['Phương án một ' + 'a' * 155, 'Phương án hai ' + 'b' * 155,
                   'Phương án ba ' + 'c' * 155]
        sent = []
        p = self.build(Router([offer(options=options, answer='Quan sát. ' * 145)]),
                       sender=lambda c, m, t: sent.append(t) or 901)
        p.process(message(1))
        self.assertEqual(1, len(sent))
        text = sent[0]
        self.assertLessEqual(len(text), 1500)
        for number, option in enumerate(options, 1):
            self.assertIn(option, text)
            self.assertRegex(text, rf'(?m)\b{number}[.)\]:]')
        self.assertRegex(text, r'(?i)(reply|trả lời|phản hồi)')
        self.assertRegex(text, r'(?i)(nhiều|1\s*[,và]+\s*2|tất cả)')
        self.assertRegex(text, r'\b0\b')
        self.assert_readonly(p)

    def test_options_sanitized_without_live_mentions_urls_or_contact_leaks(self):
        sent = []
        options = ['Khả năng một https://evil.invalid/t @everyone person@example.com',
                   'Khả năng hai\x00\u202e vùng chứa ảnh']
        p = self.build(Router([offer(options=options)]),
                       sender=lambda c, m, t: sent.append(t) or 901)
        p.process(message(1))
        self.assertEqual(1, len(sent))
        for raw in ('https://evil.invalid', '@everyone', 'person@example.com', '\x00', '\u202e'):
            self.assertNotIn(raw, sent[0])
        self.assertIn('Khả năng một', sent[0])
        self.assertIn('Khả năng hai', sent[0])
        self.assertLessEqual(len(sent[0]), 1500)
        self.assert_readonly(p)

    def test_single_selection_binds_sent_notice_or_original_without_model_or_mutation(self):
        for target in (1, 901):
            with self.subTest(target=target):
                p, sent = self.begin_offer()
                prior = dict(p.store.get(message().chat_id, 1))
                result = p.process(message(10, text='1', reply_to_id=target,
                                           received_at=NOW + timedelta(minutes=1), mentions_listener=False))
                self.assertEqual('chat', result.decision)
                self.assertEqual(1, len(p.classifier.calls))
                self.assertEqual(2, len(sent))
                self.assertEqual(10, sent[-1][1])
                self.assertIn(OPTIONS[0], sent[-1][2])
                self.assertEqual(prior, dict(p.store.get(message().chat_id, 1)))
                self.assertNotEqual(prior['case_key'], p.store.get(message().chat_id, 10)['case_key'])
                self.assert_readonly(p)

    def test_multi_and_all_selections_acknowledge_all_selected_options_readonly(self):
        for choice in ('1,2', 'tất cả'):
            with self.subTest(choice=choice):
                p, sent = self.begin_offer()
                result = p.process(message(10, text=choice, reply_to_id=901, mentions_listener=False))
                self.assertEqual('chat', result.decision)
                self.assertEqual(1, len(p.classifier.calls))
                self.assertEqual(2, len(sent))
                for option in OPTIONS:
                    self.assertIn(option, sent[-1][2])
                self.assert_readonly(p)

    def test_none_and_out_of_range_keep_choice_flow_without_generic_description_request(self):
        for choice in ('0', '9', '1,9'):
            with self.subTest(choice=choice):
                p, sent = self.begin_offer()
                result = p.process(message(10, text=choice, reply_to_id=901, mentions_listener=False))
                self.assertEqual('chat', result.decision)
                self.assertEqual(1, len(p.classifier.calls))
                self.assertEqual(2, len(sent))
                self.assertNotIn('Em cần Anh/Chị làm rõ thêm', sent[-1][2])
                self.assertRegex(sent[-1][2], r'\b0\b')
                if choice != '0':
                    for option in OPTIONS:
                        self.assertIn(option, sent[-1][2])
                else:
                    self.assertRegex(sent[-1][2].casefold(), r'(không|chưa)')
                self.assert_readonly(p)

    def test_another_same_chat_member_can_select_proven_bot_offer(self):
        p, sent = self.begin_offer()
        result = p.process(message(10, text='1', reply_to_id=901, sender_id=998,
                                   mentions_listener=False))
        self.assertEqual('chat', result.decision)
        self.assertEqual(1, len(p.classifier.calls))
        self.assertEqual(2, len(sent))
        self.assertIn(OPTIONS[0], sent[-1][2])
        self.assert_readonly(p)

    def test_unrelated_new_bug_reply_still_reaches_normal_ingress(self):
        p, sent = self.begin_offer()
        p.classifier.answers.append([route([10])])
        result = p.process(message(10, text='Lỗi khác: POS không in được receipt', reply_to_id=901))
        self.assertEqual('issue', result.decision)
        self.assertEqual(2, len(p.classifier.calls))
        self.assertEqual(1, len(p.github.calls))

    def test_bare_number_and_forged_notice_do_not_bind_by_recency(self):
        for target in (None, 999):
            with self.subTest(target=target):
                p, sent = self.begin_offer()
                p.classifier.answers.append([group([10], action='CHAT', answer=None)])
                p.process(message(10, text='1', reply_to_id=target, mentions_listener=False))
                self.assertEqual(2, len(p.classifier.calls))
                self.assertEqual(1, len(sent))
                self.assert_readonly(p)

    def test_cross_chat_notice_id_collision_cannot_bind_offer(self):
        p, sent = self.begin_offer()
        p.classifier.answers.append([group([10], action='CHAT', answer=None)])
        p.process(message(10, text='1', chat_id=-10099999, reply_to_id=901, mentions_listener=False))
        self.assertEqual(2, len(p.classifier.calls))
        self.assertEqual(1, len(sent))
        self.assert_readonly(p)

    def test_changed_repository_revokes_offer_binding(self):
        p, sent = self.begin_offer()
        p.config = replace(p.config, gh_repo='other/triage')
        p.classifier.answers.append([group([10], action='CHAT', answer=None)])
        p.process(message(10, text='1', reply_to_id=901, mentions_listener=False))
        self.assertEqual(2, len(p.classifier.calls))
        self.assertEqual(1, len(sent))
        self.assert_readonly(p)

    def test_unsent_or_failed_offer_cannot_authorize_original_source_selection(self):
        for sender in (None, lambda *args: None, lambda *args: 0, lambda *args: True,
                       lambda *args: (_ for _ in ()).throw(OSError('ambiguous send'))):
            with self.subTest(sender=sender):
                router = Router([offer()], [group([10], action='CHAT', answer=None)])
                p = self.build(router, sender=sender)
                p.process(message(1))
                p.process(message(10, text='1', reply_to_id=1, mentions_listener=False))
                self.assertEqual(2, len(router.calls))
                self.assert_readonly(p)

    def test_dry_run_never_sends_or_authorizes_choice_reply(self):
        sent = []
        p = self.build(Router([offer()], [group([10], action='CHAT', answer=None)]), mode='dry-run',
                       sender=lambda *args: sent.append(args) or 901)
        p.process(message(1))
        p.process(message(10, text='1', reply_to_id=1))
        self.assertEqual([], sent)
        self.assertEqual(2, len(p.classifier.calls))
        self.assert_readonly(p)

    def test_restart_preserves_offer_binding_and_duplicate_selection_ack_not_resent(self):
        directory = self.enterContext(TemporaryDirectory())
        path = str(Path(directory) / 'choices.sqlite3')
        p, sent = self.begin_offer(path=path)
        p.store.close()
        resumed = self.build(Router(), path=path,
                             sender=lambda c, m, t: sent.append((c, m, t)) or 902)
        reply = message(10, text='1', reply_to_id=901, mentions_listener=False)
        resumed.process(reply)
        resumed.process(reply)
        resumed.close_due_cases()
        self.assertEqual([], resumed.classifier.calls)
        self.assertEqual(2, len(sent))
        self.assertIn(OPTIONS[0], sent[-1][2])
        self.assert_readonly(resumed)

    def test_crash_after_selection_ack_reservation_never_replays_send(self):
        directory = self.enterContext(TemporaryDirectory())
        path = str(Path(directory) / 'crash.sqlite3')
        p, sent = self.begin_offer(path=path)
        def crash(c, m, t):
            sent.append((c, m, t))
            raise SystemExit('after acknowledgement reservation')
        p.notice_sender = crash
        with self.assertRaises(SystemExit):
            p.process(message(10, text='1', reply_to_id=901, mentions_listener=False))
        p.store.close()
        resumed = self.build(Router(), path=path,
                             sender=lambda c, m, t: sent.append((c, m, t)) or 902)
        resumed.process(message(10, text='1', reply_to_id=901, mentions_listener=False))
        resumed.close_due_cases()
        self.assertEqual(2, len(sent))
        self.assertEqual([], resumed.classifier.calls)
        self.assert_readonly(resumed)

    def test_crashed_offer_reservation_is_not_resent_or_authority_for_selection(self):
        directory = self.enterContext(TemporaryDirectory())
        path = str(Path(directory) / 'offer-crash.sqlite3')
        attempts = []
        def crash(c, m, t):
            attempts.append((c, m, t))
            raise SystemExit('offer transport ambiguous after reservation')
        p = self.build(Router([offer()]), path=path, sender=crash)
        with self.assertRaises(SystemExit):
            p.process(message(1))
        p.store.close()
        model = Router([group([10], action='CHAT', answer=None)])
        resumed = self.build(model, path=path,
                             sender=lambda c, m, t: attempts.append((c, m, t)) or 902)
        resumed.process(message(1))
        resumed.process(message(10, text='1', reply_to_id=1, mentions_listener=False))
        self.assertEqual(1, len(attempts))
        self.assertEqual(1, len(model.calls))
        self.assertEqual([10], [m.message_id for m in model.calls[0][0]])
        self.assert_readonly(resumed)

    def test_mixed_backlog_selection_consumes_only_reply_and_preserves_new_bug(self):
        p, sent = self.begin_offer()
        p.classifier.answers.append([route([11])])
        independent = message(11, text='POS không in được receipt', mentions_listener=False)
        p.store.enqueue(independent)
        p.process(message(10, text='1', reply_to_id=901, mentions_listener=False))
        # The same sweep or the next process may drain the independent queued request.
        if not p.github.calls:
            p.process(independent)
        self.assertEqual(1, len(p.github.calls))
        self.assertEqual(2, len(p.classifier.calls))
        self.assertEqual([11], [m.message_id for m in p.classifier.calls[-1][0]])
        self.assertNotEqual(p.store.get(message().chat_id, 10)['case_key'],
                            p.store.get(message().chat_id, 11)['case_key'])

    def test_fresh_provider_requires_task_while_legacy_parser_remains_compatible(self):
        legacy = wire()
        self.assertIsNotNone(parse_ingress(json.dumps(legacy, ensure_ascii=False)))
        rejected = {'message': {'content': json.dumps(legacy, ensure_ascii=False)}}
        http = FakeHTTP(rejected, rejected)
        model = Classifier(config().ollama, opener=http)
        self.assertIsNone(model.analyze_ingress([message(10)], []))
        self.assertGreaterEqual(len(http.calls), 1)
        self.assertLessEqual(len(http.calls), 2)
        explicit = producer_task_wire()
        http = FakeHTTP({'message': {'content': json.dumps(explicit, ensure_ascii=False)}})
        parsed = Classifier(config().ollama, opener=http).analyze_ingress([message(10)], [])
        self.assertIsNotNone(parsed)
        self.assertIn('task', parsed[0])
        self.assertEqual([], list(parsed[0].get('options', [])))
        self.assertEqual(1, len(http.calls))

    def test_fresh_other_action_still_allows_omitted_analysis_choice_state(self):
        payload = wire([], action='CHAT')
        http = FakeHTTP({'message': {'content': json.dumps(payload, ensure_ascii=False)}})
        parsed = Classifier(config().ollama, opener=http).analyze_ingress([message(10)], [])
        self.assertIsNotNone(parsed)
        self.assertEqual('CHAT', parsed[0]['action'])
        self.assertEqual(1, len(http.calls))

    def test_rejected_fresh_choice_state_is_not_published_as_a_canned_offer(self):
        rejected = {'message': {'content': json.dumps(wire(), ensure_ascii=False)}}
        http = FakeHTTP(rejected, rejected)
        sent = []
        p = self.build(Classifier(config().ollama, opener=http),
                       sender=lambda c, m, t: sent.append(t) or 901)
        result = p.process(message(10, text='Phân tích và cho các khả năng để chọn'))
        self.assertEqual('pending_review', result.decision)
        self.assertLessEqual(len(http.calls), 2)
        self.assertLessEqual(len(sent), 1)
        for option in OPTIONS:
            self.assertFalse(any(option in text for text in sent))
        plan = p.store.autonomous_plan(message().chat_id, 10)
        self.assertNotEqual('ANALYZE', plan['action'])
        self.assert_readonly(p)

    def test_legacy_choices_roundtrip_has_no_authority_fields(self):
        payload = wire()
        payload['groups'][0]['needs_choice'] = True
        parsed = parse_ingress(json.dumps(payload, ensure_ascii=False))
        self.assertIsNotNone(parsed)
        self.assertEqual(OPTIONS, list(parsed[0]['options']))
        self.assertIsNone(parsed[0]['candidate_id'])
        self.assertEqual([10], parsed[0]['message_ids'])
