import json
import unittest
from pathlib import Path

from tg_triage.issue_format import format_issue, source_links
from tests.helpers import classification, message
from tests.test_template_golden import outputs


class SourceLinksTests(unittest.TestCase):
    def test_ipv6_brackets_only_remain_in_authority(self):
        lines = source_links([message(text='https://[::1]:8080/p[x]?q=[y]#f[z]')])
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith('(https://[::1]:8080/p%5Bx%5D?q=%5By%5D#f%5Bz%5D)'))

    def test_idna_and_punycode_hosts_deduplicate(self):
        lines = source_links([message(text='https://bücher.example/p https://xn--bcher-kva.example/p')])
        self.assertEqual(lines, ['- 🔗 [xn--bcher-kva.example/p](https://xn--bcher-kva.example/p)'])

    def test_issue_82(self):
        first = message(843, text='https://bitcoinnailbar.com/#gallery @SotaThao  sao những hình Nail trước đó em không mang qua bên nầy', chat_id=-5264814171)
        second = message(844, text='Đã nhận', chat_id=-5264814171)
        body = format_issue(first, classification(type='task'), 'model', False,
                            case_messages=[first, second], details={}).body
        line = '- 🔗 [bitcoinnailbar.com/#gallery](https://bitcoinnailbar.com/#gallery)'
        section = body.split('### 🖼 Minh họa / Evidence (đầu vào)\n')[1].split('\n\n### ')[0]
        self.assertEqual(section, line)

    def test_trim_deduplicate_cap_and_time_order(self):
        later = message(2, text='HTTPS://A.EXAMPLE/x https://a.example/x). ' +
                        ' '.join(f'https://a.example/{i}' for i in range(15)))
        earlier = message(1, text='https://a.example/x).')
        lines = source_links([later, earlier])
        self.assertEqual(lines[0], '- 🔗 [a.example/x](https://a.example/x)')
        self.assertEqual(len(lines), 10)
        self.assertEqual(sum('a.example/x]' in line for line in lines), 1)

    def test_validation_and_redaction(self):
        text = ('https://user:pw@a.example/ https://user@a.example/ javascript:alert ftp://a.example '
                'https://\ud800.example/ https://a.example/' + 'x' * 2001)
        self.assertEqual(source_links([message(text=text)]), [])

    def test_phone_email_and_github_source_links(self):
        urls = ('https://a.example/x(555)123-4567', 'https://t.me/c/1234567890/5',
                'https://shop.example/p/12345678901', 'https://a.example/?phone=0901234567',
                'https://a.example/?email=a@b.example')
        lines = source_links([message(text=' '.join(urls))])
        self.assertEqual(len(lines), len(urls))
        for line, url in zip(lines, urls):
            with self.subTest(url=url):
                self.assertTrue(line.endswith('(' + url.replace('(', '%28').replace(')', '%29') + ')'))
        self.assertIn('phone=0901234567', lines[3])
        self.assertIn('email=a@\u200bb.example', lines[4])
        self.assertEqual(source_links([message(text='https://github.com/o/r/issues/1')]),
                         ['- 🔗 [github\u200b.com/o/r/issues/1](https://github.com/o/r/issues/1)'])

    def test_display_order_port_idna_and_backslash(self):
        lines = source_links([message(text='https://a.example/x\\ https://a.example:8080/p?q=1#f https://bücher.example/p')])
        self.assertEqual(lines, ['- 🔗 [a.example/x\\\\](https://a.example/x%5C)',
                                 '- 🔗 [a.example:8080/p?q=1#f](https://a.example:8080/p?q=1#f)',
                                 '- 🔗 [xn--bcher-kva.example/p](https://xn--bcher-kva.example/p)'])

    def test_encoding_truncation_and_sanitized_display(self):
        lines = source_links([message(text='https://a.example/a(b)/@name/[hello] https://a.example/' + 'z' * 100)])
        self.assertEqual(lines[0], '- 🔗 [a.example/a\\(b\\)/@\u200bname/\\[hello\\]](https://a.example/a%28b%29/@name/%5Bhello%5D)')
        self.assertEqual(lines[1], '- 🔗 [a.example/' + 'z' * 69 + '…](https://a.example/' + 'z' * 100 + ')')

    def test_all_evidence_sections_and_bug_question(self):
        source = message(text='https://a.example/x')
        line = '- 🔗 [a.example/x](https://a.example/x)'
        for category, heading, after in (
            ('bug', '### 🖼 Minh họa / Evidence', '### ❓ Cần làm rõ'),
            ('feedback', '### 🧭 Ngữ cảnh', '### ❓ Cần làm rõ'),
            ('task', '### 🖼 Minh họa / Evidence (đầu vào)', '### 🏁 Xong khi'),
            ('human', '### 🖼 Minh họa / Evidence', '<details>'),
        ):
            with self.subTest(category=category):
                human = category == 'human'
                body = format_issue(source, None if human else classification(type=category),
                                    'model', human, details=None if human else {}).body
                self.assertLess(body.index(heading), body.index(line))
                self.assertLess(body.index(line), body.index(after))
                if category == 'bug':
                    self.assertNotIn('Gửi thêm ảnh/video màn hình lỗi', body)

    def test_no_url_matches_prechange_golden_for_each_type(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'template_golden.json').read_text(encoding='utf-8'))
        actual = outputs()
        for key in ('bug/False/0/False/False', 'feedback/0000000', 'task/0000000', 'human/model/False/True'):
            with self.subTest(key=key):
                self.assertEqual(actual[key]['body'], fixture[key]['body'])
