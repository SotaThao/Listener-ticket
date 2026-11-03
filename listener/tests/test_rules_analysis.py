"""Actual provider requests with fake HTTP; semantic fixture verdicts are not model-quality tests."""
import base64
import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tg_triage.classifier import Classifier
from tg_triage.decision_rules import RulesSnapshot
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tg_triage.thinking import CURRENT, Turn
from tg_triage.rules_intake import evaluate_batch
from tg_triage import rules_analysis
from tests.helpers import config,message,FakeHTTP,FakeGitHub,PNG
from tests import test_rules_extensions as fixtures
ROOT=fixtures.ROOT
PNG=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a4x8AAAAASUVORK5CYII=")

TASK=dict(title='Kiểm tra phần bị che',work='Phân tích nguyên nhân hiển thị thiếu',acceptance=['Báo vị trí nội dung bị che'])
TEXT='Nhờ phân tích ảnh và báo vị trí nội dung bị che trong bảng báo cáo.'


def generated(ids=(42,),task=None):
    return dict(groups=[dict(message_ids=[mid],facts=dict(intent=dict(value='analyze',source_ids=[mid]),task=task or TASK)) for mid in ids])


def verdict(ids=(42,),mode='text',quote=TEXT,approve=True):
    return dict(reviews=[dict(group_index=i,verdict='approve' if approve else 'reject',claims=[
        dict(field=field,evidence=[dict(source_id=mid,modality=mode,quote=quote)])
        for field in ('title','work','acceptance/0')]) for i,mid in enumerate(ids)])


def envelope(value):return dict(message=dict(content=json.dumps(value,ensure_ascii=False)))


class RulesAnalysisTests(unittest.TestCase):
    assert_wire_schema=fixtures.ExtensionRulesTests.assert_wire_schema
    def setUp(self):
        fixtures.ExtensionRulesTests.setUp(self)
        self.addCleanup(patch.stopall)
        patch("socket.socket.connect",side_effect=AssertionError("unexpected network")).start()
        patch("subprocess.Popen",side_effect=AssertionError("unexpected subprocess")).start()
        for name in ('states','facts','readiness','questions','channels','examples'):
            (self.directory/(name+'.md')).write_text((ROOT/(name+'.md')).read_text(encoding='utf8'),encoding='utf8')
        self.snapshot=RulesSnapshot(self.directory,ROOT.parent/'templates'/'github-ticket',extensions=True)
        self.store=Store(':memory:');self.addCleanup(self.store.close)

    def run_review(self,values,sources=None,images=None,documents=None,context=()):
        http=FakeHTTP(*(envelope(v) for v in values));provider=Classifier(config().ollama,opener=http)
        turn=Turn(self.store,'unit','unit',42);token=CURRENT.set(turn)
        try:result=evaluate_batch(self.snapshot,provider,sources or [message(42,text=TEXT)],images=images,documents=documents,read_context=context)
        finally:CURRENT.reset(token)
        return result,http,turn

    def test_semantic_paraphrase_uses_two_real_provider_requests_and_full_coverage(self):
        result,http,turn=self.run_review([generated(),verdict()])
        self.assertEqual('ANALYZE',result[0][1].state)
        self.assertEqual(2,turn.calls);self.assertEqual(2,len(http.calls))
        first,second=[json.loads(r.data) for r,_ in http.calls]
        self.assertEqual(first['messages'][1],second['messages'][1])
        self.assertNotIn('_evidence_view',first)
        self.assertFalse(second['format']['additionalProperties'])
        self.assert_wire_schema(generated(),first['format'])
        self.assert_wire_schema(verdict(),second['format'])
        self.assertEqual(TASK,result[0][1].analysis_review['task'])
        self.assertNotIn(TASK['title'],TEXT)

    def test_all_analysis_groups_use_one_batched_critic(self):
        result,http,turn=self.run_review([generated((42,43)),verdict((42,43))],sources=[message(42,text=TEXT),message(43,text=TEXT)])
        self.assertEqual(2,len(result));self.assertEqual(2,len(http.calls));self.assertEqual(2,turn.calls)
        for _,decision in result:self.assertIsNotNone(decision.analysis_review)

    def test_real_pixels_are_identical_in_both_requests(self):
        path=self.directory/'view.png';path.write_bytes(PNG)
        result,http,_=self.run_review([generated(),verdict(mode='pixels',quote='')],sources=[message(42,text=TEXT,has_image=True)],images={42:path})
        self.assertIsNotNone(result)
        bodies=[json.loads(r.data) for r,_ in http.calls]
        for body in bodies:self.assertEqual([base64.b64encode(PNG).decode()],body['messages'][1]['images'])
        self.assertEqual(hashlib.sha256(PNG).hexdigest(),result[0][1].analysis_review['pixels']['42'])

    def test_rejected_semantics_and_missing_or_foreign_claims_hold(self):
        values=[]
        cases=[
            ('contradiction','Nhờ phân tích lỗi; không được xóa dữ liệu.',dict(title='Xóa dữ liệu',work='Xóa toàn bộ dữ liệu để sửa lỗi',acceptance=['Dữ liệu đã được xóa'])),
            ('wrong_speaker','Lan hỏi ý kiến; Minh chưa nhận thực hiện.',dict(title='Giao Minh thực hiện',work='Minh triển khai thay đổi đã duyệt',acceptance=['Minh hoàn thành thay đổi'])),
            ('fabricated_diagnosis',TEXT,dict(title='Sửa Redis',work='Khắc phục Redis gây lỗi báo cáo',acceptance=['Redis đã hết lỗi']))]
        for why,text,task in cases:
            with self.subTest(semantic_fixture=why):
                result,http,_=self.run_review([generated(task=task),verdict(approve=False)],sources=[message(42,text=text)])
                self.assertIsNone(result);self.assertEqual(2,len(http.calls))
        missing=verdict();missing['reviews'][0]['claims'].pop();values.append(missing)
        foreign=verdict();foreign['reviews'][0]['claims'][0]['evidence'][0]['source_id']=99;values.append(foreign)
        quote=verdict();quote['reviews'][0]['claims'][0]['evidence'][0]['quote']='invented';values.append(quote)
        extra=verdict();extra['reviews'][0]['permission']='create';values.append(extra)
        duplicate=verdict();duplicate['reviews']*=2;values.append(duplicate)
        absent=verdict();absent['reviews']=[];values.append(absent)
        for value in values:
            with self.subTest(value=value):self.assertIsNone(self.run_review([generated(),value])[0])

    def test_reply_context_allowed_only_on_actual_reply(self):
        context=message(10,text='Bảng báo cáo bị che phần tổng số')
        value=verdict(quote=context.text)
        for claim in value['reviews'][0]['claims']:claim['evidence'][0]['source_id']=10
        for reply,ok in ((10,True),(None,False)):
            with self.subTest(reply=reply):
                result,_,_=self.run_review([generated(),value],sources=[message(42,text=TEXT,reply_to_id=reply)],context=[context])
                self.assertEqual(ok,result is not None)

    def test_document_packet_ownership_and_short_supplement(self):
        data=('Tài liệu về bảng báo cáo. '*400).encode();text=data.decode()
        row=dict(message_id=42,name='proof.txt',read_status='read',byte_count=len(data),sha256=hashlib.sha256(data).hexdigest(),text=text)
        result,http,_=self.run_review([generated(),verdict(mode='document',quote='Tài liệu về bảng báo cáo.')],
            sources=[message(42,text=TEXT,has_document=True,document_name='proof.txt',document_mime='text/plain')],documents=[row])
        self.assertIsNotNone(result);self.assertEqual(2,len(http.calls))
        for request,_ in http.calls:
            packet=json.loads(json.loads(request.data)['messages'][1]['content'])
            self.assertEqual([42],packet['DOCUMENT_SOURCE_IDS']);self.assertEqual(text,packet['DOCUMENT_CONTENTS'][0]['text'])

    def test_budget_exhausted_before_critic_never_outputs(self):
        http=FakeHTTP(envelope(generated()),envelope(verdict()))
        provider=Classifier(config().ollama,opener=http);turn=Turn(self.store,'spent','spent',42);turn.calls=1
        token=CURRENT.set(turn)
        try:result=evaluate_batch(self.snapshot,provider,[message(42,text=TEXT)])
        finally:CURRENT.reset(token)
        self.assertIsNone(result);self.assertEqual(2,turn.calls);self.assertEqual(1,len(http.calls))

    def pipeline(self,values,sources=None,**options):
        http=FakeHTTP(*(envelope(v) for v in values));provider=Classifier(config().ollama,opener=http)
        sent=[];base=config('live')
        settings=replace(base,decision_rules_enabled=True,thinking_enabled=False,autonomous_enabled=True,
            telegram=replace(base.telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,self.store,provider,FakeGitHub(),rules_snapshot=self.snapshot,notice_sender=lambda *args:sent.append(args) or (900+len(sent)),**options)
        return pipeline,http,sent

    def test_thinking_disabled_still_bounds_turn_and_persists_review(self):
        p,http,sent=self.pipeline([generated(),verdict()])
        result=p.process(message(42,text=TEXT))
        self.assertEqual('semantic_analyze',result.reason);self.assertEqual(2,len(http.calls));self.assertEqual(1,len(sent))
        self.assertTrue(sent[0][2].startswith('Việc:'));self.assertNotIn('Trích nguồn',sent[0][2])
        self.assertEqual(1,self.store.connection.execute('SELECT count(*) FROM rules_analysis_reviews').fetchone()[0])

    def test_receipt_and_plan_tamper_do_not_validate(self):
        p,http,sent=self.pipeline([generated(),verdict()]);p.process(message(42,text=TEXT))
        row=self.store.connection.execute('SELECT plan FROM rules_batch_groups').fetchone();plan=json.loads(row[0])
        self.assertTrue(rules_analysis.validate(p,plan))
        for field,value in (('task',dict(TASK,title='Xóa dữ liệu')),('source_keys',[[message().chat_id,43]])):
            changed=dict(plan);changed[field]=value
            self.assertFalse(rules_analysis.validate(p,changed))
        with self.store.connection:self.store.connection.execute("UPDATE messages SET text='changed' WHERE message_id=42")
        self.assertFalse(rules_analysis.validate(p,plan))
        self.assertEqual(2,len(http.calls));self.assertEqual(1,len(sent))

    def test_source_change_during_review_holds_without_notice(self):
        p,http,sent=self.pipeline([generated(),verdict()])
        original=http.__call__
        class MutatingHTTP:
            def __call__(inner,request,timeout):
                if len(http.calls)==1:
                    with self.store.connection:self.store.connection.execute("UPDATE messages SET text='changed during critic' WHERE message_id=42")
                return original(request,timeout=timeout)
        p.classifier.opener=MutatingHTTP()
        result=p.process(message(42,text=TEXT))
        self.assertEqual('pending_review',result.decision);self.assertEqual([],sent)


    def test_long_document_existing_allowance_counts_parts_generation_and_critic(self):
        from tg_triage.document_processing import parts,prepare
        data=('Nội dung bảng báo cáo cần phân tích.\n'*850).encode();text=data.decode()
        chunks=parts(text)
        selections=[dict(status='relevant',block_ids=[part['blocks'][0]['block_id']]) for part in chunks]
        row=dict(message_id=42,name='long.txt',read_status='read',byte_count=len(data),sha256=hashlib.sha256(data).hexdigest(),text=text,
                 text_sha256=hashlib.sha256(data).hexdigest(),text_byte_count=len(data),extraction_kind='utf8_text_v1')
        http=FakeHTTP(*(envelope(v) for v in [*selections,generated(),verdict(mode='document',quote='Nội dung bảng báo cáo cần phân tích.')]))
        provider=Classifier(config().ollama,opener=http);turn=Turn(self.store,'doc','doc',42);token=CURRENT.set(turn)
        try:
            rows=prepare([row],provider)
            result=evaluate_batch(self.snapshot,provider,[message(42,text=TEXT,has_document=True,document_name='long.txt',document_mime='text/plain')],documents=rows)
        finally:CURRENT.reset(token)
        self.assertIsNotNone(result);self.assertEqual(len(chunks)+2,turn.calls)
        self.assertEqual(len(chunks)+4,turn.max_calls);self.assertEqual(40+20*len(chunks),turn.deadline-turn.started)
        self.assertEqual(len(chunks)+2,len(http.calls))
        self.assertIn('processing',result[0][1].analysis_review['documents'][0])

    def test_cloud_actual_retry_and_fallback_cannot_buy_third_normal_call(self):
        from tg_triage.provider import CloudProvider
        from tg_triage.ai_transport import ProviderError
        base=config();settings=replace(base,model=replace(base.model,provider='openai',cloud_consent=True,fallback='ollama'))
        for responses in (({},generated()),(ProviderError('provider_failed'),)):
            with self.subTest(responses=responses):
                http=FakeHTTP(envelope(generated()),envelope(verdict()))
                provider=CloudProvider(settings,opener=http);turn=Turn(self.store,'cloud','cloud',42);token=CURRENT.set(turn)
                try:
                    with patch('tg_triage.ai_transport.read_key',return_value='fake-key'),patch('tg_triage.thinking_transport.complete',side_effect=responses) as complete:
                        result=evaluate_batch(self.snapshot,provider,[message(42,text=TEXT)])
                finally:CURRENT.reset(token)
                self.assertIsNone(result);self.assertEqual(2,turn.calls)
                self.assertEqual(2,complete.call_count+len(http.calls))

    def test_cloud_redaction_and_real_pixels_are_pinned_for_both_calls(self):
        from tg_triage.provider import CloudProvider
        from tg_triage.ai_transport import redact
        path=self.directory/'pixels.png';path.write_bytes(PNG)
        source=message(42,text=TEXT+' contact test@example.com',has_image=True)
        base=config();settings=replace(base,model=replace(base.model,provider='openai',cloud_consent=True,cloud_image_consent=True,fallback='ollama'))
        provider=CloudProvider(settings,opener=FakeHTTP());turn=Turn(self.store,'view','view',42);token=CURRENT.set(turn)
        try:
            with patch('tg_triage.ai_transport.read_key',return_value='fake-key'),patch('tg_triage.thinking_transport.complete',side_effect=[generated(),verdict(mode='pixels',quote='')]) as complete:
                result=evaluate_batch(self.snapshot,provider,[source],images={42:path})
        finally:CURRENT.reset(token)
        self.assertIsNotNone(result);self.assertEqual(2,turn.calls)
        first,second=[call.args[3][1] for call in complete.call_args_list]
        self.assertEqual(first,second);self.assertNotIn('test@example.com',json.dumps(first))
        self.assertIn(base64.b64encode(PNG).decode(),json.dumps(first))
        self.assertEqual('cloud',result[0][1].analysis_review['provider_view'])

    def test_cloud_critic_fallback_keeps_redacted_view_and_same_long_turn(self):
        from tg_triage.provider import CloudProvider
        from tg_triage.ai_transport import ProviderError
        base=config();settings=replace(base,model=replace(base.model,provider='openai',cloud_consent=True,fallback='ollama'))
        text=TEXT+' test@example.com';http=FakeHTTP(envelope(verdict(quote=TEXT)))
        provider=CloudProvider(settings,opener=http);turn=Turn(self.store,'fallback','fallback',42)
        self.assertTrue(turn.configure_document_work(1,('host-manifest',)))
        token=CURRENT.set(turn)
        try:
            with patch('tg_triage.ai_transport.read_key',return_value='fake-key'),patch('tg_triage.thinking_transport.complete',side_effect=[generated(),ProviderError('provider_failed')]):
                result=evaluate_batch(self.snapshot,provider,[message(42,text=text)])
        finally:CURRENT.reset(token)
        self.assertIsNotNone(result);self.assertEqual(3,turn.calls)
        local=json.loads(http.calls[0][0].data)
        self.assertNotIn('test@example.com',local['messages'][1]['content'])

    def test_status_repo_case_and_notice_identity_collision(self):
        from tg_triage import ticket_status
        self.pipeline([])
        source=message(1);self.store.enqueue(source)
        with self.store.connection:
            self.store.connection.execute("UPDATE messages SET github_called_at='2026-01-01',issue_number=101,issue_url='https://GITHUB.com/Sandbox/TRIAGE/issues/101/' WHERE message_id=1")
        request=message(42,reply_to_id=1)
        self.assertEqual('created',ticket_status.resolve(self.store,[request],'sandbox/triage')['state'])
        with self.store.connection:
            self.store.connection.execute("INSERT INTO group_notices(chat_id,message_id,kind,status,reserved_at,notice_message_id) VALUES(?,1,'ISSUE','sent','now',90)",(source.chat_id,))
        self.assertEqual('created',ticket_status.resolve(self.store,[message(100,reply_to_id=90)],'sandbox/triage')['state'])
        self.store.enqueue(message(90))
        self.assertEqual('unavailable',ticket_status.resolve(self.store,[message(100,reply_to_id=90)],'sandbox/triage')['state'])


    def test_review_crash_before_notice_resumes_without_new_model_calls(self):
        from tg_triage.rules_pending_store import RulesPendingStore
        from tg_triage.rules_intake import sweep
        p,http,sent=self.pipeline([generated(),verdict()])
        with patch('tg_triage.rules_intake._read_only_result',side_effect=RuntimeError('checkpoint crash')):
            with self.assertRaises(RuntimeError):p.process(message(42,text=TEXT))
        self.assertEqual([],sent);self.assertEqual(2,len(http.calls))
        p.rules_pending=RulesPendingStore(self.store)
        self.assertEqual('PLANNED',self.store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])
        sweep(p);sweep(p)
        self.assertEqual(1,len(sent));self.assertEqual(2,len(http.calls))
        self.assertTrue(sent[0][2].startswith('Việc:'))

    def test_missing_or_copied_review_receipt_does_not_regenerate(self):
        from tg_triage.rules_intake import sweep
        from tg_triage.rules_pending_store import RulesPendingStore
        p,http,sent=self.pipeline([generated(),verdict()])
        with patch('tg_triage.rules_intake._read_only_result',side_effect=RuntimeError('before send')):
            with self.assertRaises(RuntimeError):p.process(message(42,text=TEXT))
        p.rules_pending=RulesPendingStore(self.store)
        row=self.store.connection.execute('SELECT batch_id,ordinal,plan FROM rules_batch_groups').fetchone();plan=json.loads(row[2])
        original=plan['analysis_review']
        for kind in ('missing','stripped','unknown','task','receipt'):
            changed=json.loads(row[2])
            if kind=='missing':changed.pop('analysis_review')
            elif kind=='stripped':
                changed.pop('analysis_review');changed.pop('analysis_origin')
            elif kind=='unknown':changed['analysis_review']['review_id']='other-source-review'
            elif kind=='task':changed['task']['title']='Invented change'
            else:changed['analysis_review']['receipt_hash']='0'*64
            with self.subTest(kind=kind),self.store.connection:
                self.store.connection.execute('UPDATE rules_batch_groups SET plan=? WHERE batch_id=? AND ordinal=?',(json.dumps(changed),row[0],row[1]))
                sweep(p)
                self.assertEqual([],sent);self.assertEqual(2,len(http.calls))
        self.assertIsNotNone(original)

    def test_unknown_send_and_sent_before_checkpoint_never_replay(self):
        from tg_triage.rules_pending_store import _recoverable_batch_groups,RulesHalt
        p,http,sent=self.pipeline([generated(),verdict()]);p.notice_sender=lambda *args:None
        with self.assertRaises(RulesHalt):p.process(message(42,text=TEXT))
        with self.assertRaises(RulesHalt):_recoverable_batch_groups(self.store.connection)
        self.assertEqual(2,len(http.calls));self.assertEqual([],sent)
        self.assertEqual('failed',self.store.connection.execute('SELECT status FROM group_notices').fetchone()[0])

    def test_notice_send_before_group_checkpoint_recovers_done_without_resend(self):
        from tg_triage.rules_pending_store import RulesPendingStore
        from tg_triage.rules_intake import sweep
        p,http,sent=self.pipeline([generated(),verdict()])
        transition=p.rules_pending.transition_group
        def crash(*args,**kwargs):
            if args[2:4]==('RESERVED','DONE'):raise RuntimeError('sent before group checkpoint')
            return transition(*args,**kwargs)
        with patch.object(p.rules_pending,'transition_group',side_effect=crash):
            with self.assertRaises(RuntimeError):p.process(message(42,text=TEXT))
        self.assertEqual(1,len(sent))
        p.rules_pending=RulesPendingStore(self.store);sweep(p)
        self.assertEqual('DONE',self.store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])
        self.assertEqual(1,len(sent));self.assertEqual(2,len(http.calls))

    def test_cached_reply_change_and_changed_pixel_bytes_hold(self):
        from tg_triage.rules_pending_store import RulesPendingStore
        from tg_triage.rules_intake import sweep
        from tests.helpers import FakeDownloader
        self.store.enqueue(message(10,text='Bảng báo cáo cần phân tích'))
        pixels={42:(PNG,'proof.png')};downloader=FakeDownloader(pixels)
        p,http,sent=self.pipeline([generated(),verdict(mode='pixels',quote='')],downloader=downloader)
        source=message(42,text=TEXT,reply_to_id=10,has_image=True)
        with patch('tg_triage.rules_intake._read_only_result',side_effect=RuntimeError('before send')):
            with self.assertRaises(RuntimeError):p.process(source)
        p.rules_pending=RulesPendingStore(self.store)
        p.attachments.downloader=FakeDownloader({42:(PNG+b'changed','proof.png')})
        sweep(p)
        self.assertEqual([],sent);self.assertEqual(2,len(http.calls))
        p.attachments.downloader=downloader
        with self.store.connection:self.store.connection.execute("UPDATE messages SET text='changed context' WHERE message_id=10")
        sweep(p)
        self.assertEqual([],sent);self.assertEqual(2,len(http.calls))

    def test_status_duplicate_sent_and_autonomous_confirmation_collisions_hold(self):
        from tg_triage import ticket_status
        self.pipeline([]);source=message(1);self.store.enqueue(source);self.store.enqueue(message(2))
        with self.store.connection:
            self.store.connection.execute("UPDATE messages SET issue_number=101,issue_url='https://github.com/sandbox/triage/issues/101' WHERE message_id IN (1,2)")
            for mid in (1,2):self.store.connection.execute("INSERT INTO group_notices(chat_id,message_id,kind,status,reserved_at,notice_message_id) VALUES(?,?,'ISSUE','sent','now',90)",(source.chat_id,mid))
        self.assertEqual('unavailable',ticket_status.resolve(self.store,[message(100,reply_to_id=90)],'sandbox/triage')['state'])
        with self.store.connection:
            self.store.connection.execute('DELETE FROM group_notices WHERE message_id=2')
            self.store.connection.execute("INSERT INTO autonomous_confirmations VALUES(?,2,101,'sent',90,'now')",(source.chat_id,))
        self.assertEqual('unavailable',ticket_status.resolve(self.store,[message(100,reply_to_id=90)],'sandbox/triage')['state'])


    def test_critic_cannot_cite_other_current_group_or_missing_pixels(self):
        foreign=verdict((42,43))
        for claim in foreign['reviews'][0]['claims']:claim['evidence'][0]['source_id']=43
        self.assertIsNone(self.run_review([generated((42,43)),foreign],sources=[message(42,text=TEXT),message(43,text=TEXT)])[0])
        self.assertIsNone(self.run_review([generated(),verdict(mode='pixels',quote='')])[0])

    def test_legacy_excerpt_and_authored_notice_receipts_keep_exact_recovery_rendering(self):
        from tg_triage.analysis_task import render_excerpts,render
        from tg_triage.rules_pending_store import RulesPendingStore
        from tg_triage.rules_intake import sweep
        task=dict(title='Kiểm tra lỗi',work='Phân tích yêu cầu',acceptance=['Nêu vấn đề'])
        for authored in (False,True):
            with self.subTest(authored=authored):
                # Model the pre-version persisted ledger bytes after an old notice was sent.
                store=Store(':memory:');self.addCleanup(store.close)
                previous=self.store;self.store=store
                text='Phân tích:\n'+render(task) if authored else '. '.join([task['title'],task['work'],*task['acceptance']])
                p,http,sent=self.pipeline([generated(task=task),verdict(quote=text)])
                p.process(message(42,text=text))
                row=store.connection.execute('SELECT batch_id,ordinal,plan FROM rules_batch_groups').fetchone();plan=json.loads(row[2])
                plan.pop('analysis_review');plan.pop('analysis_origin');body=render(task) if authored else render_excerpts(task)
                from tg_triage.rules_pending_store import readonly_plan_id
                planid=readonly_plan_id(row[0],row[1],self.snapshot.fingerprint,plan)
                with store.connection:
                    store.connection.execute('DROP TABLE rules_analysis_review_members')
                    store.connection.execute('DROP TABLE rules_analysis_reviews')
                    store.connection.execute("UPDATE rules_batch_groups SET status='RESERVED',plan=?",(json.dumps(plan),))
                    store.connection.execute("UPDATE rules_topics SET status='OPEN'")
                    store.connection.execute('UPDATE rules_readonly_notices SET plan_id=?,body_hash=?,body=?',(planid,hashlib.sha256(body.encode()).hexdigest(),body))
                from tg_triage.rules_pending_store import _recoverable_batch_groups
                self.assertEqual([(row[0],row[1],'DONE')],_recoverable_batch_groups(store.connection))
                p.rules_pending=RulesPendingStore(store);sweep(p)
                self.assertEqual('DONE',store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])
                self.assertEqual(body,store.connection.execute('SELECT body FROM rules_readonly_notices').fetchone()[0])
                self.assertEqual(1,len(sent));self.assertEqual(2,len(http.calls))
                self.store=previous

    def test_unavailable_critic_and_model_approval_flags_never_authorize(self):
        forged=generated();forged['groups'][0]['facts']['approved']=True
        forged['groups'][0]['facts']['receipt']={'reviewed':True}
        self.assertIsNone(self.run_review([forged,{}])[0])
        self.assertIsNone(self.run_review([generated(),dict(reviews=[])])[0])

    def test_changed_provider_view_cannot_review_generation(self):
        from tg_triage.provider import CloudProvider
        base=config();settings=replace(base,model=replace(base.model,provider='openai',cloud_consent=True,fallback='ollama'))
        provider=CloudProvider(settings,opener=FakeHTTP());turn=Turn(self.store,'pin','pin',42);token=CURRENT.set(turn)
        original=provider._cloud_messages;calls=[]
        def changed(messages):
            result=original(messages);calls.append(1)
            if len(calls)==2:result[1]['content']=result[1]['content'].replace('bảng báo cáo','bảng khác')
            return result
        try:
            with patch('tg_triage.ai_transport.read_key',return_value='fake-key'),patch('tg_triage.thinking_transport.complete',side_effect=[generated(),verdict()]),patch.object(provider,'_cloud_messages',side_effect=changed):
                result=evaluate_batch(self.snapshot,provider,[message(42,text=TEXT)])
        finally:CURRENT.reset(token)
        self.assertIsNone(result);self.assertEqual(2,turn.calls)


    def test_local_generation_after_cloud_failure_pins_local_critic(self):
        from tg_triage.provider import CloudProvider
        from tg_triage.ai_transport import ProviderError
        base=config();settings=replace(base,model=replace(base.model,provider='openai',cloud_consent=True,fallback='ollama'))
        http=FakeHTTP(envelope(generated()),envelope(verdict()))
        provider=CloudProvider(settings,opener=http);turn=Turn(self.store,'localpin','localpin',42)
        self.assertTrue(turn.configure_document_work(1,('host-manifest',)));token=CURRENT.set(turn)
        try:
            with patch('tg_triage.ai_transport.read_key',return_value='fake-key'),patch('tg_triage.thinking_transport.complete',side_effect=ProviderError('provider_failed')) as complete:
                result=evaluate_batch(self.snapshot,provider,[message(42,text=TEXT+' test@example.com')])
        finally:CURRENT.reset(token)
        self.assertIsNotNone(result);self.assertEqual(1,complete.call_count);self.assertEqual(3,turn.calls)
        first,second=[json.loads(request.data)['messages'][1] for request,_ in http.calls]
        self.assertEqual(first,second);self.assertIn('test@example.com',first['content'])
        self.assertEqual('local',result[0][1].analysis_review['provider_view'])

    def test_long_document_thinking_disabled_actual_pipeline_keeps_existing_allowance(self):
        from tg_triage.document_processing import parts
        from tests.helpers import FakeDownloader
        text='Nội dung bảng báo cáo cần phân tích.\n'*850;chunks=parts(text)
        selections=[dict(status='relevant',block_ids=[part['blocks'][0]['block_id']]) for part in chunks]
        p,http,sent=self.pipeline([*selections,generated(),verdict(mode='document',quote='Nội dung bảng báo cáo cần phân tích.')],
            downloader=FakeDownloader({42:(text.encode(),'long.txt')}))
        result=p.process(message(42,text=TEXT,has_document=True,document_name='long.txt',document_mime='text/plain'))
        self.assertEqual('semantic_analyze',result.reason);self.assertEqual(1,len(sent))
        self.assertEqual(len(chunks)+2,len(http.calls))
        receipt=json.loads(self.store.connection.execute('SELECT receipt FROM rules_analysis_reviews').fetchone()[0])
        self.assertEqual(len(chunks)+2,receipt['budget']['calls']);self.assertEqual(len(chunks)+4,receipt['budget']['max_calls'])

    def test_cloud_document_packet_preserves_typed_supplement_and_redacted_view(self):
        from tg_triage.provider import CloudProvider
        text='Tài liệu báo cáo. '*500+' test@example.com';data=text.encode()
        row=dict(message_id=42,name='proof.txt',read_status='read',byte_count=len(data),sha256=hashlib.sha256(data).hexdigest(),text=text)
        base=config();settings=replace(base,model=replace(base.model,provider='openai',cloud_consent=True,fallback='ollama'))
        provider=CloudProvider(settings,opener=FakeHTTP());turn=Turn(self.store,'clouddoc','clouddoc',42);token=CURRENT.set(turn)
        try:
            with patch('tg_triage.ai_transport.read_key',return_value='fake-key'),patch('tg_triage.thinking_transport.complete',side_effect=[generated(),verdict(mode='document',quote='Tài liệu báo cáo.')]) as complete:
                result=evaluate_batch(self.snapshot,provider,[message(42,text=TEXT,has_document=True,document_name='proof.txt',document_mime='text/plain')],documents=[row])
        finally:CURRENT.reset(token)
        self.assertIsNotNone(result);self.assertEqual(2,complete.call_count)
        first,second=[call.args[3][1] for call in complete.call_args_list]
        self.assertEqual(first,second);self.assertNotIn('test@example.com',first['content'])
        self.assertEqual([42],json.loads(first['content'])['DOCUMENT_SOURCE_IDS'])


    def test_actual_multi_group_receipt_copy_cannot_authorize_other_partition(self):
        from tg_triage.rules_intake import _process_batch
        sources=[message(42,text=TEXT),message(43,text=TEXT)]
        p,http,sent=self.pipeline([generated((42,43)),verdict((42,43))])
        for source in sources:self.store.enqueue(source)
        _process_batch(p,sources)
        self.assertEqual(2,len(sent));self.assertEqual(2,len(http.calls))
        plans=[json.loads(row[0]) for row in self.store.connection.execute('SELECT plan FROM rules_batch_groups ORDER BY ordinal')]
        self.assertTrue(rules_analysis.validate(p,plans[0]))
        copied=dict(plans[0],analysis_review=plans[1]['analysis_review'],analysis_origin=plans[1]['analysis_origin'])
        self.assertFalse(rules_analysis.validate(p,copied))
        row=self.store.connection.execute('SELECT review_id,receipt FROM rules_analysis_reviews LIMIT 1').fetchone()
        changed=json.loads(row[1]);changed['view_hash']='0'*64
        with self.store.connection:self.store.connection.execute('UPDATE rules_analysis_reviews SET receipt=? WHERE review_id=?',(json.dumps(changed),row[0]))
        self.assertFalse(rules_analysis.validate(p,plans[0]))

    def test_sqlite_close_reopen_resumes_review_without_regeneration(self):
        from tg_triage.rules_pending_store import check_before_recovery
        from tg_triage.rules_intake import sweep
        previous=self.store;path=self.directory/'analysis-restart.sqlite3';self.store=Store(path)
        p,http,sent=self.pipeline([generated(),verdict()])
        with patch('tg_triage.rules_intake._read_only_result',side_effect=RuntimeError('pre-notice crash')):
            with self.assertRaises(RuntimeError):p.process(message(42,text=TEXT))
        self.store.close();check_before_recovery(path)
        self.store=Store(path);self.addCleanup(self.store.close)
        restarted,empty_http,delivered=self.pipeline([])
        sweep(restarted);sweep(restarted)
        self.assertEqual(1,len(delivered));self.assertEqual([],empty_http.calls)
        self.assertEqual(2,len(http.calls));self.assertEqual([],sent)
        self.assertEqual('DONE',self.store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])
        self.store=previous


    def test_cloud_long_numeric_source_ids_keep_exact_json_identity(self):
        from tg_triage.provider import CloudProvider
        mid=123456789
        base=config();settings=replace(base,model=replace(base.model,provider='openai',cloud_consent=True,fallback='ollama'))
        provider=CloudProvider(settings,opener=FakeHTTP());turn=Turn(self.store,'longid','longid',mid);token=CURRENT.set(turn)
        try:
            with patch('tg_triage.ai_transport.read_key',return_value='fake-key'),patch('tg_triage.thinking_transport.complete',side_effect=[generated((mid,)),verdict((mid,))]) as complete:
                result=evaluate_batch(self.snapshot,provider,[message(mid,text=TEXT)])
        finally:CURRENT.reset(token)
        self.assertIsNotNone(result)
        self.assertEqual(complete.call_args_list[0].args[3][1],complete.call_args_list[1].args[3][1])


    def mixed_fixture(self,reverse=False):
        from tests.helpers import FakeDownloader
        doc='Báo vị trí nội dung bị che trong bảng báo cáo.'
        sources=[message(42,text=TEXT,sender_id=100),message(43,text='',sender_id=200,reply_to_id=42,has_image=True),
                 message(44,text='',sender_id=300,reply_to_id=42,has_document=True,document_name='evidence.txt',document_mime='text/plain')]
        if reverse:sources.reverse()
        proposal=dict(groups=[dict(message_ids=[44,42,43],facts=dict(intent=dict(value='analyze',source_ids=[42]),task=TASK))])
        review=dict(reviews=[dict(group_index=0,verdict='approve',claims=[
            dict(field='title',evidence=[dict(source_id=42,modality='text',quote=TEXT)]),
            dict(field='work',evidence=[dict(source_id=43,modality='pixels',quote='')]),
            dict(field='acceptance/0',evidence=[dict(source_id=44,modality='document',quote=doc)])])])
        downloader=FakeDownloader({43:(PNG,'evidence.png'),44:(doc.encode(),'evidence.txt')})
        p,http,sent=self.pipeline([proposal,review],downloader=downloader)
        for source in sources:self.store.enqueue(source)
        return p,http,sent,sources,downloader,doc

    def test_grouped_text_image_document_multi_sender_uses_one_canonical_anchor(self):
        from tg_triage.rules_intake import _process_batch
        p,http,sent,sources,_,doc=self.mixed_fixture(reverse=True)
        def send(chat,mid,body):
            self.assertFalse(self.store.connection.in_transaction)
            self.assertEqual(42,mid)
            self.assertEqual(['pending_review']*3,[r[0] for r in self.store.connection.execute('SELECT decision FROM messages ORDER BY message_id')])
            self.assertEqual(3,self.store.connection.execute('SELECT count(*) FROM rules_analysis_review_members').fetchone()[0])
            self.assertEqual('reserved',self.store.connection.execute('SELECT status FROM group_notices').fetchone()[0])
            sent.append((chat,mid,body));return 901
        p.notice_sender=send
        _process_batch(p,sources)
        self.assertEqual(1,len(sent));self.assertEqual(2,len(http.calls));self.assertEqual([],p.github.calls)
        self.assertEqual([('chat','semantic_analyze')]*3,[tuple(r) for r in self.store.connection.execute('SELECT decision,reason FROM messages ORDER BY message_id')])
        plan=json.loads(self.store.connection.execute('SELECT plan FROM rules_batch_groups').fetchone()[0])
        self.assertEqual([[sources[0].chat_id,mid] for mid in (42,43,44)],plan['source_keys'])
        first,second=[json.loads(request.data)['messages'][1] for request,_ in http.calls]
        self.assertEqual(first,second);packet=json.loads(first['content'])
        self.assertEqual([43],packet['images_order']);self.assertEqual(doc,packet['DOCUMENT_CONTENTS'][0]['text'])
        self.assertEqual(3,len({r['sender_ref'] for r in packet['sources']}))
        self.assertEqual([base64.b64encode(PNG).decode()],first['images'])

    def test_grouped_unknown_send_keeps_every_member_pending_and_cannot_reclassify_sibling(self):
        from tg_triage.rules_intake import _process_batch,sweep
        from tg_triage.rules_pending_store import RulesHalt,_recoverable_batch_groups
        p,http,sent,sources,_,_=self.mixed_fixture()
        attempts=[];p.notice_sender=lambda *args:attempts.append(args) or None
        with self.assertRaises(RulesHalt):_process_batch(p,sources)
        self.assertEqual(['pending_review']*3,[r[0] for r in self.store.connection.execute('SELECT decision FROM messages ORDER BY message_id')])
        with self.assertRaises(RulesHalt):_recoverable_batch_groups(self.store.connection)
        with self.assertRaises(RulesHalt):p.process(sources[-1])
        self.assertEqual(2,len(http.calls));self.assertEqual(1,len(attempts))
        self.assertEqual([],p.github.calls)

    def test_grouped_second_member_persistence_failure_rolls_back_completion_and_sent_checkpoint(self):
        from tg_triage.rules_intake import _process_batch,sweep
        from tg_triage.rules_pending_store import RulesHalt,_recoverable_batch_groups,RulesPendingStore
        p,http,sent,sources,_,_=self.mixed_fixture()
        with self.store.connection:
            self.store.connection.execute("""CREATE TRIGGER fail_second_analysis BEFORE UPDATE OF decision ON messages
                WHEN NEW.message_id=43 AND NEW.decision='chat' BEGIN SELECT RAISE(ABORT,'fixture second member failure'); END""")
        with self.assertRaises(RulesHalt):_process_batch(p,sources)
        self.assertEqual(1,len(sent));self.assertEqual(2,len(http.calls))
        self.assertEqual(['pending_review']*3,[r[0] for r in self.store.connection.execute('SELECT decision FROM messages ORDER BY message_id')])
        self.assertEqual('reserved',self.store.connection.execute('SELECT status FROM group_notices').fetchone()[0])
        self.assertEqual('RESERVED',self.store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])
        with self.assertRaises(RulesHalt):_recoverable_batch_groups(self.store.connection)
        with self.store.connection:self.store.connection.execute('DROP TRIGGER fail_second_analysis')
        p.rules_pending=RulesPendingStore(self.store)
        sweep(p)
        with self.assertRaises(RulesHalt):p.process(sources[-1])
        self.assertIn(self.store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0],('RESERVED','AMBIGUOUS'))
        self.assertEqual(1,len(sent));self.assertEqual(2,len(http.calls))

    def test_grouped_sent_before_batch_checkpoint_recovers_all_members_without_send(self):
        from tg_triage.rules_intake import _process_batch,sweep
        from tg_triage.rules_pending_store import RulesPendingStore
        p,http,sent,sources,_,_=self.mixed_fixture(reverse=True)
        transition=p.rules_pending.transition_group
        def crash(*args,**kwargs):
            if args[2:4]==('RESERVED','DONE'):raise RuntimeError('sent before batch checkpoint')
            return transition(*args,**kwargs)
        with patch.object(p.rules_pending,'transition_group',side_effect=crash):
            with self.assertRaises(RuntimeError):_process_batch(p,sources)
        self.assertEqual(1,len(sent))
        p.rules_pending=RulesPendingStore(self.store);sweep(p)
        self.assertEqual('DONE',self.store.connection.execute('SELECT status FROM rules_batch_groups').fetchone()[0])
        self.assertEqual(['chat']*3,[r[0] for r in self.store.connection.execute('SELECT decision FROM messages ORDER BY message_id')])
        self.assertEqual(1,len(sent));self.assertEqual(2,len(http.calls))

    def test_grouped_exact_sent_receipt_can_settle_pending_members_without_resend(self):
        from tg_triage.rules_intake import _process_batch,sweep
        from tg_triage.rules_pending_store import RulesPendingStore
        p,http,sent,sources,_,_=self.mixed_fixture()
        transition=p.rules_pending.transition_group
        def crash(*args,**kwargs):
            if args[2:4]==('RESERVED','DONE'):raise RuntimeError('fixture old member checkpoint')
            return transition(*args,**kwargs)
        with patch('tg_triage.rules_analysis.finish_members'),patch.object(p.rules_pending,'transition_group',side_effect=crash):
            with self.assertRaises(RuntimeError):_process_batch(p,sources)
        self.assertEqual('sent',self.store.connection.execute('SELECT status FROM group_notices').fetchone()[0])
        self.assertEqual(['pending_review']*3,[r[0] for r in self.store.connection.execute('SELECT decision FROM messages ORDER BY message_id')])
        p.rules_pending=RulesPendingStore(self.store);sweep(p)
        self.assertEqual(['chat']*3,[r[0] for r in self.store.connection.execute('SELECT decision FROM messages ORDER BY message_id')])
        self.assertEqual(1,len(sent));self.assertEqual(2,len(http.calls))

    def test_grouped_source_pixel_document_or_member_receipt_change_holds_on_resume(self):
        from tg_triage.rules_intake import _process_batch,sweep
        from tg_triage.rules_pending_store import RulesPendingStore
        from tests.helpers import FakeDownloader
        previous=self.store
        for change in ('source','pixel','document','member_receipt'):
            with self.subTest(change=change):
                self.store=Store(':memory:');self.addCleanup(self.store.close)
                p,http,sent,sources,downloader,doc=self.mixed_fixture(reverse=True)
                with patch('tg_triage.rules_intake._read_only_result',side_effect=RuntimeError('pre-notice crash')):
                    with self.assertRaises(RuntimeError):_process_batch(p,sources)
                p.rules_pending=RulesPendingStore(self.store)
                if change=='source':
                    with self.store.connection:self.store.connection.execute("UPDATE messages SET text='changed image caption' WHERE message_id=43")
                elif change=='member_receipt':
                    with self.store.connection:self.store.connection.execute('DELETE FROM rules_analysis_review_members WHERE message_id=43')
                else:
                    p.attachments.downloader=FakeDownloader({43:(PNG+b'changed' if change=='pixel' else PNG,'evidence.png'),
                        44:((doc+' changed' if change=='document' else doc).encode(),'evidence.txt')})
                sweep(p)
                self.assertEqual([],sent);self.assertEqual(2,len(http.calls));self.assertEqual([],p.github.calls)
        self.store=previous

    def test_grouped_analysis_sqlite_restart_reuses_receipt_without_model_or_send_duplication(self):
        from tg_triage.rules_intake import _process_batch,sweep
        from tg_triage.rules_pending_store import check_before_recovery
        previous=self.store;path=self.directory/'mixed-restart.sqlite3';self.store=Store(path)
        p,http,sent,sources,downloader,_=self.mixed_fixture(reverse=True)
        with patch('tg_triage.rules_intake._read_only_result',side_effect=RuntimeError('pre-notice crash')):
            with self.assertRaises(RuntimeError):_process_batch(p,sources)
        self.store.close();check_before_recovery(path)
        self.store=Store(path);self.addCleanup(self.store.close)
        restarted,empty_http,delivered=self.pipeline([],downloader=downloader)
        sweep(restarted);sweep(restarted)
        self.assertEqual(1,len(delivered));self.assertEqual(42,delivered[0][1]);self.assertEqual([],empty_http.calls)
        self.assertEqual(2,len(http.calls));self.assertEqual([],sent)
        self.assertEqual(['chat']*3,[r[0] for r in self.store.connection.execute('SELECT decision FROM messages ORDER BY message_id')])
        self.store=previous

    def test_status_different_target_group_holds_instead_of_borrowing_anchor(self):
        payload=dict(groups=[dict(message_ids=[42,43],facts=dict(intent=dict(value='status',source_ids=[42,43])))])
        result,http,_=self.run_review([payload],sources=[message(42,text='trạng thái?',reply_to_id=1),message(43,text='trạng thái?',reply_to_id=2)])
        self.assertIsNone(result);self.assertEqual(1,len(http.calls))


    def test_literal_new_task_cannot_downgrade_by_stripping_both_host_fields(self):
        from tg_triage.rules_intake import sweep,_task_grounded
        from tg_triage.rules_pending_store import RulesPendingStore
        task=dict(title='Kiểm tra lỗi',work='Phân tích yêu cầu',acceptance=['Nêu vấn đề'])
        text='. '.join([task['title'],task['work'],*task['acceptance']])
        source=message(42,text=text)
        self.assertTrue(_task_grounded(task,[source]))
        p,http,sent=self.pipeline([generated(task=task),verdict(quote=text)])
        with patch('tg_triage.rules_intake._read_only_result',side_effect=RuntimeError('before send')):
            with self.assertRaises(RuntimeError):p.process(source)
        p.rules_pending=RulesPendingStore(self.store)
        plan=json.loads(self.store.connection.execute('SELECT plan FROM rules_batch_groups').fetchone()[0])
        plan.pop('analysis_review');plan.pop('analysis_origin')
        with self.store.connection:self.store.connection.execute('UPDATE rules_batch_groups SET plan=?',(json.dumps(plan),))
        result=sweep(p)
        self.assertTrue(result)
        self.assertEqual('rules_analysis_receipt_missing',result[0].reason)
        self.assertEqual([],sent);self.assertEqual(2,len(http.calls))
