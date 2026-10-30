"""Writer integration checks; synthetic text/pixels, never real-model semantic evidence."""
import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from tg_triage import document_reader,document_processing
from tg_triage.classifier import Classifier
from tg_triage.models import IncomingMessage
from tg_triage.provider import CloudProvider
from tg_triage.store import Store
from tg_triage.thinking import CURRENT,Turn,DocumentContextJSON
from tests.helpers import config,message,FakeDownloader,FakeHTTP
from tests.test_document_reader_safety import body,packet


class HTMLDocumentProcessingTests(TestCase):
    def with_turn(self):
        store=Store(':memory:');self.addCleanup(store.close)
        turn=Turn(store,'document:test','document:test',1)
        token=CURRENT.set(turn);self.addCleanup(CURRENT.reset,token)
        return turn

    def read(self,text,*,name='request.html',mime='application/octet-stream'):
        incoming=replace(message(1),has_document=True,document_name=name,document_mime=mime)
        data=text.encode()
        return incoming,document_reader.read_documents([incoming],FakeDownloader({1:(data,name)}))[0]

    def test_html_static_projection_preserves_form_constraints_and_skips_active_hidden_data(self):
        text='''<!doctype html><html><head><style>CSS_SENTINEL</style><script>fetch('PRIVATE_JS')</script></head>
        <body><h1>Contact us</h1><form><label>Message</label><textarea name="message" required minlength="10" maxlength="2000" placeholder="Describe request"></textarea>
        <input type="hidden" name="honeypot" placeholder="HIDDEN_SENTINEL"><select name="topic"><option>Support</option></select>
        <a href="/privacy-policy">Privacy</a><a href="javascript:alert('X')">Unsafe link</a>
        <div hidden>HIDDEN_CONTENT</div><!-- COMMENT_SENTINEL --></form></body></html>'''
        incoming,row=self.read(text)
        self.assertEqual('read',row['read_status'])
        for value in ('Contact us','Message','Describe request','minlength=10','maxlength=2000','required','Support','/privacy-policy'):
            self.assertIn(value,row['text'])
        for value in ('PRIVATE_JS','CSS_SENTINEL','HIDDEN_SENTINEL','HIDDEN_CONTENT','COMMENT_SENTINEL','javascript:'):
            self.assertNotIn(value,row['text'])
        self.assertNotEqual(row['sha256'],row['text_sha256'])
        self.assertEqual(len(text.encode()),row['byte_count'])
        self.assertTrue(document_reader.valid_proof(document_reader.proof([row]),[incoming]))

    def test_html_octet_requires_html_content_and_rejects_executable_name(self):
        for text,name in (('plain request','request.html'),('<html><p>Request</p></html>','request.exe.html')):
            with self.subTest(name=name):
                _,row=self.read(text,name=name)
                self.assertEqual('unsupported',row['read_status'])
                self.assertNotIn('text',row)
        from tg_triage.attachments import document_extension
        self.assertIsNone(document_extension(None,'application/octet-stream'))

    def test_tampered_processed_text_cannot_skip_document_analysis(self):
        self.with_turn()
        incoming,row=self.read('Requirement\n'*4000,name='request.md',mime='text/markdown')
        class Model:
            def analyze_document_part(self,row,part):
                return dict(status='relevant',block_ids=[part['blocks'][0]['block_id']])
        result=document_processing.prepare([row],Model())
        self.assertEqual('read',result[0]['read_status'])
        result[0]['text']+='INVENTED_TEXT'
        blocked=document_processing.prepare(result,Model())
        self.assertEqual('processing_failed',blocked[0]['read_status'])
        self.assertNotIn('text',blocked[0])

    def test_html_proof_requires_complete_matching_static_metadata_while_legacy_md_is_compatible(self):
        html=replace(message(1),has_document=True,document_name='request.html',document_mime='text/html')
        md=replace(html,document_name='request.md',document_mime='text/markdown')
        legacy=dict(message_id=1,read_status='read',byte_count=100,sha256='a'*64)
        self.assertTrue(document_reader.valid_proof([legacy],[md]))
        self.assertFalse(document_reader.valid_proof([legacy],[html]))
        valid=dict(legacy,extraction_kind='html_static_v1',text_byte_count=20,text_sha256='b'*64)
        self.assertTrue(document_reader.valid_proof([valid],[html]))
        self.assertFalse(document_reader.valid_proof([valid],[md]))
        self.assertFalse(document_reader.valid_proof([dict(legacy,byte_count=200*1024)],[md]))
        originals=[replace(md,message_id=i) for i in (1,2,3)]
        legacy_rows=[dict(legacy,message_id=i,byte_count=16*1024) for i in (1,2,3)]
        self.assertFalse(document_reader.valid_proof(legacy_rows,originals))
        for source in (html,md):
            for extra in (dict(text_byte_count='bad',text_sha256='not-a-hash'),dict(text_byte_count=20),
                          dict(extraction_kind='html_static_v1'),dict(processing={})):
                with self.subTest(source=source.document_name,extra=extra):
                    self.assertFalse(document_reader.valid_proof([dict(legacy,**extra)],[source]))

    def test_wire_budget_ignores_private_fallback_and_never_serializes_it(self):
        image=base64.b64encode(b'\xff\xd8\xff'+b'x'*65000).decode()
        request=body(packet('Actual document'))
        request['messages'][-1]['images']=[image]
        request['_local_messages']=[dict(role='user',content='PRIVATE_LOCAL',images=[image])]
        self.assertTrue(document_reader.request_fits(request))
        stripped=document_reader.wire_body(request)
        self.assertNotIn('_local_messages',stripped)
        self.assertEqual(document_reader.wire_text_bytes(request),document_reader.wire_text_bytes(stripped))
        self.assertNotIn('PRIVATE_LOCAL',json.dumps(stripped))

    def test_long_utf8_parts_are_lossless_and_late_requirement_reaches_final_context(self):
        turn=self.with_turn()
        text=('Thông tin tham khảo không yêu cầu sửa.\n'*1300)+'FINAL-REQUIREMENT: Preserve seven routes and Sunday hours.\n'
        incoming,row=self.read(text,name='request.md',mime='text/markdown')
        self.assertEqual('read',row['read_status'])
        calls=[]
        class Model:
            def analyze_document_part(self,row,part):
                calls.append(part)
                turn.attempt(dict(messages=[dict(role='user',content='bounded')],format={}))
                ids=[b['block_id'] for b in part['blocks'] if 'FINAL-REQUIREMENT' in b['text']]
                return dict(status='relevant' if ids else 'irrelevant',block_ids=ids)
        rows=document_processing.prepare([row],Model())
        processed=rows[0]
        self.assertGreater(len(calls),1)
        self.assertEqual(len(calls),turn.calls)
        self.assertEqual(len(calls)+4,turn.max_calls)
        self.assertIn('FINAL-REQUIREMENT',processed['text'])
        self.assertEqual(text.encode(),b''.join(''.join(b['text'] for b in p['blocks']).encode() for p in calls))
        self.assertTrue(document_reader.valid_proof(document_reader.proof(rows),[incoming]))
        self.assertEqual(len(text.encode()),processed['processing']['parts'][-1]['end_byte'])
        self.assertEqual(rows,document_processing.prepare(rows,Model()))
        self.assertEqual(len(calls),turn.calls)

    def test_failed_part_is_atomic_not_partial_usable_evidence(self):
        self.with_turn()
        incoming,row=self.read('Requirement\n'*4000,name='request.md',mime='text/markdown')
        count=[]
        class Model:
            def analyze_document_part(self,row,part):
                count.append(part)
                return None if len(count)==2 else dict(status='relevant',block_ids=[part['blocks'][0]['block_id']])
        result=document_processing.prepare([row],Model())
        self.assertEqual(2,len(count))
        self.assertEqual('processing_failed',result[0]['read_status'])
        self.assertNotIn('text',result[0])
        self.assertFalse(document_reader.valid_proof(document_reader.proof(result),[incoming]))

    def test_real_classifier_part_wire_reads_all_blocks_and_selects_only_valid_whole_blocks(self):
        turn=self.with_turn()
        incoming,row=self.read('Reference paragraph\n'*1700+'LAST_CONSTRAINT\n',name='request.md',mime='text/markdown')
        class HTTP:
            def __init__(self):self.calls=[]
            def __call__(self,request,timeout):
                from io import BytesIO
                self.calls.append(request)
                data=json.loads(request.data)
                packet=json.loads(data['messages'][-1]['content'])
                blocks=json.loads(packet['DOCUMENT_CONTENTS'][0]['text'])
                ids=[b['block_id'] for b in blocks if 'LAST_CONSTRAINT' in b['text']]
                reply=dict(status='relevant' if ids else 'irrelevant',block_ids=ids)
                return BytesIO(json.dumps(dict(message=dict(content=json.dumps(reply)))).encode())
        http=HTTP();model=Classifier(config().ollama,opener=http)
        rows=document_processing.prepare([row],model)
        self.assertGreater(len(http.calls),1)
        self.assertEqual(len(http.calls),turn.calls)
        self.assertIn('LAST_CONSTRAINT',rows[0]['text'])
        self.assertTrue(document_reader.valid_proof(document_reader.proof(rows),[incoming]))

    def test_followup_document_lane_carries_current_full_html_projection_and_bound_ids(self):
        incoming,row=self.read('<html><h1>Contact form</h1><textarea required minlength="10" maxlength="2000"></textarea></html>')
        response=dict(message=dict(content=json.dumps(dict(action='chat',confidence=.95,target_ordinal=None,summary='',questions=[],groups=[]))))
        http=FakeHTTP(response);model=Classifier(config().ollama,opener=http)
        old=dict(type='task',title='UNSUPPORTED_OLD_TASK',details=dict(work='OLD_WORK',scope_out='EXCLUDE_CURRENT_DOCUMENT',done=['OLD_DONE']))
        ticket=dict(ordinal=0,issue_number=70,message_ids=[10],confirmed_context=dict(bindings={'10':0},managed_snapshot=dict(analysis=old)))
        model.analyze_followup([message(10,text='Please review this work.')],incoming,[ticket],[],document_contents=[row])
        self.assertEqual(1,len(http.calls))
        raw=json.loads(http.calls[0][0].data)
        p=json.loads(raw['messages'][-1]['content'])
        self.assertEqual([incoming.message_id],p['DOCUMENT_SOURCE_IDS'])
        self.assertEqual(row['text'],p['DOCUMENT_CONTENTS'][0]['text'])
        self.assertIn('maxlength=2000',p['DOCUMENT_CONTENTS'][0]['text'])
        self.assertEqual({'10':0},p['HUMAN_BINDINGS']['bindings'])
        self.assertEqual(old,p['CURRENT_TICKET_MODEL_HYPOTHESIS'])
        guidance=raw['messages'][0]['content']
        for rule in ('unverified old BOT draft, NOT a human request/confirmed scope',
                'derive current work FIRST from REPLY+full document/excerpts+pixels',
                'Replace contradicted old title/summary/work/scope_in/scope_out/done',
                'Omitted detail keys RETAIN old values',
                'explicitly send empty string/list to clear unsupported old fields',
                'Work=one objective<=200 chars','NONEMPTY details.done required',
                'Task category=type=task (feature belongs to feedback)',
                'Task facts ONLY in details work,reason_context,scope_in,scope_out,done,links,due',
                'never feedback context/acceptance/problem aliases','Known task=>missing_info=[]'):
            self.assertIn(rule.casefold(),guidance.casefold())

    def test_exact_reply_with_current_html_and_photo_fits_real_ingress_schema(self):
        # Incident shape, different IDs: no target-specific exception.
        from tests.test_ingress_packing import envelope
        from tests.helpers import JPG
        from tg_triage.ingress_model import INGRESS_SCHEMA
        incoming,row=self.read('<html><h1>Contact us</h1><label>Request</label><textarea required minlength="10" maxlength="2000"></textarea></html>')
        current=[message(901,reply_to_id=801,text='Đọc nội dung đầy đủ file HTML và ảnh này để cập nhật yêu cầu Contact Us vào ticket này.'),
            replace(incoming,message_id=902,is_forward=True),message(903,text='',has_image=True,is_forward=True)]
        row=dict(row,message_id=902)
        candidate=dict(candidate_id='c1',pending=False,exact_reply=True,reply_source_ids=[901],split=False,current_scopes=[],
            pending_questions=[],clarification_outcomes=[],messages=[
                message(801,text='https://github.com/example/project/issues/2244'),
                message(802,text='@teammate chút check làm ticket này nha e')])
        reply=envelope(901,'c1');group=json.loads(reply['message']['content'])['groups'][0]
        group['message_ids']=[901,902,903];group['analysis']['key_message_ids']=[901,902,903]
        reply['message']['content']=json.dumps(dict(groups=[group]))
        http=FakeHTTP(reply);model=Classifier(config().ollama,opener=http)
        host_bodies=[];ask=model._ask
        def observe(body,*args,**kwargs):
            host_bodies.append(body);return ask(body,*args,**kwargs)
        model._ask=observe
        with TemporaryDirectory() as folder:
            image=Path(folder)/'proof.jpg';image.write_bytes(JPG)
            answer=model.analyze_ingress(current,[candidate],images={903:image},document_contents=[row])
        self.assertTrue(http.calls,model.last_ingress_outcome)
        self.assertEqual('CONTINUE',answer[0]['action'])
        self.assertEqual('c1',answer[0]['candidate_id'])
        body=json.loads(http.calls[0][0].data);packet=json.loads(body['messages'][1]['content'])
        self.assertEqual(INGRESS_SCHEMA,body['format'])
        guidance=body['messages'][0]['content']
        for rule in ('Route FIRST','explicit “update/correct the ticket I replied to”',
                '=>CONTINUE of that exact_reply candidate via reply_source_ids',
                'even if old bot draft is wrong or original text terse','independent fix=>NEW',
                'Reply alone≠match','Amend HOST ticket,not NEW tracking;URL never chooses destination',
                'work=ONE objective<=200 chars,not copied spec'):
            self.assertIn(rule.casefold(),guidance.casefold())
        self.assertNotIn('Changes/defects=>NEW;ONLY',guidance)
        self.assertNotIn('forwarded requirements=>NEW',guidance)
        self.assertLess(guidance.index('Route FIRST'),guidance.index('=>NEW'))
        self.assertLessEqual(model.last_ingress_context['text_bytes'],8000)
        self.assertGreaterEqual(model.last_ingress_context['new_text_cap'],30)
        self.assertGreaterEqual(model.last_ingress_context['candidate_text_cap'],30)
        self.assertEqual([901,902,903],packet['NEW_SOURCE_IDS'])
        self.assertEqual(801,packet['sources'][0][1])
        self.assertEqual('c1',packet['candidates'][0]['candidate_id'])
        self.assertTrue(packet['candidates'][0]['exact_reply'])
        self.assertEqual([801,802],[source[0] for source in packet['candidates'][0]['sources']])
        self.assertEqual([row],packet['DOCUMENT_CONTENTS'])
        self.assertEqual([dict(image_index=0,message_id=903)],packet['images_order'])
        self.assertEqual(1,len(body['messages'][1]['images']))
        self.assertTrue(document_reader.request_fits(host_bodies[0]))

    def test_followup_near_base_limit_reserves_document_source_ids_before_packing(self):
        self.with_turn()
        incoming,row=self.read('<html><h1>Contact</h1><p>Keep every required form constraint.</p></html>')
        reply=message(100,reply_to_id=10,text='Bổ sung yêu cầu theo tài liệu. '*100)
        originals=[replace(incoming,text='Original requirement. '*100)]+[
            message(i,text='Historical source context. '*100) for i in range(10,17)]
        ticket=dict(ordinal=0,issue_number=91,message_ids=[10],lifecycle_state=dict(state='open'),
            confirmed_context=dict(managed_snapshot=dict(analysis=dict(summary='Previous draft. '*100))))
        response=dict(message=dict(content=json.dumps(dict(action='chat',confidence=.95,target_ordinal=None,
            summary='',questions=[],groups=[]))))
        http=FakeHTTP(response);model=Classifier(config().ollama,opener=http)
        host_bodies=[];ask=model._ask
        def observe(body,*args,**kwargs):
            host_bodies.append(body);return ask(body,*args,**kwargs)
        model._ask=observe
        answer=model.analyze_followup(originals,reply,[ticket],[],document_contents=[row])
        self.assertIsNotNone(answer,model.last_followup_outcome)
        self.assertEqual(1,len(http.calls))
        self.assertTrue(document_reader.request_fits(host_bodies[0]))
        body=json.loads(http.calls[0][0].data);packet=json.loads(body['messages'][1]['content'])
        self.assertEqual([incoming.message_id],packet['DOCUMENT_SOURCE_IDS'])
        self.assertEqual([row],packet['DOCUMENT_CONTENTS'])
        self.assertEqual([91],[t['issue_number'] for t in packet['TRUSTED_TICKETS']])
        self.assertEqual(10,packet['REPLY']['reply_to_id'])
        self.assertTrue(any(source['text_truncated'] for source in packet['ORIGINAL_METADATA']))
        from tg_triage.thinking import ContextJSON,request_text_bytes
        packet.pop('DOCUMENT_CONTENTS')
        body['messages'][1]['content']=ContextJSON(json.dumps(packet,ensure_ascii=False,separators=(',',':')))
        self.assertLessEqual(request_text_bytes(body),8000)
        self.assertGreater(request_text_bytes(body),7900)

    def test_technical_followup_budget_failure_preserves_context_without_ticket_count_question(self):
        from tests.test_followups import FollowupTests
        helper=FollowupTests();self.addCleanup(helper.doCleanups)
        store,model,gh,sent,pipeline=helper.setup_case()
        model.value=None;model.last_followup_outcome='document_context_budget_exhausted'
        before=len(gh.calls)
        helper.reply(pipeline,text='Cập nhật yêu cầu vào ticket này.')
        self.assertEqual(before,len(gh.calls));self.assertEqual([],gh.comments)
        text=sent[-1][2]
        self.assertIn('đã lưu nội dung',text)
        self.assertIn('chưa cập nhật được ticket',text)
        self.assertNotIn('bao nhiêu',text);self.assertNotIn('xác nhận thêm',text)
        plan=json.loads(store.connection.execute('SELECT plan FROM followup_jobs ORDER BY id DESC LIMIT 1').fetchone()[0])
        self.assertEqual('document_context_budget_exhausted',plan['context_reason'])
        self.assertEqual([],plan['questions'])

    def test_document_provider_failure_keeps_read_proof_and_sends_no_scope_question(self):
        from tests.test_followups import FollowupTests
        for outcome in ('invalid_output','provider_unavailable'):
            with self.subTest(outcome=outcome):
                helper=FollowupTests();self.addCleanup(helper.doCleanups)
                store,model,gh,sent,pipeline=helper.setup_case()
                model.value=None;model.last_followup_outcome=outcome
                html=b'<html><h1>Contact</h1><p>Required form constraints.</p></html>'
                pipeline.attachments.downloader=FakeDownloader({21:(html,'contact.html')})
                incoming=replace(message(21,reply_to_id=901,text='Bổ sung ticket này theo tài liệu.'),
                    has_document=True,document_name='contact.html',document_mime='text/html')
                pipeline.process(incoming)
                self.assertEqual(1,len(gh.calls));self.assertEqual([],gh.comments)
                text=sent[-1][2]
                self.assertIn('chưa cập nhật được ticket',text)
                self.assertNotIn('bao nhiêu',text);self.assertNotIn('xác nhận thêm',text)
                plan=json.loads(store.connection.execute('SELECT plan FROM followup_jobs ORDER BY id DESC LIMIT 1').fetchone()[0])
                self.assertEqual(outcome,plan['context_reason']);self.assertEqual([],plan['questions'])
                self.assertTrue(document_reader.valid_proof(plan['host_document_reads'],[incoming]))

    def test_document_followup_task_parser_requires_work_and_nonempty_done_without_aliases(self):
        from tests.test_followups import answer
        from tests.test_followup_enrichment import projection
        incoming,row=self.read('<html><h1>Profile editor</h1><input name="display_name" required></html>')
        good=dict(work='Update the profile editor.',done=['Required display name is preserved.'],context='',acceptance=[])
        cases=[dict(work=good['work'],done=[],acceptance=['Wrong alias']),
            dict(work=good['work'],done=[]),dict(done=good['done']),
            dict(good,context='Wrong task alias'),dict(good,acceptance=['Wrong task alias']),good]
        for details in cases:
            with self.subTest(details=details):
                analysis=projection(category='task',type='task',key_message_ids=[incoming.message_id],details=details)
                response=dict(message=dict(content=json.dumps(answer('update',case_analysis=analysis))))
                http=FakeHTTP(response);model=Classifier(config().ollama,opener=http)
                result=model.analyze_followup([incoming],incoming,[dict(ordinal=0,issue_number=91,message_ids=[1])],[],document_contents=[row])
                self.assertEqual(details is good,result is not None)
                self.assertEqual(1,len(http.calls))
                if details is not good:self.assertEqual('invalid_output',model.last_followup_outcome)

    def test_document_task_empty_or_aliased_criteria_stop_before_any_github_effect(self):
        from tests.test_followups import FollowupTests,answer
        from tests.test_followup_enrichment import projection
        for details in (dict(work='Update profile.',done=[]),
                dict(work='Update profile.',done=[],acceptance=['Required display name']),
                dict(work='Update profile.',done=['Required display name'],context='Misplaced task context')):
            with self.subTest(details=details):
                helper=FollowupTests();self.addCleanup(helper.doCleanups)
                store,model,gh,sent,pipeline=helper.setup_case()
                model.value=answer('update',case_analysis=projection(category='task',type='task',key_message_ids=[21],details=details))
                html=b'<html><h1>Profile editor</h1><input name="display_name" required></html>'
                pipeline.attachments.downloader=FakeDownloader({21:(html,'profile.html')})
                incoming=replace(message(21,reply_to_id=901,text='Update this ticket from the document.'),
                    has_document=True,document_name='profile.html',document_mime='text/html')
                pipeline.process(incoming)
                self.assertEqual(1,len(gh.calls));self.assertEqual([],gh.comments)
                plan=json.loads(store.connection.execute('SELECT plan FROM followup_jobs ORDER BY id DESC LIMIT 1').fetchone()[0])
                self.assertEqual('invalid_output',plan['context_reason']);self.assertEqual([],plan['questions'])
                self.assertTrue(document_reader.valid_proof(plan['host_document_reads'],[incoming]))
                self.assertNotIn('bao nhiêu',sent[-1][2])

    def test_document_followup_task_feature_taxonomy_mismatch_stays_rejected(self):
        from tests.test_followups import answer
        from tests.test_followup_enrichment import projection
        incoming,row=self.read('<html><h1>Profile editor</h1><input name="display_name" required></html>')
        details=dict(work='Update the profile editor.',done=['Required display name is preserved.'])
        analysis=projection(category='task',type='feature',key_message_ids=[1],details=details)
        response=dict(message=dict(content=json.dumps(answer('update',case_analysis=analysis))))
        http=FakeHTTP(response);model=Classifier(config().ollama,opener=http)
        result=model.analyze_followup([incoming],incoming,[dict(ordinal=0,issue_number=91,message_ids=[1])],[],document_contents=[row])
        self.assertIsNone(result);self.assertEqual('invalid_output',model.last_followup_outcome)
        self.assertEqual(1,len(http.calls))

    def test_normal_turn_keeps_two_calls_and_cannot_extend_from_untrusted_request(self):
        turn=self.with_turn()
        self.assertEqual(2,turn.max_calls)
        turn.attempt(dict(messages=[dict(role='user',content='normal')],format={}))
        self.assertFalse(turn.configure_document_work(2,((1,'x','y'),)))
        self.assertEqual(2,turn.max_calls)
