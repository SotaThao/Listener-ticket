import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from tg_triage.classifier import Classifier
from tg_triage.ingress_model import parse_ingress
from tg_triage.models import CaseAnalysis
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import config,message,classification,FakeGitHub,FakeHTTP,NOW


def draft(ids,**extra):
    fields=dict(verdict='ticket',classification=classification(),key_message_ids=tuple(ids),category='bug',actionable=True)
    fields.update(extra)
    return CaseAnalysis(**fields)


def route(ids,action='NEW',candidate=None,analysis=None,question=''):
    return dict(message_ids=ids,action=action,candidate_id=candidate,confidence=.95,
                question=question,analysis=analysis or draft(ids))


class Router:
    def __init__(self,*answers): self.answers=list(answers); self.calls=[]
    def analyze_ingress(self,sources,candidates,**options):
        self.calls.append((sources,candidates,options)); return self.answers.pop(0)


class AutonomousTests(TestCase):
    def build(self,router,store=None,**extra):
        store=store or Store(':memory:')
        p=Pipeline(replace(config('live',window=2),autonomous_enabled=True),store,router,FakeGitHub(),**extra)
        self.addCleanup(store.close)
        return p

    def test_clear_request_no_tag_does_not_wait_two_minutes(self):
        p=self.build(Router([route([1])]))
        result=p.process(message(mentions_listener=False))
        self.assertEqual('issue',result.decision); self.assertEqual(1,len(p.github.calls))
        self.assertEqual('done',p.store.get_case(result.case_key)['status'])

    def test_three_messages_one_ticket_first_reply(self):
        notices=[]; p=self.build(Router([route([1,2,3])]),notice_sender=lambda c,m,t:notices.append(m) or 100)
        for i in (1,2,3): p.store.enqueue(message(i,mentions_listener=False))
        p.process(message(1,mentions_listener=False))
        self.assertEqual(1,len(p.github.calls)); self.assertEqual([1],notices)
        self.assertEqual(3,p.store.reconcile(f'{message().chat_id}:1').received)
        self.assertIsNone(p.process(message(2)))

    def test_three_requests_interleaved_three_corresponding_replies(self):
        notices=[]; p=self.build(Router([route([1]),route([2]),route([3])]),notice_sender=lambda c,m,t:notices.append(m) or 100+m)
        for i in (1,2,3): p.store.enqueue(message(i,sender_id=i%2,mentions_listener=False))
        p.process(message(1,sender_id=1,mentions_listener=False))
        self.assertEqual(3,len(p.github.calls)); self.assertEqual([1,2,3],notices)

    def test_model_failure_does_not_merge_adjacent_same_author(self):
        notices=[]; p=self.build(Router(None),notice_sender=lambda c,m,t:notices.append((m,t)) or 100+m)
        p.store.enqueue(message(2))
        p.process(message(1))
        self.assertEqual(0,len(p.github.calls)); self.assertEqual([1],[n[0] for n in notices])
        self.assertNotEqual(p.store.get(message().chat_id,1)['case_key'],p.store.get(message().chat_id,2)['case_key'])

    def test_pending_photo_followed_by_caption_reuses_sources(self):
        notices=[]
        router=Router([route([1],'CLARIFY',question='Anh/Chị muốn đổi phần nào của bảng?')],
                      [route([2],'CONTINUE','c1',draft([1,2]))])
        p=self.build(router,notice_sender=lambda c,m,t:notices.append(m) or 100+m)
        p.process(message(1,text='',has_image=True,mentions_listener=False))
        result=p.process(message(2,text='thiết kế lại bảng ở chế độ này',reply_to_id=1,mentions_listener=False))
        self.assertEqual('issue',result.decision); self.assertEqual(1,len(p.github.calls))
        self.assertEqual(2,p.store.reconcile(result.case_key).received)
        self.assertIn('thiết kế lại',p.github.calls[0].body)

    def test_unknown_candidate_fails_closed_visible(self):
        p=self.build(Router([route([1],'CONTINUE','c999')]))
        self.assertEqual('pending_review',p.process(message()).decision)
        self.assertFalse(p.github.calls)

    def test_foreign_source_id_fails_closed(self):
        p=self.build(Router([route([1],analysis=draft([999]))]))
        self.assertEqual('pending_review',p.process(message()).decision)
        self.assertFalse(p.github.calls)

    def test_old_exact_reply_candidate_has_no_age_cutoff(self):
        p=self.build(Router([route([1])],[route([2],'CLARIFY',question='Lỗi còn tiếp diễn hay là yêu cầu khác?')]),notice_sender=lambda *a:101)
        p.process(message(1))
        p.process(message(2,reply_to_id=1,received_at=NOW+timedelta(days=365)))
        candidates=p.classifier.calls[1][1]
        self.assertTrue(candidates[0]['exact_reply']); self.assertEqual(1,candidates[0]['messages'][0].message_id)

    def test_new_scope_reply_old_creates_separate_case(self):
        p=self.build(Router([route([1])],[route([2])]))
        p.process(message(1)); p.process(message(2,text='Yêu cầu khác: đổi bảng tuyển dụng',reply_to_id=1))
        self.assertEqual(2,len(p.github.calls))
        self.assertIn('Ticket liên quan: https://github.com/sandbox/triage/issues/42',p.github.calls[1].body)
        self.assertNotEqual(p.store.get(message().chat_id,1)['case_key'],p.store.get(message().chat_id,2)['case_key'])

    def test_pending_survives_restart_and_immutable_route(self):
        with TemporaryDirectory() as directory:
            path=str(Path(directory)/'memory.sqlite3')
            store=Store(path); p=Pipeline(replace(config('live'),autonomous_enabled=True),store,Router([route([1],'CLARIFY')]),FakeGitHub())
            p.process(message(1)); store.close()
            store=Store(path); self.addCleanup(store.close)
            p=Pipeline(replace(config('live'),autonomous_enabled=True),store,Router([route([2],'CONTINUE','c1',draft([1,2]))]),FakeGitHub())
            self.assertEqual('issue',p.process(message(2,reply_to_id=1)).decision)
            with self.assertRaises(Exception): store.connection.execute("UPDATE autonomous_routes SET action='NEW'")
            store.close()

    def test_strict_parser_rejects_authority_fields(self):
        payload=dict(groups=[dict(message_ids=[1],action='NEW',candidate_id=None,confidence=.9,question='',analysis={})])
        payload['groups'][0]['repo']='attacker/repo'
        self.assertIsNone(parse_ingress(json.dumps(payload)))


    def test_crash_after_route_before_case_reuses_cached_draft(self):
        with TemporaryDirectory() as directory:
            path=str(Path(directory)/'memory.sqlite3'); store=Store(path)
            p=Pipeline(replace(config('live'),autonomous_enabled=True),store,Router([route([1])]),FakeGitHub())
            store.autonomy_case=lambda *a: (_ for _ in ()).throw(SystemExit('injected'))
            with self.assertRaises(SystemExit): p.process(message(1))
            store.close(); store=Store(path)
            p=Pipeline(replace(config('live'),autonomous_enabled=True),store,Router(),FakeGitHub())
            self.assertEqual('issue',p.process(message(1)).decision)
            self.assertEqual([],p.classifier.calls); self.assertEqual(1,len(p.github.calls))
            store.close()

    def test_crash_after_case_before_effect_sweep_reuses_cached_draft(self):
        with TemporaryDirectory() as directory:
            path=str(Path(directory)/'memory.sqlite3'); store=Store(path)
            p=Pipeline(replace(config('live'),autonomous_enabled=True),store,Router([route([1])]),FakeGitHub())
            p._run=lambda *a: (_ for _ in ()).throw(SystemExit('injected'))
            with self.assertRaises(SystemExit): p.process(message(1))
            store.close(); store=Store(path)
            p=Pipeline(replace(config('live'),autonomous_enabled=True),store,Router(),FakeGitHub())
            self.assertEqual('issue',p.close_due_cases()[0].decision)
            self.assertEqual([],p.classifier.calls); self.assertEqual(1,len(p.github.calls))
            self.assertEqual([],p.close_due_cases()); store.close()

    def test_pending_confirmation_replies_first_original_and_notice_maps_back(self):
        notices=[]
        router=Router([route([1],'CLARIFY',question='Anh/Chị muốn đổi phần nào?')],
                      [route([2],'CONTINUE','c1',draft([1,2]))])
        p=self.build(router,notice_sender=lambda c,m,t:notices.append((m,t)) or 100+len(notices))
        p.process(message(1,text='nội dung chưa rõ'))
        p.process(message(2,text='nút lưu bị lỗi không phản ứng',reply_to_id=1))
        self.assertEqual([1,1],[n[0] for n in notices])
        self.assertIn('tạo ticket:',notices[1][1])
        candidate=p.store.autonomy_candidates(message(3,reply_to_id=102),p.config.gh_repo)[0]
        self.assertTrue(candidate['exact_reply'])
        self.assertEqual(42,candidate['source']['issue_number'])

    def test_joint_caption_pixels_and_draft_reuse_case46(self):
        from tests.helpers import FakeDownloader,FakeUploader,JPG
        from tg_triage.thinking import request_text_bytes
        wire=dict(vars(classification(type='task',title='Thiết kế lại bảng hiển thị thông tin salon',
                     summary='Thiết kế lại bảng khi chọn chế độ Chat only.')),
                  category='task',actionable=True,verdict='ticket',key_message_ids=[442],missing_info=[],reason='Design work',
                  details=dict(work='Thiết kế lại bảng ở chế độ Chat only'))
        payload=dict(groups=[dict(message_ids=[442],action='NEW',candidate_id=None,confidence=.95,question='',analysis=wire)])
        http=FakeHTTP({'message':{'content':json.dumps(payload,ensure_ascii=False)}})
        classifier=Classifier(config().ollama,opener=http)
        p=self.build(classifier,downloader=FakeDownloader({442:(JPG,'image.jpg')}),uploader=FakeUploader())
        result=p.process(message(442,text='Sau này mình cho chế độ nầy thì mình sẽ design đổi bảng nầy',has_image=True,mentions_listener=False))
        self.assertEqual('issue',result.decision); self.assertEqual(1,len(http.calls))
        body=json.loads(http.calls[0][0].data)
        users=[m for m in body['messages'] if m['role']=='user']
        self.assertEqual(1,len(users)); self.assertEqual(1,len(users[0]['images']))
        packet=json.loads(users[0]['content'])
        self.assertEqual([442],packet['NEW_SOURCE_IDS'])
        self.assertEqual(442,packet['images_order'][0]['message_id'])
        self.assertIn('design đổi bảng',packet['sources'][0][3])
        self.assertLessEqual(request_text_bytes(body),8000)
        self.assertIn('Thiết kế lại',p.github.calls[0].body)
        self.assertNotIn('backend',p.github.calls[0].body)

    def test_pos47_five_source_context_one_bug_with_environment(self):
        wire=dict(vars(classification(title='POS không in được ticket và receipt trên app và web',
                     summary='POS không in được ticket/receipt trên cả app và web, máy in có dây.')),
                  category='bug',actionable=True,verdict='ticket',key_message_ids=[448,450,451],missing_info=[],reason='Printing failure',
                  details=dict(environment='Cả app và web, máy in có dây',actual='POS không in được ticket và receipt',expected='In được ticket và receipt'))
        payload=dict(groups=[dict(message_ids=[448,449,450,451,452],action='NEW',candidate_id=None,confidence=.95,question='',analysis=wire)])
        http=FakeHTTP({'message':{'content':json.dumps(payload,ensure_ascii=False)}})
        p=self.build(Classifier(config().ollama,opener=http))
        texts=['hôm nay POS không print được ticket và receipt','Anh đang sử dụng app hay web anh?','cả 2',
               'vừa app và web app anh dùng print có dây','Dạ để em check']
        for i,text in enumerate(texts,448): p.store.enqueue(message(i,text=text,mentions_listener=False))
        self.assertEqual('issue',p.process(message(448,text=texts[0],mentions_listener=False)).decision)
        self.assertEqual(1,len(http.calls)); self.assertEqual(1,len(p.github.calls))
        self.assertIn('máy in có dây',p.github.calls[0].body)
        self.assertEqual(5,p.store.reconcile(f'{message().chat_id}:448').received)

    def test_current_split_projection_known_partial_and_unresolved(self):
        p=self.build(Router([route([1,2,3])]))
        for i in (1,2,3): p.store.enqueue(message(i))
        p.process(message(1)); chat=message().chat_id; case=f'{chat}:1'; root=f'{chat}:{case}'
        split=dict(groups=[dict(message_ids=[1]),dict(message_ids=[2])],unresolved_message_ids=[3])
        with p.store.connection:
            p.store.connection.execute('UPDATE followup_roots SET split_plan=?,context_state=? WHERE root_key=?',
                (json.dumps(split),json.dumps(dict(bindings={'1':0,'2':1})),root))
            p.store.project_topics(chat,case)
        scopes=p.store.current_topic_context(root)
        self.assertEqual([42,None],[s['issue_number'] for s in scopes])
        self.assertEqual('awaiting_issue',scopes[1]['scope_status'])
        self.assertEqual([3],scopes[0]['unresolved_ids'])
        with p.store.connection:
            p.store.connection.execute('UPDATE followup_roots SET siblings=? WHERE root_key=?',('[43]',root))
            p.store.project_topics(chat,case)
        scopes=p.store.current_topic_context(root)
        self.assertEqual([42,43],[s['issue_number'] for s in scopes])
        self.assertEqual([[1],[2]],[s['source_ids'] for s in scopes])
        self.assertEqual(3,p.store.reconcile(case).received)
        self.assertEqual(3,p.store.get_group(case)['message_count'])
        candidates=p.store.autonomy_candidates(message(4,reply_to_id=2),p.config.gh_repo)
        self.assertEqual(2,len(candidates[0]['current_scopes']))
        with self.assertRaises(Exception): p.store.connection.execute('DELETE FROM triage_current_topic_events')

    def test_twenty_sources_fit_joint_provider_budget(self):
        from tg_triage.thinking import request_text_bytes
        http=FakeHTTP({'message':{'content':'{}'}})
        model=Classifier(config().ollama,opener=http)
        model.analyze_ingress([message(i) for i in range(1,21)],[])
        self.assertEqual(1,len(http.calls))
        body=json.loads(http.calls[0][0].data)
        self.assertLessEqual(request_text_bytes(body),8000)
        packet=json.loads(body['messages'][1]['content'])
        self.assertEqual(list(range(1,21)),packet['NEW_SOURCE_IDS'])
        self.assertEqual(20,len(packet['sources']))

    def test_cached_plan_repo_change_quarantines_before_create_or_resume(self):
        for checkpoint in ('route','case'):
            with self.subTest(checkpoint=checkpoint), TemporaryDirectory() as directory:
                path=str(Path(directory)/'memory.sqlite3'); store=Store(path)
                p=Pipeline(replace(config('live'),autonomous_enabled=True),store,Router([route([1])]),FakeGitHub())
                if checkpoint=='route': store.autonomy_case=lambda *a: (_ for _ in ()).throw(SystemExit('injected'))
                else: p._run=lambda *a: (_ for _ in ()).throw(SystemExit('injected'))
                with self.assertRaises(SystemExit): p.process(message(1))
                store.close(); store=Store(path); notices=[]
                p=Pipeline(replace(config('live'),autonomous_enabled=True,gh_repo='another/repo'),store,Router(),FakeGitHub(),
                    notice_sender=lambda c,m,t:notices.append(t) or 101)
                result=p.process(message(1)) if checkpoint=='route' else p.close_due_cases()[0]
                self.assertEqual('cached_repo_changed',result.reason)
                self.assertFalse(p.github.calls); self.assertFalse(p.classifier.calls)
                self.assertIn('Repository đã thay đổi',notices[0]); store.close()

    def test_recurrence_episode_requires_actual_reply_to_update(self):
        from tests.test_lifecycle import LifecycleTests
        from tests.test_followups import answer
        helper=LifecycleTests('test_closed_historical_evidence_comments_without_refresh_or_reopen')
        store,model,gh,sent,p=helper.setup_lifecycle(); self.addCleanup(store.close)
        store.enable_topic_registry(); store.enable_autonomy()
        helper.recurrence(p,model)
        for target in (11,911):
            candidates=store.autonomy_candidates(message(12,reply_to_id=target),p.config.gh_repo)
            exact=[c['source']['issue_number'] for c in candidates if c['exact_reply']]
            self.assertEqual([102],exact)
        candidates=store.autonomy_candidates(message(12),p.config.gh_repo)
        self.assertEqual(102,candidates[0]['source']['issue_number'])
        gh.lifecycle_snapshot=lambda n:dict(number=n,repo=p.config.gh_repo,state='open',observed_at=NOW.isoformat())
        model.value=answer('update',summary='Bổ sung cho lần lỗi mới')
        model.analyze_ingress=lambda sources,candidates,**kw:[route([12],'CONTINUE',next(c['candidate_id'] for c in candidates if c['source']['issue_number']==102))]
        self.assertEqual('followup',p.process(message(12,text='Thêm chi tiết cho lỗi hôm nay',reply_to_id=911,mentions_listener=False)).decision)
        self.assertEqual(102,gh.comments[-1][0]); self.assertEqual(2,len(gh.calls))

    def test_followup_batch_commit_crash_resumes_one_durable_job(self):
        with TemporaryDirectory() as directory:
            path=str(Path(directory)/'memory.sqlite3'); store=Store(path)
            router=Router([route([1])],[route([2,3],'CONTINUE','c1',draft([2,3]))])
            p=Pipeline(replace(config('live'),autonomous_enabled=True),store,router,FakeGitHub(),notice_sender=lambda c,m,t:100+m)
            p.process(message(1)); store.enqueue(message(3,reply_to_id=1))
            enqueue=store.enqueue_followup_batch
            def crash(sources,candidate):
                enqueue(sources,candidate)
                raise SystemExit('injected after atomic batch commit')
            store.enqueue_followup_batch=crash
            with self.assertRaises(SystemExit): p.process(message(2,reply_to_id=1))
            self.assertEqual('followup',store.get(message().chat_id,2)['decision'])
            self.assertEqual('followup',store.get(message().chat_id,3)['decision']); store.close()
            store=Store(path); router=Router()
            p=Pipeline(replace(config('live'),autonomous_enabled=True),store,router,FakeGitHub(),notice_sender=lambda c,m,t:100+m)
            p.close_due_cases()
            self.assertEqual(1,store.connection.execute('SELECT COUNT(*) FROM followup_jobs').fetchone()[0])
            self.assertEqual(2,store.connection.execute('SELECT COUNT(*) FROM followup_job_sources').fetchone()[0])
            self.assertFalse(p.github.calls); self.assertFalse(router.calls); store.close()

    def test_actual_pipeline_joint_multiphoto_continue_two_calls_one_job(self):
        from tests.test_lifecycle import LifecycleTests
        from tests.test_followups import answer
        from tests.helpers import FakeDownloader,FakeUploader,JPG,PNG
        from tg_triage.thinking import request_text_bytes
        helper=LifecycleTests('test_closed_historical_evidence_comments_without_refresh_or_reopen')
        store,model,gh,sent,p=helper.setup_lifecycle(); self.addCleanup(store.close)
        store.enable_topic_registry(); store.enable_autonomy()
        gh.lifecycle_snapshot=lambda n:dict(number=n,repo=p.config.gh_repo,state='open',observed_at=NOW.isoformat())
        wire=dict(vars(classification()),category='bug',actionable=True,verdict='ticket',
                  key_message_ids=[2,3,4],missing_info=[],reason='Bổ sung ảnh',details={})
        routing=dict(groups=[dict(message_ids=[2,3,4],action='CONTINUE',candidate_id='c1',confidence=.95,question='',analysis=wire)])
        http=FakeHTTP({'message':{'content':json.dumps(routing,ensure_ascii=False)}},
                      {'message':{'content':json.dumps(answer('update',summary='Bổ sung hai ảnh cho lỗi in POS'),ensure_ascii=False)}})
        p.classifier=Classifier(p.config.ollama,opener=http)
        p.attachments.downloader=FakeDownloader({3:(JPG,'3.jpg'),4:(PNG,'4.png')})
        p.attachments.uploader=FakeUploader()
        store.enqueue(message(3,text='Ảnh lỗi trên app',has_image=True,reply_to_id=1,mentions_listener=False))
        store.enqueue(message(4,text='Ảnh lỗi trên web',has_image=True,reply_to_id=1,mentions_listener=False))
        result=p.process(message(2,text='Đây là hình ảnh của lỗi in trên cả app và web',reply_to_id=1,mentions_listener=False))
        self.assertEqual('followup',result.decision)
        self.assertEqual(2,len(http.calls)); self.assertEqual(1,len(gh.calls)); self.assertEqual(1,len(gh.comments))
        self.assertEqual(1,store.connection.execute('SELECT COUNT(*) FROM followup_jobs').fetchone()[0])
        self.assertEqual(3,store.connection.execute('SELECT COUNT(*) FROM followup_job_sources').fetchone()[0])
        for request,_ in http.calls:
            body=json.loads(request.data); users=[m for m in body['messages'] if m['role']=='user']
            self.assertEqual(1,len(users)); self.assertEqual(2,len(users[0]['images']))
            packet=json.loads(users[0]['content'])
            self.assertEqual([3,4],[i['message_id'] for i in packet['images_order']])
            self.assertLessEqual(request_text_bytes(body),8000)
        self.assertEqual(2,len(p.attachments.uploader.uploads))
        self.assertEqual(2,sent[-1][1])

    def test_clarify_existing_root_keeps_answer_on_same_ticket(self):
        from tests.test_followups import answer
        router=Router([route([1])],[route([2],'CLARIFY','c1',question='Anh/Chị dùng app hay web?')],
                      [route([3],'CONTINUE','c1',draft([1,3]))])
        p=self.build(router,notice_sender=lambda c,m,t:100+m)
        p.github.lifecycle_snapshot=lambda n:dict(number=n,repo=p.config.gh_repo,state='open',observed_at=NOW.isoformat())
        p.github.add_comment=lambda n,b:(setattr(p.github,'comment_target',n),301)[1]
        router.analyze_followup=lambda *a,**kw:answer('update',summary='Bổ sung: cả app và web')
        p.process(message(1)); p.process(message(2,text='Lỗi như ảnh này',reply_to_id=1))
        self.assertEqual('followup',p.store.get(message().chat_id,2)['decision'])
        self.assertEqual('followup',p.process(message(3,text='cả 2',reply_to_id=102)).decision)
        self.assertEqual(1,len(p.github.calls)); self.assertEqual(42,p.github.comment_target)
        self.assertEqual(['Anh/Chị dùng app hay web?'],router.calls[2][1][0]['pending_questions'])

    def test_actual_transport_episode_notice_normalizes_failure_not_working_source(self):
        import asyncio
        from tests.test_lifecycle import LifecycleTests
        from tests.test_followups import answer
        from tests.test_followup_enrichment import projection
        from tests.test_telegram_io import event
        from tg_triage.telegram_io import enqueue_event
        from tg_triage.followups import sweep
        helper=LifecycleTests('test_closed_historical_evidence_comments_without_refresh_or_reopen')
        store,model,gh,sent,p=helper.setup_lifecycle(); self.addCleanup(store.close)
        store.enable_topic_registry(); store.enable_autonomy()
        model.value=answer('update',lifecycle=dict(kind='recurrence',knownworking_source_ids=[10],recurrence_source_ids=[11]),
                           case_analysis=projection(key_message_ids=[11]))
        incoming=[message(10,reply_to_id=901,text='Hôm qua đã chạy tốt'),message(11,text='Hôm nay lại không in được')]
        for item in incoming: store.start_classifying(item)
        store.enqueue_followup_batch(incoming,store.followup_source(incoming[0])); sweep(p)
        self.assertEqual(102,store.followup_source(message(12,reply_to_id=910))['issue_number'])
        async def receive():
            queue=asyncio.Queue()
            await enqueue_event(event(id=12,reply_to_msg_id=910),queue,p.config.telegram.chat_ids,store)
            return queue.get_nowait()
        transported=asyncio.run(receive())
        self.assertEqual(11,transported.reply_to_id)
        self.assertEqual(102,store.followup_source(transported)['issue_number'])
        self.assertEqual(101,store.followup_source(message(13,reply_to_id=10))['issue_number'])

    def test_actual_pos47_open_dense_previous_hypothesis_stays_within_two_calls(self):
        from tests.test_followup_enrichment import EnrichmentTests,projection,TEXTS
        from tests.test_followups import answer
        from tg_triage.thinking import request_text_bytes
        helper=EnrichmentTests('test_new_photo_is_joint_on_wire_and_refreshes_canonical_bug_once')
        store,p,api,uploader,downloader,sent,http=helper.setup_case(); self.addCleanup(store.close)
        p.config=replace(p.config,autonomous_enabled=True); store.enable_topic_registry(); store.enable_autonomy()
        api.issue['state']='open'
        root=store.connection.execute('SELECT root_key,context_state FROM followup_roots').fetchone()
        state=json.loads(root['context_state'])
        dense=projection(summary=('POS không in được ticket và receipt trên app và web. '*10)[:500],
            details=dict(environment=('App và web, máy in có dây. '*20)[:500],
                actual=('Không in được ticket và receipt. '*20)[:500],
                expected=('In được ticket và receipt. '*20)[:500],
                error_text='Nothing was sent to the printer. Try printing again.',
                steps=[('Báo cáo POS không in được ticket và receipt. '*5)[:200]]*5))
        state['managed_snapshot']['analysis']=dense
        with store.connection: store.connection.execute('UPDATE followup_roots SET context_state=? WHERE root_key=?',(json.dumps(state,ensure_ascii=False),root['root_key']))
        routing=dict(groups=[dict(message_ids=[454],action='CONTINUE',candidate_id='c1',confidence=.95,question='',
            analysis=projection(key_message_ids=[448,451,454]))])
        http.responses=[{'message':{'content':json.dumps(routing,ensure_ascii=False)}},
                        {'message':{'content':json.dumps(answer('update',summary='Bổ sung ảnh lỗi POS',case_analysis=projection()),ensure_ascii=False)}}]
        result=p.process(message(454,text='Đây là hình ảnh',sender_id=77,reply_to_id=453,has_image=True))
        self.assertEqual('followup',result.decision); self.assertEqual(2,len(http.calls)); self.assertEqual(1,api.patches)
        followup=json.loads(http.calls[1][0].data)
        self.assertNotIn('lifecycle',followup['format']['properties'])
        self.assertNotIn('When LIFECYCLE_STATE',followup['messages'][0]['content'])
        packet=json.loads(followup['messages'][1]['content'])
        self.assertTrue(packet['hypothesis_text_truncated'])
        self.assertEqual('open',packet['TRUSTED_TICKETS'][0]['lifecycle_state']['state'])
        texts={m['message_id']:m['text'] for m in packet['ORIGINAL_METADATA']}
        self.assertEqual('Đây là hình ảnh',texts[454])
        self.assertTrue(all(texts[i]==text for i,text in zip(range(448,453),TEXTS)))
        for request,_ in http.calls: self.assertLessEqual(request_text_bytes(json.loads(request.data)),8000)

    def test_ingress_schema_rejects_invented_draft_fields(self):
        from tests.test_followup_enrichment import projection
        value=dict(groups=[dict(message_ids=[1],action='NEW',candidate_id=None,confidence=.95,question='',analysis=projection(key_message_ids=[1]))])
        value['groups'][0]['analysis']['future_layout']='invented design'
        self.assertIsNone(parse_ingress(json.dumps(value)))
        value['groups'][0]['analysis'].pop('future_layout')
        value['groups'][0]['analysis']['details']['invented_owner']='Someone'
        self.assertIsNone(parse_ingress(json.dumps(value)))

    def test_closed_joint_context_requires_lifecycle_and_fits_dense_pos_packet(self):
        from tests.test_followup_enrichment import projection,TEXTS
        from tests.test_followups import answer
        from tg_triage.thinking import decision_turn,request_text_bytes
        p=self.build(Router())
        originals=[message(i,text=text,sender_id=77) for i,text in zip(range(448,453),TEXTS)]
        originals.append(message(454,text='Hôm qua đã in thành công cả app và web',sender_id=77))
        reply=message(455,text='Hôm nay lại không in được trên cả hai',reply_to_id=454,sender_id=77)
        old=projection(summary='POS không in được. '*28,
            details=dict(environment='App và web dùng máy in có dây. '*12,actual='Không in được ticket và receipt. '*12))
        ticket=dict(ordinal=0,issue_number=47,message_ids=list(range(448,453)),
            lifecycle_state=dict(state='closed',number=47,repo=p.config.gh_repo,observed_at=NOW.isoformat()),
            confirmed_context=dict(managed_snapshot=dict(analysis=old)))
        response=answer('update',summary='Lỗi tái diễn',lifecycle=dict(kind='recurrence',knownworking_source_ids=[454],recurrence_source_ids=[455]),
            case_analysis=projection(key_message_ids=[455]))
        http=FakeHTTP({'message':{'content':json.dumps(response,ensure_ascii=False)}})
        model=Classifier(p.config.ollama,opener=http)
        with decision_turn(p,'ingress:test-closed',f'{message().chat_id}:448',455):
            value=model.analyze_followup(originals,reply,[ticket],[])
        self.assertIsNotNone(value); self.assertEqual(1,len(http.calls))
        body=json.loads(http.calls[0][0].data)
        self.assertIn('lifecycle',body['format']['required'])
        self.assertLessEqual(request_text_bytes(body),8000)
        packet=json.loads(body['messages'][1]['content'])
        actual={m['message_id']:m['text'] for m in packet['ORIGINAL_METADATA']}
        for source in originals+[reply]: self.assertEqual(source.text,actual[source.message_id])
