import unittest
import json

from tg_triage.rules_intake import early_decision, evaluate_batch
from tests.helpers import message
from tests.test_rules_bootstrap import RulesBootstrapTests
from types import SimpleNamespace
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, patch
import asyncio
from pathlib import Path
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tg_triage.telegram_io import enqueue_event, worker, _TICK
from tg_triage.pacing import RELEASED_SOURCES
from tg_triage.rules_pending_store import RulesHalt
from tests.helpers import config, FakeGitHub, JPG


class RulesIntakeFactsTests(RulesBootstrapTests):
    def test_rules_sender_refuses_non_ops_but_legacy_sender_is_unchanged(self):
        store = Store(':memory:'); self.addCleanup(store.close)
        sent = []
        sender = lambda *args: sent.append(args) or 77
        settings = replace(config(), decision_rules_enabled=True,
                           telegram=replace(config().telegram, ops_chat_id=-1009876543210))
        pipeline = Pipeline(settings, store, object(), rules_snapshot=self.snapshot(), notice_sender=sender)
        with self.assertLogs('tg_triage.pipeline', level='ERROR'), self.assertRaises(RulesHalt):
            pipeline.notice_sender(-123, 42, 'text')
        self.assertEqual(sent, [])
        self.assertEqual(pipeline.notice_sender(-1009876543210, None, 'text'), 77)
        legacy = Pipeline(replace(settings, decision_rules_enabled=False), store, object(), notice_sender=sender)
        self.assertIs(legacy.notice_sender, sender)
        legacy.notice_sender(-123, 42, 'legacy')
        self.assertEqual(sent[-1], (-123, 42, 'legacy'))

    def test_basic_group_question_has_fallback_header(self):
        class Provider:
            settings = SimpleNamespace(model='test'); think = False
            def _ask(self, body, parse, attempts):
                return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},"object":{"value":"nút này","source_ids":[42]}}}]}')
        store = Store(':memory:'); self.addCleanup(store.close)
        settings = replace(config(), mode='live', autonomous_enabled=True, decision_rules_enabled=True,
                           telegram=replace(config().telegram, ops_chat_id=-1009876543210))
        sent = []
        pipeline = Pipeline(settings, store, Provider(), rules_snapshot=self.snapshot(),
                            notice_sender=lambda *args: sent.append(args) or 77)
        pipeline.process(message(42, text='nút này lỗi', chat_id=-12345))
        self.assertEqual(sent[0][:2], (-1009876543210, None))
        self.assertIn('Tin gốc: (group thường, không có link) · mã tin 42', sent[0][2])

    def test_model_independent_table_rows_precede_extraction_failure(self):
        snapshot=self.snapshot()
        for source, expected in ((message(text='/question test',reply_to_id=5),'CHAT'),
                                 (message(text='/task'),'CHAT'),
                                 (message(text='',has_document=True),'MEDIA_ONLY'),
                                 (message(text='',has_image=True),'MEDIA_ONLY')):
            with self.subTest(source=source):
                self.assertEqual(early_decision(snapshot,[source]).state,expected)
        self.assertIsNone(early_decision(snapshot,[message(text='cần sửa POS')]))
        self.assertIsNone(early_decision(snapshot,[message(text='',has_image=True,reply_to_id=3)]))
        self.assertIsNone(early_decision(snapshot,[message(1,text=''),message(2,text='bổ sung')]))

    def test_unseen_image_does_not_supply_facts_absent_from_caption(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},"object":{"value":"POS","source_ids":[42]},"observed":{"value":"mã lỗi 123","source_ids":[42]}}}]}')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot)
        result=pipeline.process(message(42,text='POS bị lỗi',has_image=True))
        self.assertEqual((result.decision,result.reason),('pending_review','R12'))
        result=pipeline.process(message(42,text='POS mã lỗi 123',has_image=True,chat_id=-1001234567891))
        self.assertEqual((result.decision,result.reason),('dry_run_issue','R9'))

    def test_batch_validates_all_groups_before_returning_any_decisions(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def __init__(self,payload): self.payload=payload; self.calls=0
            def _ask(self,body,parse,attempts): self.calls+=1; return parse(self.payload)
        provider=Provider('{"groups":[{"message_ids":[1],"facts":{"intent":{"value":"chat","source_ids":[1]},"object":{"value":"POS","source_ids":[1]},"observed":{"value":"kẹt giấy","source_ids":[1]},"confidence":0}},{"message_ids":[2],"facts":{"intent":{"value":"chat","source_ids":[2]}}}]}')
        result=evaluate_batch(snapshot,provider,[message(1,text='/bug POS kẹt giấy'),message(2,text='cảm ơn')])
        # "cảm ơn" is small talk: the local rule F4 decides it before the model's chat/R7 (Owner 2026-11-06).
        self.assertEqual([(d.state,d.rule) for _,d in result],[('READY','R9'),('CHAT','F4')])
        provider.payload='{"groups":[{"message_ids":[1],"facts":{}},{"message_ids":[2],"facts":{"expected":null}}]}'
        self.assertIsNone(evaluate_batch(snapshot,provider,[message(1),message(2)]))
        provider.calls=0
        self.assertEqual(evaluate_batch(snapshot,provider,[message(text='/question sao?')])[0][1].state,'CHAT')
        self.assertEqual(provider.calls,0)
        provider.payload='{"groups":[{"message_ids":[1],"facts":{}},{"message_ids":[2],"facts":{}}]}'
        self.assertIsNone(evaluate_batch(snapshot,provider,[message(1,has_image=True),message(2,reply_to_id=1)]))

    def test_pipeline_early_cases_do_not_call_provider_or_side_effects(self):
        snapshot=self.snapshot()
        class Provider:
            def __getattr__(self,key): raise AssertionError('provider must not be called')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True)
        sent=[]
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,notice_sender=lambda *args: sent.append(args))
        for source,expected in ((message(1,text='/question test'),'chat'),(message(2,text='/task'),'chat'),
                                (message(3,text='',has_document=True),'pending_review')):
            self.assertEqual(pipeline.process(source).decision,expected)
        self.assertEqual(sent,[])
        self.assertEqual(store.connection.execute('SELECT count(*) FROM rules_decisions').fetchone()[0],3)

    def test_pipeline_need_info_sends_only_ops_question(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},"object":{"value":"nút này","source_ids":[42]}}}]}')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        sent=[]
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,
                          notice_sender=lambda *args: sent.append(args) or 77)
        result=pipeline.process(message(42,text='nút này lỗi',chat_title='Nhóm POS'))
        self.assertEqual(result.decision,'pending_review')
        self.assertEqual(len(sent),1)
        self.assertEqual(sent[0][:2],(-1009876543210,None))
        self.assertIn('Nhóm POS',sent[0][2])
        self.assertIn('https://t.me/c/1234567890/42',sent[0][2])
        self.assertEqual(pipeline.rules_pending.bind_reply(-1009876543210,77,42,7),('-1001234567890:42',2))

    def test_pipeline_ready_uses_rules_and_existing_github_reservation(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},"object":{"value":"POS","source_ids":[42]},"observed":{"value":"kẹt giấy","source_ids":[42]},"confidence":0}}]}')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        sent=[]; github=FakeGitHub()
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,
                          notice_sender=lambda *args: sent.append(args) or 77)
        result=pipeline.process(message(42,text='POS kẹt giấy',mentions_listener=False,chat_title='Nhóm POS'))
        self.assertEqual((result.decision,result.issue_number),('issue',42))
        self.assertEqual(len(github.calls),1)
        self.assertEqual(sent[0][:2],(-1009876543210,None))
        self.assertIn('https://github.com/sandbox/triage/issues/42',sent[0][2])
        self.assertIsNotNone(store.get(-1001234567890,42)['github_called_at'])

    def test_ops_reply_completes_single_topic_with_real_event_provenance(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1
                if self.calls==1:
                    return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]}}}]}')
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[2]},"observed":{"value":"kẹt giấy","source_ids":[2]}}}]}')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        sent=[]; github=FakeGitHub()
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *args: sent.append(args) or (77+len(sent)))
        source=message(42,text='bị lỗi',chat_title='Nhóm POS')
        self.assertEqual(pipeline.process(source).decision,'pending_review')
        async def reply():
            queue=asyncio.Queue()
            e=SimpleNamespace(out=False,is_private=False,is_group=True,chat_id=-1009876543210,id=42,
                raw_text='POS kẹt giấy',date=source.received_at,sender_id=1,reply_to_msg_id=78,reply_to=None,
                sticker=None,file=None,photo=None,document=None,fwd_from=None,message=None,
                get_sender=AsyncMock(return_value=SimpleNamespace(first_name='B',last_name=None)),
                get_chat=AsyncMock(return_value=SimpleNamespace(title='Ops',username=None)))
            await enqueue_event(e,queue,settings.telegram.chat_ids,store,ops_chat_id=settings.telegram.ops_chat_id,
                                rules_pending=pipeline.rules_pending,pending_expiry_days=7)
            return queue.get_nowait()
        result=pipeline.process(asyncio.run(reply()))
        self.assertEqual((result.decision,result.issue_number),('issue',42))
        self.assertEqual(len(github.calls),1)
        self.assertIn('https://t.me/c/1234567890/42',github.calls[0].body)
        self.assertIn('https://t.me/c/9876543210/42',github.calls[0].body)
        journal=store.connection.execute('SELECT present_sources FROM rules_decisions ORDER BY id DESC').fetchone()[0]
        self.assertIn('"chat_id":-1009876543210,"message_id":42',journal)
        self.assertTrue(all(item[0] == settings.telegram.ops_chat_id for item in sent))

    def test_second_ops_answer_during_extraction_forces_fresh_evaluation(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1
                ids=json.loads(body['messages'][1]['content'])['source_ids']
                if self.calls==1: return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]}}}]}')
                if self.calls==2: self.assert_admitted=pipeline.rules_pending.accept_ops_message(second,7)
                return parse(json.dumps({'groups':[{'message_ids':ids,'facts':{'intent':{'value':'bug','source_ids':[1]},'object':{'value':'POS','source_ids':[2]},'observed':{'value':'kẹt giấy','source_ids':[2]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider(); github=FakeGitHub(); sent=[]
        pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a) or 77+len(sent))
        pipeline.process(message(42,text='bị lỗi'))
        first=message(51,text='POS kẹt giấy',chat_id=settings.telegram.ops_chat_id,reply_to_id=78)
        second=message(52,text='thêm chi tiết',chat_id=settings.telegram.ops_chat_id,reply_to_id=78)
        self.assertTrue(pipeline.rules_pending.accept_ops_message(first,7))
        result=pipeline.process(first)
        self.assertTrue(provider.assert_admitted)
        self.assertEqual(result.reason,'rules_pending_changed')
        self.assertEqual(github.calls,[])
        result=pipeline.process(second)
        self.assertEqual((result.decision,result.issue_number),('issue',42))
        self.assertEqual(len(github.calls),1)
        self.assertIn('https://t.me/c/9876543210/52',github.calls[0].body)

    def test_closed_topic_with_inflight_effect_retains_answer_but_closed_alone_ignores(self):
        snapshot=self.snapshot(); store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,store,SimpleNamespace(),rules_snapshot=snapshot)
        pending=pipeline.rules_pending; source=message(10,text='lỗi'); store.enqueue(source)
        topic=f'{source.chat_id}:10'; pending.create(topic,source.chat_id,10)
        seq=pending.reserve_notice(topic,settings.telegram.ops_chat_id,'question'); pending.finish_notice(topic,seq,100)
        self.assertTrue(pending.claim_topic(topic,pending.topic_snapshot(topic)))
        pending.close(topic,'CLOSED')
        late=message(101,text='thêm',chat_id=settings.telegram.ops_chat_id,reply_to_id=100)
        self.assertTrue(pending.accept_ops_message(late,7))
        self.assertTrue(pending.is_deferred_ops_reply(late.chat_id,late.message_id))
        self.assertTrue(pending.release_topic(topic))
        later=message(102,text='sau đó',chat_id=settings.telegram.ops_chat_id,reply_to_id=100)
        self.assertFalse(pending.accept_ops_message(later,7))
        self.assertIsNone(store.get(later.chat_id,later.message_id))

    def test_answer_admitted_after_real_ticket_close_before_claim_release(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1
                if self.calls==1: return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]}}}]}')
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[2]},"observed":{"value":"kẹt giấy","source_ids":[2]}}}]}')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); sent=[]
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a) or 77+len(sent))
        pipeline.process(message(42,text='bị lỗi'))
        first=message(51,text='POS kẹt giấy',chat_id=settings.telegram.ops_chat_id,reply_to_id=78)
        late=message(52,text='thêm chi tiết',chat_id=settings.telegram.ops_chat_id,reply_to_id=78)
        self.assertTrue(pipeline.rules_pending.accept_ops_message(first,7))
        real_close=pipeline.rules_pending.close; admitted=[]
        def close_with_reply(topic,status):
            result=real_close(topic,status)
            if status=='CLOSED': admitted.append(pipeline.rules_pending.accept_ops_message(late,7))
            return result
        with patch.object(pipeline.rules_pending,'close',side_effect=close_with_reply): result=pipeline.process(first)
        self.assertEqual((result.decision,result.issue_number),('issue',42))
        self.assertEqual(admitted,[True])
        self.assertTrue(pipeline.rules_pending.is_deferred_ops_reply(late.chat_id,late.message_id))
        self.assertEqual(pipeline.process(late).reason,'rules_reply_deferred_during_effect')
        later=message(53,text='sau đó',chat_id=settings.telegram.ops_chat_id,reply_to_id=78)
        self.assertFalse(pipeline.rules_pending.accept_ops_message(later,7))
        self.assertEqual(len(github.calls),1)

    def test_completed_issue_claim_recovers_without_reposting(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1
                if self.calls==1: return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]}}}]}')
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[2]},"observed":{"value":"kẹt giấy","source_ids":[2]}}}]}')
        from tg_triage.rules_pending_store import check_before_recovery
        db=Path(self.temp.name)/'claimed.sqlite3'; store=Store(db)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); sent=[]; provider=Provider()
        pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 77+len(sent))
        pipeline.process(message(42,text='bị lỗi'))
        answer=message(51,text='POS kẹt giấy',chat_id=settings.telegram.ops_chat_id,reply_to_id=78)
        self.assertTrue(pipeline.rules_pending.accept_ops_message(answer,7))
        with patch.object(pipeline.rules_pending,'close',side_effect=RuntimeError('interrupted after notice')):
            with self.assertRaisesRegex(RuntimeError,'interrupted after notice'): pipeline.process(answer)
        self.assertEqual(len(github.calls),1); self.assertEqual(len(sent),2)
        self.assertEqual(store.get(-1001234567890,42)['decision'],'issue')
        store.close(); check_before_recovery(db)
        store=Store(db); self.addCleanup(store.close)
        pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,notice_sender=lambda *a: 100)
        self.assertEqual(pipeline.rules_pending.topic_snapshot('-1001234567890:42')['status'],'CLOSED')
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM rules_inflight_topics').fetchone()[0],0)
        self.assertIsNone(pipeline.process(answer))
        self.assertEqual(len(github.calls),1)

    def test_hold_retries_survive_restart_and_escalate_once(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts): self.calls+=1; return None
        provider=Provider(); path=Path(self.temp.name)/'hold.sqlite3'
        store=Store(path)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        sent=[]; github=FakeGitHub()
        def build(db): return Pipeline(settings,db,provider,github,rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a) or 99)
        pipeline=build(store)
        result=pipeline.process(message(42,text='cần sửa POS',chat_title='Nhóm POS'))
        self.assertEqual(result.reason,'rules_hold')
        pipeline.close_due_cases(); store.close()
        store=Store(path); self.addCleanup(store.close); pipeline=build(store)
        pipeline.close_due_cases(); pipeline.close_due_cases(); pipeline.close_due_cases(); pipeline.close_due_cases()
        self.assertEqual(provider.calls,4)
        self.assertEqual(len(sent),1)
        self.assertIn('sau 3 lần thử lại',sent[0][2])
        self.assertEqual(github.calls,[])

    def test_ops_image_and_source_image_with_colliding_ids_are_uploaded(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=True
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1
                if self.calls==1: return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]}}}]}')
                self.images_seen=len(body['messages'][1].get('images',[]))
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[2]},"observed":{"value":"kẹt giấy","source_ids":[2]}}}]}')
        class Uploader:
            def __init__(self): self.files=[]
            def ensure_release(self,tag): pass
            def upload(self,tag,path,asset_name): self.files.append(asset_name); return 'https://example.com/'+asset_name
        def downloader(chat_id,message_id,directory):
            path=directory/'image.jpg'; path.write_bytes(JPG); return path
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider(); uploader=Uploader(); github=FakeGitHub(); sent=[]
        pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,downloader=downloader,uploader=uploader,
                          notice_sender=lambda *a: sent.append(a) or 77+len(sent))
        source=message(42,text='lỗi này',has_image=True,chat_title='Nhóm POS')
        self.assertEqual(pipeline.process(source).decision,'pending_review')
        async def reply():
            queue=asyncio.Queue()
            e=SimpleNamespace(out=False,is_private=False,is_group=True,chat_id=-1009876543210,id=42,
                raw_text='POS kẹt giấy',date=source.received_at,sender_id=1,reply_to_msg_id=78,reply_to=None,
                sticker=None,file=SimpleNamespace(size=len(JPG),mime_type='image/jpeg'),photo=object(),document=None,fwd_from=None,message=None,
                get_sender=AsyncMock(return_value=SimpleNamespace(first_name='B',last_name=None)),
                get_chat=AsyncMock(return_value=SimpleNamespace(title='Ops',username=None)))
            await enqueue_event(e,queue,settings.telegram.chat_ids,store,ops_chat_id=settings.telegram.ops_chat_id,
                                rules_pending=pipeline.rules_pending,pending_expiry_days=7)
            return queue.get_nowait()
        result=pipeline.process(asyncio.run(reply()))
        self.assertEqual(result.issue_number,42)
        self.assertEqual(provider.images_seen,2)
        self.assertEqual(len(uploader.files),2)
        self.assertNotEqual(*uploader.files)
        self.assertEqual({tuple(row) for row in store.connection.execute('SELECT chat_id,message_id FROM attachments')},
                         {(-1001234567890,42),(-1009876543210,42)})
        self.assertIn('https://t.me/c/9876543210/42',github.calls[0].body)

    def test_reserved_github_prevents_model_retry_on_sweep(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts): self.calls+=1; return None
        provider=Provider(); store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,store,provider,rules_snapshot=snapshot)
        pipeline.process(message(42,text='cần sửa POS'))
        with store.connection:
            store.connection.execute("UPDATE messages SET github_called_at='2026-01-01' WHERE message_id=42")
        pipeline.close_due_cases()
        self.assertEqual(provider.calls,1)
        self.assertEqual(store.connection.execute('SELECT status FROM rules_topics').fetchone()[0],'ESCALATED')

    def test_batch_resume_after_first_group_without_repartition(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1
                return parse('{"groups":[{"message_ids":[1],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[1]},"observed":{"value":"kẹt giấy","source_ids":[1]}}},{"message_ids":[2],"facts":{"intent":{"value":"task","source_ids":[2]},"object":{"value":"báo cáo","source_ids":[2]},"work":{"value":"thêm CSV","source_ids":[2]}}}]}')
        class GitHub:
            def __init__(self): self.calls=[]
            def create_issue(self,issue): self.calls.append(issue); return len(self.calls),None
        path=Path(self.temp.name)/'batch.sqlite3'; store=Store(path); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider(); github=GitHub()
        notices=[]
        def send(*args): notices.append(args); return 88+len(notices)
        def build(db): return Pipeline(settings,db,provider,github,rules_snapshot=snapshot,notice_sender=send)
        pipeline=build(store); a=message(1,text='POS kẹt giấy'); b=message(2,text='thêm CSV báo cáo')
        store.enqueue(a); store.enqueue(b)
        transition=pipeline.rules_pending.transition_group
        def interrupted(batch,ordinal,old,new):
            if ordinal == 1 and old == 'PLANNED': raise RuntimeError('simulated crash')
            return transition(batch,ordinal,old,new)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,1),(a.chat_id,2)}))
        try:
            with patch.object(pipeline.rules_pending,'transition_group',side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError,'simulated crash'): pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual((provider.calls,len(github.calls)),(1,1))
        store.close(); store=Store(path); self.addCleanup(store.close); pipeline=build(store)
        pipeline.close_due_cases()
        self.assertEqual((provider.calls,len(github.calls)),(1,2))

    def test_actual_worker_sweep_reacts_after_batch_resume(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[1],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[1]},"observed":{"value":"kẹt giấy","source_ids":[1]}}},{"message_ids":[2],"facts":{"intent":{"value":"task","source_ids":[2]},"object":{"value":"báo cáo","source_ids":[2]},"work":{"value":"thêm CSV","source_ids":[2]}}}]}')
        class GitHub:
            def __init__(self): self.issues=[]
            def create_issue(self,issue): self.issues.append(issue); return len(self.issues),None
        path=Path(self.temp.name)/'sweep.sqlite3'; store=Store(path); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=GitHub(); pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot)
        a=message(1,text='POS kẹt giấy'); b=message(2,text='thêm CSV báo cáo'); store.enqueue(a); store.enqueue(b)
        transition=pipeline.rules_pending.transition_group
        def crash(batch,ordinal,old,new):
            if ordinal==1 and old=='PLANNED': raise RuntimeError('crash')
            return transition(batch,ordinal,old,new)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,1),(b.chat_id,2)}))
        try:
            with patch.object(pipeline.rules_pending,'transition_group',side_effect=crash):
                with self.assertRaises(RuntimeError): pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        store.close(); store=Store(path); self.addCleanup(store.close)
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot)
        async def exercise():
            queue=asyncio.Queue(); clock=[0]; seen=[]; done=asyncio.Event()
            async def receive(q,stop,timeout):
                if clock[0]==0: clock[0]=16; return _TICK
                await asyncio.sleep(60)
            async def react(chat,mid,emoji):
                seen.append((chat,mid,emoji))
                if emoji=='👌': done.set()
            task=asyncio.create_task(worker(queue,pipeline,react=react,_clock=lambda:clock[0],_receive=receive))
            try: await asyncio.wait_for(done.wait(),5)
            finally:
                task.cancel(); await asyncio.gather(task,return_exceptions=True)
            self.assertIn((-1001234567890,2,'👌'),seen)
        asyncio.run(exercise())
        self.assertEqual(len(github.issues),2)

    def test_batch_reserved_group_is_never_replayed(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1
                return parse('{"groups":[{"message_ids":[1],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[1]},"observed":{"value":"kẹt giấy","source_ids":[1]}}},{"message_ids":[2],"facts":{"intent":{"value":"task","source_ids":[2]},"object":{"value":"báo cáo","source_ids":[2]},"work":{"value":"thêm CSV","source_ids":[2]}}}]}')
        class GitHub:
            def __init__(self): self.calls=[]
            def create_issue(self,issue): self.calls.append(issue); return len(self.calls),None
        path=Path(self.temp.name)/'ambiguous.sqlite3'; store=Store(path); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider(); github=GitHub()
        def build(db): return Pipeline(settings,db,provider,github,rules_snapshot=snapshot)
        pipeline=build(store); a=message(1,text='POS kẹt giấy'); b=message(2,text='thêm CSV báo cáo')
        store.enqueue(a); store.enqueue(b); run=pipeline._run
        def interrupted(plan,case):
            if plan.first.message_id==2: raise RuntimeError('simulated crash after reservation')
            return run(plan,case)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,1),(a.chat_id,2)}))
        try:
            with patch.object(pipeline,'_run',side_effect=interrupted):
                with self.assertRaisesRegex(RuntimeError,'simulated crash'): pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        store.close(); store=Store(path); self.addCleanup(store.close)
        with self.assertRaisesRegex(RulesHalt,'Owner inspection'): build(store)
        self.assertEqual((provider.calls,len(github.calls)),(1,1))
        states=[r[0] for r in store.connection.execute('SELECT status FROM rules_batch_groups ORDER BY ordinal')]
        self.assertEqual(states,['DONE','RESERVED'])

    def test_batch_readonly_checkpoint_recovers_without_reextract(self):
        from tg_triage.rules_pending_store import check_before_recovery
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1
                return parse('{"groups":[{"message_ids":[1],"facts":{}},{"message_ids":[2],"facts":{"intent":{"value":"chat","source_ids":[2]}}}]}')
        path=Path(self.temp.name)/'readonly-batch.sqlite3'; store=Store(path)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider(); pipeline=Pipeline(settings,store,provider,rules_snapshot=snapshot)
        a=message(1,text='',has_image=True); b=message(2,text='cảm ơn'); store.enqueue(a);store.enqueue(b)
        record=pipeline.rules_pending.record_decision
        def crash(topic,decision,*args,**kwargs):
            if decision.state=='MEDIA_ONLY': raise RuntimeError('crash after claim')
            return record(topic,decision,*args,**kwargs)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,1),(b.chat_id,2)}))
        try:
            with patch.object(pipeline.rules_pending,'record_decision',side_effect=crash):
                with self.assertRaisesRegex(RuntimeError,'crash after claim'): pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        store.close(); check_before_recovery(path)
        store=Store(path); self.addCleanup(store.close); pipeline=Pipeline(settings,store,provider,rules_snapshot=snapshot)
        self.assertEqual(store.connection.execute('SELECT status FROM rules_batch_groups WHERE ordinal=0').fetchone()[0],'PLANNED')
        pipeline.close_due_cases()
        self.assertEqual(provider.calls,1)
        self.assertEqual(store.connection.execute('SELECT status FROM rules_batch_groups WHERE ordinal=0').fetchone()[0],'DONE')
        self.assertEqual(store.get(a.chat_id,1)['decision'],'pending_review')

    def test_batch_completed_issue_checkpoint_recovers_without_repost(self):
        from tg_triage.rules_pending_store import check_before_recovery
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1
                return parse('{"groups":[{"message_ids":[1],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[1]},"observed":{"value":"kẹt giấy","source_ids":[1]}}},{"message_ids":[2],"facts":{"intent":{"value":"chat","source_ids":[2]}}}]}')
        path=Path(self.temp.name)/'done-batch.sqlite3'; store=Store(path)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider(); github=FakeGitHub(); sent=[]
        def build(db): return Pipeline(settings,db,provider,github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 77+len(sent))
        pipeline=build(store); a=message(1,text='POS kẹt giấy');b=message(2,text='cảm ơn');store.enqueue(a);store.enqueue(b)
        transition=pipeline.rules_pending.transition_group
        def crash(batch,ordinal,old,new,**kwargs):
            if ordinal==0 and old=='RESERVED' and new=='DONE': raise RuntimeError('crash before checkpoint')
            return transition(batch,ordinal,old,new,**kwargs)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,1),(b.chat_id,2)}))
        try:
            with patch.object(pipeline.rules_pending,'transition_group',side_effect=crash):
                with self.assertRaisesRegex(RuntimeError,'crash before checkpoint'): pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual((len(github.calls),len(sent)),(1,1)); store.close(); check_before_recovery(path)
        store=Store(path); self.addCleanup(store.close); pipeline=build(store)
        self.assertEqual(store.connection.execute('SELECT status FROM rules_batch_groups WHERE ordinal=0').fetchone()[0],'DONE')
        pipeline.close_due_cases()
        self.assertEqual((provider.calls,len(github.calls),len(sent)),(1,1,1))
        self.assertEqual(store.get(b.chat_id,2)['decision'],'chat')

    def test_batch_unknown_issue_post_rejects_restart_without_mutation(self):
        from tg_triage.rules_pending_store import check_before_recovery
        import hashlib
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[1],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[1]},"observed":{"value":"kẹt giấy","source_ids":[1]}}},{"message_ids":[2],"facts":{"intent":{"value":"chat","source_ids":[2]}}}]}')
        class GitHub:
            def __init__(self): self.posts=0
            def create_issue(self,issue): self.posts+=1; raise TimeoutError('response lost')
        path=Path(self.temp.name)/'unknown-post.sqlite3';store=Store(path)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=GitHub();pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:80)
        a=message(1,text='POS kẹt giấy');b=message(2,text='cảm ơn');store.enqueue(a);store.enqueue(b)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,1),(b.chat_id,2)}))
        try:
            with self.assertRaises(RulesHalt): pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual(github.posts,1); store.close(); before=hashlib.sha256(path.read_bytes()).digest()
        with self.assertRaises(RulesHalt): check_before_recovery(path)
        self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),before)

    def test_completed_pending_chat_checkpoint_resumes_sibling_after_restart(self):
        from tg_triage.rules_pending_store import check_before_recovery
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1; ids=json.loads(body['messages'][1]['content'])['source_ids']
                if ids==[10]: return parse('{"groups":[{"message_ids":[10],"facts":{"intent":{"value":"bug","source_ids":[10]}}}]}')
                if set(ids)=={11,12}: return parse('{"groups":[{"message_ids":[11],"facts":{"intent":{"value":"chat","source_ids":[11]}}},{"message_ids":[12],"facts":{"intent":{"value":"bug","source_ids":[12]},"object":{"value":"POS","source_ids":[12]},"observed":{"value":"kẹt giấy","source_ids":[12]}}}]}')
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"chat","source_ids":[2]}}}]}')
        path=Path(self.temp.name)/'chat-checkpoint.sqlite3'; store=Store(path)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); provider=Provider(); sent=[]
        def build(db): return Pipeline(settings,db,provider,github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 77+len(sent))
        pipeline=build(store);pipeline.process(message(10,text='lỗi'))
        a=message(11,text='chỉ trao đổi',reply_to_id=10);b=message(12,text='POS kẹt giấy');store.enqueue(a);store.enqueue(b)
        transition=pipeline.rules_pending.transition_group
        def crash(batch,ordinal,old,new,**kwargs):
            if ordinal==0 and old=='RESERVED' and new=='DONE': raise RuntimeError('after chat close')
            return transition(batch,ordinal,old,new,**kwargs)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,11),(b.chat_id,12)}))
        try:
            with patch.object(pipeline.rules_pending,'transition_group',side_effect=crash):
                with self.assertRaisesRegex(RuntimeError,'after chat close'): pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        calls=provider.calls;self.assertEqual(pipeline.rules_pending.topic_snapshot(f'{a.chat_id}:10')['status'],'CLOSED')
        store.close();check_before_recovery(path);store=Store(path);self.addCleanup(store.close);pipeline=build(store)
        self.assertEqual(store.connection.execute('SELECT status FROM rules_batch_groups WHERE ordinal=0').fetchone()[0],'DONE')
        pipeline.close_due_cases();self.assertEqual(provider.calls,calls);self.assertEqual(len(github.calls),1)
        self.assertEqual(store.get(b.chat_id,12)['decision'],'issue')

    def test_completed_append_with_two_replies_recovers_without_reposting(self):
        from tg_triage.rules_pending_store import check_before_recovery
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1;ids=json.loads(body['messages'][1]['content'])['source_ids']
                if ids==[42]: return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},"object":{"value":"POS","source_ids":[42]},"observed":{"value":"kẹt giấy","source_ids":[42]}}}]}')
                return parse('{"groups":[{"message_ids":[43,44],"facts":{"intent":{"value":"bug","source_ids":[43]}}},{"message_ids":[45],"facts":{"intent":{"value":"chat","source_ids":[45]}}}]}')
        class GitHub:
            def __init__(self):self.issues=[];self.comments=[]
            def create_issue(self,issue):self.issues.append(issue);return 42,None
            def lifecycle_snapshot(self,n):return {'number':n,'repo':'sandbox/triage','state':'open','observed_at':'now'}
            def add_comment(self,n,body):self.comments.append((n,body));return 101
        path=Path(self.temp.name)/'append-checkpoint.sqlite3';store=Store(path)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider();github=GitHub();sent=[]
        def build(db):return Pipeline(settings,db,provider,github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 77+len(sent))
        pipeline=build(store);pipeline.process(message(42,text='POS kẹt giấy'))
        a=message(43,text='thêm 1',reply_to_id=42);b=message(44,text='thêm 2',reply_to_id=42);c=message(45,text='cảm ơn')
        for m in (a,b,c):store.enqueue(m)
        transition=pipeline.rules_pending.transition_group
        def crash(batch,ordinal,old,new,**kwargs):
            if ordinal==0 and old=='RESERVED' and new=='DONE':raise RuntimeError('after comment')
            return transition(batch,ordinal,old,new,**kwargs)
        token=RELEASED_SOURCES.set(frozenset((m.chat_id,m.message_id) for m in (a,b,c)))
        try:
            with patch.object(pipeline.rules_pending,'transition_group',side_effect=crash):
                with self.assertRaisesRegex(RuntimeError,'after comment'):pipeline.process(a)
        finally:RELEASED_SOURCES.reset(token)
        calls=provider.calls;self.assertEqual(len(github.comments),1);store.close();check_before_recovery(path)
        store=Store(path);self.addCleanup(store.close);pipeline=build(store)
        self.assertEqual(store.connection.execute('SELECT status FROM rules_batch_groups WHERE ordinal=0').fetchone()[0],'DONE')
        pipeline.close_due_cases();self.assertEqual(provider.calls,calls);self.assertEqual(len(github.comments),1)
        self.assertEqual(store.get(c.chat_id,45)['decision'],'chat')

    def test_release_read_timeout_does_not_become_ambiguous_write(self):
        import subprocess
        from tg_triage.attachments import GhCliUploader
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test');think=False;supports_vision=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},"object":{"value":"POS","source_ids":[42]},"observed":{"value":"kẹt giấy","source_ids":[42]}}}]}')
        calls=[]
        def runner(argv,**kwargs):calls.append(argv);raise subprocess.TimeoutExpired('gh',1)
        def download(chat_id,message_id,directory):
            out=directory/'image.jpg';out.write_bytes(JPG);return out
        store=Store(':memory:');self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub();uploader=GhCliUploader('sandbox/triage','token',runner=runner,executable='gh')
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,uploader=uploader,downloader=download)
        result=pipeline.process(message(42,text='POS kẹt giấy',has_image=True))
        self.assertEqual((result.decision,result.issue_number),('issue',42));self.assertEqual(len(github.calls),1)
        self.assertEqual([call[1:3] for call in calls],[['release','view']])
        self.assertEqual(store.connection.execute('SELECT status FROM attachments').fetchone()[0],'prewrite_error')
        self.assertFalse(pipeline.rules_pending.unresolved_ambiguity())

    def test_unknown_image_and_document_upload_halt_worker_and_restart(self):
        from tg_triage.rules_pending_store import check_before_recovery
        import hashlib
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                return parse(json.dumps({'groups':[{'message_ids':[mid],'facts':{'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},'observed':{'value':'kẹt giấy','source_ids':[mid]}}}]}))
        class Uploader:
            def __init__(self): self.side_effects=[]
            def ensure_release(self,tag): pass
            def upload(self,tag,path,name): self.side_effects.append(name); raise TimeoutError('response lost')
        for kind in ('image','document'):
            with self.subTest(kind=kind):
                path=Path(self.temp.name)/(kind+'-upload.sqlite3'); store=Store(path)
                settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,telegram=replace(config().telegram,ops_chat_id=-1009876543210))
                uploader=Uploader(); github=FakeGitHub(); notices=[]
                def download(chat_id,message_id,directory):
                    out=directory/('image.jpg' if kind=='image' else 'document.pdf'); out.write_bytes(JPG if kind=='image' else b'pdf'); return out
                pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,uploader=uploader,downloader=download,notice_sender=lambda *a:notices.append(a) or 80)
                kwargs={'has_image':True,'has_document':True,'document_name':'proof.pdf','document_mime':'application/pdf'} if kind=='image' else {'has_document':True,'document_name':'proof.pdf','document_mime':'application/pdf'}
                a=message(42,text='POS kẹt giấy',**kwargs); b=message(1,text='khác',chat_id=-1001234567891)
                async def exercise():
                    queue=asyncio.Queue(); store.enqueue(a);store.enqueue(b);queue.put_nowait(a);queue.put_nowait(b); clock=[0]
                    async def receive(q,stop,timeout):
                        if not q.empty(): return await q.get()
                        if clock[0]==0: clock[0]=31; return _TICK
                        await asyncio.sleep(60)
                    with self.assertRaisesRegex(RulesHalt,'attachment outcome'):
                        await asyncio.wait_for(worker(queue,pipeline,_clock=lambda:clock[0],_receive=receive),5)
                asyncio.run(exercise())
                self.assertEqual(len(uploader.side_effects),1); self.assertEqual(github.calls,[])
                self.assertEqual(store.get(b.chat_id,b.message_id)['decision'],'queued')
                self.assertEqual(store.connection.execute('SELECT status FROM attachments').fetchone()[0],'ambiguous')
                self.assertEqual(len(notices),1); self.assertIn('tệp đính kèm',notices[0][2])
                store.close(); before=hashlib.sha256(path.read_bytes()).digest()
                with self.assertRaises(RulesHalt): check_before_recovery(path)
                self.assertEqual(hashlib.sha256(path.read_bytes()).digest(),before)

    def test_unsupported_attachment_is_local_skip_not_ambiguous_write(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},"object":{"value":"POS","source_ids":[42]},"observed":{"value":"kẹt giấy","source_ids":[42]}}}]}')
        class Uploader:
            def ensure_release(self,*args): raise AssertionError('unsupported file is not uploaded')
            def upload(self,*args): raise AssertionError('unsupported file is not uploaded')
        store=Store(':memory:');self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=FakeGitHub(); pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,uploader=Uploader())
        result=pipeline.process(message(42,text='POS kẹt giấy',has_document=True,document_name='run.exe',document_mime='application/octet-stream'))
        self.assertEqual((result.decision,result.issue_number),('issue',42))
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM attachments').fetchone()[0],0)
        self.assertFalse(pipeline.rules_pending.unresolved_ambiguity())

    def test_verified_open_reply_appends_once_and_closed_reply_holds(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                ids=json.loads(body['messages'][1]['content'])['source_ids']; mid=ids[0]
                return parse(json.dumps({'groups':[{'message_ids':[mid],'facts':{
                    'intent':{'value':'bug','source_ids':[mid]},
                    'object':{'value':'POS','source_ids':[mid]},
                    'observed':{'value':'kẹt giấy','source_ids':[mid]}}}]}))
        class GitHub:
            state='open'
            def __init__(self): self.issues=[]; self.comments=[]
            def create_issue(self,issue): self.issues.append(issue); return 42,None
            def lifecycle_snapshot(self,n): return {'number':n,'repo':'sandbox/triage','state':self.state,'observed_at':'2026-01-01'}
            def add_comment(self,n,body): self.comments.append((n,body)); return 101
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        sent=[]; github=GitHub()
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a) or 100+len(sent))
        source=message(42,text='POS kẹt giấy',chat_title='Nhóm POS')
        self.assertEqual(pipeline.process(source).issue_number,42)
        reply=message(43,text='vẫn bị',reply_to_id=42,chat_title='Nhóm POS')
        self.assertEqual(pipeline.process(reply).decision,'followup')
        self.assertEqual(len(github.comments),1)
        self.assertEqual(github.comments[0][0],42)
        self.assertIn('https://t.me/c/1234567890/43',github.comments[0][1])
        self.assertTrue(all(item[0]==settings.telegram.ops_chat_id for item in sent))
        github.state='closed'
        result=pipeline.process(message(44,text='vẫn bị',reply_to_id=42,chat_title='Nhóm POS'))
        self.assertEqual(result.reason,'closed_target_not_supported')
        self.assertEqual((len(github.comments),len(github.issues)),(1,1))

    def test_unknown_comment_result_is_not_retried(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1; mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                return parse(json.dumps({'groups':[{'message_ids':[mid],'facts':{'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},'observed':{'value':'kẹt giấy','source_ids':[mid]}}}]}))
        class GitHub:
            def __init__(self): self.comments=0
            def create_issue(self,issue): return 42,None
            def lifecycle_snapshot(self,n): return {'number':n,'repo':'sandbox/triage','state':'open','observed_at':'2026-01-01'}
            def add_comment(self,n,body): self.comments+=1; raise OSError('unknown outcome')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider(); github=GitHub()
        pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot)
        pipeline.process(message(42,text='POS kẹt giấy'))
        result=pipeline.process(message(43,text='vẫn bị',reply_to_id=42))
        self.assertEqual(result.reason,'comment_ambiguous')
        with self.assertRaises(RulesHalt): pipeline.close_due_cases()
        self.assertEqual((github.comments,provider.calls),(1,2))
        self.assertEqual(pipeline.rules_pending.comment_status(-1001234567890,43),'ambiguous')

    def test_worker_halts_on_ambiguous_comment_and_preserves_other_queue(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                return parse(json.dumps({'groups':[{'message_ids':[mid],'facts':{'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},'observed':{'value':'kẹt giấy','source_ids':[mid]}}}]}))
        class GitHub:
            def create_issue(self,issue): return 42,None
            def lifecycle_snapshot(self,n): return {'number':n,'repo':'sandbox/triage','state':'open','observed_at':'now'}
            def add_comment(self,n,body): raise OSError('unknown outcome')
        path=Path(self.temp.name)/'halt.sqlite3'; store=Store(path); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        notices=[]; counter=[70]
        def send(*args): counter[0]+=1; notices.append(args); return counter[0]
        pipeline=Pipeline(settings,store,Provider(),GitHub(),rules_snapshot=snapshot,notice_sender=send)
        pipeline.process(message(42,text='POS kẹt giấy')); notices.clear()
        async def exercise():
            queue=asyncio.Queue(); a=message(43,text='vẫn bị',reply_to_id=42); b=message(1,text='việc khác',chat_id=-1001234567891)
            store.enqueue(a); store.enqueue(b); queue.put_nowait(a); queue.put_nowait(b)
            clock=[0]
            async def receive(q,stop,timeout):
                if not q.empty(): return await q.get()
                if clock[0]==0: clock[0]=31; return _TICK
                await asyncio.sleep(60)
            with self.assertRaisesRegex(RulesHalt,'Owner inspection'):
                await asyncio.wait_for(worker(queue,pipeline,_clock=lambda:clock[0],_receive=receive),5)
            self.assertEqual(store.get(b.chat_id,b.message_id)['decision'],'queued')
        asyncio.run(exercise())
        self.assertEqual(len(notices),1)
        self.assertEqual(notices[0][0],settings.telegram.ops_chat_id)
        self.assertIn('chưa xác nhận',notices[0][2])
        self.assertEqual(store.connection.execute("SELECT status FROM rules_notices WHERE kind='error'").fetchone()[0],'sent')
        store.close(); store=Store(path); self.addCleanup(store.close)
        with self.assertRaises(RulesHalt): Pipeline(settings,store,Provider(),GitHub(),rules_snapshot=snapshot)

    def test_ambiguous_batch_halts_and_retains_untouched_planned_group(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[1],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[1]},"observed":{"value":"kẹt giấy","source_ids":[1]}}},{"message_ids":[2],"facts":{"intent":{"value":"task","source_ids":[2]},"object":{"value":"báo cáo","source_ids":[2]},"work":{"value":"thêm CSV","source_ids":[2]}}}]}')
        class GitHub:
            def __init__(self): self.calls=0
            def create_issue(self,issue): self.calls+=1; raise OSError('unknown result')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        notices=[]; github=GitHub()
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a: notices.append(a) or 80)
        a=message(1,text='POS kẹt giấy'); b=message(2,text='thêm CSV báo cáo')
        store.enqueue(a); store.enqueue(b)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,1),(b.chat_id,2)}))
        try:
            with self.assertRaises(RulesHalt): pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual([r[0] for r in store.connection.execute('SELECT status FROM rules_batch_groups ORDER BY ordinal')],['AMBIGUOUS','PLANNED'])
        self.assertEqual(github.calls,1)
        self.assertEqual(len(notices),1)
        self.assertEqual(notices[0][0],settings.telegram.ops_chat_id)
        with self.assertRaises(RulesHalt): pipeline.process(message(3,text='việc khác'))

    def test_ambiguous_notice_stops_before_next_batch_group(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[1],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[1]},"observed":{"value":"kẹt giấy","source_ids":[1]}}},{"message_ids":[2],"facts":{"intent":{"value":"task","source_ids":[2]},"object":{"value":"báo cáo","source_ids":[2]},"work":{"value":"thêm CSV","source_ids":[2]}}}]}')
        class GitHub:
            def __init__(self): self.calls=0
            def create_issue(self,issue): self.calls+=1; return 42,None
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        sent=[]; github=GitHub()
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a))
        a=message(1,text='POS kẹt giấy'); b=message(2,text='thêm CSV báo cáo')
        store.enqueue(a); store.enqueue(b)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,1),(b.chat_id,2)}))
        try:
            with self.assertRaises(RulesHalt): pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual(github.calls,1)
        self.assertEqual(len(sent),1)
        self.assertEqual([r[0] for r in store.connection.execute('SELECT status FROM rules_batch_groups ORDER BY ordinal')],['RESERVED','PLANNED'])
        self.assertEqual(store.connection.execute('SELECT status FROM rules_notices').fetchone()[0],'ambiguous')

    def test_notice_completion_failure_halts_even_when_transport_succeeds(self):
        snapshot=self.snapshot(); store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,store,SimpleNamespace(),rules_snapshot=snapshot,notice_sender=lambda *a: 123)
        source=message(41,text='lỗi'); store.enqueue(source); topic=f'{source.chat_id}:41'
        pipeline.rules_pending.create(topic,source.chat_id,source.message_id)
        from tg_triage.rules_intake import _send_ops
        with patch.object(pipeline.rules_pending,'finish_notice',return_value=False):
            with self.assertRaisesRegex(RulesHalt,'notice outcome'): _send_ops(pipeline,source,topic,'question','cần thêm')
        self.assertEqual(store.connection.execute('SELECT status FROM rules_notices').fetchone()[0],'reserved')

    def test_sweep_stops_at_unknown_question_notice_before_next_topic(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1
                if self.calls<=2: return None
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                if self.calls==3:
                    return parse(json.dumps({'groups':[{'message_ids':[mid],'facts':{'intent':{'value':'bug','source_ids':[mid]}}}]}))
                return parse(json.dumps({'groups':[{'message_ids':[mid],'facts':{'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},'observed':{'value':'kẹt giấy','source_ids':[mid]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider(); github=FakeGitHub(); sent=[]
        pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a))
        a=message(41,text='lỗi A'); b=message(42,text='POS kẹt giấy',chat_id=-1001234567891)
        pipeline.process(a); pipeline.process(b)
        with self.assertRaisesRegex(RulesHalt,'notice outcome'): pipeline.close_due_cases()
        self.assertEqual(provider.calls,3)
        self.assertEqual(github.calls,[])
        self.assertEqual(store.get(b.chat_id,b.message_id)['decision'],'pending_review')
        self.assertEqual(len(sent),1)
        self.assertEqual(store.connection.execute('SELECT status FROM rules_notices').fetchone()[0],'ambiguous')

    def test_batch_reply_authority_is_scoped_to_its_own_topic(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                ids=json.loads(body['messages'][1]['content'])['source_ids']
                if ids==[42]:
                    return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},"object":{"value":"POS","source_ids":[42]},"observed":{"value":"kẹt giấy","source_ids":[42]}}}]}')
                return parse('{"groups":[{"message_ids":[43],"facts":{"intent":{"value":"bug","source_ids":[43]}}},{"message_ids":[44],"facts":{"intent":{"value":"task","source_ids":[44]},"object":{"value":"báo cáo","source_ids":[44]},"work":{"value":"thêm CSV","source_ids":[44]}}}]}')
        class GitHub:
            def __init__(self): self.issues=[]; self.comments=[]
            def create_issue(self,issue): self.issues.append(issue); return 41+len(self.issues),None
            def lifecycle_snapshot(self,n): return {'number':n,'repo':'sandbox/triage','state':'open','observed_at':'2026-01-01'}
            def add_comment(self,n,body): self.comments.append((n,body)); return 101
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=GitHub(); sent=[]
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a) or 77+len(sent))
        pipeline.process(message(42,text='POS kẹt giấy'))
        a=message(43,text='vẫn bị',reply_to_id=42); b=message(44,text='thêm CSV báo cáo')
        store.enqueue(a); store.enqueue(b)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,43),(b.chat_id,44)}))
        try: pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual(len(github.comments),1)
        self.assertEqual(github.comments[0][0],42)
        self.assertEqual(len(github.issues),2)
        self.assertIn('thêm CSV',github.issues[1].body)
        self.assertTrue(all(item[0]==settings.telegram.ops_chat_id for item in sent))

    def test_batch_done_target_closes_before_planned_group_resume(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1; ids=json.loads(body['messages'][1]['content'])['source_ids']
                if ids==[42]: return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},"object":{"value":"POS","source_ids":[42]},"observed":{"value":"kẹt giấy","source_ids":[42]}}}]}')
                return parse('{"groups":[{"message_ids":[43],"facts":{"intent":{"value":"bug","source_ids":[43]}}},{"message_ids":[44],"facts":{"intent":{"value":"task","source_ids":[44]},"object":{"value":"báo cáo","source_ids":[44]},"work":{"value":"thêm CSV","source_ids":[44]}}}]}')
        class GitHub:
            state='open'
            def __init__(self): self.issues=[]; self.comments=[]
            def create_issue(self,issue): self.issues.append(issue); return 41+len(self.issues),None
            def lifecycle_snapshot(self,n): return {'number':n,'repo':'sandbox/triage','state':self.state,'observed_at':'now'}
            def add_comment(self,n,body): self.comments.append((n,body)); return 101
        path=Path(self.temp.name)/'done-target.sqlite3'; store=Store(path); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider(); github=GitHub(); sent=[]
        def build(db): return Pipeline(settings,db,provider,github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 900+len(sent))
        pipeline=build(store); pipeline.process(message(42,text='POS kẹt giấy'))
        a=message(43,text='vẫn bị',reply_to_id=42); b=message(44,text='thêm CSV báo cáo')
        store.enqueue(a); store.enqueue(b); transition=pipeline.rules_pending.transition_group
        def crash(batch,ordinal,old,new):
            if ordinal==1 and old=='PLANNED': raise RuntimeError('crash before reserve')
            return transition(batch,ordinal,old,new)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,43),(b.chat_id,44)}))
        try:
            with patch.object(pipeline.rules_pending,'transition_group',side_effect=crash):
                with self.assertRaises(RuntimeError): pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual((len(github.comments),len(github.issues)),(1,1))
        store.close(); github.state='closed'; store=Store(path); self.addCleanup(store.close)
        build(store).close_due_cases()
        self.assertEqual((len(github.comments),len(github.issues),provider.calls),(1,2,2))

    def test_two_ops_answers_in_one_release_keep_separate_topic_proofs(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                packet=json.loads(body['messages'][1]['content']); ids=packet['source_ids']
                if len(ids)==1:
                    mid=ids[0]
                    return parse(json.dumps({'groups':[{'message_ids':ids,'facts':{'intent':{'value':'bug','source_ids':[mid]}}}]}))
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[2]},"observed":{"value":"kẹt giấy","source_ids":[2]}}}]}')
        class GitHub:
            def __init__(self): self.issues=[]
            def create_issue(self,issue): self.issues.append(issue); return len(self.issues),None
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=GitHub(); sent=[]
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a) or 77+len(sent))
        pipeline.process(message(42,text='lỗi',chat_id=-1001234567890))
        pipeline.process(message(42,text='lỗi',chat_id=-1001234567891))
        async def replies():
            queue=asyncio.Queue()
            for mid,ref in ((42,78),(102,79)):
                e=SimpleNamespace(out=False,is_private=False,is_group=True,chat_id=settings.telegram.ops_chat_id,id=mid,
                    raw_text='POS kẹt giấy',date=message().received_at,sender_id=1,reply_to_msg_id=ref,reply_to=None,
                    sticker=None,file=None,photo=None,document=None,fwd_from=None,message=None,
                    get_sender=AsyncMock(return_value=SimpleNamespace(first_name='B',last_name=None)),
                    get_chat=AsyncMock(return_value=SimpleNamespace(title='Ops',username=None)))
                await enqueue_event(e,queue,settings.telegram.chat_ids,store,ops_chat_id=settings.telegram.ops_chat_id,
                                    rules_pending=pipeline.rules_pending,pending_expiry_days=7)
            return [queue.get_nowait(),queue.get_nowait()]
        answers=asyncio.run(replies())
        token=RELEASED_SOURCES.set(frozenset((m.chat_id,m.message_id) for m in answers))
        try:
            for answer in answers: pipeline.process(answer)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual(len(github.issues),2)
        self.assertIn('https://t.me/c/1234567890/42',github.issues[0].body)
        self.assertNotIn('https://t.me/c/1234567891/42',github.issues[0].body)
        self.assertIn('https://t.me/c/1234567891/42',github.issues[1].body)
        self.assertNotIn('https://t.me/c/1234567890/42',github.issues[1].body)

    def test_actual_worker_reacts_to_two_source_chats_not_ops(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                ids=json.loads(body['messages'][1]['content'])['source_ids']
                if len(ids)==1:
                    mid=ids[0]; return parse(json.dumps({'groups':[{'message_ids':ids,'facts':{'intent':{'value':'bug','source_ids':[mid]}}}]}))
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[2]},"observed":{"value":"kẹt giấy","source_ids":[2]}}}]}')
        class GitHub:
            def __init__(self): self.issues=[]
            def create_issue(self,issue): self.issues.append(issue); return len(self.issues),None
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=GitHub(); sent=[]
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a) or 77+len(sent))
        for chat in (-1001234567890,-1001234567891): pipeline.process(message(42,text='lỗi',chat_id=chat))
        async def exercise():
            queue=asyncio.Queue()
            for mid,ref in ((42,78),(102,79)):
                e=SimpleNamespace(out=False,is_private=False,is_group=True,chat_id=settings.telegram.ops_chat_id,id=mid,
                    raw_text='POS kẹt giấy',date=message().received_at,sender_id=1,reply_to_msg_id=ref,reply_to=None,
                    sticker=None,file=None,photo=None,document=None,fwd_from=None,message=None,
                    get_sender=AsyncMock(return_value=SimpleNamespace(first_name='B',last_name=None)),
                    get_chat=AsyncMock(return_value=SimpleNamespace(title='Ops',username=None)))
                await enqueue_event(e,queue,settings.telegram.chat_ids,store,ops_chat_id=settings.telegram.ops_chat_id,
                                    rules_pending=pipeline.rules_pending,pending_expiry_days=7)
            clock=[0]; seen=[]; done=asyncio.Event()
            async def receive(q,stop,timeout):
                if not q.empty(): return await q.get()
                if clock[0]==0: clock[0]=31; return _TICK
                await asyncio.sleep(60)
            async def react(chat,mid,emoji):
                seen.append((chat,mid,emoji))
                if sum(1 for _,_,emoji in seen if emoji=='👌') == 2: done.set()
            task=asyncio.create_task(worker(queue,pipeline,react=react,_clock=lambda:clock[0],_receive=receive))
            try: await asyncio.wait_for(done.wait(),5)
            finally:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            self.assertEqual({(chat,mid) for chat,mid,emoji in seen if emoji=='👌'}, {(-1001234567890,42),(-1001234567891,42)})
            self.assertTrue(all(chat != settings.telegram.ops_chat_id for chat,_,_ in seen))
        asyncio.run(exercise())

    def test_media_only_then_source_reply_retains_original_image(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[2]},"object":{"value":"POS","source_ids":[2]},"observed":{"value":"kẹt giấy","source_ids":[2]}}}]}')
        def download(chat_id,message_id,directory):
            path=directory/'image.jpg'; path.write_bytes(JPG); return path
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,downloader=download)
        self.assertEqual(pipeline.process(message(42,text='',has_image=True)).decision,'pending_review')
        result=pipeline.process(message(43,text='POS kẹt giấy',reply_to_id=42))
        self.assertEqual((result.decision,result.reason),('dry_run_issue','R9'))
        members=store.case_messages('-1001234567890:42')
        self.assertEqual([m.message_id for m in members],[42,43])
        self.assertEqual(result.images,1)

    def test_media_only_reply_to_media_only_stays_pending_without_reaction(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"unknown","source_ids":[1]}}}]}')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        reactions=[]; notices=[]
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot,
                          reactor=lambda *a: reactions.append(a),notice_sender=lambda *a: notices.append(a))
        pipeline.process(message(20,text='',has_image=True))
        result=pipeline.process(message(21,text='',has_image=True,reply_to_id=20))
        self.assertEqual((result.decision,result.reason),('pending_review','R3'))
        self.assertEqual(pipeline.rules_pending.sources('-1001234567890:20'),{1:(-1001234567890,20),2:(-1001234567890,21)})
        self.assertEqual((reactions,notices),([],[]))

    def test_batch_reply_reuses_open_pending_and_original_evidence(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                ids=json.loads(body['messages'][1]['content'])['source_ids']
                if ids==[10]: return parse('{"groups":[{"message_ids":[10],"facts":{"intent":{"value":"bug","source_ids":[10]}}}]}')
                if set(ids)=={11,12}: return parse('{"groups":[{"message_ids":[11],"facts":{"intent":{"value":"bug","source_ids":[11]}}},{"message_ids":[12],"facts":{"intent":{"value":"chat","source_ids":[12]}}}]}')
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[2]},"observed":{"value":"kẹt giấy","source_ids":[2]}}}]}')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot)
        pipeline.process(message(10,text='bị lỗi'))
        a=message(11,text='POS kẹt giấy',reply_to_id=10); b=message(12,text='cảm ơn')
        store.enqueue(a); store.enqueue(b)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,11),(b.chat_id,12)}))
        try: result=pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual(result.decision,'dry_run_issue')
        self.assertEqual(store.get(a.chat_id,11)['case_key'],'-1001234567890:10')
        self.assertEqual([m.message_id for m in store.case_messages('-1001234567890:10')],[10,11])
        self.assertEqual(store.get(a.chat_id,12)['decision'],'chat')

    def test_failed_pending_merged_extraction_keeps_original_membership(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                ids=json.loads(body['messages'][1]['content'])['source_ids']
                if ids==[10]: return parse('{"groups":[{"message_ids":[10],"facts":{"intent":{"value":"bug","source_ids":[10]}}}]}')
                if set(ids)=={11,12}: return parse('{"groups":[{"message_ids":[11],"facts":{}},{"message_ids":[12],"facts":{}}]}')
                return None
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot)
        pipeline.process(message(10,text='bị lỗi'))
        topic='-1001234567890:10'; before=pipeline.rules_pending.sources(topic)
        a=message(11,text='POS kẹt giấy',reply_to_id=10); b=message(12,text='cảm ơn')
        store.enqueue(a); store.enqueue(b); token=RELEASED_SOURCES.set(frozenset({(a.chat_id,11),(b.chat_id,12)}))
        try: result=pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual(result.reason,'rules_hold')
        self.assertEqual(pipeline.rules_pending.sources(topic),before)
        self.assertEqual((store.get(a.chat_id,11)['decision'],store.get(b.chat_id,12)['decision']),('pending_review','pending_review'))

    def test_pending_membership_change_during_reextract_holds_batch(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                ids=json.loads(body['messages'][1]['content'])['source_ids']
                if ids==[10]: return parse('{"groups":[{"message_ids":[10],"facts":{"intent":{"value":"bug","source_ids":[10]}}}]}')
                if set(ids)=={11,12}: return parse('{"groups":[{"message_ids":[11],"facts":{}},{"message_ids":[12],"facts":{}}]}')
                pipeline.rules_pending.add_source('-1001234567890:10',late.chat_id,late.message_id)
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[2]},"observed":{"value":"kẹt giấy","source_ids":[2]}}}]}')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot)
        pipeline.process(message(10,text='bị lỗi'))
        a=message(11,text='POS kẹt giấy',reply_to_id=10); b=message(12,text='cảm ơn'); late=message(13,text='late')
        for m in (a,b,late): store.enqueue(m)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,11),(b.chat_id,12)}))
        try: result=pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual(result.reason,'rules_pending_changed')
        self.assertEqual(set(pipeline.rules_pending.sources('-1001234567890:10').values()),{(a.chat_id,10),(late.chat_id,13)})
        self.assertIsNone(pipeline.rules_pending.batch(f'batch:{a.chat_id}:11'))

    def test_ops_answer_during_effect_is_durable_and_held(self):
        snapshot=self.snapshot(); store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,store,SimpleNamespace(),rules_snapshot=snapshot)
        pending=pipeline.rules_pending; source=message(10,text='lỗi')
        store.enqueue(source); topic='-1001234567890:10'; pending.create(topic,source.chat_id,10)
        seq=pending.reserve_notice(topic,settings.telegram.ops_chat_id,'question')
        self.assertTrue(pending.finish_notice(topic,seq,100))
        expected=pending.topic_snapshot(topic)
        self.assertTrue(pending.save_batch('batch:test',snapshot.fingerprint,[{}]))
        self.assertTrue(pending.transition_group('batch:test',0,'PLANNED','RESERVED',topic=topic,snapshot=expected))
        answer=message(100,text='POS kẹt giấy',chat_id=settings.telegram.ops_chat_id,reply_to_id=100)
        self.assertTrue(pending.accept_ops_message(answer,7))
        self.assertEqual(pending.topic_snapshot(topic),expected)
        self.assertTrue(pending.is_bound_ops_reply(answer.chat_id,answer.message_id))
        from tg_triage.rules_intake import _process_ops_reply
        result=_process_ops_reply(pipeline,answer)
        self.assertEqual(result.reason,'rules_reply_deferred_during_effect')
        self.assertEqual(store.get(answer.chat_id,answer.message_id)['decision'],'pending_review')

    def test_batch_close_callback_defers_ops_answer_until_owner_review(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                ids=json.loads(body['messages'][1]['content'])['source_ids']
                if ids==[10]: return parse('{"groups":[{"message_ids":[10],"facts":{"intent":{"value":"bug","source_ids":[10]}}}]}')
                if set(ids)=={11,12}: return parse('{"groups":[{"message_ids":[11],"facts":{}},{"message_ids":[12],"facts":{"intent":{"value":"chat","source_ids":[12]}}}]}')
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[2]},"observed":{"value":"kẹt giấy","source_ids":[2]}}}]}')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        sent=[]; github=FakeGitHub()
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a: sent.append(a) or 77+len(sent))
        pipeline.process(message(10,text='bị lỗi'))
        a=message(11,text='POS kẹt giấy',reply_to_id=10); b=message(12,text='cảm ơn')
        for m in (a,b): store.enqueue(m)
        late=message(51,text='thêm chi tiết',chat_id=settings.telegram.ops_chat_id,reply_to_id=78)
        real_close=pipeline.rules_pending.close; admitted=[]
        def close_with_reply(topic,status):
            result=real_close(topic,status)
            if topic==f'{a.chat_id}:10' and status=='CLOSED': admitted.append(pipeline.rules_pending.accept_ops_message(late,7))
            return result
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,11),(b.chat_id,12)}))
        try:
            with patch.object(pipeline.rules_pending,'close',side_effect=close_with_reply): result=pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual((result.decision,result.issue_number),('issue',42))
        self.assertEqual(admitted,[True]); self.assertTrue(pipeline.rules_pending.is_deferred_ops_reply(late.chat_id,late.message_id))
        self.assertEqual(pipeline.process(late).reason,'rules_reply_deferred_during_effect')
        later=message(52,text='sau đó',chat_id=settings.telegram.ops_chat_id,reply_to_id=78)
        self.assertFalse(pipeline.rules_pending.accept_ops_message(later,7))
        self.assertEqual(len(github.calls),1)

    def test_pending_change_before_batch_claim_never_records_stale_ready(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                ids=json.loads(body['messages'][1]['content'])['source_ids']
                if ids==[10]: return parse('{"groups":[{"message_ids":[10],"facts":{"intent":{"value":"bug","source_ids":[10]}}}]}')
                if set(ids)=={11,12}: return parse('{"groups":[{"message_ids":[11],"facts":{}},{"message_ids":[12],"facts":{"intent":{"value":"chat","source_ids":[12]}}}]}')
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[1]},"object":{"value":"POS","source_ids":[2]},"observed":{"value":"kẹt giấy","source_ids":[2]}}}]}')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        sent=[]; github=FakeGitHub(); pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 77+len(sent))
        pipeline.process(message(10,text='bị lỗi')); topic='-1001234567890:10'
        a=message(11,text='POS kẹt giấy',reply_to_id=10); b=message(12,text='cảm ơn')
        for m in (a,b): store.enqueue(m)
        late=message(51,text='thêm chi tiết',chat_id=settings.telegram.ops_chat_id,reply_to_id=78)
        transition=pipeline.rules_pending.transition_group; admitted=[]
        def admit_before_claim(*args,**kwargs):
            if kwargs.get('topic')==topic: admitted.append(pipeline.rules_pending.accept_ops_message(late,7))
            return transition(*args,**kwargs)
        token=RELEASED_SOURCES.set(frozenset({(a.chat_id,11),(b.chat_id,12)}))
        try:
            with patch.object(pipeline.rules_pending,'transition_group',side_effect=admit_before_claim): result=pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual(result.reason,'rules_pending_changed'); self.assertEqual(admitted,[True])
        self.assertEqual(github.calls,[])
        self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM rules_decisions WHERE topic_id=? AND state='READY'",(topic,)).fetchone()[0],0)
        self.assertEqual(store.connection.execute("SELECT status FROM rules_batch_groups WHERE ordinal=0").fetchone()[0],'PLANNED')
        self.assertEqual(store.get(a.chat_id,11)['decision'],'pending_review')

    def test_batch_pending_reextract_receives_historical_pixels(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=True
            def _ask(self,body,parse,attempts):
                ids=json.loads(body['messages'][1]['content'])['source_ids']
                if set(ids)=={11,12}: return parse('{"groups":[{"message_ids":[11],"facts":{"intent":{"value":"bug","source_ids":[11]}}},{"message_ids":[12],"facts":{"intent":{"value":"chat","source_ids":[12]}}}]}')
                self.pixel_count=len(body['messages'][1].get('images',[]))
                if not self.pixel_count: return None
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[2]},"object":{"value":"panel POS","source_ids":[1]},"observed":{"value":"mã lỗi 123","source_ids":[1]}}}]}')
        def download(chat_id,message_id,directory):
            path=directory/'image.jpg'; path.write_bytes(JPG); return path
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider(); pipeline=Pipeline(settings,store,provider,rules_snapshot=snapshot,downloader=download)
        pipeline.process(message(10,text='',has_image=True))
        a=message(11,text='lỗi này',reply_to_id=10); b=message(12,text='cảm ơn')
        store.enqueue(a); store.enqueue(b); token=RELEASED_SOURCES.set(frozenset({(a.chat_id,11),(b.chat_id,12)}))
        try: result=pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual((result.decision,provider.pixel_count),('dry_run_issue',1))
        self.assertEqual([m.message_id for m in store.case_messages('-1001234567890:10')],[10,11])

    def test_two_pending_bindings_hold_without_mutating_either_history(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                ids=json.loads(body['messages'][1]['content'])['source_ids']
                if len(ids)==1:
                    mid=ids[0]; return parse(json.dumps({'groups':[{'message_ids':[mid],'facts':{'intent':{'value':'bug','source_ids':[mid]}}}]}))
                return parse('{"groups":[{"message_ids":[11],"facts":{}},{"message_ids":[21],"facts":{}}]}')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot)
        pipeline.process(message(10,text='lỗi A')); pipeline.process(message(20,text='lỗi C'))
        before=[pipeline.rules_pending.sources(f'-1001234567890:{mid}') for mid in (10,20)]
        a=message(11,text='A',reply_to_id=10); b=message(21,text='C',reply_to_id=20)
        store.enqueue(a); store.enqueue(b); token=RELEASED_SOURCES.set(frozenset({(a.chat_id,11),(b.chat_id,21)}))
        try: result=pipeline.process(a)
        finally: RELEASED_SOURCES.reset(token)
        self.assertEqual(result.reason,'rules_hold')
        self.assertEqual([pipeline.rules_pending.sources(f'-1001234567890:{mid}') for mid in (10,20)],before)
        self.assertEqual((store.get(a.chat_id,11)['decision'],store.get(b.chat_id,21)['decision']),('pending_review','pending_review'))

    def test_unthreaded_new_message_does_not_reuse_old_media_pending(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[43],"facts":{"intent":{"value":"bug","source_ids":[43]},"object":{"value":"POS","source_ids":[43]},"observed":{"value":"kẹt giấy","source_ids":[43]}}}]}')
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot)
        pipeline.process(message(42,text='',has_image=True))
        result=pipeline.process(message(43,text='POS kẹt giấy'))
        self.assertEqual((result.decision,result.case_key,result.images),('dry_run_issue','-1001234567890:43',0))
        self.assertEqual(store.get(-1001234567890,42)['decision'],'pending_review')

    def test_append_incomplete_notice_blocks_sweep_and_restart(self):
        from tg_triage.rules_pending_store import RulesHalt, check_before_recovery
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                return parse(json.dumps({'groups':[{'message_ids':[mid],'facts':{'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},'observed':{'value':'kẹt giấy','source_ids':[mid]}}}]}))
        class GitHub:
            def __init__(self): self.comments=[]
            def create_issue(self,issue): return 42,None
            def lifecycle_snapshot(self,n): return {'number':n,'repo':'sandbox/triage','state':'open','observed_at':'now'}
            def add_comment(self,n,body): self.comments.append(body); return 101
        path=Path(self.temp.name)/'append-incomplete.sqlite3'; store=Store(path)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=GitHub(); sent=[]
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 900+len(sent))
        pipeline.process(message(42,text='POS kẹt giấy'))
        with patch('tg_triage.rules_intake._send_ops',return_value=False):
            with self.assertRaises(RulesHalt): pipeline.process(message(43,text='vẫn lỗi',reply_to_id=42))
        self.assertEqual(1,len(github.comments))
        with self.assertRaises(RulesHalt): pipeline.close_due_cases()
        store.close()
        with self.assertRaises(RulesHalt): check_before_recovery(path)
        import sqlite3
        connection=sqlite3.connect(path)
        with connection:
            connection.execute('DROP TABLE rules_append_completions')
        connection.close()
        with self.assertRaises(RulesHalt): check_before_recovery(path)
        connection=sqlite3.connect(path)
        with connection:
            connection.execute('DROP TABLE rules_sources')
        connection.close()
        with self.assertRaises(RulesHalt): check_before_recovery(path)
        self.assertEqual(1,len(github.comments))

    def test_reply_image_upload_failure_is_visible_in_comment(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=True
            def _ask(self,body,parse,attempts):
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                return parse(json.dumps({'groups':[{'message_ids':[mid],'facts':{'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},'observed':{'value':'kẹt giấy','source_ids':[mid]}}}]}))
        class GitHub:
            def __init__(self): self.comments=[]
            def create_issue(self,issue): return 42,None
            def lifecycle_snapshot(self,n): return {'number':n,'repo':'sandbox/triage','state':'open','observed_at':'2026-01-01'}
            def add_comment(self,n,body): self.comments.append(body); return 101
        def download(chat_id,message_id,directory):
            path=directory/'image.jpg'; path.write_bytes(JPG); return path
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=GitHub(); sent=[]; pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,downloader=download,uploader=None,notice_sender=lambda *a:sent.append(a) or 900+len(sent))
        pipeline.process(message(42,text='POS kẹt giấy'))
        result=pipeline.process(message(43,text='vẫn lỗi',reply_to_id=42,has_image=True))
        self.assertEqual(result.decision,'followup')
        self.assertIn('Ảnh đính kèm không tải được',github.comments[0])

    def test_short_text_document_with_caption_supplies_grounded_facts(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                packet=json.loads(body['messages'][1]['content'])
                self.asserted=packet['DOCUMENT_CONTENTS'][0]['text']
                return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"task","source_ids":[42]},"object":{"value":"màn báo cáo","source_ids":[42]},"work":{"value":"thêm CSV","source_ids":[42]}}}]}')
        def download(chat_id,message_id,directory):
            path=directory/'work.txt'; path.write_text('màn báo cáo thêm CSV',encoding='utf8'); return path
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider(); pipeline=Pipeline(settings,store,provider,rules_snapshot=snapshot,downloader=download)
        result=pipeline.process(message(42,text='Nhờ xử lý tài liệu này',has_document=True,document_name='work.txt',document_mime='text/plain'))
        self.assertEqual((result.decision,result.reason),('dry_run_issue','R10'))
        self.assertEqual(provider.asserted,'màn báo cáo thêm CSV')

    def test_unread_pdf_clear_caption_is_ready_but_document_dependent_request_holds(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts):
                mid=json.loads(body['messages'][1]['content'])['source_ids'][0]
                return parse(json.dumps({'groups':[{'message_ids':[mid],'facts':{'intent':{'value':'bug','source_ids':[mid]},'object':{'value':'POS','source_ids':[mid]},'observed':{'value':'không in được','source_ids':[mid]}}}]}))
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        pipeline=Pipeline(settings,store,Provider(),rules_snapshot=snapshot)
        clear=message(42,text='POS không in được',has_document=True,document_name='proof.pdf',document_mime='application/pdf')
        self.assertEqual(pipeline.process(clear).decision,'dry_run_issue')
        dependent=message(43,text='Sửa theo tài liệu này',has_document=True,document_name='proof.pdf',document_mime='application/pdf')
        self.assertEqual(pipeline.process(dependent).reason,'rules_hold')

    def test_unread_pdf_mixed_citations_need_text_witness(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[1,2]},"object":{"value":"POS","source_ids":[1,2]},"observed":{"value":"kẹt giấy","source_ids":[1,2]}}}]}')
        docs=[{'message_id':2,'read_status':'unsupported','byte_count':0}]
        pdf=message(2,text='xem file',has_document=True,document_name='a.pdf',document_mime='application/pdf')
        self.assertIsNone(evaluate_batch(snapshot,Provider(),[message(1,text='x'),pdf],documents=docs))
        result=evaluate_batch(snapshot,Provider(),[message(1,text='POS kẹt giấy'),pdf],documents=docs)
        self.assertEqual(result[0][1].state,'READY')

    def test_mixed_read_text_and_unread_pdf_must_witness_facts(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False
            def _ask(self,body,parse,attempts):
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"bug","source_ids":[1,2]},"object":{"value":"POS","source_ids":[1,2]},"observed":{"value":"kẹt giấy","source_ids":[1,2]}}}]}')
        from tg_triage.document_reader import read_documents
        txt=message(1,text='',has_document=True,document_name='a.txt',document_mime='text/plain')
        pdf=message(2,text='xem file',has_document=True,document_name='a.pdf',document_mime='application/pdf')
        contents=['hello']
        def download(chat_id,message_id,directory):
            path=directory/'a.txt'; path.write_text(contents[0],encoding='utf-8'); return path
        docs=read_documents([txt,pdf],download)
        self.assertIsNone(evaluate_batch(snapshot,Provider(),[txt,pdf],documents=docs))
        contents[0]='POS kẹt giấy'; docs=read_documents([txt,pdf],download)
        self.assertEqual(evaluate_batch(snapshot,Provider(),[txt,pdf],documents=docs)[0][1].state,'READY')

    def test_ops_text_document_preserves_composite_provenance(self):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1
                if self.calls==1: return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"task","source_ids":[42]}}}]}')
                doc=json.loads(body['messages'][1]['content'])['DOCUMENT_CONTENTS'][0]
                self.document_ref=doc['message_id']
                return parse('{"groups":[{"message_ids":[1,2],"facts":{"intent":{"value":"task","source_ids":[1]},"object":{"value":"màn báo cáo","source_ids":[2]},"work":{"value":"thêm CSV","source_ids":[2]}}}]}')
        class Uploader:
            def __init__(self): self.files=[]
            def ensure_release(self,tag): pass
            def upload(self,tag,path,asset_name): self.files.append(asset_name); return 'https://example.com/'+asset_name
        def download(chat_id,message_id,directory):
            path=directory/'work.txt'; path.write_text('màn báo cáo thêm CSV',encoding='utf8'); return path
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider(); uploader=Uploader(); github=FakeGitHub(); sent=[]
        pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,downloader=download,uploader=uploader,
                          notice_sender=lambda *a: sent.append(a) or 77+len(sent))
        source=message(42,text='cần làm thêm'); pipeline.process(source)
        async def reply():
            queue=asyncio.Queue()
            e=SimpleNamespace(out=False,is_private=False,is_group=True,chat_id=settings.telegram.ops_chat_id,id=42,
                raw_text='theo tài liệu',date=source.received_at,sender_id=1,reply_to_msg_id=78,reply_to=None,
                sticker=None,file=SimpleNamespace(size=100,mime_type='text/plain',name='work.txt'),photo=None,document=object(),fwd_from=None,message=None,
                get_sender=AsyncMock(return_value=SimpleNamespace(first_name='B',last_name=None)),
                get_chat=AsyncMock(return_value=SimpleNamespace(title='Ops',username=None)))
            await enqueue_event(e,queue,settings.telegram.chat_ids,store,ops_chat_id=settings.telegram.ops_chat_id,
                                rules_pending=pipeline.rules_pending,pending_expiry_days=7)
            return queue.get_nowait()
        result=pipeline.process(asyncio.run(reply()))
        self.assertEqual((result.issue_number,provider.document_ref),(42,2))
        self.assertEqual(len(uploader.files),1)
        self.assertEqual(tuple(store.connection.execute('SELECT chat_id,message_id FROM attachments').fetchone()),(settings.telegram.ops_chat_id,42))


    def test_genuine_deferred_ops_reply_raw_recovery_before_completed_binding(self):
        from tg_triage.rules_pending_store import check_before_recovery
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test');think=False;supports_vision=False
            calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1
                return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]}}}]}')
        db=Path(self.temp.name)/'deferred-raw-restart.sqlite3';store=Store(db)
        settings=replace(config(),mode='live',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider=Provider();github=FakeGitHub();sent=[]
        pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 78)
        pipeline.process(message(42,text='bị lỗi'))
        pending=pipeline.rules_pending;topic=f'{message().chat_id}:42'
        self.assertTrue(pending.claim_topic(topic,pending.topic_snapshot(topic)))
        late=message(101,text='POS kẹt giấy',chat_id=settings.telegram.ops_chat_id,reply_to_id=78,received_at=store.clock())
        self.assertTrue(pending.accept_ops_message(late,7))
        self.assertTrue(pending.is_deferred_ops_reply(late.chat_id,late.message_id))
        self.assertIsNone(store.connection.execute('SELECT 1 FROM rules_ops_replies WHERE message_id=101').fetchone())
        store.close();before=db.read_bytes();check_before_recovery(db);self.assertEqual(before,db.read_bytes())
        store=Store(db);self.addCleanup(store.close)
        restarted=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,notice_sender=lambda *a:sent.append(a) or 79)
        result=restarted.process(late)
        self.assertIsNone(result)  # Store recovery already retained the queued deferred Reply as HOLD.
        self.assertEqual(('pending_review','rules_reply_deferred_during_effect'),tuple(store.connection.execute('SELECT decision,reason FROM messages WHERE chat_id=? AND message_id=?',(late.chat_id,late.message_id)).fetchone()))
        self.assertEqual(1,provider.calls);self.assertEqual([],github.calls);self.assertEqual(1,len(sent))


class RulesPrefilterTests(RulesBootstrapTests):
    """Owner 2026-11-05 ("Duyệt lọc trước"): PR/issue review requests are CHAT before any model call (#95, #111)."""
    REVIEW = 'https://github.com/org/repo/pull/1427\nanh @​PmqQuan review giúp em pr nha'
    TASK_FACTS = ('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"task","source_ids":[42]},'
                  '"object":{"value":"pr","source_ids":[42]},"work":{"value":"review giúp em pr","source_ids":[42]}}}]}')

    def test_batch_of_review_requests_settles_chat_f1_without_model(self):
        import json
        from tg_triage.rules_intake import _process_batch
        from tg_triage.decision_rules import RulesSnapshot
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False; calls=0
            def _ask(self,body,parse,attempts): Provider.calls+=1; return parse('{}')
        class GitHub:
            calls=[]
            def create_issue(self,issue): GitHub.calls.append(issue); return 1,None
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        notices=[]
        pipeline=Pipeline(settings,store,Provider(),GitHub(),rules_snapshot=RulesSnapshot(root/'rules',root/'templates/github-ticket',extensions=True),
                          notice_sender=lambda *a:notices.append(a) or 77)
        sources=[message(41,text=self.REVIEW),message(42,text='anh merge PR này giúp em https://github.com/org/repo/pull/12')]
        for source in sources: store.enqueue(source)
        _process_batch(pipeline,sources)
        self.assertEqual((0,[],[]),(Provider.calls,GitHub.calls,notices))
        self.assertEqual({('chat','F1')},{tuple(r) for r in store.connection.execute('SELECT decision,reason FROM messages WHERE message_id IN (41,42)')})
        groups=[(json.loads(plan)['rule'],status) for plan,status in store.connection.execute('SELECT plan,status FROM rules_batch_groups')]
        self.assertEqual([('F1','DONE')],groups)

    def test_tokenizer_edges(self):
        import unicodedata
        snapshot=self.snapshot()
        self.assertTrue(snapshot.prefilter_text(unicodedata.normalize('NFD','anh duyệt giúp em nhé https://github.com/org/repo/pull/12')))
        self.assertTrue(snapshot.prefilter_text('review gi​úp em https://github.com/org/repo/pull/12'))
        self.assertFalse(snapshot.prefilter_text('anh Quân review giúp em https://github.com/org/repo/pull/12'))  # named teammate: residual, model decides
        self.assertFalse(snapshot.prefilter_text(None))

    def run_live(self, text, *, extensions, facts=None, history=(), settled_chat=(), gave_up=(), **extra):
        from tg_triage.decision_rules import RulesSnapshot
        from pathlib import Path
        root=Path(__file__).resolve().parents[1]
        snapshot=RulesSnapshot(root/'rules',root/'templates/github-ticket',extensions=extensions)
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def __init__(self): self.calls=0
            def _ask(self,body,parse,attempts):
                self.calls+=1; return parse(facts or RulesPrefilterTests.TASK_FACTS)
        class GitHub:
            def __init__(self): self.calls=[]
            def create_issue(self,issue): self.calls.append(issue); return len(self.calls),None
        store=Store(':memory:'); self.addCleanup(store.close)
        for earlier in history:
            store.enqueue(earlier)
            if earlier.message_id in settled_chat:
                from tg_triage.models import Decision as Settled
                store.finish(earlier, Settled('chat', 'rule', reason='F2'))
            if earlier.message_id in gave_up:
                from tg_triage.models import Decision as GaveUp
                store.finish(earlier, GaveUp('pending_review', 'rule', case_key='batch:%s:%s' % (earlier.chat_id, earlier.message_id), reason='rules_hold_escalated'))
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        provider,github,notices=Provider(),GitHub(),[]
        pipeline=Pipeline(settings,store,provider,github,rules_snapshot=snapshot,notice_sender=lambda *a:notices.append(a) or 77)
        result=pipeline.process(message(42,text=text,**extra))
        row=store.connection.execute("SELECT state,decision_rule,intent_rule FROM rules_decisions ORDER BY id DESC LIMIT 1").fetchone()
        return result,provider,github,notices,row

    def test_live_review_request_is_chat_without_model_or_ticket(self):
        for extensions in (False,True):
            with self.subTest(extensions=extensions):
                result,provider,github,notices,row=self.run_live(self.REVIEW,extensions=extensions)
                self.assertEqual(('chat','F1'),(result.decision,result.reason))
                self.assertEqual(0,provider.calls)
                self.assertEqual([],github.calls)
                self.assertEqual([],notices)
                self.assertEqual(('CHAT','F1','prefilter'),tuple(row))

    def test_bug_next_to_pr_link_still_reaches_model_and_ticket(self):
        bug=('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},'
             '"object":{"value":"màn Queue","source_ids":[42]},"observed":{"value":"bị trắng","source_ids":[42]}}}]}')
        result,provider,github,_,row=self.run_live('merge PR này xong màn Queue bị trắng https://github.com/org/repo/pull/12',
                                                   extensions=True,facts=bug)
        self.assertEqual(1,provider.calls)
        self.assertEqual(1,len(github.calls))
        self.assertEqual(('READY','R9'),tuple(row)[:2])

    def test_reply_or_attachment_is_out_of_prefilter_scope(self):
        from tg_triage.rules_intake import _prefilter_source
        snapshot=self.snapshot()
        self.assertTrue(_prefilter_source(snapshot,message(42,text=self.REVIEW)))
        self.assertFalse(_prefilter_source(snapshot,message(42,text=self.REVIEW,reply_to_id=7)))
        self.assertFalse(_prefilter_source(snapshot,message(42,text=self.REVIEW,has_image=True)))
        self.assertFalse(_prefilter_source(snapshot,message(42,text='/task '+self.REVIEW)))

    def test_batch_all_reviews_skip_model_and_mixed_group_keeps_model(self):
        from tg_triage.rules_intake import evaluate_batch
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def __init__(self,raw): self.raw=raw; self.calls=0
            def _ask(self,body,parse,attempts): self.calls+=1; return parse(self.raw)
        reviews=[message(41,text=self.REVIEW),message(42,text='xem issue này giúp em https://github.com/org/repo/issues/80')]
        provider=Provider('{}')
        groups=evaluate_batch(snapshot,provider,reviews)
        self.assertEqual(0,provider.calls)
        self.assertEqual([('CHAT','F1')],[(d.state,d.rule) for _,d in groups])
        mixed=[message(41,text=self.REVIEW),message(42,text='màn Queue bị trắng khi mở')]
        provider=Provider('{"groups":[{"message_ids":[41],"facts":{"intent":{"value":"task","source_ids":[41]},'
                          '"object":{"value":"pr","source_ids":[41]},"work":{"value":"review","source_ids":[41]}}},'
                          '{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},'
                          '"object":{"value":"màn Queue","source_ids":[42]},"observed":{"value":"bị trắng","source_ids":[42]}}}]}')
        groups=evaluate_batch(snapshot,provider,mixed)
        self.assertEqual(1,provider.calls)
        self.assertEqual({41:('CHAT','F1'),42:('READY','R9')},{m.message_id:(d.state,d.rule) for ms,d in groups for m in ms})


class RulesTeamRequestTests(RulesBootstrapTests):
    """Owner 2026-11-06 ("Có, là CHAT"): a task handed to or asked of a teammate is CHAT (#112, #113)."""
    ASSIGN = '@teamlead01 e qua phụ @devmember02 làm ticket chỉnh sửa sidebar nha'
    ASK = 'anh @leaddev01 check giúp em key MAPS_API_KEY ở staging có chưa anh'
    TASK_FACTS = RulesPrefilterTests.TASK_FACTS
    QUESTION_FACTS = ('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"question","source_ids":[42]},'
                      '"object":{"value":"key MAPS_API_KEY ở staging","source_ids":[42]},"work":{"value":"check key MAPS_API_KEY","source_ids":[42]}}}]}')
    PLAIN = dict(mentions_listener=False)  # tests.helpers.message tags the listener by default

    def run_live(self, text, *, extensions, facts=None, **extra):
        extra.setdefault('mentions_listener', False)
        return RulesPrefilterTests.run_live(self, text, extensions=extensions, facts=facts, **extra)

    @staticmethod
    def task(ids, intent_ids, work_ids):
        import json
        return json.dumps({'groups':[{'message_ids':ids,'facts':{'intent':{'value':'task','source_ids':intent_ids},
            'object':{'value':'sidebar','source_ids':intent_ids},'work':{'value':'chỉnh sửa sidebar','source_ids':work_ids}}}]})

    def batch(self, sources, raw):
        from tg_triage.rules_intake import _process_batch
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False; calls=0
            def _ask(self,body,parse,attempts): Provider.calls+=1; return parse(raw)
        class GitHub:
            calls=[]
            def create_issue(self,issue): GitHub.calls.append(issue); return 1,None
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        notices=[]
        pipeline=Pipeline(settings,store,Provider(),GitHub(),rules_snapshot=self.snapshot(),notice_sender=lambda *a:notices.append(a) or 77)
        for source in sources: store.enqueue(source)
        _process_batch(pipeline,sources)
        rows={tuple(r) for r in store.connection.execute('SELECT decision,reason FROM messages WHERE message_id IN (%s)'%','.join(str(m.message_id) for m in sources))}
        return Provider.calls,GitHub.calls,notices,rows

    def test_live_team_task_is_chat_f2_without_ticket_or_notice(self):
        for extensions in (False,True):
            for text,facts,intent_rule in ((self.ASSIGN,self.TASK_FACTS,'I2'),(self.ASK,self.QUESTION_FACTS,'I1')):
                with self.subTest(extensions=extensions,text=text):
                    result,provider,github,notices,row=self.run_live(text,extensions=extensions,facts=facts)
                    self.assertEqual(('chat','F2'),(result.decision,result.reason))
                    self.assertEqual(1,provider.calls)
                    self.assertEqual([],github.calls)
                    self.assertEqual([],notices)
                    self.assertEqual(('CHAT','F2',intent_rule),tuple(row))

    def test_live_case_two_grouped_messages_is_chat_f2(self):
        # Live #112: two messages of one sender, both tagging teammates, grouped into one topic.
        sources=[message(41,text=self.ASSIGN,**self.PLAIN),message(42,text='@devmember02 e tạo ticket con rồi assign cho @devmember03',**self.PLAIN)]
        calls,github,notices,rows=self.batch(sources,self.task([41,42],[41],[42]))
        self.assertEqual((1,[],[]),(calls,github,notices))
        self.assertEqual({('chat','F2')},rows)

    def test_teammate_tag_outside_the_cited_message_keeps_the_ticket(self):
        # Review B4: a requester's untagged message grouped with a PM's "@dev nhận nha" stays a ticket.
        sources=[message(41,text='báo cáo doanh thu cần thêm cột phí giao dịch',**self.PLAIN),
                 message(42,text='@devmember02 em nhận cái này nha',**self.PLAIN)]
        for intent_ids in ([41],[41,42]):  # however the model cites intent, the untagged request keeps the ticket
            with self.subTest(intent_ids=intent_ids):
                calls,github,_,rows=self.batch(sources,self.task([41,42],intent_ids,[41]))
                self.assertEqual(1,len(github))
                self.assertEqual({('issue','R10')},rows)

    def test_follow_up_cited_for_intent_does_not_lift_the_tag_from_the_work_message(self):
        # Live #125: 6523 "... @Sellisloss =))" carries the work, 6524 "review xong mới đc đi ngủ" has no tag.
        sources = [message(41, text='rồi giờ call ct kéo ảnh vào zoom cho ảnh review đi @Sellisloss =))', sender_id=10, **self.PLAIN),
                   message(42, text='review xong mới đc đi ngủ', sender_id=10, **self.PLAIN)]
        for intent_ids in ([41], [41, 42], [42]):
            with self.subTest(intent_ids=intent_ids):
                calls, github, notices, rows = self.batch(sources, self.task([41, 42], intent_ids, [41]))
                self.assertEqual((1, [], []), (calls, github, notices))
                self.assertEqual({('chat', 'F2')}, rows)

    def test_untagged_message_carrying_the_work_keeps_the_ticket_even_if_the_tag_is_cited_for_intent(self):
        sources = [message(41, text='báo cáo doanh thu cần thêm cột phí giao dịch', **self.PLAIN),
                   message(42, text='@devmember02 em nhận cái này nha', **self.PLAIN)]
        calls, github, _, rows = self.batch(sources, self.task([41, 42], [42], [41]))
        self.assertEqual(1, len(github))
        self.assertEqual({('issue', 'R10')}, rows)

    def test_requesters_untagged_intent_message_from_another_person_keeps_the_ticket(self):
        # Review N4: the PM's tagged restatement of the work never silences a different person's untagged request.
        sources = [message(41, text='báo cáo doanh thu cần thêm cột phí giao dịch', sender_id=1, **self.PLAIN),
                   message(42, text='@devmember02 em làm giúp anh thêm cột phí giao dịch nha', sender_id=2, **self.PLAIN)]
        for sender_of_41 in (1, None):
            with self.subTest(sender_of_41=sender_of_41):
                sources[0] = message(41, text=sources[0].text, sender_id=sender_of_41, **self.PLAIN)
                calls, github, _, rows = self.batch(sources, self.task([41, 42], [41], [42]))
                self.assertEqual(1, len(github))
                self.assertEqual({('issue', 'R10')}, rows)

    def test_forwarded_requester_message_is_never_the_same_sender_as_the_tagged_follow_up(self):
        # Review round 2: A forwards a customer's request and then writes "@dev làm giúp chị": the customer's request stays a ticket.
        sources = [message(41, text='Chị ơi bên chị cần thêm nút xuất Excel ở báo cáo', is_forward=True, sender_id=1, **self.PLAIN),
                   message(42, text='@devmember02 em làm giúp chị cái này nha', sender_id=1, **self.PLAIN)]
        calls, github, _, rows = self.batch(sources, self.task([41, 42], [41], [42]))
        self.assertEqual(1, len(github))
        self.assertEqual({('issue', 'R10')}, rows)

    def test_forwarded_tag_is_the_requester_speaking_and_keeps_the_ticket(self):
        # Review B2: a forwarded customer message that tags a sales contact is not internal assignment.
        _,_,github,_,row=self.run_live('Chị @sales_lan ơi bên chị cần thêm nút xuất Excel ở báo cáo',extensions=True,
                                       facts=self.task([42],[42],[42]),is_forward=True)
        self.assertEqual(1,len(github.calls))
        self.assertEqual(('READY','R10'),tuple(row)[:2])

    def test_pending_answer_merge_never_applies_team_request(self):
        # Review B1: an answer merged into a NEED_INFO topic keeps the open question's topic.
        from tg_triage.rules_intake import evaluate_batch
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            # Model cites only the tagged answer: a fresh topic would be F2, so R10 proves the merge guard.
            def _ask(self,body,parse,attempts): return parse(RulesTeamRequestTests.task([41,42],[42],[42]))
        sources=[message(41,text='màn báo cáo cần thêm 1 nút',**self.PLAIN),message(42,text='@devmember02 thêm nút xuất Excel nhé em',**self.PLAIN)]
        merged=evaluate_batch(self.snapshot(),Provider(),sources,allow_mixed=True)
        self.assertEqual([('READY','R10')],[(d.state,d.rule) for _,d in merged])
        fresh=evaluate_batch(self.snapshot(),Provider(),sources)
        self.assertEqual([('CHAT','F2')],[(d.state,d.rule) for _,d in fresh])

    def test_tagging_the_bot_still_creates_the_ticket(self):
        _,_,github,_,row=self.run_live(self.ASSIGN,extensions=True,facts=self.TASK_FACTS,mentions_listener=True)
        self.assertEqual(1,len(github.calls))
        self.assertEqual(('READY','R10'),tuple(row)[:2])

    def test_bug_addressed_to_teammate_still_creates_the_ticket(self):
        bug=('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},'
             '"object":{"value":"màn Queue","source_ids":[42]},"observed":{"value":"bị trắng","source_ids":[42]}}}]}')
        _,_,github,_,row=self.run_live('@devmember02 màn Queue bị trắng khi mở',extensions=True,facts=bug)
        self.assertEqual(1,len(github.calls))
        self.assertEqual(('READY','R9'),tuple(row)[:2])

    def test_task_without_teammate_tag_or_with_command_still_creates_the_ticket(self):
        for text in ('thêm nút xuất Excel ở báo cáo','/task @devmember02 thêm nút xuất Excel ở báo cáo',
                     'thêm nút xuất Excel, gửi file cho ngan@example.com',
                     'cập nhật link https://www.tiktok.com/@shopofficial ở footer trang chủ'):
            with self.subTest(text=text):
                _,_,github,_,row=self.run_live(text,extensions=True,facts=self.TASK_FACTS)
                self.assertEqual(1,len(github.calls))
                self.assertEqual(('READY','R10'),tuple(row)[:2])

    def test_code_fact_scope(self):
        from tg_triage.rules_intake import code_facts
        p=self.PLAIN
        self.assertEqual(frozenset({42}),code_facts([message(41,text='làm sidebar',**p),message(42,text='@devmember02 nhận nha',**p)]).team_mention_ids)
        self.assertEqual(frozenset(),code_facts([message(41,text=self.ASSIGN,**p),message(42,text='tạo ticket',mentions_listener=True)]).team_mention_ids)
        self.assertEqual(frozenset(),code_facts([message(42,text=self.ASSIGN,is_forward=True,**p)]).team_mention_ids)
        self.assertEqual(frozenset(),code_facts([message(42,text=self.ASSIGN,**p)],team=False).team_mention_ids)  # merge / recurrence

    def test_mention_tokenizer_edges(self):
        from tg_triage.decision_rules import has_team_mention
        self.assertTrue(has_team_mention('anh @​PmqQuan check giúp'))  # zero-width inside is removed first
        self.assertTrue(has_team_mention('(@devmember02) nhận nha'))
        for text in ('gửi cho ngan@example.com','@dev1 làm','@1abcdef làm',None,'xem https://medium.com/@johnsmith/post',
                     'youtube.com/@kenhchinh','nâng @angular/core lên 17','sửa `@Transactional` ở PaymentService'):
            with self.subTest(text=text): self.assertFalse(has_team_mention(text))

    def test_reply_into_ticket_or_bot_question_is_out_of_scope(self):
        from tg_triage.decision_rules import CodeFacts
        snapshot=self.snapshot()
        model={'intent':{'value':'task','source_ids':[42]},'object':{'value':'sidebar','source_ids':[42]},'work':{'value':'sửa','source_ids':[42]}}
        def facts(**kw):
            base=dict(command='∅',command_payload='none',text='có',extraction_ok='true',reply_target='none',topic_ids=frozenset({42}),
                      image_ids=frozenset(),seen_image_ids=frozenset(),text_source_ids=frozenset({42}),document_ids=frozenset(),team_mention_ids=frozenset({42}))
            base.update(kw); return CodeFacts(**base)
        self.assertEqual(('CHAT','F2'),(lambda d:(d.state,d.rule))(snapshot.evaluate(model,facts())))
        self.assertEqual('R6',snapshot.evaluate(model,facts(reply_target='ticket_open')).rule)
        self.assertEqual('R10',snapshot.evaluate(model,facts(reply_target='bot_question')).rule)
        self.assertEqual('R10',snapshot.evaluate(model,facts(team_mention_ids=frozenset())).rule)
        with self.assertRaises(ValueError): snapshot.evaluate(model,facts(team_mention_ids=frozenset({43})))


class RulesLinkOnlyTests(RulesBootstrapTests):
    """Owner 2026-11-06 ("Duyệt F3", live 6435): a message that is only a link is CHAT before any model call."""
    LINK = 'https://www.onorca.dev/'

    def run_live(self, text, *, extensions, facts=None, **extra):
        extra.setdefault('mentions_listener', False)
        return RulesPrefilterTests.run_live(self, text, extensions=extensions, facts=facts or RulesPrefilterTests.TASK_FACTS, **extra)

    def test_live_link_only_is_chat_f3_without_model_ticket_or_notice(self):
        for extensions in (False,True):
            with self.subTest(extensions=extensions):
                result,provider,github,notices,row=self.run_live(self.LINK,extensions=extensions)
                self.assertEqual(('chat','F3'),(result.decision,result.reason))
                self.assertEqual((0,[],[]),(provider.calls,github.calls,notices))
                self.assertEqual(('CHAT','F3','prefilter'),tuple(row))

    def test_link_with_any_word_still_reaches_the_model(self):
        _,provider,github,_,row=self.run_live('lỗi '+self.LINK,extensions=True)
        self.assertEqual(1,provider.calls)
        self.assertEqual(1,len(github.calls))
        self.assertEqual(('READY','R10'),tuple(row)[:2])

    def test_scope_excludes_reply_attachment_and_command(self):
        from tg_triage.rules_intake import _local_rule
        snapshot=self.snapshot()
        self.assertEqual('F3',_local_rule(snapshot,[message(42,text=self.LINK)]))
        self.assertIsNone(_local_rule(snapshot,[message(42,text=self.LINK,reply_to_id=7)]))
        self.assertIsNone(_local_rule(snapshot,[message(42,text=self.LINK,has_image=True)]))
        self.assertIsNone(_local_rule(snapshot,[message(42,text=self.LINK,has_document=True)]))
        self.assertIsNone(_local_rule(snapshot,[message(42,text=self.LINK,is_forward=True)]))  # W1: customer content
        self.assertIsNone(_local_rule(snapshot,[message(42,text='/task '+self.LINK)]))
        self.assertIsNone(_local_rule(snapshot,[message(42,text=self.LINK+'\nlỗi')]))
        self.assertEqual('F3',_local_rule(snapshot,[message(42,text=self.LINK+'\nhttps://example.com/b')]))
        self.assertIsNone(_local_rule(snapshot,[]))

    def test_batch_all_links_skip_model_and_mixed_batch_keeps_model(self):
        from tg_triage.rules_intake import evaluate_batch
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def __init__(self,raw): self.raw=raw; self.calls=0
            def _ask(self,body,parse,attempts): self.calls+=1; return parse(self.raw)
        provider=Provider('{}')
        groups=evaluate_batch(snapshot,provider,[message(41,text=self.LINK),message(42,text='www.example.com/tools 👍')])
        self.assertEqual(0,provider.calls)
        self.assertEqual([('CHAT','F3')],[(d.state,d.rule) for _,d in groups])
        provider=Provider('{"groups":[{"message_ids":[41],"facts":{"intent":{"value":"chat","source_ids":[41]}}},'
                          '{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},'
                          '"object":{"value":"màn Queue","source_ids":[42]},"observed":{"value":"bị trắng","source_ids":[42]}}}]}')
        groups=evaluate_batch(snapshot,provider,[message(41,text=self.LINK),message(42,text='màn Queue bị trắng khi mở')])
        self.assertEqual(1,provider.calls)
        self.assertEqual({41:('CHAT','F3'),42:('READY','R9')},{m.message_id:(d.state,d.rule) for ms,d in groups for m in ms})

    def test_pending_answer_merge_is_never_short_circuited(self):
        from tg_triage.rules_intake import early_decision
        self.assertIsNone(early_decision(self.snapshot(),[message(42,text=self.LINK)],prefilter=False))


class RulesChatOnlyTests(RulesBootstrapTests):
    """Owner 2026-11-06 ("Duyệt cả ba"): small talk made only of chat_words is CHAT before any model call."""

    def run_live(self, text, *, extensions, facts=None, **extra):
        extra.setdefault('mentions_listener', False)
        return RulesPrefilterTests.run_live(self, text, extensions=extensions, facts=facts or RulesPrefilterTests.TASK_FACTS, **extra)

    def test_live_small_talk_is_chat_f4_without_model_ticket_or_notice(self):
        for extensions in (False,True):
            for text in ('ok anh','Dạ em cảm ơn ạ!','👍'):
                with self.subTest(extensions=extensions,text=text):
                    result,provider,github,notices,row=self.run_live(text,extensions=extensions)
                    self.assertEqual(('chat','F4'),(result.decision,result.reason))
                    self.assertEqual((0,[],[]),(provider.calls,github.calls,notices))
                    self.assertEqual(('CHAT','F4','prefilter'),tuple(row))

    def test_problem_words_still_reach_the_model(self):
        for text in ('vẫn không được','ok anh, lỗi rồi'):
            with self.subTest(text=text):
                _,provider,_,_,_=self.run_live(text,extensions=True)
                self.assertEqual(1,provider.calls)

    def test_scope_excludes_reply_forward_attachment_and_command(self):
        from tg_triage.rules_intake import _local_rule
        snapshot=self.snapshot()
        self.assertEqual('F4',_local_rule(snapshot,[message(42,text='ok anh')]))
        self.assertIsNone(_local_rule(snapshot,[message(42,text='ok anh',reply_to_id=7)]))
        self.assertIsNone(_local_rule(snapshot,[message(42,text='ok anh',is_forward=True)]))
        self.assertIsNone(_local_rule(snapshot,[message(42,text='ok anh',has_image=True)]))
        self.assertIsNone(_local_rule(snapshot,[message(42,text='/task ok anh')]))
        self.assertIsNone(_local_rule(snapshot,[message(42,text='   ')]))

    def test_batch_all_small_talk_skips_model_and_mixed_batch_keeps_model(self):
        from tg_triage.rules_intake import evaluate_batch
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def __init__(self,raw): self.raw=raw; self.calls=0
            def _ask(self,body,parse,attempts): self.calls+=1; return parse(self.raw)
        provider=Provider('{}')
        groups=evaluate_batch(snapshot,provider,[message(41,text='ok anh'),message(42,text='thanks team 🙏')])
        self.assertEqual(0,provider.calls)
        self.assertEqual([('CHAT','F4')],[(d.state,d.rule) for _,d in groups])
        provider=Provider('{"groups":[{"message_ids":[41],"facts":{"intent":{"value":"chat","source_ids":[41]}}},'
                          '{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},'
                          '"object":{"value":"màn Queue","source_ids":[42]},"observed":{"value":"bị trắng","source_ids":[42]}}}]}')
        groups=evaluate_batch(snapshot,provider,[message(41,text='ok anh'),message(42,text='màn Queue bị trắng khi mở')])
        self.assertEqual(1,provider.calls)
        self.assertEqual({41:('CHAT','F4'),42:('READY','R9')},{m.message_id:(d.state,d.rule) for ms,d in groups for m in ms})

    def test_pending_answer_merge_is_never_short_circuited(self):
        from tg_triage.rules_intake import early_decision, evaluate_batch
        self.assertIsNone(early_decision(self.snapshot(),[message(42,text='ok anh')],prefilter=False))
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False; calls=0
            def _ask(self,body,parse,attempts):
                Provider.calls+=1
                return parse('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"chat","source_ids":[42]}}}]}')
        groups=evaluate_batch(self.snapshot(),Provider(),[message(42,text='ok anh')],allow_mixed=True)
        self.assertEqual(1,Provider.calls)
        self.assertEqual([('CHAT','R7')],[(d.state,d.rule) for _,d in groups])

    def test_status_question_is_never_small_talk(self):
        from tg_triage.rules_intake import _local_rule
        for text in ('xong rồi à anh?','ok?','rồi à？'):
            with self.subTest(text=text): self.assertIsNone(_local_rule(self.snapshot(),[message(42,text=text)]))

    def test_report_back_with_particles_is_not_work(self):
        # N6 + generic work: #115 with a trailing particle no longer turns a question into a ticket.
        from tg_triage.decision_rules import CodeFacts
        snapshot=self.snapshot()
        facts=CodeFacts('∅','none','có','true','none',frozenset({1}),frozenset(),frozenset(),frozenset({1}),frozenset())
        for intent,state,rule in (('question','CHAT','R8'),('task','NEED_INFO','R12')):
            for work in ('báo giùm em nha','cho mình biết với','báo dùm ạ'):
                with self.subTest(intent=intent,work=work):
                    d=snapshot.evaluate({'intent':{'value':intent,'source_ids':[1]},'object':{'value':'link zoom daily','source_ids':[1]},
                                         'work':{'value':work,'source_ids':[1]}},facts)
                    self.assertEqual((state,rule),(d.state,d.rule))


class RulesReadyTicketBodyTests(RulesBootstrapTests):
    """Live 2026-11-05 (#95-#99): READY facts must fill the type template, not raise false questions."""

    @staticmethod
    def section(body, title):
        # Heading-format agnostic: Owner may renumber or drop emoji ("### 2. Việc cần làm").
        import re
        return re.split(r'^### .*'+re.escape(title)+r'.*$',body,maxsplit=1,flags=re.M)[1].split('\n### ',1)[0]

    def created(self, facts_json, text):
        snapshot=self.snapshot()
        class Provider:
            settings=SimpleNamespace(model='test'); think=False; supports_vision=False
            def _ask(self,body,parse,attempts): return parse(facts_json)
        class GitHub:
            def __init__(self): self.calls=[]
            def create_issue(self,issue): self.calls.append(issue); return len(self.calls),None
        store=Store(':memory:'); self.addCleanup(store.close)
        settings=replace(config(),mode='live',gh_repo='sandbox/triage',autonomous_enabled=True,decision_rules_enabled=True,
                         telegram=replace(config().telegram,ops_chat_id=-1009876543210))
        github=GitHub()
        pipeline=Pipeline(settings,store,Provider(),github,rules_snapshot=snapshot,notice_sender=lambda *a:77)
        pipeline.process(message(42,text=text))
        self.assertEqual(1,len(github.calls))
        return github.calls[0]

    def test_ready_task_fills_work_and_asks_no_false_work_question(self):
        issue=self.created('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"task","source_ids":[42]},'
                           '"object":{"value":"function này","source_ids":[42]},"work":{"value":"gán lại function","source_ids":[42]}}}]}',
                           'function này cần gán lại function')
        section=self.section(issue.body,'Việc cần làm')
        self.assertIn('gán lại function',section)
        self.assertNotIn('(chưa có thông tin)',section)
        self.assertNotIn('Việc cần làm là gì?',issue.body)

    def test_ready_bug_fills_observed_as_actual(self):
        issue=self.created('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},'
                           '"object":{"value":"POS","source_ids":[42]},"observed":{"value":"không in được receipt","source_ids":[42]}}}]}',
                           'POS không in được receipt')
        self.assertIn('**Thực tế:** không in được receipt',issue.body)

    def test_ready_feedback_fills_problem_and_proposal(self):
        issue=self.created('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"feedback","source_ids":[42]},'
                           '"problem":{"value":"khó tìm giao dịch","source_ids":[42]},"proposal":{"value":"thêm bộ lọc ngày","source_ids":[42]}}}]}',
                           'khó tìm giao dịch, nên thêm bộ lọc ngày')
        section=self.section(issue.body,'Vấn đề / nhu cầu')
        self.assertIn('khó tìm giao dịch',section)
        self.assertIn('**Đề xuất:** thêm bộ lọc ngày',issue.body)

    def test_ready_titles_are_sentences_not_joined_facts(self):
        task=self.created('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"task","source_ids":[42]},'
                          '"object":{"value":"function này","source_ids":[42]},"work":{"value":"gán lại function","source_ids":[42]}}}]}',
                          'function này cần gán lại function')
        self.assertEqual('[task] gán lại function — function này',task.title)
        self.assertIn('gán lại function — function này',self.section(task.body,'Tóm tắt'))
        self.assertNotIn(';',task.title)
        bug=self.created('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"bug","source_ids":[42]},'
                         '"object":{"value":"POS","source_ids":[42]},"observed":{"value":"không in được receipt","source_ids":[42]}}}]}',
                         'POS không in được receipt')
        self.assertEqual('[bug] POS: không in được receipt',bug.title)
        feedback=self.created('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"feedback","source_ids":[42]},'
                              '"problem":{"value":"khó tìm giao dịch","source_ids":[42]},"proposal":{"value":"thêm bộ lọc ngày","source_ids":[42]}}}]}',
                              'khó tìm giao dịch, nên thêm bộ lọc ngày')
        self.assertEqual('[feedback] thêm bộ lọc ngày',feedback.title)

    def test_ready_footer_names_markdown_rule_not_command_tier(self):
        issue=self.created('{"groups":[{"message_ids":[42],"facts":{"intent":{"value":"task","source_ids":[42]},'
                           '"object":{"value":"màn báo cáo","source_ids":[42]},"work":{"value":"thêm CSV","source_ids":[42]}}}]}',
                           'màn báo cáo thêm CSV')
        footer=issue.body.rsplit('---',1)[1]
        self.assertIn('luật R10 (rules/readiness.md)',footer)
        self.assertNotIn('tầng 1 lệnh',footer)


class RulesBanterTests(RulesBootstrapTests):
    """Owner 2026-11-09 (live #118, #119): a joke inside a group that is already joking is CHAT, not a ticket."""
    JOKE = 'để e mở tab ẩn danh login 10 acc test vào xem live 😁'

    def history(self):
        talk = [(10, 101, 'em thấu hiểu tâm lý người dùng mà 😆'), (11, 102, '😂😂😂'),
                (12, 103, 'Thiếu tính năng fake mắt xem rồi'), (13, 103, 'Kéo dòng họ vô xem live nè')]
        return [message(mid, text=text, sender_id=sender, mentions_listener=False) for mid, sender, text in talk]

    def run_live(self, text, *, extensions=True, history=None, **extra):
        extra.setdefault('mentions_listener', False)
        extra.setdefault('sender_id', 105)
        return RulesPrefilterTests.run_live(self, text, extensions=extensions, facts=RulesPrefilterTests.TASK_FACTS,
                                            history=self.history() if history is None else history, **extra)

    def test_live_joke_in_a_joking_group_is_chat_f5_without_model_ticket_or_notice(self):
        for extensions in (False, True):
            with self.subTest(extensions=extensions):
                result, provider, github, notices, row = self.run_live(self.JOKE, extensions=extensions)
                self.assertEqual(('chat', 'F5'), (result.decision, result.reason))
                self.assertEqual((0, [], []), (provider.calls, github.calls, notices))
                self.assertEqual(('CHAT', 'F5', 'prefilter'), tuple(row))

    def test_same_joke_without_a_joking_group_reaches_the_model(self):
        _, provider, _, _, _ = self.run_live(self.JOKE, history=[])
        self.assertEqual(1, provider.calls)

    def test_one_person_laughing_twice_is_not_a_joking_group(self):
        talk = [message(10, text='haha', sender_id=101, mentions_listener=False), message(11, text='😂😂', sender_id=101, mentions_listener=False)]
        _, provider, _, _, _ = self.run_live(self.JOKE, history=talk)
        self.assertEqual(1, provider.calls)

    def test_old_laughter_outside_the_window_does_not_count(self):
        old = [replace(m, received_at=m.received_at - timedelta(minutes=45)) for m in self.history()]
        _, provider, _, _, _ = self.run_live(self.JOKE, history=old)
        self.assertEqual(1, provider.calls)

    def test_problem_words_bot_tag_and_missing_laugh_sign_still_reach_the_model(self):
        for text, extra in (('Màn Queue không in được bill 😅', {}), ('lỗi lắm 😂', {}), (self.JOKE, {'mentions_listener': True}),
                            ('Thiếu tính năng fake mắt xem rồi', {})):
            with self.subTest(text=text, extra=extra):
                _, provider, _, _, _ = self.run_live(text, **extra)
                self.assertEqual(1, provider.calls)

    def test_scope_excludes_reply_forward_attachment_and_command(self):
        from tg_triage.rules_intake import _local_rule
        snapshot = self.snapshot()
        history = self.history()
        def at(text='haha xem live nè', **extra): return message(42, text=text, mentions_listener=False, sender_id=105, **extra)
        self.assertEqual('F5', _local_rule(snapshot, [at()], history))
        self.assertIsNone(_local_rule(snapshot, [at()]))   # no group history: never matches
        for extra in ({'reply_to_id': 7}, {'is_forward': True}, {'has_image': True}):
            self.assertIsNone(_local_rule(snapshot, [at(**extra)], history))
        self.assertIsNone(_local_rule(snapshot, [at('/task haha xem live nè')], history))
        self.assertIsNone(_local_rule(snapshot, [message(42, text='haha xem live nè', sender_id=105)], history))  # tags the bot

    def test_model_sees_group_history_and_the_banter_instruction(self):
        from tg_triage.rules_extraction import partition_and_extract
        from tg_triage.decision_rules import RulesSnapshot
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        snapshot = RulesSnapshot(root/'rules', root/'templates/github-ticket', extensions=True)
        provider = SimpleNamespace(settings=SimpleNamespace(model='test'), think=False, supports_vision=False)
        out = []
        partition_and_extract(provider, snapshot, [message(42, text='Thiếu tính năng fake mắt xem rồi', mentions_listener=False, sender_id=105)],
                              read_context=self.history(), body_out=out)
        system, user = out[0]['messages'][0]['content'], out[0]['messages'][1]['content']
        self.assertIn('read_only_context', system)
        self.assertIn('đùa', system)
        self.assertIn('😂😂😂', user)


class RulesGroupMoodPacketTests(RulesBootstrapTests):
    """Owner 2026-11-09 (#118): the model packet carries code-counted joking signals, only while the group is joking."""

    history = RulesBanterTests.history

    def build(self, history, text='Thiếu tính năng fake mắt xem rồi', limit=24000):
        from tg_triage.rules_extraction import partition_and_extract
        from tg_triage.decision_rules import RulesSnapshot
        root = Path(__file__).resolve().parents[1]
        snapshot = RulesSnapshot(root/'rules', root/'templates/github-ticket', extensions=True)
        provider = SimpleNamespace(settings=SimpleNamespace(model='test'), think=False, supports_vision=False)
        out = []
        partition_and_extract(provider, snapshot, [message(42, text=text, mentions_listener=False, sender_id=105, received_at=history[-1].received_at + timedelta(minutes=2))],
                              read_context=history, limit=limit, body_out=out)
        self.assertTrue(out, 'request must fit')
        system = json.loads(out[0]['messages'][0]['content'].split('\n', 1)[1])
        packet = json.loads(out[0]['messages'][1]['content'])
        return snapshot, system, packet

    def test_joking_group_gets_mood_flags_minutes_and_contrast_examples(self):
        snapshot, system, packet = self.build(self.history())
        self.assertEqual({'joking': True, 'laugh_messages': 2, 'laugh_senders': 2, 'window_minutes': 15, 'note': snapshot.mood_note}, packet['group_mood'])
        context = {row['message_id']: row for row in packet['read_only_context']}
        self.assertEqual({10, 11}, {mid for mid, row in context.items() if row.get('laugh')})
        self.assertTrue(all(isinstance(row['min_ago'], int) and row['min_ago'] >= 0 for row in context.values()))
        tails = [row['Tin (rút gọn)'] for row in system['examples'][-2:]]
        self.assertTrue(tails[0].startswith('Nhóm đang đùa') and 'Thiếu nút xuất Excel' in tails[1])
        self.assertEqual(['chat', 'feedback'], [row['intent (model)'].strip('`') for row in system['examples'][-2:]])

    def test_quiet_group_gets_no_mood_no_flags_no_extra_examples(self):
        quiet = [message(10 + i, text=t, sender_id=101 + i, mentions_listener=False) for i, t in enumerate(('ok anh', 'xem giúp em màn Queue', 'để em xem'))]
        _, system, packet = self.build(quiet)
        self.assertNotIn('group_mood', packet)
        self.assertTrue(all('laugh' not in row and 'min_ago' not in row for row in packet['read_only_context']))
        self.assertEqual(9, len(system['examples']))

    def test_incident_laughter_is_not_flagged_and_does_not_make_mood(self):
        talk = [message(10, text='prod sập rồi =))', sender_id=101, mentions_listener=False), message(11, text='lại lỗi 😂', sender_id=102, mentions_listener=False)]
        _, _, packet = self.build(talk)
        self.assertNotIn('group_mood', packet)

    def test_mood_leaves_in_order_examples_then_context_then_mood_without_rejecting_the_request(self):
        from tg_triage.thinking import request_text_bytes
        from tg_triage.rules_extraction import partition_and_extract
        from tg_triage.decision_rules import RulesSnapshot
        root = Path(__file__).resolve().parents[1]
        snapshot = RulesSnapshot(root/'rules', root/'templates/github-ticket', extensions=True)
        provider = SimpleNamespace(settings=SimpleNamespace(model='test'), think=False, supports_vision=False)
        history = self.history()
        src = [message(42, text='Thiếu tính năng fake mắt xem rồi', mentions_listener=False, sender_id=105, received_at=history[-1].received_at + timedelta(minutes=2))]
        sizes = {}
        for limit in (24000, 8000, 3600, 3000):
            out = []
            partition_and_extract(provider, snapshot, src, read_context=history, limit=limit, body_out=out)
            if not out:
                sizes[limit] = None; continue
            packet = json.loads(out[0]['messages'][1]['content'])
            sizes[limit] = (len(json.loads(out[0]['messages'][0]['content'].split('\n', 1)[1])['examples']), len(packet['read_only_context']), 'group_mood' in packet)
            self.assertLessEqual(request_text_bytes(out[0]), limit)
        self.assertEqual((11, 4, True), sizes[24000])
        self.assertLess(sizes[8000][0], 11)   # examples are trimmed first on the local budget
        for limit, got in sizes.items():
            if got is None: continue
            examples, context, mood = got
            if context < 4: self.assertEqual(0, examples, limit)     # context is only trimmed after every example is gone
            if not mood: self.assertEqual((0, 0), (examples, context), limit)   # the mood is the very last thing to go


class RulesReplyChatAndImageSplitTests(RulesBootstrapTests):
    """Owner 2026-11-10 (live #121-#123): "Làm sửa 1 và 2"."""
    REPLY = 'lát họp xong nói qua v3 này nha anh Mến'

    def history(self):
        return [message(10, text='@vomaingoc community v3 html xong chưa em', sender_id=201, mentions_listener=False)]

    def live(self, text, *, settled=True, **extra):
        extra.setdefault('mentions_listener', False)
        extra.setdefault('sender_id', 202)
        extra.setdefault('reply_to_id', 10)
        return RulesPrefilterTests.run_live(self, text, extensions=True, facts=RulesPrefilterTests.TASK_FACTS, history=self.history(),
                                            settled_chat=(10,) if settled else (), **extra)

    def test_reply_to_a_chat_message_is_chat_f6_without_model_ticket_or_notice(self):
        result, provider, github, notices, row = self.live(self.REPLY)
        self.assertEqual(('chat', 'F6'), (result.decision, result.reason))
        self.assertEqual((0, [], []), (provider.calls, github.calls, notices))
        self.assertEqual(('CHAT', 'F6', 'prefilter'), tuple(row))

    def test_reply_to_something_not_settled_as_chat_still_reaches_the_model(self):
        _, provider, _, _, _ = self.live(self.REPLY, settled=False)
        self.assertEqual(1, provider.calls)

    def test_problem_word_command_bot_tag_and_attachment_still_reach_the_model(self):
        for text, extra in (('vẫn lỗi login nè anh', {}), ('/task cập nhật màn báo cáo', {}), (self.REPLY, {'mentions_listener': True}),
                            (self.REPLY, {'has_image': True}), (self.REPLY, {'is_forward': True})):
            with self.subTest(text=text, extra=extra):
                _, provider, _, _, _ = self.live(text, **extra)
                self.assertEqual(1, provider.calls)

    def test_reply_chat_never_applies_to_a_non_reply(self):
        from tg_triage.rules_intake import _local_rule
        snapshot = self.snapshot()
        self.assertEqual('F6', _local_rule(snapshot, [message(42, text=self.REPLY, reply_to_id=10, mentions_listener=False)], (), frozenset({42})))
        self.assertIsNone(_local_rule(snapshot, [message(42, text=self.REPLY, mentions_listener=False)], (), frozenset({42})))
        self.assertIsNone(_local_rule(snapshot, [message(42, text=self.REPLY, reply_to_id=10, mentions_listener=False)]))

    def facts_json(self):
        return ('{"groups":[{"message_ids":[41,42],"facts":{"intent":{"value":"task","source_ids":[42]},'
                '"object":{"value":"v3","source_ids":[42]},"work":{"value":"nói qua v3","source_ids":[42]},'
                '"observed":{"value":"Connection timed out","source_ids":[41]}}}]}')

    def test_ticket_121_shape_splits_the_foreign_image_and_settles_the_reply_as_chat(self):
        from tg_triage.rules_intake import evaluate_batch
        class Provider:
            settings = SimpleNamespace(model='test'); think = False; supports_vision = False
            def __init__(self, raw): self.raw = raw; self.calls = 0
            def _ask(self, body, parse, attempts): self.calls += 1; return parse(self.raw)
        image = message(41, text='', has_image=True, sender_id=201, mentions_listener=False)
        reply = message(42, text=self.REPLY, reply_to_id=10, sender_id=202, mentions_listener=False)
        provider = Provider(self.facts_json())
        groups = evaluate_batch(self.snapshot(), provider, [image, reply], chat_replies=frozenset({42}))
        self.assertEqual(1, provider.calls)
        self.assertEqual({41: ('MEDIA_ONLY', 'R3'), 42: ('CHAT', 'F6')}, {m.message_id: (d.state, d.rule) for ms, d in groups for m in ms})
        # Without fix 2 the reply group would still be a task ticket, but never one that carries the image evidence.
        groups = evaluate_batch(self.snapshot(), Provider(self.facts_json()), [image, reply])
        by_id = {m.message_id: d for ms, d in groups for m in ms}
        self.assertEqual(('MEDIA_ONLY', 'R3'), (by_id[41].state, by_id[41].rule))
        self.assertEqual(('READY', 'R10'), (by_id[42].state, by_id[42].rule))
        self.assertNotIn('observed', by_id[42].present)

    def test_same_sender_or_reply_link_keeps_the_group(self):
        from tg_triage.rules_intake import _split_foreign_images
        image = message(41, text='', has_image=True, sender_id=201)
        facts = {'observed': {'value': 'timeout', 'source_ids': [41]}}
        text_same = message(42, text='xem giúp em', sender_id=201)
        text_other = message(42, text='ảnh lỗi login', sender_id=202, reply_to_id=41)
        for companion in (text_same, text_other):
            groups = _split_foreign_images((((41, 42), facts),), [image, companion])
            self.assertEqual((((41, 42), facts),), groups)

    def test_split_drops_only_facts_that_cite_the_image_and_keeps_the_rest(self):
        from tg_triage.rules_intake import _split_foreign_images
        image = message(41, text='', has_image=True, sender_id=201)
        other = message(42, text='nói qua v3', sender_id=202)
        facts = {'intent': {'value': 'task', 'source_ids': [42]}, 'observed': {'value': 'timeout', 'source_ids': [41]},
                 'media_ids': {'value': [41], 'source_ids': [41]}}
        groups = _split_foreign_images((((41, 42), facts),), [image, other])
        self.assertEqual((((42,), {'intent': facts['intent']}), ((41,), {})), groups)

    def test_two_foreign_bare_images_both_split_and_a_task_fact_is_never_split(self):
        from tg_triage.rules_intake import _split_foreign_images
        one = message(41, text='', has_image=True, sender_id=201)
        two = message(42, text='', has_image=True, sender_id=202)
        self.assertEqual((((41,), {}), ((42,), {})), _split_foreign_images((((41, 42), {}),), [one, two]))
        keep = (((41, 42), {'task': {'title': 'x'}}),)
        self.assertEqual(keep, _split_foreign_images(keep, [one, message(42, text='nói', sender_id=202)]))


class RulesReplyGaveUpTests(RulesBootstrapTests):
    """Owner 2026-11-10 ("Bỏ (b)"): a reply to a message the bot gave up on is NOT settled; the model decides."""
    TEXT = 'rồi giờ call ct kéo ảnh vào zoom cho ảnh review đi @Sellisloss'
    history = RulesReplyChatAndImageSplitTests.history

    def test_reply_to_a_given_up_message_reaches_the_model(self):
        _, provider, _, _, _ = RulesPrefilterTests.run_live(self, self.TEXT, extensions=True, facts=RulesPrefilterTests.TASK_FACTS,
                                                         history=self.history(), gave_up=(10,), sender_id=202, reply_to_id=10, mentions_listener=False)
        self.assertEqual(1, provider.calls)

    def test_reply_target_settled_only_for_f2_f4_f5_by_a_different_known_person(self):
        snapshot = self.snapshot()
        self.assertTrue(snapshot.reply_target_settled('chat', 'F2', 1, 2))
        self.assertFalse(snapshot.reply_target_settled('chat', 'F2', 1, 1))   # N1: one's own message
        self.assertFalse(snapshot.reply_target_settled('chat', 'F2', None, 2))
        self.assertTrue(snapshot.reply_target_settled('chat', 'F4', 1, 2))
        self.assertTrue(snapshot.reply_target_settled('chat', 'F5', 1, 2))
        for rule in ('F1', 'F3', 'F6', 'R1', 'R5', 'R7', 'R8'):   # B2: reply to a review request, link, question ... may be a real request
            self.assertFalse(snapshot.reply_target_settled('chat', rule, 1, 2), rule)
        for reason in ('semantic_analyze', 'semantic_status', 'dry_run_append', 'unbound_ops_reply', 'rules_reply_processed', None, ''):
            self.assertFalse(snapshot.reply_target_settled('chat', reason, 1, 2), reason)
        for reason in ('rules_hold_escalated', 'rules_hold', 'rules_extract_pending'):   # (b) dropped
            self.assertFalse(snapshot.reply_target_settled('pending_review', reason, 1, 2), reason)
        self.assertFalse(snapshot.reply_target_settled('issue', None, 1, 2))

    def test_reply_to_an_f2_message_without_an_at_name_is_not_settled(self):
        # Review round 2: a topic's other messages are also stored chat/F2; only the message that carries the @name settles replies.
        history = [message(10, text='báo cáo doanh thu cần thêm cột phí giao dịch', sender_id=201, mentions_listener=False)]
        _, provider, _, _, _ = RulesPrefilterTests.run_live(self, 'bên kế toán cũng cần cột này gấp', extensions=True, facts=RulesPrefilterTests.TASK_FACTS,
                                                         history=history, settled_chat=(10,), sender_id=202, reply_to_id=10, mentions_listener=False)
        self.assertEqual(1, provider.calls)

    def test_reply_to_a_forwarded_f2_message_is_not_settled(self):
        # Review round 3: A forwards a customer's tagged request; the whole topic is stored chat/F2, but a forward is not A's own content.
        history = [message(10, text='Chị @sales_lan ơi bên chị cần thêm nút xuất Excel ở báo cáo', is_forward=True, sender_id=201, mentions_listener=False)]
        _, provider, _, _, _ = RulesPrefilterTests.run_live(self, 'bên chị cần gấp trước thứ 6 nha', extensions=True, facts=RulesPrefilterTests.TASK_FACTS,
                                                         history=history, settled_chat=(10,), sender_id=202, reply_to_id=10, mentions_listener=False)
        self.assertEqual(1, provider.calls)
