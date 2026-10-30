"""Old content requires this group's real Telegram reply, including recovery."""
from dataclasses import replace
from unittest import TestCase

from tg_triage.autonomous import resume_case
from tg_triage.classifier import Classifier
from tg_triage.followups import sweep
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import config, message, FakeHTTP, FakeGitHub, NOW
from tests.test_autonomous import Router, route, draft
from tests.test_incident_context import response, wire
from tests import test_followups as followup_fixtures
from tests.test_followups import answer


class ReplyAuthorityTests(TestCase):
    def build(self,*answers):
        store=Store(':memory:');self.addCleanup(store.close)
        p=Pipeline(replace(config('live'),autonomous_enabled=True),store,Router(*answers),FakeGitHub(),
                   notice_sender=lambda c,m,t:900+m)
        return p

    def promoted_pipeline(self):
        p=self.build([route([1],'CLARIFY',question='Anh/Chị bổ sung màn hình giúp Em nhé?')],
                     [route([2],'CONTINUE','c1',draft([1,2]))],
                     [route([3],'CONTINUE','c1',draft([3]))])
        p.sent=[]
        def send(chat,mid,text):
            p.sent.append((chat,mid,text));return 9000+len(p.sent)
        p.notice_sender=send
        p.github.comments=[]
        p.github.add_comment=lambda number,body:p.github.comments.append((number,body)) or 800
        p.github.lifecycle_snapshot=lambda number:dict(number=number,repo=p.config.gh_repo,
            state='open',observed_at=NOW.isoformat())
        p.classifier.followups=[]
        def update(*args,**options):
            p.classifier.followups.append(args)
            return answer('update',summary='Bổ sung bằng chứng lỗi đăng nhập')
        p.classifier.analyze_followup=update
        p.process(message(1,text='Màn hình này cần xử lý',mentions_listener=False))
        p.process(message(2,text='Đăng nhập đúng mật khẩu nhưng bị từ chối',reply_to_id=9001,mentions_listener=False))
        confirmation=p.store.connection.execute('SELECT * FROM autonomous_confirmations').fetchone()
        self.assertEqual('sent',confirmation['status']);self.assertEqual(9002,confirmation['notice_message_id'])
        self.assertEqual(42,confirmation['issue_number'])
        return p

    def test_reply_to_sent_autonomous_confirmation_updates_once_with_actual_edge(self):
        p=self.promoted_pipeline()
        incoming=message(3,text='Bổ sung: cùng lỗi trên web',reply_to_id=9002,mentions_listener=False)
        target=p.store.followup_source(incoming)
        self.assertEqual(42,target['issue_number']);self.assertEqual(1,target['source_message_id'])
        self.assertEqual('followup',p.process(incoming).decision)
        self.assertEqual(1,len(p.github.comments));self.assertEqual(42,p.github.comments[0][0])
        self.assertEqual(1,len(p.github.calls));self.assertEqual(1,len(p.classifier.followups))
        self.assertEqual(9002,p.store.get(incoming.chat_id,3)['reply_to_id'])
        self.assertEqual('done',p.store.connection.execute('SELECT status FROM followup_jobs').fetchone()[0])
        self.assertIsNone(p.process(incoming));sweep(p)
        self.assertEqual(1,len(p.github.comments));self.assertEqual(1,len(p.classifier.followups))

    def test_confirmation_alias_requires_sent_same_chat_current_source_issue(self):
        for bad in ('reserved','ambiguous','other_chat','wrong_issue','wrong_url','wrong_root','no_reply'):
            with self.subTest(bad=bad):
                p=self.promoted_pipeline()
                incoming=message(3,reply_to_id=9002,mentions_listener=False)
                with p.store.connection:
                    if bad in ('reserved','ambiguous'):
                        p.store.connection.execute('UPDATE autonomous_confirmations SET status=?',(bad,))
                    elif bad=='other_chat':
                        incoming=replace(incoming,chat_id=-1002222222222)
                    elif bad=='wrong_issue':
                        p.store.connection.execute('UPDATE autonomous_confirmations SET issue_number=999')
                    elif bad=='wrong_url':
                        p.store.connection.execute("UPDATE messages SET issue_url='https://github.com/sandbox/triage/issues/999' WHERE message_id=1")
                    elif bad=='wrong_root':
                        target=p.store.followup_source(incoming)
                        p.store.connection.execute('''INSERT OR REPLACE INTO followup_roots
                            (root_key,chat_id,source_message_id,group_key,case_key,issue_number)
                            VALUES(?,?,?,?,?,999)''',(target['root_key'],target['chat_id'],target['source_message_id'],
                                target['group_key'],target['case_key']))
                    else:
                        incoming=replace(incoming,reply_to_id=None)
                self.assertIsNone(p.store.followup_source(incoming))
                p.process(incoming)
                self.assertFalse(p.github.comments);self.assertFalse(p.classifier.followups)
                self.assertEqual(1,len(p.github.calls))
                self.assertEqual(incoming.reply_to_id,p.store.get(incoming.chat_id,3)['reply_to_id'])

    def test_old_sent_clarification_notice_still_resolves_after_promotion(self):
        p=self.promoted_pipeline()
        incoming=message(3,text='Cùng lỗi, bổ sung qua tin hỏi cũ',reply_to_id=9001,mentions_listener=False)
        self.assertEqual(42,p.store.followup_source(incoming)['issue_number'])
        p.process(incoming)
        self.assertEqual(1,len(p.github.comments));self.assertEqual(9001,p.store.get(incoming.chat_id,3)['reply_to_id'])

    def test_confirmation_cycle_is_rejected_without_recursive_resolution(self):
        p=self.promoted_pipeline()
        with p.store.connection:
            p.store.connection.execute('UPDATE autonomous_confirmations SET notice_message_id=2')
            p.store.connection.execute("INSERT INTO autonomous_confirmations VALUES(?,2,42,'sent',1,'now')",(message().chat_id,))
        self.assertIsNone(p.store.followup_source(message(3,reply_to_id=2)))
        self.assertIsNone(p.store.followup_source(message(3,reply_to_id=1)))

    def test_stored_sent_confirmation_resolves_when_autonomous_mode_is_off(self):
        p=self.promoted_pipeline()
        p.config=replace(p.config,autonomous_enabled=False)
        p.store._autonomy_enabled=False
        incoming=message(3,text='Bổ sung qua confirmation cũ',reply_to_id=9002,mentions_listener=False)
        self.assertEqual(42,p.store.followup_source(incoming)['issue_number'])
        p.process(incoming)
        self.assertEqual(1,len(p.github.comments));self.assertEqual(9002,p.store.get(incoming.chat_id,3)['reply_to_id'])

    def test_no_reply_cannot_promote_matching_pending_root(self):
        for action in ('CONTINUE','CLARIFY'):
            with self.subTest(action=action):
                p=self.build([route([1],'CLARIFY')],[route([2],action,'c1',draft([1,2]))])
                p.process(message(1,text='Login đúng pass mà không login được'))
                before=dict(p.store.get(message().chat_id,1))
                result=p.process(message(2,text='Login đúng pass mà không login được',mentions_listener=False))
                self.assertEqual('pending_review',result.decision)
                self.assertFalse(p.github.calls);self.assertEqual([],p.classifier.calls[1][1])
                self.assertEqual(before,dict(p.store.get(message().chat_id,1)))

    def test_no_reply_matching_closed_ticket_is_new_without_related_link(self):
        p=self.build([route([1])],[route([2])])
        p.process(message(1));before=dict(p.store.get(message().chat_id,1))
        p.github.lifecycle_snapshot=lambda *a: self.fail('No Reply must not inspect/update old ticket')
        p.process(message(2,mentions_listener=False))
        self.assertEqual(2,len(p.github.calls));self.assertEqual([],p.classifier.calls[1][1])
        self.assertNotIn('Ticket liên quan:',p.github.calls[1].body)
        self.assertEqual(before,dict(p.store.get(message().chat_id,1)))

    def test_group_cannot_borrow_sibling_reply_authority(self):
        p=self.build([route([1])],[route([2],'CONTINUE','c1'),route([3],'CLARIFY','c1')])
        p.process(message(1));before=dict(p.store.get(message().chat_id,1))
        p.store.enqueue(message(3,text='Cùng lỗi nhưng không reply',mentions_listener=False))
        p.process(message(2,reply_to_id=1,mentions_listener=False))
        self.assertEqual([2],p.classifier.calls[1][1][0]['reply_source_ids'])
        self.assertEqual(1,len(p.github.calls));self.assertEqual(before,dict(p.store.get(message().chat_id,1)))
        self.assertEqual(0,p.store.connection.execute('SELECT COUNT(*) FROM followup_jobs').fetchone()[0])

    def test_direct_classifier_rejects_optional_candidate_continuation_without_reply(self):
        for action in ('CONTINUE','CLARIFY'):
            with self.subTest(action=action):
                choice=dict(candidate_id='c1',pending=True,exact_reply=False,reply_source_ids=[],messages=[message(1)])
                http=FakeHTTP(response(wire([2],action=action,candidate='c1')))
                classifier=Classifier(config().ollama,opener=http)
                self.assertIsNone(classifier.analyze_ingress([message(2)],[choice]))
                self.assertEqual('invalid_ingress_output',classifier.last_ingress_outcome)

    def legacy_plan(self,p,mid=2):
        p.process(message(1))
        incoming=message(mid,mentions_listener=False);p.store.enqueue(incoming)
        candidate=p.store.autonomy_candidates(incoming,p.config.gh_repo)[0]
        candidate.update(candidate_id='c1',reply_source_ids=[],exact_reply=False)
        group=route([mid],'CONTINUE','c1',draft([1,mid]));group['candidate']=candidate
        p.store.save_autonomous_plans([incoming],[group],p.config.gh_repo)
        return incoming,candidate

    def test_cached_implicit_pending_plan_cannot_execute_after_upgrade(self):
        p=self.build([route([1],'CLARIFY')]);incoming,_=self.legacy_plan(p)
        before=dict(p.store.get(incoming.chat_id,1))
        result=p.process(incoming)
        self.assertEqual('cached_reply_authority_revoked',result.reason)
        self.assertFalse(p.github.calls);self.assertEqual(before,dict(p.store.get(incoming.chat_id,1)))

    def test_periodic_resume_implicit_pending_plan_cannot_execute_after_upgrade(self):
        p=self.build([route([1],'CLARIFY')]);incoming,candidate=self.legacy_plan(p)
        p.store.start_classifying(incoming)
        key,_,_=p.store.autonomy_case([incoming],candidate)
        with p.store.connection:p.store.connection.execute("UPDATE cases SET status='open' WHERE case_key=?",(key,))
        result=resume_case(p,key)
        self.assertEqual('cached_reply_authority_revoked',result.reason);self.assertFalse(p.github.calls)

    def test_queued_planned_ambiguous_followup_recovery_requires_original_reply(self):
        for state in ('queued','planned','ambiguous'):
            with self.subTest(state=state):
                helper=followup_fixtures.FollowupTests('test_split_two_total_and_reply_new_message_preserves_original_fields')
                store,model,gh,sent,p=helper.setup_case(count=1);self.addCleanup(store.close)
                actual=message(10,text='Bổ sung cùng lỗi nhưng không reply',mentions_listener=False)
                store.start_classifying(actual)
                source=store.followup_source(message(10,reply_to_id=901))
                store.enqueue_followup(actual,source)
                job=store.pending_followups()[0]
                if state!='queued':
                    store.save_followup_plan(job,answer('update'),[('comment',dict(number=101,body='Old planned write'))])
                if state=='ambiguous':
                    with store.connection:
                        store.connection.execute("UPDATE followup_jobs SET status='ambiguous' WHERE id=?",(job['id'],))
                        store.connection.execute("UPDATE followup_operations SET status='ambiguous',reserved_at='unknown' WHERE job_id=?",(job['id'],))
                operations_before=store.followup_operations(job)
                sweep(p)
                self.assertFalse(model.followups);self.assertFalse(gh.comments);self.assertEqual(1,len(gh.calls))
                self.assertEqual('blocked',store.connection.execute('SELECT status FROM followup_jobs').fetchone()[0])
                self.assertEqual(operations_before,store.followup_operations(job))
                self.assertIsNone(store.get(actual.chat_id,10)['reply_to_id'])

    def test_reply_within_unlinked_job_is_not_old_root_authority(self):
        helper=followup_fixtures.FollowupTests('test_split_two_total_and_reply_new_message_preserves_original_fields')
        store,model,gh,sent,p=helper.setup_case(count=1);self.addCleanup(store.close)
        sources=[message(10,text='Unlinked new request'),message(11,text='Details',reply_to_id=10)]
        for m in sources:store.start_classifying(m)
        store.enqueue_followup_batch(sources,store.followup_source(message(10,reply_to_id=901)))
        sweep(p)
        self.assertFalse(model.followups);self.assertFalse(gh.comments)
        self.assertEqual('blocked',store.connection.execute('SELECT status FROM followup_jobs').fetchone()[0])
