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
EXTENSION_FILES = ('lifecycle', 'analysis-status')
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

    def test_approved_source_extensions_load_without_fixture_activation(self):
        snapshot=_load_decision_rules()
        self.assertEqual(snapshot.extensions.disposition('recurrence'),'RELATED')
        self.assertEqual(snapshot.extensions.route('analyze'),'ANALYZE')
        self.assertEqual(snapshot.extensions.route('status'),'STATUS')
        self.assertEqual(len(snapshot.fingerprint),64)
        for name in EXTENSION_FILES:
            self.assertIn('version 3 · status ACTIVE',(ROOT/'rules'/f'{name}.md').read_text(encoding='utf8').splitlines()[0])

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
        # The startup ledger check is real; never point it at the host's production ledger.
        self.config(self.enabled.replace('/var/lib/agentfleet/tg/triage.sqlite3', (self.root / 'absent.sqlite3').as_posix()))
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
        for name in FILES + EXTENSION_FILES:
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
        from tests.helpers import message
        store.enqueue(message(1));store.enqueue(message(2))
        with store.connection:store.connection.execute("UPDATE messages SET decision='chat' WHERE message_id=2")
        pending.create('topic',-1001234567890,1); pending.reserve_attempt('topic',3)
        pending.reserve_comment(-1001234567890,2,'sandbox/triage',42); pending.finish_comment(-1001234567890,2,100)
        store.close()
        check_before_recovery(db)

    def test_legacy_work_halts_before_recovery_without_mutating_database(self):
        import hashlib
        import sqlite3
        from tg_triage.rules_pending_store import check_before_recovery, RulesHalt
        cases = (
            ('messages', 'decision TEXT', "INSERT INTO messages VALUES ('pending_review')"),
            ('cases', 'status TEXT', "INSERT INTO cases VALUES ('analyzing')"),
            ('followup_jobs', 'status TEXT', "INSERT INTO followup_jobs VALUES ('planned')"),
            ('followup_operations', 'status TEXT', "INSERT INTO followup_operations VALUES ('reserved')"),
            ('followup_analysis_retries', 'edit_status TEXT', "INSERT INTO followup_analysis_retries VALUES ('reserved')"),
            ('autonomous_confirmations', 'status TEXT', "INSERT INTO autonomous_confirmations VALUES ('ambiguous')"),
            ('group_notices', 'status TEXT', "INSERT INTO group_notices VALUES ('reserved')"),
            ('attachments', 'status TEXT', "INSERT INTO attachments VALUES ('uploading')"),
        )
        for table, schema, insert in cases:
            with self.subTest(table=table):
                db = self.root / (table + '.sqlite3')
                with contextlib.closing(sqlite3.connect(db)) as connection:
                    with connection:
                        connection.execute(f'CREATE TABLE {table} ({schema})')
                        connection.execute(insert)
                before = hashlib.sha256(db.read_bytes()).digest()
                with self.assertRaises(RulesHalt):
                    check_before_recovery(db)
                self.assertEqual(hashlib.sha256(db.read_bytes()).digest(), before)

        for name, schema, row in (
            ('old-error', 'decision TEXT', "'error'"),
            ('reserved-post', 'decision TEXT, github_called_at TEXT', "'error', '2026-01-01'"),
        ):
            with self.subTest(name=name):
                db = self.root / (name + '.sqlite3')
                with contextlib.closing(sqlite3.connect(db)) as connection:
                    with connection:
                        connection.execute(f'CREATE TABLE messages ({schema})')
                        connection.execute(f'INSERT INTO messages VALUES ({row})')
                before = hashlib.sha256(db.read_bytes()).digest()
                with self.assertRaises(RulesHalt):
                    check_before_recovery(db)
                self.assertEqual(hashlib.sha256(db.read_bytes()).digest(), before)

    def test_completed_legacy_effects_are_not_treated_as_pending(self):
        # A terminal status alone cannot establish a supported schema or owner.
        # Produce actual completed legacy work through the existing fake pipeline.
        from tests.test_followups import FollowupTests,answer
        from tg_triage.rules_pending_store import check_before_recovery
        fixture=FollowupTests('test_update_comment_only_uses_trusted_number_and_keeps_body')
        self.addCleanup(fixture.doCleanups)
        db=self.root/'complete.sqlite3'
        store,model,github,sent,pipeline=fixture.setup_case(path=db)
        model.value=answer('update')
        fixture.reply(pipeline,mid=1000,target=901,text='Thông tin rõ hơn')
        self.assertEqual('done',store.connection.execute('SELECT status FROM followup_jobs').fetchone()[0])
        self.assertEqual('done',store.connection.execute('SELECT status FROM followup_operations').fetchone()[0])
        self.assertEqual(1,len(github.calls));self.assertEqual(1,len(github.comments))
        result=store.connection.execute('SELECT result FROM followup_jobs').fetchone()[0]
        self.assertIsInstance(result,str)
        with self.assertRaises(ValueError):json.loads(result)  # Producer stores opaque notice text, not JSON.
        store.close();before=db.read_bytes();check_before_recovery(db)
        self.assertEqual(before,db.read_bytes())

    def test_status_only_completed_legacy_schema_is_unknown(self):
        import sqlite3
        from tg_triage.rules_pending_store import check_before_recovery,RulesHalt
        db=self.root/'status-only.sqlite3'
        with contextlib.closing(sqlite3.connect(db)) as connection:
            with connection:
                connection.execute('CREATE TABLE followup_jobs (status TEXT)')
                connection.execute("INSERT INTO followup_jobs VALUES ('done')")
                connection.execute('CREATE TABLE followup_operations (status TEXT)')
                connection.execute("INSERT INTO followup_operations VALUES ('done')")
                connection.execute('CREATE TABLE group_notices (status TEXT)')
                connection.execute("INSERT INTO group_notices VALUES ('sent')")
        before=db.read_bytes()
        with self.assertRaises(RulesHalt):check_before_recovery(db)
        self.assertEqual(before,db.read_bytes())

    def test_run_stops_on_legacy_pending_before_store_or_provider(self):
        import hashlib
        from tg_triage.store import Store
        from tests.helpers import message
        db = self.root / 'legacy.sqlite3'
        self.config(self.enabled.replace('/var/lib/agentfleet/tg/triage.sqlite3', db.as_posix()))
        snapshot = self.snapshot()
        store = Store(db)
        source = message(123, text='legacy pending')
        store.enqueue(source)
        store.close()
        before = hashlib.sha256(db.read_bytes()).digest()
        with patch('tg_triage.__main__._load_decision_rules', return_value=snapshot), \
             patch('tg_triage.__main__.validate_data_directories'), \
             patch('tg_triage.__main__.ProcessLock', return_value=contextlib.nullcontext()), \
             patch('tg_triage.__main__.Store') as open_store, \
             patch('tg_triage.__main__._build_classifier') as provider:
            code, _, err = self.cli('run')
        self.assertEqual(code, 2)
        self.assertIn('legacy pending', err)
        open_store.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(hashlib.sha256(db.read_bytes()).digest(), before)

    def _escalated_hold_db(self, name, *, decision='pending_review', topic_status='ESCALATED', notice='sent',
                           github=False, issue=False, also_closed=False):
        """Live 2026-11-05 shape: HOLD topic escalated to the Owner (sent escalation notice),
        source left as no-ticket pending_review."""
        from tg_triage.store import Store
        from tg_triage.models import Decision
        from tg_triage.rules_pending_store import RulesPendingStore
        from tests.helpers import message
        db=self.root/name; store=Store(db); pending=RulesPendingStore(store)
        source=message(6344,text='không trích được'); store.enqueue(source)
        topic=f'batch:{source.chat_id}:6344'
        pending.create(topic,source.chat_id,source.message_id)
        if also_closed:
            pending.create('other-topic',source.chat_id,source.message_id); self.assertTrue(pending.close('other-topic','CLOSED'))
        store.finish(source,Decision('pending_review','rule',case_key=topic,reason='rules_extract_pending'))
        with store.connection:
            store.connection.execute("UPDATE rules_topics SET phase='HOLD' WHERE topic_id=?",(topic,))
            if decision!='pending_review':
                store.connection.execute('UPDATE messages SET decision=? WHERE message_id=6344',(decision,))
            if github:
                store.connection.execute("UPDATE messages SET github_called_at='2026-11-05T04:46:50+00:00' WHERE message_id=6344")
            if issue:
                store.connection.execute("UPDATE messages SET issue_number=7,issue_url='https://github.com/o/r/issues/7' WHERE message_id=6344")
        if notice is not None:
            sequence=pending.reserve_notice(topic,-1009876543210,'escalation'); self.assertIsNotNone(sequence)
            if notice=='sent': self.assertTrue(pending.finish_notice(topic,sequence,555))
        if topic_status!='OPEN': self.assertTrue(pending.close(topic,topic_status))
        store.close()
        return db

    def test_escalated_hold_source_does_not_halt_restart(self):
        import hashlib
        from tg_triage.rules_pending_store import check_before_recovery
        db=self._escalated_hold_db('escalated.sqlite3')
        before=hashlib.sha256(db.read_bytes()).digest()
        check_before_recovery(db)
        self.assertEqual(hashlib.sha256(db.read_bytes()).digest(),before)

    def test_escalated_exemption_is_narrow(self):
        from tg_triage.rules_pending_store import check_before_recovery,RulesHalt
        # Owner 2026-11-12 "Duyệt sửa gate": a pending_review source of an EXPIRED topic is settled
        # (live halt 2026-11-12), so that shape left this must-halt list; see the expired tests below.
        with self.subTest('expired_topic'):
            check_before_recovery(self._escalated_hold_db('narrow-expired_topic.sqlite3',topic_status='EXPIRED'))
        pending_cases={'closed_topic':dict(topic_status='CLOSED'),
                       'queued_message':dict(decision='queued'),'classifying_message':dict(decision='classifying'),
                       'no_owner_notice':dict(notice=None),'issue_without_call':dict(issue=True),
                       'escalated_and_closed_with_issue':dict(issue=True,also_closed=True)}
        for label,kwargs in pending_cases.items():
            with self.subTest(label):
                db=self._escalated_hold_db(f'narrow-{label}.sqlite3',**kwargs)
                with self.assertRaisesRegex(RulesHalt,'legacy pending'): check_before_recovery(db)
        with self.subTest('github_called_with_issue'):
            # Not exempt, so the pending barrier halts before the POST-outcome check.
            db=self._escalated_hold_db('narrow-github.sqlite3',github=True,issue=True)
            with self.assertRaisesRegex(RulesHalt,'legacy pending'): check_before_recovery(db)
        with self.subTest('github_called_without_issue'):
            db=self._escalated_hold_db('narrow-github-noissue.sqlite3',github=True)
            with self.assertRaisesRegex(RulesHalt,'unconfirmed issue metadata'): check_before_recovery(db)
        with self.subTest('notice_not_sent'):
            db=self._escalated_hold_db('narrow-reserved-notice.sqlite3',notice='reserved')
            with self.assertRaises(RulesHalt): check_before_recovery(db)

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

    def test_deferred_ops_are_modern_for_each_pending_state(self):
        import hashlib
        from tg_triage.store import Store
        from tg_triage.rules_pending_store import RulesPendingStore, check_before_recovery
        from tests.helpers import message
        for status in ('queued', 'classifying', 'pending_review'):
            with self.subTest(status=status):
                db = self.root / ('deferred-' + status + '.sqlite3')
                store = Store(db); pending = RulesPendingStore(store)
                source = message(1); store.enqueue(source)
                pending.create('topic', source.chat_id, source.message_id)
                seq = pending.reserve_notice('topic', -1009876543210, 'question')
                pending.finish_notice('topic', seq, 70)
                self.assertTrue(pending.claim_topic('topic', pending.topic_snapshot('topic')))
                reply = message(71, chat_id=-1009876543210, reply_to_id=70)
                self.assertTrue(pending.accept_ops_message(reply, 7))
                with store.connection:
                    store.connection.execute('UPDATE messages SET decision=? WHERE chat_id=? AND message_id=?',
                                             (status, reply.chat_id, reply.message_id))
                store.close(); before = hashlib.sha256(db.read_bytes()).digest()
                check_before_recovery(db)
                self.assertEqual(hashlib.sha256(db.read_bytes()).digest(), before)

    def test_only_explicit_prewrite_attachment_status_passes(self):
        import hashlib
        from tg_triage.store import Store
        from tg_triage.rules_pending_store import check_before_recovery, RulesHalt
        for status in ('prewrite_error', 'error', 'ambiguous', 'uploading'):
            with self.subTest(status=status):
                db = self.root / ('upload-' + status + '.sqlite3')
                store = Store(db)
                ident = store.reserve_attachment_call('case', 10, 'asset', 10)
                store.finish_attachment(ident, status)
                store.close(); before = hashlib.sha256(db.read_bytes()).digest()
                if status == 'prewrite_error':
                    check_before_recovery(db)
                else:
                    with self.assertRaises(RulesHalt): check_before_recovery(db)
                self.assertEqual(hashlib.sha256(db.read_bytes()).digest(), before)

    def test_legacy_preflight_inventory_is_read_only_and_never_claims_migration(self):
        import hashlib
        from tg_triage.legacy_preflight import inspect
        from tg_triage.store import Store
        from tests.helpers import message
        db=self.root/'inventory.sqlite3'; store=Store(db)
        source=message(99,text='private content'); store.enqueue(source)
        ident=store.reserve_attachment_call('case',99,'private-name',10)
        store.finish_attachment(ident,'error'); store.close()
        before=hashlib.sha256(db.read_bytes()).digest()
        result=inspect(db)
        self.assertEqual(result['recovery_gate'],'HOLD')
        self.assertFalse(result['migration_performed'])
        self.assertEqual(result['counts']['legacy_messages'],1)
        self.assertEqual(result['counts']['attachments'],1)
        self.assertNotIn('private',json.dumps(result))
        self.assertEqual(hashlib.sha256(db.read_bytes()).digest(),before)

    def test_offline_attachment_evidence_separates_prewrite_from_historical_error(self):
        import hashlib
        from tg_triage.legacy_preflight import attachment_evidence
        from tg_triage.store import Store
        db=self.root/'attachment-evidence.sqlite3'; store=Store(db)
        for status in ('prewrite_error','error','uploading'):
            ident=store.reserve_attachment_call('case',99,'private-name',10,chat_id=-1001234567890)
            store.finish_attachment(ident,status)
        store.close(); before=hashlib.sha256(db.read_bytes()).digest()
        result=attachment_evidence(db,limit=2)
        self.assertEqual(['proven_prewrite','outcome_unresolved'],[row['evidence'] for row in result['items']])
        self.assertTrue(result['truncated'])
        self.assertFalse(result['migration_performed'])
        self.assertNotIn('private',json.dumps(result))
        self.assertEqual(before,hashlib.sha256(db.read_bytes()).digest())

    def test_release_read_error_after_release_setup_is_ambiguous(self):
        from tg_triage.attachments import AttachmentProcessor, AttachmentResult, AttachmentAmbiguous, ReleaseReadError
        from tg_triage.store import Store
        from tests.helpers import config, message
        class Uploader:
            def ensure_release(self, tag): pass
            def upload(self, tag, path, name): raise ReleaseReadError('late failure')
        store=Store(':memory:'); self.addCleanup(store.close)
        processor=AttachmentProcessor(config().attachments,store,repo='sandbox/triage',uploader=Uploader(),strict_uploads=True)
        with self.assertRaises(AttachmentAmbiguous):
            processor._upload('case',[(message(42),self.root/'photo.jpg')],AttachmentResult())
        self.assertEqual(store.connection.execute('SELECT status FROM attachments').fetchone()[0],'ambiguous')

    def test_late_release_error_still_halts_when_ledger_update_fails(self):
        from tg_triage.attachments import AttachmentProcessor, AttachmentResult, ReleaseReadError
        from tg_triage.rules_pending_store import RulesHalt
        from tg_triage.store import Store
        from tests.helpers import config, message
        class Uploader:
            def ensure_release(self, tag): pass
            def upload(self, tag, path, name): raise ReleaseReadError('late error')
        for document in (False,True):
            with self.subTest(document=document):
                store=Store(':memory:'); self.addCleanup(store.close)
                processor=AttachmentProcessor(config().attachments,store,repo='sandbox/triage',uploader=Uploader(),strict_uploads=True)
                with patch.object(store,'finish_attachment',side_effect=OSError('ledger unavailable')):
                    with self.assertRaises(RulesHalt):
                        if document:
                            processor._upload_documents('case',[(message(42),self.root/'file.txt')],AttachmentResult(),{'ensured':False})
                        else:
                            processor._upload('case',[(message(42),self.root/'photo.jpg')],AttachmentResult())
                self.assertEqual(store.connection.execute('SELECT status FROM attachments').fetchone()[0],'uploading')

    def test_prewrite_lookup_error_with_failed_ledger_still_halts(self):
        from tg_triage.attachments import AttachmentProcessor, AttachmentResult, ReleaseReadError
        from tg_triage.rules_pending_store import RulesHalt
        from tg_triage.store import Store
        from tests.helpers import config, message
        class Uploader:
            def ensure_release(self, tag): raise ReleaseReadError('lookup failed before write')
            def upload(self, tag, path, name): raise AssertionError('upload must not run')
        for document in (False,True):
            with self.subTest(document=document):
                store=Store(':memory:'); self.addCleanup(store.close)
                processor=AttachmentProcessor(config().attachments,store,repo='sandbox/triage',uploader=Uploader(),strict_uploads=True)
                with patch.object(store,'finish_attachment',side_effect=OSError('ledger unavailable')):
                    with self.assertRaises(RulesHalt):
                        if document:
                            processor._upload_documents('case',[(message(42),self.root/'file.txt')],AttachmentResult(),{'ensured':False})
                        else:
                            processor._upload('case',[(message(42),self.root/'photo.jpg')],AttachmentResult())
                self.assertEqual(store.connection.execute('SELECT status FROM attachments').fetchone()[0],'uploading')

    def test_interrupted_deferred_reply_survives_generic_retry_limit(self):
        from tg_triage.store import Store
        from tg_triage.rules_pending_store import RulesPendingStore, check_before_recovery
        from tests.helpers import message
        db=self.root/'deferred-retry.sqlite3'; store=Store(db); pending=RulesPendingStore(store)
        source=message(1); store.enqueue(source); pending.create('topic',source.chat_id,source.message_id)
        seq=pending.reserve_notice('topic',-1009876543210,'question'); pending.finish_notice('topic',seq,70)
        self.assertTrue(pending.claim_topic('topic',pending.topic_snapshot('topic')))
        reply=message(71,chat_id=-1009876543210,reply_to_id=70)
        self.assertTrue(pending.accept_ops_message(reply,7))
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='classifying',attempts=2 WHERE chat_id=? AND message_id=?",(reply.chat_id,reply.message_id))
        store.close(); check_before_recovery(db)
        store=Store(db); self.addCleanup(store.close)
        self.assertEqual(store.get(reply.chat_id,reply.message_id)['decision'],'pending_review')
        self.assertEqual(store.get(reply.chat_id,reply.message_id)['reason'],'rules_reply_deferred_during_effect')

    def test_deferred_reply_with_github_reservation_still_blocks(self):
        from tg_triage.store import Store
        from tg_triage.rules_pending_store import RulesPendingStore, check_before_recovery, RulesHalt
        from tests.helpers import message
        db=self.root/'deferred-reserved.sqlite3'; store=Store(db); pending=RulesPendingStore(store)
        source=message(1); store.enqueue(source); pending.create('topic',source.chat_id,source.message_id)
        seq=pending.reserve_notice('topic',-1009876543210,'question'); pending.finish_notice('topic',seq,70)
        pending.claim_topic('topic',pending.topic_snapshot('topic'))
        reply=message(71,chat_id=-1009876543210,reply_to_id=70); pending.accept_ops_message(reply,7)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='classifying',github_called_at='2026-01-01' WHERE chat_id=? AND message_id=?",(reply.chat_id,reply.message_id))
        store.close()
        with self.assertRaises(RulesHalt): check_before_recovery(db)

    def test_unconfirmed_case_or_group_post_is_not_silently_allowed(self):
        import hashlib
        import sqlite3
        from tg_triage.rules_pending_store import check_before_recovery, RulesHalt
        for table in ('cases','groups'):
            with self.subTest(table=table):
                db=self.root/(table+'-post.sqlite3')
                with contextlib.closing(sqlite3.connect(db)) as connection:
                    with connection:
                        connection.execute(f'CREATE TABLE {table}(status TEXT, github_called_at TEXT, issue_number INTEGER)')
                        connection.execute(f"INSERT INTO {table} VALUES('error','2026-01-01',NULL)")
                before=hashlib.sha256(db.read_bytes()).digest()
                with self.assertRaises(RulesHalt): check_before_recovery(db)
                self.assertEqual(hashlib.sha256(db.read_bytes()).digest(),before)

    def test_legacy_choice_offer_is_reported_and_blocks_without_mutation(self):
        import hashlib
        from tg_triage.legacy_preflight import inspect
        from tg_triage.rules_pending_store import check_before_recovery, RulesHalt
        from tg_triage.store import Store
        db=self.root/'legacy-offer.sqlite3'; store=Store(db); store.enable_autonomy()
        with store.connection:
            store.connection.execute('INSERT INTO autonomous_plans VALUES(?,?,?,?,?,?)',
                (1,2,'[2]','case',json.dumps({'action':'ANALYZE','options':['a','b']}),'now'))
        store.close(); before=hashlib.sha256(db.read_bytes()).digest()
        with self.assertRaises(RulesHalt): check_before_recovery(db)
        report=inspect(db)
        self.assertEqual(report['recovery_gate'],'HOLD')
        self.assertEqual(report['counts']['legacy_offers_or_unreadable_plans'],1)
        self.assertEqual(hashlib.sha256(db.read_bytes()).digest(),before)

    def test_wheel_includes_decision_and_draft_extension_files(self):
        builder = ROOT.parent / 'deploy' / 'build_wheel.py'
        if not builder.is_file():
            # Deploy stages ship only the package tree; there verify_stage binds the built wheel instead.
            self.skipTest('deploy/build_wheel.py absent outside a repository checkout')
        spec = importlib.util.spec_from_file_location('build_wheel', builder)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _, data = module.wheel_bytes(ROOT, 'listener', 'test')
        with zipfile.ZipFile(io.BytesIO(data)) as wheel:
            names = {name.rsplit('/', 1)[-1] for name in wheel.namelist() if '/share/tg-triage/rules/' in name}
        self.assertEqual(names, {name + '.md' for name in FILES + EXTENSION_FILES})


class ExpiredStartupGuardTests(unittest.TestCase):
    """Restart the API-produced NEED_INFO + CLOSED batch shape from 2026-11-12."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def _expired_need_info_db(self, name, *, expire=True):
        from dataclasses import replace
        from datetime import timedelta
        from types import SimpleNamespace
        from unittest.mock import Mock
        from tests.helpers import NOW, config, message
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        clock = [NOW]
        settings = replace(config(), mode='live', autonomous_enabled=True, decision_rules_enabled=True,
                           telegram=replace(config().telegram, ops_chat_id=-1009876543210))
        snapshot = _load_decision_rules()
        source = message(8, chat_id=-1003936738808, sender_id=17, text='nút này lỗi')
        provider = SimpleNamespace(settings=SimpleNamespace(model='test'), think=False,
                                   _ask=Mock(side_effect=lambda body, parse, attempts: parse(json.dumps({
                                       'groups': [{'message_ids': [8], 'facts': {
                                           'intent': {'value': 'bug', 'source_ids': [8]},
                                           'object': {'value': 'nút này', 'source_ids': [8]}}}]}))))
        sender = Mock(return_value=3)
        github = Mock()
        db = self.root / name
        store = Store(db, clock=lambda: clock[0])
        try:
            pipeline = Pipeline(settings, store, provider, github, rules_snapshot=snapshot, notice_sender=sender)
            result = pipeline.process(source)
            self.assertEqual((result.decision, result.reason), ('pending_review', 'R12'))
            self.assertEqual(provider._ask.call_count, 1)
            sender.assert_called_once()
            self.assertEqual(github.mock_calls, [])
            topic = f'{source.chat_id}:8'
            self.assertEqual([tuple(row) for row in store.connection.execute(
                'SELECT topic_id,status,phase,attempts FROM rules_topics ORDER BY topic_id')],
                [(topic, 'OPEN', 'NEW', 0), (f'batch:{topic}', 'CLOSED', 'WAITING', 0)])
            self.assertEqual(tuple(store.connection.execute(
                'SELECT kind,status,notice_id FROM rules_notices').fetchone()), ('question', 'sent', 3))
            self.assertEqual(tuple(store.connection.execute(
                'SELECT decision,attempts,github_called_at,issue_number,issue_url FROM messages').fetchone()),
                ('pending_review', 1, None, None, None))
            self.assertEqual(pipeline.rules_pending.sources(topic), {1: (source.chat_id, 8)})
            self.assertEqual(pipeline.rules_pending.sources(f'batch:{topic}'), {1: (source.chat_id, 8)})
            clock[0] += timedelta(days=snapshot.limits['pending_expiry_days'])
            if expire is True:
                self.assertEqual(pipeline.rules_pending.expire_due(snapshot.limits['pending_expiry_days']), 1)
            elif expire is False:
                self.assertTrue(pipeline.rules_pending.close(topic, 'CLOSED'))
        finally:
            store.close()
        provider._ask.reset_mock(side_effect=True)
        provider._ask.side_effect = AssertionError('expired topic must not call the model')
        sender.reset_mock()
        return db, settings, snapshot, source, clock, provider, sender, github

    def test_expired_need_info_restart_and_sweep_are_settled_readonly(self):
        import sqlite3
        from tg_triage.legacy_preflight import inspect
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_pending_store import check_before_recovery, local_binding_blockers
        from tg_triage.store import Store
        db, settings, snapshot, source, clock, provider, sender, github = self._expired_need_info_db('expired.sqlite3')
        with contextlib.closing(sqlite3.connect(db)) as connection:
            before = '\n'.join(connection.iterdump()).encode('utf8')
            file_before = db.read_bytes()
            check_before_recovery(db)
            self.assertEqual('\n'.join(connection.iterdump()).encode('utf8'), before)
            self.assertEqual(db.read_bytes(), file_before)
            self.assertEqual(local_binding_blockers(connection), [])
        report = inspect(db)
        self.assertEqual(report['recovery_gate'], 'CLEAR')
        # Raw pending counts describe stored rows, not recovery safety or conversion authority.
        self.assertEqual(report['counts']['legacy_messages'], 1)
        store = Store(db, clock=lambda: clock[0]); self.addCleanup(store.close)
        pipeline = Pipeline(settings, store, provider, github, rules_snapshot=snapshot, notice_sender=sender)
        self.assertFalse(pipeline.rules_pending.unresolved_ambiguity())
        notices = [tuple(row) for row in store.connection.execute('SELECT * FROM rules_notices')]
        decisions = [tuple(row) for row in store.connection.execute('SELECT * FROM rules_decisions')]
        self.assertEqual(pipeline.close_due_cases(), [])
        self.assertEqual([tuple(row) for row in store.connection.execute('SELECT * FROM rules_notices')], notices)
        self.assertEqual([tuple(row) for row in store.connection.execute('SELECT * FROM rules_decisions')], decisions)
        self.assertEqual(store.get(source.chat_id, source.message_id)['decision'], 'pending_review')
        provider._ask.assert_not_called()
        sender.assert_not_called()
        self.assertEqual(github.mock_calls, [])

    def test_first_sweep_expires_due_need_info_and_next_restart_is_clear(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_pending_store import check_before_recovery
        from tg_triage.store import Store
        db, settings, snapshot, source, clock, provider, sender, github = self._expired_need_info_db(
            'due.sqlite3', expire=None)
        check_before_recovery(db)
        store = Store(db, clock=lambda: clock[0])
        try:
            pipeline = Pipeline(settings, store, provider, github, rules_snapshot=snapshot, notice_sender=sender)
            topic = f'{source.chat_id}:8'
            self.assertEqual(pipeline.rules_pending.topic_snapshot(topic)['status'], 'OPEN')
            self.assertEqual(pipeline.close_due_cases(), [])
            self.assertEqual(pipeline.rules_pending.topic_snapshot(topic)['status'], 'EXPIRED')
            provider._ask.assert_not_called()
            sender.assert_not_called()
            self.assertEqual(github.mock_calls, [])
        finally:
            store.close()
        check_before_recovery(db)

    def test_new_message_from_same_sender_after_expiry_starts_new_topic(self):
        from dataclasses import replace
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_pending_store import check_before_recovery
        from tg_triage.store import Store
        db, settings, snapshot, source, clock, provider, sender, github = self._expired_need_info_db('new.sqlite3')
        check_before_recovery(db)
        store = Store(db, clock=lambda: clock[0]); self.addCleanup(store.close)
        pipeline = Pipeline(settings, store, provider, github, rules_snapshot=snapshot, notice_sender=sender)
        provider._ask.side_effect = lambda body, parse, attempts: parse(json.dumps({
            'groups': [{'message_ids': [9], 'facts': {
                'intent': {'value': 'bug', 'source_ids': [9]},
                'object': {'value': 'nút khác', 'source_ids': [9]}}}]}))
        sender.return_value = 4
        new = replace(source, message_id=9, received_at=clock[0], text='nút khác lỗi')
        result = pipeline.process(new)
        self.assertEqual((result.decision, result.reason), ('pending_review', 'R12'))
        old_topic, new_topic = f'{source.chat_id}:8', f'{source.chat_id}:9'
        self.assertEqual(pipeline.rules_pending.topic_snapshot(old_topic)['status'], 'EXPIRED')
        self.assertEqual(pipeline.rules_pending.sources(old_topic), {1: (source.chat_id, 8)})
        self.assertEqual(pipeline.rules_pending.topic_snapshot(new_topic)['status'], 'OPEN')
        self.assertEqual(pipeline.rules_pending.sources(new_topic), {1: (source.chat_id, 9)})
        provider._ask.assert_called_once()
        sender.assert_called_once()
        self.assertEqual(github.mock_calls, [])

    def test_expired_exemption_is_narrow(self):
        import sqlite3
        from tg_triage.rules_pending_store import check_before_recovery, RulesHalt
        cases = {
            'issue_number': dict(issue_number=7),
            'issue_url': dict(issue_url='https://github.com/sandbox/triage/issues/7'),
            'github_called': dict(github_called_at='2026-11-05T04:46:50+00:00'),
            'github_with_issue': dict(github_called_at='2026-11-05T04:46:50+00:00', issue_number=7,
                                      issue_url='https://github.com/sandbox/triage/issues/7'),
            **{state: dict(decision=state) for state in ('queued', 'classifying', 'in_case', 'pending')},
            'closed_only': {},
        }
        for label, changes in cases.items():
            with self.subTest(label):
                db, *_ = self._expired_need_info_db(f'narrow-{label}.sqlite3', expire=label != 'closed_only')
                with contextlib.closing(sqlite3.connect(db)) as connection:
                    with connection:
                        if changes:
                            connection.execute('UPDATE messages SET ' + ','.join(f'{key}=?' for key in changes),
                                               tuple(changes.values()))
                    before = '\n'.join(connection.iterdump()).encode('utf8')
                    file_before = db.read_bytes()
                    with self.assertRaises(RulesHalt): check_before_recovery(db)
                    self.assertEqual('\n'.join(connection.iterdump()).encode('utf8'), before)
                    self.assertEqual(db.read_bytes(), file_before)

    def test_expired_source_does_not_bypass_other_binding_or_notice_blockers(self):
        from tg_triage.rules_pending_store import check_before_recovery, RulesHalt, RulesPendingStore
        from tg_triage.store import Store
        for label in ('orphan_binding', 'reserved_notice'):
            with self.subTest(label):
                db, settings, snapshot, source, clock, *_ = self._expired_need_info_db(f'blocked-{label}.sqlite3')
                store = Store(db, clock=lambda: clock[0])
                try:
                    pending = RulesPendingStore(store)
                    pending.create('other-topic', source.chat_id, source.message_id)
                    if label == 'reserved_notice':
                        self.assertIsNotNone(pending.reserve_notice('other-topic', settings.telegram.ops_chat_id, 'escalation'))
                    else:
                        # Corruption cannot be produced by the API: retain the source but remove its owner.
                        with store.connection:
                            store.connection.execute("DELETE FROM rules_topics WHERE topic_id='other-topic'")
                finally:
                    store.close()
                with self.assertRaises(RulesHalt): check_before_recovery(db)

    def test_expired_source_with_escalated_binding_still_requires_sent_escalation(self):
        from tg_triage.rules_pending_store import check_before_recovery, RulesHalt, RulesPendingStore
        from tg_triage.store import Store
        for notice in (None, 'question', 'escalation'):
            with self.subTest(notice=notice):
                db, settings, snapshot, source, clock, *_ = self._expired_need_info_db(f'mixed-{notice}.sqlite3')
                store = Store(db, clock=lambda: clock[0])
                try:
                    pending = RulesPendingStore(store)
                    pending.create('other-escalated', source.chat_id, source.message_id)
                    if notice is not None:
                        sequence = pending.reserve_notice('other-escalated', settings.telegram.ops_chat_id, notice)
                        self.assertIsNotNone(sequence)
                        self.assertTrue(pending.finish_notice('other-escalated', sequence, 4))
                    self.assertTrue(pending.close('other-escalated', 'ESCALATED'))
                finally:
                    store.close()
                if notice == 'escalation':
                    check_before_recovery(db)
                else:
                    with self.assertRaisesRegex(RulesHalt, 'legacy pending'): check_before_recovery(db)
