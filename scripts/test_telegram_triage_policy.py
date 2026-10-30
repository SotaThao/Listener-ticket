"""Negative tests for documentation drift checks, independent of live services."""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('policy_checker', Path(__file__).with_name('check-telegram-triage-policy.py'))
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)

class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        source = Path(__file__).resolve().parents[1]
        for folder in ('listener/src/tg_triage', 'listener/prompts', 'listener/tests', 'listener/templates'):
            shutil.copytree(source/folder, self.root/folder, ignore=shutil.ignore_patterns('__pycache__'))
        self.policy = self.root/checker.POLICY
        self.policy.parent.mkdir(parents=True)
        self.trace = {'schema': 'telegram-triage-policy/v1', 'owner': 'AlexChu', 'rules': [],
                      'runtime_inventory': {}, 'legacy_documents': [], 'related_documents': []}
        for n in range(1,34):
            self.trace['rules'].append({'id': f'TG-{n:02}',
                'code': [{'path':'listener/src/tg_triage/rules.py', 'symbol':'evaluate'}],
                'tests': [{'path':'listener/tests/test_rules.py', 'symbol':'RulesTests.test_all_commands_are_confident_and_keep_payload'}]})
        for folder, pattern in [('listener/src/tg_triage','*.py'),('listener/prompts','*.md'),('listener/templates','**/*.md')]:
            for path in (self.root/folder).glob(pattern):
                self.trace['runtime_inventory'][path.relative_to(self.root).as_posix()] = checker.normalized_hash(path)
        self.headings = [f'### TG-{n:02} Rule' for n in range(1,34)]
        self.extra = ''
    def write(self):
        self.policy.write_text('\n'.join(self.headings)+'\n```json telegram-triage-trace\n'+json.dumps(self.trace)+'\n```\n'+self.extra,encoding='utf-8')
    def assert_rejected(self, expected):
        self.write()
        with self.assertRaisesRegex(checker.PolicyError, expected):
            checker.check(self.root)
    def test_valid_and_git_lf_normalization(self):
        p=self.root/'listener/src/tg_triage/rules.py'
        p.write_bytes(p.read_bytes().replace(b'\r\n',b'\n').replace(b'\n',b'\r\n'))
        self.write()
        self.assertEqual(checker.check(self.root)['rules'],33)
    def test_duplicate_heading(self):
        self.headings.append(self.headings[0]); self.assert_rejected('headings')
    def test_missing_rule(self):
        self.trace['rules'].pop(); self.assert_rejected('trace rules')
    def test_duplicate_rule(self):
        self.trace['rules'].append(self.trace['rules'][0]); self.assert_rejected('trace rules')
    def test_missing_symbol(self):
        self.trace['rules'][0]['code'][0]['symbol']='invented'; self.assert_rejected('missing symbol')
    def test_path_escape(self):
        self.trace['rules'][0]['code'][0]['path']='../../outside.py'; self.assert_rejected('escaping')
    def test_windows_absolute_path(self):
        self.trace['rules'][0]['code'][0]['path']='C:/secret.py'; self.assert_rejected('invalid local path')
    def test_hash_drift(self):
        p=self.root/'listener/src/tg_triage/rules.py';p.write_text(p.read_text()+'\n# drift\n')
        self.assert_rejected('REVIEW_REQUIRED runtime hash')
    def test_inventory_drift(self):
        (self.root/'listener/prompts/unreviewed.md').write_text('new')
        self.assert_rejected('REVIEW_REQUIRED runtime inventory')
    def test_template_drift(self):
        p=self.root/'listener/templates/github-ticket/bug.md'
        p.write_text(p.read_text(encoding='utf-8')+'\n',encoding='utf-8')
        self.assert_rejected('REVIEW_REQUIRED runtime hash')
    def test_broken_link(self):
        self.extra='[broken](missing.md)'; self.assert_rejected('missing or escaping')
    def test_legacy_requires_status_and_source(self):
        self.trace['legacy_documents']=['old.md']; (self.root/'old.md').write_text('old')
        self.assert_rejected('legacy triage status/link')
    def test_missing_code_trace(self):
        self.trace['rules'][0]['code']=[]; self.assert_rejected('missing code trace')
    def test_missing_tests_trace(self):
        self.trace['rules'][0]['tests']=[]; self.assert_rejected('missing tests trace')
    def test_production_function_is_not_a_test(self):
        self.trace['rules'][0]['tests']=[{'path':'listener/src/tg_triage/rules.py','symbol':'command_type'}]
        self.assert_rejected('real test method')
    def test_removed_inventory_entry(self):
        self.trace['runtime_inventory'].pop(next(iter(self.trace['runtime_inventory'])))
        self.assert_rejected('REVIEW_REQUIRED runtime inventory')
    def test_broken_related_document_link(self):
        self.trace['related_documents']=['related.md']
        (self.root/'related.md').write_text('[bad](missing.md)')
        self.assert_rejected('missing or escaping')
    def test_duplicate_trace_block(self):
        self.extra='```json telegram-triage-trace\n{}\n```'; self.assert_rejected('one embedded')

if __name__=='__main__':
    unittest.main()
