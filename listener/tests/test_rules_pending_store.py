import tempfile
import unittest
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tg_triage.rules_pending_store import RulesPendingStore
from tg_triage.decision_rules import Decision, Field
from tg_triage.store import Store
from tg_triage.telegram_io import enqueue_event
from tests.helpers import message


class RulesPendingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'db.sqlite3'
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.open()

    def open(self):
        self.store = Store(self.path, clock=lambda: self.now)
        self.pending = RulesPendingStore(self.store)
        self.addCleanup(lambda: self.store.close())

    def test_attempts_survive_restart_and_stop_after_three_retries(self):
        p = self.pending
        self.assertTrue(p.create('a', -1, 12))
        self.assertEqual(p.reserve_attempt('a', 3), 1)
        self.store.close()
        self.open(); p = self.pending
        for attempt in (2,3,4):
            self.assertEqual(p.reserve_attempt('a', 3), attempt)
            self.assertTrue(p.finish_attempt('a', attempt))
        self.assertIsNone(p.reserve_attempt('a', 3))
        seq = p.reserve_notice('a', -3, 'escalation')
        self.assertEqual(seq, 1)
        self.assertTrue(p.finish_notice('a', seq, None))
        self.assertIsNone(p.reserve_notice('a', -3, 'escalation'))

    def test_reply_requires_sent_question_and_open_unexpired_topic(self):
        p = self.pending; p.create('a', -1, 12)
        seq = p.reserve_notice('a', -3, 'question', ('object',))
        self.assertIsNone(p.bind_reply(-3, 55, 42, 7))
        self.assertTrue(p.finish_notice('a', seq, 55))
        self.assertEqual(p.bind_reply(-3, 55, 42, 7), ('a', 2))
        self.assertIsNone(p.bind_reply(-4, 55, 42, 7))
        self.assertIsNone(p.reserve_notice('a', -3, 'question', ('object',)))
        self.now += timedelta(days=7)
        self.assertIsNone(p.bind_reply(-3, 55, 42, 7))

    def test_success_ends_failure_streak_for_future_reply(self):
        p = self.pending; p.create('a', -1, 12)
        self.assertEqual(p.reserve_attempt('a',3), 1)
        p.finish_attempt('a',1)
        self.assertEqual(p.reserve_attempt('a',3), 2)
        p.finish_attempt('a',2,success=True)
        self.assertEqual(p.reserve_attempt('a',3), 1)
        p.finish_attempt('a',1,success=True)
        self.assertEqual(p.reserve_attempt('a',3), 1)

    def test_collision_keeps_distinct_real_source_provenance(self):
        p = self.pending
        p.create('a', -1, 12); p.create('b', -2, 12)
        qa = p.reserve_notice('a', -3, 'question', ('object',)); p.finish_notice('a', qa, 30)
        qb = p.reserve_notice('b', -3, 'question', ('object',)); p.finish_notice('b', qb, 31)
        self.assertEqual(p.bind_reply(-3,30,12,7), ('a',2))
        self.assertEqual(p.add_source('a', -3, 12), 2)
        self.assertEqual(p.sources('a'), {1:(-1,12), 2:(-3,12)})
        self.assertEqual(p.sources('b'), {1:(-2,12)})
        self.assertEqual(p.add_source('b', -1, 12), 2)
        self.assertEqual(p.sources('b')[2], (-1,12))
        self.assertIsNone(p.bind_reply(-3,31,12,7))

    def test_journal_stores_real_composite_sources_without_fact_values(self):
        p = self.pending; p.create('a', -1, 12); p.add_source('a', -3, 12)
        decision = Decision('READY','bug','I2','R9',{'object':Field('private content',(1,2))},(),(),'fingerprint')
        p.record_decision('a',decision,0.1)
        row = self.store.connection.execute('SELECT * FROM rules_decisions').fetchone()
        self.assertIn('"chat_id":-1,"message_id":12',row['present_sources'])
        self.assertIn('"chat_id":-3,"message_id":12',row['present_sources'])
        self.assertNotIn('private content',str(tuple(row)))
        bad = Decision('READY','bug','I2','R9',{'object':Field('x',(99,))},(),(),'fingerprint')
        with self.assertRaises(ValueError): p.record_decision('a',bad)

    def test_source_group_title_survives_store_reopen(self):
        self.store.enqueue(message(chat_title='Nhóm vận hành'))
        self.store.close(); self.open()
        self.assertEqual(self.store.queued_messages()[0].chat_title, 'Nhóm vận hành')

    def test_actual_ops_event_requires_successful_question_and_keeps_original_identity(self):
        async def exercise():
            p=self.pending; p.create('a',-10001,12)
            sequence=p.reserve_notice('a',-10003,'question',('object',))
            queue=asyncio.Queue()
            def event(mid, reply):
                return SimpleNamespace(out=False,is_private=False,is_group=True,chat_id=-10003,id=mid,
                    raw_text='màn báo cáo',date=self.now,sender_id=1,reply_to_msg_id=reply,reply_to=None,
                    sticker=None,file=None,photo=None,document=None,fwd_from=None,message=None,
                    get_sender=AsyncMock(return_value=SimpleNamespace(first_name='A',last_name=None)),
                    get_chat=AsyncMock(return_value=SimpleNamespace(title='Ops',username=None)))
            kwargs=dict(ops_chat_id=-10003,rules_pending=p,pending_expiry_days=7)
            await enqueue_event(event(20,None),queue,[-10001],self.store,**kwargs)
            await enqueue_event(event(21,55),queue,[-10001],self.store,**kwargs)
            self.assertTrue(queue.empty())
            p.finish_notice('a',sequence,55)
            await enqueue_event(event(12,55),queue,[-10001],self.store,**kwargs)
            accepted=queue.get_nowait()
            self.assertEqual((accepted.chat_id,accepted.message_id,accepted.reply_to_id),(-10003,12,55))
            self.assertEqual(p.sources('a'),{1:(-10001,12),2:(-10003,12)})
            await enqueue_event(event(12,55),queue,[-10001],self.store,**kwargs)
            self.assertTrue(queue.empty())
        asyncio.run(exercise())

    def test_failed_message_insert_rolls_back_binding(self):
        p=self.pending; p.create('a',-10001,12)
        sequence=p.reserve_notice('a',-10003,'question',('object',)); p.finish_notice('a',sequence,55)
        reply=message(12,chat_id=-10003,reply_to_id=55)
        with patch.object(self.store,'_enqueue_sql',side_effect=OSError('write failed')):
            with self.assertRaises(OSError): p.accept_ops_message(reply,7)
        self.assertFalse(p.is_bound_ops_reply(-10003,12))
        self.assertEqual(p.sources('a'),{1:(-10001,12)})
