"""Offline storage/trace regression tests; no claims about model accuracy."""
import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tg_triage.__main__ import main
from tg_triage.config import ConfigError, load_config
from tg_triage.models import Decision
from tg_triage.store import Store
from tg_triage.trace import trace
from tests.helpers import NOW, message, classification, EXAMPLE

REPO='sandbox/triage'
CHAT=-5264814171


class TopicTraceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'triage.sqlite3'
        self.store=Store(self.path,clock=lambda:NOW,recover_pending=False)
        self.addCleanup(self.store.close)

    def case(self, ids=(448,449,450,451,452), *, chat=CHAT):
        msgs=[]
        for i in ids:
            m=message(i,chat_id=chat,sender_id=99,text='POS app/web wired printer' if i==ids[0] else 'context')
            self.store.enqueue(m)
            assignment=self.store.assign_case(m,window_minutes=2,max_messages=20)
            msgs.append(m)
        return assignment.case_key,msgs

    def issue(self, m, number=47):
        self.store.finish(m,Decision('issue','manual',classification(),number,f'https://github.com/{REPO}/issues/{number}'))

    def followup(self, case, original, *, mid=454, notice=453):
        self.store.reserve_group_notice(CHAT,original.message_id,'issue')
        self.store.finish_group_notice(CHAT,original.message_id,'sent',notice)
        m=message(mid,chat_id=CHAT,reply_to_id=notice,text='Đây là hình ảnh',has_image=True)
        self.store.enqueue(m)
        source=self.store.followup_source(m)
        self.assertIsNotNone(source)
        self.store.enqueue_followup(m,source)
        return m,source

    def query(self, sql, params=()):
        return [dict(r) for r in self.store.connection.execute(sql,params)]

    def test_explicit_registry_disabled_until_enabled_and_readonly_rejected(self):
        self.case()
        self.assertFalse(self.query("SELECT name FROM sqlite_master WHERE name='triage_topics'"))
        self.store.enable_topic_registry()
        ro=Store(self.path,read_only=True)
        self.addCleanup(ro.close)
        with self.assertRaises(RuntimeError): ro.enable_topic_registry()

    def test_hydration_bounded_cursor_idempotent_and_accounting_unchanged(self):
        self.case((1,),chat=-1); self.case((1,),chat=-2)
        before=self.query('SELECT * FROM messages ORDER BY chat_id,message_id')
        self.store.enable_topic_registry(hydration_batch=1)
        self.assertEqual(len(self.query('SELECT * FROM triage_topics')),1)
        self.store.enable_topic_registry(hydration_batch=1)
        events=self.query('SELECT * FROM triage_topic_events ORDER BY topic_id,version')
        self.assertEqual(len(events),2)
        self.store.enable_topic_registry(hydration_batch=1)
        self.assertEqual(events,self.query('SELECT * FROM triage_topic_events ORDER BY topic_id,version'))
        self.assertEqual(before,self.query('SELECT * FROM messages ORDER BY chat_id,message_id'))

    def test_partition_split_retires_provisional_case_and_no_crosschat_collision(self):
        case,msgs=self.case((1,2,3))
        self.store.enable_topic_registry()
        self.store.plan_groups(case,[(case+':a',[1,2]),(case+':b',[3])])
        rows=self.query('SELECT * FROM triage_topics WHERE active=1')
        self.assertEqual(len(rows),2)
        self.assertEqual({r['source_kind'] for r in rows},{'group'})
        membership=self.query('SELECT * FROM triage_topic_messages WHERE active=1')
        self.assertEqual(len(membership),3)
        self.assertNotEqual(membership[0]['topic_id'],membership[-1]['topic_id'])
        other,_=self.case((1,),chat=-123)
        with self.store.connection: self.store.project_topics(-123,other)
        self.assertEqual(len({r['topic_id'] for r in self.query('SELECT * FROM triage_topics WHERE active=1')}),3)
        self.assertEqual(len(self.query('SELECT * FROM messages')),4)

    def test_events_immutable_and_have_alexchu_provenance_no_text(self):
        case,msgs=self.case((1,))
        self.store.enable_topic_registry(); self.issue(msgs[0])
        events=self.query('SELECT * FROM triage_topic_events')
        self.assertEqual(len(events),2)
        self.assertEqual(json.loads(events[-1]['metadata'])['harness_owner'],'AlexChu')
        self.assertNotIn('POS app/web',events[-1]['metadata'])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("UPDATE triage_topic_events SET event='changed'")
        self.store.connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute('DELETE FROM triage_topic_events')
        self.store.connection.rollback()

    def test_generic_legacy_time_followup_is_not_membership(self):
        case,msgs=self.case((1,2))
        with self.store.connection:
            self.store.connection.execute("UPDATE messages SET decision='followup' WHERE message_id=2")
        self.store.enable_topic_registry()
        self.assertEqual([r['message_id'] for r in self.query('SELECT * FROM triage_topic_messages')],[1])

    def test_historical_reply_to_bot_notice_links_proven_root_and_keeps_accounting(self):
        case,msgs=self.case(); self.issue(msgs[0]); self.store.enable_topic_registry()
        reply,source=self.followup(case,msgs[0])
        members=self.query('SELECT * FROM triage_topic_messages WHERE active=1')
        self.assertEqual(len(members),6)
        self.assertEqual([r['role'] for r in members if r['message_id']==454],['followup'])
        self.assertEqual(self.store.get(CHAT,454)['decision'],'followup')
        report=trace(self.store,repo=REPO,chat_id=CHAT,message_id=454,limit=200)
        self.assertEqual(len(report['partitions']),1)
        self.assertEqual({r['message_id'] for r in report['sections']['messages']},{448,449,450,451,452,454})
        self.assertEqual(report['sections']['group_notices'][0]['notice_message_id'],453)
        self.assertEqual(report['sections']['followup_jobs'][0]['message_id'],454)

    def test_issue_selector_repo_exact_no_forged_user_url_or_crosschat_reply(self):
        case,msgs=self.case((448,)); self.issue(msgs[0])
        fake=message(448,chat_id=-5,text=f'https://github.com/{REPO}/issues/47',reply_to_id=453)
        self.store.enqueue(fake)
        self.store.finish(fake,Decision('issue','manual',classification(),47,'https://github.com/foreign/repo/issues/47'))
        report=trace(self.store,repo=REPO,issue=47)
        self.assertEqual({r['chat_id'] for r in report['sections']['messages']},{CHAT})
        report=trace(self.store,repo=REPO,chat_id=-5,message_id=448)
        self.assertNotIn(CHAT,{r['chat_id'] for r in report['sections']['messages']})

    def test_ambiguous_same_issue_partitions_kept_separate(self):
        case,msgs=self.case((1,2))
        self.store.plan_groups(case,[(case+':a',[1]),(case+':b',[2])])
        self.issue(msgs[0]);self.issue(msgs[1])
        report=trace(self.store,repo=REPO,issue=47)
        self.assertEqual(len(report['partitions']),2)
        self.assertIn('multiple_partitions_kept_separate',report['gaps'])

    def test_metadata_whitelist_private_optin_redacts_known_credentials(self):
        case,msgs=self.case((448,)); self.issue(msgs[0]); reply,source=self.followup(case,msgs[0])
        secret='private-context ghp_ABCDEF1234 user@example.com password=secret123'
        with self.store.connection:
            self.store.connection.execute('UPDATE messages SET text=? WHERE message_id=454',(secret,))
            self.store.connection.execute('UPDATE followup_jobs SET plan=?,result=?',
                (json.dumps({'action':'update','reason':secret}),secret))
            self.store.connection.execute('UPDATE followup_roots SET context_state=?',
                (json.dumps({'bindings':{'448':secret},'managed_snapshot':{'repo':REPO,'body_sha':'a'*64,'analysis':{'summary':secret}}}),))
            self.store.connection.execute("INSERT INTO followup_operations VALUES(1,0,'comment',?,'planned',NULL,NULL)",(json.dumps({'body':secret}),))
        default=json.dumps(trace(self.store,repo=REPO,issue=47,limit=200))
        self.assertNotIn('private-context',default)
        self.assertNotIn('ABCDEF1234',default)
        private=json.dumps(trace(self.store,repo=REPO,issue=47,limit=200,include_text=True))
        self.assertIn('private-context',private)
        self.assertNotIn('ABCDEF1234',private)
        self.assertNotIn('user@example.com',private)
        self.assertNotIn('secret123',private)

    def test_trace_cli_readonly_file_hash_mtime_no_credentials_model_or_network(self):
        case,msgs=self.case((448,));self.issue(msgs[0])
        cfg=Path(self.temp.name)/'minimal.toml'; cfg.write_text(f'[storage]\ndb_path="{self.path.as_posix()}"',encoding='utf-8')
        before=(hashlib.sha256(self.path.read_bytes()).hexdigest(),self.path.stat().st_mtime_ns)
        with patch('tg_triage.__main__.load_config',side_effect=AssertionError('credentials')), \
             patch('tg_triage.__main__.build_provider',side_effect=AssertionError('model')), \
             patch('urllib.request.urlopen',side_effect=AssertionError('network')), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            result=main(['trace','--config',str(cfg),'--repo',REPO,'--issue','47'])
        self.assertEqual(result,0)
        self.assertTrue(json.loads(out.getvalue())['read_only'])
        self.assertEqual(before,(hashlib.sha256(self.path.read_bytes()).hexdigest(),self.path.stat().st_mtime_ns))
        self.assertFalse(self.query("SELECT name FROM sqlite_master WHERE name='triage_topics'"))

    def test_legacy_minimal_database_reports_gaps_without_migration(self):
        path=Path(self.temp.name)/'legacy.sqlite3'
        db=sqlite3.connect(path)
        db.execute('CREATE TABLE messages(chat_id INTEGER,message_id INTEGER,issue_number INTEGER,issue_url TEXT,decision TEXT)')
        db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',(CHAT,448,47,f'https://github.com/{REPO}/issues/47','issue'))
        db.commit();db.close()
        ro=Store(path,read_only=True);self.addCleanup(ro.close)
        report=trace(ro,repo=REPO,issue=47)
        self.assertEqual(report['sections']['messages'][0]['message_id'],448)
        self.assertEqual(set(ro.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone()),{'messages'})

    def test_bounded_rows_and_large_text_indicate_truncation(self):
        case,msgs=self.case(tuple(range(1,15))); self.issue(msgs[0])
        with self.store.connection:
            self.store.connection.execute('UPDATE messages SET text=?',('x'*100000,))
        report=trace(self.store,repo=REPO,issue=47,limit=5,include_text=True)
        self.assertLessEqual(sum(len(v) for v in report['sections'].values()),5)
        self.assertTrue(report['truncated'])
        self.assertLess(len(json.dumps(report)),512000)

    def test_invalid_selector_limit_and_repo_rejected(self):
        for kwargs in ({'repo':'https://github.com/a/b','issue':47},{'repo':REPO,'issue':47,'message_id':1,'chat_id':CHAT},
                       {'repo':REPO,'message_id':1},{'repo':REPO,'issue':47,'limit':1001}):
            with self.assertRaises(ConfigError): trace(self.store,**kwargs)

    def test_config_topic_memory_strict_boolean_default_false(self):
        cfg=Path(self.temp.name)/'config.toml'
        base=EXAMPLE.read_text(encoding='utf-8')
        env={'TG_API_ID':'1','TG_API_HASH':'0'*32,'GH_TOKEN':'fake','GH_REPO':REPO}
        cfg.write_text(base,encoding='utf-8')
        self.assertFalse(load_config(cfg,env).topic_memory_enabled)
        # Example already has a thinking table; append the key inside it.
        for value,expected in (('true',True),('"true"',None)):
            cfg.write_text(base.replace('topic_memory_enabled = false','topic_memory_enabled = '+value) if 'topic_memory_enabled = false' in base else base.replace('[thinking]','[thinking]\ntopic_memory_enabled = '+value),encoding='utf-8')
            if expected is None:
                with self.assertRaises(ConfigError):load_config(cfg,env)
            else:
                self.assertTrue(load_config(cfg,env).topic_memory_enabled)

    def test_malformed_snapshot_and_nested_metadata_never_leak_or_crash(self):
        case,msgs=self.case((448,));self.issue(msgs[0]);self.followup(case,msgs[0])
        secret='PRIVATE PROSE MUST NOT LEAK'
        with self.store.connection:
            self.store.connection.execute('UPDATE followup_roots SET context_state=?',
                (json.dumps({'managed_snapshot':['bad',secret]}),))
            record=dict(budget={'model_calls_used':secret,'max_model_calls':2},reason_code={'raw':secret},
                        intent={'raw':secret},schema=secret,input_message_id={'raw':secret})
            self.store.connection.execute('INSERT INTO thinking_decisions VALUES(?,?,1,?)',
                ('case:'+case,case,json.dumps(record)))
        report=trace(self.store,repo=REPO,issue=47,limit=200)
        self.assertIn('snapshot_repository_mismatch',report['gaps'])
        self.assertNotIn(secret,json.dumps(report))
        self.assertEqual(report['sections']['thinking_decisions'][0]['budget'],{'max_model_calls':2})
        with self.store.connection:
            self.store.connection.execute('UPDATE followup_roots SET context_state=?',
                (json.dumps({'managed_snapshot':{'repo':REPO,'body_sha':{'raw':secret},'source_ids':[448,{'raw':secret}]}}),))
        report=trace(self.store,repo=REPO,issue=47,limit=200)
        self.assertNotIn(secret,json.dumps(report))
        self.assertEqual(report['sections']['followup_roots'][0]['snapshot']['source_ids'],[448])

    def test_wal_trace_no_logical_writes_and_reports_shared_memory_caveat(self):
        self.store.connection.execute('PRAGMA journal_mode=WAL')
        case,msgs=self.case((448,));self.issue(msgs[0])
        before=self.query('SELECT * FROM messages')
        ro=Store(self.path,read_only=True);self.addCleanup(ro.close)
        report=trace(ro,repo=REPO,issue=47)
        self.assertEqual(report['journal_mode'],'wal')
        self.assertIn('shared-memory',report['filesystem_note'])
        self.assertEqual(before,self.query('SELECT * FROM messages'))
        self.assertFalse(self.query("SELECT name FROM sqlite_master WHERE name='triage_topics'"))

    def test_partial_historical_split_exposes_bindings_unassigned_and_intake_scope(self):
        case,msgs=self.case((440,441,442));self.issue(msgs[0],46)
        self.store.enable_topic_registry()
        reply,source=self.followup(case,msgs[0],mid=445,notice=444)
        job=self.store.pending_followups()[0]
        self.store.save_followup_plan(job,{'action':'split','groups':[{'message_ids':[440]},{'message_ids':[441]}]},[])
        with self.store.connection:
            self.store.connection.execute('UPDATE followup_roots SET siblings=?,context_state=? WHERE root_key=?',
                ('[48]',json.dumps({'bindings':{'440':0,'441':1},'unresolved_message_ids':[442]}),source['root_key']))
        self.store.finish_followup(job,'done','confirmed partial mapping')
        report=trace(self.store,repo=REPO,issue=46,limit=200)
        self.assertIn('split_partition_not_projected',report['gaps'])
        root=report['sections']['followup_roots'][0]
        self.assertEqual(root['siblings'],[48])
        self.assertEqual(root['bindings'],{'440':0,'441':1})
        self.assertEqual(root['unresolved_message_ids'],[442])
        topic=report['sections']['triage_topics'][0]
        self.assertEqual(topic['projection_scope'],'original_intake')
        self.assertEqual(topic['split_partition_not_projected'],1)
        self.assertEqual(topic['issue_number'],46)

    def test_metadata_operation_presence_and_mixedcase_repo_match(self):
        case,msgs=self.case((448,));self.issue(msgs[0]);self.followup(case,msgs[0])
        with self.store.connection:
            self.store.connection.execute('UPDATE messages SET issue_url=?',('https://github.com/Sandbox/Triage/issues/47',))
            self.store.connection.execute("INSERT INTO followup_operations VALUES(1,0,'comment','private body','planned',NULL,NULL)")
        report=trace(self.store,repo=REPO,issue=47,limit=200)
        self.assertTrue(report['sections']['followup_operations'][0]['payload_present'])
        self.assertNotIn('private body',json.dumps(report))

    def test_fetched_byte_budget_caps_legacy_large_json_before_accumulating(self):
        from tg_triage.trace import Reader
        case,msgs=self.case((448,));self.issue(msgs[0]);self.followup(case,msgs[0])
        with self.store.connection:
            self.store.connection.execute('UPDATE followup_roots SET context_state=?',('x'*100000,))
        reader=Reader(self.store.connection,100,False)
        reader.read_bytes_remaining=1000
        self.assertEqual(reader.rows('followup_roots'),[])
        self.assertIn('read_bytes',reader.truncated)

    def test_deep_legacy_json_and_private_traversal_are_bounded(self):
        from tg_triage.trace import _private
        case,msgs=self.case((448,));self.issue(msgs[0]);self.followup(case,msgs[0])
        with self.store.connection:
            self.store.connection.execute('UPDATE followup_roots SET context_state=?,siblings=?',
                ('{"bad":'+('['*1500)+'0'+(']'*1500)+'}',('['*1500)+'0'+(']'*1500)))
        report=trace(self.store,repo=REPO,issue=47,limit=200,include_text=True)
        self.assertFalse(report['sections']['followup_roots'][0]['snapshot_present'])
        value='leaf'
        for _ in range(100):
            value={'child':value}
        self.assertIn('[nested content truncated]',json.dumps(_private(value)))


if __name__=='__main__':
    unittest.main()
