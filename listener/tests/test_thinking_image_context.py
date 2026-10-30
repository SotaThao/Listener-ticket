"""Caption/image partitioning through the production planner and real provider payloads."""
import base64
import json
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import asdict,replace
from pathlib import Path

from tg_triage.classifier import Classifier,TOPIC_SCHEMA,load_prompt
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tg_triage.thinking import pack_messages,request_text_bytes
from tg_triage.provider import build_provider
from tg_triage.ai_transport import ProviderError
from tests.helpers import config,message,topic,FakeHTTP,FakeDownloader,FakeUploader,JPG,NOW
from tests.test_followups import GitHub

CAPTION='Sau này mình cho chế độ nầy thì mình sẽ design đổi bảng nầy'
BACKEND=('Câu hỏi thông tin ASP.NET: JWT và refresh token hoạt động như thế nào? '
         'Trả lời: access token có hạn; refresh token dùng để cấp lại token. ')*45


def envelope(*groups):
    return {'message':{'content':json.dumps({'groups':[asdict(g) for g in groups]},ensure_ascii=False)}}


class ThinkingImageTests(unittest.TestCase):
    def hosted_repair_pipeline(self, responses, *, enabled=True):
        store=Store(':memory:',clock=lambda:NOW);self.addCleanup(store.close)
        cfg=replace(config('live',window=2,max_messages=3),thinking_enabled=enabled)
        cfg=replace(cfg,model=replace(cfg.model,provider='deepseek',name='deepseek-flash',think=False,
            cloud_consent=True,cloud_image_consent=True,fallback='ollama',fallback_think=False))
        provider=build_provider(cfg);gh=GitHub();sent=[]
        p=Pipeline(cfg,store,provider,gh,notice_sender=lambda c,m,t:sent.append((m,t)) or 1000+m,
            downloader=FakeDownloader({442:(JPG,'source.jpg')}),uploader=FakeUploader())
        with patch('tg_triage.ai_transport.read_key',return_value='fake'), \
             patch('tg_triage.thinking_transport.complete',side_effect=responses) as request:
            self.feed(p)
        return store,gh,sent,request

    @staticmethod
    def hosted_groups():
        return {'groups':[asdict(g) for g in (
            topic((442,),category='task',type='task',title='Thiết kế lại Salon information & visibility',summary='Thiết kế khi chọn Chat only'),
            topic((443,),category='feedback',type='question'),topic((444,),category='chat',verdict='noise'))]}

    def test_hosted_format_failure_retries_same_bundle_before_local(self):
        for failure in (ProviderError('invalid_json'),{}):
            with self.subTest(failure=type(failure).__name__):
                store,gh,sent,request=self.hosted_repair_pipeline([failure,self.hosted_groups()])
                self.assertEqual([c.args[0] for c in request.call_args_list],['deepseek','deepseek'])
                self.assertEqual(request.call_args_list[0].args[3:5],request.call_args_list[1].args[3:5])
                self.assertEqual(len(gh.calls),1);self.assertEqual([m for m,_ in sent],[442])
                self.assertEqual(store.get(self.sources()[0].chat_id,443)['decision'],'chat')
                record=json.loads(store.connection.execute('SELECT record FROM thinking_decisions').fetchone()[0])
                self.assertEqual(record['budget']['model_calls_used'],2)

    def test_hosted_network_failure_keeps_second_slot_for_local(self):
        _,gh,sent,request=self.hosted_repair_pipeline([ProviderError('timeout'),self.hosted_groups()])
        self.assertEqual([c.args[0] for c in request.call_args_list],['deepseek','ollama'])
        self.assertEqual(len(gh.calls),1);self.assertEqual([m for m,_ in sent],[442])

    def test_two_hosted_format_failures_stop_without_third_or_local_call(self):
        store,gh,sent,request=self.hosted_repair_pipeline([ProviderError('invalid_json'),{}])
        self.assertEqual([c.args[0] for c in request.call_args_list],['deepseek','deepseek'])
        self.assertEqual(len(gh.calls),1);self.assertNotIn('ASP.NET',gh.calls[0].body)
        self.assertEqual([m for m,_ in sent],[442,443,444])
        self.assertEqual(store.get(self.sources()[0].chat_id,442)['decision'],'needs_human')
        self.assertEqual(store.get(self.sources()[0].chat_id,443)['decision'],'pending_review')

    def make(self,*responses,downloader=None):
        store=Store(':memory:',clock=lambda:NOW);self.addCleanup(store.close)
        cfg=config('live',window=2,max_messages=3)
        http=FakeHTTP(*responses);model=Classifier(cfg.ollama,opener=http);gh=GitHub();sent=[]
        p=Pipeline(cfg,store,model,gh,notice_sender=lambda c,m,t:sent.append((m,t)) or 1000+m,
            downloader=downloader or FakeDownloader({442:(JPG,'source.jpg')}),uploader=FakeUploader())
        return store,http,model,gh,sent,p

    def sources(self):
        return [message(442,text=CAPTION,has_image=True,image_size=len(JPG),image_mime='image/jpeg',sender_id=7,mentions_listener=False),
                message(443,text=BACKEND,sender_id=7,mentions_listener=False),
                message(444,text='dạ được anh',sender_id=7,mentions_listener=False)]

    def feed(self,p):
        for m in self.sources():p.process(m)

    def test_actual_caption_long_backend_ack_fits_and_sends_image_not_merged(self):
        groups=[topic((442,),category='task',type='task',title='Thiết kế lại Salon information & visibility',
            summary='Thiết kế lại bảng khi chọn Chat only',actionable=True),
            topic((443,),category='feedback',type='question'),topic((444,),category='chat',type='task',verdict='noise')]
        store,http,_,gh,sent,p=self.make(envelope(*groups));self.feed(p)
        self.assertEqual(len(http.calls),1);self.assertEqual(len(gh.calls),1)
        body=json.loads(http.calls[0][0].data)
        self.assertEqual([m['role'] for m in body['messages']],['system','user'])
        self.assertLessEqual(request_text_bytes(body),8000)
        self.assertEqual(base64.b64decode(body['messages'][1]['images'][0]),JPG)
        packet=json.loads(body['messages'][-1]['content'])
        self.assertEqual(packet['source_ids'],[442,443,444])
        self.assertEqual(packet['images_order'],[dict(image_index=0,message_id=442,id_ref='[442]')])
        self.assertEqual(packet['sources'][0]['text'],CAPTION)
        self.assertTrue(packet['sources'][1]['text_truncated'])
        self.assertEqual(packet['sources'][2]['text'],'dạ được anh')
        self.assertNotIn('ASP.NET',gh.calls[0].body);self.assertNotIn('dạ được anh',gh.calls[0].body)
        self.assertIn('Chat only',gh.calls[0].body)
        self.assertEqual([s[0] for s in sent],[442])
        self.assertEqual(store.get(self.sources()[0].chat_id,443)['decision'],'chat')

    def test_single_caption_image_fits_default_projection(self):
        store,http,_,gh,_,p=self.make(envelope(topic((442,),category='task',type='task')))
        p.config=replace(p.config,aggregation=replace(p.config.aggregation,max_messages_per_case=1))
        p.process(self.sources()[0]);self.assertEqual(len(http.calls),1)
        self.assertEqual(len(gh.calls),1)
        self.assertLessEqual(request_text_bytes(json.loads(http.calls[0][0].data)),8000)

    def test_two_images_captions_reply_edges_and_independent_work_share_one_user_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            first=Path(directory)/'first.jpg';first.write_bytes(JPG)
            second=Path(directory)/'second.jpg';second.write_bytes(JPG+b'second')
            sources=[message(11,text='Thiết kế bảng thứ nhất',has_image=True,mentions_listener=False),
                message(12,text='Sửa trạng thái bảng thứ hai',has_image=True,reply_to_id=11,mentions_listener=False),
                message(13,text='Việc độc lập: xuất báo cáo CSV',mentions_listener=False)]
            rendered=pack_messages(load_prompt('thinking-topics.md'),sources,{11:first,12:second},TOPIC_SCHEMA)
            self.assertEqual([m['role'] for m in rendered],['system','user'])
            user=rendered[1];packet=json.loads(user['content'])
            self.assertEqual(packet['images_order'],[
                dict(image_index=0,message_id=11,id_ref='[11]'),dict(image_index=1,message_id=12,id_ref='[12]')])
            self.assertEqual([s['text'] for s in packet['sources']],[m.text for m in sources])
            self.assertEqual(packet['sources'][1]['reply_to_id'],11)
            self.assertEqual([base64.b64decode(x) for x in user['images']],[JPG,JPG+b'second'])
            cfg=config('live');cfg=replace(cfg,model=replace(cfg.model,provider='deepseek',name='deepseek-flash',
                think=False,cloud_consent=True,cloud_image_consent=True,fallback='none'))
            cloud=build_provider(cfg)._cloud_messages(rendered)
            self.assertEqual([m['role'] for m in cloud],['system','user'])
            parts=cloud[1]['content'];self.assertEqual([x['type'] for x in parts],['text','image_url','image_url'])
            self.assertEqual(json.loads(parts[0]['text'])['images_order'],packet['images_order'])
            self.assertEqual([base64.b64decode(x['image_url']['url'].split(',',1)[1]) for x in parts[1:]],
                             [JPG,JPG+b'second'])

    def test_uncertain_image_group_does_not_swallow_independent_task_or_chat(self):
        question='Anh/Chị muốn đổi các trường nào ngoài phần đã xác nhận?'
        store,_,_,gh,sent,p=self.make(envelope(
            topic((442,),verdict='needs_human',category='feedback',type='feature',title='Thiết kế bảng',missing_info=(question,)),
            topic((443,),category='task',type='task',title='Việc độc lập'),topic((444,),category='chat',verdict='noise')))
        self.feed(p);self.assertEqual(len(gh.calls),2)
        self.assertNotIn('ASP.NET',gh.calls[0].body)
        self.assertEqual([s[0] for s in sent],[442,443]);self.assertIn(question,sent[0][1])

    def test_invalid_partition_isolates_sources_and_pending_reply_creates_fresh_intake(self):
        invalid=envelope(topic((999,)))
        store,http,_,gh,sent,p=self.make(invalid,invalid,envelope(topic((445,),category='task',type='task',title='Sửa refresh token')))
        self.feed(p)
        self.assertEqual(len(gh.calls),1)
        self.assertNotIn('ASP.NET',gh.calls[0].body)
        self.assertEqual([s[0] for s in sent],[442,443,444])
        pending=dict(store.get(self.sources()[0].chat_id,443))
        self.assertEqual(pending['decision'],'pending_review')
        self.assertIn('chưa gộp',sent[1][1])
        reply=message(445,text='Nhờ sửa refresh token ở trang đăng nhập để phiên được gia hạn.',
                      reply_to_id=1443,sender_id=7,mentions_listener=False)
        result=p.process(reply)
        self.assertEqual(result.decision,'issue');self.assertEqual(len(gh.calls),2)
        self.assertNotEqual(store.get(reply.chat_id,445)['case_key'],pending['case_key'])
        self.assertEqual(dict(store.get(reply.chat_id,443)),pending)
        self.assertIsNone(p.process(reply));self.assertEqual(len(gh.calls),2)

    def test_pending_direct_source_requires_sent_notice_and_foreign_ids_do_not_authorize(self):
        store,_,_,_,_,p=self.make(None,None);self.feed(p)
        original=self.sources()[0]
        direct=message(445,reply_to_id=443,sender_id=7)
        self.assertIsNotNone(store.pending_context_source(direct))
        self.assertIsNone(store.pending_context_source(replace(direct,chat_id=-123)))
        self.assertIsNone(store.pending_context_source(replace(direct,reply_to_id=999999)))
        self.assertIsNone(store.pending_context_source(replace(direct,reply_to_id=1442)))
        with store.connection:
            store.connection.execute("UPDATE group_notices SET status='failed' WHERE chat_id=? AND message_id=443",(original.chat_id,))
        self.assertIsNone(store.pending_context_source(direct))

    def test_image_reply_edge_contradiction_only_falls_back_affected_component(self):
        store,_,_,gh,sent,p=self.make(envelope(topic((442,),category='chat',verdict='noise'),
            topic((443,),category='task',type='task'),topic((444,),category='chat',verdict='noise')))
        sources=self.sources();sources[1]=replace(sources[1],text='Thiết kế bảng trong ảnh này',reply_to_id=442)
        for m in sources:p.process(m)
        self.assertEqual(len(gh.calls),1);self.assertIn('Thiết kế bảng trong ảnh',gh.calls[0].body)
        self.assertNotIn('dạ được anh',gh.calls[0].body);self.assertEqual([s[0] for s in sent],[442])

    def test_unavailable_pixels_keep_text_question_and_independent_task(self):
        store,http,_,gh,sent,p=self.make(envelope(topic((442,),category='feedback',type='question',verdict='needs_human'),
            topic((443,),category='task',type='task'),topic((444,),category='chat',verdict='noise')),downloader=FakeDownloader())
        self.feed(p);self.assertEqual(len(http.calls),1);self.assertEqual(len(gh.calls),1)
        body=json.loads(http.calls[0][0].data)
        self.assertFalse(any('images' in m for m in body['messages']))
        self.assertFalse(json.loads(body['messages'][-1]['content'])['sources'][0]['pixels_available'])
        self.assertEqual(store.get(self.sources()[0].chat_id,442)['decision'],'chat')
        self.assertEqual([s[0] for s in sent],[443])

    def test_failure_preserves_explicit_command_without_unrelated_sources(self):
        store,_,_,gh,sent,p=self.make(None,None)
        sources=self.sources();sources[1]=replace(sources[1],text='/task Sửa refresh token')
        # Command closes the current window immediately; source444 is outside this case.
        for m in sources[:2]:p.process(m)
        self.assertEqual(len(gh.calls),2)
        self.assertEqual(gh.calls[1].labels[0],'task')
        self.assertNotIn(CAPTION,gh.calls[1].body)

    def test_image_command_failure_preserves_type_but_asks_for_unresolved_scope(self):
        _,_,_,gh,sent,p=self.make(None,None)
        result=p.process(replace(self.sources()[0],text='/task Sửa phần trong ảnh này'))
        self.assertEqual(result.decision,'needs_human')
        self.assertIn('task',gh.calls[0].labels)
        self.assertIn('needs-human',gh.calls[0].labels)
        self.assertIn('chưa nhận được phân tích',sent[0][1])

    def test_packing_retains_large_ids_reply_metadata_and_full_source_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'img.jpg';path.write_bytes(JPG)
            sources=[replace(m,message_id=1234567800+i,reply_to_id=1234567800 if i else None) for i,m in enumerate(self.sources())]
            rendered=pack_messages(load_prompt('thinking-topics.md'),sources,{1234567800:path},TOPIC_SCHEMA)
            self.assertLessEqual(request_text_bytes(dict(messages=rendered,format=TOPIC_SCHEMA)),8000)
            packet=json.loads(rendered[-1]['content'])
            self.assertEqual(packet['sources'][1]['reply_ref'],'[1234567800]')
            self.assertTrue(packet['sources'][1]['text_truncated'])

    def test_cloud_image_consent_off_sends_text_only_and_preserves_caption_intent(self):
        store=Store(':memory:',clock=lambda:NOW);self.addCleanup(store.close)
        cfg=config('live',window=2,max_messages=3)
        cfg=replace(cfg,model=replace(cfg.model,provider='deepseek',name='deepseek-flash',think=False,
            base_url='https://api.deepseek.com',cloud_consent=True,cloud_image_consent=False,fallback='none'))
        gh=GitHub();provider=build_provider(cfg)
        p=Pipeline(cfg,store,provider,gh,uploader=FakeUploader(),downloader=FakeDownloader({442:(JPG,'photo.jpg')}))
        groups=[topic((442,),category='task',type='task',title='Thiết kế bảng đã nêu',summary='Yêu cầu thiết kế từ caption'),
            topic((443,),category='feedback',type='question'),topic((444,),category='chat',verdict='noise')]
        with patch('tg_triage.ai_transport.read_key',return_value='fake'), \
             patch('tg_triage.thinking_transport.complete',return_value={'groups':[asdict(g) for g in groups]}) as request:
            self.feed(p)
        self.assertEqual(request.call_count,1)
        self.assertFalse(any('images' in m or isinstance(m.get('content'),list) for m in request.call_args.args[3]))
        packet=json.loads(request.call_args.args[3][-1]['content'])
        self.assertFalse(packet['sources'][0]['pixels_available'])
        self.assertEqual(len(gh.calls),1)
        self.assertNotIn('Salon',gh.calls[0].title)

    def test_model_cannot_turn_bare_image_group_into_task_beside_text_task(self):
        store,_,_,gh,sent,p=self.make(envelope(topic((442,),category='task',type='task'),
            topic((443,),category='task',type='task'),topic((444,),category='chat',verdict='noise')))
        sources=self.sources();sources[0]=replace(sources[0],text='')
        for m in sources:p.process(m)
        self.assertEqual(store.get(sources[0].chat_id,442)['decision'],'pending_review')
        self.assertEqual(len(gh.calls),1);self.assertEqual([s[0] for s in sent],[443])

    def test_rejected_vision_provider_bare_image_does_not_dispatch_or_create(self):
        store,http,model,gh,sent,p=self.make(envelope(topic((442,),category='task',type='task')))
        model.supports_vision=False;model.rejects_images=True
        p.config=replace(p.config,aggregation=replace(p.config.aggregation,max_messages_per_case=1))
        result=p.process(replace(self.sources()[0],text=''))
        self.assertEqual(result.decision,'pending_review')
        self.assertEqual(http.calls,[]);self.assertEqual(gh.calls,[]);self.assertEqual(sent,[])


if __name__=='__main__':unittest.main()
