"""Operator transitions use local throwaway SQLite fixtures only."""
import hashlib
import tempfile
import unittest
from pathlib import Path

from tg_triage.legacy_reconciliation import transition_pristine, release_pristine
from tg_triage.rules_pending_store import RulesHalt, check_before_recovery, RulesPendingStore
from tg_triage.store import Store
from tests.helpers import message


class LegacyReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'offline.sqlite3'
        store=Store(self.db); store.enqueue(message(42)); store.close()

    def manifest(self):
        return {'offline_copy':True,'sha256':hashlib.sha256(self.db.read_bytes()).hexdigest(),
                'chat_id':message(42).chat_id,'message_id':42,'decision':'queued',
                'operator':'test operator','reason':'inspected unstarted offline fixture'}

    def test_attested_pristine_source_is_bound_once_without_external_effect(self):
        result=transition_pristine(self.db,self.manifest())
        self.assertTrue(result['transitioned']); self.assertFalse(result['external_effects_attempted'])
        check_before_recovery(self.db)
        store=Store(self.db); self.addCleanup(store.close)
        pending=RulesPendingStore(store)
        self.assertEqual('LEGACY_REVIEW',store.connection.execute('SELECT phase FROM rules_topics').fetchone()[0])
        self.assertEqual({1:(message(42).chat_id,42)},pending.sources(result['topic_id']))
        self.assertEqual('pending_review',store.get(message(42).chat_id,42)['decision'])
        self.assertEqual(1,store.connection.execute('SELECT count(*) FROM rules_legacy_transitions').fetchone()[0])
        from datetime import datetime, timezone
        store.clock=lambda:datetime(2040,1,1,tzinfo=timezone.utc)
        self.assertEqual(0,pending.expire_due(1))
        self.assertEqual([],pending.hold_topics())
        store.close()
        with self.assertRaises(RulesHalt): transition_pristine(self.db,self.manifest())

    def test_canonical_immutable_legacy_triggers_are_supported(self):
        store=Store(self.db); store.enable_autonomy(); store.close()
        receipt=transition_pristine(self.db,self.manifest())
        release={'offline_copy':True,'sha256':hashlib.sha256(self.db.read_bytes()).hexdigest(),'chat_id':message(42).chat_id,
            'message_id':42,'operator':'test','reason':'reviewed','release_for_rules':True,'stage_receipt_sha256':receipt['stage_receipt_sha256']}
        self.assertTrue(release_pristine(self.db,release)['released'])

    def test_spoofed_canonical_trigger_is_not_trusted(self):
        import sqlite3
        connection=sqlite3.connect(self.db)
        with connection:
            connection.execute("CREATE TRIGGER autonomous_routes_no_update AFTER UPDATE ON messages BEGIN SELECT 1; END")
        connection.close(); before=self.db.read_bytes()
        with self.assertRaises(RulesHalt): transition_pristine(self.db,self.manifest())
        self.assertEqual(before,self.db.read_bytes())

    def test_cli_legacy_commands_run_via_main_entrypoint(self):
        import io
        import json
        from unittest.mock import patch
        from tg_triage.__main__ import main
        # 1. Test CLI legacy-preflight
        out = io.StringIO()
        with patch('sys.stdout', out):
            code = main(['legacy-preflight', '--db', str(self.db), '--attachment-evidence'])
        self.assertEqual(0, code)
        report = json.loads(out.getvalue())
        self.assertTrue(report['preflight_only'])
        self.assertIn('attachment_evidence', report)

        # 2. Test CLI legacy-transition
        manifest_path = Path(self.temp.name) / 'manifest_stage.json'
        manifest_data = self.manifest()
        manifest_path.write_text(json.dumps(manifest_data, ensure_ascii=False), encoding='utf-8')
        out = io.StringIO()
        with patch('sys.stdout', out):
            code = main(['legacy-transition', '--db', str(self.db), '--manifest', str(manifest_path)])
        self.assertEqual(0, code)
        transition_result = json.loads(out.getvalue())
        self.assertTrue(transition_result['transitioned'])
        self.assertIn('stage_receipt_sha256', transition_result)

        # 3. Test CLI legacy-release
        fresh_sha = hashlib.sha256(self.db.read_bytes()).hexdigest()
        release_path = Path(self.temp.name) / 'manifest_release.json'
        release_data = {
            'offline_copy': True,
            'sha256': fresh_sha,
            'chat_id': message(42).chat_id,
            'message_id': 42,
            'operator': 'cli-operator',
            'reason': 'cli-test',
            'release_for_rules': True,
            'stage_receipt_sha256': transition_result['stage_receipt_sha256'],
            'reviewed_context': True,
        }
        release_path.write_text(json.dumps(release_data, ensure_ascii=False), encoding='utf-8')
        out = io.StringIO()
        with patch('sys.stdout', out):
            code = main(['legacy-release', '--db', str(self.db), '--manifest', str(release_path)])
        self.assertEqual(0, code)
        release_result = json.loads(out.getvalue())
        self.assertTrue(release_result['released'])

    def test_default_topic_registry_can_be_staged_but_advanced_cursor_cannot(self):
        from tg_triage.topic_store import init_registry
        store=Store(self.db); init_registry(store.connection); store.close()
        receipt=transition_pristine(self.db,self.manifest())
        release={'offline_copy':True,'sha256':hashlib.sha256(self.db.read_bytes()).hexdigest(),'chat_id':message(42).chat_id,
            'message_id':42,'operator':'test','reason':'reviewed','release_for_rules':True,'stage_receipt_sha256':receipt['stage_receipt_sha256']}
        self.assertTrue(release_pristine(self.db,release)['released'])
        db=Path(self.temp.name)/'advanced.sqlite3'; store=Store(db); store.enqueue(message(42)); init_registry(store.connection)
        with store.connection: store.connection.execute('UPDATE triage_topic_hydration SET last_case_id=1')
        store.close(); before=db.read_bytes()
        manifest={'offline_copy':True,'sha256':hashlib.sha256(before).hexdigest(),'chat_id':message(42).chat_id,
            'message_id':42,'decision':'queued','operator':'test','reason':'reviewed'}
        with self.assertRaises(RulesHalt): transition_pristine(db,manifest)
        self.assertEqual(before,db.read_bytes())

    def test_separate_release_is_explicit_and_only_arms_rules_hold(self):
        self.receipt=transition_pristine(self.db,self.manifest())
        release={'offline_copy':True,'sha256':hashlib.sha256(self.db.read_bytes()).hexdigest(),
            'chat_id':message(42).chat_id,'message_id':42,'operator':'test','reason':'reviewed source',
            'release_for_rules':True,'stage_receipt_sha256':self.receipt['stage_receipt_sha256'],'reviewed_context':True}
        self.assertFalse(release_pristine(self.db,release)['external_effects_attempted'])
        store=Store(self.db); self.addCleanup(store.close)
        pending=RulesPendingStore(store)
        self.assertEqual([f'{message(42).chat_id}:42'],pending.hold_topics())
        self.assertEqual(1,store.connection.execute('SELECT count(*) FROM rules_legacy_releases').fetchone()[0])

    def test_inert_multi_message_history_is_preserved_and_bound_at_release(self):
        store=Store(self.db)
        for mid,decision in ((10,'chat'),(11,'noise')):
            store.enqueue(message(mid,text='old inert text'))
            with store.connection:
                store.connection.execute('UPDATE messages SET decision=?,reason=? WHERE message_id=?',(decision,'original',mid))
        store.close()
        with self.assertRaises(RulesHalt): transition_pristine(self.db,self.manifest())
        self.receipt=transition_pristine(self.db,{**self.manifest(),'reviewed_context':True})
        release={'offline_copy':True,'sha256':hashlib.sha256(self.db.read_bytes()).hexdigest(),
            'chat_id':message(42).chat_id,'message_id':42,'operator':'test','reason':'reviewed all sources','release_for_rules':True,'stage_receipt_sha256':self.receipt['stage_receipt_sha256'],'reviewed_context':True}
        self.assertTrue(release_pristine(self.db,release)['released'])
        store=Store(self.db); self.addCleanup(store.close)
        rows=store.connection.execute('SELECT message_id,decision,reason FROM messages ORDER BY message_id').fetchall()
        self.assertEqual([(10,'chat','original'),(11,'noise','original'),(42,'pending_review','legacy_reconciled_pending')],[tuple(r) for r in rows])

    def test_inert_history_cannot_be_altered_after_staging(self):
        store=Store(self.db); store.enqueue(message(10,text='old inert text'))
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='chat',reason='original' WHERE message_id=10")
        store.close(); self.receipt=transition_pristine(self.db,{**self.manifest(),'reviewed_context':True})
        import sqlite3
        connection=sqlite3.connect(self.db)
        with connection: connection.execute("UPDATE messages SET reason='altered' WHERE message_id=10")
        connection.close(); before=self.db.read_bytes()
        release={'offline_copy':True,'sha256':hashlib.sha256(before).hexdigest(),
            'chat_id':message(42).chat_id,'message_id':42,'operator':'test','reason':'reviewed','release_for_rules':True,'stage_receipt_sha256':self.receipt['stage_receipt_sha256'],'reviewed_context':True}
        with self.assertRaises(RulesHalt): release_pristine(self.db,release)
        self.assertEqual(before,self.db.read_bytes())

    def test_external_receipt_rejects_rewritten_internal_evidence_hash(self):
        self.receipt=transition_pristine(self.db,self.manifest())
        import sqlite3
        from tg_triage.legacy_reconciliation import _source_evidence
        connection=sqlite3.connect(self.db)
        with connection:
            connection.execute("UPDATE messages SET text='altered' WHERE message_id=42")
            tables={r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            forged=_source_evidence(connection,tables,(message(42).chat_id,42))
            connection.execute('UPDATE rules_legacy_transitions SET source_evidence_sha256=?',(forged,))
        connection.close(); before=self.db.read_bytes()
        release={'offline_copy':True,'sha256':hashlib.sha256(before).hexdigest(),'chat_id':message(42).chat_id,'message_id':42,
            'operator':'test','reason':'reviewed','release_for_rules':True,'stage_receipt_sha256':self.receipt['stage_receipt_sha256']}
        with self.assertRaises(RulesHalt): release_pristine(self.db,release)
        self.assertEqual(before,self.db.read_bytes())

    def test_release_rolls_back_if_topic_trigger_requeues_staged_source(self):
        self.receipt=transition_pristine(self.db,self.manifest())
        import sqlite3
        connection=sqlite3.connect(self.db)
        with connection:
            connection.execute("""CREATE TRIGGER requeue_on_release AFTER UPDATE OF phase ON rules_topics
                WHEN NEW.phase='HOLD' BEGIN UPDATE messages SET decision='queued' WHERE message_id=42; END""")
        connection.close(); before=self.db.read_bytes()
        release={'offline_copy':True,'sha256':hashlib.sha256(before).hexdigest(),
            'chat_id':message(42).chat_id,'message_id':42,'operator':'test','reason':'reviewed','release_for_rules':True,'stage_receipt_sha256':self.receipt['stage_receipt_sha256'],'reviewed_context':True}
        with self.assertRaises(RulesHalt): release_pristine(self.db,release)
        connection=sqlite3.connect(self.db)
        self.assertEqual(('pending_review','legacy_reconciled_pending'),connection.execute('SELECT decision,reason FROM messages WHERE message_id=42').fetchone())
        self.assertEqual(('LEGACY_REVIEW',),connection.execute('SELECT phase FROM rules_topics').fetchone())
        connection.close()

    def test_release_rejects_trigger_injected_source_binding(self):
        store=Store(self.db); store.enqueue(message(10,text='historical'))
        with store.connection: store.connection.execute("UPDATE messages SET decision='chat' WHERE message_id=10")
        store.close(); self.receipt=transition_pristine(self.db,{**self.manifest(),'reviewed_context':True})
        import sqlite3
        connection=sqlite3.connect(self.db)
        with connection:
            connection.execute("""CREATE TRIGGER inject_source AFTER UPDATE OF phase ON rules_topics
                WHEN NEW.phase='HOLD' BEGIN INSERT INTO rules_sources VALUES(NEW.topic_id,2,NEW.source_chat_id,10); END""")
        connection.close(); before=self.db.read_bytes()
        release={'offline_copy':True,'sha256':hashlib.sha256(before).hexdigest(),'chat_id':message(42).chat_id,'message_id':42,
            'operator':'test','reason':'reviewed','release_for_rules':True,'reviewed_context':True,
            'stage_receipt_sha256':self.receipt['stage_receipt_sha256']}
        with self.assertRaises(RulesHalt): release_pristine(self.db,release)
        connection=sqlite3.connect(self.db)
        self.assertEqual(1,connection.execute('SELECT count(*) FROM rules_sources').fetchone()[0]); connection.close()

    def test_external_receipt_binds_staging_attestation(self):
        self.receipt=transition_pristine(self.db,self.manifest())
        import sqlite3
        connection=sqlite3.connect(self.db)
        with connection: connection.execute("UPDATE rules_legacy_transitions SET attestation=replace(attestation,'test operator','other person')")
        connection.close(); before=self.db.read_bytes()
        release={'offline_copy':True,'sha256':hashlib.sha256(before).hexdigest(),'chat_id':message(42).chat_id,'message_id':42,
            'operator':'test','reason':'reviewed','release_for_rules':True,'stage_receipt_sha256':self.receipt['stage_receipt_sha256']}
        with self.assertRaises(RulesHalt): release_pristine(self.db,release)

    def test_release_rejects_changed_topic_attempt_state(self):
        self.receipt=transition_pristine(self.db,self.manifest())
        import sqlite3
        connection=sqlite3.connect(self.db)
        with connection: connection.execute('UPDATE rules_topics SET attempts=999')
        connection.close(); before=self.db.read_bytes()
        release={'offline_copy':True,'sha256':hashlib.sha256(before).hexdigest(),'chat_id':message(42).chat_id,'message_id':42,
            'operator':'test','reason':'reviewed','release_for_rules':True,'stage_receipt_sha256':self.receipt['stage_receipt_sha256']}
        with self.assertRaises(RulesHalt): release_pristine(self.db,release)
        self.assertEqual(before,self.db.read_bytes())

    def test_extra_non_inert_history_cannot_be_staged(self):
        for kind in ('unresolved','linked','attachment'):
            with self.subTest(kind=kind):
                db=Path(self.temp.name)/(kind+'.sqlite3'); store=Store(db)
                source=message(42); store.enqueue(source); store.enqueue(message(10,text='old'))
                with store.connection:
                    store.connection.execute("UPDATE messages SET decision=? WHERE message_id=10",('error' if kind=='unresolved' else 'chat',))
                    if kind=='linked': store.connection.execute('UPDATE messages SET reply_to_id=1 WHERE message_id=10')
                    if kind=='attachment': store.connection.execute('UPDATE messages SET has_image=1 WHERE message_id=10')
                store.close(); before=db.read_bytes()
                manifest={'offline_copy':True,'sha256':hashlib.sha256(before).hexdigest(),'chat_id':source.chat_id,'message_id':42,
                    'decision':'queued','operator':'test','reason':'inspected'}
                with self.assertRaises(RulesHalt): transition_pristine(db,manifest)
                self.assertEqual(before,db.read_bytes())

    def test_reply_to_staged_source_is_held_without_model_or_write(self):
        from types import SimpleNamespace
        from tg_triage.rules_intake import process
        self.receipt=transition_pristine(self.db,self.manifest())
        store=Store(self.db); self.addCleanup(store.close)
        RulesPendingStore(store)
        reply=message(43,text='có thêm thông tin',reply_to_id=42); store.enqueue(reply)
        pipeline=SimpleNamespace(_prepared_images=None,store=store,rules_snapshot=SimpleNamespace(extensions=None),config=SimpleNamespace(telegram=SimpleNamespace(ops_chat_id=999)))
        result=process(pipeline,reply,None)
        self.assertEqual('legacy_transition_awaiting_release',result.reason)
        self.assertEqual('pending_review',store.get(reply.chat_id,reply.message_id)['decision'])
        self.assertEqual('LEGACY_REVIEW',store.connection.execute('SELECT phase FROM rules_topics').fetchone()[0])
        store.close()
        before=self.db.read_bytes()
        release={'offline_copy':True,'sha256':hashlib.sha256(before).hexdigest(),
            'chat_id':reply.chat_id,'message_id':42,'operator':'test','reason':'reviewed source','release_for_rules':True,'stage_receipt_sha256':self.receipt['stage_receipt_sha256'],'reviewed_context':True}
        with self.assertRaises(RulesHalt): release_pristine(self.db,release)
        self.assertEqual(before,self.db.read_bytes())

    def test_uncertain_or_changed_history_is_not_transitioned(self):
        store=Store(self.db)
        ident=store.reserve_attachment_call('case',42,'file',1); store.finish_attachment(ident,'error')
        store.close(); before=self.db.read_bytes()
        with self.assertRaises(RulesHalt): transition_pristine(self.db,self.manifest())
        self.assertEqual(before,self.db.read_bytes())

    def test_missing_legacy_reply_parent_is_never_released_as_new_work(self):
        store=Store(self.db)
        with store.connection:
            store.connection.execute('UPDATE messages SET reply_to_id=10 WHERE message_id=42')
        store.close(); before=self.db.read_bytes()
        with self.assertRaises(RulesHalt): transition_pristine(self.db,self.manifest())
        self.assertEqual(before,self.db.read_bytes())
        manifest=self.manifest(); manifest['sha256']='0'*64
        with self.assertRaises(RulesHalt): transition_pristine(self.db,manifest)
        self.assertEqual(before,self.db.read_bytes())

    def test_attested_prewrite_attachment_can_be_staged_but_historical_error_cannot(self):
        for status in ('prewrite_error','error'):
            with self.subTest(status=status):
                db=Path(self.temp.name)/(status+'.sqlite3'); store=Store(db)
                source=message(42,has_image=True); store.enqueue(source)
                ident=store.reserve_attachment_call('case',42,'fixture.jpg',2,chat_id=source.chat_id)
                store.finish_attachment(ident,status); store.close()
                before=db.read_bytes()
                manifest={'offline_copy':True,'sha256':hashlib.sha256(before).hexdigest(),'chat_id':source.chat_id,
                    'message_id':42,'decision':'queued','operator':'test','reason':'checked prewrite evidence',
                    'prewrite_attachment_ids':[ident]}
                if status=='error':
                    with self.assertRaises(RulesHalt): transition_pristine(db,manifest)
                    self.assertEqual(before,db.read_bytes())
                    continue
                self.receipt=transition_pristine(db,manifest)
                self.assertTrue(self.receipt['transitioned'])
                release={'offline_copy':True,'sha256':hashlib.sha256(db.read_bytes()).hexdigest(),'chat_id':source.chat_id,
                    'message_id':42,'operator':'test','reason':'reviewed prewrite evidence','release_for_rules':True,'stage_receipt_sha256':self.receipt['stage_receipt_sha256'],'reviewed_context':True}
                with self.assertRaises(RulesHalt): release_pristine(db,release)
                store=Store(db); self.addCleanup(store.close)
                self.assertEqual('prewrite_error',store.connection.execute('SELECT status FROM attachments WHERE id=?',(ident,)).fetchone()[0])

    def test_prewrite_attachment_requires_exact_chat_identity(self):
        for chat_id in (None,-1002222222222):
            with self.subTest(chat_id=chat_id):
                db=Path(self.temp.name)/('chat-'+str(chat_id)+'.sqlite3'); store=Store(db)
                source=message(42,has_image=True); store.enqueue(source)
                ident=store.reserve_attachment_call('case',42,'fixture.jpg',2,chat_id=chat_id)
                store.finish_attachment(ident,'prewrite_error'); store.close()
                before=db.read_bytes()
                manifest={'offline_copy':True,'sha256':hashlib.sha256(before).hexdigest(),'chat_id':source.chat_id,
                    'message_id':42,'decision':'queued','operator':'test','reason':'inspected','prewrite_attachment_ids':[ident]}
                with self.assertRaises(RulesHalt): transition_pristine(db,manifest)
                self.assertEqual(before,db.read_bytes())

    def test_release_rejects_changed_staged_source_or_attachment(self):
        import sqlite3
        for changed in ('text','asset'):
            with self.subTest(changed=changed):
                db=Path(self.temp.name)/('changed-'+changed+'.sqlite3'); store=Store(db)
                source=message(42,has_image=changed=='asset'); store.enqueue(source)
                ids=[]
                if changed=='asset':
                    ident=store.reserve_attachment_call('case',42,'original.jpg',2,chat_id=source.chat_id)
                    store.finish_attachment(ident,'prewrite_error'); ids=[ident]
                store.close()
                common={'offline_copy':True,'chat_id':source.chat_id,'message_id':42,'operator':'test','reason':'reviewed'}
                manifest={**common,'sha256':hashlib.sha256(db.read_bytes()).hexdigest(),'decision':'queued'}
                if ids: manifest['prewrite_attachment_ids']=ids
                self.receipt=transition_pristine(db,manifest)
                connection=sqlite3.connect(db)
                with connection:
                    if changed=='text': connection.execute("UPDATE messages SET text='changed'")
                    else: connection.execute("UPDATE attachments SET asset_name='changed.jpg'")
                connection.close()
                before=db.read_bytes()
                release={**common,'sha256':hashlib.sha256(before).hexdigest(),'release_for_rules':True,'stage_receipt_sha256':self.receipt['stage_receipt_sha256'],'reviewed_context':True}
                with self.assertRaises(RulesHalt): release_pristine(db,release)
                self.assertEqual(before,db.read_bytes())

    def test_sidecar_created_after_initial_check_aborts_under_lock(self):
        from unittest.mock import patch
        from tg_triage import legacy_reconciliation
        original=legacy_reconciliation._digest; calls=[]
        sidecar=Path(str(self.db)+'-journal')
        def racing_digest(path):
            result=original(path); calls.append(1)
            if len(calls)==2: sidecar.write_bytes(b'fixture')
            return result
        try:
            manifest=self.manifest()
            with patch.object(legacy_reconciliation,'_digest',side_effect=racing_digest):
                with self.assertRaises(RulesHalt): transition_pristine(self.db,manifest)
        finally:
            sidecar.unlink(missing_ok=True)

    def test_ignored_stage_and_release_updates_rollback_without_false_success(self):
        import sqlite3
        connection=sqlite3.connect(self.db)
        with connection:
            connection.execute("""CREATE TRIGGER ignore_stage BEFORE UPDATE OF decision ON messages
                WHEN NEW.reason='legacy_reconciled_pending' BEGIN SELECT RAISE(IGNORE); END""")
        connection.close(); before=self.db.read_bytes()
        with self.assertRaises(RulesHalt): transition_pristine(self.db,self.manifest())
        self.assertEqual(before,self.db.read_bytes())
        connection=sqlite3.connect(self.db)
        with connection: connection.execute('DROP TRIGGER ignore_stage')
        connection.close()
        self.receipt=transition_pristine(self.db,self.manifest())
        connection=sqlite3.connect(self.db)
        with connection:
            connection.execute("""CREATE TRIGGER ignore_release BEFORE UPDATE OF phase ON rules_topics
                WHEN NEW.phase='HOLD' BEGIN SELECT RAISE(IGNORE); END""")
        connection.close(); before=self.db.read_bytes()
        release={'offline_copy':True,'sha256':hashlib.sha256(before).hexdigest(),'chat_id':message(42).chat_id,
            'message_id':42,'operator':'test','reason':'reviewed','release_for_rules':True,'stage_receipt_sha256':self.receipt['stage_receipt_sha256'],'reviewed_context':True}
        with self.assertRaises(RulesHalt): release_pristine(self.db,release)
        self.assertEqual(before,self.db.read_bytes())
