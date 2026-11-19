"""Offline startup, installed resource and classification provenance regressions."""
import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tg_triage import assistant_pack, classification_log
from tg_triage.__main__ import _load_assistant_pack, _load_decision_rules, main
from tg_triage.pipeline import Pipeline
from tg_triage.rules_intake import evaluate_batch
from tg_triage.store import Store
from tg_triage.thinking import CURRENT, Turn
from tests.helpers import config, message

ROOT = Path(__file__).resolve().parents[1]


class AssistantPackRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'listener'
        for directory in ('rules', 'assistant', 'templates'):
            shutil.copytree(ROOT / directory, self.source / directory)
        self.pack_path = self.source / 'assistant' / assistant_pack.PACK_FILE
        self.fingerprint = assistant_pack.load(self.source / 'assistant', self.source / 'rules').fingerprint
        self.settings = replace(config(), autonomous_enabled=True, decision_rules_enabled=True,
                                db_path=str(self.root / 'triage.sqlite3'))
        self.enterContext(patch('tg_triage.__main__.__file__', str(self.source / 'src/tg_triage/__main__.py')))
        self.enterContext(patch('tg_triage.__main__.sys.prefix', str(self.root / 'prefix')))
        self.enterContext(patch('tg_triage.__main__.load_config', return_value=self.settings))
        self.validate = self.enterContext(patch('tg_triage.__main__.validate_data_directories'))

    def cli(self, command):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main([command, '--config', str(self.root / 'unused.toml')])
        return code, out.getvalue(), err.getvalue()

    def assert_rules_failure(self, expected):
        for command in ('check-config', 'run'):
            with self.subTest(command=command), patch('tg_triage.__main__.Store') as store:
                code, _, err = self.cli(command)
                self.assertEqual(code, 2, err)
                self.assertTrue(err.startswith('RulesError: '), err)
                self.assertIn(expected, err)
                store.assert_not_called()
        self.validate.assert_not_called()

    def test_startup_logs_pack_fingerprint_and_pipeline_records_it(self):
        rows = []

        async def run(settings, pipeline):
            self.assertIsInstance(pipeline, Pipeline)
            source = message(42, 'ok', mentions_listener=False)
            pipeline.store.enqueue(source)
            self.assertEqual(pipeline.process(source).decision, 'chat')
            rows.extend(dict(row) for row in pipeline.store.connection.execute(
                'SELECT phase,rules_version FROM classification_runs'))

        with patch('tg_triage.__main__.ProcessLock', return_value=contextlib.nullcontext()), \
             patch('tg_triage.__main__._build_classifier', return_value=object()), \
             patch('tg_triage.telegram_io.run', new=AsyncMock(side_effect=run)), \
             self.assertLogs('tg_triage.__main__', level='INFO') as logs:
            code, _, err = self.cli('run')
        self.assertEqual(code, 0, err)
        self.assertIn('assistant pack fingerprint=' + self.fingerprint, '\n'.join(logs.output))
        self.assertIn('rules fingerprint=', '\n'.join(logs.output))
        self.assertEqual(rows, [dict(phase='early', rules_version=self.fingerprint)])
        self.validate.assert_called_once_with(self.settings)

    def test_missing_pack_uses_invalid_rules_exit_path(self):
        self.pack_path.unlink()
        self.assert_rules_failure('cannot read UTF-8 file')

    def test_tampered_pack_uses_invalid_rules_exit_path(self):
        self.pack_path.write_bytes(self.pack_path.read_bytes() + b'edited\n')
        self.assert_rules_failure('extra pack content')

    def test_rule_drift_uses_invalid_rules_exit_path(self):
        path = self.source / 'rules' / 'facts.md'
        path.write_bytes(path.read_bytes() + b'\n<!-- source changed -->\n')
        self.assert_rules_failure('source drift')

    def test_principles_drift_uses_invalid_rules_exit_path(self):
        path = self.source / 'assistant' / assistant_pack.PRINCIPLES_FILE
        path.write_bytes(path.read_bytes() + b'\n<!-- principles changed -->\n')
        self.assert_rules_failure('source drift')

    def test_invalid_rules_use_same_exit_path(self):
        path = self.source / 'rules' / 'states.md'
        path.write_text('invalid rules\n', encoding='utf-8')
        self.assert_rules_failure('states.md')

    def test_check_config_contains_pack_fingerprint_and_retention(self):
        code, out, err = self.cli('check-config')
        self.assertEqual(code, 0, err)
        assistant = json.loads(out)['assistant']
        self.assertEqual(assistant['retention_days'], self.settings.assistant.retention_days)
        self.assertEqual(assistant['pack_fingerprint'], self.fingerprint)
        self.assertEqual(assistant['pack_mode'], 'full')
        self.assertFalse(assistant['enabled'])
        self.assertEqual(assistant['mode'], 'shadow')
        self.assertEqual(assistant['models'], ['ag/gemini-3.8-flash-medium', 'cx/gpt-6.1-sol'])
        self.assertFalse(assistant['router_key_configured'])
        self.assertNotIn('router_key', assistant)

    def test_disabled_rules_do_not_require_pack_and_report_null(self):
        with patch('tg_triage.__main__.load_config', return_value=replace(self.settings, decision_rules_enabled=False)), \
             patch('tg_triage.__main__._load_assistant_pack', side_effect=AssertionError('must not load')):
            code, out, err = self.cli('check-config')
        self.assertEqual(code, 0, err)
        self.assertIsNone(json.loads(out)['assistant']['pack_fingerprint'])

    def test_installed_fallback_loads_and_missing_pack_fails_closed(self):
        installed = self.root / 'prefix/share/tg-triage'
        installed.parent.mkdir(parents=True)
        self.source.rename(installed)
        self.assertEqual(_load_assistant_pack().fingerprint, self.fingerprint)
        self.assertEqual(len(_load_decision_rules().fingerprint), 64)
        code, out, err = self.cli('check-config')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['assistant']['pack_fingerprint'], self.fingerprint)
        (installed / 'assistant' / assistant_pack.PACK_FILE).unlink()
        self.assert_rules_failure('cannot read UTF-8 file')

    def test_core_check_config_and_installed_bundle_with_no_reference_source_files(self):
        settings = replace(self.settings, assistant=replace(self.settings.assistant, enabled=True,
                            mode='live', authority='decide', pack_mode='core', router_key='fake'))
        core = _load_assistant_pack(pack_mode='core').core
        installed = self.root / 'prefix/share/tg-triage'
        installed.parent.mkdir(parents=True)
        self.source.rename(installed)
        for name in ('README.md', 'preflight.md'):
            (installed / 'rules' / name).unlink()
        with patch('tg_triage.__main__.load_config', return_value=settings):
            code, out, err = self.cli('check-config')
            self.assertEqual(code, 0, err)
            assistant = json.loads(out)['assistant']
            self.assertEqual((assistant['pack_mode'], assistant['pack_fingerprint'],
                              assistant['core_pack_fingerprint']), ('core', self.fingerprint, core.fingerprint))
            (installed / 'assistant' / assistant_pack.CORE_FILE).write_text('{}', encoding='utf-8')
            self.assert_rules_failure('invalid assistant core bundle')

    def test_all_classification_phases_record_pack_version_or_null(self):
        snapshot = _load_decision_rules()
        for fingerprint in (self.fingerprint, None):
            with self.subTest(fingerprint=fingerprint), contextlib.closing(Store(':memory:')) as store:
                if fingerprint is not None:
                    store.assistant_pack_fingerprint = fingerprint
                pipeline = Pipeline(self.settings, store, object(), rules_snapshot=snapshot)
                source = message(42, '/question sao?', mentions_listener=False)
                store.enqueue(source)
                self.assertEqual(pipeline.process(source).decision, 'chat')
                for options in ({}, {'allow_mixed': True}, {'confirm_followup': True}):
                    token = CURRENT.set(Turn(store, 'rules:test', 'test', 42))
                    try:
                        self.assertIsNotNone(evaluate_batch(snapshot, object(), [source], **options))
                    finally:
                        CURRENT.reset(token)
                classification_log.flush(store)
                rows = list(store.connection.execute('SELECT phase,rules_version FROM classification_runs ORDER BY run_id'))
                self.assertEqual([(r['phase'], r['rules_version']) for r in rows],
                                 [(phase, fingerprint) for phase in ('early', 'extract', 'pending_merge', 'followup_confirm')])

    def test_wheel_contains_pack_and_principles_with_valid_record(self):
        builder = ROOT.parent / 'deploy/build_wheel.py'
        if not builder.is_file():
            self.skipTest('deploy/build_wheel.py absent outside a repository checkout')
        spec = importlib.util.spec_from_file_location('assistant_pack_wheel_builder', builder)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        filename, data = module.wheel_bytes(ROOT, 'listener', 'test')
        wheel_path = self.root / filename
        wheel_path.write_bytes(data)
        self.assertTrue(module.verify_record(wheel_path.read_bytes()))
        with zipfile.ZipFile(wheel_path) as wheel:
            resources = {name.rsplit('/', 1)[-1]: name for name in wheel.namelist()
                         if '/share/tg-triage/assistant/' in name}
            self.assertEqual(set(resources), {assistant_pack.PRINCIPLES_FILE, assistant_pack.PACK_FILE,
                                              assistant_pack.CORE_FILE})
            self.assertIn('tg_triage/assistant_pack.py', wheel.namelist())
            for name, entry in resources.items():
                self.assertEqual(wheel.read(entry), (ROOT / 'assistant' / name).read_bytes())
            self.assertEqual(assistant_pack.parse(wheel.read(resources[assistant_pack.PACK_FILE]).decode('utf-8')).fingerprint,
                             self.fingerprint)
