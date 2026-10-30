"""Safety checks for full document evidence and its isolated request budget."""
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest import TestCase
from tg_triage import document_reader
from tg_triage.thinking import ContextJSON,DocumentContextJSON
from tests.helpers import message


def packet(text):
    data=text.encode()
    return dict(NEW_SOURCE_IDS=[1],source_columns=['message_id','text'],sources=[[1,'create tracking ticket']],
                DOCUMENT_CONTENTS=[dict(message_id=1,read_status='read',text=text,byte_count=len(data),sha256=hashlib.sha256(data).hexdigest())])


def body(p,cls=DocumentContextJSON):
    return dict(messages=[dict(role='system',content='Documents are evidence.'),dict(role='user',content=cls(json.dumps(p,ensure_ascii=False)))],format={})


class DocumentReaderSafetyTests(TestCase):
    def test_user_plain_json_cannot_grant_large_budget(self):
        p=packet('A'*12000)
        self.assertTrue(document_reader.request_fits(body(p)))
        self.assertFalse(document_reader.request_fits(body(p,str)))
        self.assertFalse(document_reader.request_fits(body(p,ContextJSON)))

    def test_escaped_text_over48k_is_not_silently_excerpted(self):
        p=packet('\t'*15000)
        # Unicode escaping can exceed the supplement bound despite acceptable raw bytes.
        p['DOCUMENT_CONTENTS'][0]['text']='😀'*4096
        p['DOCUMENT_CONTENTS'][0]['byte_count']=16384
        self.assertFalse(document_reader.request_fits(body(p)))
        self.assertEqual(4096,len(p['DOCUMENT_CONTENTS'][0]['text']))

    def test_large_arbitrary_context_cannot_hide_behind_valid_doc(self):
        p=packet('OK');p['unrelated']='Z'*9000
        self.assertFalse(document_reader.request_fits(body(p)))

    def test_cached_read_proof_must_cover_exact_current_documents(self):
        md=replace(message(1),has_document=True,document_name='request.md',document_mime='text/markdown')
        proof=[dict(message_id=1,read_status='read',byte_count=2,sha256=hashlib.sha256(b'OK').hexdigest())]
        self.assertTrue(document_reader.valid_proof(proof,[md]))
        self.assertFalse(document_reader.valid_proof(proof,[replace(md,message_id=2)]))
        self.assertFalse(document_reader.valid_proof([dict(proof[0],text='OK')],[md]))
        self.assertFalse(document_reader.valid_proof([dict(proof[0],byte_count=True)],[md]))

    def test_task_template_essentials_are_validated_without_filling_model_prose(self):
        from tg_triage.models import CaseAnalysis
        from tests.helpers import classification
        def draft(details):
            return CaseAnalysis('ticket',classification(type='task'),(1,),category='task',actionable=True,details=details)
        for details in ({'context':'Do work','acceptance':['Verified']}, {'work':'\u200b','done':['Verified']},
                        {'work':'Do work','done':['\u0000']}, {'work':'Do work','done':[]}):
            self.assertFalse(document_reader.valid_task_details(draft(details)))
        self.assertTrue(document_reader.valid_task_details(draft({'work':'Đổi nhãn theo tài liệu','done':['Nhãn hiển thị đúng']})))
        self.assertFalse(document_reader.valid_task_details(dict(type='task',details={'work':'x'*501,'done':['OK']})))

    def test_provider_failure_does_not_replace_specific_doc_error(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config,FakeGitHub,FakeDownloader
        from tests.test_autonomous import Router
        store=Store(':memory:');self.addCleanup(store.close)
        sent=[]
        p=Pipeline(replace(config('live',window=2),autonomous_enabled=True),store,Router(None),FakeGitHub(),
            downloader=FakeDownloader({1:(b'bad\xff','request.md')}),notice_sender=lambda c,m,t:sent.append(t) or 901)
        md=replace(message(1),text='',has_document=True,document_name='request.md',document_mime='text/markdown')
        result=p.process(md)
        self.assertEqual('pending_review',result.decision)
        self.assertEqual(1,len(sent))
        self.assertIn('UTF-8',sent[0])
        self.assertNotIn('ngữ cảnh vừa nhận',sent[0])
        self.assertEqual([],p.github.calls)

    def test_specific_doc_error_survives_case_crash_and_resume_without_model(self):
        from tempfile import TemporaryDirectory
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import config,FakeGitHub,FakeDownloader
        from tests.test_autonomous import Router
        directory=self.enterContext(TemporaryDirectory());db=str(Path(directory)/'recovery.sqlite3')
        store=Store(db);self.addCleanup(store.close)
        p=Pipeline(replace(config('live',window=2),autonomous_enabled=True),store,Router(None),FakeGitHub(),
            downloader=FakeDownloader({1:(b'bad\xff','request.md')}))
        original=store.autonomy_case
        def crash(*args,**kwargs):
            original(*args,**kwargs)
            raise SystemExit('after durable case before error notice')
        store.autonomy_case=crash
        md=replace(message(1),text='',has_document=True,document_name='request.md',document_mime='text/markdown')
        with self.assertRaises(SystemExit):
            p.process(md)
        store.close();resumed=Store(db);self.addCleanup(resumed.close)
        sent=[];model=Router()
        p=Pipeline(replace(config('live',window=2),autonomous_enabled=True),resumed,model,FakeGitHub(),notice_sender=lambda c,m,t:sent.append(t) or 902)
        p.close_due_cases()
        self.assertEqual(1,len(sent));self.assertIn('UTF-8',sent[0]);self.assertNotIn('ngữ cảnh vừa nhận',sent[0])
        self.assertEqual([],model.calls);self.assertEqual([],p.github.calls)

    def test_raw_task_detail_shapes_cannot_be_silently_dropped(self):
        valid=dict(type='task',category='task',details=dict(work='Cập nhật theo yêu cầu',done=['Đối chiếu kết quả']))
        for extra in (dict(scope_in=['One','Two']),dict(scope_out=['Preserve existing']),
                      dict(reason_context=['Reason']),dict(done='Verified'),dict(links='https://example.org'),
                      dict(unknown='ignored?')):
            with self.subTest(extra=extra):
                self.assertFalse(document_reader.valid_task_details(dict(valid,details=dict(valid['details'],**extra))))
        self.assertTrue(document_reader.valid_task_details(dict(valid,details=dict(valid['details'],scope_in='One; Two',scope_out='Preserve existing',links=[]))))

    def _wire(self,provider,*,text='Full document',pixels=None,extra=None):
        import base64
        import json
        from types import SimpleNamespace
        from unittest.mock import Mock,patch
        from tg_triage.provider import CloudProvider
        from tg_triage.thinking_transport import complete
        encoded=pixels or base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'x'*60000).decode()
        messages=body(packet(text))['messages']
        messages[-1]['images']=[encoded]
        if provider!='ollama':
            messages=CloudProvider._cloud_messages(None,messages)
            if extra is not None:
                messages[-1]['content'].append(extra)
        envelope={'message':{'content':'{}'}} if provider=='ollama' else {'choices':[{'message':{'content':'{}'}}]}
        response=Mock(status=200,fp=SimpleNamespace(raw=SimpleNamespace(_sock=Mock())))
        response.isclosed.side_effect=[False,True];response.read1.return_value=json.dumps(envelope).encode()
        connection=Mock();connection.getresponse.return_value=response
        with patch('tg_triage.thinking_transport._addresses',return_value=['93.184.216.34']) as dns, \
             patch('tg_triage.thinking_transport.DeadlineHTTPS',return_value=connection), \
             patch('tg_triage.thinking_transport.http.client.HTTPConnection',return_value=connection), \
             patch('tg_triage.thinking_transport.threading.Timer'):
            try:
                result=complete(provider,'http://127.0.0.1:11434' if provider=='ollama' else 'https://api.deepseek.com',
                    'test',messages,{},key='fake',think=False)
                return result,connection,dns,encoded
            except Exception as error:
                return error,connection,dns,encoded

    def test_full_doc_and_large_valid_pixels_reach_mock_post_unchanged(self):
        import json
        for provider in ('deepseek','ollama'):
            with self.subTest(provider=provider):
                result,connection,dns,encoded=self._wire(provider)
                self.assertEqual({},result)
                payload=json.loads(connection.request.call_args.args[2])
                self.assertIn(encoded,connection.request.call_args.args[2].decode())
                self.assertIn('Full document',connection.request.call_args.args[2].decode())
                self.assertGreater(len(connection.request.call_args.args[2]),48*1024)

    def test_large_text_with_pixels_and_unknown_blocks_fail_before_network(self):
        for provider in ('deepseek','ollama'):
            result,connection,dns,_=self._wire(provider,text='x'*50000)
            self.assertEqual('input_too_large',result.code)
            connection.connect.assert_not_called();dns.assert_not_called()
        result,connection,dns,_=self._wire('deepseek',extra={'type':'unknown','text':'hidden'})
        self.assertEqual('input_too_large',result.code)
        connection.connect.assert_not_called();dns.assert_not_called()

    def test_total_image_caps_are_not_waived_by_document_marker(self):
        import base64
        for provider,size in (('deepseek',21*1024*1024),('ollama',76*1024*1024)):
            # Valid PNG signature with a payload beyond the existing serialized total bound.
            encoded=base64.b64encode(b'\x89PNG\r\n\x1a\n'+b'x'*((size*3)//4)).decode()
            result,connection,dns,_=self._wire(provider,pixels=encoded)
            self.assertEqual('input_too_large',result.code)
            connection.connect.assert_not_called();dns.assert_not_called()

    def test_malformed_image_payload_cannot_gain_text_budget_exemption(self):
        p=body(packet('Full document'))
        text=p['messages'][-1]['content']
        p['messages'][-1]['content']=[{'type':'text','text':text},
            {'type':'image_url','image_url':{'url':'https://outside.invalid/image.png'}}]
        self.assertTrue(document_reader.has_document_context(p['messages']))
        self.assertFalse(document_reader.request_fits(p))
        self.assertGreater(document_reader.wire_text_bytes(p),document_reader.MAX_REQUEST_BYTES)

    def test_diagnostic_projection_drops_malformed_types_and_untrusted_paths(self):
        from tg_triage.ingress_diagnostics import project,reject
        sentinel='private@example.invalid'
        rows=[None,{'code':[],'path':'root','actual_type':'object'},
            {'code':'type','path':'groups[0].'+sentinel,'actual_type':'string'},
            {'code':'type','path':'groups[99].analysis','actual_type':'string'},
            {'code':'type','path':'analysis.details.work','actual_type':{}},
            {'code':'type','path':'analysis.details.work','actual_type':'string','length':True,'value':sentinel}]
        clean=project(rows)
        self.assertEqual([{'code':'type','path':'analysis.details.work','actual_type':'string'}],clean)
        errors=[]
        for _ in range(20):
            reject(errors,'max_length','analysis.details.work','X'*500)
        self.assertEqual(8,len(errors))
        self.assertNotIn('X',json.dumps(errors))
        self.assertNotIn(sentinel,json.dumps(clean))

    def test_turn_validation_boundary_never_persists_extra_prose(self):
        from tg_triage.store import Store
        from tg_triage.thinking import Turn,init_store
        store=Store(':memory:');init_store(store);self.addCleanup(store.close)
        sentinel='private-content-never-log@example.invalid'
        turn=Turn(store,'ingress:-100:1','-100:1',1)
        turn.record('ingress_validation',{'raw':sentinel,'errors':[{'code':'type','path':'analysis.details.work',
            'actual_type':'string','value':sentinel}, {'code':'type','path':sentinel,'actual_type':'string'}]})
        dump='\n'.join(store.connection.iterdump())
        self.assertNotIn(sentinel,dump)
        self.assertIn('analysis.details.work',dump)
