"""Offline inventory/archival regression fixtures; no live DB, models or external APIs."""
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tg_triage.store import Store
from tg_triage.rules_pending_store import RulesHalt,RulesPendingStore,check_before_recovery,local_binding_blockers
from tg_triage.legacy_preflight import inspect,attachment_evidence
from tg_triage.legacy_reconciliation import transition_pristine,release_pristine
from tests.helpers import message


class LegacyPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.db=Path(self.temp.name)/'offline.sqlite3';store=Store(self.db);store.enqueue(message(42));store.close()
        self.addCleanup(patch.stopall)
        patch('socket.socket.connect',side_effect=AssertionError('unexpected network')).start()
        patch('subprocess.Popen',side_effect=AssertionError('unexpected process')).start()

    def change(self,sql,params=()):
        connection=sqlite3.connect(self.db)
        try:
            with connection:connection.execute(sql,params)
        finally:connection.close()

    def attestation(self,**extra):
        return dict(offline_copy=True,sha256=hashlib.sha256(self.db.read_bytes()).hexdigest(),chat_id=message().chat_id,message_id=42,
                    decision='queued',operator='test',reason='explicit offline fixture review',**extra)

    def release(self,receipt):
        value=self.attestation();value.pop('decision')
        return dict(value,release_for_rules=True,stage_receipt_sha256=receipt['stage_receipt_sha256'])

    def test_recovery_clear_does_not_mean_terminal_linked_history_can_convert(self):
        self.change("UPDATE messages SET decision='issue',github_called_at='2026-01-01',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101'")
        before=self.db.read_bytes();result=inspect(self.db)
        self.assertEqual('CLEAR',result['recovery_gate']);self.assertEqual(1,result['inventory']['messages']['terminal'])
        self.assertNotEqual('CANDIDATE_REQUIRES_ATTESTATION',result['conversion']['stage'])
        with self.assertRaises(RulesHalt):transition_pristine(self.db,self.attestation())
        self.assertEqual(before,self.db.read_bytes())

    def test_complete_known_family_registry_and_absent_is_never_zero(self):
        result=inspect(self.db)
        required='messages cases groups reconciliation attachments group_notices model_usage followup_roots followup_jobs followup_operations followup_analysis_retries followup_episodes followup_episode_sources followup_job_sources autonomous_routes autonomous_confirmations autonomous_outages autonomous_clarifications autonomous_plans triage_topic_hydration triage_topics triage_topic_messages triage_topic_events triage_current_topics triage_current_topic_events rules_topics rules_sources rules_ops_replies rules_inflight_topics rules_deferred_ops rules_notices rules_decisions rules_batches rules_batch_groups rules_comments rules_append_completions rules_readonly_notices rules_lifecycle_questions rules_lifecycle_answers rules_related_episodes rules_legacy_transitions rules_legacy_releases thinking_decisions thinking_decision_events rules_related_receipts rules_analysis_reviews rules_analysis_review_members'.split()
        self.assertEqual(set(required),set(result['inventory']))
        self.assertEqual('ABSENT_UNKNOWN',result['inventory']['rules_analysis_reviews']['schema'])
        self.assertIsNone(result['inventory']['rules_analysis_reviews']['total'])
        self.assertFalse(result['inventory']['rules_analysis_reviews']['validation_complete'])
        self.assertFalse(result['inspection']['schema_coverage_complete']);self.assertFalse(result['inspection']['validation_complete'])

    def test_terminal_notice_accounting_root_and_lineage_are_visible_blockers(self):
        cases=[('group_notices',"INSERT INTO group_notices(chat_id,message_id,kind,status,reserved_at,notice_message_id) VALUES(-1001234567890,42,'issue','sent','now',900)"),
               ('model_usage',"INSERT INTO model_usage(ts,provider,model,role,outcome) VALUES(0,'fake','fake','primary','ok')"),
               ('followup_roots',"INSERT INTO followup_roots(root_key,chat_id,source_message_id,case_key) VALUES('r',-1001234567890,42,'old')")]
        previous=self.db
        for table,sql in cases:
            with self.subTest(table=table):
                self.db=Path(self.temp.name)/(table+'.sqlite3');store=Store(self.db);store.enqueue(message(42));store.close();self.change(sql)
                result=inspect(self.db)
                self.assertEqual(1,result['inventory'][table]['total'])
                self.assertNotEqual('CANDIDATE_REQUIRES_ATTESTATION',result['conversion']['stage'])
        self.db=previous

    def test_all_new_receipt_tables_report_orphan_evidence_without_payload(self):
        store=Store(self.db);RulesPendingStore(store);store.close()
        self.change("INSERT INTO rules_analysis_review_members VALUES(-1001234567890,42,'PRIVATE-receipt')")
        result=inspect(self.db)
        self.assertIn('ORPHAN_ANALYSIS_MEMBER',result['binding_codes'])
        self.assertEqual(1,result['inventory']['rules_analysis_review_members']['total'])
        self.assertNotIn('PRIVATE',json.dumps(result))
        with self.assertRaises(RulesHalt):check_before_recovery(self.db)

    def test_unknown_table_names_and_private_invalid_statuses_are_never_echoed(self):
        self.change('CREATE TABLE "PRIVATE-table"(payload TEXT)');self.change('INSERT INTO "PRIVATE-table" VALUES(?)',('PRIVATE-body',))
        store=Store(self.db);ident=store.reserve_attachment_call('case',42,'PRIVATE-name',1,chat_id=message().chat_id);store.close()
        self.change('UPDATE attachments SET status=? WHERE id=?',('PRIVATE-status',ident))
        before=self.db.read_bytes();result=inspect(self.db,include_attachment_evidence=True)
        self.assertEqual(1,result['unsupported_history']['nonempty_tables'])
        self.assertIn('UNKNOWN_STATUS',result['inventory']['attachments']['codes'])
        self.assertEqual('UNKNOWN_STATUS',result['attachment_evidence']['items'][0]['status'])
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertEqual(before,self.db.read_bytes())

    def test_old_message_schema_is_unknown_not_empty_or_clear(self):
        self.db=Path(self.temp.name)/'old.sqlite3';self.change('CREATE TABLE messages(decision TEXT)');self.change("INSERT INTO messages VALUES('chat')")
        result=inspect(self.db)
        self.assertEqual('UNKNOWN',result['inventory']['messages']['schema']);self.assertEqual(1,result['inventory']['messages']['total'])
        self.assertIsNone(result['counts']['legacy_messages']);self.assertEqual('UNKNOWN',result['recovery_gate'])
        self.assertEqual('UNKNOWN',result['conversion']['stage'])
        with self.assertRaises(RulesHalt):check_before_recovery(self.db)

    def test_malformed_duplicate_oversized_and_truncated_json_are_bounded_unknown(self):
        previous=self.db
        for kind,raw in (('malformed','PRIVATE-{'),('duplicate','{"action":"CHAT","action":"ANALYZE"}'),('oversized','PRIVATE-'*40000),('nested','['*2000+']'*2000)):
            with self.subTest(kind=kind):
                self.db=Path(self.temp.name)/(kind+'.sqlite3');store=Store(self.db);store.enqueue(message(42));store.enable_autonomy();store.close()
                self.change('INSERT INTO autonomous_plans VALUES(?,?,?,?,?,?)',(message().chat_id,42,'[42]','case',raw,'now'))
                result=inspect(self.db)
                self.assertFalse(result['inventory']['autonomous_plans']['validation_complete'])
                self.assertIn('MALFORMED_RECORD',result['inventory']['autonomous_plans']['codes']);self.assertEqual('UNKNOWN',result['recovery_gate'])
                self.assertNotIn('PRIVATE',json.dumps(result))
        self.db=Path(self.temp.name)/'truncated.sqlite3';store=Store(self.db);store.enable_autonomy();store.close()
        for mid in (1,2,3):self.change('INSERT INTO autonomous_plans VALUES(?,?,?,?,?,?)',(1,mid,json.dumps([mid]),'case','{"action":"CHAT"}','now'))
        result=inspect(self.db,limit=2)
        self.assertTrue(result['inventory']['autonomous_plans']['truncated']);self.assertEqual('UNKNOWN',result['recovery_gate'])
        self.db=previous

    def test_orphan_sources_and_closed_topic_cannot_hide_queued_legacy_work(self):
        store=Store(self.db);pending=RulesPendingStore(store);pending.create('valid',message().chat_id,42);store.close()
        self.change("UPDATE rules_sources SET topic_id='missing'")
        before=self.db.read_bytes();result=inspect(self.db)
        self.assertIn('ORPHAN_RULES_SOURCE',result['binding_codes']);self.assertEqual('HOLD',result['recovery_gate'])
        with self.assertRaises(RulesHalt):check_before_recovery(self.db)
        self.assertEqual(before,self.db.read_bytes())
        self.change("UPDATE rules_sources SET topic_id='valid'");self.change("UPDATE rules_topics SET status='CLOSED'")
        with self.assertRaises(RulesHalt):check_before_recovery(self.db)

    def test_orphan_deferred_binding_requires_actual_notice_and_reply(self):
        store=Store(self.db);pending=RulesPendingStore(store);pending.create('valid',message().chat_id,42);store.enqueue(message(50,chat_id=-1009,reply_to_id=900));store.close()
        self.change("INSERT INTO rules_deferred_ops VALUES(-1009,50,'valid')")
        result=inspect(self.db)
        self.assertIn('MISMATCH_DEFERRED_NOTICE',result['binding_codes'])
        with self.assertRaises(RulesHalt):check_before_recovery(self.db)

    def test_sidecars_and_symlink_are_rejected_before_any_sqlite_open(self):
        from tg_triage import offline_snapshot
        for suffix in ('-wal','-shm','-journal'):
            sidecar=Path(str(self.db)+suffix);sidecar.write_bytes(b'fixture')
            try:
                with patch.object(offline_snapshot.sqlite3,'connect',side_effect=AssertionError('must not open SQLite')):
                    with self.assertRaisesRegex(RulesHalt,'SNAPSHOT_SIDECAR_PRESENT'):inspect(self.db)
            finally:sidecar.unlink()
        with patch.object(Path,'is_symlink',return_value=True),patch.object(offline_snapshot.sqlite3,'connect',side_effect=AssertionError('must not open SQLite')):
            with self.assertRaisesRegex(RulesHalt,'SNAPSHOT_UNSAFE_PATH'):inspect(self.db)

    def test_one_private_query_only_snapshot_for_cli_inventory_and_attachments(self):
        from tg_triage import offline_snapshot
        from tg_triage.__main__ import main
        real=sqlite3.connect;uris=[]
        def connect(database,*args,**kwargs):uris.append(database);return real(database,*args,**kwargs)
        before=self.db.read_bytes();out=io.StringIO()
        with patch.object(offline_snapshot.sqlite3,'connect',side_effect=connect),patch('sys.stdout',out):code=main(['legacy-preflight','--db',str(self.db),'--attachment-evidence'])
        self.assertEqual(0,code);self.assertEqual(1,len(uris));self.assertIn('immutable=1',uris[0]);self.assertNotIn(str(self.db),uris[0])
        self.assertEqual(before,self.db.read_bytes());self.assertFalse(any(Path(str(self.db)+suffix).exists() for suffix in ('-wal','-shm','-journal')))
        with offline_snapshot.isolated_snapshot(self.db) as (connection,_):
            self.assertEqual((1,),connection.execute('PRAGMA query_only').fetchone())
            with self.assertRaises(sqlite3.OperationalError):connection.execute('CREATE TABLE forbidden(x)')

    def test_changing_copy_or_late_sidecar_blocks_without_silent_counts(self):
        from tg_triage import offline_snapshot
        real=offline_snapshot._digest;calls=[]
        def changed(path):
            value=real(path);calls.append(path)
            if len(calls)==1:self.change("UPDATE messages SET text='changed fixture'")
            return value
        with patch.object(offline_snapshot,'_digest',side_effect=changed):
            with self.assertRaisesRegex(RulesHalt,'SNAPSHOT_CHANGED'):inspect(self.db)
        calls=[];sidecar=Path(str(self.db)+'-journal')
        def sidecar_race(path):
            value=real(path);calls.append(path)
            if len(calls)==1:sidecar.write_bytes(b'fixture')
            return value
        try:
            with patch.object(offline_snapshot,'_digest',side_effect=sidecar_race):
                with self.assertRaisesRegex(RulesHalt,'SNAPSHOT_SIDECAR_PRESENT'):inspect(self.db)
        finally:sidecar.unlink(missing_ok=True)

    def test_original_target_decision_reason_archived_and_receipt_bound(self):
        previous=self.db
        for decision,reason in (('queued',None),('pending','PRIVATE-original-reason'),('pending_review','original reason')):
            with self.subTest(decision=decision):
                self.db=Path(self.temp.name)/(decision+'.sqlite3');store=Store(self.db);store.enqueue(message(42));store.close()
                self.change('UPDATE messages SET decision=?,reason=?',(decision,reason));manifest=self.attestation();manifest['decision']=decision
                receipt=transition_pristine(self.db,manifest)
                connection=sqlite3.connect(self.db)
                archive=json.loads(connection.execute('SELECT original_state FROM rules_legacy_transitions').fetchone()[0]);connection.close()
                self.assertEqual(dict(version=1,decision=decision,reason=reason),archive)
                self.assertNotIn('PRIVATE',json.dumps(inspect(self.db)))
                self.assertTrue(release_pristine(self.db,self.release(receipt))['released'])
        self.db=previous

    def test_archive_tamper_and_old_stage_without_archive_never_fabricate_history(self):
        self.change("UPDATE messages SET reason='PRIVATE-original'");receipt=transition_pristine(self.db,self.attestation())
        self.change("UPDATE rules_legacy_transitions SET original_state=?",(json.dumps(dict(version=1,decision='queued',reason='forged')),))
        before=self.db.read_bytes()
        with self.assertRaises(RulesHalt):release_pristine(self.db,self.release(receipt))
        self.assertEqual(before,self.db.read_bytes())
        self.change('ALTER TABLE rules_legacy_transitions DROP COLUMN original_state')
        result=inspect(self.db)
        self.assertEqual('HOLD_ARCHIVE_MISSING',result['conversion']['release'])
        before=self.db.read_bytes()
        with self.assertRaises(RulesHalt):release_pristine(self.db,self.release(receipt))
        self.assertEqual(before,self.db.read_bytes())

    def test_inert_history_limit_1000_preserved_1001_refused(self):
        previous=self.db
        for count in (0,1,1000,1001):
            with self.subTest(count=count):
                self.db=Path(self.temp.name)/f'history-{count}.sqlite3';store=Store(self.db);store.enqueue(message(42))
                for mid in range(100,100+count):store.enqueue(message(mid,text='inert text'))
                with store.connection:store.connection.execute("UPDATE messages SET decision='chat',reason='retained' WHERE message_id>=100")
                store.close();before=self.db.read_bytes();manifest=self.attestation(reviewed_context=True)
                if count>1000:
                    with self.assertRaises(RulesHalt):transition_pristine(self.db,manifest)
                    self.assertEqual(before,self.db.read_bytes());continue
                receipt=transition_pristine(self.db,manifest);release=self.release(receipt);release['reviewed_context']=True
                self.assertTrue(release_pristine(self.db,release)['released'])
                connection=sqlite3.connect(self.db);self.assertEqual(count,connection.execute("SELECT count(*) FROM messages WHERE decision='chat' AND reason='retained'").fetchone()[0]);connection.close()
        self.db=previous


    def test_cli_stage_release_then_actual_rules_processing_is_separate_functional_proof(self):
        from dataclasses import replace
        from tg_triage.__main__ import main
        from tg_triage.decision_rules import RulesSnapshot
        from tg_triage.classifier import Classifier
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_intake import sweep
        from tests.helpers import config,FakeHTTP,FakeGitHub
        self.change("UPDATE messages SET text='POS kẹt giấy nhờ sửa',reason='PRIVATE-before-stage'")
        def cli(args):
            out=io.StringIO()
            with patch('sys.stdout',out):code=main(args)
            self.assertEqual(0,code);return json.loads(out.getvalue())
        report=cli(['legacy-preflight','--db',str(self.db),'--attachment-evidence'])
        self.assertFalse(report['migration_performed'])
        stage=self.attestation();stagefile=Path(self.temp.name)/'stage.json';stagefile.write_text(json.dumps(stage),encoding='utf8')
        receipt=cli(['legacy-transition','--db',str(self.db),'--manifest',str(stagefile)])
        self.assertEqual('STAGED_REVIEW_ONLY',receipt['outcome']);self.assertFalse(receipt['functional_verified'])
        staged=inspect(self.db);self.assertEqual('UNVERIFIED',staged['conversion']['functional'])
        release=self.release(receipt);releasefile=Path(self.temp.name)/'release.json';releasefile.write_text(json.dumps(release),encoding='utf8')
        armed=cli(['legacy-release','--db',str(self.db),'--manifest',str(releasefile)])
        self.assertEqual('ARMED_HOLD_ONLY',armed['outcome']);self.assertFalse(armed['functional_verified'])
        self.assertEqual('ARMED_NOT_FUNCTIONALLY_VERIFIED',inspect(self.db)['conversion']['release'])
        rules=Path(__file__).resolve().parents[1]/'rules';snapshot=RulesSnapshot(rules,rules.parent/'templates'/'github-ticket')
        payload=dict(groups=[dict(message_ids=[42],facts=dict(intent=dict(value='bug',source_ids=[42]),object=dict(value='POS',source_ids=[42]),observed=dict(value='kẹt giấy',source_ids=[42])))])
        http=FakeHTTP(dict(message=dict(content=json.dumps(payload,ensure_ascii=False))))
        base=config('live');settings=replace(base,decision_rules_enabled=True,autonomous_enabled=True,telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        store=Store(self.db);self.addCleanup(store.close);sent=[];github=FakeGitHub()
        pipeline=Pipeline(settings,store,Classifier(base.ollama,opener=http),github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 900)
        sweep(pipeline);sweep(pipeline)
        self.assertEqual(1,len(github.calls));self.assertEqual(1,len(http.calls));self.assertEqual('issue',store.get(message().chat_id,42)['decision'])
        self.assertTrue(sent)
        archive=json.loads(store.connection.execute('SELECT original_state FROM rules_legacy_transitions').fetchone()[0])
        self.assertEqual('PRIVATE-before-stage',archive['reason'])


    def test_actual_lineage_autonomous_and_thinking_history_remain_visible(self):
        from tg_triage.models import Decision
        from tg_triage.thinking import Turn
        store=Store(self.db);store.enable_topic_registry();store.enable_autonomy()
        assignment=store.assign_case(message(42),window_minutes=0,max_messages=20)
        store.finish(message(42),Decision('chat','rule',case_key=assignment.case_key))
        store.autonomy_route([message(42)],'CHAT',None,assignment.case_key,'fixture')
        Turn(store,'fixture','fixture',42).record('fixture',{})
        store.close();result=inspect(self.db)
        for table in ('triage_topics','triage_topic_messages','triage_topic_events','autonomous_routes','thinking_decisions','thinking_decision_events'):
            self.assertGreater(result['inventory'][table]['total'],0,table)
        self.assertNotEqual('CANDIDATE_REQUIRES_ATTESTATION',result['conversion']['stage'])

    def test_terminal_unconfirmed_post_is_counted_and_blocks_recovery(self):
        self.change("UPDATE messages SET decision='issue',github_called_at='2026-01-01',issue_number=NULL,issue_url=NULL")
        result=inspect(self.db)
        self.assertEqual(1,result['inventory']['messages']['uncertain_posts']);self.assertEqual(1,result['counts']['uncertain_issue_posts'])
        self.assertEqual('HOLD',result['recovery_gate'])
        with self.assertRaises(RulesHalt):check_before_recovery(self.db)

    def test_archive_insert_failure_rolls_back_original_fields_and_all_staging_state(self):
        from tg_triage import legacy_reconciliation
        self.change("UPDATE messages SET reason='PRIVATE-original'");before=self.db.read_bytes();manifest=self.attestation();real=sqlite3.connect
        class FailingConnection(sqlite3.Connection):
            def execute(inner,sql,*args,**kwargs):
                if sql.startswith('INSERT INTO rules_legacy_transitions'):raise sqlite3.OperationalError('fixture archive failure')
                return super().execute(sql,*args,**kwargs)
        def connect(database,*args,**kwargs):return real(database,*args,factory=FailingConnection,**kwargs)
        with patch.object(legacy_reconciliation.sqlite3,'connect',side_effect=connect):
            with self.assertRaises(RulesHalt):transition_pristine(self.db,manifest)
        self.assertEqual(before,self.db.read_bytes())
        connection=sqlite3.connect(self.db)
        self.assertEqual(('queued','PRIVATE-original'),connection.execute('SELECT decision,reason FROM messages').fetchone())
        self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='rules_legacy_transitions'").fetchone());connection.close()

    def test_inspection_rejects_source_change_during_read_snapshot(self):
        from tg_triage import legacy_preflight
        original=legacy_preflight.check_connection_before_recovery
        def changing(connection):
            try:original(connection)
            finally:self.change("UPDATE messages SET text='changed while clone inspected'")
        with patch.object(legacy_preflight,'check_connection_before_recovery',side_effect=changing):
            with self.assertRaisesRegex(RulesHalt,'SNAPSHOT_CHANGED'):inspect(self.db)

    def test_snapshot_size_limit_is_unknown_without_opening_sqlite(self):
        from tg_triage import offline_snapshot
        identity=offline_snapshot._identity(self.db)
        with patch.object(offline_snapshot,'_identity',return_value=(identity[0],identity[1],512*1024*1024+1,identity[3])),patch.object(offline_snapshot.sqlite3,'connect',side_effect=AssertionError('must not open')):
            with self.assertRaisesRegex(RulesHalt,'SNAPSHOT_SIZE_LIMIT'):inspect(self.db)


    def lifecycle_fixture(self,choice='A',direct=False):
        from tests import test_rules_extensions as fixtures
        t=fixtures.ExtensionRulesTests('test_direct_grounded_recurrence_and_latest_episode');t.setUp();self.addCleanup(t.doCleanups)
        self.db=Path(self.temp.name)/('genuine-'+choice+('-direct' if direct else '')+'.sqlite3')
        store,provider,github,sent,pipeline=t.direct_recurrence_fixture(str(self.db))
        comments=[];github.add_comment=lambda number,body:comments.append(number) or 301
        if not direct:provider.changes['kind']='unclear'
        result=pipeline.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10))
        if direct:self.assertEqual('issue',result.decision)
        else:
            self.assertEqual('lifecycle_question_sent',result.reason)
            self.assertEqual('issue' if choice=='B' else 'followup',pipeline.process(message(44,text=choice,reply_to_id=43)).decision)
        store.close();return self.db

    def test_r2_genuine_A_B_answers_reject_invalid_choice_missing_message_and_reply(self):
        previous=self.db
        for choice in ('A','B'):
            for change in ('choice','answer_id','reply','text'):
                with self.subTest(choice=choice,change=change):
                    self.lifecycle_fixture(choice)
                    check_before_recovery(self.db)
                    if change=='choice':self.change("UPDATE rules_lifecycle_answers SET choice='PRIVATE-invalid'")
                    elif change=='answer_id':self.change('UPDATE rules_lifecycle_answers SET answer_id=98765')
                    elif change=='reply':self.change('UPDATE messages SET reply_to_id=98765 WHERE message_id=44')
                    else:self.change("UPDATE messages SET text='PRIVATE-not-the-answer' WHERE message_id=44")
                    before=self.db.read_bytes();report=inspect(self.db)
                    self.assertIn(report['recovery_gate'],('UNKNOWN','HOLD'))
                    self.assertNotIn('PRIVATE',json.dumps(report))
                    with self.assertRaises(RulesHalt):check_before_recovery(self.db)
                    self.assertEqual(before,self.db.read_bytes())
                    # Reset fixture only by replacing its whole temporary file, never resetting live ledgers.
                    self.db.unlink()
        self.db=previous

    def test_r2_valid_direct_related_and_orphan_sent_notice(self):
        previous=self.db;self.lifecycle_fixture(direct=True)
        check_before_recovery(self.db);self.assertEqual('CLEAR',inspect(self.db)['recovery_gate'])
        self.change("INSERT INTO rules_notices VALUES('missing',1,-999,'question','sent',900)")
        before=self.db.read_bytes();result=inspect(self.db)
        self.assertIn('ORPHAN_RULES_NOTICE',result['binding_codes'])
        with self.assertRaises(RulesHalt):check_before_recovery(self.db)
        self.assertEqual(before,self.db.read_bytes());self.db=previous

    def test_r2_invalid_single_claim_cannot_pass_pure_gate_or_be_deleted(self):
        store=Store(self.db);pending=RulesPendingStore(store);pending.create('valid',message().chat_id,42)
        pending.claim_topic('valid',pending.topic_snapshot('valid'));store.close()
        for ordinal in (-5,1):
            with self.subTest(ordinal=ordinal):
                self.change('UPDATE rules_inflight_topics SET ordinal=?',(ordinal,));before=self.db.read_bytes()
                result=inspect(self.db)
                self.assertIn(result['recovery_gate'],('UNKNOWN','HOLD'))
                with self.assertRaises(RulesHalt):check_before_recovery(self.db)
                self.assertEqual(before,self.db.read_bytes())
                connection=sqlite3.connect(self.db)
                self.assertEqual((1,ordinal),connection.execute('SELECT count(*),ordinal FROM rules_inflight_topics').fetchone());connection.close()

    def test_r2_unknown_and_null_topic_phase_are_unknown_in_both_gates(self):
        store=Store(self.db);pending=RulesPendingStore(store);pending.create('valid',message().chat_id,42)
        with store.connection:store.connection.execute("UPDATE messages SET decision='chat'")
        store.close()
        for phase in ('PRIVATE-invalid-phase',None):
            with self.subTest(phase=phase):
                if phase is None:
                    # Old schema without NOT NULL must still be assessed as unknown, never initialized.
                    self.change('ALTER TABLE rules_topics RENAME COLUMN phase TO old_phase');self.change('ALTER TABLE rules_topics ADD COLUMN phase TEXT')
                else:self.change('UPDATE rules_topics SET phase=?',(phase,))
                before=self.db.read_bytes();result=inspect(self.db)
                self.assertEqual('UNKNOWN',result['recovery_gate']);self.assertIn('UNKNOWN_PHASE',result['inventory']['rules_topics']['codes'])
                self.assertNotIn('PRIVATE',json.dumps(result))
                with self.assertRaises(RulesHalt):check_before_recovery(self.db)
                self.assertEqual(before,self.db.read_bytes())

    def test_r2_stats_and_uploaded_attachments_are_not_stage_candidates(self):
        previous=self.db
        for kind in ('stats','uploaded','missing_media','exact_prewrite'):
            with self.subTest(kind=kind):
                self.db=Path(self.temp.name)/(kind+'.sqlite3');store=Store(self.db)
                source=message(42,has_image=kind in ('missing_media','exact_prewrite'));store.enqueue(source)
                ident=None
                if kind in ('uploaded','exact_prewrite'):
                    ident=store.reserve_attachment_call('unlinked',42,'PRIVATE-name',1,chat_id=source.chat_id)
                    store.finish_attachment(ident,'uploaded' if kind=='uploaded' else 'prewrite_error')
                store.close()
                if kind=='stats':self.change('ANALYZE')
                before=self.db.read_bytes();report=inspect(self.db)
                manifest=self.attestation()
                if ident:manifest['prewrite_attachment_ids']=[ident]
                if kind=='exact_prewrite':
                    self.assertEqual('CANDIDATE_REQUIRES_ATTESTATION',report['conversion']['stage'])
                    receipt=transition_pristine(self.db,manifest)
                    with self.assertRaises(RulesHalt):release_pristine(self.db,self.release(receipt))
                else:
                    self.assertNotEqual('CANDIDATE_REQUIRES_ATTESTATION',report['conversion']['stage'])
                    with self.assertRaises(RulesHalt):transition_pristine(self.db,manifest)
                    self.assertEqual(before,self.db.read_bytes())
                if kind=='stats':self.assertGreater(report['sqlite_internal_history']['nonempty_tables'],0)
                self.assertNotIn('PRIVATE',json.dumps(report))
        self.db=previous


    def test_r2_opaque_followup_results_keep_type_bounds_and_private_payloads(self):
        from tests.test_followups import FollowupTests,answer
        fixture=FollowupTests('test_update_comment_only_uses_trusted_number_and_keeps_body');self.addCleanup(fixture.doCleanups)
        self.db=Path(self.temp.name)/'opaque.sqlite3'
        store,model,github,sent,pipeline=fixture.setup_case(path=self.db);model.value=answer('update')
        fixture.reply(pipeline,mid=1000,target=901,text='Thông tin rõ hơn');store.close()
        check_before_recovery(self.db)
        for value in (b'PRIVATE-blob','PRIVATE-'*40000):
            with self.subTest(kind=type(value).__name__):
                self.change('UPDATE followup_jobs SET result=?',(value,));before=self.db.read_bytes()
                report=inspect(self.db)
                self.assertIn('MALFORMED_TEXT_RECORD',report['inventory']['followup_jobs']['codes'])
                self.assertEqual('UNKNOWN',report['recovery_gate']);self.assertNotIn('PRIVATE',json.dumps(report))
                with self.assertRaises(RulesHalt):check_before_recovery(self.db)
                self.assertEqual(before,self.db.read_bytes())

    def test_r2_real_retry_preserves_opaque_original_result_contract(self):
        from tests.test_followup_retry import RetryTests
        fixture=RetryTests('test_explicit_retry_edits_existing_notice_once_with_immutable_audit');self.addCleanup(fixture.doCleanups)
        self.db=Path(self.temp.name)/'retry.sqlite3'
        store,github,sent,pipeline,job=fixture.failed(self.db)
        pipeline.notice_editor=lambda *args:True
        self.assertTrue(fixture.retry(store,pipeline))
        fixture.provider(pipeline);pipeline.close_due_cases();pipeline.close_due_cases()
        retry=store.followup_analysis_retry(job['id'])
        self.assertEqual('done',retry['edit_status']);self.assertEqual(job['result'],retry['original_result'])
        self.assertIsInstance(retry['original_result'],str)
        with self.assertRaises(ValueError):json.loads(retry['original_result'])
        store.close();before=self.db.read_bytes()
        report=inspect(self.db)
        self.assertEqual(0,report['inventory']['followup_analysis_retries']['invalid'])
        check_before_recovery(self.db);self.assertEqual(before,self.db.read_bytes())
        self.change('UPDATE followup_analysis_retries SET original_result=?',(b'PRIVATE-blob',))
        report=inspect(self.db)
        self.assertIn('MALFORMED_TEXT_RECORD',report['inventory']['followup_analysis_retries']['codes'])
        self.assertNotIn('PRIVATE',json.dumps(report))
        with self.assertRaises(RulesHalt):check_before_recovery(self.db)

    def test_r3_completed_A_B_question_repository_and_saved_text_are_bound(self):
        previous=self.db
        for choice in ('A','B'):
            for field,value in (('repo','foreign/repository'),('repo','OWNER/REPO'),('question','PRIVATE-forged-question')):
                with self.subTest(choice=choice,field=field,value=value):
                    self.lifecycle_fixture(choice);check_before_recovery(self.db)
                    self.change('UPDATE rules_lifecycle_questions SET '+field+'=?',(value,))
                    before=self.db.read_bytes();report=inspect(self.db)
                    self.assertEqual('HOLD',report['recovery_gate'])
                    self.assertIn('LIFECYCLE_QUESTION_MISMATCH',report['binding_codes'])
                    self.assertNotIn('PRIVATE',json.dumps(report));self.assertNotIn(value,json.dumps(report))
                    with self.assertRaises(RulesHalt):check_before_recovery(self.db)
                    self.assertEqual(before,self.db.read_bytes());self.db.unlink()
        self.db=previous

    def test_r3_completed_A_B_latest_parent_origin_and_notice_alias_remain_clear(self):
        from tests import test_rules_extensions as fixtures
        previous=self.db
        for choice in ('A','B'):
            for reply in (10,43):
                with self.subTest(choice=choice,reply=reply):
                    fixture=fixtures.ExtensionRulesTests('test_direct_grounded_recurrence_and_latest_episode')
                    fixture.setUp();self.addCleanup(fixture.doCleanups)
                    self.db=Path(self.temp.name)/f'latest-{choice}-{reply}.sqlite3'
                    store,provider,github,sent,pipeline=fixture.direct_recurrence_fixture(str(self.db))
                    github.add_comment=lambda *args:301
                    text='POS worked yesterday; failed today'
                    self.assertEqual(202,pipeline.process(message(42,text=text,reply_to_id=10)).issue_number)
                    self.assertEqual('lifecycle_question_sent',pipeline.process(message(50,text=text,reply_to_id=reply)).reason)
                    question=store.connection.execute('SELECT parent_number FROM rules_lifecycle_questions').fetchone()
                    self.assertEqual(202,question[0])
                    result=pipeline.process(message(52,text=choice,reply_to_id=51))
                    self.assertEqual('followup' if choice=='A' else 'issue',result.decision)
                    store.close();before=self.db.read_bytes()
                    check_before_recovery(self.db);self.assertEqual('CLEAR',inspect(self.db)['recovery_gate'])
                    self.assertEqual(before,self.db.read_bytes())
        self.db=previous
