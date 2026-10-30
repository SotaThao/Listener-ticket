"""THINKING production paths with fake external tools; no live ticket/message writes."""
import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tg_triage.classifier import Classifier
from tg_triage.config import load_config, ConfigError
from tg_triage.model_lock import ModelLock
from tg_triage.pipeline import Pipeline
from tg_triage.provider import CloudProvider
from tg_triage.store import Store
from tg_triage.thinking import Turn, CURRENT, BudgetExhausted, compact_context
from tg_triage.thinking_transport import DeadlineHTTPS, _addresses
from tg_triage.ai_transport import ProviderError
from tests.helpers import config, message, topic, FakeHTTP, FakeTopicModel, NOW
from tests.helpers import EXAMPLE
from tests.test_followups import GitHub, Model, answer


def envelope(groups):
    return {'message': {'content': json.dumps({'groups':[vars(g) for g in groups]},ensure_ascii=False),
                        'thinking':'PRIVATE REASONING MUST NOT PERSIST'}}


class ThinkingTests(unittest.TestCase):
    def initial(self, groups, texts=None, *, enabled=True):
        cfg=replace(config('live',window=5,max_messages=3),thinking_enabled=enabled)
        store=Store(':memory:',clock=lambda:NOW); self.addCleanup(store.close)
        http=FakeHTTP(envelope(groups))
        model=Classifier(cfg.ollama,opener=http)
        gh=GitHub(); sent=[]
        p=Pipeline(cfg,store,model,gh,notice_sender=lambda c,m,t: sent.append((m,t)) or 900+m)
        for i,text in enumerate(texts or ['Nút lưu báo cáo bị lỗi','Bấm lưu không phản hồi','Nhờ team sửa nút lưu'],1):
            p.process(message(i,text=text,sender_id=7,mentions_listener=False))
        return store,http,gh,sent,p

    def test_three_messages_one_ticket_real_default_prompt_and_first_reply(self):
        store,http,gh,sent,p=self.initial([topic((1,2,3))])
        self.assertEqual(len(http.calls),1)
        self.assertEqual(len(gh.calls),1)
        self.assertEqual([s[0] for s in sent],[1])
        self.assertIn('/issues/101',sent[0][1])
        row=store.connection.execute('SELECT record FROM thinking_decisions').fetchone()
        record=json.loads(row[0])
        self.assertEqual(record['status'],'verified')
        self.assertEqual(record['budget']['model_calls_used'],1)
        self.assertEqual(record['harness_owner'],'AlexChu')
        self.assertNotIn('PRIVATE',row[0])
        self.assertNotIn('Nút lưu',row[0])

    def test_three_requests_three_tickets_corresponding_replies(self):
        store,http,gh,sent,p=self.initial([topic((1,)),topic((2,)),topic((3,))],
            ['Nút lưu báo cáo lỗi','Thêm bộ lọc ngày trong danh sách','Sửa stop opt out trong automation'])
        self.assertEqual(len(http.calls),1)
        self.assertEqual(len(gh.calls),3)
        self.assertEqual([s[0] for s in sent],[1,2,3])
        for i,(_,text) in enumerate(sent,101):
            self.assertIn('/issues/'+str(i),text)

    def test_default_single_case_prompt_preserves_optional_details_under_budget(self):
        from tests.test_issue_templates import BUG_DETAILS
        from tests.helpers import FakeGitHub
        class SingleClassifier(Classifier):
            split_topics=None
        cfg=config('live'); store=Store(':memory:',clock=lambda:NOW); self.addCleanup(store.close)
        value=dict(category='bug',type='bug',actionable=True,title='Lỗi đăng nhập',summary='Đăng nhập lỗi',
            priority='medium',confidence=.9,missing_info=[],reason='Lỗi rõ',key_message_ids=[1],details=BUG_DETAILS)
        http=FakeHTTP({'message':{'content':json.dumps(value,ensure_ascii=False)}})
        gh=FakeGitHub(); p=Pipeline(cfg,store,SingleClassifier(cfg.ollama,opener=http),gh)
        result=p.process(message(1,text='Đăng nhập bị lỗi',sender_id=7))
        self.assertEqual(result.decision,'issue')
        self.assertEqual(len(http.calls),1)
        self.assertIn(BUG_DETAILS['environment'],gh.calls[0].body)

    def test_missing_work_details_reply_in_group_untagged(self):
        _,http,gh,sent,_=self.initial([topic((1,2,3),verdict='needs_human',missing_info=('Màn hình nào cần sửa?',))])
        self.assertEqual(len(gh.calls),1)
        self.assertIn('Màn hình nào',sent[0][1])

    def test_decision_persisted_before_external_create(self):
        cfg=config('live'); store=Store(':memory:',clock=lambda:NOW); self.addCleanup(store.close)
        gh=GitHub()
        def before():
            r=json.loads(store.connection.execute('SELECT record FROM thinking_decisions').fetchone()[0])
            self.assertEqual(r['status'],'planned')
        gh.before_create=before
        p=Pipeline(cfg,store,FakeTopicModel((topic((1,)),)),gh)
        p.process(message(sender_id=7))
        self.assertEqual(len(gh.calls),1)

    def test_kill_switch_keeps_legacy_path_without_decision_journal(self):
        store,http,gh,sent,_=self.initial([topic((1,2,3))],enabled=False)
        self.assertEqual(len(gh.calls),1)
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM thinking_decisions').fetchone()[0],0)

    def test_config_switch_loads_defaults_and_rejects_non_boolean(self):
        original=EXAMPLE.read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'listener.toml'
            for literal,expected in [('true',True),('false',False),('"false"',None)]:
                path.write_text(original.replace('enabled = true','enabled = '+literal),encoding='utf-8')
                if expected is None:
                    with self.assertRaises(ConfigError):
                        load_config(path,validate_only=True)
                else:
                    self.assertEqual(load_config(path,validate_only=True).thinking_enabled,expected)

    def test_output_cap_is_narrow_only_and_disabled_switch_preserves_legacy_cap(self):
        store=Store(':memory:'); self.addCleanup(store.close)
        for active,narrow,expected in [(True,True,512),(False,True,2048),(True,False,2048)]:
            model=Classifier(config().ollama); model.max_output=2048
            token=CURRENT.set(Turn(store,'cap','root',1)) if active else CURRENT.set(None)
            response={'source_roles':[dict(message_id=1,role='ancillary',confidence=.9)]} if narrow else answer('chat')
            try:
                with patch('tg_triage.thinking_transport.complete',return_value=response) as bounded, patch('tg_triage.ai_transport.complete',return_value=response) as legacy:
                    if narrow:
                        model.analyze_source_roles([], [message(1)])
                    else:
                        model.analyze_followup([],message(2),[],[])
                    call=bounded.call_args if active else legacy.call_args
                    self.assertEqual(call.kwargs['max_output'],expected)
            finally:
                CURRENT.reset(token)

    def test_failed_decision_persistence_prevents_external_create(self):
        cfg=config('live'); store=Store(':memory:',clock=lambda:NOW); self.addCleanup(store.close)
        gh=GitHub(); p=Pipeline(cfg,store,FakeTopicModel((topic((1,)),)),gh)
        with patch('tg_triage.thinking.Turn.record',side_effect=OSError('journal unavailable')):
            with self.assertRaises(OSError):
                p.process(message(sender_id=7))
        self.assertEqual(gh.calls,[])

    def test_model_partition_is_hypothesis_and_reply_targets_are_truthful(self):
        store,_,_,_,_=self.initial([topic((1,2,3))])
        events=[json.loads(r[0]) for r in store.connection.execute('SELECT record FROM thinking_decision_events ORDER BY version')]
        self.assertEqual(events[0]['confirmed']['source_ids'],[1,2,3])
        self.assertNotIn('partitions',events[0]['confirmed'])
        self.assertEqual(events[0]['proposed_plan'][0]['ids'],[1,2,3])
        self.assertIsNone(events[-1]['reply_to_message_id'])
        self.assertEqual(events[-1]['reply_to_message_ids'],[1])
        self.assertEqual(events[-1]['input_message_id'],3)

    def test_cas_rejects_stale_writer_and_resumed_version_increments(self):
        store=Store(':memory:'); self.addCleanup(store.close)
        a=Turn(store,'same','root',1); b=Turn(store,'same','root',1)
        a.record('clarify',{})
        with self.assertRaises(RuntimeError):
            b.record('update',{})
        resumed=Turn(store,'same','root',1)
        self.assertEqual(resumed.record('clarify',{})['state_version'],2)

    def test_deadline_and_context_and_lookup_budgets(self):
        store=Store(':memory:'); self.addCleanup(store.close)
        now=[0.0]; turn=Turn(store,'x','r',1,clock=lambda:now[0])
        body={'model':'test','messages':[{'role':'user','content':'x'}]}
        self.assertEqual(turn.attempt(body),20)
        now[0]=35
        self.assertEqual(turn.attempt(body),5)
        with self.assertRaises(BudgetExhausted):
            turn.attempt(body)
        turn=Turn(store,'y','r',2)
        with self.assertRaises(BudgetExhausted):
            turn.attempt(dict(body,messages=[{'role':'user','content':'đ'*5000}]))
        for _ in range(3):
            turn.lookup()
        with self.assertRaises(BudgetExhausted):
            turn.lookup()

    def test_cloud_invalid_retries_share_budget_and_cannot_fallback_third_attempt(self):
        cfg=config(); cfg=replace(cfg,model=replace(cfg.model,provider='openai',name='test',think=False,
            cloud_consent=True,base_url='https://api.openai.com/v1'))
        store=Store(':memory:'); self.addCleanup(store.close)
        provider=CloudProvider(cfg)
        turn=Turn(store,'x','r',1); token=CURRENT.set(turn)
        try:
            with patch('tg_triage.ai_transport.read_key',return_value='fake'), patch('tg_triage.thinking_transport.complete',return_value={}) as request:
                result=provider.classify('nội dung')
            self.assertIsNone(result)
            self.assertEqual(request.call_count,2)
            self.assertEqual(turn.calls,2)
            self.assertEqual(turn.reason,'model_call_budget_exhausted')
        finally:
            CURRENT.reset(token)

    def test_lock_wait_deadline_consumes_no_http_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            lock=ModelLock(directory)
            store=Store(':memory:'); self.addCleanup(store.close)
            model=Classifier(config().ollama,opener=FakeHTTP())
            turn=Turn(store,'x','r',1); turn.deadline=time.monotonic()+.12
            results=[]
            def worker():
                token=CURRENT.set(turn)
                try:
                    results.append(model.classify('request'))
                finally:
                    CURRENT.reset(token)
            with patch('tg_triage.classifier.MODEL_LOCK',lock):
                with lock.enter():
                    t=threading.Thread(target=worker); t.start(); t.join(1)
                    self.assertFalse(t.is_alive())
            self.assertEqual(results,[None])
            self.assertEqual(turn.calls,0)

    def test_dns_uses_remaining_deadline(self):
        def slow(*args,**kwargs):
            time.sleep(.05)
            return []
        with patch('tg_triage.thinking_transport.socket.getaddrinfo',side_effect=slow):
            started=time.monotonic()
            with self.assertRaises(ProviderError) as error:
                _addresses('example.invalid',started+.01)
            self.assertIn(error.exception.code,('dns_timeout','timeout'))
            self.assertLess(time.monotonic()-started,.2)

    def test_tls_handshake_gets_only_remaining_time_after_tcp(self):
        now=[0.0]; raw=unittest.mock.Mock()
        def tcp(*args,**kwargs):
            now[0]=19
            return raw
        with patch('tg_triage.thinking_transport.time.monotonic',side_effect=lambda:now[0]):
            conn=DeadlineHTTPS('example.com','93.184.216.34',20)
            with patch('tg_triage.thinking_transport.socket.create_connection',side_effect=tcp), patch.object(conn._context,'wrap_socket',return_value=raw):
                conn.connect()
            raw.settimeout.assert_called_once_with(1)

    def test_deepseek_false_is_explicitly_disabled_on_wire_only_for_deepseek(self):
        from types import SimpleNamespace
        from tg_triage.thinking_transport import complete
        from unittest.mock import Mock
        for provider,url in [('deepseek','https://api.deepseek.com'),('openai','https://api.openai.com/v1'),
                             ('compatible','https://example.com'),('anthropic','https://api.anthropic.com'),
                             ('ollama','http://127.0.0.1:11434')]:
            with self.subTest(provider=provider):
                content=({'content':[{'type':'tool_use','name':'result','input':{}}]} if provider=='anthropic'
                    else {'message':{'content':'{}'}} if provider=='ollama'
                    else {'choices':[{'message':{'content':'{}','reasoning_content':'PRIVATE_NOT_READ'}}]})
                response=Mock(status=200)
                response.fp=SimpleNamespace(raw=SimpleNamespace(_sock=Mock()))
                response.isclosed.side_effect=[False,True]
                response.read1.return_value=json.dumps(content).encode()
                connection=Mock(); connection.getresponse.return_value=response
                with patch('tg_triage.thinking_transport._addresses',return_value=['93.184.216.34']), \
                     patch('tg_triage.thinking_transport.DeadlineHTTPS',return_value=connection), \
                     patch('tg_triage.thinking_transport.http.client.HTTPConnection',return_value=connection), \
                     patch('tg_triage.thinking_transport.threading.Timer'):
                    result=complete(provider,url,'test',[],{},key='fake',think=False)
                self.assertEqual(result,{})
                body=json.loads(connection.request.call_args.args[2])
                if provider=='deepseek':
                    self.assertEqual(body['thinking'],{'type':'disabled'})
                    self.assertEqual(body['temperature'],0)
                else:
                    self.assertNotIn('thinking',body)
                    self.assertNotIn('temperature',body)

    def test_screenshot_confirmed_context_survives_restart_and_history_trim(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'test.sqlite'
            cfg=config('live',window=5,max_messages=4)
            store=Store(path,clock=lambda:NOW)
            model=Model(ids=(429,430,431,432)); gh=GitHub(); sent=[]
            send=lambda c,m,t: sent.append((m,t)) or 1000+m
            p=Pipeline(cfg,store,model,gh,notice_sender=send)
            for mid,text in [(429,'nên thêm ký tự'),(430,''),(431,'sửa stop opt out'),(432,'')]:
                p.process(message(mid,text=text,sender_id=7,has_image=not text,mentions_listener=False))
            p.process(message(440,text='đây là 2 tin nhắn cần lên 2 ticket riêng biệt',reply_to_id=1429,sender_id=7))
            p.process(message(441,text='đây là ticket 1',reply_to_id=429,sender_id=7))
            self.assertIn('ticket 1',sent[-1][1])
            model.value=answer('chat')
            for mid in range(442,449):
                p.process(message(mid,text='cảm ơn',reply_to_id=1441,sender_id=7))
            store.close()
            store=Store(path,clock=lambda:NOW); self.addCleanup(store.close)
            p=Pipeline(cfg,store,model,gh,notice_sender=send)
            p.process(message(449,text='đây là ticket 2',reply_to_id=431,sender_id=7))
            self.assertEqual(len(gh.calls),2)
            self.assertIn('stop opt out',gh.calls[1].body)
            self.assertNotIn('nên thêm ký tự',gh.calls[1].body)
            root=store.connection.execute('SELECT context_state,split_plan FROM followup_roots').fetchone()
            self.assertEqual(json.loads(root[0])['bindings'],{'429':0,'431':1})
            self.assertEqual(json.loads(root[1])['unresolved_message_ids'],[430,432])
            record=json.loads(store.connection.execute("SELECT record FROM thinking_decisions WHERE turn_key='followup:10'").fetchone()[0])
            self.assertEqual(record['confirmed']['bindings'],{'429':0,'431':1})
            before=len(gh.calls)
            self.assertIsNone(p.process(message(449,text='đây là ticket 2',reply_to_id=431,sender_id=7)))
            self.assertEqual(len(gh.calls),before)
            store.close()


if __name__=='__main__':
    unittest.main()
