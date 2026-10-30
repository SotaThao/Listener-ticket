import contextlib
import io
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tg_triage.__main__ import _load_decision_rules, main
from tg_triage.config import ConfigError, load_config
from tg_triage.decision_rules import RulesSnapshot
from tests.helpers import EXAMPLE, FAKE_HASH

ROOT = Path(__file__).resolve().parents[1]
FILES = ('states', 'facts', 'readiness', 'questions', 'channels', 'examples')
ENV = {'TG_API_ID': '123', 'TG_API_HASH': FAKE_HASH}


class RulesBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.text = EXAMPLE.read_text(encoding='utf8')
        self.enabled = self.text.replace('autonomous_enabled = false', 'autonomous_enabled = true').replace('decision_rules_enabled = false', 'decision_rules_enabled = true').replace('# ops_chat_id = -1009876543210', 'ops_chat_id = -1009876543210')
        self.path = self.root / 'config.toml'

    def config(self, text):
        self.path.write_text(text, encoding='utf8')
        return load_config(self.path, ENV)

    def snapshot(self):
        directory = self.root / 'rules'; directory.mkdir(exist_ok=True)
        for name in FILES:
            text = (ROOT / 'rules' / f'{name}.md').read_text(encoding='utf8')
            (directory / f'{name}.md').write_text(text.replace('status DRAFT', 'status ACTIVE'), encoding='utf8')
        return RulesSnapshot(directory, ROOT / 'templates' / 'github-ticket')

    def cli(self, command):
        out, err = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, ENV, clear=True), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = main([command, '--config', str(self.path)])
        return result, out.getvalue(), err.getvalue()

    def test_flag_defaults_off_and_validates_dependencies_and_ops(self):
        default = self.config(self.text)
        self.assertFalse(default.decision_rules_enabled)
        self.assertIsNone(default.telegram.ops_chat_id)
        valid = self.config(self.enabled)
        self.assertTrue(valid.decision_rules_enabled)
        self.assertEqual(valid.telegram.ops_chat_id, -1009876543210)
        for text in (self.enabled.replace('enabled = true', 'enabled = false', 1),
                     self.enabled.replace('autonomous_enabled = true', 'autonomous_enabled = false'),
                     self.enabled.replace('ops_chat_id = -1009876543210', '# ops_chat_id = -1009876543210'),
                     self.enabled.replace('ops_chat_id = -1009876543210', 'ops_chat_id = -1001234567890'),
                     self.enabled.replace('ops_chat_id = -1009876543210', 'ops_chat_id = true'),
                     self.enabled.replace('decision_rules_enabled = true', 'decision_rules_enabled = 1')):
            with self.subTest(text=text), self.assertRaises(ConfigError): self.config(text)

    def test_check_config_disabled_does_not_load_rules(self):
        self.config(self.text)
        with patch('tg_triage.__main__._load_decision_rules', side_effect=AssertionError('must not load')):
            code, out, _ = self.cli('check-config')
        self.assertEqual(code, 0)
        self.assertNotIn('rules', json.loads(out))

    def test_draft_rules_block_check_config(self):
        self.config(self.enabled)
        directory = self.root / 'draft'; directory.mkdir()
        for name in FILES:
            text = (ROOT / 'rules' / f'{name}.md').read_text(encoding='utf8')
            (directory / f'{name}.md').write_text(text.replace('status ACTIVE', 'status DRAFT'), encoding='utf8')
        with patch('tg_triage.__main__._load_decision_rules', side_effect=lambda: RulesSnapshot(directory, ROOT / 'templates' / 'github-ticket')):
            code, _, err = self.cli('check-config')
        self.assertEqual(code, 2)
        self.assertIn('rules must be ACTIVE', err)

    def test_active_fixture_fingerprint_and_run_uses_same_snapshot(self):
        self.config(self.enabled)
        snapshot = self.snapshot()
        from tg_triage.store import Store
        store = Store(':memory:')
        with patch('tg_triage.__main__._load_decision_rules', return_value=snapshot) as load, \
             patch('tg_triage.__main__.Store', return_value=store), \
             patch('tg_triage.__main__.validate_data_directories'), \
             patch('tg_triage.__main__.ProcessLock', return_value=contextlib.nullcontext()), \
             patch('tg_triage.__main__._build_classifier', return_value=object()), \
             patch('tg_triage.telegram_io.run', new_callable=AsyncMock) as run:
            code, out, _ = self.cli('check-config')
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)['rules']['fingerprint'], snapshot.fingerprint)
            code, _, err = self.cli('run')
            self.assertEqual(code, 0, err)
            self.assertIs(run.await_args.args[1].rules_snapshot,snapshot)
            self.assertEqual(load.call_count,2)

    def test_installed_fallback_and_missing_file_fail_closed(self):
        installed = self.root / 'prefix' / 'share' / 'tg-triage'
        (installed / 'rules').mkdir(parents=True)
        for name in FILES:
            text = (ROOT / 'rules' / f'{name}.md').read_text(encoding='utf8')
            (installed / 'rules' / f'{name}.md').write_text(text.replace('status DRAFT', 'status ACTIVE'), encoding='utf8')
        shutil.copytree(ROOT / 'templates', installed / 'templates')
        with patch('tg_triage.__main__.__file__', str(self.root / 'lib' / 'tg_triage' / '__main__.py')), patch('tg_triage.__main__.sys.prefix', str(self.root / 'prefix')):
            self.assertEqual(len(_load_decision_rules().fingerprint), 64)
            (installed / 'rules' / 'states.md').unlink()
            self.config(self.enabled)
            code, _, err = self.cli('check-config')
            self.assertEqual(code, 2)
            self.assertIn('states.md:1:', err)

    def test_startup_halt_checks_reserved_comment_before_any_recovery(self):
        from tg_triage.store import Store
        from tg_triage.rules_pending_store import RulesPendingStore
        from tests.helpers import message
        import hashlib
        db=self.root/'state.sqlite3'
        value=self.enabled.replace('/var/lib/agentfleet/tg/triage.sqlite3',db.as_posix())
        self.config(value); snapshot=self.snapshot()
        store=Store(db); pending=RulesPendingStore(store)
        pending.reserve_comment(-1001234567890,55,'sandbox/triage',42)
        incoming=message(60,text='unrelated'); store.enqueue(incoming); store.start_classifying(incoming)
        store.close(); before=hashlib.sha256(db.read_bytes()).hexdigest()
        with patch('tg_triage.__main__._load_decision_rules',return_value=snapshot), \
             patch('tg_triage.__main__.validate_data_directories'), \
             patch('tg_triage.__main__.ProcessLock',return_value=contextlib.nullcontext()), \
             patch('tg_triage.__main__._build_classifier') as provider, \
             patch('tg_triage.telegram_io.run',new_callable=AsyncMock) as run:
            code,_,err=self.cli('run')
        self.assertEqual(code,2)
        self.assertIn('Owner inspection',err)
        provider.assert_not_called(); run.assert_not_awaited()
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(),before)
        import sqlite3
        with contextlib.closing(sqlite3.connect(db)) as check:
            self.assertEqual(check.execute('SELECT decision FROM messages WHERE message_id=60').fetchone()[0],'classifying')
            self.assertEqual(check.execute('SELECT status FROM rules_comments').fetchone()[0],'reserved')

    def test_pre_recovery_allows_model_attempt_and_completed_effect(self):
        from tg_triage.store import Store
        from tg_triage.rules_pending_store import RulesPendingStore, check_before_recovery
        db=self.root/'safe.sqlite3'; store=Store(db); pending=RulesPendingStore(store)
        pending.create('topic',-1001234567890,1); pending.reserve_attempt('topic',3)
        pending.reserve_comment(-1001234567890,2,'sandbox/triage',42); pending.finish_comment(-1001234567890,2,100)
        store.close()
        check_before_recovery(db)

    def test_readonly_claim_recovers_after_safe_preflight(self):
        from tg_triage.store import Store
        from tg_triage.rules_pending_store import RulesPendingStore, check_before_recovery
        from tests.helpers import message
        db=self.root/'claim.sqlite3'; store=Store(db); pending=RulesPendingStore(store)
        source=message(42,text='',has_image=True); store.enqueue(source)
        pending.create('topic',source.chat_id,source.message_id)
        self.assertTrue(pending.claim_topic('topic',pending.topic_snapshot('topic')))
        store.close(); check_before_recovery(db)
        store=Store(db); pending=RulesPendingStore(store); self.addCleanup(store.close)
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM rules_inflight_topics').fetchone()[0],0)
        self.assertEqual(pending.topic_snapshot('topic')['status'],'OPEN')

    def test_preflight_rejects_unknown_attachment_without_mutation(self):
        from tg_triage.store import Store
        from tg_triage.rules_pending_store import RulesPendingStore, check_before_recovery, RulesHalt
        from tests.helpers import message
        import hashlib
        db=self.root/'attachment.sqlite3'; store=Store(db); pending=RulesPendingStore(store)
        source=message(42,text='lỗi',has_image=True); store.enqueue(source); pending.create('topic',source.chat_id,source.message_id)
        store.reserve_attachment_call('topic',42,'image.jpg',100,chat_id=source.chat_id)
        store.close(); before=hashlib.sha256(db.read_bytes()).digest()
        with self.assertRaises(RulesHalt): check_before_recovery(db)
        self.assertEqual(hashlib.sha256(db.read_bytes()).digest(),before)

    def test_wheel_includes_only_the_six_decision_files(self):
        spec = importlib.util.spec_from_file_location('build_wheel', ROOT.parent / 'deploy' / 'build_wheel.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _, data = module.wheel_bytes(ROOT, 'listener', 'test')
        with zipfile.ZipFile(io.BytesIO(data)) as wheel:
            names = {name.rsplit('/', 1)[-1] for name in wheel.namelist() if '/share/tg-triage/rules/' in name}
        self.assertEqual(names, {name + '.md' for name in FILES})
