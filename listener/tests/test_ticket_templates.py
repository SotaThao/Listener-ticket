"""Template validation, injection safety, and startup failure tests."""
import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tg_triage.__main__ import main
from tg_triage.ticket_templates import TemplateError, validate
from tests.helpers import config

SOURCE = Path(__file__).resolve().parents[1] / 'templates' / 'github-ticket'


class TicketTemplatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / 'templates'
        shutil.copytree(SOURCE, self.directory)

    def change(self, filename, old, new):
        path = self.directory / filename
        text = path.read_text(encoding='utf-8')
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding='utf-8', newline='\n')

    def test_rejects_missing_files_blocks_slots_and_syntax(self):
        cases = [
            ('{{#block body}}', '{{#block wrong}}'),
            ('{{summary}}', 'text'),
            ('{{summary}}', '{{unknown}}'),
            ('{{original}}', 'prefix {{original}}'),
            ('{{@steps}}', '{{steps}}'),
            ('{{#if steps}}', '{{#if steps}}\n{{#if steps}}\n{{#if steps}}\n{{#if steps}}'),
            ('{{#else}}', '{{#else}}\n{{#else}}'),
            ('{{/if}}', ''),
            ('{{@steps}}', '{{@x}}'),
            ('{{summary}}', '{{summary'),
            ('{{summary}}', '{{summary|bad|fallback}}'),
            ('{{summary}}', '{{@index}}'),
            ('{{@index}}', '{{@index|fallback}}'),
            ('{{summary}}', '{{summary|://unsafe}}'),
            ('{{summary}}', '{{#else}}'),
            ('{{summary}}', '{{#bogus}}'),
            ('{{summary}}', '{{#block nested}}'),
            ('{{#block body}}', '{{summary}}\n{{#block body}}'),
        ]
        original = (self.directory / 'bug.md').read_text(encoding='utf-8')
        for old, new in cases:
            with self.subTest(old=old, new=new):
                (self.directory / 'bug.md').write_text(original.replace(old, new, 1), encoding='utf-8')
                with self.assertRaisesRegex(TemplateError, r'bug\.md:\d+:'):
                    validate(self.directory)
        (self.directory / 'bug.md').unlink()
        with self.assertRaisesRegex(TemplateError, r'bug\.md:1:'):
            validate(self.directory)

    def test_rejects_unsafe_static_text_and_bad_unicode(self):
        for value in ('<script>', '<!--', '://', '](', '#12', '@name', 'www.example', '![', '```',
                      'trailing ', '\x01', 'e\u0301', 'x' * 301):
            with self.subTest(value=value):
                path = self.directory / 'bug.md'
                original = (SOURCE / 'bug.md').read_text(encoding='utf-8')
                path.write_text(original.replace('### 🐞 Lỗi', value, 1), encoding='utf-8')
                with self.assertRaises(TemplateError):
                    validate(self.directory)

    def test_bom_crlf_and_values_are_not_reparsed(self):
        path = self.directory / 'bug.md'
        path.write_bytes(b'\xef\xbb\xbf' + path.read_bytes().replace(b'\n', b'\r\n'))
        templates = validate(self.directory)
        hostile = '{{summary}} {{#if x}} {{/block}} {{@steps}}'
        text = templates.render('bug.md', 'body', {'summary': hostile, 'evidence': '', 'notes': [],
            'questions': [], 'original': '', 'footer': '', 'steps': []})
        self.assertIn(hostile, text)

    def test_rejects_extra_file_lone_cr_and_multiline_string_block(self):
        extra = self.directory / 'extra.md'
        extra.write_text('extra', encoding='utf-8')
        with self.assertRaises(TemplateError): validate(self.directory)
        extra.unlink()
        path = self.directory / 'bug.md'
        original = path.read_bytes()
        path.write_bytes(original + b'\r')
        with self.assertRaises(TemplateError): validate(self.directory)
        path.write_bytes(original)
        self.change('bug.md', 'Các bước để tái hiện lỗi là gì?', 'Các bước\nkhác')
        with self.assertRaises(TemplateError): validate(self.directory)

    def test_group_replies_forbid_static_mentions_links_and_html(self):
        original = (SOURCE / 'group-replies.md').read_text(encoding='utf-8')
        path = self.directory / 'group-replies.md'
        for value in ('@', '://', 'www.', '<'):
            path.write_text(original.replace('Chưa tạo GitHub issue', value, 1), encoding='utf-8')
            with self.subTest(value=value), self.assertRaises(TemplateError): validate(self.directory)

    def test_template_edit_changes_rendered_order(self):
        baseline = validate(self.directory)
        self.change('bug.md', '**Mong đợi:** {{expected|(chưa có thông tin)}}\n**Thực tế:** {{actual|(chưa có thông tin)}}',
                    '**Thực tế:** {{actual|(chưa có thông tin)}}\n**Mong đợi:** {{expected|(chưa có thông tin)}}')
        changed = validate(self.directory)
        values = {'summary': 'x', 'evidence': '', 'notes': [], 'questions': [],
                  'original': '', 'footer': '', 'steps': []}
        self.assertNotEqual(baseline.render('bug.md', 'body', values), changed.render('bug.md', 'body', values))
        rendered = changed.render('bug.md', 'body', values)
        self.assertLess(rendered.index('**Thực tế:**'), rendered.index('**Mong đợi:**'))

    def test_check_config_rejects_broken_template(self):
        (self.directory / 'bug.md').unlink()
        errors = io.StringIO()
        with patch('tg_triage.__main__.load_config', return_value=config()), \
             patch('tg_triage.__main__.load_templates', side_effect=lambda: validate(self.directory)), \
             contextlib.redirect_stderr(errors):
            self.assertEqual(main(['check-config']), 2)
        self.assertIn('TemplateError: bug.md:1:', errors.getvalue())
