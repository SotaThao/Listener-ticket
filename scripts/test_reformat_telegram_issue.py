"""Offline regressions for manual existing-ticket repair, without external writes."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

loader = importlib.util.spec_from_file_location('reformatter', Path(__file__).with_name('reformat-telegram-issue.py'))
module = importlib.util.module_from_spec(loader)
loader.loader.exec_module(module)


def fixture():
    return dict(category='task', title='Thiết kế lại bảng Salon information & visibility cho chế độ Chat only',
                summary='Thiết kế lại bảng Salon information & visibility cho chế độ Chat only theo ảnh và caption.',
                sources=[dict(chat_id=-123, message_id=442, received_at='2026-10-15T22:49:00+07:00',
                              sender_name='Người gửi', text='Sau này mình cho chế độ nầy thì mình sẽ design đổi bảng nầy',
                              has_image=True)], images=[['Ảnh nguồn', 'https://example.invalid/442.jpg']],
                details=dict(work='Thiết kế lại bảng Salon information & visibility cho chế độ Chat only.',
                             done=['Có thiết kế mới cho bảng ở chế độ Chat only']))


class ReformatTests(unittest.TestCase):
    def test_context_uses_canonical_task_and_manual_provenance(self):
        issue = module.render(fixture())
        self.assertEqual([line for line in issue.body.splitlines() if line.startswith('### ')],
                         ['### 📝 Tóm tắt', '### 🛠 Việc cần làm',
                          '### 🖼 Minh họa / Evidence (đầu vào)', '### 🏁 Xong khi'])
        task_body = module.active().render('task.md', 'body', {'done': [], 'questions': []})
        dod_line = next(line for line in task_body.splitlines() if line.startswith('- [ ] '))
        self.assertIn(dod_line, issue.body)
        self.assertIn(fixture()['sources'][0]['text'], issue.body)
        self.assertIn('https://example.invalid/442.jpg', issue.body)
        self.assertIn('xác nhận thủ công', issue.body)
        self.assertNotIn('confidence', issue.body)

    def test_missing_details_cannot_fall_back_to_legacy_template(self):
        spec = fixture(); spec.pop('details')
        body = module.render(spec).body
        self.assertIn('### 🛠 Việc cần làm', body)
        self.assertIn('### 🏁 Xong khi', body)
        self.assertIn('Việc cần làm là gì?', body)

    def test_preview_does_not_contact_github(self):
        with patch.object(module, 'gh', side_effect=AssertionError('External call')):
            module.render(fixture())

    def test_source_scope_rejects_duplicates_or_other_chat(self):
        for mode in ('duplicate', 'other_chat'):
            spec = fixture(); other = dict(spec['sources'][0])
            if mode == 'other_chat': other.update(chat_id=-456, message_id=443)
            spec['sources'].append(other)
            with self.assertRaises(ValueError): module.render(spec)

    def test_patch_existing_only_and_preserve_unrelated_labels(self):
        issue = module.render(fixture()); original = '<!-- tg:-123:442 -->\nOld body'
        calls = []; current = dict(number=46, title='Old', body=original,
                                   labels=[dict(name='needs-human'), dict(name='owner-label')],
                                   html_url='https://github.com/owner/repo/issues/46')
        def request(args, payload=None):
            calls.append(args)
            if payload: current.update(payload, labels=[dict(name=x) for x in payload['labels']])
            return copy.deepcopy(current)
        result = module.update_existing('owner/repo', 46, issue, hashlib.sha256(original.encode()).hexdigest(), request)
        self.assertEqual(result['github_create_calls'], 0)
        self.assertEqual([a[2] for a in calls], ['GET', 'PATCH', 'GET'])
        self.assertTrue(all(a[0] == 'repos/owner/repo/issues/46' for a in calls))
        self.assertEqual({x['name'] for x in current['labels']}, {'owner-label', 'task', 'triage', 'source:telegram'})

    def test_changed_body_or_wrong_source_never_patch(self):
        issue = module.render(fixture())
        for body in ('<!-- tg:-123:443 -->\nOld', '<!-- tg:-123:442 -->\nChanged'):
            calls = []
            def request(args, payload=None):
                calls.append(args)
                return dict(number=46, title='Old', body=body, labels=[])
            with self.assertRaises(ValueError):
                module.update_existing('owner/repo', 46, issue, '0' * 64, request)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][2], 'GET')

    def test_already_rendered_issue_is_read_only(self):
        issue = module.render(fixture()); calls = []
        def request(args, payload=None):
            calls.append(args)
            return dict(number=46, title=issue.title, body=issue.body,
                        labels=[dict(name=x) for x in issue.labels], html_url='https://github.com/owner/repo/issues/46')
        module.update_existing('owner/repo', 46, issue, hashlib.sha256(issue.body.encode()).hexdigest(), request)
        self.assertEqual([a[2] for a in calls], ['GET', 'GET'])

    def test_wrong_number_or_pull_request_never_patch(self):
        issue = module.render(fixture())
        for metadata in (dict(number=47), dict(number=46, pull_request={})):
            calls = []
            def request(args, payload=None):
                calls.append(args)
                return dict(body=issue.body, **metadata)
            with self.assertRaises(ValueError):
                module.update_existing('owner/repo', 46, issue, hashlib.sha256(issue.body.encode()).hexdigest(), request)
            self.assertEqual([a[2] for a in calls], ['GET'])


if __name__ == '__main__':
    unittest.main()
