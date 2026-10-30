"""Current incident evidence must not inherit optional historical screenshots."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from tg_triage.classifier import Classifier
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tg_triage.thinking import request_text_bytes
from tests.helpers import (config, message, classification, FakeHTTP, FakeGitHub,
                           FakeDownloader, FakeUploader, JPG, NOW)
from tests.test_autonomous import Router, route, draft


def wire(ids, *, action='NEW', candidate=None, title='Lỗi đặt lịch', summary='AI voice không gửi link booking; khách không đặt lịch trên web được.'):
    analysis=dict(vars(classification(title=title,summary=summary)),verdict='ticket',category='bug',
                  actionable=True,key_message_ids=ids,missing_info=[],reason='Reported behavior',
                  details=dict(error_text='COMMON_VALIDATION_ERROR'))
    return dict(message_ids=ids,action=action,candidate_id=candidate,confidence=.95,question='',analysis=analysis)


def response(*groups):
    return {'message':{'content':json.dumps(dict(groups=list(groups)),ensure_ascii=False)}}


def incident():
    return [message(463,text='',has_image=True,is_forward=True,mentions_listener=False),
            message(464,text='Kiểu nầy chết luôn',is_forward=True,mentions_listener=False),
            message(465,text='AI voice không gửi link booking',is_forward=True,mentions_listener=False),
            message(466,text='Khách vào web không book được luôn',is_forward=True,mentions_listener=False)]


class IncidentContextTests(TestCase):
    def pipeline(self,http,*,cap=5):
        store=Store(':memory:'); self.addCleanup(store.close)
        cfg=replace(config('live'),autonomous_enabled=True,
                    attachments=replace(config().attachments,max_per_case=cap))
        seed=Router([route([457],'CLARIFY',analysis=draft([457],verdict='needs_human',actionable=False))],
                    [route([458],'CLARIFY',analysis=draft([458],verdict='needs_human',actionable=False))],
                    [route([461],'CLARIFY',analysis=draft([461],verdict='needs_human',actionable=False))])
        downloader=FakeDownloader({461:(JPG,'old.jpg'),463:(JPG,'new.jpg')})
        pipeline=Pipeline(cfg,store,seed,FakeGitHub(),downloader=downloader,uploader=FakeUploader())
        for mid,text,image in ((457,'Login đúng pass mà không login được',False),
                               (458,'Sao tính tiền',False),(461,'Invalid username or password',True)):
            pipeline.process(message(mid,text=text,has_image=image,received_at=NOW-timedelta(hours=3),mentions_listener=False))
        pipeline.classifier=Classifier(cfg.ollama,opener=http)
        pipeline.notices=[]
        pipeline.notice_sender=lambda c,m,t:pipeline.notices.append((m,t)) or 9000+len(pipeline.notices)
        return pipeline,downloader

    def test_critic_rejects_wrong_four_way_split_without_rewriting_or_tool_effect(self):
        http=FakeHTTP(response(wire([463]),wire([464]),wire([465]),wire([466])),
                      {'message':{'content':json.dumps({'reviews':[{'group_index':i,'verdict':'reject'} for i in range(4)]})}})
        p,download=self.pipeline(http,cap=1)
        for m in incident():p.store.enqueue(m)
        result=p.process(incident()[0])
        self.assertEqual('pending_review',result.decision)
        self.assertFalse(p.github.calls); self.assertEqual([463],[m for m,_ in p.notices])
        self.assertEqual([463],[mid for _,mid in download.calls])
        self.assertEqual(2,len(http.calls))
        self.assertEqual(4,p.store.connection.execute("SELECT COUNT(*) FROM autonomous_routes WHERE message_id>=463").fetchone()[0])
        self.assertNotEqual(p.store.get(incident()[0].chat_id,457)['case_key'],result.case_key)
        self.assertEqual('pending_review',p.store.get(incident()[0].chat_id,457)['decision'])
        self.assertEqual({'CLARIFY'},{r[0] for r in p.store.connection.execute('SELECT action FROM autonomous_routes WHERE message_id>=463')})
        for index,(request,_) in enumerate(http.calls):
            body=json.loads(request.data); packet=json.loads(body['messages'][1]['content'])
            self.assertEqual([463],[m['message_id'] for m in packet['images_order']])
            self.assertEqual([],packet['candidates'])
            self.assertLessEqual(request_text_bytes(body),8000)
            self.assertEqual(bool(index),'PROPOSED_ROUTES_FOR_REVIEW' in packet)
        self.assertEqual('semantic_review_rejected',p.classifier.last_ingress_outcome)

    def test_invalid_review_fails_closed_before_plans_and_one_technical_notice(self):
        http=FakeHTTP(response(wire([463]),wire([464,465,466])),
                      {'message':{'content':'invalid review'}})
        p,_=self.pipeline(http)
        for m in incident():p.store.enqueue(m)
        p.process(incident()[0])
        self.assertFalse(p.github.calls); self.assertEqual(1,len(p.notices))
        self.assertEqual(2,len(http.calls))
        self.assertEqual({'CLARIFY'},{r[0] for r in p.store.connection.execute('SELECT action FROM autonomous_routes WHERE message_id>=463')})

    def test_review_over_budget_never_executes_first_proposal(self):
        http=FakeHTTP(response(wire([463]),wire([464,465,466])))
        p,_=self.pipeline(http)
        for m in incident():p.store.enqueue(m)
        def measure(body):
            if 'PROPOSED_ROUTES_FOR_REVIEW' in body['messages'][1]['content']:
                return 8001
            return request_text_bytes(body)
        with patch('tg_triage.thinking.request_text_bytes',side_effect=measure):
            p.process(incident()[0])
        self.assertFalse(p.github.calls);self.assertEqual(1,len(http.calls));self.assertEqual(1,len(p.notices))
        self.assertEqual('context_budget_exhausted',p.classifier.last_ingress_outcome)

    def test_review_foreign_source_rejected_before_any_tool_effect(self):
        http=FakeHTTP(response(wire([463]),wire([464,465,466])),
                      response(wire([463,464,465,466,999])))
        p,_=self.pipeline(http)
        for m in incident():p.store.enqueue(m)
        p.process(incident()[0])
        self.assertFalse(p.github.calls);self.assertEqual(1,len(p.notices));self.assertEqual(2,len(http.calls))
        events=[json.loads(r[0]) for r in p.store.connection.execute('SELECT record FROM thinking_decision_events')]
        reviews=[e['confirmed'] for e in events if e['intent']=='ingress_review']
        self.assertEqual(['proposed','rejected'],[r['outcome'] for r in reviews])
        self.assertEqual('invalid_ingress_output',reviews[-1]['reason'])

    def test_review_can_keep_independent_login_separate_from_booking(self):
        proposal=response(wire([463,464,465,466]),wire([467],title='Lỗi đăng nhập',summary='Đăng nhập thất bại.'))
        http=FakeHTTP(proposal,{'message':{'content':json.dumps({'reviews':[{'group_index':0,'verdict':'approve'},{'group_index':1,'verdict':'approve'}]})}});p,_=self.pipeline(http)
        sources=incident()+[message(467,text='Yêu cầu riêng: đăng nhập đúng mật khẩu vẫn bị từ chối.',mentions_listener=False)]
        for m in sources:p.store.enqueue(m)
        p.process(sources[0])
        self.assertEqual(2,len(p.github.calls));self.assertEqual([463,467],[m for m,_ in p.notices])
        self.assertNotEqual(p.store.get(sources[0].chat_id,463)['case_key'],p.store.get(sources[0].chat_id,467)['case_key'])

    def test_direct_api_filters_optional_pixels_preserves_exact_reply_and_metadata(self):
        with TemporaryDirectory() as folder:
            path=Path(folder)/'safe.jpg';path.write_bytes(JPG)
            choices=[dict(candidate_id='c1',pending=True,exact_reply=False,messages=[message(461,has_image=True)]),
                     dict(candidate_id='c2',pending=True,exact_reply=True,reply_source_ids=[463],messages=[message(450,has_image=True)])]
            http=FakeHTTP(response(wire([463],action='CONTINUE',candidate='c2')))
            classifier=Classifier(config().ollama,opener=http)
            source=message(463,reply_to_id=450,has_image=True,is_forward=True)
            classifier.analyze_ingress([source],choices,images={461:path,450:path,463:path})
            packet=json.loads(json.loads(http.calls[0][0].data)['messages'][1]['content'])
            self.assertEqual([463,450],[m['message_id'] for m in packet['images_order']])
            row=dict(zip(packet['source_columns'],packet['sources'][0]))
            self.assertEqual(NOW.isoformat(),packet['source_defaults']['received_at'])
            self.assertEqual(0,row['received_at_offset_us']);self.assertTrue(row['is_forward'])
            self.assertEqual(2,len(packet['candidates']));self.assertEqual(1,len(http.calls))

    def test_twenty_distinct_arrival_times_and_forward_hints_fit_budget(self):
        sources=[message(i,received_at=NOW+timedelta(seconds=i*2),is_forward=i%2==0) for i in range(1,21)]
        http=FakeHTTP(response(wire(list(range(1,21)))))
        classifier=Classifier(config().ollama,opener=http)
        classifier.analyze_ingress(sources,[])
        self.assertEqual(1,len(http.calls))
        body=json.loads(http.calls[0][0].data)
        self.assertLessEqual(request_text_bytes(body),8000)
        packet=json.loads(body['messages'][1]['content'])
        base=datetime.fromisoformat(packet['source_defaults']['received_at'])
        for original,values in zip(sources,packet['sources']):
            row=dict(zip(packet['source_columns'],values))
            self.assertEqual(original.received_at,base+timedelta(microseconds=row['received_at_offset_us']))
            self.assertEqual(original.is_forward,row['is_forward'])

    def test_mixed_timezone_and_naive_hints_do_not_invent_time_identity(self):
        sources=[message(1,received_at=NOW),message(2,received_at=NOW.astimezone(timezone(timedelta(hours=7)))),
                 message(3,received_at=NOW.replace(tzinfo=None))]
        http=FakeHTTP(response(wire([1,2,3])));classifier=Classifier(config().ollama,opener=http)
        classifier.analyze_ingress(sources,[])
        packet=json.loads(json.loads(http.calls[0][0].data)['messages'][1]['content'])
        rows=[dict(zip(packet['source_columns'],values)) for values in packet['sources']]
        self.assertEqual([0,0,NOW.replace(tzinfo=None).isoformat()],[r['received_at_offset_us'] for r in rows])

    def test_emotional_comment_chat_creates_no_standalone_ticket(self):
        analysis=draft([464],verdict='noise',category='chat',actionable=False)
        store=Store(':memory:');self.addCleanup(store.close)
        p=Pipeline(replace(config('live'),autonomous_enabled=True),store,
                   Router([route([464],'CHAT',analysis=analysis)]),FakeGitHub())
        p.process(incident()[1]);self.assertFalse(p.github.calls)

    def test_exact_reply_candidate_pixels_keep_current_first_under_download_cap(self):
        http=FakeHTTP(response(wire([463],action='CONTINUE',candidate='c1')))
        p,download=self.pipeline(http,cap=1)
        source=message(463,text='Cùng lỗi đăng nhập trước, đây là ảnh mới.',has_image=True,reply_to_id=461,mentions_listener=False)
        p.process(source)
        self.assertEqual([463],[mid for _,mid in download.calls])
        packet=json.loads(json.loads(http.calls[0][0].data)['messages'][1]['content'])
        self.assertEqual([463],[m['message_id'] for m in packet['images_order']])
        self.assertTrue(packet['candidates'][0]['exact_reply'])

    def test_exact_reply_candidate_pixels_available_when_cap_allows(self):
        http=FakeHTTP(response(wire([463],action='CONTINUE',candidate='c1')))
        p,download=self.pipeline(http,cap=2)
        p.process(message(463,text='Ảnh mới cùng lỗi trước',has_image=True,reply_to_id=461,mentions_listener=False))
        self.assertEqual([463,461],[mid for _,mid in download.calls])
        packet=json.loads(json.loads(http.calls[0][0].data)['messages'][1]['content'])
        self.assertEqual([463,461],[m['message_id'] for m in packet['images_order']])
