from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import config,message,FakeGitHub,NOW
from tests.test_autonomous import Router,route,draft


class ConversationContextTests(TestCase):
    def build(self,router,store=None,sender=None):
        store=store or Store(':memory:')
        self.addCleanup(store.close)
        return Pipeline(replace(config('live'),autonomous_enabled=True),store,router,FakeGitHub(),notice_sender=sender)

    def test_late_photo_reply_notice_retains_question_and_original_sources(self):
        sent=[]
        router=Router([route([1],'CLARIFY',question='Anh/Chị gửi ảnh lỗi giúp Em nhé?')],
                      [route([2],'CONTINUE','c1',draft([1,2]))])
        p=self.build(router,sender=lambda c,m,t:sent.append((m,t)) or 901)
        p.process(message(1,text='Không đăng nhập được'))
        result=p.process(message(2,text='Đây là ảnh báo lỗi đăng nhập',has_image=True,
                                 reply_to_id=901,received_at=NOW+timedelta(minutes=5)))
        candidates=router.calls[1][1]
        self.assertTrue(candidates[0]['exact_reply'])
        self.assertEqual(['Anh/Chị gửi ảnh lỗi giúp Em nhé?'],candidates[0]['pending_questions'])
        self.assertEqual('sent',candidates[0]['clarification_outcomes'][0]['delivery_status'])
        self.assertEqual([1], [m.message_id for m in candidates[0]['messages']])
        self.assertEqual('issue',result.decision)
        self.assertEqual(2,p.store.reconcile(result.case_key).received)
        self.assertEqual([1,1],[v[0] for v in sent])

    def test_duplicate_question_after_restart_is_suppressed_sources_accounted(self):
        sent=[]
        with TemporaryDirectory() as directory:
            path=str(Path(directory)/'context.sqlite3')
            store=Store(path)
            p=self.build(Router([route([1],'CLARIFY',question='Màn hình nào?')]),store,
                         lambda c,m,t:sent.append(m) or 901)
            p.process(message(1)); store.close()
            store=Store(path)
            p=self.build(Router([route([2],'CLARIFY','c1',question='Màn hình nào?')]),store,
                         lambda c,m,t:sent.append(m) or 902)
            result=p.process(message(2,reply_to_id=901,received_at=NOW+timedelta(days=1)))
            self.assertEqual([1],sent)
            self.assertEqual(2,p.store.reconcile(result.case_key).received)
            self.assertEqual('pending_review',p.store.get(message().chat_id,2)['decision'])
            store.close()

    def test_new_essential_question_and_different_topic_are_not_suppressed(self):
        sent=[]
        p=self.build(Router([route([1],'CLARIFY',question='Màn hình nào?')],
                            [route([2],'CLARIFY','c1',question='Báo lỗi gì?')],
                            [route([3],'CLARIFY',question='Màn hình nào?')]),
                     sender=lambda c,m,t:sent.append(m) or 900+m)
        p.process(message(1)); p.process(message(2,reply_to_id=1)); p.process(message(3,text='Yêu cầu khác'))
        self.assertEqual([1,2,3],sent)
        self.assertNotEqual(p.store.get(message().chat_id,1)['case_key'],p.store.get(message().chat_id,3)['case_key'])

    def test_ambiguous_transport_never_resends_same_question(self):
        sent=[]
        def send(c,m,t):
            sent.append(m)
            raise TimeoutError('Unknown delivery')
        p=self.build(Router([route([1],'CLARIFY',question='Màn hình nào?')],
                            [route([2],'CONTINUE','c1',draft([1,2],verdict='needs_human',actionable=False),question='')]),sender=send)
        p.process(message(1)); p.process(message(2,reply_to_id=1))
        self.assertEqual([1],sent)
        candidates=p.classifier.calls[1][1]
        self.assertEqual('failed',candidates[0]['clarification_outcomes'][0]['delivery_status'])

    def test_failed_semantic_reservation_prevents_send(self):
        sent=[]
        p=self.build(Router([route([1],'CLARIFY',question='Màn hình nào?')]),sender=lambda *args:sent.append(args))
        with patch.object(p.store,'reserve_autonomous_clarification',side_effect=OSError('reservation unavailable')):
            p.process(message(1))
        self.assertEqual([],sent)
        self.assertEqual('pending_review',p.store.get(message().chat_id,1)['decision'])

    def test_provider_failure_one_batch_notice_without_semantic_merge(self):
        sent=[]
        p=self.build(Router(None),sender=lambda c,m,t:sent.append((m,t)) or 901)
        p.store.enqueue(message(2,text='Đổi bảng tuyển dụng'))
        p.process(message(1,text='Lỗi đăng nhập'))
        self.assertEqual([1],[item[0] for item in sent])
        self.assertFalse(p.github.calls)
        keys=[p.store.get(message().chat_id,i)['case_key'] for i in (1,2)]
        self.assertNotEqual(*keys)
        self.assertEqual([1,1],[p.store.reconcile(key).received for key in keys])
        self.assertEqual(2,p.store.connection.execute('SELECT COUNT(*) FROM autonomous_routes').fetchone()[0])

    def test_crash_after_question_reservation_is_terminal_on_next_reply(self):
        sent=[]
        p=self.build(Router([route([1],'CLARIFY',question='Màn hình nào?')],
                            [route([2],'CLARIFY','c1',question='Màn hình nào?')]),
                     sender=lambda c,m,t:sent.append(m) or 901)
        with patch('tg_triage.autonomous.group_notices.notify',side_effect=SystemExit('crash before transport')):
            with self.assertRaises(SystemExit):
                p.process(message(1))
        p.process(message(2,reply_to_id=1,received_at=NOW+timedelta(minutes=10)))
        self.assertEqual([],sent)
        self.assertEqual('reserved',p.classifier.calls[1][1][0]['clarification_outcomes'][0]['delivery_status'])
        self.assertEqual(2,p.store.reconcile(p.store.get(message().chat_id,1)['case_key']).received)

    def test_upgrade_old_pending_plan_retains_attempted_question(self):
        sent=[]
        p=self.build(Router([route([1],'CLARIFY',question='Màn hình nào?')],
                            [route([2],'CLARIFY','c1',question='Màn hình nào?')]),
                     sender=lambda c,m,t:sent.append(m) or 901)
        p.process(message(1))
        # Simulate a deployed pending case that predates this additive ledger.
        p.store.connection.execute('DELETE FROM autonomous_clarifications')
        p.store.connection.commit()
        p.process(message(2,reply_to_id=901,received_at=NOW+timedelta(days=1)))
        self.assertEqual([1],sent)
        self.assertEqual(['Màn hình nào?'],p.classifier.calls[1][1][0]['pending_questions'])

    def test_upgrade_old_empty_plan_recovers_actual_canned_question(self):
        import json
        from tg_triage.group_notices import LEGACY_PENDING_QUESTION
        sent=[]
        p=self.build(Router([route([1],'CLARIFY')],
                            [route([2],'CLARIFY','c1',question=LEGACY_PENDING_QUESTION)]),
                     sender=lambda c,m,t:sent.append(m) or 901)
        p.process(message(1))
        p.store.connection.execute('DELETE FROM autonomous_clarifications')
        # Original deployed fallback had empty plan.question and no batch-anchor metadata.
        old=p.store.connection.execute('SELECT plan FROM autonomous_plans').fetchone()[0]
        plan=json.loads(old); plan['question']=''; plan.pop('host_batch_notice_anchor',None)
        p.store.connection.execute('UPDATE autonomous_plans SET plan=?',(json.dumps(plan),))
        p.store.connection.commit()
        p.process(message(2,reply_to_id=901,received_at=NOW+timedelta(minutes=10)))
        self.assertEqual([1],sent)
        self.assertEqual([LEGACY_PENDING_QUESTION],p.classifier.calls[1][1][0]['pending_questions'])

    def test_technical_outage_late_batch_restart_notifies_new_anchor_without_merging(self):
        sent=[]
        with TemporaryDirectory() as directory:
            path=str(Path(directory)/'outage.sqlite3')
            store=Store(path)
            p=self.build(Router(None),store,lambda c,m,t:sent.append(m) or 901)
            p.process(message(1,text='Lỗi đăng nhập')); store.close()
            store=Store(path)
            p=self.build(Router(None),store,lambda c,m,t:sent.append(m) or 902)
            p.process(message(2,text='Ảnh bổ sung',reply_to_id=901,received_at=NOW+timedelta(seconds=90)))
            self.assertEqual([1,2],sent)
            p.close_due_cases()
            self.assertEqual([1,2],sent)
            self.assertNotEqual(store.get(message().chat_id,1)['case_key'],store.get(message().chat_id,2)['case_key'])
            self.assertEqual(2,store.connection.execute('SELECT COUNT(*) FROM autonomous_routes').fetchone()[0])
            self.assertEqual(1,store.connection.execute('SELECT COUNT(*) FROM autonomous_outages').fetchone()[0])
            store.close()

    def test_success_closes_outage_next_failure_can_notify(self):
        sent=[]
        p=self.build(Router(None,[route([2])],None),sender=lambda c,m,t:sent.append(m) or 900+m)
        p.process(message(1)); p.process(message(2,text='Nút lưu bị lỗi')); p.process(message(3))
        self.assertEqual([1,2,3],sent)
        rows=p.store.connection.execute('SELECT status FROM autonomous_outages ORDER BY cycle').fetchall()
        self.assertEqual(['closed','open'],[r[0] for r in rows])

    def test_technical_outage_unknown_transport_does_not_retry_same_anchor(self):
        attempts=[]
        def sender(c,m,t):
            attempts.append(m); raise TimeoutError('unknown delivery')
        p=self.build(Router(None,None),sender=sender)
        p.process(message(1)); p.process(message(2,reply_to_id=1,received_at=NOW+timedelta(minutes=5)))
        self.assertEqual([1,2],attempts)
        p.close_due_cases()
        self.assertEqual([1,2],attempts)
        self.assertEqual(2,p.store.connection.execute('SELECT COUNT(*) FROM autonomous_routes').fetchone()[0])

    def test_two_unvalidated_old_topics_late_photo_does_not_fake_merge(self):
        sent=[]
        p=self.build(Router(None,[route([461],'CLARIFY',question='Anh/Chị muốn team xử lý lỗi gì trong ảnh này?')]),
                     sender=lambda c,m,t:sent.append(m) or 900+m)
        p.store.enqueue(message(458,text='Sao tính tiền'))
        p.process(message(457,text='Login đúng pass mà không login được'))
        historical={i:dict(p.store.get(message().chat_id,i)) for i in (457,458)}
        p.process(message(461,text='Ảnh bổ sung',has_image=True,received_at=NOW+timedelta(minutes=5)))
        keys=[p.store.get(message().chat_id,i)['case_key'] for i in (457,458,461)]
        self.assertEqual(3,len(set(keys)))
        self.assertFalse(p.github.calls)
        self.assertEqual([457,461],sent)
        self.assertEqual([],p.classifier.calls[1][1])
        self.assertEqual(historical,{i:dict(p.store.get(message().chat_id,i)) for i in (457,458)})

    def test_host_rejected_foreign_ids_never_record_ok_failure_reason(self):
        router=Router([route([1],analysis=draft([999]))])
        router.last_ingress_outcome='ok'
        sent=[]
        p=self.build(router,sender=lambda c,m,t:sent.append(m) or 901)
        p.process(message(1))
        self.assertFalse(p.github.calls)
        row=p.store.connection.execute('SELECT reason_code FROM autonomous_routes').fetchone()
        self.assertEqual('invalid_ingress_output',row[0])
        self.assertEqual([1],sent)
