import json
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory

from tg_triage.decision_rules import RulesSnapshot
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import config, message


class OpsOnlyRegressionTests(unittest.TestCase):
    def test_h7_guard_restoration_is_idempotent_after_5000_restores(self):
        from tg_triage.rules_pending_store import RulesHalt
        root=Path(__file__).resolve().parents[1]
        snapshot=RulesSnapshot(root/'rules',root/'templates'/'github-ticket',extensions=True)
        store=Store(':memory:'); self.addCleanup(store.close)
        base=config(); ops=-1009876543210
        settings=replace(base,decision_rules_enabled=True,telegram=replace(base.telegram,ops_chat_id=ops))
        sent=[]; pipeline=Pipeline(settings,store,object(),rules_snapshot=snapshot,notice_sender=lambda *args:sent.append(args) or 99)
        for _ in range(5000):
            sender=pipeline.notice_sender
            pipeline.notice_sender=None
            pipeline.notice_sender=sender
        self.assertEqual(99,pipeline.notice_sender(ops,None,'text'))
        self.assertEqual([(ops,None,'text')],sent)
        with self.assertRaises(RulesHalt): pipeline.notice_sender(-123,42,'forbidden')
        self.assertEqual(len(sent),1)
        other_ops=-1009876543211
        other=Pipeline(replace(settings,telegram=replace(settings.telegram,ops_chat_id=other_ops)),store,object(),
                       rules_snapshot=snapshot,notice_sender=pipeline.notice_sender)
        self.assertEqual(99,other.notice_sender(other_ops,None,'other'))
        with self.assertRaises(RulesHalt): other.notice_sender(ops,None,'forbidden')

    def test_lifecycle_b_allows_equal_source_and_ops_message_ids(self):
        from tests.helpers import FakeGitHub
        root=Path(__file__).resolve().parents[1]
        snapshot=RulesSnapshot(root/'rules',root/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'bug','source_ids':[42]},'object':{'value':'POS','source_ids':[42]},
                    'observed':{'value':'kẹt giấy','source_ids':[42]},
                    'lifecycle':{'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='lỗi cũ'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); ops=-1009876543210
        settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(base.telegram,ops_chat_id=ops))
        gh=FakeGitHub(); gh.lifecycle_snapshot=lambda n:{'number':n,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
        gh.verify_created_issue=lambda n,issue:n
        sent=[]; pipeline=Pipeline(settings,store,Provider(),gh,rules_snapshot=snapshot,
                                  notice_sender=lambda *args:sent.append(args) or (30 if len(sent)==1 else 50))
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(42,text='POS kẹt giấy',reply_to_id=10)).reason)
        answer=message(42,text='B',chat_id=ops,reply_to_id=30)
        self.assertTrue(pipeline.rules_pending.accept_ops_message(answer,7))
        result=pipeline.process(answer)
        self.assertEqual(('issue',42),(result.decision,result.issue_number))
        self.assertEqual(1,len(gh.calls))
        self.assertEqual(1,gh.calls[0].body.count('tin chính'))
        self.assertEqual('done',store.connection.execute('SELECT status FROM rules_related_episodes').fetchone()[0])
        self.assertEqual(42,store.get(answer.chat_id,42)['issue_number'])

    def test_related_recovery_rejects_legacy_receipt_collisions_without_resend(self):
        from unittest.mock import patch
        from tg_triage.rules_pending_store import RulesHalt, _recoverable_batch_groups
        from tests.helpers import FakeGitHub
        root=Path(__file__).resolve().parents[1]
        snapshot=RulesSnapshot(root/'rules',root/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'bug','source_ids':[42]},'object':{'value':'POS','source_ids':[42]},
                    'observed':{'value':'failed today','source_ids':[42]},
                    'lifecycle':{'kind':'recurrence','knownworking_source_ids':[42],'recurrence_source_ids':[42],
                      'knownworking_quote':'worked yesterday','recurrence_quote':'failed today','temporal_order_confirmed':True}}}]}))
        for table in ('group_notices','autonomous_confirmations'):
            with self.subTest(table=table):
                store=Store(':memory:'); self.addCleanup(store.close)
                old=message(10,text='old'); store.enqueue(old)
                with store.connection:
                    store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
                base=config(); ops=-1009876543210
                settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                                 telegram=replace(base.telegram,ops_chat_id=ops))
                gh=FakeGitHub(); gh.lifecycle_snapshot=lambda n:{'number':n,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
                gh.verify_created_issue=lambda n,issue:n
                sent=[]; pipeline=Pipeline(settings,store,Provider(),gh,rules_snapshot=snapshot,notice_sender=lambda *args:sent.append(args) or 99)
                transition=pipeline.rules_pending.transition_group
                def crash(*args,**kwargs):
                    if args[2:4]==('RESERVED','DONE'): raise RuntimeError('crash after sent notice')
                    return transition(*args,**kwargs)
                with patch.object(pipeline.rules_pending,'transition_group',side_effect=crash):
                    with self.assertRaises(RuntimeError): pipeline.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10))
                self.assertEqual('DONE',_recoverable_batch_groups(store.connection)[0][2])
                with store.connection:
                    if table=='group_notices':
                        store.connection.execute("INSERT INTO group_notices(chat_id,message_id,kind,status,reserved_at,notice_message_id) VALUES(?,777,'issue','sent','now',99)",(ops,))
                    else:
                        store.connection.execute("INSERT INTO autonomous_confirmations VALUES(?,777,1,'sent',99,'now')",(ops,))
                with self.assertRaises(RulesHalt): _recoverable_batch_groups(store.connection)
                self.assertEqual('RESERVED',store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])
                with self.assertRaises(RulesHalt): pipeline.process(message(43,text='retry'))
                self.assertEqual(1,len(sent)); self.assertEqual(1,len(gh.calls))

    def test_ops_notice_collision_remains_ambiguous_and_blocks_resend(self):
        from tg_triage.rules_pending_store import RulesHalt
        from tg_triage.rules_intake import _send_ops
        root=Path(__file__).resolve().parents[1]
        snapshot=RulesSnapshot(root/'rules',root/'templates'/'github-ticket',extensions=True)
        store=Store(':memory:'); self.addCleanup(store.close)
        base=config(); ops=-1009876543210
        settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(base.telegram,ops_chat_id=ops))
        source=message(42); store.enqueue(source); store.enqueue(message(99,chat_id=ops))
        sent=[]; pipeline=Pipeline(settings,store,object(),rules_snapshot=snapshot,
                                  notice_sender=lambda *args:sent.append(args) or 99)
        topic=f'{source.chat_id}:{source.message_id}'
        pipeline.rules_pending.create(topic,source.chat_id,source.message_id)
        with self.assertRaises(RulesHalt): _send_ops(pipeline,source,topic,'other','text')
        self.assertEqual('ambiguous',store.connection.execute('SELECT status FROM rules_notices').fetchone()[0])
        with self.assertRaises(RulesHalt): pipeline.process(message(50))
        self.assertEqual(len(sent),1)

    def test_readonly_notice_is_not_resent_after_restart(self):
        from tg_triage.rules_intake import _read_only_result
        root=Path(__file__).resolve().parents[1]
        snapshot=RulesSnapshot(root/'rules',root/'templates'/'github-ticket',extensions=True)
        with TemporaryDirectory() as directory:
            path=Path(directory)/'state.sqlite'
            store=Store(path)
            source=message(42,text='analyze this'); store.enqueue(source); store.start_classifying(source)
            base=config(); ops=-1009876543210
            settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
                             telegram=replace(base.telegram,ops_chat_id=ops))
            sent=[]; sender=lambda *args: sent.append(args) or 101
            pipeline=Pipeline(settings,store,object(),rules_snapshot=snapshot,notice_sender=sender)
            topic=f'{source.chat_id}:{source.message_id}'
            pipeline.rules_pending.create(topic,source.chat_id,source.message_id)
            decision=SimpleNamespace(state='ANALYZE',task={'title':'Analyze','work':'Inspect','acceptance':['Result']})
            _read_only_result(pipeline,source,topic,decision,plan_id='plan')
            store.close()
            reopened=Store(path)
            pipeline=Pipeline(settings,reopened,object(),rules_snapshot=snapshot,notice_sender=sender)
            self.assertEqual(_read_only_result(pipeline,source,topic,decision,plan_id='plan').reason,'readonly_notice_conflict')
            self.assertEqual(len(sent),1)
            reopened.close()

    def test_lifecycle_b_creates_related_notice_in_ops(self):
        from tests.helpers import FakeGitHub
        root=Path(__file__).resolve().parents[1]
        snapshot=RulesSnapshot(root/'rules',root/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'bug','source_ids':[42]},'object':{'value':'POS','source_ids':[42]},
                    'observed':{'value':'kẹt giấy','source_ids':[42]},
                    'lifecycle':{'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='lỗi cũ'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); ops=-1009876543210
        settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(base.telegram,ops_chat_id=ops))
        gh=FakeGitHub(); gh.lifecycle_snapshot=lambda number:{'number':number,'repo':'sandbox/triage','state':'closed' if number==101 else 'open','observed_at':'now','updated_at':'version-1'}
        gh.verify_created_issue=lambda number,issue:number
        sent=[]; pipeline=Pipeline(settings,store,Provider(),gh,rules_snapshot=snapshot,
                                  notice_sender=lambda *args:sent.append(args) or (99 if len(sent)==1 else 200))
        self.assertEqual(pipeline.process(message(42,text='POS kẹt giấy',reply_to_id=10)).reason,'lifecycle_question_sent')
        answer=message(100,text='B',chat_id=ops,reply_to_id=99)
        self.assertTrue(pipeline.rules_pending.accept_ops_message(answer,7))
        result=pipeline.process(answer)
        self.assertEqual(result.decision,'issue')
        self.assertEqual([s[0] for s in sent],[ops,ops])
        self.assertIn('Tin gốc:',sent[-1][2]); self.assertIn('/issues/42',sent[-1][2])
        from tg_triage.rules_pending_store import local_binding_blockers
        self.assertEqual(local_binding_blockers(store.connection),[])

    def test_lifecycle_question_is_ops_bound_and_source_reply_has_no_authority(self):
        from tests.helpers import FakeGitHub
        root = Path(__file__).resolve().parents[1]
        snapshot = RulesSnapshot(root / 'rules', root / 'templates' / 'github-ticket', extensions=True)
        class Provider:
            settings = SimpleNamespace(model='test'); think = False
            def _ask(self, body, parser, attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42], 'facts':{
                    'intent':{'value':'bug','source_ids':[42]}, 'observed':{'value':'lỗi','source_ids':[42]},
                    'lifecycle':{'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='lỗi cũ'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); ops=-1009876543210
        settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(base.telegram,ops_chat_id=ops))
        gh=FakeGitHub(); gh.lifecycle_snapshot=lambda number:{'number':number,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
        comments=[]; gh.add_comment=lambda number,body: comments.append((number,body)) or 201
        sent=[]; pipeline=Pipeline(settings,store,Provider(),gh,rules_snapshot=snapshot,
                                  notice_sender=lambda *args:sent.append(args) or (99 if len(sent)==1 else 200+len(sent)))
        self.assertEqual(pipeline.process(message(42,text='lỗi',reply_to_id=10)).reason,'lifecycle_question_sent')
        self.assertEqual(sent[0][:2],(ops,None))
        self.assertIn('Tin gốc:',sent[0][2])
        pipeline.process(message(100,text='A',reply_to_id=99))
        self.assertEqual(comments,[])
        answer=message(100,text='A',chat_id=ops,reply_to_id=99)
        self.assertTrue(pipeline.rules_pending.accept_ops_message(answer,7))
        self.assertEqual(pipeline.process(answer).decision,'followup')
        self.assertEqual(len(comments),1)
        self.assertTrue(all(item[0] == ops for item in sent))
        from tg_triage.rules_pending_store import local_binding_blockers
        self.assertEqual(local_binding_blockers(store.connection),[])

    def test_analysis_receipt_is_keyed_to_ops_and_does_not_resend(self):
        from tg_triage.rules_intake import _read_only_result
        root = Path(__file__).resolve().parents[1]
        snapshot = RulesSnapshot(root / 'rules', root / 'templates' / 'github-ticket', extensions=True)
        store = Store(':memory:'); self.addCleanup(store.close)
        source = message(42, text='analyze this')
        store.enqueue(source); store.start_classifying(source)
        base = config(); ops = -1009876543210
        settings = replace(base, mode='live', autonomous_enabled=True, decision_rules_enabled=True,
                           telegram=replace(base.telegram, ops_chat_id=ops))
        sent = []
        pipeline = Pipeline(settings, store, object(), rules_snapshot=snapshot,
                            notice_sender=lambda *args: sent.append(args) or 101)
        topic = f'{source.chat_id}:{source.message_id}'
        pipeline.rules_pending.create(topic, source.chat_id, source.message_id)
        decision = SimpleNamespace(state='ANALYZE', task={'title':'Analyze', 'work':'Inspect', 'acceptance':['Result']})
        self.assertEqual(_read_only_result(pipeline, source, topic, decision, plan_id='plan').reason, 'semantic_analyze')
        self.assertEqual(sent[0][0:2], (ops, None))
        self.assertIn('Tin gốc:', sent[0][2])
        row = store.connection.execute('SELECT ops_chat_id,status,notice_id FROM rules_notices').fetchone()
        self.assertEqual(tuple(row), (ops, 'sent', 101))
        self.assertEqual(_read_only_result(pipeline, source, topic, decision, plan_id='plan').reason, 'readonly_notice_conflict')
        self.assertEqual(len(sent), 1)

    def test_status_missing_target_uses_ops_binding_and_reads_exact_source(self):
        root = Path(__file__).resolve().parents[1]
        snapshot = RulesSnapshot(root / 'rules', root / 'templates' / 'github-ticket', extensions=True)

        class Provider:
            settings = SimpleNamespace(model='test'); think = False
            def _ask(self, body, parser, attempts):
                return parser(json.dumps({'groups': [{'message_ids': [42], 'facts': {
                    'intent': {'value': 'status', 'source_ids': [42]}}}]}))

        store = Store(':memory:'); self.addCleanup(store.close)
        original = message(10, text='request')
        store.enqueue(original)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url=? WHERE chat_id=? AND message_id=?",
                ('https://github.com/sandbox/triage/issues/101', original.chat_id, 10))
        base = config(); ops = -1009876543210
        settings = replace(base, mode='live', gh_repo='sandbox/triage', autonomous_enabled=True,
                           decision_rules_enabled=True, telegram=replace(base.telegram, ops_chat_id=ops))
        sent = []; reactions = []
        pipeline = Pipeline(settings, store, Provider(), rules_snapshot=snapshot,
                            notice_sender=lambda *args: sent.append(args) or 100 + len(sent),
                            reactor=lambda *args: reactions.append(args) or True)
        pipeline.process(message(42, text='status?'))
        self.assertEqual(sent[0][0:2], (ops, None))
        self.assertIn('Tin gốc:', sent[0][2])
        self.assertTrue(reactions)
        self.assertEqual(pipeline.process(message(43,text='10',reply_to_id=42)).reason,'status_reply_requires_ops')
        self.assertEqual(len(sent),1)
        answer = message(103, chat_id=ops, text='10', reply_to_id=101)
        self.assertTrue(pipeline.rules_pending.accept_ops_message(answer, 7))
        result = pipeline.process(answer)
        self.assertEqual(result.reason, 'status_target_processed')
        self.assertEqual(sent[-1][0:2], (ops, None))
        self.assertIn('/issues/101', sent[-1][2])
