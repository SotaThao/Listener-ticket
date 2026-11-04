import tempfile
import unittest
import json
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

from tg_triage.decision_rules import RulesError
from tg_triage.rules_extensions import ExtensionRules
from tg_triage.decision_rules import RulesSnapshot

ROOT = Path(__file__).resolve().parents[1] / 'rules'


def ops_reply(pipeline, incoming):
    """Deliver a real ops question reply through the same binding as ingress."""
    incoming=replace(incoming,chat_id=pipeline.config.telegram.ops_chat_id)
    pipeline.rules_pending.accept_ops_message(incoming,pipeline.rules_snapshot.limits['pending_expiry_days'])
    return pipeline.process(incoming)


def fixture_analysis_review(body):
    """Compatibility fixture for literal legacy proposals; never a model quality claim."""
    if 'reviews' not in body.get('format',{}).get('properties',{}):return None
    proposals=json.loads(body['messages'][0]['content'].split('Proposals:\n',1)[1])
    packet=json.loads(body['messages'][1]['content']);texts={}
    for row in packet['sources']+packet.get('read_only_context',[]):texts[row['message_id']]=row['text']
    for row in packet.get('DOCUMENT_CONTENTS',[]):
        if row.get('read_status')=='read':texts[row['message_id']]=texts.get(row['message_id'],'')+'\n'+row['text']
    reviews=[]
    for proposal in proposals:
        fields=[('title',proposal['task']['title']),('work',proposal['task']['work'])]+[(f'acceptance/{i}',v) for i,v in enumerate(proposal['task']['acceptance'])]
        claims=[]
        for field,value in fields:
            match=next(((mid,text) for mid,text in texts.items() if value in text),None)
            if match:
                mid,text=match
                mode='text' if any(r['message_id']==mid and value in r['text'] for r in packet['sources']+packet.get('read_only_context',[])) else 'document'
                claims.append(dict(field=field,evidence=[dict(source_id=mid,modality=mode,quote=value)]))
        reviews.append(dict(group_index=proposal['group_index'],verdict='approve' if len(claims)==len(fields) else 'reject',claims=claims))
    return json.dumps(dict(reviews=reviews))

class ExtensionRulesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        for name in ('lifecycle.md', 'analysis-status.md'):
            text = (ROOT / name).read_text(encoding='utf8')
            (self.directory / name).write_text(text.replace('status DRAFT', 'status ACTIVE'), encoding='utf8')

    def test_drafts_fail_closed_and_active_fixture_routes(self):
        rules = ExtensionRules(ROOT)
        for name in ('lifecycle.md','analysis-status.md'):
            path=self.directory/name;active=path.read_text(encoding='utf8')
            path.write_text(active.replace('status ACTIVE','status DRAFT'),encoding='utf8')
            with self.subTest(draft=name),self.assertRaisesRegex(RulesError,'ACTIVE'):
                ExtensionRules(self.directory)
            path.write_text(active,encoding='utf8')
        rules = ExtensionRules(self.directory)
        self.assertEqual(rules.disposition('evidence'), 'APPEND_CLOSED')
        self.assertEqual(rules.disposition('recurrence'), 'RELATED')
        self.assertEqual(rules.disposition('reused_success'), 'QUESTION')
        self.assertEqual(rules.disposition('unknown'), 'HOLD')
        self.assertEqual(rules.route('analyze'), 'ANALYZE')
        self.assertEqual(rules.route('status'), 'STATUS')
        self.assertIsNone(rules.route('other'))
        with self.assertRaises(AttributeError):
            rules.fingerprint = 'changed'

    def test_unsupported_or_duplicate_rule_fails_closed(self):
        path = self.directory / 'lifecycle.md'
        source = path.read_text(encoding='utf8')
        for changed in (source.replace('`APPEND_CLOSED`', '`RELATED`'),
                        source.replace('| `unclear` | `QUESTION` |', '| `evidence` | `QUESTION` |')):
            path.write_text(changed, encoding='utf8')
            with self.assertRaises(RulesError):
                ExtensionRules(self.directory)

    def test_extended_runtime_snapshot_requires_drafts_to_be_approved(self):
        for name in ('states', 'facts', 'readiness', 'questions', 'channels', 'examples'):
            (self.directory / (name + '.md')).write_text((ROOT / (name + '.md')).read_text(encoding='utf8'), encoding='utf8')
        templates = ROOT.parent / 'templates' / 'github-ticket'
        ordinary = RulesSnapshot(self.directory, templates)
        extended = RulesSnapshot(self.directory, templates, extensions=True)
        self.assertNotEqual(ordinary.fingerprint, extended.fingerprint)
        self.assertEqual(extended.extensions.route('status'), 'STATUS')
        path = self.directory / 'lifecycle.md'
        path.write_text(path.read_text(encoding='utf8').replace('status ACTIVE', 'status DRAFT'), encoding='utf8')
        with self.assertRaises(RulesError):
            RulesSnapshot(self.directory, templates, extensions=True)

    def test_single_source_analysis_and_missing_target_status_use_rules_runtime(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states', 'facts', 'readiness', 'questions', 'channels', 'examples'):
            (self.directory / (name + '.md')).write_text((ROOT / (name + '.md')).read_text(encoding='utf8'), encoding='utf8')
        snapshot = RulesSnapshot(self.directory, ROOT.parent / 'templates' / 'github-ticket', extensions=True)

        class Provider:
            settings = SimpleNamespace(model='test')
            think = False
            def __init__(self, intent): self.intent = intent
            def _ask(self, body, parser, attempts):
                review=fixture_analysis_review(body)
                if review is not None:return parser(review)
                facts = {'intent': {'value': self.intent, 'source_ids': [42]}}
                if self.intent == 'analyze':
                    facts['task'] = {'title': 'Kiểm tra hiển thị', 'work': 'Phân tích phần bị cắt',
                                     'acceptance': ['Nêu vị trí bị cắt']}
                return parser(json.dumps({'groups': [{'message_ids': [42], 'facts': facts}]}))

        for intent in ('analyze', 'status'):
            with self.subTest(intent=intent):
                store = Store(':memory:')
                self.addCleanup(store.close)
                base = config()
                settings = replace(base, mode='live', autonomous_enabled=True, decision_rules_enabled=True,
                                   telegram=replace(base.telegram, ops_chat_id=-1009876543210))
                sent = []
                pipeline = Pipeline(settings, store, Provider(intent), rules_snapshot=snapshot,
                                    notice_sender=lambda *args: sent.append(args) or 99)
                source = message(42, text='Kiểm tra giúp em. Kiểm tra hiển thị. Phân tích phần bị cắt. Nêu vị trí bị cắt')
                result = pipeline.process(source)
                self.assertEqual(result.reason, 'semantic_' + intent)
                self.assertEqual(len(sent), 1)
                self.assertIn('Việc:' if intent == 'analyze' else 'reply', sent[0][2])
                notice=store.connection.execute('SELECT ops_chat_id,status FROM rules_notices').fetchone()
                self.assertEqual(tuple(notice),(-1009876543210,'sent'))
                self.assertEqual(sent[0][0],-1009876543210)

    def test_status_reply_and_batch_read_each_exact_target_without_github(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_intake import _process_batch
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states', 'facts', 'readiness', 'questions', 'channels', 'examples'):
            (self.directory / (name + '.md')).write_text((ROOT / (name + '.md')).read_text(encoding='utf8'), encoding='utf8')
        snapshot = RulesSnapshot(self.directory, ROOT.parent / 'templates' / 'github-ticket', extensions=True)

        class Provider:
            settings = SimpleNamespace(model='test')
            think = False
            def _ask(self, body, parser, attempts):
                ids = json.loads(body['messages'][1]['content'])['source_ids']
                return parser(json.dumps({'groups': [{'message_ids':[mid], 'facts':
                    {'intent':{'value':'status','source_ids':[mid]}}} for mid in ids]}))
        class NoGitHub:
            def __getattr__(self, name): raise AssertionError('STATUS must not access GitHub')
        store = Store(':memory:'); self.addCleanup(store.close)
        base = config()
        settings = replace(base, mode='live', gh_repo='sandbox/triage', autonomous_enabled=True,
                           decision_rules_enabled=True, telegram=replace(base.telegram, ops_chat_id=-1009876543210))
        sent = []
        pipeline = Pipeline(settings, store, Provider(), NoGitHub(), rules_snapshot=snapshot,
                            notice_sender=lambda *args: sent.append(args) or (1000 + len(sent)))
        for mid, number in ((10, 101), (20, 102)):
            old = message(mid, text='original')
            store.enqueue(old)
            with store.connection:
                store.connection.execute("UPDATE messages SET decision='issue',issue_number=?,issue_url=? WHERE chat_id=? AND message_id=?",
                    (number, f'https://github.com/sandbox/triage/issues/{number}', old.chat_id, mid))
        first = message(30, text='ticket tới đâu?', reply_to_id=10)
        self.assertEqual(pipeline.process(first).reason, 'semantic_status')
        self.assertIn('/issues/101', sent[-1][2])
        sources = [message(40,text='trạng thái?',reply_to_id=10), message(41,text='tiến độ?',reply_to_id=20)]
        for source in sources:
            store.enqueue(source); self.assertTrue(store.start_classifying(source))
        _process_batch(pipeline, sources)
        self.assertEqual(len(sent), 3)
        self.assertIn('/issues/101', sent[-2][2])
        self.assertIn('/issues/102', sent[-1][2])

    def test_analysis_reply_reads_exact_historical_text_without_ticket_authority(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states', 'facts', 'readiness', 'questions', 'channels', 'examples'):
            (self.directory / (name + '.md')).write_text((ROOT / (name + '.md')).read_text(encoding='utf8'), encoding='utf8')
        snapshot = RulesSnapshot(self.directory, ROOT.parent / 'templates' / 'github-ticket', extensions=True)
        class Provider:
            settings = SimpleNamespace(model='test')
            think = False
            def _ask(self, body, parser, attempts):
                review=fixture_analysis_review(body)
                if review is not None:return parser(review)
                self.packet = json.loads(body['messages'][1]['content'])
                return parser(json.dumps({'groups':[{'message_ids':[42], 'facts':{
                    'intent':{'value':'analyze','source_ids':[42]},
                    'task':{'title':'Kiểm tra lỗi','work':'Phân tích yêu cầu','acceptance':['Nêu vấn đề']}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='Thông tin lịch sử',has_image=True); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='chat' WHERE chat_id=? AND message_id=?", (old.chat_id,old.message_id))
        base=config(); settings=replace(base, mode='dry-run', autonomous_enabled=True, decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        provider=Provider(); pipeline=Pipeline(settings,store,provider,rules_snapshot=snapshot)
        result=pipeline.process(message(42,text='phân tích tin này. Kiểm tra lỗi. Phân tích yêu cầu. Nêu vấn đề',reply_to_id=10))
        self.assertEqual(result.reason,'semantic_analyze')
        context=provider.packet['read_only_context']
        self.assertEqual(context[-1]['message_id'],10)
        self.assertFalse(context[-1]['pixels_available'])
        self.assertEqual(context[-1]['text'],'Thông tin lịch sử')
        self.assertEqual(store.connection.execute('SELECT count(*) FROM rules_sources WHERE message_id=10').fetchone()[0],0)

    def test_closed_historical_evidence_appends_without_reopen_or_duplicate(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states', 'facts', 'readiness', 'questions', 'channels', 'examples'):
            (self.directory / (name + '.md')).write_text((ROOT / (name + '.md')).read_text(encoding='utf8'), encoding='utf8')
        snapshot=RulesSnapshot(self.directory, ROOT.parent/'templates'/'github-ticket', extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                return parser(json.dumps({'groups':[{'message_ids':[mid], 'facts':{
                    'intent':{'value':'bug','source_ids':[mid]},
                    'observed':{'value':'vẫn lỗi','source_ids':[mid]},
                    'lifecycle':{'kind':'evidence','knownworking_source_ids':[], 'recurrence_source_ids':[]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='lỗi cũ'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub()
        github.lifecycle_snapshot=lambda number: {'number':number,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
        github.comments=[]
        def comment(number, body):
            github.comments.append((number,body))
            return 201
        github.add_comment=comment
        sent=[]; pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a) or 99)
        reply=message(42,text='vẫn lỗi',reply_to_id=10)
        result=pipeline.process(reply)
        self.assertEqual(result.decision,'pending_review')
        self.assertEqual(result.reason,'lifecycle_evidence_pending')
        self.assertEqual(len(github.comments),0)
        self.assertIsNone(pipeline.process(reply))
        self.assertEqual(len(github.comments),0)

    def test_direct_closed_evidence_is_appended_once_without_reopen(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'bug','source_ids':[42]},'observed':{'value':'chưa từng hết','source_ids':[42]},
                    'lifecycle':{'kind':'evidence','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='lỗi cũ'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); github.lifecycle_snapshot=lambda number:{'number':number,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
        comments=[]; github.add_comment=lambda number,body:comments.append((number,body)) or 201
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *args:99)
        reply=message(42,text='Lỗi này chưa từng hết.',reply_to_id=10)
        result=pipeline.process(reply)
        self.assertEqual('followup',result.decision)
        self.assertEqual(101,comments[0][0]); self.assertEqual(1,len(comments))
        self.assertIn('Lỗi này chưa từng hết.',comments[0][1])
        self.assertIsNone(pipeline.process(reply)); self.assertEqual(1,len(comments))
        self.assertEqual([],github.calls)
        from tg_triage.rules_intake import _direct_closed_evidence
        self.assertFalse(_direct_closed_evidence(snapshot,[replace(reply,is_forward=True)]))
        self.assertFalse(_direct_closed_evidence(snapshot,[replace(reply,text='Họ nói: Lỗi này chưa từng hết.')]))
        # A positive POST result without a durable CAS must halt, and the
        # next intake must not send the comment again.
        from unittest.mock import patch
        from tg_triage.rules_pending_store import RulesHalt
        other=Store(':memory:'); self.addCleanup(other.close); other.enqueue(old)
        with other.connection:
            other.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        comments.clear()
        p2=Pipeline(settings,other,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *args:99)
        with patch.object(p2.rules_pending,'finish_comment',return_value=False):
            with self.assertRaises(RulesHalt): p2.process(reply)
        with self.assertRaises(RulesHalt): p2.process(reply)
        self.assertEqual(1,len(comments))
        self.assertNotEqual('DONE',other.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])

    def test_comment_confirmation_requires_persisted_cas(self):
        from unittest.mock import patch
        from tg_triage.rules_intake import _finish_comment_proven
        from tg_triage.rules_pending_store import RulesPendingStore, RulesHalt
        from tg_triage.store import Store
        store=Store(':memory:'); self.addCleanup(store.close)
        pending=RulesPendingStore(store)
        self.assertTrue(pending.reserve_comment(1,2,'sandbox/triage',101))
        pipeline=SimpleNamespace(store=store,rules_pending=pending,config=SimpleNamespace(gh_repo='sandbox/triage'))
        with patch.object(pending,'finish_comment',return_value=False):
            with self.assertRaises(RulesHalt): _finish_comment_proven(pipeline,1,2,101,201)
        self.assertEqual('reserved',pending.comment_status(1,2))

    def test_future_direct_parent_cannot_authorize_lifecycle(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_intake import _resolve_reply_target
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        store=Store(':memory:'); self.addCleanup(store.close)
        parent=message(100,text='closed ticket'); store.enqueue(parent)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=100",(parent.chat_id,))
        base=config(); settings=replace(base,gh_repo='sandbox/triage')
        github=FakeGitHub()
        github.lifecycle_snapshot=lambda number: self.fail('impossible parent must not be read')
        pipeline=Pipeline(settings,store,None,github)
        with self.assertRaises(ValueError): _resolve_reply_target(pipeline,message(42,text='new',reply_to_id=100))

    def test_closed_unclear_question_and_direct_evidence_answer_append_once(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            calls=0
            def _ask(self,body,parser,attempts):
                self.calls+=1
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'bug','source_ids':[42]},'observed':{'value':'lỗi','source_ids':[42]},
                    'lifecycle':{'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='lỗi cũ'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); github.lifecycle_snapshot=lambda number:{'number':number,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
        comments=[]; github.add_comment=lambda number,body:comments.append((number,body)) or 201
        sent=[]; provider=Provider()
        pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,notice_sender=lambda *args:sent.append(args) or (99 if len(sent)==1 else 200+len(sent)))
        result=pipeline.process(message(42,text='lỗi',reply_to_id=10))
        self.assertEqual('lifecycle_question_sent',result.reason)
        self.assertIn('reply A hoặc B',sent[0][2]); self.assertEqual([],comments)
        answer=message(100,text='A',reply_to_id=99)
        result=ops_reply(pipeline,answer)
        self.assertEqual('followup',result.decision)
        self.assertEqual(101,comments[0][0]); self.assertEqual(1,len(comments))
        self.assertEqual('done',store.connection.execute('SELECT status FROM rules_lifecycle_answers').fetchone()[0])
        self.assertIsNone(ops_reply(pipeline,answer)); self.assertEqual(1,len(comments)); self.assertEqual(1,provider.calls)

    def test_confirmed_answer_never_completes_after_mapping_or_notice_failure(self):
        from unittest.mock import patch
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_pending_store import RulesHalt, check_before_recovery
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'bug','source_ids':[42]},'observed':{'value':'lỗi','source_ids':[42]},
                    'lifecycle':{'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        for failure in ('mapping','notice'):
            with self.subTest(failure=failure):
                db=self.directory/('append-'+failure+'.sqlite3'); store=Store(db)
                old=message(10,text='old'); store.enqueue(old)
                with store.connection:
                    store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
                base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                    telegram=replace(base.telegram,ops_chat_id=-1009876543210))
                github=FakeGitHub(); github.lifecycle_snapshot=lambda n:{'number':n,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
                comments=[]; github.add_comment=lambda n,body:comments.append((n,body)) or 201
                pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:99)
                pipeline.process(message(42,text='lỗi',reply_to_id=10))
                if failure=='mapping':
                    with store.connection:
                        store.connection.execute("CREATE TRIGGER ignore_mapping BEFORE UPDATE OF issue_number ON messages WHEN NEW.message_id=42 BEGIN SELECT RAISE(IGNORE); END")
                with patch('tg_triage.rules_intake._send_ops',return_value=False if failure=='notice' else True):
                    with self.assertRaises(RulesHalt): ops_reply(pipeline,message(100,text='A',reply_to_id=99))
                self.assertEqual(1,len(comments))
                self.assertNotEqual('done',store.connection.execute('SELECT status FROM rules_lifecycle_answers').fetchone()[0])
                store.close()
                with self.assertRaises(RulesHalt): check_before_recovery(db)
                self.assertEqual(1,len(comments))

    def test_sent_lifecycle_question_recovers_without_resend(self):
        from unittest.mock import patch
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_pending_store import check_before_recovery
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'bug','source_ids':[42]},'observed':{'value':'lỗi','source_ids':[42]},
                    'lifecycle':{'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        db=self.directory/'question-crash.sqlite3'; store=Store(db)
        old=message(10,text='lỗi cũ'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); github.lifecycle_snapshot=lambda n:{'number':n,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
        comments=[]; github.add_comment=lambda n,body:comments.append((n,body)) or 201
        sent=[]; pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or (99 if len(sent)==1 else 200+len(sent)))
        real=pipeline.rules_pending.transition_group
        def crash(batch,ordinal,old,new,**kwargs):
            if new=='DONE': raise RuntimeError('crash after sent question')
            return real(batch,ordinal,old,new,**kwargs)
        with patch.object(pipeline.rules_pending,'transition_group',side_effect=crash):
            with self.assertRaises(RuntimeError): pipeline.process(message(42,text='lỗi',reply_to_id=10))
        from tg_triage.rules_pending_store import RulesHalt, _recoverable_batch_groups
        store.enqueue(message(99,text='conflicting local source',chat_id=settings.telegram.ops_chat_id))
        with self.assertRaises(RulesHalt): _recoverable_batch_groups(store.connection)
        with store.connection:
            store.connection.execute('DELETE FROM messages WHERE chat_id=? AND message_id=99',(settings.telegram.ops_chat_id,))
        store.close(); check_before_recovery(db)
        store=Store(db); self.addCleanup(store.close)
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or (99 if len(sent)==1 else 200+len(sent)))
        self.assertEqual('DONE',store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])
        self.assertEqual('followup',ops_reply(pipeline,message(100,text='A',reply_to_id=99)).decision)
        self.assertEqual(1,len(comments)); self.assertEqual(2,len(sent))

    def test_unresolved_lifecycle_replies_never_enter_generic_routing(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_intake import _process_batch
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            calls=0
            def _ask(self,body,parser,attempts):
                self.calls+=1
                if self.calls>1: raise AssertionError('descendant must not reach model')
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'bug','source_ids':[42]},'observed':{'value':'lỗi','source_ids':[42]},
                    'lifecycle':{'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        for scenario in ('source','recurrence_descendant','changed_mapping','changed_source','changed_version','batch','closed_notice','expired_notice','unavailable','extra_context','attested_context','question_collision','old_answer_a','old_answer_b'):
            with self.subTest(scenario=scenario):
                store=Store(':memory:'); self.addCleanup(store.close)
                old=message(10,text='lỗi cũ'); store.enqueue(old)
                with store.connection:
                    store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
                base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                    telegram=replace(base.telegram,ops_chat_id=-1009876543210))
                github=FakeGitHub(); github.lifecycle_snapshot=lambda number:{'number':number,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
                comments=[]; github.add_comment=lambda number,body:comments.append((number,body)) or 201
                sent=[]; provider=Provider(); pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,notice_sender=lambda *args:sent.append(args) or (99 if len(sent)==1 else 200+len(sent)))
                if scenario=='unavailable': pipeline.notice_sender=None
                question=pipeline.process(message(42,text='lỗi',reply_to_id=10))
                self.assertEqual('lifecycle_question_unavailable' if scenario=='unavailable' else 'lifecycle_question_sent',question.reason)
                if scenario=='source':
                    result=pipeline.process(message(43,text='tạo ticket mới',reply_to_id=42))
                elif scenario=='unavailable':
                    result=pipeline.process(message(43,text='tạo ticket mới',reply_to_id=42))
                elif scenario in ('extra_context','attested_context'):
                    pipeline.process(message(43,text='Bổ sung bằng chứng cùng lỗi: thêm thông tin' if scenario=='attested_context' else 'lỗi đăng nhập khác',reply_to_id=42))
                    result=ops_reply(pipeline,message(100,text='A',reply_to_id=99))
                elif scenario=='closed_notice':
                    self.assertEqual('followup',ops_reply(pipeline,message(100,text='A',reply_to_id=99)).decision)
                    result=ops_reply(pipeline,message(101,text='tạo ticket mới',reply_to_id=99))
                elif scenario=='expired_notice':
                    with store.connection:
                        store.connection.execute("UPDATE rules_topics SET status='EXPIRED' WHERE topic_id=?",(f'{old.chat_id}:42',))
                    result=ops_reply(pipeline,message(100,text='tạo ticket mới',reply_to_id=99))
                elif scenario=='recurrence_descendant':
                    pipeline.config=replace(pipeline.config,max_issues_per_hour=0)
                    self.assertEqual('lifecycle_recurrence_answer_pending',ops_reply(pipeline,message(100,text='B',reply_to_id=99)).reason)
                    result=ops_reply(pipeline,message(101,text='tạo ticket mới',reply_to_id=100))
                elif scenario=='changed_mapping':
                    with store.connection:
                        store.connection.execute("UPDATE rules_lifecycle_questions SET evidence_answer='B',recurrence_answer='A'")
                    result=ops_reply(pipeline,message(100,text='A',reply_to_id=99))
                elif scenario=='changed_source':
                    with store.connection:
                        store.connection.execute("UPDATE messages SET text='changed source' WHERE chat_id=? AND message_id=42",(old.chat_id,))
                    result=ops_reply(pipeline,message(100,text='B',reply_to_id=99))
                elif scenario=='changed_version':
                    github.lifecycle_snapshot=lambda n:{'number':n,'repo':'sandbox/triage','state':'closed','observed_at':'later','updated_at':'version-2'}
                    result=ops_reply(pipeline,message(100,text='B',reply_to_id=99))
                elif scenario=='question_collision':
                    store.enqueue(message(99,text='conflicting local source',chat_id=settings.telegram.ops_chat_id))
                    result=ops_reply(pipeline,message(100,text='A',reply_to_id=99))
                elif scenario in ('old_answer_a','old_answer_b'):
                    result=ops_reply(pipeline,message(43,text='A' if scenario=='old_answer_a' else 'B',reply_to_id=99))
                else:
                    sources=[message(100,text='A',reply_to_id=42),message(101,text='/question')]
                    for source in sources: store.enqueue(source); store.start_classifying(source)
                    result=_process_batch(pipeline,sources)
                if scenario=='batch':
                    self.assertEqual([],comments); self.assertEqual([],github.calls); self.assertEqual(1,provider.calls)
                    self.assertEqual('pending_review',store.get(sources[0].chat_id,100)['decision'])
                    self.assertNotEqual('pending_review',store.get(sources[1].chat_id,101)['decision'])
                    continue
                if scenario=='closed_notice':
                    self.assertEqual('chat',result.decision); self.assertEqual('unbound_ops_reply',result.reason); self.assertEqual(1,len(comments)); self.assertEqual(1,provider.calls)
                    continue
                if scenario in ('expired_notice','recurrence_descendant'):
                    self.assertEqual('chat',result.decision); self.assertEqual('unbound_ops_reply',result.reason)
                    self.assertEqual([],comments); self.assertEqual([],github.calls); self.assertEqual(1,provider.calls)
                    continue
                if scenario=='attested_context':
                    self.assertEqual('followup',result.decision); self.assertEqual(1,len(comments)); self.assertIn('thêm thông tin',comments[0][1])
                    self.assertEqual(1,provider.calls); self.assertEqual([],github.calls)
                    continue
                self.assertEqual('pending_review',result.decision)
                self.assertEqual([],comments); self.assertEqual([],github.calls); self.assertEqual(1,provider.calls)

    def test_confirmed_recurrence_creates_verified_related_episode_and_routes_notice_reply(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                facts={'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},'observed':{'value':'kẹt giấy','source_ids':[mid]}}
                if mid in (42,203): facts['lifecycle']={'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}
                return parser(json.dumps({'groups':[{'message_ids':[mid],'facts':facts}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='lỗi cũ'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); github.lifecycle_snapshot=lambda number:{'number':number,'repo':'sandbox/triage','state':'closed' if number==101 else 'open','observed_at':'now','updated_at':'version-1'}
        github.verify_created_issue=lambda number,issue:number
        comments=[]; github.add_comment=lambda number,body:comments.append((number,body)) or 201
        sent=[]; notice_ids=[]
        def sender(*args):
            sent.append(args)
            notice_id=99 if len(sent)==1 else 200 if len(sent)==2 else 300+10*len(sent)
            notice_ids.append(notice_id)
            return notice_id
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=sender)
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(42,text='POS kẹt giấy',reply_to_id=10)).reason)
        result=ops_reply(pipeline,message(100,text='B',reply_to_id=99))
        self.assertEqual('issue',result.decision); self.assertEqual(42,result.issue_number)
        self.assertEqual(1,len(github.calls)); self.assertIn('/issues/101',github.calls[0].body)
        self.assertEqual(101,store.get(old.chat_id,10)['issue_number'])
        self.assertEqual(42,store.get(old.chat_id,42)['issue_number'])
        self.assertEqual('done',store.connection.execute('SELECT status FROM rules_related_episodes').fetchone()[0])
        self.assertEqual('followup',pipeline.process(message(201,text='thêm lỗi',reply_to_id=42)).decision)
        self.assertEqual(42,comments[0][0])
        ignored=ops_reply(pipeline,message(202,text='B',reply_to_id=99))
        self.assertEqual(('chat','unbound_ops_reply'),(ignored.decision,ignored.reason))
        self.assertEqual(1,len(github.calls))
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(203,text='lỗi',reply_to_id=10)).reason)
        next_notice=notice_ids[-1]
        self.assertEqual('lifecycle_recurrence_answer_pending',ops_reply(pipeline,message(next_notice+1,text='B',reply_to_id=next_notice)).reason)
        self.assertEqual(1,len(github.calls))
        other_chat=-1009876500001
        other_parent=message(10,text='lỗi cũ',chat_id=other_chat); store.enqueue(other_parent)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(other_chat,))
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(42,text='POS kẹt giấy',chat_id=other_chat,reply_to_id=10)).reason)
        other_notice=notice_ids[-1]
        self.assertEqual('lifecycle_recurrence_answer_pending',ops_reply(pipeline,message(other_notice+1,text='B',reply_to_id=other_notice)).reason)
        self.assertEqual(1,len(github.calls))

    def test_uncertain_related_issue_never_replays_on_restart(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_pending_store import RulesHalt, check_before_recovery
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'bug','source_ids':[42]},'object':{'value':'POS','source_ids':[42]},'observed':{'value':'kẹt giấy','source_ids':[42]},
                    'lifecycle':{'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        for failure in ('readback','notice','mapping','collision'):
            with self.subTest(failure=failure):
                db=self.directory/(failure+'.sqlite3'); store=Store(db); self.addCleanup(store.close)
                old=message(10,text='lỗi cũ'); store.enqueue(old)
                with store.connection:
                    store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
                base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                    telegram=replace(base.telegram,ops_chat_id=-1009876543210))
                github=FakeGitHub(); github.lifecycle_snapshot=lambda n:{'number':n,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
                github.verify_created_issue=lambda n,issue: None if failure=='readback' else n
                calls=[]
                def sender(*args):
                    calls.append(args)
                    if failure=='notice' and len(calls)>1: raise OSError('unknown delivery')
                    return 99 if len(calls)==1 else 101
                pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=sender)
                pipeline.process(message(42,text='POS kẹt giấy',reply_to_id=10))
                if failure=='collision': store.enqueue(message(101,text='conflicting local source',chat_id=settings.telegram.ops_chat_id))
                if failure=='mapping':
                    with store.connection:
                        store.connection.execute("""CREATE TRIGGER ignore_episode_mapping BEFORE UPDATE OF issue_number ON messages
                            WHEN NEW.message_id=42 AND NEW.reason='lifecycle_related_episode' BEGIN SELECT RAISE(IGNORE); END""")
                with self.assertRaises(RulesHalt): ops_reply(pipeline,message(100,text='B',reply_to_id=99))
                self.assertEqual(1,len(github.calls)); store.close()
                with self.assertRaises(RulesHalt): check_before_recovery(db)
                self.assertEqual(1,len(github.calls))

    def test_question_about_closed_episode_notice_recovers_after_send(self):
        from unittest.mock import patch
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_pending_store import check_before_recovery
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                return parser(json.dumps({'groups':[{'message_ids':[mid],'facts':{
                    'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},
                    'observed':{'value':'kẹt giấy','source_ids':[mid]},
                    'lifecycle':{'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        db=self.directory/'episode-question.sqlite3'; store=Store(db)
        old=message(10,text='lỗi cũ'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); github.lifecycle_snapshot=lambda n:{'number':n,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
        github.verify_created_issue=lambda n,issue:n
        sent=[]; pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or (97+2*len(sent)))
        pipeline.process(message(42,text='POS kẹt giấy',reply_to_id=10))
        self.assertEqual('issue',ops_reply(pipeline,message(100,text='B',reply_to_id=99)).decision)
        real=pipeline.rules_pending.transition_group
        def crash(batch,ordinal,old,new,**kwargs):
            if new=='DONE': raise RuntimeError('crash after sent question')
            return real(batch,ordinal,old,new,**kwargs)
        with patch.object(pipeline.rules_pending,'transition_group',side_effect=crash):
            with self.assertRaises(RuntimeError): pipeline.process(message(102,text='POS kẹt giấy',reply_to_id=42))
        from tg_triage.rules_pending_store import RulesHalt, _recoverable_batch_groups
        with store.connection:
            store.connection.execute("INSERT INTO group_notices(chat_id,message_id,kind,status,reserved_at,notice_message_id) VALUES(?,777,'issue','sent','now',103)",(settings.telegram.ops_chat_id,))
        with self.assertRaises(RulesHalt): _recoverable_batch_groups(store.connection)
        with store.connection:
            store.connection.execute('DELETE FROM group_notices WHERE chat_id=? AND message_id=777',(settings.telegram.ops_chat_id,))
        store.enqueue(message(103,text='conflicting local source',chat_id=settings.telegram.ops_chat_id))
        with self.assertRaises(RulesHalt): _recoverable_batch_groups(store.connection)
        with store.connection:
            store.connection.execute('DELETE FROM messages WHERE chat_id=? AND message_id=103',(settings.telegram.ops_chat_id,))
        store.close(); check_before_recovery(db)
        store=Store(db); self.addCleanup(store.close)
        Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or (97+2*len(sent)))
        self.assertEqual('DONE',store.connection.execute("SELECT status FROM rules_batch_groups WHERE batch_id=?",(f'batch:{old.chat_id}:102',)).fetchone()[0])
        self.assertEqual(3,len(sent))

    def test_new_recurrence_after_latest_closed_episode_creates_one_successor(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                return parser(json.dumps({'groups':[{'message_ids':[mid],'facts':{
                    'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},
                    'observed':{'value':'kẹt giấy','source_ids':[mid]},
                    'lifecycle':{'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='old'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); states={42:'open'}; snapshots=[]; comments=[]
        def lifecycle(n):
            snapshots.append(n)
            return {'number':n,'repo':'sandbox/triage','state':states.get(n,'closed'),'observed_at':'now','updated_at':'version-1'}
        github.lifecycle_snapshot=lifecycle; github.add_comment=lambda n,body:comments.append((n,body)) or 201
        github.verify_created_issue=lambda n,issue:n
        def create(issue):
            github.calls.append(issue)
            number=41+len(github.calls)
            return number,f'https://github.com/sandbox/triage/issues/{number}'
        github.create_issue=create
        notices=iter((99,101,501,105,107)); pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:next(notices))
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(42,text='POS kẹt giấy',reply_to_id=10)).reason)
        self.assertEqual(42,ops_reply(pipeline,message(100,text='B',reply_to_id=99)).issue_number)
        self.assertEqual('followup',pipeline.process(message(102,text='POS kẹt giấy',reply_to_id=42)).decision)
        self.assertEqual(42,comments[0][0]); self.assertEqual(1,len(github.calls))
        states[42]='closed'
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(104,text='POS kẹt giấy',reply_to_id=42)).reason)
        result=ops_reply(pipeline,message(106,text='B',reply_to_id=105))
        self.assertEqual(('issue',43),(result.decision,result.issue_number))
        self.assertEqual(2,len(github.calls)); self.assertIn('/issues/42',github.calls[1].body)
        self.assertEqual(42,store.get(old.chat_id,42)['issue_number'])
        self.assertEqual(42,store.get(old.chat_id,102)['issue_number'])
        self.assertEqual(43,store.get(old.chat_id,104)['issue_number']); self.assertIn(42,snapshots)
        self.assertIsNone(ops_reply(pipeline,message(106,text='B',reply_to_id=105)))
        self.assertEqual(2,len(github.calls))

    def test_model_reused_success_is_not_in_outgoing_contract(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                assert 'reused_success' not in body['format']['properties']['groups']['items']['properties']['facts']['properties']['lifecycle']['properties']['kind']['enum']
                return parser(json.dumps({'groups':[{'message_ids':[mid],'facts':{
                    'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},
                    'observed':{'value':'lỗi lặp lại','source_ids':[mid]},
                    'lifecycle':{'kind':'reused_success','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='old'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); github.lifecycle_snapshot=lambda n:{'number':n,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
        sent=[]; pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 99)
        result=pipeline.process(message(42,text='POS lỗi lặp lại',reply_to_id=10))
        self.assertEqual('rules_hold',result.reason)
        self.assertEqual([],sent)
        self.assertEqual([],github.calls)

    def test_three_successive_episodes_in_recurrence_chain(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                return parser(json.dumps({'groups':[{'message_ids':[mid],'facts':{
                    'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},
                    'observed':{'value':'kẹt giấy','source_ids':[mid]},
                    'lifecycle':{'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='old'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); states={42:'open',43:'open',44:'open'}
        github.lifecycle_snapshot=lambda n:{'number':n,'repo':'sandbox/triage','state':states.get(n,'closed'),'observed_at':'now','updated_at':f'version-{n}'}
        github.verify_created_issue=lambda n,issue:n
        def create(issue):
            github.calls.append(issue)
            number=41+len(github.calls)
            return number,f'https://github.com/sandbox/triage/issues/{number}'
        github.create_issue=create
        notices=iter((99,101,105,107,109,111))
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:next(notices))
        # Ep 1: Parent 101 -> Issue 42
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(42,text='POS kẹt giấy',reply_to_id=10)).reason)
        self.assertEqual(42,ops_reply(pipeline,message(100,text='B',reply_to_id=99)).issue_number)
        states[42]='closed'
        # Ep 2: Issue 42 -> Issue 43
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(104,text='POS kẹt giấy',reply_to_id=42)).reason)
        self.assertEqual(43,ops_reply(pipeline,message(106,text='B',reply_to_id=105)).issue_number)
        states[43]='closed'
        # Ep 3: Issue 43 -> Issue 44
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(108,text='POS kẹt giấy',reply_to_id=104)).reason)
        result3=ops_reply(pipeline,message(110,text='B',reply_to_id=109))
        self.assertEqual(('issue',44),(result3.decision,result3.issue_number))
        self.assertEqual(3,len(github.calls))
        self.assertIn('/issues/42',github.calls[1].body)
        self.assertIn('/issues/43',github.calls[2].body)
        self.assertEqual(44,store.get(old.chat_id,108)['issue_number'])

    def test_recurrence_obeys_markdown_readiness_and_preserves_intent(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        for kind in ('task','feedback','insufficient'):
            with self.subTest(kind=kind):
                class Provider:
                    settings=SimpleNamespace(model='test'); think=False
                    def _ask(self,body,parser,attempts):
                        intent='bug' if kind=='insufficient' else kind
                        facts={'intent':{'value':intent,'source_ids':[42]},'lifecycle':{'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}}
                        if kind=='task': facts.update(object={'value':'bảng điều khiển','source_ids':[42]},work={'value':'đổi bố cục','source_ids':[42]})
                        if kind=='feedback': facts.update(problem={'value':'phản hồi quá chậm','source_ids':[42]})
                        return parser(json.dumps({'groups':[{'message_ids':[42],'facts':facts}]}))
                store=Store(':memory:'); self.addCleanup(store.close)
                old=message(10,text='yêu cầu cũ'); store.enqueue(old)
                with store.connection:
                    store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
                base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                    telegram=replace(base.telegram,ops_chat_id=-1009876543210))
                github=FakeGitHub(); github.lifecycle_snapshot=lambda n:{'number':n,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
                github.verify_created_issue=lambda n,issue:n
                sent=[]; pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *args:sent.append(args) or (97+2*len(sent)))
                text='bảng điều khiển đổi bố cục' if kind=='task' else 'phản hồi quá chậm' if kind=='feedback' else 'lỗi'
                pipeline.process(message(42,text=text,reply_to_id=10))
                result=ops_reply(pipeline,message(100,text='B',reply_to_id=99))
                if kind=='insufficient':
                    self.assertEqual('lifecycle_recurrence_answer_pending',result.reason); self.assertEqual([],github.calls)
                else:
                    self.assertEqual('issue',result.decision); self.assertIn(kind,github.calls[0].labels)

    def test_status_batch_does_not_absorb_independent_new_issue(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_intake import _process_batch
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states', 'facts', 'readiness', 'questions', 'channels', 'examples'):
            (self.directory / (name + '.md')).write_text((ROOT / (name + '.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[
                    {'message_ids':[40],'facts':{'intent':{'value':'status','source_ids':[40]}}},
                    {'message_ids':[41],'facts':{'intent':{'value':'bug','source_ids':[41]},
                     'object':{'value':'POS','source_ids':[41]},'observed':{'value':'kẹt giấy','source_ids':[41]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='yêu cầu cũ'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); sent=[]
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a) or (100+len(sent)))
        sources=[message(40,text='trạng thái?',reply_to_id=10),message(41,text='POS kẹt giấy')]
        for source in sources: store.enqueue(source); store.start_classifying(source)
        _process_batch(pipeline,sources)
        self.assertEqual(len(github.calls),1)
        self.assertEqual(store.get(sources[0].chat_id,40)['reason'],'semantic_status')
        self.assertEqual(store.get(sources[1].chat_id,41)['issue_number'],42)

    def test_status_sent_before_checkpoint_is_not_sent_again_after_restart(self):
        from unittest.mock import patch
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_intake import _process_batch
        from tg_triage.rules_pending_store import RulesPendingStore, check_before_recovery
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{'intent':{'value':'status','source_ids':[42]}}}]}))
        db=self.directory/'crash.sqlite3'; store=Store(db)
        base=config(); settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        sent=[]; pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a) or 99)
        source=message(42,text='trạng thái?'); store.enqueue(source); store.start_classifying(source)
        real=pipeline.rules_pending.transition_group
        def crash(batch,ordinal,old,new,**kwargs):
            if new=='DONE': raise RuntimeError('simulated crash after notice')
            return real(batch,ordinal,old,new,**kwargs)
        with patch.object(pipeline.rules_pending,'transition_group',side_effect=crash):
            with self.assertRaises(RuntimeError): _process_batch(pipeline,[source])
        self.assertEqual(len(sent),1)
        original_body=store.connection.execute('SELECT body FROM rules_readonly_notices').fetchone()[0]
        from tg_triage.rules_pending_store import RulesHalt, _recoverable_batch_groups
        with store.connection:
            store.connection.execute("UPDATE rules_readonly_notices SET kind='ANALYZE'")
        with self.assertRaises(RulesHalt): _recoverable_batch_groups(store.connection)
        with store.connection:
            store.connection.execute("UPDATE rules_readonly_notices SET kind='STATUS', body='wrong content'")
        with self.assertRaises(RulesHalt): _recoverable_batch_groups(store.connection)
        with store.connection:
            store.connection.execute('UPDATE rules_readonly_notices SET body=?',(original_body,))
        store.close(); check_before_recovery(db)
        store=Store(db); self.addCleanup(store.close)
        RulesPendingStore(store)
        self.assertEqual(store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0],'DONE')
        self.assertEqual(len(sent),1)

    def test_recovery_closes_readonly_topic_when_crash_precedes_close(self):
        from unittest.mock import patch
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_pending_store import check_before_recovery
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{'intent':{'value':'status','source_ids':[42]}}}]}))
        db=self.directory/'close-crash.sqlite3'; store=Store(db)
        base=config(); settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        sent=[]; pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 99)
        target=message(10,text='original'); store.enqueue(target)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(target.chat_id,))
        source=message(42,text='trạng thái?',reply_to_id=10); topic=f'{source.chat_id}:42'
        real=pipeline.rules_pending.close
        def crash(value,status):
            if value==topic: raise RuntimeError('crash before close')
            return real(value,status)
        with patch.object(pipeline.rules_pending,'close',side_effect=crash):
            with self.assertRaises(RuntimeError): pipeline.process(source)
        self.assertEqual('OPEN',store.connection.execute('SELECT status FROM rules_topics WHERE topic_id=?',(topic,)).fetchone()[0])
        store.close(); check_before_recovery(db)
        store=Store(db); self.addCleanup(store.close)
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 99)
        self.assertEqual('CLOSED',store.connection.execute('SELECT status FROM rules_topics WHERE topic_id=?',(topic,)).fetchone()[0])
        self.assertEqual('DONE',store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])
        self.assertIsNone(pipeline.rules_pending.topic_for_source_reply(source.chat_id,42,snapshot.limits['pending_expiry_days']))
        self.assertEqual(1,len(sent))

    def test_old_confirmation_is_not_proof_of_status_notice(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{'intent':{'value':'status','source_ids':[42]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        base=config(); settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        source=message(42,text='trạng thái?')
        store.reserve_group_notice(source.chat_id,source.message_id,'issue')
        store.finish_group_notice(source.chat_id,source.message_id,'sent',77)
        sent=[]; pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 99)
        result=pipeline.process(source)
        self.assertEqual('pending_review',result.decision)
        self.assertEqual('readonly_notice_conflict',result.reason)
        self.assertEqual([],sent)
        self.assertEqual('BLOCKED',store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])

    def test_document_excerpts_are_rechecked_against_saved_proof(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeDownloader
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                review=fixture_analysis_review(body)
                if review is not None:return parser(review)
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'analyze','source_ids':[42]},
                    'task':{'title':'Kiểm tra hiển thị','work':'Phân tích phần bị cắt','acceptance':['Nêu vị trí bị cắt']}}}]}))
        text='Kiểm tra hiển thị. Phân tích phần bị cắt. Nêu vị trí bị cắt'
        for changed in (False,True):
            with self.subTest(changed=changed):
                store=Store(':memory:'); self.addCleanup(store.close)
                base=config(); settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
                    telegram=replace(base.telegram,ops_chat_id=-1009876543210))
                data=FakeDownloader({42:(text.encode('utf8'),'request.md')}); calls=[]
                def download(*args):
                    calls.append(1)
                    if changed and len(calls)>1:
                        return FakeDownloader({42:(b'changed','request.md')})(*args)
                    return data(*args)
                sent=[]; pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,downloader=download,
                    notice_sender=lambda *a:sent.append(a) or 99)
                result=pipeline.process(message(42,text='Phân tích tài liệu',has_document=True,
                    document_name='request.md',document_mime='text/markdown'))
                if changed:
                    self.assertEqual('rules_document_provenance_changed',result.reason)
                    self.assertEqual([],sent)
                else:
                    self.assertEqual('semantic_analyze',result.reason)
                    self.assertIn('\nViệc:',sent[0][2])
                    self.assertIn('Thực hiện: Phân tích phần bị cắt',sent[0][2])

    def test_processed_long_document_rechecks_original_digest_on_resume(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeDownloader
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def analyze_document_part(self,row,part):
                ids=[b['block_id'] for b in part['blocks'] if 'Kiểm tra hiển thị' in b['text']]
                return {'status':'relevant' if ids else 'irrelevant','block_ids':ids}
            def _ask(self,body,parser,attempts):
                review=fixture_analysis_review(body)
                if review is not None:return parser(review)
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'analyze','source_ids':[42]},
                    'task':{'title':'Kiểm tra hiển thị','work':'Phân tích phần bị cắt','acceptance':['Nêu vị trí bị cắt']}}}]}))
        text='Kiểm tra hiển thị. Phân tích phần bị cắt. Nêu vị trí bị cắt\n'+('context only\n'*2400)
        for changed in (False,True):
            with self.subTest(changed=changed):
                store=Store(':memory:'); self.addCleanup(store.close)
                base=config(); settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
                    telegram=replace(base.telegram,ops_chat_id=-1009876543210))
                downloader=FakeDownloader({42:(text.encode('utf8'),'request.md')}); calls=[]
                def download(*args):
                    calls.append(1)
                    return (FakeDownloader({42:((text+'changed').encode('utf8'),'request.md')})(*args)
                            if changed and len(calls)>1 else downloader(*args))
                sent=[]; pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,downloader=download,
                    notice_sender=lambda *a:sent.append(a) or 99)
                result=pipeline.process(message(42,text='Phân tích tài liệu',has_document=True,
                    document_name='request.md',document_mime='text/markdown'))
                if changed:
                    self.assertEqual('rules_document_provenance_changed',result.reason); self.assertEqual([],sent)
                else:
                    self.assertEqual('semantic_analyze',result.reason); self.assertEqual(1,len(sent))

    def test_single_plan_survives_crash_before_notice_without_reclassification(self):
        from unittest.mock import patch
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_pending_store import RulesPendingStore, check_before_recovery
        from tg_triage.rules_intake import sweep
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            calls=0
            def _ask(self,body,parser,attempts):
                self.calls+=1
                if self.calls>1: raise AssertionError('plan must not be reclassified')
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{'intent':{'value':'status','source_ids':[42]}}}]}))
        db=self.directory/'single-crash.sqlite3'; store=Store(db)
        base=config(); settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        provider=Provider(); sent=[]
        pipeline=Pipeline(settings,store,provider,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 99)
        real_finish=store.finish
        def crash_after_finish(source,result,*args,**kwargs):
            real_finish(source,result,*args,**kwargs)
            if result.reason=='semantic_status': raise RuntimeError('crash after message finish')
        with patch.object(store,'finish',side_effect=crash_after_finish):
            with self.assertRaises(RuntimeError): pipeline.process(message(42,text='trạng thái?'))
        self.assertEqual('RESERVED',store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])
        self.assertEqual('semantic_status',store.get(message(42).chat_id,42)['reason'])
        self.assertIsNone(store.connection.execute('SELECT 1 FROM group_notices').fetchone())
        store.close(); check_before_recovery(db)
        store=Store(db); self.addCleanup(store.close)
        pipeline=Pipeline(settings,store,provider,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 99)
        self.assertEqual('PLANNED',store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])
        sweep(pipeline)
        self.assertEqual(1,provider.calls)
        self.assertEqual(1,len(sent))

    def test_pristine_legacy_source_released_into_actual_rules_status_route(self):
        import hashlib
        from tg_triage.legacy_reconciliation import transition_pristine, release_pristine
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_intake import sweep
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{'intent':{'value':'status','source_ids':[42]}}}]}))
        db=self.directory/'transition-runtime.sqlite3'; store=Store(db)
        source=message(42,text='trạng thái?'); store.enqueue(source); store.close()
        common={'offline_copy':True,'chat_id':source.chat_id,'message_id':42,'operator':'test','reason':'reviewed'}
        receipt=transition_pristine(db,{**common,'sha256':hashlib.sha256(db.read_bytes()).hexdigest(),'decision':'queued'})
        release_pristine(db,{**common,'sha256':hashlib.sha256(db.read_bytes()).hexdigest(),'release_for_rules':True,'stage_receipt_sha256':receipt['stage_receipt_sha256']})
        store=Store(db); self.addCleanup(store.close)
        base=config(); settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        sent=[]; pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 99)
        sweep(pipeline); sweep(pipeline)
        self.assertEqual(1,len(sent))
        self.assertEqual('semantic_status',store.get(source.chat_id,42)['reason'])
        self.assertEqual('OPEN',store.connection.execute('SELECT status FROM rules_topics WHERE topic_id=?',(f'{source.chat_id}:42',)).fetchone()[0])
        self.assertEqual('question',store.connection.execute('SELECT kind FROM rules_notices').fetchone()[0])

    def test_unimplemented_recurrence_and_its_reply_never_create_replacement(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                return parser(json.dumps({'groups':[{'message_ids':[mid],'facts':{
                    'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},
                    'observed':{'value':'lỗi lại','source_ids':[mid]},
                    'lifecycle':{'kind':'recurrence','knownworking_source_ids':[mid],'recurrence_source_ids':[mid]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='lỗi cũ'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); github.lifecycle_snapshot=lambda number: {'number':number,'repo':'sandbox/triage','state':'closed','observed_at':'now','updated_at':'version-1'}
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot)
        held=pipeline.process(message(42,text='POS lỗi lại',reply_to_id=10))
        self.assertEqual(held.reason,'lifecycle_recurrence_pending')
        result=pipeline.process(message(43,text='POS lỗi lại',reply_to_id=42))
        self.assertEqual(result.decision,'pending_review')
        self.assertEqual(github.calls,[])

    def test_closed_evidence_claim_cannot_override_visible_recurrence(self):
        from tg_triage.rules_intake import evaluate_batch
        from tests.helpers import message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'bug','source_ids':[42]},
                    'lifecycle':{'kind':'evidence','knownworking_source_ids':[],'recurrence_source_ids':[]}}}]}))
        result=evaluate_batch(snapshot,Provider(),[message(42,text='Hôm qua đã chạy tốt, hôm nay lỗi lại',reply_to_id=10)],reply_target_for=lambda _:'ticket_closed')
        self.assertNotEqual(result[0][1].state,'APPEND_CLOSED')

    def test_analysis_task_fabricated_from_unrelated_text_is_not_returned(self):
        from tg_triage.rules_intake import evaluate_batch
        from tests.helpers import message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                review=fixture_analysis_review(body)
                if review is not None:return parser(review)
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'analyze','source_ids':[42]},
                    'task':{'title':'Xóa dữ liệu','work':'Xóa toàn bộ cơ sở dữ liệu','acceptance':['Dữ liệu đã bị xóa']}}}]}))
        result=evaluate_batch(snapshot,Provider(),[message(42,text='Phân tích ảnh lỗi giao diện')])
        self.assertIsNone(result)
        result=evaluate_batch(snapshot,Provider(),[message(42,text='Không Xóa dữ liệu. Không Xóa toàn bộ cơ sở dữ liệu. Dữ liệu đã bị xóa')])
        self.assertIsNone(result)

    def test_excerpt_cannot_drop_speaker_or_change_case(self):
        from tg_triage.rules_intake import _task_grounded
        from tests.helpers import message
        task={'title':'đã duyệt','work':'Kiểm tra lỗi','acceptance':['Báo kết quả']}
        self.assertFalse(_task_grounded(task,[message(42,text='Bạn đã duyệt. Kiểm tra lỗi. Báo kết quả')]))
        self.assertFalse(_task_grounded(task,[message(42,text='Đã duyệt. Kiểm tra lỗi. Báo kết quả')]))
        self.assertTrue(_task_grounded(task,[message(42,text='đã duyệt. Kiểm tra lỗi. Báo kết quả')]))

    def test_excerpt_notice_respects_business_length_limit(self):
        from tg_triage.analysis_task import render_excerpts
        task={'title':'a'*90,'work':'b'*160,'acceptance':['c'*90,'d'*90]}
        self.assertLessEqual(len(render_excerpts(task)),500)

    def test_explicit_authored_analysis_task_renders_documented_three_lines(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                review=fixture_analysis_review(body)
                if review is not None:return parser(review)
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'analyze','source_ids':[42]},
                    'task':{'title':'Kiểm tra lỗi','work':'Phân tích yêu cầu','acceptance':['Nêu vấn đề']}}}]}))
        task_text='Việc: Kiểm tra lỗi\nThực hiện: Phân tích yêu cầu\nĐạt: Nêu vấn đề'
        store=Store(':memory:'); self.addCleanup(store.close)
        base=config(); settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        sent=[]; pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 99)
        result=pipeline.process(message(42,text='Phân tích:\n'+task_text))
        self.assertEqual('semantic_analyze',result.reason)
        self.assertTrue(sent[0][2].endswith('\n'+task_text))
        from tg_triage.analysis_task import explicit_task
        task={'title':'Kiểm tra lỗi','work':'Phân tích yêu cầu','acceptance':['Nêu vấn đề']}
        self.assertTrue(explicit_task(task,('Phân tích:\n'+task_text).replace('\n','\r\n'),False))
        self.assertFalse(explicit_task(task,('Phân tích:\n'+task_text).replace('\n','\r\n'),True))
        self.assertFalse(explicit_task(task,task_text,True))
        self.assertFalse(explicit_task(task,task_text,False))

    def test_analyze_grounded_via_reply_read_context(self):
        from tg_triage.models import Decision
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                review=fixture_analysis_review(body)
                if review is not None:return parser(review)
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'analyze','source_ids':[42]},
                    'task':{'title':'Kiểm tra giao diện','work':'Phân tích bảng điều khiển','acceptance':['Hiển thị đầy đủ','Không lỗi bố cục']}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        prior=message(10,text='Kiểm tra giao diện. Phân tích bảng điều khiển. Hiển thị đầy đủ. Không lỗi bố cục.')
        store.enqueue(prior)
        store.finish(prior,Decision('chat','rule',case_key='test',reason='prior_context'))
        base=config(); settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        sent=[]; pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 99)
        result=pipeline.process(message(42,text='Phân tích yêu cầu trên',reply_to_id=10))
        self.assertEqual('semantic_analyze',result.reason)
        self.assertEqual(1,len(sent))
        self.assertIn('\nViệc:',sent[0][2])
        self.assertIn('Kiểm tra giao diện',sent[0][2])
        self.assertIn('Không lỗi bố cục',sent[0][2])

    def test_deferred_append_is_not_checkpointed_as_done(self):
        from unittest.mock import patch
        from tg_triage.models import Decision
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{
                    'intent':{'value':'bug','source_ids':[42]},'observed':{'value':'lỗi','source_ids':[42]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='lỗi cũ'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); github.lifecycle_snapshot=lambda number:{'number':number,'repo':'sandbox/triage','state':'open','observed_at':'now','updated_at':'version-1'}
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot)
        with patch('tg_triage.rules_intake._append_batch_group',return_value=Decision('pending_review','rule',reason='lifecycle_lookup_failed')):
            result=pipeline.process(message(42,text='vẫn lỗi',reply_to_id=10))
        self.assertEqual(result.decision,'pending_review')
        self.assertEqual(store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0],'BLOCKED')
        self.assertEqual(store.get(old.chat_id,42)['reason'],'lifecycle_lookup_failed')

    def test_uncertain_read_only_notice_halts_and_is_not_resent(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_pending_store import RulesHalt
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{'intent':{'value':'status','source_ids':[42]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        base=config(); settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        calls=[]
        def unknown(*args): calls.append(args); raise OSError('unknown delivery')
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,notice_sender=unknown)
        with self.assertRaises(RulesHalt): pipeline.process(message(42,text='trạng thái?'))
        with self.assertRaises(RulesHalt): pipeline.process(message(42,text='trạng thái?'))
        self.assertEqual(len(calls),1)
        self.assertEqual(store.connection.execute('SELECT status FROM rules_notices').fetchone()[0],'ambiguous')

    def test_notice_finish_without_persistence_never_completes_checkpoint(self):
        from unittest.mock import patch
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_pending_store import RulesHalt
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{'intent':{'value':'status','source_ids':[42]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        base=config(); settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        sent=[]; pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 99)
        with patch.object(pipeline.rules_pending,'finish_notice',return_value=None):
            with self.assertRaises(RulesHalt): pipeline.process(message(42,text='trạng thái?'))
        self.assertEqual(1,len(sent))
        self.assertEqual('reserved',store.connection.execute('SELECT status FROM rules_notices').fetchone()[0])
        self.assertNotEqual('DONE',store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])

    def test_notice_verification_read_failure_raises_rules_halt(self):
        import sqlite3
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_pending_store import RulesHalt
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{'intent':{'value':'status','source_ids':[42]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        base=config(); settings=replace(base,mode='live',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        def sender(*args):
            store.connection.set_authorizer(lambda action,*rest: sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_SELECT else sqlite3.SQLITE_OK)
            return 99
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,notice_sender=sender)
        try:
            with self.assertRaises(RulesHalt): pipeline.process(message(42,text='trạng thái?'))
        finally:
            store.connection.set_authorizer(None)
        self.assertNotEqual('DONE',store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])

    def test_sweep_uses_extended_status_reply_path(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parser,attempts):
                return parser(json.dumps({'groups':[{'message_ids':[42],'facts':{'intent':{'value':'status','source_ids':[42]}}}]}))
        class NoGitHub:
            def __getattr__(self,name): raise AssertionError('STATUS must not access GitHub')
        store=Store(':memory:'); self.addCleanup(store.close)
        old=message(10,text='nguồn'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(old.chat_id,))
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        sent=[]; pipeline=Pipeline(settings,store,Provider(),NoGitHub(),rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 99)
        reply=message(42,text='trạng thái?',reply_to_id=10); store.enqueue(reply)
        topic=f'{reply.chat_id}:{reply.message_id}'; pipeline.rules_pending.create(topic,reply.chat_id,reply.message_id)
        with store.connection:
            store.connection.execute("UPDATE rules_topics SET phase='HOLD' WHERE topic_id=?",(topic,))
            store.connection.execute("UPDATE messages SET decision='pending_review',reason='rules_hold' WHERE chat_id=? AND message_id=?",(reply.chat_id,reply.message_id))
        pipeline.close_due_cases()
        self.assertEqual(store.get(reply.chat_id,42)['reason'],'semantic_status')
        self.assertIn('/issues/101',sent[0][2])

    def test_cached_closed_append_from_old_model_label_cannot_write(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.rules_intake import _process_batch
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        store=Store(':memory:'); self.addCleanup(store.close)
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); pipeline=Pipeline(settings,store,SimpleNamespace(),github,rules_snapshot=snapshot)
        source=message(42,text='lỗi lại',reply_to_id=10); store.enqueue(source)
        batch=f'batch:{source.chat_id}:42'; pipeline.rules_pending.create(batch,source.chat_id,42)
        plan={'source_keys':[[source.chat_id,42]],'state':'APPEND_CLOSED','intent':'bug',
              'intent_rule':'extension','rule':'lifecycle_evidence','present':{},'questions':[],
              'asked_fields':[],'targets':[],'pending_topic':None,'pending_reply_ids':[],'task':None}
        pipeline.rules_pending.save_batch(batch,snapshot.fingerprint,[plan])
        result=_process_batch(pipeline,[source],batch)
        self.assertEqual(result.reason,'lifecycle_evidence_pending')
        self.assertEqual(github.calls,[])
        self.assertEqual(store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0],'PLANNED')

    def test_recurrence_quotes_require_current_chronological_literal_sources(self):
        from tg_triage.lifecycle import grounded_recurrence
        from tests.helpers import message
        earlier=message(10,text='Hôm qua POS đã chạy tốt')
        later=message(11,text='Hôm nay POS lại lỗi')
        evidence={'kind':'recurrence','knownworking_source_ids':[10],'recurrence_source_ids':[11],
                  'knownworking_quote':'POS đã chạy tốt','recurrence_quote':'POS lại lỗi'}
        self.assertTrue(grounded_recurrence(evidence,[earlier],[later]))
        self.assertFalse(grounded_recurrence({**evidence,'knownworking_quote':'bịa đặt'},[earlier],[later]))
        self.assertFalse(grounded_recurrence({**evidence,'recurrence_source_ids':[10]},[earlier],[later]))
        self.assertFalse(grounded_recurrence(evidence,[earlier],[]))

    def direct_recurrence_fixture(self, path=':memory:'):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config, message, FakeGitHub
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        owner=self
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def __init__(self): self.bodies=[]; self.changes={}
            def _ask(self,body,parser,attempts):
                self.bodies.append(body)
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                lifecycle={'kind':'recurrence','knownworking_source_ids':[mid],'recurrence_source_ids':[mid],
                    'knownworking_quote':'worked yesterday','recurrence_quote':'failed today','temporal_order_confirmed':True}
                lifecycle.update(self.changes)
                schema=body['format']['properties']['groups']['items']['properties']['facts']['properties']['lifecycle']
                owner.assertIn(lifecycle['kind'],schema['properties']['kind']['enum'])
                owner.assertTrue(set(schema['required'])<=set(lifecycle))
                facts={'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},
                    'observed':{'value':'failed today','source_ids':[mid]},'lifecycle':lifecycle}
                payload={'groups':[{'message_ids':[mid],'facts':facts}]}
                owner.assert_wire_schema(payload,body['format'])
                return parser(json.dumps(payload))
        store=Store(path); self.addCleanup(store.close)
        old=message(10,text='old failure'); store.enqueue(old)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE message_id=10")
        base=config(); settings=replace(base,mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        gh=FakeGitHub(); gh.states={}
        gh.lifecycle_snapshot=lambda n:{'number':n,'repo':'sandbox/triage','state':gh.states.get(n,'closed'),'observed_at':'now','updated_at':'v1'}
        gh.verify_created_issue=lambda n,issue:n
        def create(issue):
            gh.calls.append(issue); n=201+len(gh.calls)
            return n,f'https://github.com/sandbox/triage/issues/{n}'
        gh.create_issue=create
        sent=[]; returned=[]; provider=Provider()
        def sender(*args):
            import re
            sent.append(args)
            match=re.search(r'Tin gốc: .*?/(\d+)(?:\s|$)',args[2])
            self.assertIsNotNone(match)
            candidate=int(match.group(1))+1
            if candidate in returned: candidate=10000+len(returned)
            returned.append(candidate)
            return candidate
        pipeline=Pipeline(settings,store,provider,gh,rules_snapshot=snapshot,notice_sender=sender)
        return store,provider,gh,sent,pipeline

    def test_direct_grounded_recurrence_and_latest_episode(self):
        from tests.helpers import message
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        text='POS worked yesterday; failed today'
        first=p.process(message(42,text=text,reply_to_id=10))
        self.assertEqual(('issue',202),(first.decision,first.issue_number))
        self.assertEqual(1,len(gh.calls)); self.assertIn('/issues/101',gh.calls[0].body)
        self.assertEqual(101,store.get(message().chat_id,10)['issue_number'])
        plan=json.loads(store.connection.execute('SELECT plan FROM rules_batch_groups').fetchone()[0])
        self.assertEqual([42],plan['lifecycle']['knownworking_source_ids'])
        self.assertIsNone(p.process(message(42,text=text,reply_to_id=10)))
        self.assertEqual('lifecycle_question_sent',p.process(message(50,text=text,reply_to_id=10)).reason)
        self.assertEqual(1,len(gh.calls))
        latest=p.process(message(60,text=text,reply_to_id=42))
        self.assertEqual('lifecycle_question_sent',latest.reason)
        latest=ops_reply(p,message(62,text='B',reply_to_id=61))
        self.assertEqual(('issue',203),(latest.decision,latest.issue_number))
        self.assertIn('/issues/202',gh.calls[1].body)
        self.assertEqual(2,len(gh.calls))
        self.assertEqual('lifecycle_answer_unresolved',ops_reply(p,message(52,text='B',reply_to_id=51)).reason)
        self.assertEqual(2,len(gh.calls))

    def test_direct_recurrence_invalid_proof_and_open_append(self):
        from tests.helpers import message
        for changes in ({'knownworking_quote':'invented'}, {'temporal_order_confirmed':False},
                        {'knownworking_source_ids':[999]}, {'kind':'unclear'}):
            with self.subTest(changes=changes):
                store,provider,gh,sent,p=self.direct_recurrence_fixture()
                provider.changes=changes
                self.assertEqual('pending_review',p.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10)).decision)
                self.assertEqual([],gh.calls)
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        gh.states[101]='open'; comments=[]; gh.add_comment=lambda *a:comments.append(a) or 301
        self.assertEqual('followup',p.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10)).decision)
        self.assertEqual([],gh.calls); self.assertEqual(1,len(comments))

    def test_direct_recurrence_uncertain_post_blocks_restart_without_replay(self):
        from tg_triage.rules_pending_store import RulesHalt, check_before_recovery
        from tests.helpers import message
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path=str(Path(temp.name)/'state.sqlite')
        store,provider,gh,sent,p=self.direct_recurrence_fixture(path)
        def uncertain(issue):
            gh.calls.append(issue); raise OSError('unknown POST')
        gh.create_issue=uncertain
        with self.assertRaises(RulesHalt): p.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10))
        self.assertEqual('reserved',store.connection.execute('SELECT status FROM rules_related_episodes').fetchone()[0])
        with self.assertRaises(RulesHalt): check_before_recovery(Path(path))
        self.assertEqual(1,len(gh.calls)); self.assertEqual([],sent)

    def test_direct_completed_checkpoint_recovers_without_replay(self):
        from tg_triage.rules_pending_store import _recoverable_batch_groups
        from tests.helpers import message
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        p.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10))
        with store.connection: store.connection.execute("UPDATE rules_batch_groups SET status='RESERVED'")
        self.assertEqual('DONE',_recoverable_batch_groups(store.connection)[0][2])
        self.assertEqual(1,len(gh.calls)); self.assertEqual(1,len(sent))

    def test_direct_checkpoint_before_post_and_after_notice(self):
        from unittest.mock import patch
        from tg_triage.rules_pending_store import _recoverable_batch_groups
        from tests.helpers import message
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        with patch('tg_triage.rules_intake._direct_related_episode',side_effect=RuntimeError('crash before reservation')):
            with self.assertRaises(RuntimeError): p.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10))
        self.assertEqual('PLANNED',_recoverable_batch_groups(store.connection)[0][2])
        self.assertEqual([],gh.calls)
        # A real notice can have completed just before the batch checkpoint.
        from tg_triage.rules_pending_store import RulesPendingStore
        p.rules_pending=RulesPendingStore(store)
        original=p.rules_pending.transition_group
        def crash(batch,ordinal,old,new,**kw):
            if old=='RESERVED' and new=='DONE': raise RuntimeError('crash after notice')
            return original(batch,ordinal,old,new,**kw)
        with patch.object(p.rules_pending,'transition_group',side_effect=crash):
            from tg_triage.rules_intake import sweep
            with self.assertRaises(RuntimeError): sweep(p)
        self.assertEqual('DONE',_recoverable_batch_groups(store.connection)[0][2])
        self.assertEqual(1,len(gh.calls)); self.assertEqual(1,len(sent))

    def test_direct_verified_create_without_notice_blocks_recovery(self):
        from tg_triage.rules_pending_store import _recoverable_batch_groups, RulesHalt
        from tests.helpers import message
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        p.notice_sender=lambda *a:None
        with self.assertRaises(RulesHalt): p.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10))
        self.assertEqual('verified',store.connection.execute('SELECT status FROM rules_related_episodes').fetchone()[0])
        with self.assertRaises(RulesHalt): _recoverable_batch_groups(store.connection)
        self.assertEqual(1,len(gh.calls))

    def test_direct_resume_rechecks_parent_and_source_proof(self):
        from unittest.mock import patch
        from tg_triage.rules_intake import sweep
        from tests.helpers import message
        for change in ('open','quote','source'):
            with self.subTest(change=change):
                store,provider,gh,sent,p=self.direct_recurrence_fixture()
                with patch('tg_triage.rules_intake._direct_related_episode',side_effect=RuntimeError('crash')):
                    with self.assertRaises(RuntimeError): p.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10))
                from tg_triage.rules_pending_store import RulesPendingStore
                p.rules_pending=RulesPendingStore(store)
                with store.connection:
                    if change=='source': store.connection.execute("UPDATE messages SET text=text || ' altered' WHERE message_id=42")
                    if change=='quote':
                        raw=store.connection.execute('SELECT plan FROM rules_batch_groups').fetchone()[0]
                        plan=json.loads(raw); plan['lifecycle']['knownworking_quote']='invented'
                        store.connection.execute('UPDATE rules_batch_groups SET plan=?',(json.dumps(plan),))
                if change=='open': gh.states[101]='open'
                sweep(p)
                self.assertEqual([],gh.calls); self.assertEqual([],sent)

    def test_direct_old_success_on_latest_episode_asks_without_create(self):
        from tests.helpers import message
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        p.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10))
        # The source that began episode 202 is not a fresh success after it.
        provider.changes={'knownworking_source_ids':[42]}
        result=p.process(message(60,text='POS failed today',reply_to_id=42))
        self.assertEqual('lifecycle_question_sent',result.reason)
        self.assertEqual(1,len(gh.calls))

    def assert_wire_schema(self,value,schema):
        """Validate fixture output against the request's real emitted schema."""
        kind=schema.get('type')
        types={'object':dict,'array':list,'string':str,'integer':int,'boolean':bool,'number':(int,float)}
        if kind in types: self.assertIsInstance(value,types[kind])
        if kind=='integer': self.assertIs(type(value),int)
        if 'enum' in schema: self.assertIn(value,schema['enum'])
        if isinstance(value,dict):
            self.assertTrue(set(schema.get('required',()))<=set(value))
            props=schema.get('properties',{})
            if schema.get('additionalProperties') is False: self.assertFalse(set(value)-set(props))
            for key,item in value.items():
                if key in props: self.assert_wire_schema(item,props[key])
        if isinstance(value,list):
            self.assertGreaterEqual(len(value),schema.get('minItems',0))
            self.assertLessEqual(len(value),schema.get('maxItems',len(value)))
            if schema.get('uniqueItems'): self.assertEqual(len(value),len(set(value)))
            for item in value: self.assert_wire_schema(item,schema.get('items',{}))
        if isinstance(value,str):
            self.assertGreaterEqual(len(value),schema.get('minLength',0))
            self.assertLessEqual(len(value),schema.get('maxLength',len(value)))
        if type(value) in (int,float):
            if 'minimum' in schema: self.assertGreaterEqual(value,schema['minimum'])
            if 'maximum' in schema: self.assertLessEqual(value,schema['maximum'])

    def seed_legacy_episode(self,store):
        from tests.helpers import message, NOW
        old=message(42,text='POS worked yesterday; failed today'); store.enqueue(old)
        with store.connection:
            store.connection.execute('UPDATE messages SET decision=?,issue_number=?,issue_url=? WHERE message_id=?',('followup',202,'https://github.com/sandbox/triage/issues/202',42))
            store.connection.execute('INSERT INTO followup_roots(root_key,chat_id,source_message_id,case_key,issue_number) VALUES(?,?,?,?,?)',('root-parent',old.chat_id,10,'case-parent',101))
            store.connection.execute('INSERT INTO followup_roots(root_key,chat_id,source_message_id,case_key,issue_number) VALUES(?,?,?,?,?)',('episode-202',old.chat_id,42,'case-parent',202))
            store.connection.execute('INSERT INTO followup_jobs(id,chat_id,message_id,root_key,status) VALUES(?,?,?,?,?)',(1,old.chat_id,42,'root-parent','done'))
            store.connection.execute('INSERT INTO followup_episodes VALUES(?,?,?,?,?,?,?,?,?)',('episode-202','root-parent',1,old.chat_id,42,202,'sandbox/triage','[42]',NOW.isoformat()))
            store.connection.execute('INSERT INTO followup_episode_sources VALUES(?,?,?)',(old.chat_id,42,'episode-202'))

    def test_success_report_arrival_does_not_prove_new_event_after_episode(self):
        from tests.helpers import message
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        self.assertEqual(202,p.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10)).issue_number)
        result=p.process(message(60,text='POS still failed today; it only worked yesterday before the last episode',reply_to_id=42))
        self.assertEqual('lifecycle_question_sent',result.reason)
        self.assertEqual(1,len(gh.calls))

    def test_higher_id_backdated_report_cannot_create_successor(self):
        from datetime import timedelta
        from tests.helpers import message,NOW
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        self.assertEqual(202,p.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10)).issue_number)
        result=p.process(message(60,text='POS worked yesterday; failed today',reply_to_id=42,received_at=NOW-timedelta(days=1)))
        self.assertEqual('pending_review',result.decision); self.assertEqual(1,len(gh.calls))

    def test_legacy_episode_requires_fresh_bound_confirmation(self):
        from tests.helpers import message
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        self.seed_legacy_episode(store)
        provider.changes={'knownworking_source_ids':[42]}
        result=p.process(message(60,text='POS failed today',reply_to_id=42))
        self.assertEqual('lifecycle_question_sent',result.reason); self.assertEqual([],gh.calls)
        # Exact B is a new direct assertion about success AFTER bound episode202.
        result=ops_reply(p,message(62,text='B',reply_to_id=61))
        self.assertEqual('issue',result.decision); self.assertEqual(1,len(gh.calls))
        self.assertIn('/issues/202',gh.calls[0].body)

    def test_backdated_confirmation_and_changed_latest_parent_cannot_create(self):
        from datetime import timedelta
        from tests.helpers import message,NOW
        for change in ('backdated','new_successor'):
            with self.subTest(change=change):
                store,provider,gh,sent,p=self.direct_recurrence_fixture()
                self.assertEqual(202,p.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10)).issue_number)
                self.assertEqual('lifecycle_question_sent',p.process(message(60,text='POS worked yesterday; failed today',reply_to_id=42)).reason)
                if change=='new_successor':
                    with store.connection:
                        store.connection.execute("INSERT INTO rules_related_episodes VALUES(?,?,?,?,?,'done',?,?,?)",(message().chat_id,'sandbox/triage',202,70,70,203,'https://github.com/sandbox/triage/issues/203','notice'))
                result=ops_reply(p,message(62,text='B',reply_to_id=61,received_at=NOW-timedelta(days=1) if change=='backdated' else NOW))
                self.assertEqual('pending_review',result.decision); self.assertEqual(1,len(gh.calls))

    def test_direct_recurrence_with_separate_reply_success_evidence(self):
        from tests.helpers import message
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        with store.connection: store.connection.execute("UPDATE messages SET text='POS worked yesterday' WHERE message_id=10")
        provider.changes={'knownworking_source_ids':[10]}
        result=p.process(message(42,text='POS failed today',reply_to_id=10))
        self.assertEqual(('issue',202),(result.decision,result.issue_number))
        plan=json.loads(store.connection.execute('SELECT plan FROM rules_batch_groups').fetchone()[0])
        self.assertEqual([10],plan['lifecycle']['knownworking_source_ids'])
        self.assertTrue(plan['lifecycle_context'])
        self.assertEqual(101,store.get(message().chat_id,10)['issue_number'])

    def test_resume_revalidates_legacy_episode_time_boundary(self):
        from unittest.mock import patch
        from datetime import timedelta
        from tg_triage.rules_pending_store import RulesPendingStore
        from tg_triage.rules_intake import sweep
        from tests.helpers import message,NOW
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        self.seed_legacy_episode(store)
        provider.changes={'knownworking_source_ids':[42]}
        with patch('tg_triage.rules_intake._direct_related_episode',side_effect=RuntimeError('crash before boundary check')):
            with self.assertRaises(RuntimeError): p.process(message(60,text='POS failed today',reply_to_id=42))
        p.rules_pending=RulesPendingStore(store)
        with store.connection:
            store.connection.execute('UPDATE followup_episodes SET created_at=?',((NOW+timedelta(days=1)).isoformat(),))
        sweep(p)
        self.assertEqual([],gh.calls); self.assertEqual([],sent)
        self.assertEqual('lifecycle_chronology_unavailable',store.get(message().chat_id,60)['reason'])

    def recurrence_batch_fixture(self, *, path=':memory:', documents=False):
        from tests.helpers import message,FakeDownloader,FakeUploader
        import base64
        store,_,gh,sent,p=self.direct_recurrence_fixture(path)
        owner=self
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=True
            def __init__(self): self.bodies=[]
            def _ask(self,body,parser,attempts):
                self.bodies.append(body)
                ids=json.loads(body['messages'][1]['content'])['source_ids']
                working,recurring=ids[:2]
                lifecycle={'kind':'recurrence','knownworking_source_ids':[working],'recurrence_source_ids':[recurring],
                    'knownworking_quote':'worked yesterday','recurrence_quote':'failed today'}
                facts={'intent':{'value':'bug','source_ids':[recurring]},'object':{'value':'POS','source_ids':[recurring]},
                    'observed':{'value':'failed today','source_ids':[recurring]},'lifecycle':lifecycle}
                payload={'groups':[{'message_ids':ids,'facts':facts}]}
                owner.assert_wire_schema(payload,body['format'])
                return parser(json.dumps(payload))
        png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aYZkAAAAASUVORK5CYII=')
        files={42:(png,'old-success.png'),44:(png,'new-failure.png')}
        members=[message(42,text='POS worked yesterday',reply_to_id=10,has_image=True,image_mime='image/png',image_size=len(png)),
            message(43,text='POS failed today',reply_to_id=42),
            message(44,text='',reply_to_id=43,has_image=True,image_mime='image/png',image_size=len(png))]
        if documents:
            files[45]=(b'POS failure evidence','failure.txt')
            members.append(message(45,text='Failure document',reply_to_id=43,has_document=True,document_name='failure.txt',document_mime='text/plain',document_size=20))
        provider=Provider(); downloader=FakeDownloader(files); uploader=FakeUploader()
        p.classifier=provider; p.attachments.downloader=downloader; p.attachments.uploader=uploader
        p.notice_sender=lambda *a:sent.append(a) or (901 if len(sent)==1 else 10000+10*len(sent))
        return store,provider,gh,sent,p,members,downloader,uploader,png

    def run_recurrence_batch(self,p,members):
        from tg_triage.rules_intake import _process_batch
        for member in members: p.store.start_classifying(member)
        return _process_batch(p,members)

    def test_direct_batch_preserves_recurrence_image_document_and_historical_ownership(self):
        from tests.helpers import message
        from tg_triage.rules_pending_store import _recoverable_batch_groups
        import base64
        store,provider,gh,sent,p,members,downloader,uploader,png=self.recurrence_batch_fixture(documents=True)
        result=self.run_recurrence_batch(p,members)
        self.assertEqual(('issue',202),(result.decision,result.issue_number))
        self.assertEqual(1,len(gh.calls)); self.assertEqual(2,len(uploader.uploads))
        self.assertEqual([42,44],json.loads(provider.bodies[0]['messages'][1]['content'])['images_order'])
        self.assertEqual([png,png],[base64.b64decode(value) for value in provider.bodies[0]['messages'][1]['images']])
        self.assertTrue(all('-42-' not in row['asset'] for row in uploader.uploads))
        body=gh.calls[0].body
        self.assertNotIn('worked yesterday',body); self.assertIn('failed today',body)
        self.assertIn('/43',body); self.assertIn('/44',body); self.assertIn('failure.txt',body)
        self.assertEqual(101,store.get(message().chat_id,42)['issue_number'])
        for mid in (43,44,45): self.assertEqual(202,store.get(message().chat_id,mid)['issue_number'])
        self.assertEqual((-1009876543210,None),sent[0][:2])
        with store.connection: store.connection.execute("UPDATE rules_batch_groups SET status='RESERVED'")
        self.assertEqual('DONE',_recoverable_batch_groups(store.connection)[0][2])
        self.assertEqual(1,len(gh.calls)); self.assertEqual(2,len(uploader.uploads))

    def test_direct_batch_unknown_upload_release_or_post_never_replays(self):
        from tg_triage.rules_pending_store import RulesHalt,check_before_recovery
        from tests.helpers import FakeUploader
        for failure in ('release','upload','post'):
            with self.subTest(failure=failure):
                temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
                path=str(Path(temp.name)/'state.sqlite')
                store,provider,gh,sent,p,members,downloader,uploader,png=self.recurrence_batch_fixture(path=path,documents=True)
                if failure=='release': uploader.release_error=OSError('unknown release')
                if failure=='upload': uploader.error=OSError('unknown upload')
                if failure=='post':
                    def unknown(issue): gh.calls.append(issue); raise OSError('unknown POST')
                    gh.create_issue=unknown
                with self.assertRaises(RulesHalt): self.run_recurrence_batch(p,members)
                self.assertEqual('reserved',store.connection.execute('SELECT status FROM rules_related_episodes').fetchone()[0])
                self.assertEqual(1,store.count_recent_issues())
                with self.assertRaises(RulesHalt): check_before_recovery(Path(path))
                self.assertEqual(1 if failure=='post' else 0,len(gh.calls))
                self.assertEqual(1,len(uploader.releases))

    def test_batch_resume_rejects_changed_document_or_pixel_before_upload(self):
        from unittest.mock import patch
        from tg_triage.rules_pending_store import RulesPendingStore
        from tg_triage.rules_intake import sweep
        for changed in ('document','pixel'):
            with self.subTest(changed=changed):
                store,provider,gh,sent,p,members,downloader,uploader,png=self.recurrence_batch_fixture(documents=True)
                with patch('tg_triage.rules_intake._direct_related_episode',side_effect=RuntimeError('before related reservation')):
                    with self.assertRaises(RuntimeError): self.run_recurrence_batch(p,members)
                p.rules_pending=RulesPendingStore(store)
                if changed=='document': downloader.files[45]=(b'changed document','failure.txt')
                else: downloader.files[44]=(png+b'changed','new-failure.png')
                sweep(p)
                self.assertEqual([],gh.calls); self.assertEqual([],uploader.uploads)

    def test_successor_batch_B_preserves_all_recurrence_assets(self):
        from tests.helpers import message
        store,provider,gh,sent,p,members,downloader,uploader,png=self.recurrence_batch_fixture(documents=True)
        self.assertEqual(202,self.run_recurrence_batch(p,members).issue_number)
        later=[replace(m,message_id=m.message_id+18,reply_to_id=(43 if m.message_id==42 else m.reply_to_id+18)) for m in members]
        downloader.files.update({mid+18:value for mid,value in list(downloader.files.items())})
        question=self.run_recurrence_batch(p,later)
        self.assertEqual('lifecycle_question_sent',question.reason); self.assertEqual(1,len(gh.calls))
        notice=store.connection.execute("SELECT notice_id FROM rules_notices WHERE kind='question' AND status='sent'").fetchone()[0]
        result=ops_reply(p,message(notice+1,text='B',reply_to_id=notice))
        self.assertEqual(('issue',203),(result.decision,result.issue_number))
        self.assertEqual(4,len(uploader.uploads))
        self.assertEqual(202,store.get(message().chat_id,60)['issue_number'])
        for mid in (61,62,63): self.assertEqual(203,store.get(message().chat_id,mid)['issue_number'])
        self.assertIn('/issues/202',gh.calls[1].body); self.assertNotIn('worked yesterday',gh.calls[1].body)

    def test_recurrence_chronology_can_be_grounded_in_read_document_text(self):
        store,provider,gh,sent,p,members,downloader,uploader,png=self.recurrence_batch_fixture(documents=True)
        downloader.files[42]=(b'POS worked yesterday','success.txt')
        downloader.files[43]=(b'POS failed today','failure-report.txt')
        members[0]=replace(members[0],text='Historical confirmation',has_image=False,has_document=True,
            document_name='success.txt',document_mime='text/plain',document_size=20)
        members[1]=replace(members[1],text='Current report',has_document=True,
            document_name='failure-report.txt',document_mime='text/plain',document_size=16)
        result=self.run_recurrence_batch(p,members)
        self.assertEqual(202,result.issue_number)
        self.assertEqual(3,len(uploader.uploads))
        self.assertTrue(all('-42-' not in row['asset'] for row in uploader.uploads))
        self.assertIn('failure-report.txt',gh.calls[0].body)

    def test_old_parent_question_binds_latest_and_recovers_before_B(self):
        from unittest.mock import patch
        from tests.helpers import message
        from tg_triage.rules_pending_store import RulesPendingStore,_recoverable_batch_groups
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        text='POS worked yesterday; failed today'
        self.assertEqual(202,p.process(message(42,text=text,reply_to_id=10)).issue_number)
        original=p.rules_pending.transition_group
        def crash(batch,ordinal,old,new,**kw):
            if old=='RESERVED' and new=='DONE': raise RuntimeError('question sent before checkpoint')
            return original(batch,ordinal,old,new,**kw)
        with patch.object(p.rules_pending,'transition_group',side_effect=crash):
            with self.assertRaises(RuntimeError): p.process(message(50,text=text,reply_to_id=10))
        q=store.connection.execute('SELECT parent_number FROM rules_lifecycle_questions WHERE source_id=50').fetchone()
        self.assertEqual(202,q[0]); self.assertEqual('DONE',_recoverable_batch_groups(store.connection)[0][2])
        p.rules_pending=RulesPendingStore(store)
        result=ops_reply(p,message(52,text='B',reply_to_id=51))
        self.assertEqual(203,result.issue_number); self.assertIn('/issues/202',gh.calls[1].body)

    def test_image_only_recurrence_requires_explicit_bound_confirmation(self):
        from tests.helpers import message,FakeDownloader,FakeUploader,PNG
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        provider.supports_vision=True
        provider.changes={'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}
        p.attachments.downloader=FakeDownloader({42:(PNG,'failure.png')}); uploader=FakeUploader(); p.attachments.uploader=uploader
        result=p.process(message(42,text='',reply_to_id=10,has_image=True,image_mime='image/png',image_size=len(PNG)))
        self.assertEqual('lifecycle_question_sent',result.reason); self.assertEqual([],gh.calls)
        result=ops_reply(p,message(44,text='B',reply_to_id=43))
        self.assertEqual(202,result.issue_number); self.assertEqual(1,len(uploader.uploads)); self.assertIn('/42-',gh.calls[0].body)

    def test_image_only_recurrence_accepts_ops_b_with_same_source_id(self):
        from tests.helpers import message,FakeDownloader,FakeUploader,PNG
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        provider.supports_vision=True
        provider.changes={'kind':'unclear','knownworking_source_ids':[],'recurrence_source_ids':[]}
        p.attachments.downloader=FakeDownloader({42:(PNG,'failure.png')}); uploader=FakeUploader(); p.attachments.uploader=uploader
        p.notice_sender=lambda *args:sent.append(args) or (30 if len(sent)==1 else 100)
        self.assertEqual('lifecycle_question_sent',p.process(message(42,text='',reply_to_id=10,has_image=True,image_mime='image/png',image_size=len(PNG))).reason)
        self.assertEqual([],gh.calls); self.assertEqual([],uploader.uploads)
        result=ops_reply(p,message(42,text='B',reply_to_id=30))
        self.assertEqual(202,result.issue_number); self.assertEqual(1,len(gh.calls)); self.assertEqual(1,len(uploader.uploads))
        self.assertIn('/42-',gh.calls[0].body)

    def test_forwarded_recurrence_and_backdated_batch_asset_do_not_write(self):
        from tests.helpers import message,NOW
        from datetime import timedelta
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        self.assertEqual('pending_review',p.process(message(42,text='POS worked yesterday; failed today',reply_to_id=10,is_forward=True)).decision)
        self.assertEqual([],gh.calls)
        store,provider,gh,sent,p,members,downloader,uploader,png=self.recurrence_batch_fixture()
        members[-1]=replace(members[-1],received_at=NOW-timedelta(days=1))
        self.assertEqual('pending_review',self.run_recurrence_batch(p,members).decision)
        self.assertEqual([],gh.calls); self.assertEqual([],uploader.uploads)

    def test_related_quota_counts_one_attempt_and_corrupt_receipt_cannot_relax_limit(self):
        store,provider,gh,sent,p,members,downloader,uploader,png=self.recurrence_batch_fixture(documents=True)
        self.assertEqual(202,self.run_recurrence_batch(p,members).issue_number)
        self.assertEqual(1,store.count_recent_issues())
        raw=store.connection.execute('SELECT recurrence_keys,source_keys,plan_hash FROM rules_related_receipts').fetchone()
        variants=[('recurrence_keys','[]'),('recurrence_keys',json.dumps([[members[0].chat_id,43],[members[0].chat_id,44]])),
            ('source_keys','[]'),('plan_hash','tampered')]
        for column,value in variants:
            with self.subTest(column=column,value=value):
                with store.connection: store.connection.execute('UPDATE rules_related_receipts SET '+column+'=?',(value,))
                self.assertEqual(3,store.count_recent_issues())
                with store.connection: store.connection.execute('UPDATE rules_related_receipts SET recurrence_keys=?,source_keys=?,plan_hash=?',tuple(raw))
        with store.connection: store.connection.execute('UPDATE messages SET issue_number=999 WHERE message_id=44')
        self.assertEqual(3,store.count_recent_issues())
        with store.connection: store.connection.execute('DROP TABLE rules_related_receipts')
        self.assertEqual(3,store.count_recent_issues())

    def unclear_question_fixture(self,*,path=':memory:',roles='proven'):
        values=self.recurrence_batch_fixture(path=path,documents=True)
        store,provider,gh,sent,p,members,downloader,uploader,png=values
        ask=provider._ask
        def unclear(body,parser,attempts):
            def convert(raw):
                data=json.loads(raw); lifecycle=data['groups'][0]['facts']['lifecycle']; lifecycle['kind']='unclear'
                if roles=='absent': lifecycle.update(knownworking_source_ids=[],recurrence_source_ids=[])
                if roles=='ungrounded': lifecycle['knownworking_quote']='invented success'
                self.assert_wire_schema(data,body['format'])
                return parser(json.dumps(data))
            return ask(body,convert,attempts)
        provider._ask=unclear
        self.assertEqual('lifecycle_question_sent',self.run_recurrence_batch(p,members).reason)
        return values

    def restart_question_fixture(self,path,p,provider,gh,downloader,uploader):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        p.store.close(); reopened=Store(path); self.addCleanup(reopened.close)
        return Pipeline(p.config,reopened,provider,gh,rules_snapshot=p.rules_snapshot,
            notice_sender=p.notice_sender,downloader=downloader,uploader=uploader)

    def test_unclear_B_preserves_proven_historical_roles_through_restart(self):
        from tests.helpers import message
        for restart in (False,True):
            with self.subTest(restart=restart):
                temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup); path=str(Path(temp.name)/'state.sqlite')
                store,provider,gh,sent,p,members,downloader,uploader,png=self.unclear_question_fixture(path=path)
                if restart:
                    p=self.restart_question_fixture(path,p,provider,gh,downloader,uploader);store=p.store
                result=ops_reply(p,message(902,text='B',reply_to_id=901))
                self.assertEqual(202,result.issue_number)
                self.assertEqual(101,store.get(members[0].chat_id,42)['issue_number'])
                for mid in (43,44,45):self.assertEqual(202,store.get(members[0].chat_id,mid)['issue_number'])
                self.assertEqual(2,len(uploader.uploads))
                self.assertTrue(all('-42-' not in row['asset'] and not row['asset'].startswith('42-') for row in uploader.uploads))
                self.assertNotIn('worked yesterday',gh.calls[0].body)

    def test_unclear_B_cannot_assign_absent_or_ungrounded_group_roles(self):
        from tests.helpers import message
        for roles in ('absent','ungrounded'):
            with self.subTest(roles=roles):
                store,provider,gh,sent,p,members,downloader,uploader,png=self.unclear_question_fixture(roles=roles)
                self.assertEqual('pending_review',ops_reply(p,message(902,text='B',reply_to_id=901)).decision)
                self.assertEqual([],gh.calls); self.assertEqual([],uploader.uploads)
                self.assertEqual(0,store.connection.execute('SELECT count(*) FROM rules_lifecycle_answers').fetchone()[0])

    def test_both_answers_revalidate_source_pixel_document_and_context_before_reservation(self):
        from tests.helpers import message
        for answer in ('A','B'):
            for changed in ('source','pixel','document','context'):
                with self.subTest(answer=answer,changed=changed):
                    temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);path=str(Path(temp.name)/'state.sqlite')
                    store,provider,gh,sent,p,members,downloader,uploader,png=self.unclear_question_fixture(path=path)
                    p=self.restart_question_fixture(path,p,provider,gh,downloader,uploader);store=p.store
                    if changed=='document':downloader.files[45]=(b'ALTERED PRIVATE DATA','failure.txt')
                    if changed=='pixel':downloader.files[44]=(png+b'changed','new-failure.png')
                    if changed in ('source','context'):
                        with store.connection:store.connection.execute("UPDATE messages SET text=text||' altered' WHERE message_id=?",(43 if changed=='source' else 10,))
                    comments=[];gh.add_comment=lambda *args:comments.append(args) or 301
                    self.assertEqual('pending_review',ops_reply(p,message(902,text=answer,reply_to_id=901)).decision)
                    self.assertEqual([],gh.calls);self.assertEqual([],comments);self.assertEqual([],uploader.uploads)
                    self.assertEqual(0,store.connection.execute('SELECT count(*) FROM rules_lifecycle_answers').fetchone()[0])

    def test_A_on_retargeted_question_appends_latest_and_survives_restart(self):
        from tests.helpers import message
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);path=str(Path(temp.name)/'state.sqlite')
        store,provider,gh,sent,p=self.direct_recurrence_fixture(path)
        text='POS worked yesterday; failed today'
        self.assertEqual(202,p.process(message(42,text=text,reply_to_id=10)).issue_number)
        self.assertEqual('lifecycle_question_sent',p.process(message(50,text=text,reply_to_id=10)).reason)
        p=self.restart_question_fixture(path,p,provider,gh,p.attachments.downloader,p.attachments.uploader);store=p.store
        comments=[];gh.add_comment=lambda *args:comments.append(args) or 301
        result=ops_reply(p,message(52,text='A',reply_to_id=51))
        self.assertEqual('followup',result.decision);self.assertEqual(202,result.issue_number)
        self.assertEqual([202],[number for number,body in comments])
        self.assertEqual(101,store.get(message().chat_id,10)['issue_number'])
        self.assertEqual('done',store.connection.execute('SELECT status FROM rules_lifecycle_answers').fetchone()[0])

    def test_unseen_image_and_unread_document_are_bound_without_claiming_viewed(self):
        from tests.helpers import message
        for changed in (None,'image','document'):
            for answer in ('A','B'):
                with self.subTest(changed=changed,answer=answer):
                    store,provider,gh,sent,p,members,downloader,uploader,png=self.recurrence_batch_fixture(documents=True)
                    provider.supports_vision=False
                    pdf=b'%PDF-1.4\noriginal evidence\n%%EOF';downloader.files[45]=(pdf,'evidence.pdf')
                    members[-1]=replace(members[-1],document_name='evidence.pdf',document_mime='application/pdf',document_size=len(pdf))
                    ask=provider._ask
                    def unclear(body,parser,attempts):
                        def convert(raw):
                            data=json.loads(raw);data['groups'][0]['facts']['lifecycle']['kind']='unclear'
                            self.assert_wire_schema(data,body['format']);return parser(json.dumps(data))
                        return ask(body,convert,attempts)
                    provider._ask=unclear
                    self.assertEqual('lifecycle_question_sent',self.run_recurrence_batch(p,members).reason)
                    self.assertNotIn('images',provider.bodies[0]['messages'][1])
                    plan=json.loads(store.connection.execute('SELECT plan FROM rules_batch_groups').fetchone()[0])
                    self.assertEqual({},plan['lifecycle_pixels'])
                    self.assertIsNotNone(plan['lifecycle_assets']['image:44'])
                    self.assertIsNotNone(plan['lifecycle_assets']['document:45'])
                    self.assertNotEqual('read',plan['document_reads'][0]['read_status'])
                    if changed=='image':downloader.files[44]=(png+b'altered','new-failure.png')
                    if changed=='document':downloader.files[45]=(b'%PDF-1.4\nALTERED\n%%EOF','evidence.pdf')
                    comments=[];gh.add_comment=lambda *args:comments.append(args) or 301
                    result=ops_reply(p,message(902,text=answer,reply_to_id=901))
                    if changed:
                        self.assertEqual('pending_review',result.decision);self.assertEqual([],comments);self.assertEqual([],gh.calls);self.assertEqual([],uploader.uploads)
                    else:
                        self.assertEqual('followup' if answer=='A' else 'issue',result.decision)
                        self.assertEqual(3 if answer=='A' else 2,len(uploader.uploads))

    def test_lifecycle_asset_proof_refuses_missing_tampered_or_unavailable_bytes(self):
        from tests.helpers import FakeDownloader,FakeUploader,message,PNG
        from tg_triage.rules_pending_store import RulesHalt
        for mode in ('missing','tampered','unavailable'):
            with self.subTest(mode=mode):
                store,provider,gh,sent,p=self.direct_recurrence_fixture()
                downloader=FakeDownloader({42:(PNG,'image.png')});uploader=FakeUploader()
                p.attachments.downloader=downloader;p.attachments.uploader=uploader
                member=message(42,has_image=True,image_mime='image/png',image_size=len(PNG))
                proof=p.attachments.capture_proof('test',[member])
                if mode=='missing':proof={}
                if mode=='tampered':proof['image:42']['sha256']='altered'
                if mode=='unavailable':downloader.files={}
                with self.assertRaises(RulesHalt):p._images('test',[member],live=True,asset_proofs=proof)
                self.assertEqual([],uploader.uploads);self.assertEqual([],uploader.releases)

    def test_lifecycle_attachment_metadata_caps_missing_and_unsupported_hold_before_question(self):
        for mode in ('oversize','disabled','missing','unsupported'):
            with self.subTest(mode=mode):
                store,provider,gh,sent,p,members,downloader,uploader,png=self.recurrence_batch_fixture(documents=True)
                if mode=='oversize':members[2]=replace(members[2],image_size=p.attachments.settings.max_bytes+1)
                if mode=='disabled':p.attachments.settings=replace(p.attachments.settings,max_per_case=0)
                if mode=='missing':downloader.files.pop(44)
                if mode=='unsupported':members[-1]=replace(members[-1],document_name='evidence.exe',document_mime='application/x-msdownload')
                result=self.run_recurrence_batch(p,members)
                self.assertEqual('pending_review',result.decision);self.assertEqual([],gh.calls);self.assertEqual([],uploader.uploads)
                self.assertEqual(0,store.connection.execute('SELECT count(*) FROM rules_lifecycle_questions').fetchone()[0])
                if mode=='oversize':self.assertNotIn((members[2].chat_id,44),downloader.calls)

    def test_asset_proof_checks_all_files_before_first_upload_for_A_and_B(self):
        from tests.helpers import message
        from tg_triage.rules_pending_store import RulesHalt
        for answer in ('A','B'):
            with self.subTest(answer=answer):
                store,provider,gh,sent,p,members,downloader,uploader,png=self.unclear_question_fixture()
                original=p.attachments._check_asset_proofs
                def change_later(images,documents,proof,required):
                    documents[0][1].write_bytes(b'changed after download before any upload')
                    return original(images,documents,proof,required)
                p.attachments._check_asset_proofs=change_later
                comments=[];gh.add_comment=lambda *args:comments.append(args) or 301
                with self.assertRaises(RulesHalt):ops_reply(p,message(902,text=answer,reply_to_id=901))
                self.assertEqual([],uploader.uploads);self.assertEqual([],uploader.releases);self.assertEqual([],comments);self.assertEqual([],gh.calls)

    def test_prepared_cache_and_uploaded_row_cannot_bypass_asset_proof(self):
        from tests.helpers import message,PNG,FakeDownloader,FakeUploader
        from tg_triage.rules_pending_store import RulesHalt
        for cached in ('prepared','uploaded'):
            with self.subTest(cached=cached):
                store,provider,gh,sent,p=self.direct_recurrence_fixture()
                p.attachments.downloader=FakeDownloader({42:(PNG,'image.png')});uploader=FakeUploader();p.attachments.uploader=uploader
                member=message(42,has_image=True,image_mime='image/png',image_size=len(PNG))
                prepared=p.attachments.prepare_images('test',[member]);self.addCleanup(prepared.close)
                proof=p.attachments.capture_proof('test',[member],prepared=prepared)
                if cached=='prepared':
                    prepared.accepted[0][1].write_bytes(PNG+b'changed');p._prepared_images=prepared
                else:
                    store.connection.commit()
                    old=store.reserve_attachment_call('test',42,'42-1.png',1000,chat_id=member.chat_id);store.finish_attachment(old,'uploaded')
                    p.attachments.downloader.files[42]=(PNG+b'changed','image.png')
                with self.assertRaises(RulesHalt):p._images('test',[member],live=True,asset_proofs=proof)
                self.assertEqual([],uploader.uploads);self.assertEqual([],uploader.releases)
                if cached=='uploaded':self.assertEqual('uploaded',store.connection.execute('SELECT status FROM attachments').fetchone()[0])

    def test_bound_assets_preserve_configured_positive_upload_cap_and_legacy_no_proof(self):
        from tests.helpers import message,PNG,FakeDownloader,FakeUploader
        store,provider,gh,sent,p=self.direct_recurrence_fixture()
        p.attachments.downloader=FakeDownloader({mid:(PNG,f'{mid}.png') for mid in (42,43)})
        uploader=FakeUploader();p.attachments.uploader=uploader;p.attachments.settings=replace(p.attachments.settings,max_per_case=1)
        members=[message(mid,has_image=True,image_mime='image/png',image_size=len(PNG)) for mid in (42,43)]
        proof=p.attachments.capture_proof('proof',members)
        store.connection.commit()
        result=p._images('proof',members,live=True,asset_proofs=proof)
        self.assertEqual(1,len(result.images));self.assertEqual(1,len(uploader.uploads));self.assertTrue(result.notes)
        # Existing callers provide no proof and retain their existing upload behavior.
        result=p._images('legacy',[members[0]],live=True)
        self.assertEqual(1,len(result.images));self.assertEqual(2,len(uploader.uploads))

    def test_uncited_roles_require_actual_reply_and_historical_reply_asset_stays_parent(self):
        from tests.helpers import message
        for binding in ('historical','unlinked'):
            with self.subTest(binding=binding):
                store,provider,gh,sent,p,members,downloader,uploader,png=self.recurrence_batch_fixture(documents=True)
                members.append(message(46,text='Old confirmation image',reply_to_id=42 if binding=='historical' else None,
                    has_image=True,image_mime='image/png',image_size=len(png)))
                downloader.files[46]=(png,'older-confirmation.png')
                ask=provider._ask
                def unclear(body,parser,attempts):
                    def convert(raw):
                        data=json.loads(raw);data['groups'][0]['facts']['lifecycle']['kind']='unclear'
                        self.assert_wire_schema(data,body['format']);return parser(json.dumps(data))
                    return ask(body,convert,attempts)
                provider._ask=unclear
                self.assertEqual('lifecycle_question_sent',self.run_recurrence_batch(p,members).reason)
                result=ops_reply(p,message(902,text='B',reply_to_id=901))
                if binding=='historical':
                    self.assertEqual(202,result.issue_number);self.assertEqual(101,store.get(members[0].chat_id,46)['issue_number'])
                    self.assertTrue(all(not row['asset'].startswith('46-') for row in uploader.uploads))
                    self.assertEqual(1,store.count_recent_issues())
                    self.assertEqual(1,len(gh.calls))
                else:
                    self.assertEqual('pending_review',result.decision);self.assertEqual([],gh.calls);self.assertEqual([],uploader.uploads)

    def test_A_does_not_need_a_recurrence_partition_to_append_old_evidence(self):
        from tests.helpers import message
        store,provider,gh,sent,p,members,downloader,uploader,png=self.recurrence_batch_fixture(documents=True)
        ask=provider._ask
        def unclear(body,parser,attempts):
            def convert(raw):
                data=json.loads(raw);data['groups'][0]['facts']['lifecycle'].update(kind='unclear',knownworking_source_ids=[42,43,44,45],recurrence_source_ids=[])
                self.assert_wire_schema(data,body['format']);return parser(json.dumps(data))
            return ask(body,convert,attempts)
        provider._ask=unclear
        self.assertEqual('lifecycle_question_sent',self.run_recurrence_batch(p,members).reason)
        comments=[];gh.add_comment=lambda *args:comments.append(args) or 301
        self.assertEqual('followup',ops_reply(p,message(902,text='A',reply_to_id=901)).decision)
        self.assertEqual([101],[number for number,body in comments]);self.assertEqual([],gh.calls)

    def test_full_evidence_is_reloaded_after_upload_for_direct_A_and_B(self):
        from tests.helpers import message
        from tg_triage.rules_pending_store import RulesHalt,check_before_recovery
        for mode in ('direct','A','B'):
            kinds=['source','context','document','pixel','ownership','plan','membership']
            if mode!='direct':kinds+=['confirmation','answer_reservation']
            for kind in kinds:
                with self.subTest(mode=mode,kind=kind):
                    temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);path=str(Path(temp.name)/'state.sqlite')
                    values=(self.recurrence_batch_fixture(path=path,documents=True) if mode=='direct' else self.unclear_question_fixture(path=path))
                    store,provider,gh,sent,p,members,downloader,uploader,png=values
                    comments=[];gh.add_comment=lambda *args:comments.append(args) or 301
                    upload=uploader.upload
                    def mutate(tag,file,asset):
                        result=upload(tag,file,asset)
                        with store.connection:
                            if kind in ('source','context'):store.connection.execute("UPDATE messages SET text=text||' changed during upload' WHERE message_id=?",(43 if kind=='source' else 10,))
                            if kind=='ownership':store.connection.execute('UPDATE messages SET issue_number=999 WHERE message_id=43')
                            if kind=='membership':store.connection.execute('DELETE FROM rules_sources WHERE message_id=45')
                            if kind=='confirmation':store.connection.execute("UPDATE messages SET text='not the approved answer' WHERE message_id=902")
                            if kind=='answer_reservation':store.connection.execute("UPDATE rules_lifecycle_answers SET status='done'")
                            if kind=='plan':
                                row=store.connection.execute('SELECT plan FROM rules_batch_groups').fetchone();plan=json.loads(row[0]);plan['present']['object']['value']='changed object'
                                store.connection.execute('UPDATE rules_batch_groups SET plan=?',(json.dumps(plan),))
                        if kind=='document':downloader.files[45]=(b'changed document after upload','failure.txt')
                        if kind=='pixel':downloader.files[44]=(png+b'changed after upload','new-failure.png')
                        return result
                    uploader.upload=mutate
                    with self.assertRaises(RulesHalt):
                        if mode=='direct':self.run_recurrence_batch(p,members)
                        else:ops_reply(p,message(902,text=mode,reply_to_id=901))
                    self.assertEqual([],gh.calls);self.assertEqual([],comments)
                    with self.assertRaises(RulesHalt):check_before_recovery(Path(path))
                    if mode=='A':self.assertEqual('reserved',store.connection.execute('SELECT status FROM rules_comments').fetchone()[0])
                    else:self.assertEqual('reserved',store.connection.execute('SELECT status FROM rules_related_episodes').fetchone()[0])

    def test_required_processor_exceptions_missing_files_and_partial_failures_stop_all_posts(self):
        from unittest.mock import patch
        from tests.helpers import message
        from tg_triage.rules_pending_store import RulesHalt
        for mode in ('direct','A','B'):
            for failure in ('missing_file','exception','invalid_result','no_uploader','quota'):
                with self.subTest(mode=mode,failure=failure):
                    values=(self.recurrence_batch_fixture(documents=True) if mode=='direct' else self.unclear_question_fixture())
                    store,provider,gh,sent,p,members,downloader,uploader,png=values
                    comments=[];gh.add_comment=lambda *args:comments.append(args) or 301
                    if failure=='no_uploader':p.attachments.uploader=None
                    if failure=='quota':p.attachments.settings=replace(p.attachments.settings,max_per_hour=0)
                    if failure=='missing_file':
                        check=p.attachments._check_asset_proofs
                        def vanish(images,documents,proof,required):
                            documents[0][1].unlink();return check(images,documents,proof,required)
                        target='_check_asset_proofs';replacement=vanish
                    elif failure in ('exception','invalid_result'):
                        target='process'
                        def broken(*args,**kwargs):
                            if failure=='exception':raise OSError('local storage unavailable')
                            return None
                        replacement=broken
                    else:target='strict_uploads';replacement=True
                    with patch.object(p.attachments,target,replacement):
                        with self.assertRaises(RulesHalt):
                            if mode=='direct':self.run_recurrence_batch(p,members)
                            else:ops_reply(p,message(902,text=mode,reply_to_id=901))
                    self.assertEqual([],gh.calls);self.assertEqual([],comments);self.assertEqual([],uploader.uploads)
        # No-proof legacy callers still turn ordinary processor failure into a note.
        with patch.object(p.attachments,'process',side_effect=OSError('legacy failure')):
            result=p._images('legacy',[members[0]],live=True)
        self.assertTrue(result.notes)

    def test_resumed_direct_plan_rechecks_full_evidence_after_a_new_upload_wait(self):
        from unittest.mock import patch
        from tg_triage.rules_pending_store import RulesPendingStore,RulesHalt,check_before_recovery
        from tg_triage.rules_intake import sweep
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);path=str(Path(temp.name)/'state.sqlite')
        store,provider,gh,sent,p,members,downloader,uploader,png=self.recurrence_batch_fixture(path=path,documents=True)
        with patch('tg_triage.rules_intake._direct_related_episode',side_effect=RuntimeError('crash before related reservation')):
            with self.assertRaises(RuntimeError):self.run_recurrence_batch(p,members)
        p.rules_pending=RulesPendingStore(store);calls=len(provider.bodies)
        upload=uploader.upload
        def mutate(tag,file,asset):
            result=upload(tag,file,asset)
            with store.connection:store.connection.execute("UPDATE messages SET text=text||' edited while resumed upload waits' WHERE message_id=10")
            return result
        uploader.upload=mutate
        with self.assertRaises(RulesHalt):sweep(p)
        self.assertEqual([],gh.calls);self.assertEqual(calls,len(provider.bodies))
        with self.assertRaises(RulesHalt):check_before_recovery(Path(path))

    def test_target_is_rechecked_after_post_upload_proof_download_wait(self):
        from tests.helpers import message
        from tg_triage.rules_pending_store import RulesHalt
        for mode in ('direct','A','B'):
            with self.subTest(mode=mode):
                values=self.recurrence_batch_fixture(documents=True) if mode=='direct' else self.unclear_question_fixture()
                store,provider,gh,sent,p,members,downloader,uploader,png=values
                uploaded=[False];upload=uploader.upload
                def record(tag,file,asset):uploaded[0]=True;return upload(tag,file,asset)
                uploader.upload=record
                download=p.attachments.downloader
                def change_after_upload(chat,mid,directory):
                    result=download(chat,mid,directory)
                    if uploaded[0]:gh.states[101]='open'
                    return result
                p.attachments.downloader=change_after_upload
                comments=[];gh.add_comment=lambda *args:comments.append(args) or 301
                with self.assertRaises(RulesHalt):
                    if mode=='direct':self.run_recurrence_batch(p,members)
                    else:ops_reply(p,message(902,text=mode,reply_to_id=901))
                self.assertEqual([],gh.calls);self.assertEqual([],comments)

    def test_bound_topic_must_exist_and_be_OPEN_at_final_POST_gate(self):
        from tests.helpers import message
        from tg_triage.rules_pending_store import RulesHalt,check_before_recovery
        for mode in ('direct','A','B'):
            for status in ('CLOSED','EXPIRED','ESCALATED','unexpected','missing'):
                with self.subTest(mode=mode,status=status):
                    temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);path=str(Path(temp.name)/'state.sqlite')
                    values=self.recurrence_batch_fixture(path=path,documents=True) if mode=='direct' else self.unclear_question_fixture(path=path)
                    store,provider,gh,sent,p,members,downloader,uploader,png=values
                    topic=f'{members[0].chat_id}:{members[0].message_id}'
                    comments=[];gh.add_comment=lambda *args:comments.append(args) or 301
                    upload=uploader.upload
                    def revoke(tag,file,asset):
                        result=upload(tag,file,asset)
                        with store.connection:
                            if status=='missing':store.connection.execute('DELETE FROM rules_topics WHERE topic_id=?',(topic,))
                            else:store.connection.execute('UPDATE rules_topics SET status=? WHERE topic_id=?',(status,topic))
                        return result
                    uploader.upload=revoke
                    with self.assertRaises(RulesHalt):
                        if mode=='direct':self.run_recurrence_batch(p,members)
                        else:ops_reply(p,message(902,text=mode,reply_to_id=901))
                    self.assertEqual([],gh.calls);self.assertEqual([],comments)
                    table='rules_comments' if mode=='A' else 'rules_related_episodes'
                    self.assertEqual('reserved',store.connection.execute('SELECT status FROM '+table).fetchone()[0])
                    with self.assertRaises(RulesHalt):check_before_recovery(Path(path))

    def test_final_topic_owner_phase_and_group_lane_state_remain_authoritative(self):
        from tests.helpers import message
        from tg_triage.rules_pending_store import RulesHalt
        for mode in ('direct','A','B'):
            changes=['owner','legacy_review','group_lane']+(['batch_closed'] if mode=='direct' else [])
            for changed in changes:
                with self.subTest(mode=mode,changed=changed):
                    values=self.recurrence_batch_fixture(documents=True) if mode=='direct' else self.unclear_question_fixture()
                    store,provider,gh,sent,p,members,downloader,uploader,png=values
                    topic=f'{members[0].chat_id}:{members[0].message_id}'
                    comments=[];gh.add_comment=lambda *args:comments.append(args) or 301
                    upload=uploader.upload
                    def revoke(tag,file,asset):
                        result=upload(tag,file,asset)
                        with store.connection:
                            if changed=='owner':store.connection.execute('UPDATE rules_topics SET source_chat_id=source_chat_id-1 WHERE topic_id=?',(topic,))
                            if changed=='legacy_review':store.connection.execute("UPDATE rules_topics SET phase='LEGACY_REVIEW' WHERE topic_id=?",(topic,))
                            if changed=='group_lane':store.connection.execute('UPDATE rules_batch_groups SET status=?',('DONE' if mode=='direct' else 'RESERVED',))
                            if changed=='batch_closed':store.connection.execute("UPDATE rules_topics SET status='CLOSED' WHERE topic_id LIKE 'batch:%'")
                        return result
                    uploader.upload=revoke
                    with self.assertRaises(RulesHalt):
                        if mode=='direct':self.run_recurrence_batch(p,members)
                        else:ops_reply(p,message(902,text=mode,reply_to_id=901))
                    self.assertEqual([],gh.calls);self.assertEqual([],comments)
