"""Chat-native pacing with virtual time; no real Telegram/model network or private screenshots."""
import asyncio
import base64
import json
import re
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tg_triage.classifier import Classifier
from tg_triage.config import ConfigError,load_config
from tg_triage.models import CaseAnalysis
from tg_triage.pacing import ChatCollector
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tg_triage import telegram_io
from tests.helpers import config,message,classification,FakeHTTP,FakeGitHub,FakeDownloader,FakeUploader,JPG,EXAMPLE,FAKE_HASH


class VirtualClock:
    def __init__(self):
        self.now=0.0; self.timers=[]; self.received=[]
    def __call__(self): return self.now
    def advance(self,value):
        if value<self.now: raise ValueError('Time must move forward')
        self.now=value
        for deadline,future in self.timers:
            if deadline<=value and not future.done(): future.set_result(True)
    async def receive(self,queue,stop,timeout):
        getter=asyncio.create_task(queue.get()); stopper=asyncio.create_task(stop.wait()) if stop else None
        timer=asyncio.get_running_loop().create_future()
        if timeout is not None:
            self.timers.append((self.now+timeout,timer))
            if timeout<=0: timer.set_result(True)
        tasks=[getter,timer]+([stopper] if stopper else [])
        released=False; outcome=telegram_io._TICK
        try:
            try:
                await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
                if stop and stop.is_set():
                    if getter.done() and not getter.cancelled(): queue.task_done();released=True
                    outcome=None
                elif getter.done():
                    outcome=getter.result()
            finally:
                for task in tasks:
                    if not task.done(): task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
        except asyncio.CancelledError:
            if getter.done() and not getter.cancelled() and not released: queue.task_done()
            raise
        if outcome is not None and outcome is not telegram_io._TICK:
            self.received.append((outcome.chat_id,outcome.message_id))
        return outcome



async def spin_until(predicate,limit=2000):
    for _ in range(limit):
        if predicate(): return
        await asyncio.sleep(.001)
    raise AssertionError('Async work did not reach expected checkpoint')


class SchedulerTests(unittest.TestCase):
    def test_trailing_quiet_and_absolute_cap(self):
        collector=ChatCollector()
        for mid,when in ((1,0),(2,20),(3,45)):
            collector.add(message(mid),when)
        self.assertEqual(15,collector.next_timeout(45))
        self.assertEqual([],collector.release_due(59.999))
        self.assertEqual([[1,2,3]],[[m.message_id for m in b] for b in collector.release_due(60)])
    def test_continuous_hundred_messages_still_release_at_sixty(self):
        collector=ChatCollector()
        for mid in range(100): collector.add(message(mid+1),mid*.6)
        self.assertEqual([],collector.release_due(59.9))
        self.assertEqual(100,len(collector.release_due(60)[0]))
    def test_multiple_chats_deadlines_are_independent_and_fair(self):
        collector=ChatCollector()
        collector.add(message(1,chat_id=-1),0)
        collector.add(message(2,chat_id=-2),5)
        collector.add(message(3,chat_id=-1),20)
        self.assertEqual([[-2]],[[m.chat_id for m in b] for b in collector.release_due(35)])
        self.assertEqual([[-1,-1]],[[m.chat_id for m in b] for b in collector.release_due(50)])
    def test_configuration_defaults_and_bounds(self):
        text=EXAMPLE.read_text(encoding='utf-8')
        text=re.sub(r'(?m)^context_(quiet|max_wait)_seconds\s*=.*\n','',text)
        for quiet,maximum,valid in ((30,60,True),(60,60,True),(29,60,False),(30,61,False),(40,30,False),(True,60,False),(30.5,60,False)):
            extra=f'context_quiet_seconds = {str(quiet).lower()}\ncontext_max_wait_seconds = {maximum}\n'
            with self.subTest(quiet=quiet,maximum=maximum),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'config.toml'; path.write_text(text.replace('[thinking]','[thinking]\n'+extra),encoding='utf-8')
                if valid:
                    parsed=load_config(path,{'TG_API_ID':'123','TG_API_HASH':FAKE_HASH})
                    self.assertEqual((quiet,maximum),(parsed.context_quiet_seconds,parsed.context_max_wait_seconds))
                else:
                    with self.assertRaises(ConfigError): load_config(path,{'TG_API_ID':'123','TG_API_HASH':FAKE_HASH})


class WorkerPacingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.store=Store(':memory:'); self.addCleanup(self.store.close)
        self.queue=asyncio.Queue(); self.stop=asyncio.Event(); self.clock=VirtualClock(); self.task=None
        self.addAsyncCleanup(self.shutdown)
    async def shutdown(self):
        if self.task and not self.task.done():
            self.stop.set(); await asyncio.wait_for(self.task,2)
    def build(self,model,**kwargs):
        p=Pipeline(replace(config('live'),autonomous_enabled=True),self.store,model,FakeGitHub(),**kwargs)
        self.task=asyncio.create_task(telegram_io.worker(self.queue,p,self.stop,_clock=self.clock,_receive=self.clock.receive))
        return p
    async def send(self,incoming):
        self.store.enqueue(incoming); await self.queue.put(incoming)
        target=len(self.clock.received)+1
        await spin_until(lambda:len(self.clock.received)>=target)
        for _ in range(20): await asyncio.sleep(0)
    async def advance(self,when):
        self.clock.advance(when)
        for _ in range(20): await asyncio.sleep(0)
    def wire(self,ids,kind='bug'):
        value=dict(vars(classification(type=kind,title='Lỗi đăng nhập' if kind=='bug' else 'Cập nhật báo cáo')),
                   category=kind,actionable=True,verdict='ticket',key_message_ids=ids,missing_info=[],reason='Supplied work',details={})
        return dict(message_ids=ids,action='NEW',candidate_id=None,confidence=.95,question='',analysis=value)
    def model(self,groups):
        http=FakeHTTP({'message':{'content':json.dumps(dict(groups=groups),ensure_ascii=False)}})
        return Classifier(config().ollama,opener=http),http

    async def test_login_text_short_context_late_photo_joint_no_early_call(self):
        model,http=self.model([self.wire([1,2,3])]); notices=[]
        p=self.build(model,downloader=FakeDownloader({3:(JPG,'synthetic-redacted.jpg')}),uploader=FakeUploader(),
                     notice_sender=lambda c,m,t:notices.append((m,t)) or 101)
        await self.send(message(1,text='Đăng nhập POS không thành công',mentions_listener=False))
        await self.advance(10)
        await self.send(message(2,text='Chưa rõ lỗi gì, Em gửi ảnh ngay sau đây',mentions_listener=False))
        await self.advance(25)
        await self.send(message(3,text='Đây là ảnh lỗi đăng nhập đã che thông tin',has_image=True,mentions_listener=False))
        join=asyncio.create_task(self.queue.join())
        await self.advance(54.999)
        self.assertEqual([],http.calls); self.assertEqual([],notices); self.assertFalse(join.done())
        await self.advance(55)
        await spin_until(lambda:len(p.github.calls)==1 and join.done())
        self.assertEqual(1,len(http.calls)); self.assertEqual([1],[m for m,_ in notices])
        body=json.loads(http.calls[0][0].data); user=body['messages'][1]
        packet=json.loads(user['content'])
        self.assertEqual([1,2,3],packet['NEW_SOURCE_IDS']); self.assertEqual(1,len(user['images']))
        self.assertEqual(JPG,base64.b64decode(user['images'][0])); self.assertEqual(3,packet['images_order'][0]['message_id'])
        await self.shutdown()

    async def test_interleaved_topics_stay_in_one_collection_and_split_correctly(self):
        model,http=self.model([self.wire([1,3]),self.wire([2],'task')]); notices=[]
        p=self.build(model,notice_sender=lambda c,m,t:notices.append(m) or 100+m)
        await self.send(message(1,text='Đăng nhập không được',mentions_listener=False))
        await self.advance(5); await self.send(message(2,text='Đổi bảng báo cáo',mentions_listener=False))
        await self.advance(10); await self.send(message(3,text='Bổ sung cho đăng nhập',reply_to_id=1,mentions_listener=False))
        await self.advance(39); self.assertFalse(http.calls)
        await self.advance(40); await spin_until(lambda:len(p.github.calls)==2)
        await asyncio.wait_for(self.queue.join(),2)
        self.assertEqual([1,2],notices); self.assertEqual(1,len(http.calls))
        await self.shutdown()

    async def test_stop_then_replay_waits_again_without_lost_queued_messages(self):
        model,http=self.model([self.wire([1,2])]); p=self.build(model)
        await self.send(message(1)); await self.advance(10); await self.send(message(2))
        await self.shutdown(); await asyncio.wait_for(self.queue.join(),2)
        self.assertFalse(http.calls)
        self.assertEqual([1,2],[m.message_id for m in self.store.queued_messages()])
        self.queue=asyncio.Queue(); self.stop=asyncio.Event(); self.clock=VirtualClock()
        for item in self.store.queued_messages(): self.queue.put_nowait(item)
        self.task=asyncio.create_task(telegram_io.worker(self.queue,p,self.stop,_clock=self.clock,_receive=self.clock.receive))
        await spin_until(lambda:len(self.clock.received)==2)
        for _ in range(20): await asyncio.sleep(0)
        await self.advance(29.99); self.assertFalse(http.calls)
        await self.advance(30); await spin_until(lambda:len(p.github.calls)==1)
        await asyncio.wait_for(self.queue.join(),2); self.assertEqual(1,len(http.calls))

    async def test_cancellation_releases_pending_queue_but_preserves_database(self):
        model,http=self.model([self.wire([1])]); self.build(model)
        await self.send(message(1)); self.task.cancel()
        with self.assertRaises(asyncio.CancelledError): await self.task
        await asyncio.wait_for(self.queue.join(),2)
        self.assertFalse(http.calls); self.assertEqual('queued',self.store.get(message().chat_id,1)['decision'])

    async def test_other_chat_collects_while_processing_and_next_boundary_is_not_stolen(self):
        class BlockingModel:
            def __init__(self): self.calls=[]; self.started=threading.Event(); self.release=threading.Event()
            def analyze_ingress(self,sources,candidates,**options):
                ids=[m.message_id for m in sources]; self.calls.append((sources[0].chat_id,ids))
                if len(self.calls)==1:
                    self.started.set()
                    if not self.release.wait(2): raise AssertionError('Missing thread release')
                return [dict(message_ids=ids,action='NEW',candidate_id=None,confidence=.95,question='',
                             analysis=CaseAnalysis('ticket',classification(),tuple(ids),'bug',True))]
        model=BlockingModel(); self.addCleanup(model.release.set); p=self.build(model)
        await self.send(message(1,chat_id=-1))
        await self.advance(5); await self.send(message(2,chat_id=-2))
        await self.advance(30); await spin_until(model.started.is_set)
        await self.advance(35)
        await self.advance(36); await self.send(message(3,chat_id=-2))
        model.release.set(); await spin_until(lambda:len(model.calls)>=2)
        self.assertEqual([(-1,[1]),(-2,[2])],model.calls)
        self.assertEqual('queued',self.store.get(-2,3)['decision'])
        await self.advance(65.99); self.assertEqual(2,len(model.calls))
        await self.advance(66); await spin_until(lambda:len(model.calls)==3)
        await asyncio.wait_for(self.queue.join(),2)
        self.assertEqual((-2,[3]),model.calls[-1]); self.assertEqual(3,len(p.github.calls))

    async def test_legacy_worker_and_direct_pipeline_remain_immediate(self):
        model,http=self.model([self.wire([1])])
        p=Pipeline(replace(config('live'),autonomous_enabled=True),self.store,model,FakeGitHub())
        self.assertEqual('issue',p.process(message(1)).decision); self.assertEqual(1,len(http.calls))
        calls=[]
        class Legacy:
            config=config()
            def process(self,incoming): calls.append(incoming.message_id)
        self.task=asyncio.create_task(telegram_io.worker(self.queue,Legacy(),self.stop))
        await self.queue.put(message(2)); await spin_until(lambda:calls==[2])
        await asyncio.wait_for(self.queue.join(),2)

    async def test_followup_messages_wait_and_then_use_one_joint_followup_job(self):
        from tests.test_followups import answer
        from tests.helpers import NOW
        initial=self.wire([10]); continuation=self.wire([11,12]); continuation.update(action='CONTINUE',candidate_id='c1')
        http=FakeHTTP({'message':{'content':json.dumps(dict(groups=[initial]),ensure_ascii=False)}},
                      {'message':{'content':json.dumps(dict(groups=[continuation]),ensure_ascii=False)}},
                      {'message':{'content':json.dumps(answer('update',summary='Bổ sung thông tin app và web'),ensure_ascii=False)}})
        notices=[]; p=self.build(Classifier(config().ollama,opener=http),notice_sender=lambda c,m,t:notices.append(m) or 100+m)
        p.github.lifecycle_snapshot=lambda n:dict(number=n,repo=p.config.gh_repo,state='open',observed_at=NOW.isoformat())
        comments=[]; p.github.add_comment=lambda n,b:comments.append(n) or 301
        self.assertEqual('issue',p.process(message(10,text='POS không in được')).decision)
        await self.send(message(11,text='Đây là thông tin bổ sung',reply_to_id=10))
        await self.advance(20); await self.send(message(12,text='Cả app và web, máy in có dây',reply_to_id=10))
        await self.advance(49.99); self.assertEqual(1,len(http.calls)); self.assertEqual([],comments)
        await self.advance(50); await spin_until(lambda:len(comments)==1)
        await asyncio.wait_for(self.queue.join(),2)
        self.assertEqual(3,len(http.calls)); self.assertEqual([42],comments); self.assertEqual([10,11],notices)
        self.assertEqual(1,self.store.connection.execute('SELECT COUNT(*) FROM followup_jobs').fetchone()[0])
        self.assertEqual(2,self.store.connection.execute('SELECT COUNT(*) FROM followup_job_sources').fetchone()[0])

    async def test_hundred_continuous_arrivals_release_at_cap_in_actual_worker(self):
        class Model:
            def __init__(self):self.calls=[]
            def analyze_ingress(self,sources,candidates,**options):
                ids=[m.message_id for m in sources]; self.calls.append(ids)
                return [dict(message_ids=ids,action='NEW',candidate_id=None,confidence=.95,question='',
                    analysis=CaseAnalysis('ticket',classification(),tuple(ids),'bug',True))]
        model=Model(); p=self.build(model)
        for i in range(100):
            await self.advance(i*.6); await self.send(message(i+1))
        self.assertEqual([],model.calls)
        await self.advance(60); await asyncio.wait_for(self.queue.join(),5)
        self.assertEqual(list(range(1,101)),[i for group in model.calls for i in group])
        self.assertEqual(5,len(p.github.calls))

    async def test_stop_during_active_model_finishes_only_released_batch(self):
        class Model:
            def __init__(self):self.calls=[];self.started=threading.Event();self.release=threading.Event()
            def analyze_ingress(self,sources,candidates,**options):
                ids=[m.message_id for m in sources];self.calls.append(ids);self.started.set()
                if not self.release.wait(5):raise AssertionError('Missing thread release')
                return [dict(message_ids=ids,action='NEW',candidate_id=None,confidence=.95,question='',
                    analysis=CaseAnalysis('ticket',classification(),tuple(ids),'bug',True))]
        model=Model();self.addCleanup(model.release.set);p=self.build(model)
        await self.send(message(1));await self.advance(30);await spin_until(model.started.is_set)
        await self.advance(31);await self.send(message(2))
        self.stop.set()
        for _ in range(20):await asyncio.sleep(0)
        self.assertFalse(self.task.done())
        model.release.set();await asyncio.wait_for(self.task,2);await asyncio.wait_for(self.queue.join(),2)
        self.assertEqual([[1]],model.calls);self.assertEqual(1,len(p.github.calls))
        self.assertEqual('queued',self.store.get(message().chat_id,2)['decision'])

    async def test_cancel_queue_receive_handoff_releases_retrieved_item(self):
        taken=asyncio.Event()
        class Queue(asyncio.Queue):
            async def get(self):
                value=await super().get();taken.set();return value
        queue=Queue();queue.put_nowait(message(1))
        task=asyncio.create_task(telegram_io._next_message(queue,None,60))
        await taken.wait();task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        await asyncio.wait_for(queue.join(),2)

    async def test_recovery_progress_is_scoped_while_other_chats_keep_collecting(self):
        class Model:
            def __init__(self):self.calls=[]
            def analyze_ingress(self,sources,candidates,**options):
                ids=[m.message_id for m in sources];self.calls.append((sources[0].chat_id,ids))
                return [dict(message_ids=ids,action='NEW',candidate_id=None,confidence=.95,question='',
                    analysis=CaseAnalysis('ticket',classification(),tuple(ids),'bug',True))]
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup);path=str(Path(folder.name)/'triage.sqlite3')
        old_store=Store(path);old=Pipeline(replace(config('live'),autonomous_enabled=True),old_store,Model(),FakeGitHub())
        old._run=lambda *a:(_ for _ in ()).throw(SystemExit('cached plan interrupted before effect'))
        for chat,mid in ((-99,100),(-2,200)):
            with self.assertRaises(SystemExit):old.process(message(mid,chat_id=chat,text='Old distinct work'))
        old_store.close();self.store=Store(path);self.addCleanup(self.store.close)
        model=Model();notices=[];p=self.build(model,notice_sender=lambda c,m,t:notices.append((c,m)) or 10000+m)
        await self.send(message(1000,chat_id=-2,text='Current unrelated work'))
        for when in range(1,131):
            await self.advance(when)
            if when>=5 and (when-5)%14==0:
                await self.send(message(2000+when,chat_id=-3,text='Third chat stays active'))
            if when%14==0:
                await self.send(message(1000+when,chat_id=-2,text='More current context'))
            if when==15:
                await spin_until(lambda:self.store.get_case('-99:100')['status']=='done')
                self.assertEqual('open',self.store.get_case('-2:200')['status'])
                self.assertEqual([(-99,100)],notices);self.assertEqual([],model.calls)
            if when==30:
                self.assertEqual('open',self.store.get_case('-2:200')['status'])
                self.assertNotIn((-2,200),notices)
        await self.advance(160)
        await asyncio.wait_for(self.queue.join(),5)
        await self.advance(176)
        await spin_until(lambda:self.store.get_case('-2:200')['status']=='done')
        self.assertIn((-99,100),notices);self.assertIn((-2,200),notices)
        await self.shutdown();await asyncio.wait_for(self.queue.join(),2)

    async def test_released_followup_executes_even_when_next_same_chat_batch_is_waiting(self):
        from tests.test_followups import answer
        from tests.helpers import NOW
        class Model:
            def __init__(self):self.calls=[];self.started=threading.Event();self.release=threading.Event();self.followups=[]
            def analyze_ingress(self,sources,candidates,**options):
                ids=[m.message_id for m in sources];self.calls.append((sources[0].chat_id,ids))
                action='NEW';candidate=None
                if sources[0].chat_id==-1:
                    self.started.set()
                    if not self.release.wait(5):raise AssertionError('Missing thread release')
                elif ids!=[100]:
                    action='CONTINUE';candidate=next(c['candidate_id'] for c in candidates if c['source']['source_message_id']==100)
                return [dict(message_ids=ids,action=action,candidate_id=candidate,confidence=.95,question='',
                    analysis=CaseAnalysis('ticket',classification(),tuple(ids),'bug',True))]
            def analyze_followup(self,originals,reply,tickets,history,**options):
                self.followups.append(([m.message_id for m in originals],reply.message_id))
                return answer('update',summary='Bổ sung cho ticket đã có')
        model=Model();self.addCleanup(model.release.set);p=self.build(model,notice_sender=lambda c,m,t:1000+m)
        p.github.lifecycle_snapshot=lambda n:dict(number=n,repo=p.config.gh_repo,state='open',observed_at=NOW.isoformat())
        comments=[];p.github.add_comment=lambda n,b:comments.append(n) or 301
        p.process(message(100,chat_id=-2,text='Existing ticket'))
        await self.send(message(1,chat_id=-1))
        await self.advance(30);await spin_until(model.started.is_set)
        await self.send(message(200,chat_id=-2,reply_to_id=100,text='Released evidence'))
        await self.advance(60)
        await self.advance(60.1);await self.send(message(201,chat_id=-2,reply_to_id=100,text='Next context still collecting'))
        model.release.set();await spin_until(lambda:len(comments)==1)
        self.assertEqual([([100,200],200)],model.followups)
        self.assertEqual('queued',self.store.get(-2,201)['decision'])
        self.assertNotIn((-2,[200,201]),model.calls)
        self.assertEqual(1,self.store.connection.execute('SELECT COUNT(*) FROM followup_jobs').fetchone()[0])
        await self.shutdown();await asyncio.wait_for(self.queue.join(),2)

    async def test_real_receive_cancel_during_cleanup_releases_retrieved_durable_item(self):
        cleanup_started=asyncio.Event(); cleanup_release=asyncio.Event()
        class SlowStop:
            def is_set(self):return False
            async def wait(self):
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleanup_started.set()
                    await cleanup_release.wait()
                    raise
        incoming=message(1);self.store.enqueue(incoming)
        queue=asyncio.Queue();queue.put_nowait(incoming)
        task=asyncio.create_task(telegram_io._next_message(queue,SlowStop(),60))
        await asyncio.wait_for(cleanup_started.wait(),2)
        self.assertFalse(task.done());self.assertTrue(queue.empty())
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        await asyncio.wait_for(queue.join(),2)
        self.assertEqual('queued',self.store.get(incoming.chat_id,incoming.message_id)['decision'])
