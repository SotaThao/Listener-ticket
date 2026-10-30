"""Minimal grounded role decisions and explicit zero-operation recovery."""
import asyncio
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

from tg_triage.store import Store
from tg_triage.pipeline import Pipeline
from tg_triage.telegram_io import make_group_notice_editor
from tests import test_followup_context as context_fixtures
OWNER=context_fixtures.OWNER
from tests.helpers import message,NOW,FakeHTTP


class RetryTests(unittest.TestCase):
    setup_conversation=context_fixtures.ContextTests.setup_conversation
    provider=context_fixtures.ContextTests.provider
    legacy=context_fixtures.ContextTests.legacy
    import_old=context_fixtures.ContextTests.import_old

    def failed(self,path=':memory:'):
        store,gh,sent,p=self.setup_conversation(path); self.import_old(store)
        self.provider(p,error=OSError('offline'))
        p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
        job=dict(store.connection.execute('SELECT * FROM followup_jobs WHERE message_id=440').fetchone())
        return store,gh,sent,p,job

    def retry(self,store,p):
        return store.retry_followup_analysis(message().chat_id,440,p.config.gh_repo,1440)

    def test_minimal_role_request_never_calls_full_followup(self):
        store,gh,sent,p=self.setup_conversation(); self.import_old(store)
        http=self.provider(p)
        with patch.object(p.classifier,'analyze_followup',side_effect=AssertionError('No full draft call')):
            p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
        self.assertEqual(len(http.calls),1); self.assertEqual(len(gh.calls),2)
        body=json.loads(http.calls[0][0].data)
        self.assertEqual(set(body['format']['properties']),{'source_roles'})
        context=json.loads(body['messages'][-1]['content'])
        self.assertEqual(set(context),{'confirmed_requests','unresolved'})
        self.assertEqual(context['unresolved'],[{'id':430,'text':'hay tại nó chưa lên APP nên nó bị như vậy'}])
        self.assertLess(len(json.dumps(body,ensure_ascii=False)),2500)
        self.assertNotIn('HISTORY',json.dumps(body)); self.assertNotIn('TRUSTED_TICKETS',json.dumps(body))

    def test_role_ids_must_exactly_cover_requested_subset(self):
        for roles in ([],[dict(message_id=429,role='ancillary',confidence=.99)],
                      [dict(message_id=430,role='ancillary',confidence=.99)]*2):
            store,gh,sent,p=self.setup_conversation(); self.import_old(store)
            self.provider(p,dict(source_roles=roles))
            p.process(message(440,text='đây là ticket 2',reply_to_id=431,sender_id=OWNER))
            self.assertEqual(len(gh.calls),1); self.assertEqual(gh.comments,[])
            plan=json.loads(store.connection.execute('SELECT plan FROM followup_jobs WHERE message_id=440').fetchone()[0])
            self.assertEqual(plan['context_reason'],'invalid_source_roles')

    def test_explicit_retry_edits_existing_notice_once_with_immutable_audit(self):
        store,gh,sent,p,job=self.failed()
        original_notice=dict(store.connection.execute('SELECT * FROM group_notices WHERE message_id=440').fetchone())
        originals=[dict(row) for row in store.connection.execute('SELECT * FROM messages WHERE message_id BETWEEN 429 AND 432')]
        cases=[dict(row) for row in store.connection.execute('SELECT * FROM cases')]
        edits=[]
        def edit(chat,notice,text):
            self.assertEqual(store.followup_analysis_retry(job['id'])['edit_status'],'reserved')
            edits.append((chat,notice,text)); return True
        p.notice_editor=edit
        self.assertTrue(self.retry(store,p)); self.assertFalse(self.retry(store,p))
        self.provider(p); p.close_due_cases(); p.close_due_cases()
        self.assertEqual(len(gh.calls),2); self.assertEqual(len(edits),1)
        self.assertEqual(edits[0][1],1440); self.assertIn('/issues/102',edits[0][2])
        self.assertEqual(len(sent),2) # initial429 + failed440, no second send for retry
        self.assertEqual(dict(store.connection.execute('SELECT * FROM group_notices WHERE message_id=440').fetchone()),original_notice)
        self.assertEqual([dict(row) for row in store.connection.execute('SELECT * FROM messages WHERE message_id BETWEEN 429 AND 432')],originals)
        self.assertEqual([dict(row) for row in store.connection.execute('SELECT * FROM cases')],cases)
        audit=store.followup_analysis_retry(job['id'])
        self.assertEqual(audit['original_plan'],job['plan']); self.assertEqual(audit['original_result'],job['result'])
        self.assertEqual(audit['edit_status'],'done'); self.assertFalse(self.retry(store,p))

    def test_retry_refuses_unsafe_or_unmatched_state(self):
        mutations=("UPDATE followup_jobs SET status='blocked' WHERE message_id=440",
                   "UPDATE followup_jobs SET plan='{\"action\":\"clarify\",\"context_reason\":\"invalid_output\"}' WHERE message_id=440",
                   "UPDATE followup_roots SET siblings='[999]'",
                   "UPDATE followup_roots SET split_plan='{}'",
                   "UPDATE group_notices SET status='failed' WHERE message_id=440",
                   "UPDATE messages SET issue_url='https://github.com/evil/repo/issues/101' WHERE message_id=429")
        for sql in mutations:
            store,gh,sent,p,job=self.failed()
            with store.connection: store.connection.execute(sql)
            self.assertFalse(self.retry(store,p)); self.assertIsNone(store.followup_analysis_retry(job['id']))
        store,gh,sent,p,job=self.failed()
        with store.connection:
            store.connection.execute("INSERT INTO followup_operations(job_id,ordinal,kind,payload) VALUES (1,0,'comment','{}')")
        self.assertFalse(self.retry(store,p))
        store2,gh2,sent2,p2,job2=self.failed()
        with store2.connection:
            store2.connection.execute("INSERT INTO followup_jobs(chat_id,message_id,root_key) VALUES (?,?,?)",(message().chat_id,450,job2['root_key']))
        self.assertFalse(self.retry(store2,p2))
        store3,gh3,sent3,p3,job3=self.failed()
        self.assertFalse(store3.retry_followup_analysis(message().chat_id,440,p3.config.gh_repo,999))
        self.assertFalse(store3.retry_followup_analysis(-999,440,p3.config.gh_repo,1440))

    def test_edit_failure_never_falls_back_to_send_or_retries(self):
        store,gh,sent,p,job=self.failed(); edits=[]
        p.notice_editor=lambda *args:edits.append(args) or False
        self.assertTrue(self.retry(store,p)); self.provider(p); p.close_due_cases(); p.close_due_cases()
        self.assertEqual(len(edits),1); self.assertEqual(len(sent),2)
        self.assertEqual(store.followup_analysis_retry(job['id'])['edit_status'],'ambiguous')
        self.assertEqual(len(gh.calls),2)

    def test_planned_edit_resumes_after_restart_without_replaying_github(self):
        directory=self.enterContext(tempfile.TemporaryDirectory()); path=Path(directory)/'db'
        store,gh,sent,p,job=self.failed(path)
        self.assertTrue(self.retry(store,p)); self.provider(p); p.close_due_cases()
        self.assertEqual(len(gh.calls),2); self.assertEqual(store.followup_analysis_retry(job['id'])['edit_status'],'planned')
        restarted=Store(path,clock=lambda:NOW); self.addCleanup(restarted.close)
        edits=[]; p2=Pipeline(p.config,restarted,p.classifier,gh,notice_sender=p.notice_sender,
                            notice_editor=lambda *args:edits.append(args) or True)
        p2.close_due_cases(); p2.close_due_cases()
        self.assertEqual(len(gh.calls),2); self.assertEqual(len(edits),1); self.assertEqual(len(sent),2)

    def test_reserved_edit_crash_is_terminal_after_restart(self):
        directory=self.enterContext(tempfile.TemporaryDirectory()); path=Path(directory)/'db'
        store,gh,sent,p,job=self.failed(path)
        self.assertTrue(self.retry(store,p)); self.provider(p)
        def crash(*args): raise SystemExit
        p.notice_editor=crash
        with self.assertRaises(SystemExit): p.close_due_cases()
        restarted=Store(path,clock=lambda:NOW); self.addCleanup(restarted.close)
        edits=[]; p2=Pipeline(p.config,restarted,p.classifier,gh,notice_sender=p.notice_sender,
                            notice_editor=lambda *args:edits.append(args) or True)
        p2.close_due_cases()
        self.assertEqual(edits,[]); self.assertEqual(len(gh.calls),2)
        self.assertEqual(restarted.followup_analysis_retry(job['id'])['edit_status'],'ambiguous')

    def test_retry_dry_run_no_external_reservations_or_edit(self):
        store,gh,sent,p,job=self.failed()
        self.assertTrue(self.retry(store,p)); self.provider(p); p.config=replace(p.config,mode='dry-run')
        edits=[]; p.notice_editor=lambda *args:edits.append(args) or True
        p.close_due_cases()
        self.assertEqual(len(gh.calls),1); self.assertEqual(len(sent),2); self.assertEqual(edits,[])
        self.assertEqual(store.followup_analysis_retry(job['id'])['edit_status'],'planned')
        self.assertEqual(store.connection.execute('SELECT count(*) FROM followup_operations WHERE reserved_at IS NOT NULL').fetchone()[0],0)


class EditorTests(unittest.IsolatedAsyncioTestCase):
    async def test_editor_group_only_existing_id_plain_text(self):
        import sys
        types=SimpleNamespace(Chat=type('Chat',(),{}))
        with patch.dict(sys.modules,{'telethon':SimpleNamespace(),'telethon.tl':SimpleNamespace(types=types)}):
            entity=types.Chat(); client=SimpleNamespace(get_entity=AsyncMock(return_value=entity),
                edit_message=AsyncMock(return_value=SimpleNamespace(id=441)))
            editor=make_group_notice_editor(client)
            self.assertTrue(await editor(-123,441,'Em đã tách rồi nhé'))
            client.edit_message.assert_awaited_once_with(entity,441,'Em đã tách rồi nhé',parse_mode=None,link_preview=False)
            client.get_entity.return_value=SimpleNamespace(megagroup=False)
            self.assertFalse(await editor(-123,441,'no private edit'))
            self.assertEqual(client.edit_message.await_count,1)
