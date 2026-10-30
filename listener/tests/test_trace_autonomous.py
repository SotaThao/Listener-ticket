"""Autonomous routing and recurrence metadata is debuggable without prose leaks."""
import json
import unittest
from tg_triage.trace import trace
from tg_triage.thinking import init_store,Turn
from tests import test_topic_trace as fixtures
from tests.helpers import message
CHAT,REPO=fixtures.CHAT,fixtures.REPO


class AutonomousTraceTests(unittest.TestCase):
    setUp=fixtures.TopicTraceTests.setUp
    case=fixtures.TopicTraceTests.case
    issue=fixtures.TopicTraceTests.issue
    followup=fixtures.TopicTraceTests.followup

    def test_nonanchor_batch_member_resolves_job_and_issue_expands_all_sources(self):
        key,messages=self.case((448,))
        self.issue(messages[0])
        anchor,source=self.followup(key,messages[0])
        member=message(455,chat_id=CHAT,text='Bổ sung ảnh',has_image=True)
        self.store.enqueue(member)
        with self.store.connection:
            self.store.connection.execute("UPDATE messages SET decision='followup' WHERE chat_id=? AND message_id=?",(CHAT,455))
            job=self.store.connection.execute('SELECT id FROM followup_jobs').fetchone()[0]
            self.store.connection.execute('INSERT INTO followup_job_sources VALUES(?,?,?)',(job,CHAT,455))
        for selector in (dict(chat_id=CHAT,message_id=455),dict(issue=47)):
            result=trace(self.store,repo=REPO,**selector)
            self.assertEqual(result['sections']['followup_jobs'][0]['id'],job)
            self.assertIn(455,[m['message_id'] for m in result['sections']['messages']])
            self.assertNotIn('unproven_topic_membership',result['gaps'])

    def test_routes_plans_episodes_visible_without_raw_plan(self):
        key,messages=self.case((448,))
        self.issue(messages[0])
        self.store.enable_autonomy()
        self.store.connection.execute('INSERT INTO autonomous_routes VALUES(?,?,?,?,?,?,?,?)',
            (CHAT,448,'now','NEW',None,key,'semantic_new','{"private":"PRIVATE_ROUTE_PROSE"}'))
        self.store.connection.execute('INSERT INTO autonomous_plans VALUES(?,?,?,?,?,?)',
            (CHAT,448,'[448]',key,'{"private":"PRIVATE_PLAN_PROSE"}','now'))
        self.store.connection.execute('INSERT INTO followup_episodes VALUES(?,?,?,?,?,?,?,?,?)',
            ('episode:test','parent:test',100,CHAT,448,48,REPO,'[448]','now'))
        self.store.connection.execute('INSERT INTO followup_episode_sources VALUES(?,?,?)',
            (CHAT,448,'episode:test'))
        self.store.connection.commit()
        result=trace(self.store,repo=REPO,issue=47,limit=100)
        output=json.dumps(result)
        self.assertNotIn('PRIVATE_ROUTE_PROSE',output)
        self.assertNotIn('PRIVATE_PLAN_PROSE',output)
        self.assertEqual(result['sections']['autonomous_routes'][0]['action'],'NEW')
        self.assertEqual(result['sections']['autonomous_plans'][0]['source_ids'],[448])
        self.assertEqual(result['sections']['followup_episodes'][0]['issue_number'],48)
        self.assertEqual(result['sections']['followup_episode_sources'][0]['root_key'],'episode:test')

    def test_confirmation_notice_selector_resolves_original(self):
        key,messages=self.case((448,))
        self.issue(messages[0])
        self.store.enable_autonomy()
        self.store.connection.execute('INSERT INTO autonomous_confirmations VALUES(?,?,?,?,?,?)',
            (CHAT,448,47,'sent',901,'now'))
        self.store.connection.commit()
        result=trace(self.store,repo=REPO,chat_id=CHAT,message_id=901)
        self.assertEqual(result['sections']['messages'][0]['message_id'],448)
        self.assertEqual(result['sections']['autonomous_confirmations'][0]['notice_message_id'],901)
        foreign=trace(self.store,repo=REPO,chat_id=CHAT-1,message_id=901)
        self.assertIn('no_stored_source_for_selector',foreign['gaps'])

    def test_clarification_reservation_is_traceable_without_question_or_hash(self):
        key,messages=self.case((448,))
        self.issue(messages[0])
        self.store.enable_autonomy()
        self.store.reserve_autonomous_clarification(key,messages[0],'PRIVATE_QUESTION_TEXT')
        self.assertTrue(self.store.reserve_autonomous_outage(messages[0]))
        result=trace(self.store,repo=REPO,issue=47)
        row=result['sections']['autonomous_clarifications'][0]
        self.assertEqual(row['case_key'],key)
        self.assertEqual(row['message_id'],448)
        self.assertNotIn('question',row)
        self.assertNotIn('question_key',row)
        self.assertNotIn('PRIVATE_QUESTION_TEXT',json.dumps(result))
        self.assertEqual(result['sections']['autonomous_outages'][0]['anchor_message_id'],448)
        self.assertEqual(result['sections']['autonomous_outages'][0]['status'],'open')

    def test_ingress_decision_reason_and_six_read_budget_are_metadata(self):
        key,messages=self.case((448,))
        self.issue(messages[0])
        init_store(self.store)
        journal_root=f'{CHAT}:1'
        turn=Turn(self.store,'ingress:'+journal_root,journal_root,1,max_reads=6)
        turn.reason='semantic_new'
        turn.record('ingress_packing',dict(text_bytes=7999,new_text_cap=700,candidate_text_cap=55,
            candidate_context_truncated=True,retained_candidate_ids=['c1'],omitted_candidate_ids=['c2','PRIVATE_PACKING'],private='PRIVATE_PACKING'))
        turn.record('ingress_review',dict(source_ids=[448,'PRIVATE_REVIEW'],outcome='accepted',reason='host_validated',
            groups=[dict(message_ids=[448,'PRIVATE_REVIEW'],action='NEW',candidate_id=None,summary='PRIVATE_REVIEW'),
                    dict(message_ids=[449],action='CONTINUE',candidate_id='PRIVATE_REVIEW')],private='PRIVATE_REVIEW'),status='verified')
        turn.record('ingress_result',{'source_ids':[448]},status='verified')
        self.store.enable_autonomy()
        self.store.connection.execute('INSERT INTO autonomous_routes VALUES(?,?,?,?,?,?,?,?)',
            (CHAT,448,'now','NEW',None,key,'semantic_new',json.dumps({'decision_turn_key':turn.key})))
        self.store.connection.commit()
        result=trace(self.store,repo=REPO,issue=47,limit=100)
        row=result['sections']['thinking_decisions'][0]
        self.assertEqual(row['intent'],'ingress_result')
        self.assertEqual(row['status'],'verified')
        self.assertEqual(row['reason_code'],'semantic_new')
        self.assertEqual(row['budget']['max_reads'],6)
        packed=next(r for r in result['sections']['thinking_decision_events'] if r.get('intent')=='ingress_packing')
        self.assertEqual(packed['packing']['text_bytes'],7999)
        self.assertEqual(packed['packing']['omitted_candidate_ids'],['c2'])
        self.assertNotIn('PRIVATE_PACKING',json.dumps(result))
        review=next(r for r in result['sections']['thinking_decision_events'] if r.get('intent')=='ingress_review')
        self.assertEqual(review['review'],dict(source_ids=[448],outcome='accepted',reason='host_validated',
            groups=[dict(message_ids=[448],action='NEW',candidate_id=None),dict(message_ids=[449],action='CONTINUE')]))
        self.assertNotIn('PRIVATE_REVIEW',json.dumps(result))
