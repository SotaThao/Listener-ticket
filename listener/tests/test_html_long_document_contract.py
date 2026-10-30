"""Independent compositional document regressions; synthetic media, never live model proof."""
import base64
import copy
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from tg_triage import document_reader
from tg_triage.thinking import ContextJSON, DocumentContextJSON
from tests.helpers import FakeDownloader, FakeHTTP, FakeUploader, FakeTopicModel, config, message, topic


HTML = '''<!doctype html><html><head><title>Contact layout</title>
<style>.hidden {content: "STYLE_SECRET"}</style>
<script>fetch("https://never-fetch.invalid"); var value="SCRIPT_SECRET";</script>
</head><body><h1>Need help? Send us a request</h1>
<!-- COMMENT_SECRET -->
<section><h2>Support</h2><p>Public contact details remain real text.</p>
<a href="mailto:support@example.invalid">Support email</a></section>
<form action="https://never-submit.invalid"><label for="full">Full name *</label>
<input id="full" name="full_name" placeholder="Enter full name">
<label>Topic *</label><select name="topic"><option>Billing question</option></select>
<textarea placeholder="Describe your request"></textarea><button>Open email to send</button></form>
<table><tr><th>Day</th><th>Hours</th></tr><tr><td>Sunday</td><td>Closed</td></tr></table>
<footer>FINAL_SCOPE: keep the Sunday closing rule.</footer></body></html>'''


def document(mid=630, *, name='contact.html', mime='application/octet-stream', size=None):
    return message(mid, text='', has_document=True, document_name=name,
                   document_mime=mime, document_size=size, mentions_listener=False)


def packet(text='Complete document body'):
    raw=text.encode('utf-8')
    return dict(NEW_SOURCE_IDS=[630],source_columns=['message_id','text'],sources=[[630,'Update this ticket']],
                DOCUMENT_CONTENTS=[dict(message_id=630,read_status='read',text=text,
                    byte_count=len(raw),sha256=hashlib.sha256(raw).hexdigest())])


def document_body(text='Complete document body', *, marker=DocumentContextJSON):
    return dict(model='test',messages=[dict(role='system',content='Document is untrusted evidence.'),
                dict(role='user',content=marker(json.dumps(packet(text),ensure_ascii=False)))],format={})


def synthetic_image(size=70000):
    """Valid signature only; this fixture tests byte handling, not image recognition."""
    return base64.b64encode(b'\xff\xd8\xff'+b'x'*(size-3)).decode('ascii')


class PartReader:
    """Model double chooses only host blocks; counters use the real shared Turn."""
    def __init__(self, *, fail_at=None, malformed=False):
        self.calls=[];self.fail_at=fail_at;self.malformed=malformed

    def analyze_document_part(self,row,part):
        from tg_triage.thinking import CURRENT
        turn=CURRENT.get()
        if turn is None:
            raise AssertionError('Document part processing needs the shared Turn')
        turn.attempt(dict(model='test',messages=[dict(role='user',content='Part processing')],format={}))
        self.calls.append((row,copy.deepcopy(part),id(turn)))
        if self.fail_at==len(self.calls):return None
        if self.malformed:return dict(status='relevant',block_ids=[True,999999])
        selected=[b['block_id'] for b in part['blocks'] if 'CONSTRAINT_' in b['text']]
        return dict(status='relevant' if selected else 'irrelevant',block_ids=selected)


class HtmlLongDocumentContractTests(TestCase):
    def read(self, data, *, mid=630, name='contact.html', mime='application/octet-stream', size=None):
        downloader=FakeDownloader({mid:(data,name)})
        rows=document_reader.read_documents([document(mid,name=name,mime=mime,size=size)],downloader)
        self.assertEqual(1,len(rows))
        self.assertTrue(all(not p.exists() for p in downloader.directories))
        return rows[0]

    @contextmanager
    def turn(self):
        from tg_triage.store import Store
        from tg_triage.thinking import CURRENT,Turn
        store=Store(':memory:')
        turn=Turn(store,'ingress:test:630','test:630',630,clock=lambda:0.0)
        token=CURRENT.set(turn)
        try:yield turn,store
        finally:CURRENT.reset(token);store.close()

    def long_row(self):
        text=('CONSTRAINT_BEGIN: implement the contact page.\n'+
              ('Thông tin tham khảo và kiểm tra độc lập. 😀\n'*1400)+
              '\nCONSTRAINT_END: preserve all seven routes and Sunday closing.')
        return self.read(text.encode('utf-8'),name='requirements.md',mime='text/markdown'),text

    def test_private_fallback_pixels_do_not_change_document_admission_in_each_phase(self):
        from tg_triage.ingress_model import CRITIC_SCHEMA,INGRESS_SCHEMA
        for phase in ('initial','repair','review'):
            with self.subTest(phase=phase):
                body=document_body();body['messages'][-1]['images']=[synthetic_image()]
                body['format']=copy.deepcopy(CRITIC_SCHEMA if phase=='review' else INGRESS_SCHEMA)
                if phase=='repair':
                    body['messages'][0]['content']+=' Host validation rejected missing task; return the valid schema.'
                if phase=='review':
                    data=json.loads(body['messages'][-1]['content'])
                    data['PROPOSED_ROUTES_FOR_REVIEW']=[dict(group_index=0,message_ids=[630],action='NEW',
                        candidate_id=None,confidence=.95,question='',analysis=dict(
                            work='Use the current document.',done=['Preserve all explicit constraints.']))]
                    body['messages'][-1]['content']=DocumentContextJSON(json.dumps(data))
                baseline=document_reader.request_fits(body)
                self.assertTrue(baseline)
                body['_local_messages']=copy.deepcopy(body['messages'])
                body['_local_messages'][-1]['images']=[synthetic_image(180000)]
                self.assertTrue(document_reader.request_fits(body))

    def test_wire_counter_is_invariant_to_private_fallback_and_private_metadata(self):
        body=document_body();body['messages'][-1]['images']=[synthetic_image()]
        expected=document_reader.wire_text_bytes(body)
        self.assertLess(expected,document_reader.MAX_REQUEST_BYTES)
        body['_local_messages']=copy.deepcopy(body['messages'])
        body['_private_metadata']='PRIVATE_SENTINEL'*12000
        self.assertEqual(expected,document_reader.wire_text_bytes(body))

    def test_untrusted_json_never_acquires_document_lane_from_a_private_marker(self):
        body=document_body('X'*12000,marker=ContextJSON)
        body['_local_messages']=document_body()['messages']
        self.assertFalse(document_reader.request_fits(body))

    def test_actual_large_text_is_still_rejected_even_with_tiny_private_fallback(self):
        body=document_body('X'*50000);body['_local_messages']=document_body('OK')['messages']
        self.assertFalse(document_reader.request_fits(body))

    def test_invalid_actual_cloud_image_part_is_not_hidden_by_private_valid_media(self):
        body=document_body();text=body['messages'][-1]['content']
        body['messages'][-1]['content']=[dict(type='text',text=text),
            dict(type='image_url',image_url=dict(url='https://outside.invalid/unsafe.png'))]
        body['_local_messages']=document_body()['messages']
        body['_local_messages'][-1]['images']=[synthetic_image()]
        self.assertFalse(document_reader.request_fits(body))
        self.assertGreater(document_reader.wire_text_bytes(body),document_reader.MAX_REQUEST_BYTES)

    def test_private_fields_do_not_mutate_or_disappear_from_caller_owned_body(self):
        body=document_body();body['_local_messages']=copy.deepcopy(body['messages'])
        body['_local_messages'][-1]['images']=[synthetic_image()]
        original=copy.deepcopy(body)
        document_reader.request_fits(body);document_reader.wire_text_bytes(body)
        self.assertEqual(original,body)

    def test_local_transport_posts_primary_document_and_pixels_without_private_fields(self):
        from tg_triage.classifier import Classifier
        body=document_body();encoded=synthetic_image()
        body['messages'][-1]['images']=[encoded]
        body['_local_messages']=copy.deepcopy(body['messages'])
        body['_local_messages'][0]['content']='PRIVATE_FALLBACK_SENTINEL'
        body['_local_messages'][-1]['images']=[synthetic_image(180000)]
        http=FakeHTTP(dict(message=dict(content='{}')))
        model=Classifier(config().ollama,opener=http)
        with self.turn() as (turn,store):
            self.assertEqual({},model._ask(body,json.loads,attempts=1))
            self.assertEqual(1,turn.calls)
        self.assertEqual(1,len(http.calls))
        posted=http.calls[0][0].data.decode()
        self.assertNotIn('_local_messages',posted)
        self.assertNotIn('PRIVATE_FALLBACK_SENTINEL',posted)
        self.assertIn('Complete document body',posted)
        self.assertIn(encoded,posted)

    def test_cloud_transport_cannot_receive_private_fallback_messages(self):
        from tg_triage.provider import CloudProvider
        from tg_triage.config import ModelConfig
        settings=replace(config(),model=ModelConfig(provider='deepseek',name='test',think=False,
            base_url='https://api.deepseek.com',cloud_consent=True,cloud_image_consent=True,fallback='none'))
        model=CloudProvider(settings)
        body=document_body();body['messages'][-1]['images']=[synthetic_image()]
        body['_local_messages']=copy.deepcopy(body['messages'])
        body['_local_messages'][0]['content']='PRIVATE_FALLBACK_SENTINEL'
        body['_local_messages'][-1]['images']=[synthetic_image(180000)]
        captured=[]
        def complete(provider,url,name,messages,schema,**kwargs):
            captured.append(copy.deepcopy(messages));return {}
        with patch('tg_triage.ai_transport.read_key',return_value='fake'), \
             patch('tg_triage.thinking_transport.complete',side_effect=complete):
            with self.turn() as (turn,store):
                self.assertEqual({},model._ask(body,json.loads,attempts=1))
                self.assertEqual(1,turn.calls)
        self.assertEqual(1,len(captured))
        self.assertNotIn('PRIVATE_FALLBACK_SENTINEL',json.dumps(captured))
        self.assertNotIn('_local_messages',json.dumps(captured))

    def test_fallback_payload_has_its_own_budget_after_primary_provider_failure(self):
        from tg_triage.provider import CloudProvider
        from tg_triage.config import ModelConfig
        from tg_triage.ai_transport import ProviderError
        settings=replace(config(),model=ModelConfig(provider='deepseek',name='test',think=False,
            base_url='https://api.deepseek.com',cloud_consent=True,cloud_image_consent=True,
            fallback='ollama',fallback_think=False))
        http=FakeHTTP(dict(message=dict(content='{}')))
        model=CloudProvider(settings,opener=http)
        body=document_body();body['_local_messages']=[dict(role='user',content='X'*9000)]
        with patch('tg_triage.ai_transport.read_key',return_value='fake'), \
             patch('tg_triage.thinking_transport.complete',side_effect=ProviderError('provider_failed')) as cloud:
            with self.turn() as (turn,store):
                self.assertIsNone(model._ask(body,json.loads,attempts=1))
                self.assertEqual(1,turn.calls)
                self.assertEqual('context_budget_exhausted',turn.reason)
        self.assertEqual(1,cloud.call_count)
        self.assertEqual([],http.calls)

    def test_html_octet_stream_and_declared_html_are_read_as_complete_static_text(self):
        raw=HTML.encode('utf-8')
        for mime in ('application/octet-stream','text/html'):
            with self.subTest(mime=mime):
                row=self.read(raw,mime=mime)
                self.assertEqual('read',row['read_status'])
                for fact in ('Need help? Send us a request','Full name','Enter full name',
                             'Billing question','Describe your request','Open email to send',
                             'Sunday','Closed','FINAL_SCOPE: keep the Sunday closing rule.'):
                    self.assertIn(fact,row['text'])
                self.assertIn('mailto:support@example.invalid',row['text'])
                self.assertEqual(len(raw),row['byte_count'])
                self.assertEqual(hashlib.sha256(raw).hexdigest(),row['sha256'])

    def test_html_scripts_styles_comments_are_not_document_facts_or_external_actions(self):
        with patch('urllib.request.urlopen',side_effect=AssertionError('HTML must not fetch')) as fetch, \
             patch('subprocess.run',side_effect=AssertionError('HTML must not execute')) as run:
            row=self.read(HTML.encode())
        self.assertEqual('read',row['read_status'])
        for hidden in ('SCRIPT_SECRET','STYLE_SECRET','COMMENT_SECRET','never-fetch.invalid'):
            self.assertNotIn(hidden,row['text'])
        fetch.assert_not_called();run.assert_not_called()

    def test_html_bom_hash_is_of_raw_bytes_not_extracted_visible_text(self):
        raw=b'\xef\xbb\xbf'+HTML.encode('utf-8')
        row=self.read(raw,name='CONTACT.HTM')
        self.assertEqual('read',row['read_status'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(),row['sha256'])
        self.assertNotEqual(hashlib.sha256(row['text'].encode()).hexdigest(),row['sha256'])
        self.assertNotIn('\ufeff',row['text'])

    def test_html_extension_does_not_override_conflicting_binary_mime(self):
        row=self.read(HTML.encode(),mime='application/pdf')
        self.assertNotEqual('read',row['read_status']);self.assertFalse(row.get('text'))

    def test_script_extension_disguised_as_html_is_never_read(self):
        row=self.read(HTML.encode(),name='payload.exe.html',mime='text/html')
        self.assertNotEqual('read',row['read_status']);self.assertFalse(row.get('text'))

    def test_html_invalid_utf8_or_binary_control_has_no_success_proof(self):
        for raw in (b'<html>bad\xff</html>',b'<html>bad\x00</html>'):
            with self.subTest(raw=raw):
                row=self.read(raw)
                self.assertNotEqual('read',row['read_status'])
                self.assertFalse(row.get('text'));self.assertFalse(row.get('sha256'))

    def test_large_html_style_scaffolding_does_not_discard_late_visible_requirement(self):
        raw=('<!doctype html><style>'+('/* CSS filler */'*2200)+
             '</style><h1>Contact</h1><p>LAST_REQUIREMENT: add the public support link.</p>').encode()
        self.assertGreater(len(raw),32768)
        row=self.read(raw,size=1)
        self.assertEqual('read',row['read_status'])
        self.assertIn('LAST_REQUIREMENT: add the public support link.',row['text'])
        self.assertNotIn('CSS filler',row['text'])
        self.assertEqual(len(raw),row['byte_count'])

    def test_long_markdown_download_is_complete_before_model_processing(self):
        text=('Yêu cầu được giữ đầy đủ.\n'*1800)+'END_CONSTRAINT: Preserve all seven routes.'
        raw=text.encode('utf-8');self.assertGreater(len(raw),32768)
        row=self.read(raw,name='requirements.md',mime='text/markdown',size=1)
        self.assertEqual('read',row['read_status'])
        self.assertEqual(text,row['text']);self.assertEqual(len(raw),row['byte_count'])
        self.assertEqual(hashlib.sha256(raw).hexdigest(),row['sha256'])

    def test_oversize_actual_file_cannot_use_underreported_telegram_size(self):
        raw=b'a'*(document_reader.MAX_BYTES+1)
        row=self.read(raw,name='requirements.md',mime='text/markdown',size=1)
        self.assertNotEqual('read',row['read_status']);self.assertFalse(row.get('text'))

    def test_html_download_path_escape_is_rejected_without_reading_external_file(self):
        with TemporaryDirectory() as directory:
            outside=Path(directory)/'outside.html';outside.write_text(HTML,encoding='utf-8')
            row=document_reader.read_documents([document()],lambda *args:outside)[0]
            self.assertNotEqual('read',row['read_status']);self.assertFalse(row.get('text'))
            self.assertTrue(outside.exists())

    def test_html_proof_cannot_be_reused_for_another_source(self):
        row=self.read(HTML.encode())
        self.assertEqual('read',row['read_status'])
        proof=document_reader.proof([row])
        self.assertTrue(document_reader.valid_proof(proof,[document()]))
        self.assertFalse(document_reader.valid_proof(proof,[document(631)]))

    def test_long_document_uses_multiple_parts_and_retains_late_required_scope(self):
        from tg_triage.document_processing import prepare
        row,text=self.long_row();model=PartReader()
        with self.turn() as (turn,store):
            rows=prepare([row],model)
            self.assertGreater(len(model.calls),2)
            self.assertLessEqual(len(model.calls),16)
            self.assertEqual(1,len({entry[2] for entry in model.calls}))
            self.assertEqual(len(model.calls),turn.calls)
            self.assertIn('CONSTRAINT_BEGIN: implement the contact page.',rows[0]['text'])
            self.assertIn('CONSTRAINT_END: preserve all seven routes and Sunday closing.',rows[0]['text'])
            self.assertNotEqual(text,rows[0]['text'])
            self.assertEqual('read',rows[0]['read_status'])
            self.assertTrue(document_reader.valid_proof(document_reader.proof(rows),
                [document(name='requirements.md',mime='text/markdown')]))

    def test_long_document_parts_cover_utf8_bytes_once_without_gaps_or_overlap(self):
        from tg_triage.document_processing import prepare
        row,text=self.long_row();model=PartReader()
        with self.turn():prepare([row],model)
        raw=text.encode('utf-8');next_byte=0
        for _,part,_ in model.calls:
            self.assertEqual(next_byte,part['start_byte'])
            self.assertGreater(part['end_byte'],part['start_byte'])
            chunk=raw[part['start_byte']:part['end_byte']]
            chunk.decode('utf-8')
            self.assertEqual(hashlib.sha256(chunk).hexdigest(),part['sha256'])
            next_byte=part['end_byte']
        self.assertEqual(len(raw),next_byte)

    def test_one_missing_long_document_part_invalidates_readiness_and_all_partial_quotes(self):
        from tg_triage.document_processing import prepare
        row,_=self.long_row();model=PartReader(fail_at=2)
        with self.turn():rows=prepare([row],model)
        self.assertEqual(2,len(model.calls))
        self.assertNotEqual('read',rows[0]['read_status'])
        self.assertFalse(rows[0].get('text'))
        self.assertFalse(document_reader.valid_proof(document_reader.proof(rows),
            [document(name='requirements.md',mime='text/markdown')]))

    def test_completed_processing_proof_cannot_omit_duplicate_or_change_covered_part(self):
        from tg_triage.document_processing import prepare
        row,text=self.long_row();model=PartReader()
        with self.turn():rows=prepare([row],model)
        original=document_reader.proof(rows)
        source=[document(name='requirements.md',mime='text/markdown')]
        self.assertTrue(document_reader.valid_proof(original,source))
        parts=original[0]['processing']['parts']
        self.assertGreater(len(parts),2)
        for bad_parts in (parts[:-1],parts+[parts[-1]],
                          [dict(parts[0],start_byte=1),*parts[1:]]):
            with self.subTest(parts=len(bad_parts)):
                bad=copy.deepcopy(original);bad[0]['processing']['parts']=bad_parts
                self.assertFalse(document_reader.valid_proof(bad,source))

    def test_processing_receipts_keep_original_extracted_text_hash_distinct_from_selection(self):
        from tg_triage.document_processing import prepare
        row,text=self.long_row();model=PartReader()
        with self.turn():rows=prepare([row],model)
        processed=rows[0];receipt=processed['processing']
        self.assertEqual('complete',receipt['status'])
        self.assertEqual(hashlib.sha256(text.encode()).hexdigest(),receipt['text_sha256'])
        self.assertEqual(len(text.encode()),receipt['text_byte_count'])
        self.assertEqual(hashlib.sha256(processed['text'].encode()).hexdigest(),receipt['selected_sha256'])
        self.assertNotEqual(receipt['text_sha256'],receipt['selected_sha256'])

    def test_part_selection_cannot_invent_foreign_block_authority(self):
        from tg_triage.document_processing import prepare
        row,_=self.long_row();model=PartReader(malformed=True)
        with self.turn():rows=prepare([row],model)
        self.assertNotEqual('read',rows[0]['read_status'])
        self.assertFalse(rows[0].get('text'))

    def test_long_document_budget_is_finite_shared_and_reserves_followup_calls(self):
        from tg_triage.document_processing import prepare
        row,_=self.long_row();model=PartReader()
        with self.turn() as (turn,store):
            prepare([row],model)
            recorded=turn.record('initial_result',{})
            maximum=recorded['budget']['max_model_calls']
            self.assertGreaterEqual(maximum,turn.calls+2)
            self.assertLessEqual(maximum,20)
            self.assertGreater(turn.deadline-turn.started,40)
            self.assertLessEqual(turn.deadline-turn.started,360)

    def test_short_text_keeps_two_call_forty_second_budget_and_avoids_chunk_model(self):
        from tg_triage.document_processing import prepare
        row=self.read(b'CONSTRAINT_SHORT: retain public support link.',name='request.md',mime='text/markdown')
        model=PartReader()
        with self.turn() as (turn,store):
            rows=prepare([row],model)
            self.assertEqual(row['text'],rows[0]['text'])
            self.assertEqual([],model.calls);self.assertEqual(0,turn.calls)
            self.assertEqual(40,turn.deadline-turn.started)
            self.assertEqual(2,turn.record('initial_result',{})['budget']['max_model_calls'])

    def test_full_html_reaches_actual_followup_serialized_prompt_with_own_source_id(self):
        from tg_triage.classifier import Classifier
        from tests.test_followups import answer
        row=self.read(HTML.encode())
        original=message(1,text='Add public support contact to the footer.')
        reply=replace(document(),reply_to_id=1,text='Bổ sung theo tài liệu này.')
        value=dict(message=dict(content=json.dumps(answer('update',summary='Preserve Sunday closing.'))))
        http=FakeHTTP(value)
        model=Classifier(config().ollama,opener=http)
        result=model.analyze_followup([original,reply],reply,
            [dict(ordinal=0,issue_number=101,message_ids=[1],scope='Footer contact')],[],document_contents=[row])
        self.assertIsNotNone(result);self.assertEqual(1,len(http.calls))
        body=json.loads(http.calls[0][0].data)
        rendered=json.dumps(body,ensure_ascii=False)
        self.assertIn('FINAL_SCOPE: keep the Sunday closing rule.',rendered)
        self.assertIn('DOCUMENT_CONTENTS',rendered)
        self.assertIn('630',rendered)

    def test_continue_html_facts_and_read_proof_precede_real_fake_github_comment(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.test_followups import GitHub,answer
        seen=[]
        class AwareModel(FakeTopicModel):
            def analyze_followup(self,originals,reply,tickets,history,**options):
                rows=options.get('document_contents',[])
                seen.append(copy.deepcopy(rows))
                if not rows or 'FINAL_SCOPE: keep the Sunday closing rule.' not in rows[0].get('text',''):
                    return None
                return answer('update',summary='FINAL_SCOPE: keep the Sunday closing rule.')
        store=Store(':memory:');self.addCleanup(store.close)
        gh=GitHub();sent=[];model=AwareModel((topic([1],verdict='needs_human'),))
        p=Pipeline(config('live'),store,model,gh,downloader=FakeDownloader({630:(HTML.encode(),'contact.html')}),
            uploader=FakeUploader(),notice_sender=lambda c,m,t:sent.append((m,t)) or 901)
        p.process(message(1,text='Add public contact to the footer.',sender_id=7))
        self.assertEqual(1,len(gh.calls))
        original_comment=gh.add_comment;proof_at_write=[]
        def before_comment(number,body):
            raw=store.connection.execute('SELECT plan FROM followup_jobs ORDER BY id DESC LIMIT 1').fetchone()[0]
            plan=json.loads(raw)
            proof=plan.get('host_document_reads') or plan.get('document_reads')
            self.assertIsInstance(proof,list)
            self.assertTrue(document_reader.valid_proof(proof,[replace(document(),reply_to_id=901)]))
            proof_at_write.append(proof)
            return original_comment(number,body)
        gh.add_comment=before_comment
        source=replace(document(),reply_to_id=901,sender_id=7,text='Bổ sung theo tài liệu HTML này.')
        p.process(source)
        self.assertEqual(1,len(seen));self.assertEqual([630],[r['message_id'] for r in seen[0]])
        self.assertEqual(1,len(proof_at_write));self.assertEqual(1,len(gh.comments))
        self.assertEqual(101,gh.comments[0][0])
        self.assertIn('FINAL_SCOPE: keep the Sunday closing rule.',gh.comments[0][1].replace('\\_','_'))
        p.process(source);p.close_due_cases()
        self.assertEqual(1,len(gh.comments));self.assertEqual(1,len(gh.calls))

    def test_failed_long_document_processing_blocks_pipeline_before_issue_creation(self):
        from tg_triage.pipeline import Pipeline
        from tg_triage.store import Store
        from tests.helpers import FakeGitHub
        row,text=self.long_row()
        class FailedModel(PartReader):
            def __init__(self):super().__init__(fail_at=1);self.routed=False
            def analyze_ingress(self,*args,**kwargs):self.routed=True;return None
        store=Store(':memory:');self.addCleanup(store.close)
        gh=FakeGitHub();model=FailedModel()
        p=Pipeline(replace(config('live'),autonomous_enabled=True),store,model,gh,
            downloader=FakeDownloader({630:(text.encode(),'requirements.md')}))
        source=document(name='requirements.md',mime='text/markdown')
        result=p.process(source)
        self.assertEqual('pending_review',result.decision)
        self.assertEqual([],gh.calls);self.assertFalse(model.routed)
        self.assertEqual(1,len(model.calls))

