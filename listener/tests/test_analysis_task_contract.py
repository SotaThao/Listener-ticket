"""Blind structured-task mechanics, not evidence of model visual understanding."""
import copy
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tg_triage.classifier import Classifier
from tg_triage.ingress_model import parse_ingress
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import FakeHTTP, config, message
from tests.test_analysis_reply_contract import AuditedGitHub, Router, group, wire_group
from tests.test_analysis_choices_contract import OPTIONS, offer
from tests.test_autonomous import route


TASK = dict(title='Kiểm tra phần hiển thị chưa đúng trong khung chat',
            work='Điều chỉnh vùng chứa nội dung bị cắt; kiểm tra mã trước khi kết luận nguyên nhân.',
            acceptance=['Nội dung hiển thị đầy đủ ở các độ rộng chat được kiểm tra',
                        'Không che hoặc cắt phần tử liền kề'])


def wire_task(task=TASK, *, ids=(10,), answer=None):
    value = wire_group(answer=answer)
    value['message_ids'] = list(ids)
    value['analysis']['key_message_ids'] = list(ids)
    value['task'] = copy.deepcopy(task)
    return dict(groups=[value])


def task_group(ids=(1,), task=TASK, **extra):
    value = group(ids, answer=None)
    value.update(task=copy.deepcopy(task), **extra)
    return value


class AnalysisTaskContractTests(TestCase):
    def build(self, model, *, path=':memory:', mode='live', sender=None):
        store = Store(path)
        self.addCleanup(store.close)
        return Pipeline(replace(config(mode, window=2), autonomous_enabled=True),
                        store, model, AuditedGitHub(), notice_sender=sender)

    def model(self, payload):
        response = dict(message=dict(content=json.dumps(payload, ensure_ascii=True)))
        http = FakeHTTP(response, response)
        return Classifier(config().ollama, opener=http), http

    def assert_readonly(self, p):
        self.assertEqual([], p.github.calls)
        self.assertEqual([], p.github.mutations)

    def assert_complete_task(self, text, task=TASK):
        lines = text.splitlines()
        self.assertEqual(3, len(lines))
        self.assertEqual('Việc: ' + task['title'], lines[0])
        self.assertEqual('Thực hiện: ' + task['work'], lines[1])
        self.assertTrue(lines[2].startswith('Đạt: '))
        for criterion in task['acceptance']:
            self.assertIn(criterion, lines[2])
        self.assertLessEqual(len(text), 500)

    def test_default_parser_accepts_structured_task_without_legacy_answer(self):
        parsed = parse_ingress(json.dumps(wire_task(), ensure_ascii=True))
        self.assertIsNotNone(parsed)
        self.assertEqual(TASK, parsed[0]['task'])

    def test_provider_accepts_task_with_omitted_or_empty_answer(self):
        for answer in (None, ''):
            with self.subTest(answer=answer):
                model, http = self.model(wire_task(answer=answer))
                parsed = model.analyze_ingress([message(10)], [])
                self.assertIsNotNone(parsed)
                self.assertEqual(TASK, parsed[0]['task'])
                self.assertEqual(1, len(http.calls))

    def test_valid_task_publishes_three_complete_lines_without_legacy_prose(self):
        payload = wire_task(answer='OLD_REPORT_MUST_NOT_BE_RENDERED')
        model, http = self.model(payload)
        sent = []
        p = self.build(model, sender=lambda c, m, t: sent.append((m, t)) or 901)
        result = p.process(message(10, text='Phân tích ảnh và tự xác định việc cần làm'))
        self.assertEqual('chat', result.decision)
        self.assertEqual(1, len(http.calls))
        self.assertEqual(1, len(sent))
        self.assertEqual(10, sent[0][0])
        self.assert_complete_task(sent[0][1])
        self.assertNotIn('OLD_REPORT_MUST_NOT_BE_RENDERED', sent[0][1])
        self.assert_readonly(p)

    def test_ignored_legacy_answer_still_has_type_utf8_and_length_validation(self):
        for answer in (None, 7, [], {}, 'x' * 1501, '\ud800'):
            payload = wire_task()
            payload['groups'][0]['answer'] = answer
            with self.subTest(answer=repr(answer)[:40]):
                self.assertIsNone(parse_ingress(json.dumps(payload, ensure_ascii=True)))

    def test_maximum_valid_field_lengths_are_delivered_intact(self):
        task = dict(title='T' * 90, work='W' * 160, acceptance=['A' * 90, 'B' * 90])
        sent = []
        p = self.build(Router([task_group(task=task)]), sender=lambda c, m, t: sent.append(t) or 901)
        self.assertEqual('chat', p.process(message(1)).decision)
        self.assertEqual(1, len(sent))
        self.assert_complete_task(sent[0], task)

    def test_parser_rejects_missing_empty_wrong_type_and_unknown_task_fields(self):
        for task in (None, '', [], {}, dict(title='x', work='y'),
                     dict(title='x', work='y', acceptance=['z'], assignee='invented')):
            with self.subTest(task=task):
                self.assertIsNone(parse_ingress(json.dumps(wire_task(task), ensure_ascii=True)))

    def test_present_bad_task_cannot_fall_back_to_valid_legacy_answer(self):
        for task in (None, {}, dict(TASK, acceptance=[])):
            with self.subTest(task=task):
                self.assertIsNone(parse_ingress(json.dumps(wire_task(task, answer='Valid old report'))))
                value = task_group(task=task, answer='Valid old report')
                p = self.build(Router([value]))
                self.assertEqual('pending_review', p.process(message(1)).decision)
                self.assert_readonly(p)

    def test_parser_rejects_task_field_bounds_types_blanks_and_bad_utf8(self):
        changes = [('title', ''), ('title', 'x' * 91), ('title', 1), ('title', '\ud800'),
                   ('work', 'x' * 161), ('work', False), ('work', '\u00a0\u2003'),
                   ('acceptance', []), ('acceptance', ['x', 'y', 'z']), ('acceptance', 'one'),
                   ('acceptance', [' ']), ('acceptance', [None]), ('acceptance', ['x' * 91]),
                   ('acceptance', ['same', 'same']), ('acceptance', ['same', ' same ']),
                   ('acceptance', ['\ud800'])]
        for key, value in changes:
            task = dict(TASK, **{key: value})
            with self.subTest(key=key, value=repr(value)):
                self.assertIsNone(parse_ingress(json.dumps(wire_task(task), ensure_ascii=True)))

    def test_task_is_forbidden_on_every_other_action(self):
        for action in ('NEW', 'CONTINUE', 'CLARIFY', 'CHAT'):
            payload = wire_task()
            payload['groups'][0]['action'] = action
            with self.subTest(action=action):
                self.assertIsNone(parse_ingress(json.dumps(payload)))

    def test_structured_task_cannot_also_offer_hypothesis_selection(self):
        for extra in (dict(options=OPTIONS), dict(needs_choice=True),
                      dict(options=OPTIONS, needs_choice=True)):
            payload = wire_task()
            payload['groups'][0].update(extra)
            with self.subTest(extra=extra):
                self.assertIsNone(parse_ingress(json.dumps(payload)))
        payload = wire_task()
        payload['groups'][0].update(options=[], needs_choice=False)
        self.assertIsNotNone(parse_ingress(json.dumps(payload)))

    def test_fresh_legacy_analysis_is_rejected_but_default_parser_accepts_it(self):
        payload = dict(groups=[wire_group()])
        payload['groups'][0].update(needs_choice=True, options=OPTIONS)
        self.assertIsNotNone(parse_ingress(json.dumps(payload)))
        model, http = self.model(payload)
        self.assertIsNone(model.analyze_ingress([message(10)], []))
        self.assertGreaterEqual(len(http.calls), 1)
        self.assertLessEqual(len(http.calls), 2)

    def test_invalid_fresh_task_fails_closed_without_publishing_a_truncated_task(self):
        model, http = self.model(wire_task(dict(TASK, work='W' * 161)))
        sent = []
        p = self.build(model, sender=lambda c, m, t: sent.append(t) or 901)
        self.assertEqual('pending_review', p.process(message(10)).decision)
        self.assertLessEqual(len(http.calls), 2)
        self.assertFalse(any(text.startswith('Việc: ') for text in sent))
        self.assert_readonly(p)

    def test_newlines_are_collapsed_and_cannot_inject_fourth_task_line(self):
        task = dict(title='Nhiệm vụ\nĐạt: giả', work='Kiểm tra\r\nThực hiện: giả',
                    acceptance=['Tiêu chí một\nViệc: giả', 'Tiêu chí hai'])
        expected = dict(title='Nhiệm vụ Đạt: giả', work='Kiểm tra Thực hiện: giả',
                        acceptance=['Tiêu chí một Việc: giả', 'Tiêu chí hai'])
        sent = []
        p = self.build(Router([task_group(task=task)]), sender=lambda c, m, t: sent.append(t) or 901)
        p.process(message(1))
        self.assertEqual(1, len(sent))
        self.assert_complete_task(sent[0], expected)
        self.assert_readonly(p)

    def test_sanitized_task_drops_contact_links_mentions_and_controls(self):
        task = dict(title='Kiểm tra @everyone', work='Xem https://evil.invalid/path person@example.com',
                    acceptance=['Không còn lỗi\x00\u202e'])
        sent = []
        p = self.build(Router([task_group(task=task)]), sender=lambda c, m, t: sent.append(t) or 901)
        p.process(message(1))
        self.assertEqual(1, len(sent))
        for raw in ('@everyone', 'https://evil.invalid', 'person@example.com', '\x00', '\u202e'):
            self.assertNotIn(raw, sent[0])
        self.assertEqual(3, len(sent[0].splitlines()))
        self.assertLessEqual(len(sent[0]), 500)
        self.assert_readonly(p)

    def test_sanitation_cannot_merge_distinct_acceptance_checks_or_overflow_caps(self):
        for task in (dict(TASK, acceptance=['Cùng tiêu chí\x00', 'Cùng tiêu chí\u202e']),
                     dict(TASK, title='a' * 77 + ' https://x.co')):
            with self.subTest(task=task):
                self.assertIsNone(parse_ingress(json.dumps(wire_task(task), ensure_ascii=True)))

    def test_task_evidence_ids_still_reject_foreign_sources(self):
        payload = wire_task()
        payload['groups'][0]['analysis']['key_message_ids'] = [999]
        model, http = self.model(payload)
        p = self.build(model)
        self.assertEqual('pending_review', p.process(message(10)).decision)
        self.assert_readonly(p)

    def test_task_notice_is_not_an_implicit_numeric_selection_offer(self):
        sent = []
        router = Router([task_group()], [group([10], action='CHAT', answer=None)])
        p = self.build(router, sender=lambda c, m, t: sent.append(t) or 901)
        p.process(message(1))
        p.process(message(10, text='1', reply_to_id=901, mentions_listener=False))
        self.assertEqual(2, len(router.calls))
        self.assertEqual(1, len(sent))
        self.assert_readonly(p)

    def test_legacy_sent_choice_offer_selection_stays_compatible(self):
        sent = []
        router = Router([offer()])
        p = self.build(router, sender=lambda c, m, t: sent.append(t) or 901)
        p.process(message(1))
        p.process(message(10, text='1', reply_to_id=901, mentions_listener=False))
        self.assertEqual(1, len(router.calls))
        self.assertEqual(2, len(sent))
        self.assertIn(OPTIONS[0], sent[-1])
        self.assert_readonly(p)

    def test_task_plan_and_notice_survive_restart_without_resend(self):
        directory = self.enterContext(TemporaryDirectory())
        path = str(Path(directory) / 'task.sqlite3')
        sent = []
        p = self.build(Router([task_group()]), path=path,
                       sender=lambda c, m, t: sent.append(t) or 901)
        p.process(message(1))
        self.assertEqual(TASK, p.store.autonomous_plan(message().chat_id, 1)['task'])
        p.store.close()
        resumed = self.build(Router(), path=path, sender=lambda c, m, t: sent.append(t) or 902)
        resumed.process(message(1))
        resumed.close_due_cases()
        self.assertEqual(1, len(sent))
        self.assertEqual([], resumed.classifier.calls)
        self.assert_readonly(resumed)

    def test_cached_task_after_case_creation_recovers_complete_notice_without_model(self):
        directory = self.enterContext(TemporaryDirectory())
        path = str(Path(directory) / 'task-case.sqlite3')
        p = self.build(Router([task_group()]), path=path)
        create_case = p.store.autonomy_case
        def crash_after_case(*args, **kwargs):
            create_case(*args, **kwargs)
            raise SystemExit('case saved before task delivery')
        p.store.autonomy_case = crash_after_case
        with self.assertRaises(SystemExit):
            p.process(message(1))
        p.store.close()
        sent = []
        resumed = self.build(Router(), path=path, sender=lambda c, m, t: sent.append(t) or 902)
        results = resumed.close_due_cases()
        self.assertEqual(1, len(results))
        self.assertEqual('chat', results[0].decision)
        self.assertEqual(1, len(sent))
        self.assert_complete_task(sent[0])
        self.assertEqual([], resumed.classifier.calls)
        self.assertEqual([], resumed.close_due_cases())
        self.assert_readonly(resumed)

    def test_task_reservation_crash_does_not_replay_transport(self):
        directory = self.enterContext(TemporaryDirectory())
        path = str(Path(directory) / 'task-crash.sqlite3')
        attempts = []
        def crash(*args):
            attempts.append(args)
            raise SystemExit('after task notice reservation')
        p = self.build(Router([task_group()]), path=path, sender=crash)
        with self.assertRaises(SystemExit):
            p.process(message(1))
        p.store.close()
        resumed = self.build(Router(), path=path, sender=lambda *args: attempts.append(args) or 902)
        resumed.process(message(1))
        resumed.close_due_cases()
        self.assertEqual(1, len(attempts))
        self.assertEqual([], resumed.classifier.calls)
        self.assert_readonly(resumed)

    def test_dry_run_never_sends_structured_task(self):
        sent = []
        p = self.build(Router([task_group()]), mode='dry-run', sender=lambda *args: sent.append(args) or 901)
        p.process(message(1))
        self.assertEqual([], sent)
        self.assert_readonly(p)

    def test_mixed_task_and_independent_new_bug_keep_disjoint_sources(self):
        sent = []
        router = Router([task_group((1,)), route([2])])
        p = self.build(router, sender=lambda c, m, t: sent.append((m, t)) or 900 + m)
        p.store.enqueue(message(2, text='POS không in được receipt', mentions_listener=False))
        p.process(message(1, text='Phân tích ảnh để xác định công việc', mentions_listener=False))
        self.assertEqual(1, len(p.github.calls))
        self.assertNotEqual(p.store.get(message().chat_id, 1)['case_key'],
                            p.store.get(message().chat_id, 2)['case_key'])
        task_notices = [text for mid, text in sent if mid == 1]
        self.assertEqual(1, len(task_notices))
        self.assert_complete_task(task_notices[0])
