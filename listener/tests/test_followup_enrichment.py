"""Canonical managed enrichment with real serialization, upload ledger and guarded REST refresh."""
import base64
import copy
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from dataclasses import replace

from tg_triage.classifier import Classifier
from tg_triage.config import AttachmentsConfig
from tg_triage.github_issues import GitHubIssues,GitHubConflict
from tg_triage.issue_format import Issue
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tg_triage.thinking import request_text_bytes,ContextJSON
from tests.helpers import config,message,topic,FakeTopicModel,FakeHTTP,FakeUploader,FakeDownloader,JPG,NOW
from tests.test_followups import answer,group

ERROR='Nothing was sent to the printer. Try printing again.'
TEXTS=['POS không print được ticket và receipt, nhờ team kiểm tra','Anh đang dùng app hay web?',
       'cả 2','vừa app và web app, dùng máy in có dây','Dạ để em check']


def projection(**changes):
    value=dict(category='bug',type='bug',actionable=True,verdict='ticket',title='POS không in được ticket và receipt',
               summary='POS không in được ticket/receipt trên app và web, dùng máy in có dây.',priority='medium',
               confidence=.95,missing_info=[],reason='broken_print',key_message_ids=[448,451,454],
               details=dict(environment='App và web app; máy in có dây',actual='Không in được ticket và receipt',error_text=ERROR))
    value.update(changes); return value


class IssueAPI:
    def __init__(self):
        self.issue=None; self.requests=[]; self.creates=0; self.patches=0; self.comments=[]
        self.patch_unknown=False; self.wrong_get=False
    def __call__(self,request,timeout):
        self.requests.append((request.method,request.full_url,request.data,timeout))
        if request.method=='POST' and request.full_url.endswith('/issues'):
            self.creates+=1; data=json.loads(request.data)
            self.issue=dict(number=47,html_url='https://github.com/sandbox/triage/issues/47',**data)
            result=self.issue
        elif request.method=='POST' and request.full_url.endswith('/comments'):
            self.comments.append(json.loads(request.data)['body']); result={'id':700+len(self.comments)}
        elif request.method=='PATCH':
            self.patches+=1; self.issue.update(json.loads(request.data))
            if self.patch_unknown: raise URLError('unknown after write')
            result=self.issue
        else:
            result=copy.deepcopy(self.issue)
            if self.wrong_get: result['number']=999
        return io.BytesIO(json.dumps(result,ensure_ascii=False).encode())


class EnrichmentTests(unittest.TestCase):
    def setup_case(self,path=':memory:',initial=None,exclude_ack=False):
        store=Store(path,clock=lambda:NOW); self.addCleanup(store.close)
        api=IssueAPI(); gh=GitHubIssues('sandbox/triage','fake',opener=api)
        uploader=FakeUploader(); downloader=FakeDownloader({454:(JPG,'photo.jpg')}); sent=[]
        settings=config('live',window=5,max_messages=5,attachments=AttachmentsConfig())
        topics=(topic([448,449,450,451],verdict='needs_human'),topic([452],verdict='noise')) if exclude_ack else (topic([448,449,450,451,452],verdict='needs_human'),)
        model=FakeTopicModel(topics)
        p=Pipeline(settings,store,model,gh,uploader=uploader,downloader=downloader,
                   notice_sender=lambda chat,target,text:sent.append((target,text)) or (453 if target==448 else target+1))
        for mid,text in zip(range(448,453),TEXTS): p.process(message(mid,text=text,sender_id=77))
        self.assertEqual(api.creates,1)
        response=answer('update',summary='Bổ sung lỗi trong ảnh',case_analysis=initial or projection())
        model_http=FakeHTTP({'message':{'content':json.dumps(response,ensure_ascii=False)}})
        p.classifier=Classifier(p.config.ollama,prompt='unused',opener=model_http)
        return store,p,api,uploader,downloader,sent,model_http

    def photo(self,p,mid=454):
        # Production ingress normalizes notice453 to original448; direct pipeline callers
        # may still pass actual outgoing notice ID and are resolved by trusted DB records.
        return p.process(message(mid,text='Đây là hình ảnh',sender_id=77,reply_to_id=453,
                                 has_image=True,image_size=len(JPG),image_mime='image/jpeg'))

    def test_new_photo_is_joint_on_wire_and_refreshes_canonical_bug_once(self):
        store,p,api,up,down,sent,http=self.setup_case()
        before_case=dict(store.get_case(store.get(message().chat_id,448)['case_key']))
        before_original=dict(store.get(message().chat_id,448))
        self.photo(p)
        self.assertEqual(api.creates,1); self.assertEqual(api.patches,1); self.assertEqual(api.comments,[])
        self.assertEqual(len(up.uploads),1); self.assertEqual(len(down.calls),1)
        self.assertTrue(all(not path.exists() for path in down.directories))
        self.assertIn(ERROR,api.issue['body']); self.assertIn('App và web app',api.issue['body'])
        self.assertIn('máy in có dây',api.issue['body']); self.assertIn('/454-1.jpg',api.issue['body'])
        self.assertIn('[bug]',api.issue['title']); self.assertIn('bug',api.issue['labels'])
        self.assertNotIn('needs-human',api.issue['labels'])
        body=json.loads(http.calls[0][0].data)
        self.assertEqual(len(body['messages']),2)
        user=body['messages'][1]; packet=json.loads(user['content'])
        self.assertEqual(packet['images_order'],[dict(image_index=0,message_id=454)])
        self.assertEqual(base64.b64decode(user['images'][0]),JPG)
        self.assertEqual([m['message_id'] for m in packet['ORIGINAL_METADATA']],[448,449,450,451,452,454])
        self.assertTrue(packet['ORIGINAL_METADATA'][-1]['pixels_available'])
        self.assertTrue(all(t in user['content'] for t in TEXTS))
        self.assertLessEqual(request_text_bytes(body),8000)
        self.assertIsNone(self.photo(p)); p.close_due_cases()
        self.assertEqual(api.patches,1); self.assertEqual(len(up.uploads),1)
        self.assertEqual(dict(store.get(message().chat_id,448)),before_original)
        self.assertEqual(dict(store.get_case(before_original['case_key'])),before_case)
        state=json.loads(store.connection.execute('SELECT context_state FROM followup_roots').fetchone()[0])
        self.assertEqual(state['managed_snapshot']['body_sha'],hashlib.sha256(api.issue['body'].encode()).hexdigest())
        self.assertIn(454,state['managed_snapshot']['source_ids'])
        record=json.loads(store.connection.execute("SELECT record FROM thinking_decisions WHERE turn_key LIKE 'followup:%'").fetchone()[0])
        self.assertEqual(record['budget']['reads_used'],3); self.assertEqual(record['budget']['model_calls_used'],1)

    def test_restart_reuses_uploaded_media_and_planned_refresh(self):
        directory=self.enterContext(tempfile.TemporaryDirectory()); path=Path(directory)/'db'
        store,p,api,up,down,sent,http=self.setup_case(path)
        with patch.object(store,'reserve_followup_operation',side_effect=SystemExit):
            with self.assertRaises(SystemExit): self.photo(p)
        self.assertEqual(len(up.uploads),1); self.assertEqual(api.patches,0)
        restarted=Store(path,clock=lambda:NOW); self.addCleanup(restarted.close)
        p2=Pipeline(p.config,restarted,p.classifier,p.github,uploader=up,downloader=down,notice_sender=p.notice_sender)
        p2.close_due_cases(); p2.close_due_cases()
        self.assertEqual(api.patches,1); self.assertEqual(api.creates,1); self.assertEqual(len(up.uploads),1)

    def test_reserved_refresh_unknown_patch_never_retries_or_advances_snapshot(self):
        store,p,api,up,down,sent,http=self.setup_case()
        old=json.loads(store.connection.execute('SELECT context_state FROM followup_roots').fetchone()[0])['managed_snapshot']
        api.patch_unknown=True; self.photo(p); p.close_due_cases()
        self.assertEqual(api.patches,1); self.assertEqual(api.creates,1)
        state=json.loads(store.connection.execute('SELECT context_state FROM followup_roots').fetchone()[0])
        self.assertEqual(state['managed_snapshot']['body_sha'],old['body_sha'])
        op=store.connection.execute('SELECT status FROM followup_operations').fetchone()[0]
        self.assertEqual(op,'ambiguous'); self.assertIn('chưa xác nhận',sent[-1][1])

    def test_reserved_refresh_crash_restart_never_resends_patch(self):
        directory=self.enterContext(tempfile.TemporaryDirectory()); path=Path(directory)/'db'
        store,p,api,up,down,sent,http=self.setup_case(path)
        with patch.object(p.github,'refresh_issue',side_effect=SystemExit):
            with self.assertRaises(SystemExit): self.photo(p)
        restarted=Store(path,clock=lambda:NOW); self.addCleanup(restarted.close)
        p2=Pipeline(p.config,restarted,p.classifier,p.github,uploader=up,downloader=down,notice_sender=p.notice_sender)
        p2.close_due_cases(); p2.close_due_cases()
        self.assertEqual(api.patches,0); self.assertEqual(len(up.uploads),1)
        self.assertEqual(restarted.connection.execute('SELECT status FROM followup_operations').fetchone()[0],'ambiguous')

    def test_legacy_update_photo_comment_preserves_body_and_later_refresh_retains_media(self):
        store,p,api,up,down,sent,http=self.setup_case()
        p.classifier=Classifier(p.config.ollama,prompt='unused',opener=FakeHTTP({'message':{'content':json.dumps(answer('update'))}}))
        old=api.issue['body']; self.photo(p)
        self.assertEqual(api.patches,0); self.assertEqual(api.issue['body'],old)
        self.assertIn('/454-1.jpg',api.comments[0])
        draft=projection(key_message_ids=[448,451,454,456])
        p.classifier=Classifier(p.config.ollama,prompt='unused',opener=FakeHTTP({'message':{'content':json.dumps(answer('update',case_analysis=draft,source_roles=[dict(message_id=454,role='ancillary',confidence=.95)]))}}))
        p.process(message(456,text='Xác nhận lỗi trong ảnh đã gửi',sender_id=77,reply_to_id=453))
        self.assertEqual(api.patches,1); self.assertIn('/454-1.jpg',api.issue['body'])
        self.assertEqual(len(up.uploads),1)

    def test_existing_manual_body_title_or_owned_label_edits_preserved(self):
        for field,value in (('body','human edit'),('title','human title'),('labels',['bug','source:telegram'])):
            store,p,api,up,down,sent,http=self.setup_case()
            api.issue[field]=value; before=copy.deepcopy(api.issue)
            self.photo(p)
            self.assertEqual(api.patches,0); self.assertEqual(api.issue,before)
            self.assertIn('giữ nguyên',sent[-1][1])

    def test_unrelated_labels_preserved_and_wrong_get_target_never_patched(self):
        store,p,api,up,down,sent,http=self.setup_case(); api.issue['labels'].append('customer-important')
        self.photo(p); self.assertIn('customer-important',api.issue['labels'])
        store2,p2,api2,up2,down2,sent2,http2=self.setup_case(); api2.wrong_get=True
        self.photo(p2); self.assertEqual(api2.patches,0); self.assertEqual(api2.creates,1)

    def test_unmanaged_historical_issue_uses_evidence_comment_only(self):
        store,p,api,up,down,sent,http=self.setup_case()
        state=json.loads(store.connection.execute('SELECT context_state FROM followup_roots').fetchone()[0]); state.pop('managed_snapshot')
        with store.connection: store.connection.execute('UPDATE followup_roots SET context_state=?',(json.dumps(state),))
        before=api.issue['body']; self.photo(p)
        self.assertEqual(api.patches,0); self.assertEqual(api.issue['body'],before)
        self.assertEqual(len(api.comments),1); self.assertIn('/454-1.jpg',api.comments[0])
        self.assertEqual(len(up.uploads),1)

    def test_vision_denied_or_invalid_image_never_invents_ocr_or_refresh(self):
        for denied in (True,False):
            store,p,api,up,down,sent,http=self.setup_case()
            if denied: p.classifier.supports_vision=False
            else: down.files[454]=(b'not image','bad.jpg')
            self.photo(p)
            self.assertEqual(api.patches,0); self.assertEqual(api.comments,[]); self.assertEqual(up.uploads,[])
            self.assertEqual(http.calls,[]); self.assertIn('ảnh mới',sent[-1][1])
            self.assertNotIn(ERROR,api.issue['body'])

    def test_upload_error_is_honest_and_no_reupload_after_ledger_exists(self):
        store,p,api,up,down,sent,http=self.setup_case(); up.error=OSError('upload unknown')
        self.photo(p); p.close_due_cases()
        self.assertEqual(len(up.uploads),1); self.assertEqual(api.patches,1)
        self.assertIn('chưa xác nhận',api.issue['body']); self.assertIn('chưa xác nhận',sent[-1][1])
        self.assertNotIn('/454-1.jpg',api.issue['body'])

    def test_malformed_or_incoherent_case_projection_is_rejected(self):
        for change in (dict(actionable=None),dict(category='chat'),dict(type='task')):
            store,p,api,up,down,sent,http=self.setup_case(initial=projection(**change))
            self.photo(p); self.assertEqual(api.patches,0); self.assertEqual(api.comments,[])
        malformed=projection(); malformed.pop('reason')
        store,p,api,up,down,sent,http=self.setup_case(initial=malformed)
        self.photo(p); self.assertEqual(api.patches,0)

    def test_split_ambiguity_or_invented_source_ids_block_enrichment(self):
        store,p,api,up,down,sent,http=self.setup_case(initial=projection(key_message_ids=[999]))
        self.photo(p); self.assertEqual(api.patches,0); self.assertEqual(up.uploads,[])
        store2,p2,api2,up2,down2,sent2,http2=self.setup_case()
        with store2.connection: store2.connection.execute("UPDATE followup_roots SET siblings='[48]'")
        self.photo(p2); self.assertEqual(api2.patches,0); self.assertEqual(up2.uploads,[])

    def test_explicit_empty_detail_clears_wrong_model_hypothesis(self):
        store,p,api,up,down,sent,http=self.setup_case(initial=projection(details=dict(error_text='',environment='App và web app; máy in có dây')))
        state=json.loads(store.connection.execute('SELECT context_state FROM followup_roots').fetchone()[0])
        state['managed_snapshot']['analysis']=projection(details=dict(error_text='Old incorrect OCR',actual='Old actual'))
        with store.connection: store.connection.execute('UPDATE followup_roots SET context_state=?',(json.dumps(state),))
        self.photo(p)
        self.assertNotIn('Old incorrect OCR',api.issue['body'])
        self.assertIn('Old actual',api.issue['body'])

    def test_preflight_get_failure_is_not_sent_but_readback_failure_is_ambiguous(self):
        for readback in (False,True):
            store,p,api,up,down,sent,http=self.setup_case()
            real=api.__call__
            def failed_get(request,timeout):
                if request.method=='GET' and ((readback and api.patches) or (not readback)):
                    raise URLError('read failure')
                return real(request,timeout)
            p.github.opener=failed_get
            self.photo(p); p.close_due_cases()
            status=store.connection.execute('SELECT status FROM followup_operations').fetchone()[0]
            self.assertEqual(status,'ambiguous' if readback else 'not_sent')
            self.assertEqual(api.patches,1 if readback else 0)
            if not readback: self.assertIn('chưa gửi thay đổi',sent[-1][1])

    def test_new_upload_runner_deadline_is_per_call_without_global_mutation(self):
        from tg_triage.attachments import GhCliUploader
        from types import SimpleNamespace
        store,p,api,up,down,sent,http=self.setup_case()
        timeouts=[]
        def runner(*args,**kwargs):
            timeouts.append(kwargs['timeout'])
            return SimpleNamespace(returncode=0)
        uploader=GhCliUploader('sandbox/triage','fake',runner=runner,executable='gh',timeout=120)
        p.attachments.uploader=uploader
        self.photo(p)
        self.assertEqual(api.patches,1)
        self.assertTrue(timeouts); self.assertTrue(all(0<t<=40 for t in timeouts))
        self.assertEqual(uploader._timeout,120); self.assertIs(uploader._runner,runner)

    def test_new_pixel_packet_is_internal_contextjson_and_preserves_numeric_ids(self):
        store,p,api,up,down,sent,http=self.setup_case()
        captured=[]; original=p.classifier._ask
        def check(body,parser,**kwargs):
            self.assertIsInstance(body['messages'][1]['content'],ContextJSON)
            captured.append(body)
            return original(body,parser,**kwargs)
        p.classifier._ask=check; self.photo(p)
        self.assertEqual(json.loads(captured[0]['messages'][1]['content'])['REPLY']['message_id'],454)

    def test_update_roles_accept_actual_reply_but_reject_unprovided_ids(self):
        for role_id in (454,452,999):
            store,p,api,up,down,sent,http=self.setup_case(exclude_ack=True)
            response=answer('update',case_analysis=projection(),source_roles=[dict(message_id=role_id,role='ancillary',confidence=.95)])
            wire=FakeHTTP({'message':{'content':json.dumps(response)}})
            p.classifier=Classifier(p.config.ollama,prompt='unused',opener=wire)
            self.photo(p)
            self.assertEqual(api.patches,1 if role_id==454 else 0)
            self.assertEqual(len(up.uploads),1 if role_id==454 else 0)
            self.assertEqual(api.creates,1)
            packet=json.loads(json.loads(wire.calls[0][0].data)['messages'][1]['content'])
            self.assertNotIn(452,[m['message_id'] for m in packet['ORIGINAL_METADATA']])

    def test_split_roles_cannot_use_new_reply_as_partition_authority(self):
        store,p,api,up,down,sent,http=self.setup_case()
        response=answer('split',groups=[group([448,449]),group([450,451,452])],
                        source_roles=[dict(message_id=454,role='request',confidence=.99)])
        p.classifier=Classifier(p.config.ollama,prompt='unused',opener=FakeHTTP({'message':{'content':json.dumps(response)}}))
        self.photo(p)
        self.assertEqual(api.creates,1); self.assertEqual(api.patches,0); self.assertEqual(up.uploads,[])
        job=store.connection.execute('SELECT plan FROM followup_jobs').fetchone()[0]
        self.assertEqual(json.loads(job)['context_reason'],'invalid_source_roles')

    def test_packet_marks_source_and_reply_excerpts_as_truncated(self):
        store,p,api,up,down,sent,http=self.setup_case()
        captured=[]
        def ask(body,parser,**kwargs):
            captured.append(body)
            return parser(json.dumps(answer('chat')))
        p.classifier._ask=ask
        originals=[message(448,text='source '*300)]
        reply=message(454,text='reply '*300,reply_to_id=448)
        p.classifier.analyze_followup(originals,reply,[],[])
        packet=json.loads(captured[0]['messages'][1]['content'])
        self.assertTrue(packet['REPLY']['text_truncated'])
        self.assertTrue(all(m['text_truncated'] for m in packet['ORIGINAL_METADATA']))
