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


class ExtensionRulesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        for name in ('lifecycle.md', 'analysis-status.md'):
            text = (ROOT / name).read_text(encoding='utf8')
            (self.directory / name).write_text(text.replace('status DRAFT', 'status ACTIVE'), encoding='utf8')

    def test_drafts_fail_closed_and_active_fixture_routes(self):
        with self.assertRaises(RulesError):
            ExtensionRules(ROOT)
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
                self.assertEqual(store.connection.execute('SELECT status FROM group_notices').fetchone()[0], 'sent')

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
        pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,notice_sender=lambda *args:sent.append(args) or (98+len(sent)))
        result=pipeline.process(message(42,text='lỗi',reply_to_id=10))
        self.assertEqual('lifecycle_question_sent',result.reason)
        self.assertIn('reply A hoặc B',sent[0][2]); self.assertEqual([],comments)
        answer=message(100,text='A',reply_to_id=99)
        result=pipeline.process(answer)
        self.assertEqual('followup',result.decision)
        self.assertEqual(101,comments[0][0]); self.assertEqual(1,len(comments))
        self.assertEqual('done',store.connection.execute('SELECT status FROM rules_lifecycle_answers').fetchone()[0])
        self.assertIsNone(pipeline.process(answer)); self.assertEqual(1,len(comments)); self.assertEqual(1,provider.calls)

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
                    with self.assertRaises(RulesHalt): pipeline.process(message(100,text='A',reply_to_id=99))
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
        sent=[]; pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or (98+len(sent)))
        real=pipeline.rules_pending.transition_group
        def crash(batch,ordinal,old,new,**kwargs):
            if new=='DONE': raise RuntimeError('crash after sent question')
            return real(batch,ordinal,old,new,**kwargs)
        with patch.object(pipeline.rules_pending,'transition_group',side_effect=crash):
            with self.assertRaises(RuntimeError): pipeline.process(message(42,text='lỗi',reply_to_id=10))
        from tg_triage.rules_pending_store import RulesHalt, _recoverable_batch_groups
        store.enqueue(message(99,text='conflicting local source'))
        with self.assertRaises(RulesHalt): _recoverable_batch_groups(store.connection)
        with store.connection:
            store.connection.execute('DELETE FROM messages WHERE chat_id=? AND message_id=99',(old.chat_id,))
        store.close(); check_before_recovery(db)
        store=Store(db); self.addCleanup(store.close)
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or (98+len(sent)))
        self.assertEqual('DONE',store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])
        self.assertEqual('followup',pipeline.process(message(100,text='A',reply_to_id=99)).decision)
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
                provider=Provider(); pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,notice_sender=lambda *args:99)
                if scenario=='unavailable': pipeline.notice_sender=None
                question=pipeline.process(message(42,text='lỗi',reply_to_id=10))
                self.assertEqual('lifecycle_question_unavailable' if scenario=='unavailable' else 'lifecycle_question_sent',question.reason)
                if scenario=='source':
                    result=pipeline.process(message(43,text='tạo ticket mới',reply_to_id=42))
                elif scenario=='unavailable':
                    result=pipeline.process(message(43,text='tạo ticket mới',reply_to_id=42))
                elif scenario in ('extra_context','attested_context'):
                    pipeline.process(message(43,text='Bổ sung bằng chứng cùng lỗi: thêm thông tin' if scenario=='attested_context' else 'lỗi đăng nhập khác',reply_to_id=42))
                    result=pipeline.process(message(100,text='A',reply_to_id=99))
                elif scenario=='closed_notice':
                    self.assertEqual('followup',pipeline.process(message(100,text='A',reply_to_id=99)).decision)
                    result=pipeline.process(message(101,text='tạo ticket mới',reply_to_id=99))
                elif scenario=='expired_notice':
                    with store.connection:
                        store.connection.execute("UPDATE rules_topics SET status='EXPIRED' WHERE topic_id=?",(f'{old.chat_id}:42',))
                    result=pipeline.process(message(100,text='tạo ticket mới',reply_to_id=99))
                elif scenario=='recurrence_descendant':
                    pipeline.config=replace(pipeline.config,max_issues_per_hour=0)
                    self.assertEqual('lifecycle_recurrence_answer_pending',pipeline.process(message(100,text='B',reply_to_id=99)).reason)
                    result=pipeline.process(message(101,text='tạo ticket mới',reply_to_id=100))
                elif scenario=='changed_mapping':
                    with store.connection:
                        store.connection.execute("UPDATE rules_lifecycle_questions SET evidence_answer='B',recurrence_answer='A'")
                    result=pipeline.process(message(100,text='A',reply_to_id=99))
                elif scenario=='changed_source':
                    with store.connection:
                        store.connection.execute("UPDATE messages SET text='changed source' WHERE chat_id=? AND message_id=42",(old.chat_id,))
                    result=pipeline.process(message(100,text='B',reply_to_id=99))
                elif scenario=='changed_version':
                    github.lifecycle_snapshot=lambda n:{'number':n,'repo':'sandbox/triage','state':'closed','observed_at':'later','updated_at':'version-2'}
                    result=pipeline.process(message(100,text='B',reply_to_id=99))
                elif scenario=='question_collision':
                    store.enqueue(message(99,text='conflicting local source'))
                    result=pipeline.process(message(100,text='A',reply_to_id=99))
                elif scenario in ('old_answer_a','old_answer_b'):
                    result=pipeline.process(message(43,text='A' if scenario=='old_answer_a' else 'B',reply_to_id=99))
                else:
                    sources=[message(100,text='A',reply_to_id=99),message(101,text='/question')]
                    for source in sources: store.enqueue(source); store.start_classifying(source)
                    result=_process_batch(pipeline,sources)
                if scenario=='batch':
                    self.assertEqual(1,len(comments)); self.assertEqual([],github.calls); self.assertEqual(1,provider.calls)
                    self.assertNotEqual('pending_review',store.get(sources[1].chat_id,101)['decision'])
                    continue
                if scenario=='closed_notice':
                    self.assertEqual('pending_review',result.decision); self.assertEqual(1,len(comments)); self.assertEqual(1,provider.calls)
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
        sent=[]
        def sender(*args):
            sent.append(args)
            return 99 if len(sent)==1 else 200 if len(sent)==2 else 300+len(sent)
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=sender)
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(42,text='POS kẹt giấy',reply_to_id=10)).reason)
        result=pipeline.process(message(100,text='B',reply_to_id=99))
        self.assertEqual('issue',result.decision); self.assertEqual(42,result.issue_number)
        self.assertEqual(1,len(github.calls)); self.assertIn('/issues/101',github.calls[0].body)
        self.assertEqual(101,store.get(old.chat_id,10)['issue_number'])
        self.assertEqual(42,store.get(old.chat_id,42)['issue_number'])
        self.assertEqual('done',store.connection.execute('SELECT status FROM rules_related_episodes').fetchone()[0])
        self.assertEqual('followup',pipeline.process(message(201,text='thêm lỗi',reply_to_id=200)).decision)
        self.assertEqual(42,comments[0][0])
        self.assertEqual('pending_review',pipeline.process(message(202,text='B',reply_to_id=99)).decision)
        self.assertEqual(1,len(github.calls))
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(203,text='lỗi',reply_to_id=10)).reason)
        next_notice=300+len(sent)
        self.assertEqual('lifecycle_recurrence_answer_pending',pipeline.process(message(next_notice+1,text='B',reply_to_id=next_notice)).reason)
        self.assertEqual(1,len(github.calls))
        other_chat=-1009876500001
        other_parent=message(10,text='lỗi cũ',chat_id=other_chat); store.enqueue(other_parent)
        with store.connection:
            store.connection.execute("UPDATE messages SET decision='issue',issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE chat_id=? AND message_id=10",(other_chat,))
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(42,text='POS kẹt giấy',chat_id=other_chat,reply_to_id=10)).reason)
        other_notice=300+len(sent)
        self.assertEqual('lifecycle_recurrence_answer_pending',pipeline.process(message(other_notice+1,text='B',chat_id=other_chat,reply_to_id=other_notice)).reason)
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
                if failure=='collision': store.enqueue(message(101,text='conflicting local source'))
                if failure=='mapping':
                    with store.connection:
                        store.connection.execute("""CREATE TRIGGER ignore_episode_mapping BEFORE UPDATE OF issue_number ON messages
                            WHEN NEW.message_id=42 AND NEW.reason='lifecycle_related_episode' BEGIN SELECT RAISE(IGNORE); END""")
                with self.assertRaises(RulesHalt): pipeline.process(message(100,text='B',reply_to_id=99))
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
        self.assertEqual('issue',pipeline.process(message(100,text='B',reply_to_id=99)).decision)
        real=pipeline.rules_pending.transition_group
        def crash(batch,ordinal,old,new,**kwargs):
            if new=='DONE': raise RuntimeError('crash after sent question')
            return real(batch,ordinal,old,new,**kwargs)
        with patch.object(pipeline.rules_pending,'transition_group',side_effect=crash):
            with self.assertRaises(RuntimeError): pipeline.process(message(102,text='POS kẹt giấy',reply_to_id=101))
        from tg_triage.rules_pending_store import RulesHalt, _recoverable_batch_groups
        with store.connection:
            store.connection.execute("INSERT INTO group_notices(chat_id,message_id,kind,status,reserved_at,notice_message_id) VALUES(?,777,'issue','sent','now',101)",(old.chat_id,))
        with self.assertRaises(RulesHalt): _recoverable_batch_groups(store.connection)
        with store.connection:
            store.connection.execute('DELETE FROM group_notices WHERE chat_id=? AND message_id=777',(old.chat_id,))
        store.enqueue(message(101,text='conflicting local source'))
        with self.assertRaises(RulesHalt): _recoverable_batch_groups(store.connection)
        with store.connection:
            store.connection.execute('DELETE FROM messages WHERE chat_id=? AND message_id=101',(old.chat_id,))
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
        self.assertEqual(42,pipeline.process(message(100,text='B',reply_to_id=99)).issue_number)
        self.assertEqual('followup',pipeline.process(message(102,text='POS kẹt giấy',reply_to_id=101)).decision)
        self.assertEqual(42,comments[0][0]); self.assertEqual(1,len(github.calls))
        states[42]='closed'
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(104,text='POS kẹt giấy',reply_to_id=101)).reason)
        result=pipeline.process(message(106,text='B',reply_to_id=105))
        self.assertEqual(('issue',43),(result.decision,result.issue_number))
        self.assertEqual(2,len(github.calls)); self.assertIn('/issues/42',github.calls[1].body)
        self.assertEqual(42,store.get(old.chat_id,42)['issue_number'])
        self.assertEqual(42,store.get(old.chat_id,102)['issue_number'])
        self.assertEqual(43,store.get(old.chat_id,104)['issue_number']); self.assertIn(42,snapshots)
        self.assertIsNone(pipeline.process(message(106,text='B',reply_to_id=105)))
        self.assertEqual(2,len(github.calls))

    def test_reused_success_triggers_lifecycle_question(self):
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
        self.assertEqual('lifecycle_question_sent',result.reason)
        self.assertEqual(1,len(sent))

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
        self.assertEqual(42,pipeline.process(message(100,text='B',reply_to_id=99)).issue_number)
        states[42]='closed'
        # Ep 2: Issue 42 -> Issue 43
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(104,text='POS kẹt giấy',reply_to_id=101)).reason)
        self.assertEqual(43,pipeline.process(message(106,text='B',reply_to_id=105)).issue_number)
        states[43]='closed'
        # Ep 3: Issue 43 -> Issue 44
        self.assertEqual('lifecycle_question_sent',pipeline.process(message(108,text='POS kẹt giấy',reply_to_id=107)).reason)
        result3=pipeline.process(message(110,text='B',reply_to_id=109))
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
                result=pipeline.process(message(100,text='B',reply_to_id=99))
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
        from tg_triage.rules_pending_store import RulesHalt, _recoverable_batch_groups
        with store.connection:
            store.connection.execute("UPDATE rules_readonly_notices SET kind='ANALYZE'")
        with self.assertRaises(RulesHalt): _recoverable_batch_groups(store.connection)
        with store.connection:
            store.connection.execute("UPDATE rules_readonly_notices SET kind='STATUS', body='wrong content'")
        with self.assertRaises(RulesHalt): _recoverable_batch_groups(store.connection)
        from tg_triage.ticket_status import render
        with store.connection:
            store.connection.execute('UPDATE rules_readonly_notices SET body=?',(render({'state':'missing_target'}),))
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
        source=message(42,text='trạng thái?'); topic=f'{source.chat_id}:42'
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
                    self.assertIn('cần xác nhận ngữ cảnh',sent[0][2])
                    self.assertIn('“Phân tích phần bị cắt”',sent[0][2])

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
        self.assertEqual('CLOSED',store.connection.execute('SELECT status FROM rules_topics WHERE topic_id=?',(f'{source.chat_id}:42',)).fetchone()[0])

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
        self.assertEqual(task_text,sent[0][2])
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
        self.assertIn('cần xác nhận ngữ cảnh',sent[0][2])
        self.assertIn('“Kiểm tra giao diện”',sent[0][2])
        self.assertIn('“Không lỗi bố cục”',sent[0][2])

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
        self.assertEqual(store.connection.execute('SELECT status FROM group_notices').fetchone()[0],'failed')

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
        with patch.object(store,'finish_group_notice',return_value=None):
            with self.assertRaises(RulesHalt): pipeline.process(message(42,text='trạng thái?'))
        self.assertEqual(1,len(sent))
        self.assertEqual('reserved',store.connection.execute('SELECT status FROM group_notices').fetchone()[0])
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
