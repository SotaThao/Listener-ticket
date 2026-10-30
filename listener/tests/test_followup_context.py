"""Conversation-shaped regression with real followup serialization and offline provider HTTP."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from tg_triage.classifier import Classifier
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import config,message,FakeTopicModel,topic,FakeHTTP,NOW
from tests.test_followups import GitHub,answer,group

OWNER=1603321775
TEXTS={429:'nên thêm ký tự',430:'hay tại nó chưa lên APP nên nó bị như vậy',
       431:'@philip sao cái stop opt out em chưa gắn vào nữa',432:''}


class ContextTests(unittest.TestCase):
    def setup_conversation(self,path=':memory:',extra_text=None):
        store=Store(path,clock=lambda:NOW); self.addCleanup(store.close)
        gh=GitHub(); sent=[]
        p=Pipeline(config('live',window=5,max_messages=4),store,
                   FakeTopicModel((topic([429,430,431,432],verdict='needs_human'),)),gh,
                   notice_sender=lambda chat,target,text:sent.append((target,text)) or target+1000)
        for mid,text in TEXTS.items():
            p.process(message(mid,text=extra_text if mid==430 and extra_text else text,
                              sender_id=OWNER,has_image=mid in (429,431,432)))
        source=store.get(message().chat_id,429)
        with store.connection:
            for mid in (429,431,432):
                store.connection.execute("INSERT INTO attachments(case_key,message_id,asset_name,status,called_at) VALUES (?,?,?,'uploaded',?)",
                                         (source['group_key'],mid,f'{mid}-1.jpg',NOW.isoformat()))
        return store,gh,sent,p

    def provider(self,p,value=None,error=None):
        content=json.dumps(value or dict(source_roles=[dict(message_id=430,role='ancillary',confidence=.95)]),ensure_ascii=False)
        http=FakeHTTP(error if error else {'message':{'content':content}})
        p.classifier=Classifier(p.config.ollama,prompt='unused',opener=http)
        return http

    def legacy(self,store,mid,text,target,done=True):
        incoming=message(mid,text=text,reply_to_id=target,sender_id=OWNER)
        store.start_classifying(incoming)
        if done:
            store.enqueue_followup(incoming,store.followup_source(incoming))
            job=next(j for j in store.pending_followups() if j['message_id']==mid)
            store.save_followup_plan(job,dict(action='clarify',questions=['old generic']),[])
            store.finish_followup(job,'done','old generic')
            store.reserve_group_notice(incoming.chat_id,mid,'followup')
            store.finish_group_notice(incoming.chat_id,mid,'sent',mid+1000)
        else:
            store.assign_case(incoming,window_minutes=5,max_messages=40)
        return incoming

    def import_old(self,store):
        self.legacy(store,434,'đây là 2 yêu cầu khác nhau. cần lên 2 ticket khác nhau',429)
        self.legacy(store,436,'đây là 2 tin nhắn cần lên 2 ticket riêng biệt',434)
        self.legacy(store,438,'đây là ticket 1',429)

    def test_exact_legacy_440_recovery_serializes_context_and_creates_two_total(self):
        store,gh,sent,p=self.setup_conversation(); self.import_old(store)
        incoming=self.legacy(store,440,'đây là ticket 2',431,done=False)
        original=dict(store.get(incoming.chat_id,429)); original_case=dict(store.get_case(original['case_key']))
        http=self.provider(p)
        self.assertTrue(store.ensure_followup_job(incoming.chat_id,440))
        self.assertFalse(store.ensure_followup_job(incoming.chat_id,440))
        p.close_due_cases()
        self.assertEqual(len(gh.calls),2); self.assertEqual(len(gh.comments),1)
        self.assertEqual(sent[-1][0],440)
        self.assertIn('/issues/101',sent[-1][1]); self.assertIn('/issues/102',sent[-1][1])
        self.assertIn('hay tại',sent[-1][1]); self.assertIn('ảnh không caption',sent[-1][1])
        self.assertIn('stop opt out',gh.calls[1].body); self.assertIn('/431-1.jpg',gh.calls[1].body)
        self.assertNotIn('/432-1.jpg',gh.calls[1].body); self.assertNotIn('hay tại',gh.calls[1].body)
        self.assertIn('Tin/ảnh chưa được gán',gh.comments[0][1]); self.assertIn('hay tại',gh.comments[0][1])
        self.assertIn('/432-1.jpg',gh.comments[0][1])
        plan=json.loads(store.connection.execute('SELECT split_plan FROM followup_roots').fetchone()[0])
        assigned=[i for g in plan['groups'] for i in g['message_ids']]
        self.assertEqual(assigned,[429,431]); self.assertEqual(plan['unresolved_message_ids'],[430,432])
        self.assertEqual(set(assigned+plan['unresolved_message_ids']),set(TEXTS))
        self.assertEqual(dict(store.get(incoming.chat_id,429)),original)
        self.assertEqual(dict(store.get_case(original['case_key'])),original_case)
        body=json.loads(http.calls[0][0].data); context=json.loads(body['messages'][-1]['content'])
        self.assertEqual([r['id'] for r in context['confirmed_requests']],[429,431])
        self.assertEqual([r['id'] for r in context['unresolved']],[430])
        self.assertNotIn('HISTORY',context); self.assertNotIn('REPLY',context)
        self.assertNotIn('groups',body['format']['properties'])
        self.assertEqual(set(body['format']['properties']),{'source_roles'})
        self.assertEqual(len(body['messages']),2)
        p.close_due_cases(); self.assertEqual(len(gh.calls),2)

    def test_general_followup_serialization_retains_reply_and_chronological_history(self):
        store,gh,sent,p=self.setup_conversation(); self.import_old(store)
        incoming=message(440,text='please clarify',reply_to_id=431,sender_id=OWNER)
        store.start_classifying(incoming); store.enqueue_followup(incoming,store.followup_source(incoming))
        job=next(j for j in store.pending_followups() if j['message_id']==440)
        root,originals,reply,history=store.followup_context(job)
        http=FakeHTTP({'message':{'content':json.dumps(answer('clarify'))}})
        classifier=Classifier(p.config.ollama,prompt='unused',opener=http)
        classifier.analyze_followup(originals,reply,[{'ordinal':0,'issue_number':101}],history)
        body=json.loads(http.calls[0][0].data); context=json.loads(body['messages'][-1]['content'])
        self.assertEqual(context['REPLY']['reply_to_id'],431)
        self.assertEqual(context['REPLY']['sender_id'],OWNER)
        self.assertEqual([h['message_id'] for h in context['HISTORY']],[434,436,438])
        self.assertEqual(context['HISTORY'][-1]['reply_to_id'],429)

    def test_direct_nonfirst_reply_routes_and_partial_label_ack_does_not_call_model(self):
        store,gh,sent,p=self.setup_conversation()
        self.legacy(store,434,'cần lên 2 ticket',429)
        with patch.object(p.classifier,'analyze_followup',create=True,side_effect=AssertionError('partial needs no model')):
            p.process(message(438,text='đây là ticket 1',reply_to_id=429,sender_id=OWNER))
        self.assertIn('nên thêm ký tự',sent[-1][1]); self.assertIn('ticket 1',sent[-1][1])
        self.assertNotIn('Em chưa xác định chắc',sent[-1][1])
        self.provider(p)
        p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
        self.assertEqual(len(gh.calls),2)
        self.assertEqual(store.get(message().chat_id,440)['group_key'],store.get(message().chat_id,429)['group_key'])

    def test_provider_failure_preserves_anchors_and_asks_specific_remaining_text(self):
        store,gh,sent,p=self.setup_conversation(); self.import_old(store)
        self.provider(p,error=OSError('provider unavailable'))
        p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
        self.assertEqual(len(gh.calls),1); self.assertEqual(gh.comments,[])
        self.assertIn('nên thêm ký tự',sent[-1][1]); self.assertIn('stop opt out',sent[-1][1])
        self.assertIn('hay tại',sent[-1][1]); self.assertIn('phân tích hợp lệ',sent[-1][1])
        state=json.loads(store.connection.execute('SELECT context_state FROM followup_roots').fetchone()[0])
        self.assertEqual(state['bindings'],{'429':0,'431':1})
        job=json.loads(store.connection.execute('SELECT plan FROM followup_jobs WHERE message_id=440').fetchone()[0])
        self.assertEqual(job['context_reason'],'provider_unavailable')

    def test_third_request_or_unknown_role_never_suppressed(self):
        for role in ('request','unknown'):
            store,gh,sent,p=self.setup_conversation(extra_text='Please implement another independent work request')
            self.import_old(store)
            self.provider(p,answer('clarify',source_roles=[dict(message_id=430,role=role,confidence=.99)]))
            p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
            self.assertEqual(len(gh.calls),1); self.assertEqual(gh.comments,[])
            self.assertIn('Please implement',sent[-1][1])

    def test_full_partition_cannot_override_explicit_third_request_role(self):
        store,gh,sent,p=self.setup_conversation(extra_text='Another independent work request')
        self.import_old(store)
        self.provider(p,answer('split',groups=[group([429,430]),group([431,432])],
                               source_roles=[dict(message_id=430,role='request',confidence=.99)]))
        p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
        self.assertEqual(len(gh.calls),1); self.assertEqual(gh.comments,[])

    def test_foreign_or_duplicate_source_role_ids_cannot_bypass_full_partition(self):
        for roles in ([dict(message_id=999,role='ancillary',confidence=.99)],
                      [dict(message_id=430,role='ancillary',confidence=.99)]*2):
            with self.subTest(roles=roles):
                store,gh,sent,p=self.setup_conversation(); self.import_old(store)
                self.provider(p,answer('split',groups=[group([429,430]),group([431,432])],source_roles=roles))
                p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
                self.assertEqual(len(gh.calls),1); self.assertEqual(gh.comments,[])
                plan=json.loads(store.connection.execute('SELECT plan FROM followup_jobs WHERE message_id=440').fetchone()[0])
                self.assertEqual(plan['context_reason'],'invalid_source_roles')

    def test_restart_and_trimmed_model_history_retain_prior_binding(self):
        directory=self.enterContext(tempfile.TemporaryDirectory()); path=Path(directory)/'db'
        store,gh,sent,p=self.setup_conversation(path); self.import_old(store)
        self.provider(p,error=OSError('offline'))
        p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
        restarted=Store(path,clock=lambda:NOW); self.addCleanup(restarted.close)
        p2=Pipeline(p.config,restarted,p.classifier,gh,notice_sender=p.notice_sender)
        for mid in range(441,447):
            self.legacy(restarted,mid,'cảm ơn',429)
        self.provider(p2)
        p2.process(message(448,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
        self.assertEqual(len(gh.calls),2)
        state=json.loads(restarted.connection.execute('SELECT context_state FROM followup_roots').fetchone()[0])
        self.assertEqual(state['bindings']['429'],0)

    def test_historical_conflict_persists_and_requires_same_anchor_repair(self):
        store,gh,sent,p=self.setup_conversation(); self.import_old(store)
        self.legacy(store,439,'đây là ticket 2',429)
        self.provider(p)
        p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
        self.assertEqual(len(gh.calls),1)
        state=json.loads(store.connection.execute('SELECT context_state FROM followup_roots').fetchone()[0])
        self.assertIn('429',state['conflicts'])
        p.process(message(441,text='cảm ơn',reply_to_id=429,sender_id=OWNER))
        self.assertEqual(len(gh.calls),1)
        self.provider(p)
        p.process(message(442,text='đây là ticket 1',reply_to_id=429,sender_id=OWNER))
        self.assertEqual(len(gh.calls),2)

    def test_foreign_chat_author_and_unproven_binding_block(self):
        store,gh,sent,p=self.setup_conversation(); self.import_old(store)
        self.assertIsNone(store.followup_source(message(440,reply_to_id=431,chat_id=-99)))
        self.provider(p)
        p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER+1))
        self.assertEqual(len(gh.calls),1)
        state=json.loads(store.connection.execute('SELECT context_state FROM followup_roots').fetchone()[0])
        state['bindings']['430']=0; state['evidence']['430']=438
        with store.connection:
            store.connection.execute('UPDATE followup_roots SET context_state=?',(json.dumps(state),))
        p.process(message(441,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
        self.assertEqual(len(gh.calls),1)
        state=json.loads(store.connection.execute('SELECT context_state FROM followup_roots').fetchone()[0])
        self.assertIn('invalid:430',state['conflicts'])

    def test_truncated_provider_json_is_identified_not_true_uncertainty(self):
        store,gh,sent,p=self.setup_conversation(); self.import_old(store)
        http=FakeHTTP({'message':{'content':'{"action":"split", "groups":['}})
        p.classifier=Classifier(p.config.ollama,prompt='unused',opener=http)
        p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
        self.assertEqual(len(gh.calls),1)
        plan=json.loads(store.connection.execute('SELECT plan FROM followup_jobs WHERE message_id=440').fetchone()[0])
        self.assertEqual(plan['context_reason'],'invalid_output')
        self.assertIn('hay tại',sent[-1][1])

    def test_later_confirmed_media_assignment_comments_correct_sibling_without_new_issue(self):
        store,gh,sent,p=self.setup_conversation(); self.import_old(store)
        self.provider(p)
        p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
        p.process(message(442,text='đây là ticket 2',reply_to_id=432,sender_id=OWNER))
        self.assertEqual(len(gh.calls),2)
        self.assertEqual(gh.comments[-1][0],102)
        self.assertIn('/432-1.jpg',gh.comments[-1][1])
        state=json.loads(store.connection.execute('SELECT context_state FROM followup_roots').fetchone()[0])
        self.assertEqual(state['bindings']['432'],1)

    def test_negated_or_quoted_labels_never_confirm_binding(self):
        from tg_triage.followups import _explicit_label,_explicit_count
        for text in ('không phải ticket 2','đừng tạo ticket 2','"đây là ticket 2"',
                     'đừng tạo 2 ticket','không cần lên 2 ticket','“cần lên 2 ticket”'):
            self.assertIsNone(_explicit_label(text))
            self.assertIsNone(_explicit_count(text))

    def test_captionless_only_unassigned_bypasses_provider_with_complete_anchors(self):
        store,gh,sent,p=self.setup_conversation()
        with store.connection:
            store.connection.execute("UPDATE messages SET text='',has_image=1 WHERE message_id=430")
        self.import_old(store)
        with patch.object(p.classifier,'analyze_followup',create=True,side_effect=AssertionError('grounded anchors should not wait')):
            p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
        self.assertEqual(len(gh.calls),2)
