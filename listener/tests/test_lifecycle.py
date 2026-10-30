"""Closed evidence and recurrence episodes across trusted targets and durable operation recovery."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tg_triage.followups import _plan, _run
from tg_triage.followup_model import validate_followup
from tg_triage.lifecycle import closed_decision
from tg_triage.store import Store
from tg_triage.github_issues import GitHubIssues, GitHubError, GitHubConflict
from tests.helpers import message, NOW, FakeHTTP
from tests.test_followups import answer
from tests import test_followups
from tests.test_followup_enrichment import projection


class LifecycleTests(unittest.TestCase):
    setup_case=test_followups.FollowupTests.setup_case
    def reply(self,p,mid=10,target=901,text='bổ sung'):
        msg=message(mid,reply_to_id=target,text=text)
        p.store.start_classifying(msg)
        source=p.store.followup_source(msg)
        self.assertIsNotNone(source)
        p.store.enqueue_followup(msg,source)
        from tg_triage.followups import sweep
        sweep(p)
    def setup_lifecycle(self, path=':memory:'):
        store,model,gh,sent,p=self.setup_case(count=1,path=path)
        p.config=replace(p.config,autonomous_enabled=True)
        gh.lifecycle_snapshot=lambda n: dict(number=n,repo=p.config.gh_repo,state='closed',observed_at=NOW.isoformat())
        gh.verify_created_issue=lambda number,issue:number
        return store,model,gh,sent,p

    def test_closed_historical_evidence_comments_without_refresh_or_reopen(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        model.value=answer('update',lifecycle=dict(kind='evidence',knownworking_source_ids=[],recurrence_source_ids=[]),
                           case_analysis=projection(key_message_ids=[1,10]))
        self.reply(p,text='Ảnh bổ sung của lỗi cũ, chưa từng hết lỗi')
        self.assertEqual(len(gh.calls),1); self.assertEqual(len(gh.comments),1)
        self.assertEqual(gh.comments[0][0],101)
        self.assertEqual(store.followup_operations({'id':1})[0]['kind'],'comment')
        self.assertEqual(model.followups[0][2][0]['lifecycle_state']['state'],'closed')

    def test_closed_uncertain_or_missing_lifecycle_asks_focused_question(self):
        for lifecycle in (None,dict(kind='unclear',knownworking_source_ids=[],recurrence_source_ids=[])):
            store,model,gh,sent,p=self.setup_lifecycle()
            model.value=answer('update',**({'lifecycle':lifecycle} if lifecycle else {}))
            self.reply(p,text='Lại lỗi rồi')
            self.assertEqual(len(gh.calls),1); self.assertEqual(gh.comments,[])
            self.assertIn('chưa từng hết',sent[-1][2])

    def recurrence(self,p,model):
        model.value=answer('update',lifecycle=dict(kind='evidence',knownworking_source_ids=[],recurrence_source_ids=[]))
        self.reply(p,text='Đã kiểm tra in thành công trên app và web hôm qua')
        model.value=answer('update',lifecycle=dict(kind='recurrence',knownworking_source_ids=[10],recurrence_source_ids=[11]),
            case_analysis=projection(key_message_ids=[11],summary='Hôm nay POS lại không in được',details={'actual':'Hôm nay không in được'}))
        self.reply(p,11,910,'Hôm nay POS lại không in được')

    def test_explicit_success_then_recurrence_creates_distinct_episode_and_reply_link(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        before=dict(store.get(message().chat_id,1))
        self.recurrence(p,model)
        self.assertEqual(len(gh.calls),2)
        self.assertIn('/issues/101',gh.calls[1].body)
        self.assertNotIn('Unique original request',gh.calls[1].body)
        self.assertNotIn('in thành công',gh.calls[1].body)
        self.assertEqual(dict(store.get(message().chat_id,1)),before)
        self.assertEqual(store.followup_operations({'id':2})[0]['kind'],'related_create')
        self.assertEqual(sent[-1][1],11); self.assertIn('/issues/102',sent[-1][2])
        episode=store.followup_source(message(12,reply_to_id=11))
        self.assertEqual(episode['issue_number'],102); self.assertEqual(json.loads(episode['siblings']),[])
        self.assertEqual(store.followup_source(message(12,reply_to_id=911))['issue_number'],102)
        gh.lifecycle_snapshot=lambda n: dict(number=n,repo=p.config.gh_repo,state='open',observed_at=NOW.isoformat())
        model.value=answer('update',summary='Thêm thông tin mới')
        self.reply(p,12,911,'Thêm thông tin cho lần lỗi hôm nay')
        self.assertEqual(len(gh.calls),2); self.assertEqual(gh.comments[-1][0],102)
        self.assertEqual([m.message_id for m in model.followups[-1][0]],[11])

    def test_foreign_or_missing_observed_target_cannot_write(self):
        for snapshot in (dict(number=101,repo='evil/repo',state='closed',observed_at='now'),
                         dict(number=999,repo='sandbox/triage',state='closed',observed_at='now'),
                         dict(number=101,repo='sandbox/triage',state='closed')):
            store,model,gh,sent,p=self.setup_lifecycle()
            gh.lifecycle_snapshot=lambda n:snapshot
            self.reply(p,text='Lại lỗi')
            self.assertFalse(model.followups); self.assertEqual(gh.comments,[]); self.assertEqual(len(gh.calls),1)

    def test_related_reservation_counts_toward_quota_and_not_siblings(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        self.recurrence(p,model)
        self.assertEqual(store.followup_create_count(NOW),1)
        root=store.connection.execute("SELECT * FROM followup_roots WHERE root_key NOT LIKE 'episode:%'").fetchone()
        self.assertEqual(json.loads(root['siblings']),[])
        self.assertIsNone(root['split_plan'])

    def test_related_unknown_post_is_never_retried(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        original=gh.create_issue
        def unknown(issue):
            raise OSError('unknown outcome')
        gh.create_issue=unknown
        self.recurrence(p,model)
        op=store.followup_operations({'id':2})[0]
        self.assertEqual(op['status'],'ambiguous'); self.assertEqual(store.followup_create_count(NOW),1)
        gh.create_issue=original
        from tg_triage.followups import sweep
        sweep(p)
        self.assertEqual(len(gh.calls),1)
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM followup_episodes').fetchone()[0],0)

    def test_proven_response_mapping_survives_finish_failure(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        finish=store.finish_followup_operation
        def fail(job,op,number):
            if op['kind']=='related_create':raise OSError('crash after POST')
            return finish(job,op,number)
        store.finish_followup_operation=fail
        self.recurrence(p,model)
        self.assertEqual(len(gh.calls),2)
        self.assertEqual(store.followup_source(message(12,reply_to_id=11))['issue_number'],102)
        self.assertEqual(store.followup_operations({'id':2})[0]['result'],102)
        self.assertEqual(store.followup_operations({'id':2})[0]['status'],'ambiguous')

    def test_related_quota_block_does_not_post(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        p.config=replace(p.config,max_issues_per_hour=1)
        self.recurrence(p,model)
        self.assertEqual(len(gh.calls),1)
        self.assertEqual(store.followup_operations({'id':2})[0]['status'],'planned')

    def test_reply_old_parent_cannot_repeat_same_episode_but_fresh_success_can_create_next(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        self.recurrence(p,model)
        model.value=answer('update',lifecycle=dict(kind='recurrence',knownworking_source_ids=[10],recurrence_source_ids=[12]),
                           case_analysis=projection(key_message_ids=[12]))
        self.reply(p,12,901,'Lỗi hôm nay vẫn còn, bổ sung thêm ảnh sau')
        self.assertEqual(len(gh.calls),2)
        self.assertIn('/issues/102',sent[-1][2])
        self.assertEqual(json.loads(store.connection.execute('SELECT plan FROM followup_jobs WHERE message_id=12').fetchone()[0])['context_reason'],'existing_recurrence_episode')
        model.value=answer('update',lifecycle=dict(kind='evidence',knownworking_source_ids=[13],recurrence_source_ids=[]))
        self.reply(p,13,901,'Sau lần lỗi vừa rồi đã sửa và kiểm tra in thành công')
        model.value=answer('update',lifecycle=dict(kind='recurrence',knownworking_source_ids=[13],recurrence_source_ids=[14]),
                           case_analysis=projection(key_message_ids=[14]))
        self.reply(p,14,901,'Sau khi in thành công hôm qua, hôm nay xảy ra lần lỗi mới')
        self.assertEqual(len(gh.calls),3)
        self.assertEqual(store.followup_source(message(15,reply_to_id=14))['issue_number'],103)

    def test_resumed_related_plan_rechecks_latest_verified_episode_boundary(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        self.recurrence(p,model)
        old_payload=json.loads(store.followup_operations({'id':2})[0]['payload'])
        msg=message(12,reply_to_id=901,text='Vẫn là lần lỗi hôm nay')
        store.start_classifying(msg);store.enqueue_followup(msg,store.followup_source(msg))
        job=store.pending_followups()[0]
        store.save_followup_plan(job,answer('update'),[('related_create',old_payload)])
        _run(p,dict(store.connection.execute('SELECT * FROM followup_jobs WHERE id=?',(job['id'],)).fetchone()))
        self.assertEqual(len(gh.calls),2)
        self.assertEqual(store.followup_operations(job)[0]['status'],'planned')
        self.assertIn('/issues/102',sent[-1][2])

    def test_episode_survives_database_reopen_and_keeps_parent_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path=str(Path(folder)/'triage.sqlite3')
            store,model,gh,sent,p=self.setup_lifecycle(path)
            self.recurrence(p,model)
            reopened=Store(path,clock=lambda:NOW)
            try:
                self.assertEqual(reopened.followup_source(message(20,reply_to_id=911))['issue_number'],102)
                self.assertEqual(reopened.followup_source(message(20,reply_to_id=901))['issue_number'],101)
                self.assertEqual(reopened.connection.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            finally:
                reopened.close()
                store.close()

    def test_closed_working_evidence_persists_beyond_short_history(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        model.value=answer('update',lifecycle=dict(kind='evidence',knownworking_source_ids=[10],recurrence_source_ids=[]))
        self.reply(p,text='Đã in tốt, xác nhận thành công')
        for mid in range(11,17):
            model.value=answer('chat')
            self.reply(p,mid,901,'Cảm ơn')
        model.value=answer('update',lifecycle=dict(kind='recurrence',knownworking_source_ids=[10],recurrence_source_ids=[17]),
            case_analysis=projection(key_message_ids=[17],details={'actual':'Lại không in được'}))
        self.reply(p,17,901,'Hôm nay lại không in được')
        self.assertEqual(len(gh.calls),2)
        self.assertIn(10,[m.message_id for m in model.followups[-1][0]])

    def test_recurrence_photo_caption_joint_packet_and_single_proven_upload(self):
        from tests.helpers import FakeDownloader,FakeUploader,JPG
        store,model,gh,sent,p=self.setup_lifecycle()
        model.value=answer('update',lifecycle=dict(kind='evidence',knownworking_source_ids=[10],recurrence_source_ids=[]))
        self.reply(p,text='Đã in tốt rồi')
        model.supports_vision=True
        p.attachments.downloader=FakeDownloader({11:(JPG,'photo.jpg')})
        uploader=FakeUploader();p.attachments.uploader=uploader
        model.value=answer('update',lifecycle=dict(kind='recurrence',knownworking_source_ids=[10],recurrence_source_ids=[11]),
            case_analysis=projection(key_message_ids=[11],details={'actual':'Không in được','error_text':'Printer error'}))
        msg=message(11,reply_to_id=910,text='Hôm nay lại lỗi như ảnh',has_image=True,image_size=len(JPG),image_mime='image/jpeg')
        store.start_classifying(msg);store.enqueue_followup(msg,store.followup_source(msg))
        from tg_triage.followups import sweep
        sweep(p);sweep(p)
        self.assertEqual(len(gh.calls),2);self.assertEqual(len(uploader.uploads),1)
        self.assertIn('/11-1.jpg',gh.calls[-1].body)
        self.assertEqual(model.followups[-1][1].text,'Hôm nay lại lỗi như ảnh')
        self.assertIn('Hôm nay lại lỗi như ảnh',gh.calls[-1].body)
        model.value=answer('update',lifecycle=dict(kind='evidence',knownworking_source_ids=[],recurrence_source_ids=[]))
        self.reply(p,12,901,'Thêm bằng chứng cho lỗi cũ')
        self.assertEqual(gh.comments[-1][0],101)
        self.assertNotIn(11,[m.message_id for m in model.followups[-1][0]])

    def test_contract_rejects_invalid_lifecycle_ids(self):
        for ids in ([True],[-1],[0],[1,1],['1']):
            value=answer('update',lifecycle=dict(kind='recurrence',knownworking_source_ids=ids,recurrence_source_ids=[2]))
            self.assertIsNone(validate_followup(value))

    def test_followup_batch_three_sources_two_images_one_joint_analysis_and_notice(self):
        from tests.helpers import FakeDownloader,FakeUploader,JPG
        store,model,gh,sent,p=self.setup_lifecycle()
        old_source=dict(store.get(message().chat_id,1))
        old_case=dict(store.get_case(old_source['case_key']))
        gh.lifecycle_snapshot=lambda n:dict(number=n,repo=p.config.gh_repo,state='open',observed_at=NOW.isoformat())
        model.supports_vision=True
        p.attachments.downloader=FakeDownloader({10:(JPG,'one.jpg'),11:(JPG,'two.jpg')})
        uploader=FakeUploader();p.attachments.uploader=uploader
        calls=[]
        def analyze(originals,reply,mapping,history,**options):
            calls.append((originals,reply,options))
            self.assertEqual(set(options['images']),{10,11})
            self.assertEqual({m.message_id for m in originals+[reply]},{1,10,11,12})
            return answer('update',summary='POS lỗi trên cả app và web, hai ảnh là bằng chứng')
        model.analyze_followup=analyze
        incoming=[message(10,reply_to_id=901,text='POS không in được, ảnh app',has_image=True,image_size=len(JPG),image_mime='image/jpeg'),
                  message(11,text='Ảnh web của cùng lỗi',has_image=True,image_size=len(JPG),image_mime='image/jpeg'),
                  message(12,text='Cả app và web, máy in có dây')]
        for msg in incoming:store.start_classifying(msg)
        source=store.followup_source(incoming[0])
        store.enqueue_followup_batch(incoming,source)
        from tg_triage.followups import sweep
        sweep(p);sweep(p)
        self.assertEqual(len(calls),1);self.assertEqual(len(gh.comments),1);self.assertEqual(len(uploader.uploads),2)
        self.assertIn('/10-1.jpg',gh.comments[0][1]);self.assertIn('/11-2.jpg',gh.comments[0][1])
        self.assertEqual(sent[-1][1],10)
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM followup_jobs').fetchone()[0],1)
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM followup_job_sources').fetchone()[0],3)
        self.assertEqual(dict(store.get(message().chat_id,1)),old_source)
        self.assertEqual(dict(store.get_case(old_source['case_key'])),old_case)
        self.assertEqual(store.followup_create_count(NOW),0)
        for mid in (10,11,12):self.assertEqual(store.followup_source(message(20,reply_to_id=mid))['issue_number'],101)
        before=[dict(store.get(incoming[0].chat_id,m.message_id)) for m in incoming]
        store.enqueue_followup_batch(incoming,source)
        self.assertEqual(before,[dict(store.get(incoming[0].chat_id,m.message_id)) for m in incoming])

    def test_followup_batch_atomic_missing_member_and_mismatched_replay(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        incoming=[message(10,reply_to_id=901),message(11)]
        store.start_classifying(incoming[0])
        source=store.followup_source(incoming[0])
        with self.assertRaises(ValueError):store.enqueue_followup_batch(incoming,source)
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM followup_jobs').fetchone()[0],0)
        self.assertEqual(store.get(incoming[0].chat_id,10)['decision'],'classifying')
        store.start_classifying(incoming[1]);store.enqueue_followup_batch(incoming,source)
        with self.assertRaises(ValueError):store.enqueue_followup_batch(incoming[:1],source)
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM followup_jobs').fetchone()[0],1)

    def test_batch_working_first_failure_second_creates_episode_on_failure_source(self):
        from tests.helpers import FakeDownloader,FakeUploader,JPG
        store,model,gh,sent,p=self.setup_lifecycle()
        model.supports_vision=True
        p.attachments.downloader=FakeDownloader({10:(JPG,'working.jpg'),11:(JPG,'failure.jpg')})
        uploader=FakeUploader();p.attachments.uploader=uploader
        model.value=answer('update',lifecycle=dict(kind='recurrence',knownworking_source_ids=[10],recurrence_source_ids=[11]),
                           case_analysis=projection(key_message_ids=[11]))
        incoming=[message(10,reply_to_id=901,text='Hôm qua đã chạy tốt',has_image=True,image_size=len(JPG),image_mime='image/jpeg'),
                  message(11,text='Hôm nay lại không in được',has_image=True,image_size=len(JPG),image_mime='image/jpeg')]
        for msg in incoming:store.start_classifying(msg)
        source=store.followup_source(incoming[0]);store.enqueue_followup_batch(incoming,source)
        from tg_triage.followups import sweep
        sweep(p)
        self.assertEqual(len(gh.calls),2);self.assertEqual(len(model.followups),1)
        self.assertEqual(sent[-1][1],10)
        self.assertEqual(store.followup_source(message(12,reply_to_id=11))['source_message_id'],11)
        self.assertEqual(store.followup_source(message(12,reply_to_id=910))['issue_number'],102)
        self.assertEqual(store.followup_source(message(12,reply_to_id=10))['issue_number'],101)
        self.assertNotIn('Hôm qua đã chạy tốt',gh.calls[-1].body)
        self.assertEqual(len(uploader.uploads),1)
        self.assertNotIn('/10-',gh.calls[-1].body);self.assertIn('/11-',gh.calls[-1].body)
        before={mid:dict(store.get(incoming[0].chat_id,mid)) for mid in (10,11)}
        store.enqueue_followup_batch(incoming,source)
        self.assertEqual(before,{mid:dict(store.get(incoming[0].chat_id,mid)) for mid in (10,11)})

    def test_batch_real_classifier_wire_keeps_two_pixels_captions_and_canonical_body_joint(self):
        from tests import test_followup_enrichment
        from tests.helpers import FakeDownloader,JPG
        store,p,api,uploader,downloader,sent,http=test_followup_enrichment.EnrichmentTests.setup_case(self)
        p.attachments.downloader=FakeDownloader({454:(JPG,'app.jpg'),455:(JPG,'web.jpg')})
        response=answer('update',summary='Bổ sung hai ảnh app và web',
                        case_analysis=projection(key_message_ids=[448,451,454,455,456]))
        http.responses=[{'message':{'content':json.dumps(response,ensure_ascii=False)}}]
        incoming=[message(454,reply_to_id=453,text='Ảnh app lỗi in',sender_id=77,has_image=True,image_size=len(JPG),image_mime='image/jpeg'),
                  message(455,text='Ảnh web cùng lỗi',sender_id=77,has_image=True,image_size=len(JPG),image_mime='image/jpeg'),
                  message(456,text='Cả app và web, dùng máy in có dây',sender_id=77)]
        for msg in incoming:store.start_classifying(msg)
        store.enqueue_followup_batch(incoming,store.followup_source(incoming[0]))
        from tg_triage.followups import sweep
        sweep(p)
        self.assertEqual(len(http.calls),1)
        wire=json.loads(http.calls[0][0].data)
        user=wire['messages'][1];packet=json.loads(user['content'])
        self.assertEqual(len(user['images']),2)
        self.assertEqual([item['message_id'] for item in packet['images_order']],[454,455])
        actual={m['message_id']:m['text'] for m in packet['ORIGINAL_METADATA']}
        for msg in incoming:self.assertEqual(actual[msg.message_id],msg.text)
        self.assertEqual(api.patches,1);self.assertEqual(len(uploader.uploads),2)
        self.assertEqual(sent[-1][0],454)

    def test_planned_recurrence_resume_rechecks_reopened_parent_before_post(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        model.value=answer('update',lifecycle=dict(kind='evidence',knownworking_source_ids=[10],recurrence_source_ids=[]))
        self.reply(p,text='Đã chạy tốt')
        model.value=answer('update',lifecycle=dict(kind='recurrence',knownworking_source_ids=[10],recurrence_source_ids=[11]),
            case_analysis=projection(key_message_ids=[11]))
        msg=message(11,reply_to_id=910,text='Hôm nay lỗi lại')
        store.start_classifying(msg);store.enqueue_followup(msg,store.followup_source(msg))
        job=store.pending_followups()[0]
        root,originals,reply,history=store.followup_context(job)
        plan,ops=_plan(p,job,root,originals,reply,history)
        store.save_followup_plan(job,plan,ops)
        gh.lifecycle_snapshot=lambda n:dict(number=n,repo=p.config.gh_repo,state='open',observed_at=NOW.isoformat())
        _run(p,dict(store.connection.execute('SELECT * FROM followup_jobs WHERE id=?',(job['id'],)).fetchone()))
        self.assertEqual(len(gh.calls),1)
        self.assertEqual(store.followup_operations(job)[0]['status'],'planned')
        self.assertIn('đối chiếu',sent[-1][2])

    def test_proven_post_readback_failure_retains_number_without_episode_authority(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        gh.verify_created_issue=lambda number,issue: (_ for _ in ()).throw(GitHubError('unknown GET'))
        self.recurrence(p,model)
        self.assertEqual(len(gh.calls),2)
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM followup_episodes').fetchone()[0],0)
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM followup_roots WHERE issue_number=102').fetchone()[0],0)
        op=store.followup_operations({'id':2})[0]
        self.assertEqual(op['result'],102);self.assertEqual(op['status'],'ambiguous')
        from tg_triage.followups import sweep
        sweep(p)
        self.assertEqual(len(gh.calls),2)

    def test_wrong_body_or_foreign_readback_never_seeds_related_snapshot(self):
        from tg_triage.issue_format import Issue
        for changes in ({'body':'human changed body'},{'html_url':'https://github.com/evil/repo/issues/102'}):
            store,model,gh,sent,p=self.setup_lifecycle()
            def readback(number,issue):
                data=dict(number=number,html_url=f'https://github.com/sandbox/triage/issues/{number}',
                          title=issue.title,body=issue.body,labels=list(issue.labels))
                data.update(changes)
                return GitHubIssues('sandbox/triage','fake',opener=FakeHTTP(data)).verify_created_issue(number,issue)
            gh.verify_created_issue=readback
            self.recurrence(p,model)
            op=store.followup_operations({'id':2})[0]
            self.assertEqual(op['result'],102);self.assertEqual(op['status'],'ambiguous')
            self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM followup_episodes').fetchone()[0],0)
            self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM followup_roots WHERE issue_number=102').fetchone()[0],0)

    def test_created_readback_validates_fixed_body_and_repo(self):
        from tg_triage.issue_format import Issue
        data=dict(number=2,html_url='https://github.com/sandbox/triage/issues/2',body='body',title='title',labels=['bug'])
        gh=GitHubIssues('sandbox/triage','fake',opener=FakeHTTP(data))
        self.assertEqual(gh.verify_created_issue(2,Issue('title','body',('bug',))),2)
        gh=GitHubIssues('sandbox/triage','fake',opener=FakeHTTP({**data,'body':'changed'}))
        with self.assertRaises(GitHubError):gh.verify_created_issue(2,Issue('title','body',('bug',)))

    def test_grounding_rejects_foreign_reversed_or_old_draft_ids(self):
        sources=[message(1,text='Đã chạy tốt'),message(2,text='Lại lỗi')]
        valid=answer('update',lifecycle=dict(kind='recurrence',knownworking_source_ids=[1],recurrence_source_ids=[2]),
                     case_analysis=projection(key_message_ids=[2]))
        self.assertEqual(closed_decision(valid,sources[:1],sources[1]),'recurrence')
        for working,recurring,keys in (([99],[2],[2]),([2],[1],[1]),([1],[2],[1]),([],[2],[2])):
            bad=copy.deepcopy(valid);bad['lifecycle'].update(knownworking_source_ids=working,recurrence_source_ids=recurring)
            bad['case_analysis']['key_message_ids']=keys
            self.assertEqual(closed_decision(bad,sources[:1],sources[1]),'unclear')

    def test_same_message_explicit_previous_success_then_recurrence(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        model.value=answer('update',lifecycle=dict(kind='recurrence',knownworking_source_ids=[10],recurrence_source_ids=[10],
            knownworking_quote='hôm qua đã chạy tốt',recurrence_quote='hôm nay lỗi lại',temporal_order_confirmed=True),
            case_analysis=projection(key_message_ids=[10],details={'actual':'Hôm nay lỗi lại'}))
        self.reply(p,text='hôm qua đã chạy tốt, hôm nay lỗi lại')
        self.assertEqual(len(gh.calls),2)
        self.assertEqual(store.followup_source(message(11,reply_to_id=10))['issue_number'],102)

    def test_reported_configured_model_temporal_pos_case_uses_only_current_episode(self):
        store,model,gh,sent,p=self.setup_lifecycle()
        model.value=answer('update',confidence=.72,lifecycle=dict(kind='recurrence',knownworking_source_ids=[10],recurrence_source_ids=[10],
            knownworking_quote='đã kiểm tra POS in được trên app và web',
            recurrence_quote='lỗi lại không in được ticket',temporal_order_confirmed=True),
            case_analysis=projection(confidence=.72,key_message_ids=[10],details={'actual':'Không in được ticket'}))
        self.reply(p,text='Hôm qua đã kiểm tra POS in được trên app và web; hôm nay lỗi lại không in được ticket.')
        self.assertEqual(len(gh.calls),2)
        self.assertNotIn('Unique original request',gh.calls[-1].body)
        self.assertEqual(json.loads(store.connection.execute('SELECT source_ids FROM followup_episodes').fetchone()[0]),[10])

    def test_same_message_overlap_requires_exact_distinct_temporal_quotes(self):
        msg=message(10,text='hôm qua đã chạy tốt, hôm nay lỗi lại')
        base=answer('update',lifecycle=dict(kind='recurrence',knownworking_source_ids=[10],recurrence_source_ids=[10],
            knownworking_quote='hôm qua đã chạy tốt',recurrence_quote='hôm nay lỗi lại',temporal_order_confirmed=True),
            case_analysis=projection(key_message_ids=[10]))
        for changes in ({'knownworking_quote':'invented success'}, {'recurrence_quote':''},
                        {'recurrence_quote':'hôm qua đã chạy tốt'},{'temporal_order_confirmed':False}):
            invalid=copy.deepcopy(base);invalid['lifecycle'].update(changes)
            self.assertEqual(closed_decision(invalid,[],msg),'unclear')

    def test_lifecycle_get_rejects_foreign_urls_and_pull_requests(self):
        base=dict(number=1,html_url='https://github.com/sandbox/triage/issues/1',body='body',title='title',state='closed')
        for changes in ({'html_url':'https://github.com/evil/repo/issues/1'},{'pull_request':{}},{'state':'unknown'}):
            gh=GitHubIssues('sandbox/triage','fake',opener=FakeHTTP({**base,**changes}))
            with self.assertRaises(GitHubError):gh.lifecycle_snapshot(1)

    def test_closed_refresh_never_patch(self):
        base=dict(number=1,html_url='https://github.com/sandbox/triage/issues/1',body='body',title='title',state='closed')
        http=FakeHTTP(base); gh=GitHubIssues('sandbox/triage','fake',opener=http)
        from tg_triage.issue_format import Issue
        with self.assertRaises(GitHubConflict):
            gh.refresh_issue(1,'unused','title','body',Issue('new','body',('bug',)),expected_labels=['bug'])
        self.assertEqual([r.method for r,_ in http.calls],['GET'])
