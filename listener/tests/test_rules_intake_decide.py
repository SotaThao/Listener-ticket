import json
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tg_triage import admin, classification_log, rules_intake
from tg_triage.__main__ import _load_assistant_pack, _load_decision_rules
from tg_triage.assistant_contract import GroupDecision, PHASE_DECIDE
from tg_triage.assistant_extractor import batch_fingerprint
from tg_triage.decision_rules import RulesSnapshot
from tg_triage.models import Decision
from tg_triage.pipeline import Pipeline
from tg_triage.router_transport import RouterError, ToolCall
from tg_triage.rules_pending_store import PARENT_PULL_CLAIM, RulesHalt, check_before_recovery
from tg_triage.store import Store
from tg_triage.ticket_templates import active
from tests.helpers import PNG, Clock, FakeGitHub, FakeUploader, config, message
from tests.test_assistant_contract import issue
from tests.test_assistant_decide_turn import reply


ROOT = Path(__file__).resolve().parents[1]
OPS = -1009876543210
CHAT = message().chat_id
CHECKS = ('F7', 'F1', 'F3', 'F4', 'F6', 'F5', 'R1', 'R2', 'R3')
PR_REVIEW = 'anh review giúp em PR https://github.com/org/repo/pull/12'
MERGE_ACK = 'a merge rồi nha'


class Audit:
    """Stub the R4 additions while letting legacy runs use the real writer."""

    def __init__(self, store, test):
        self.store = store
        self.legacy_calls = []
        self.set_thread = Mock(side_effect=self._set_thread)
        with store.connection:
            columns = {row[1] for row in store.connection.execute('PRAGMA table_info(classification_runs)')}
            for name, kind in (('code_precheck_json', 'TEXT'), ('log_thread_id', 'INTEGER')):
                if name not in columns:
                    store.connection.execute(f'ALTER TABLE classification_runs ADD COLUMN {name} {kind}')
        self.legacy_run = classification_log.run
        self.legacy_record = classification_log.record_decision
        for name in ('run', 'record_decision', 'find_thread', 'set_thread'):
            patcher = patch.object(classification_log, name, getattr(self, name), create=True)
            patcher.start()
            test.addCleanup(patcher.stop)

    def rows(self):
        return self.store.connection.execute('SELECT * FROM classification_runs ORDER BY run_id').fetchall()

    def write(self, snapshot, sources, topic, phase, *, trace, thread, state, rule):
        with self.store.connection:
            run_id = self.store.connection.execute('''INSERT INTO classification_runs
                (created_at,turn_key,chat_id,message_ids,topic_id,phase,batch_fingerprint,id_space,
                 rules_fingerprint,final_state,decision_rule,code_precheck_json,log_thread_id)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (self.store.clock().timestamp(), 'test:' + topic, sources[0].chat_id,
                 json.dumps([m.message_id for m in sources]), topic, phase, batch_fingerprint(sources),
                 'message', snapshot.fingerprint, state, rule, json.dumps(trace), thread)).lastrowid
            if thread is None:
                self.store.connection.execute('UPDATE classification_runs SET log_thread_id=? WHERE run_id=?', (run_id, run_id))
            return run_id

    @contextmanager
    def run(self, snapshot, sources, images=None, phase='extract', *, topic_id=None,
            code_precheck=None, log_thread_id=None, ref_sources=None):
        self.legacy_calls.append((phase, code_precheck))
        if code_precheck is None:
            with self.legacy_run(snapshot, sources, images, phase, topic_id=topic_id,
                                 ref_sources=ref_sources) as attempt:
                yield attempt
        else:
            attempt = SimpleNamespace(result=None)
            yield attempt
            decision = attempt.result[0][1]
            self.write(snapshot, sources, topic_id, phase, trace=code_precheck, thread=log_thread_id,
                       state=decision.state, rule=decision.rule)

    def record_decision(self, store, snapshot, topic_id, sources, decision, *, phase='early',
                        code_precheck=None, log_thread_id=None):
        if code_precheck is None:
            return self.legacy_record(store, snapshot, topic_id, sources, decision, phase=phase)
        return self.write(snapshot, sources, topic_id, phase, trace=code_precheck, thread=log_thread_id,
                          state=decision.state, rule=decision.rule)

    def find_thread(self, store, chat_id, *, reply_to_ids=(), topic_id=None, ticket_ref=None):
        for row in reversed(self.rows()):
            if row['chat_id'] != chat_id:
                continue
            ticket_url = f'https://github.com/sandbox/triage/issues/{ticket_ref}'
            if (set(json.loads(row['message_ids'])) & set(reply_to_ids)
                    or topic_id is not None and row['topic_id'] == topic_id
                    or ticket_ref is not None and row['ticket_ref'] == ticket_url):
                return row['log_thread_id'] or row['run_id']
        return None

    def _set_thread(self, store, run_id, thread_id):
        with store.connection:
            store.connection.execute('''UPDATE classification_runs SET log_thread_id=? WHERE run_id=?
                AND (log_thread_id IS NULL OR log_thread_id=run_id)''', (thread_id, run_id))


class LegacyProvider:
    settings = SimpleNamespace(model='test')
    think = False
    supports_vision = False

    def __init__(self):
        self.calls = []

    def _ask(self, body, parse, attempts):
        self.calls.append(body)
        ids = json.loads(body['messages'][1]['content'])['source_ids']
        return parse(json.dumps({'groups': [{'message_ids': ids, 'facts': {
            name: {'value': value, 'source_ids': [ids[-1]]}
            for name, value in (('intent', 'bug'), ('object', 'POS'), ('observed', 'kẹt giấy'))
        }}]}))


class DecideIntakeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = RulesSnapshot(ROOT / 'rules', ROOT / 'templates' / 'github-ticket')

    def pipeline(self, *, authority='decide', canary=(), outcome='done', candidate=None):
        base = config('live')
        settings = replace(base, autonomous_enabled=True, decision_rules_enabled=True,
                           assistant=replace(base.assistant, mode='live', authority=authority, decide_chat_ids=canary),
                           telegram=replace(base.telegram, ops_chat_id=OPS))
        store = Store(':memory:', clock=lambda: message().received_at)
        self.addCleanup(store.close)
        github = FakeGitHub()
        github.lifecycle_snapshot = Mock(side_effect=lambda n: dict(
            number=n, repo=settings.gh_repo, state='open', observed_at='2026-11-19'))
        github.add_comment = Mock(return_value=101)
        sent = []
        pipeline = Pipeline(settings, store, LegacyProvider(), github, rules_snapshot=self.snapshot,
                            notice_sender=lambda *args: sent.append(args) or 100 + len(sent),
                            reactor=Mock(return_value=True))
        audit = Audit(store, self)
        jobs = []

        def decide(job):
            jobs.append(job)
            response = (candidate(job) if candidate else self.group(job)) if outcome == 'done' else None
            groups = response if isinstance(response, list) else [response] if response is not None else None
            run_id = audit.write(job['snapshot'], job['sources'], job['topic_id'], PHASE_DECIDE,
                                 trace=job['code_precheck'], thread=job['log_thread_id'],
                                 state=groups[0].state if groups else 'HOLD',
                                 rule=groups[0].rule if groups else 'guard_grounding' if outcome == 'guard' else None)
            return groups, run_id, outcome

        runtime = SimpleNamespace(decide=decide, publish=lambda: None, settings=settings.assistant,
                                  waiting=lambda sources, *, admit, fingerprint=None: ('NEW', True))
        pipeline.assistant_runtime = runtime
        return pipeline, audit, jobs, sent

    @staticmethod
    def group(job, state='CHAT', rule='R7', **kwargs):
        defaults = dict(message_ids=tuple(m.message_id for m in job['sources']), chat_id=job['chat_id'],
                        state=state, rule=rule, template=None, issue=None, target=None, relation=None,
                        question=None, refs=(), confidence=0.9, reasoning_summary='Dựa trên nội dung nhóm',
                        rule_text=job['guard_ctx'].rule_text(rule))
        return GroupDecision(**(defaults | kwargs))

    def ready(self, job, **kwargs):
        mid = job['sources'][0].message_id
        return self.group(job, 'READY', 'R9', template='bug', issue={
            'title': 'POS kẹt giấy',
            'fields': {'object': {'value': 'POS', 'source_ids': [mid]},
                       'observed': {'value': 'kẹt giấy', 'source_ids': [mid]}},
        }, **kwargs)

    @staticmethod
    def seed_parent(pipeline, *, message_id=7133, reason='F2', open_topic=False, number=None,
                    text='Đổi icon menu @devmember', **overrides):
        parent = message(message_id, text, **(
            dict(sender_id=1, mentions_listener=False) | overrides))
        pipeline.store.enqueue(parent)
        pipeline.store.finish(parent, Decision('chat', 'rule', reason=reason, issue_number=number))
        topic = f'{CHAT}:{message_id}'
        pipeline.rules_pending.create(topic, CHAT, message_id)
        decision = replace(pipeline.rules_snapshot.prefilter_decision(), rule=reason)
        pipeline.rules_pending.record_decision(topic, decision)
        if not open_topic:
            pipeline.rules_pending.close(topic, 'CLOSED')
        return parent

    def parent_reply(self, message_id=7134, **overrides):
        return message(message_id, 'Cần sửa lỗi icon trên trang khách', **(
            dict(reply_to_id=7133, sender_id=2, mentions_listener=False) | overrides))

    def seed_recent_assignment(self, pipeline, *, age=timedelta(minutes=30)):
        return self.seed_parent(pipeline, message_id=7132, text='@leokhang đổi icon menu sang svg nha',
                                received_at=message().received_at - age)

    def test_propose_reply_shapes_match_in_process_no_pull_control(self):
        # This is the same worktree with the selector stubbed out, not a HEAD checkout.
        for extensions in (False, True):
            snapshot = RulesSnapshot(ROOT / 'rules', ROOT / 'templates' / 'github-ticket', extensions=extensions)
            for reply_id, sender, text, expected, batch in (
                (7132, 2, 'dạ em sửa liền anh', 'R7', False),
                (7132, 2, 'ok anh để em fix', 'R7', False),
                (7133, 2, 'ok anh', 'R7', False),
                (7133, 2, 'dạ em nhận nha anh', 'R7', False),
                (7132, 2, 'lát họp xong nói qua v3 này nha anh Mến', 'F6', False),
                (7132, 1, '@leokhang đổi luôn icon footer sang svg nha', 'F2', False),
                (7132, 2, '@quanpham95 icon menu bị lỗi font, em sửa giúp anh nha', 'F2', False),
                (7132, 2, 'dạ em sửa liền anh', 'R7', True),
                (7132, 2, 'ok anh để em fix', 'R7', True),
                (7133, 2, 'ok anh', 'R7', True),
            ):
                with self.subTest(extensions=extensions, reply=reply_id, text=text, batch=batch):
                    def run(control):
                        pipeline, _, jobs, sent = self.pipeline(authority='propose')
                        pipeline.rules_snapshot = snapshot
                        pipeline.timer = lambda: 0
                        store = pipeline.store
                        self.seed_parent(pipeline, message_id=7132, open_topic=True,
                                         text='@vomaingoc chỉnh lại icon menu sang svg nha',
                                         received_at=message().received_at - timedelta(minutes=30))
                        image = message(7133, '', has_image=True, sender_id=1, mentions_listener=False,
                                        received_at=message().received_at - timedelta(minutes=29))
                        store.enqueue(image)
                        store.finish(image, Decision('chat', 'rule', reason='F2'))
                        topic = f'{CHAT}:7132'
                        pipeline.rules_pending.add_source(topic, CHAT, 7133)
                        pipeline.rules_pending.close(topic, 'CLOSED')
                        before = [dict(store.get(CHAT, mid)) for mid in (7132, 7133)]
                        requests = []
                        source_ids = []

                        def extract(body, parse, attempts):
                            requests.append(body)
                            packet = json.loads(body['messages'][1]['content'])
                            self.assertNotIn('parent_pull', packet)
                            ids = packet['source_ids']
                            source_ids.append(ids)
                            # Like the review probe, task exists only if "icon" is in the model's sources.
                            match = next((row['message_id'] for row in packet['sources'] if 'icon' in row['text']), None)
                            facts = {'intent': dict(value='chat', source_ids=ids[-1:])}
                            if match is not None:
                                facts = {name: dict(value=value, source_ids=[match]) for name, value in
                                         (('intent', 'task'), ('object', 'icon'), ('work', 'sửa icon sang svg'))}
                            return parse(json.dumps({'groups': [{'message_ids': ids, 'facts': facts}]}))

                        pipeline.classifier._ask = extract
                        incoming = message(7134, text, reply_to_id=reply_id, sender_id=sender, mentions_listener=False)
                        if batch:
                            meeting = message(7135, 'chiều nay ai rảnh họp nhanh 15 phút', sender_id=3, mentions_listener=False)
                            for source in (incoming, meeting):
                                store.enqueue(source)
                        with patch.object(rules_intake, '_pull_parents', return_value=([], {}) if control else None,
                                          create=True) as select:
                            result = rules_intake._process_batch(pipeline, [incoming, meeting]) if batch else pipeline.process(incoming)
                        select.assert_not_called()
                        self.assertEqual(('chat', expected, None), (result.decision, result.reason, result.issue_number))
                        self.assertEqual([] if expected == 'F6' else [[7134, 7135] if batch else [7134]], source_ids)
                        self.assertEqual(([], [], []), (jobs, sent, pipeline.github.calls))
                        self.assertEqual(before, [dict(store.get(CHAT, mid)) for mid in (7132, 7133)])
                        tables = ('messages', 'rules_topics', 'rules_sources', 'rules_decisions',
                                  'rules_batches', 'rules_batch_groups', 'classification_runs', 'classification_run_sources')
                        ledgers = {name: [dict(row) for row in store.connection.execute(f'SELECT * FROM {name} ORDER BY rowid')]
                                   for name in tables}
                        record = json.dumps((requests, asdict(result), ledgers), ensure_ascii=False, separators=(',', ':')).encode('utf-8')
                        self.assertNotIn(b'parent_pull', record)
                        return record

                    self.assertEqual(run(control=True), run(control=False))

    def test_parent_pull_decide_applies_assistants_chat_f2_for_internal_assignments(self):
        for sender, text in ((1, '@leokhang đổi luôn icon footer sang svg nha'),
                             (2, '@quanpham95 icon menu bị lỗi font, em sửa giúp anh nha')):
            with self.subTest(sender=sender):
                pipeline, _, jobs, sent = self.pipeline(candidate=lambda job: self.group(job, 'CHAT', 'F2'))
                self.seed_recent_assignment(pipeline)
                before = dict(pipeline.store.get(CHAT, 7132))
                result = pipeline.process(message(7150, text, sender_id=sender, mentions_listener=False, reply_to_id=7132))
                self.assertEqual(([[7132, 7150]], 'chat', 'F2'),
                                 ([job['data']['source_ids'] for job in jobs], result.decision, result.reason))
                self.assertEqual(([], []), (pipeline.github.calls, sent))
                self.assertEqual(before, dict(pipeline.store.get(CHAT, 7132)))

    def test_propose_restarts_saved_decide_pull_with_original_sources(self):
        pipeline, audit, jobs, _ = self.pipeline(candidate=self.ready)
        self.seed_parent(pipeline)
        original_parent = dict(pipeline.store.get(CHAT, 7133))
        source = self.parent_reply()
        save = pipeline.rules_pending.save_batch

        def interrupt(*args):
            self.assertTrue(save(*args))
            raise SystemExit

        with patch.object(pipeline.rules_pending, 'save_batch', side_effect=interrupt):
            with self.assertRaises(SystemExit):
                pipeline.process(source)
        batch = f'batch:{CHAT}:7134'
        self.assertEqual([[CHAT, 7133]], pipeline.rules_pending.batch(batch)[1][0][1]['pulled_keys'])
        self.assertEqual(1, len(jobs))
        pipeline.config = replace(pipeline.config, assistant=replace(pipeline.config.assistant, authority='propose'))
        with patch.object(rules_intake, '_pull_parents') as select:
            result = rules_intake._process_batch(pipeline, [source], batch)
        select.assert_not_called()
        self.assertEqual(('issue', 'R9', 42), (result.decision, result.reason, result.issue_number))
        packet, = [json.loads(body['messages'][1]['content']) for body in pipeline.classifier.calls]
        self.assertEqual([7134], packet['source_ids'])
        self.assertNotIn('parent_pull', packet)
        self.assertEqual(['SUPERSEDED', 'READY'], [row['final_state'] for row in audit.rows()])
        self.assertEqual(original_parent, dict(pipeline.store.get(CHAT, 7133)))
        self.assertEqual([], pipeline.rules_pending.hold_topics())
        self.assertEqual([], rules_intake.sweep(pipeline))
        self.assertEqual(1, len(pipeline.github.calls))

    def test_parent_pull_trigger_content_keeps_pr_review_merge_and_bare_link_local(self):
        snapshot = _load_decision_rules()
        link = 'https://github.com/org/repo/pull/13'
        cases = (f'review giúp anh pr {link}', f'@Maiphuong review giúp anh pr {link}', 'merge rồi nha', link)
        for authority in ('decide', 'propose'):
            for text in cases:
                with self.subTest(authority=authority, text=text):
                    pipeline, _, jobs, sent = self.pipeline(authority=authority, candidate=self.ready)
                    pipeline.rules_snapshot = snapshot
                    self.seed_recent_assignment(pipeline)
                    before = dict(pipeline.store.get(CHAT, 7132))
                    reply = message(7150, text, sender_id=2, mentions_listener=False, reply_to_id=7132)
                    for has_image in (False, True):
                        self.assertEqual(([], {}), rules_intake._pull_parents(pipeline, [replace(reply, has_image=has_image)]))
                    result = pipeline.process(reply)
                    self.assertEqual(('chat', 'F6', None), (result.decision, result.reason, result.issue_number))
                    self.assertEqual(([], [], [], []), (jobs, pipeline.classifier.calls, pipeline.github.calls, sent))
                    self.assertEqual(before, dict(pipeline.store.get(CHAT, 7132)))

    def test_mixed_github_links_never_trigger_parent_pull(self):
        for path in ('pull/13', 'issues/8'):
            with self.subTest(path=path):
                pipeline, _, jobs, _ = self.pipeline()
                self.seed_recent_assignment(pipeline)
                before = dict(pipeline.store.get(CHAT, 7132))
                source = message(7150, f'xong rồi, lỗi icon đã sửa, review giúp anh https://github.com/org/repo/{path}',
                                 sender_id=2, reply_to_id=7132, mentions_listener=False)
                self.assertEqual(([], {}), rules_intake._pull_parents(pipeline, [source]))
                self.assertEqual('chat', pipeline.process(source).decision)
                self.assertEqual([[7150]], [job['data']['source_ids'] for job in jobs])
                self.assertNotIn('parent_pull', jobs[0]['data'])
                self.assertEqual(before, dict(pipeline.store.get(CHAT, 7132)))

    def test_assignee_acknowledgements_follow_assistants_chat_f2(self):
        for text in ('dạ em sửa liền anh', 'ok anh', 'dạ em nhận nha anh'):
            with self.subTest(text=text):
                pipeline, _, jobs, sent = self.pipeline(candidate=lambda job: self.group(job, 'CHAT', 'F2'))
                self.seed_parent(pipeline, message_id=7132, text='@vomaingoc chỉnh lại icon menu sang svg nha', open_topic=True)
                picture = message(7133, '', has_image=True, sender_id=1, mentions_listener=False)
                pipeline.store.enqueue(picture)
                pipeline.store.finish(picture, Decision('chat', 'rule', reason='F2'))
                pipeline.rules_pending.add_source(f'{CHAT}:7132', CHAT, 7133)
                pipeline.rules_pending.close(f'{CHAT}:7132', 'CLOSED')
                before = [dict(pipeline.store.get(CHAT, mid)) for mid in (7132, 7133)]
                decision = pipeline.process(message(7134, text, sender_id=2, mentions_listener=False, reply_to_id=7133))
                self.assertEqual(('chat', 'F2', None), (decision.decision, decision.reason, decision.issue_number))
                self.assertEqual([{7134: [7132, 7133]}], [job['data']['parent_pull'] for job in jobs])
                self.assertEqual(([], []), (pipeline.github.calls, sent))
                self.assertEqual(before, [dict(pipeline.store.get(CHAT, mid)) for mid in (7132, 7133)])

    def test_parent_pull_f6_falls_back_before_admission_hold_or_escalation(self):
        from tg_triage.rules_pending_store import check_connection_before_recovery

        for status in ('WAIT_INFRA', 'ESCALATED'):
            with self.subTest(status=status):
                pipeline, audit, jobs, sent = self.pipeline(candidate=self.ready)
                pipeline.assistant_runtime.waiting = Mock(return_value=(status, False))
                self.seed_recent_assignment(pipeline)
                before = dict(pipeline.store.get(CHAT, 7132))
                ack = message(7150, 'ok anh', sender_id=2, mentions_listener=False, reply_to_id=7132)
                result = pipeline.process(ack)
                self.assertEqual(('chat', 'F6'), (result.decision, result.reason))
                pipeline.assistant_runtime.waiting.assert_called_once()
                self.assertEqual([7132, 7150], [m.message_id for m in pipeline.assistant_runtime.waiting.call_args.args[0]])
                self.assertEqual(('chat', 'F6'), tuple(pipeline.store.get(CHAT, 7150)[k] for k in ('decision', 'reason')))
                self.assertEqual(([], [], [], []), (jobs, sent, pipeline.github.calls, pipeline.rules_pending.hold_topics()))
                for _ in range(3):
                    self.assertEqual([], rules_intake.sweep(pipeline))
                self.assertEqual(before, dict(pipeline.store.get(CHAT, 7132)))
                row, = audit.rows()
                self.assertEqual(('CHAT', 'F6'), (row['final_state'], row['decision_rule']))
                check_connection_before_recovery(pipeline.store.connection)

    def test_parent_pull_keeps_history_on_chat_need_info_and_append(self):
        for state, rule in (('CHAT', 'R7'), ('NEED_INFO', 'R12'), ('APPEND', 'R6')):
            with self.subTest(state=state):
                target = dict(number=42, repo='sandbox/triage', state='open', basis='search')
                extra = dict(target=dict(number=42, basis='search'), targets=(target,)) if state == 'APPEND' else {}
                pipeline, _, jobs, _ = self.pipeline(candidate=lambda job: self.group(job, state, rule, **extra))
                parent = self.seed_parent(pipeline)
                before = dict(pipeline.store.get(CHAT, parent.message_id))
                if state == 'APPEND':
                    # Keep target verification local while examining the scope handed to the comment executor.
                    def append(pipeline, topic, members, *args, **kwargs):
                        self.assertEqual([7134], [m.message_id for m in members])
                        return Decision('followup', 'rule', issue_number=42)
                    with patch.object(rules_intake, '_append_batch_group', side_effect=append) as mocked:
                        self.assertEqual('followup', pipeline.process(self.parent_reply()).decision)
                        mocked.assert_called_once()
                else:
                    result = pipeline.process(self.parent_reply())
                    self.assertEqual('chat' if state == 'CHAT' else 'pending_review', result.decision)
                self.assertEqual({7134: [7133]}, jobs[0]['data']['parent_pull'])
                self.assertEqual([7133, 7134], jobs[0]['data']['source_ids'])
                self.assertEqual([], pipeline.github.calls)
                self.assertEqual(before, dict(pipeline.store.get(CHAT, parent.message_id)))
                self.assertEqual({1: (CHAT, 7134)}, pipeline.rules_pending.sources(f'{CHAT}:7134'))

    def test_parent_pull_skips_ineligible_parents_and_replies(self):
        at = message().received_at
        for label, options, reply_options in (
                ('F1', dict(reason='F1'), {}),
                ('forwarded parent', dict(is_forward=True), {}),
                ('older than 24 hours', dict(received_at=at - timedelta(hours=24, seconds=1)), {}),
                ('has issue', dict(number=9), {}),
                ('open topic', dict(open_topic=True), {}),
                ('forwarded reply', {}, dict(is_forward=True))):
            with self.subTest(label=label):
                pipeline, _, jobs, _ = self.pipeline()
                parent = self.seed_parent(pipeline, **options)
                before = dict(pipeline.store.get(CHAT, parent.message_id))
                source = self.parent_reply(**reply_options)
                self.assertEqual(([], {}), rules_intake._pull_parents(pipeline, [source]))
                if label != 'open topic':
                    self.assertEqual('chat', pipeline.process(source).decision)
                    self.assertEqual([7134], jobs[0]['data']['source_ids'])
                    self.assertNotIn('parent_pull', jobs[0]['data'])
                self.assertEqual(before, dict(pipeline.store.get(CHAT, parent.message_id)))

    def test_parent_pull_respects_local_filters_and_max_batch_size(self):
        pipeline, _, jobs, _ = self.pipeline()
        self.seed_parent(pipeline)
        unfiltered = self.parent_reply(7135)
        mixed = message(7136, PR_REVIEW)
        for source in (unfiltered, mixed):
            pipeline.store.enqueue(source)
        self.assertEqual('chat', rules_intake._process_batch(pipeline, [unfiltered, mixed]).decision)
        self.assertEqual([7135, 7136], jobs[0]['data']['source_ids'])
        aggregation = replace(pipeline.config.aggregation, max_messages_per_window=1)
        pipeline.config = replace(pipeline.config, aggregation=aggregation)
        self.assertEqual('chat', pipeline.process(self.parent_reply(7137)).decision)
        self.assertEqual([7137], jobs[-1]['data']['source_ids'])

    def test_parent_pull_precedes_f6_only_if_ready(self):
        source = self.parent_reply()
        source = replace(source, text='Cần dùng cùng icon cho menu')
        for requested in ('READY', 'CHAT', 'NEED_INFO'):
            with self.subTest(requested=requested):
                candidate = self.ready if requested == 'READY' else lambda job: self.group(job, requested, 'R7')
                pipeline, _, jobs, sent = self.pipeline(candidate=candidate)
                before = self.seed_parent(pipeline)
                old = dict(pipeline.store.get(CHAT, before.message_id))
                result = pipeline.process(source)
                self.assertEqual({7134: [7133]}, jobs[0]['data']['parent_pull'])
                self.assertEqual(1, len(jobs))
                self.assertFalse(jobs[0]['code_precheck']['handled_by_code'])
                if requested == 'READY':
                    self.assertEqual((42, 1), (result.issue_number, len(pipeline.github.calls)))
                else:
                    self.assertEqual(('chat', 'F6'), (result.decision, result.reason))
                    self.assertEqual([], sent)
                    self.assertEqual(old, dict(pipeline.store.get(CHAT, 7133)))

    def test_parent_pull_checks_each_f2_candidate_against_local_content_rules(self):
        pipeline, _, jobs, _ = self.pipeline()
        self.seed_parent(pipeline, open_topic=True)
        topic = f'{CHAT}:7133'
        for mid, text in ((7128, 'review giúp em PR https://github.com/org/repo/pull/12'),
                          (7129, 'https://example.com'), (7130, 'ok nha'),
                          (7131, 'merge giúp em'), (7132, 'Sửa sidebar @devmember')):
            source = message(mid, text, sender_id=1, mentions_listener=False,
                             reply_to_id=7120 if mid in (7129, 7130) else None,
                             has_image=mid in (7128, 7130, 7131))
            pipeline.store.enqueue(source)
            pipeline.store.finish(source, Decision('chat', 'rule', reason='F2'))
            pipeline.rules_pending.add_source(topic, CHAT, mid)
        pipeline.rules_pending.close(topic, 'CLOSED')
        self.assertEqual('chat', pipeline.process(self.parent_reply()).decision)
        self.assertEqual([7132, 7133, 7134], jobs[0]['data']['source_ids'])
        self.assertEqual({7134: [7132, 7133]}, jobs[0]['data']['parent_pull'])

    def test_filtered_pr_review_reply_parent_never_pulls_itself_or_siblings(self):
        for with_sibling in (False, True):
            with self.subTest(with_sibling=with_sibling):
                pipeline, _, jobs, _ = self.pipeline(candidate=self.ready)
                parent = message(7131, '@PmqQuan review giúp em pr https://github.com/org/repo/pull/12',
                                 sender_id=1, mentions_listener=False, reply_to_id=7100)
                topic = f'{CHAT}:7131'
                pipeline.rules_pending.create(topic, CHAT, parent.message_id)
                sources = [parent]
                if with_sibling:
                    sources.append(message(7132, '@leo đổi icon menu sang svg nha', sender_id=1,
                                           mentions_listener=False))
                for source in sources:
                    pipeline.store.enqueue(source)
                    pipeline.store.finish(source, Decision('chat', 'rule', reason='F2'))
                    pipeline.rules_pending.add_source(topic, CHAT, source.message_id)
                pipeline.rules_pending.record_decision(topic, replace(pipeline.rules_snapshot.prefilter_decision(), rule='F2'))
                pipeline.rules_pending.close(topic, 'CLOSED')
                before = [dict(pipeline.store.get(CHAT, m.message_id)) for m in sources]
                child = message(7140, 'ok để anh xem', sender_id=2, mentions_listener=False, reply_to_id=7131)
                self.assertEqual(([], {}), rules_intake._pull_parents(pipeline, [child]))
                result = pipeline.process(child)
                self.assertEqual(('chat', 'F6'), (result.decision, result.reason))
                self.assertEqual(([], []), (jobs, pipeline.github.calls))
                self.assertEqual(before, [dict(pipeline.store.get(CHAT, m.message_id)) for m in sources])

    def test_parent_pull_uses_same_sender_and_one_closed_topic(self):
        pipeline, _, _, _ = self.pipeline()
        at = message().received_at
        self.seed_parent(pipeline, open_topic=True, received_at=at - timedelta(hours=24))
        topic = f'{CHAT}:7133'
        for mid, sender in ((7131, 2), (7132, 1)):
            sibling = message(mid, 'Chi tiết menu', sender_id=sender, received_at=at - timedelta(hours=24))
            pipeline.store.enqueue(sibling)
            pipeline.store.finish(sibling, Decision('chat', 'rule', reason='F2'))
            pipeline.rules_pending.add_source(topic, CHAT, mid)
        pipeline.rules_pending.close(topic, 'CLOSED')
        source = self.parent_reply()
        pulled, links = rules_intake._pull_parents(pipeline, [source])
        self.assertEqual({7132, 7133}, {m.message_id for m in pulled})
        self.assertEqual({7134: [7132, 7133]}, links)

        second = f'{CHAT}:other-f2'
        pipeline.rules_pending.create(second, CHAT, 7133)
        pipeline.rules_pending.record_decision(second, replace(pipeline.rules_snapshot.prefilter_decision(), rule='F2'))
        pipeline.rules_pending.close(second, 'CLOSED')
        pulled, links = rules_intake._pull_parents(pipeline, [source])
        self.assertEqual([7133], [m.message_id for m in pulled])
        self.assertEqual({7134: [7133]}, links)

    def test_parent_pull_leaves_split_history_unbound(self):
        def split(job):
            if 'parent_pull' not in job['data']:
                return self.ready(job)
            return [self.group(job, message_ids=(7133,), rule='F2'),
                    self.group(job, 'READY', 'R9', message_ids=(7134,), template='bug', issue={
                        'title': 'Lỗi icon',
                        'fields': {'object': {'value': 'trang khách', 'source_ids': [7134]},
                                   'observed': {'value': 'lỗi icon', 'source_ids': [7134]}}})]
        pipeline, audit, jobs, _ = self.pipeline(candidate=split)
        parent = self.seed_parent(pipeline)
        before = dict(pipeline.store.get(CHAT, parent.message_id))
        self.assertEqual(42, pipeline.process(self.parent_reply()).issue_number)
        _, plans = pipeline.rules_pending.batch(f'batch:{CHAT}:7134')
        self.assertEqual([('DONE', [[CHAT, 7134]])],
                         [(status, plan['source_keys']) for _, plan, status in plans])
        self.assertEqual([[7133, 7134], [7134]], [job['data']['source_ids'] for job in jobs])
        self.assertEqual(before, dict(pipeline.store.get(CHAT, 7133)))
        self.assertNotIn(parent.text, pipeline.github.calls[0].body)
        first, final = audit.rows()
        self.assertEqual(('SUPERSEDED', 'rules_parent_pull_superseded', None),
                         (first['final_state'], first['error'], first['ticket_ref']))
        self.assertEqual(('READY', first['log_thread_id']),
                         (final['final_state'], final['log_thread_id']))

    def test_original_only_retry_stays_disabled_after_admission_hold_and_sweep(self):
        def split(job):
            return [self.ready(job, message_ids=(7133,)),
                    self.group(job, message_ids=(7134,), rule='R7')]

        pipeline, audit, _, _ = self.pipeline(candidate=split)
        self.seed_parent(pipeline)
        runtime = pipeline.assistant_runtime
        real_decide = runtime.decide
        requests = []

        def decide(job):
            ids = job['data']['source_ids']
            requests.append(ids)
            if ids == [7133, 7134]:
                return real_decide(job)
            return None, None, 'model_error'

        runtime.decide = Mock(side_effect=decide)
        runtime.waiting = Mock(side_effect=[('NEW', True), ('NEW', False)], return_value=('NEW', True))
        self.assertEqual('rules_hold', pipeline.process(self.parent_reply()).reason)
        self.assertEqual([[7133, 7134]], requests)
        self.assertEqual('SUPERSEDED', audit.rows()[0]['final_state'])
        self.assertEqual('READY', json.loads(audit.rows()[0]['outcomes_json'])[0]['superseded_state'])
        runtime.waiting.side_effect = None
        last = None
        for _ in range(pipeline.rules_snapshot.limits['hold_max_retries'] + 1):
            result, = rules_intake.sweep(pipeline)
            last = result.reason
            if last == 'rules_hold_escalated':
                break
        self.assertEqual('rules_hold_escalated', last)
        self.assertTrue(all(ids == [7134] for ids in requests[1:]))
        self.assertGreater(len(requests), 1)
        self.assertEqual([], pipeline.github.calls)
        self.assertEqual(('chat', 'F2'), tuple(pipeline.store.get(CHAT, 7133)[key] for key in ('decision', 'reason')))

    def test_group_only_retry_stays_disabled_when_swept_after_hold(self):
        pipeline, audit, jobs, _ = self.pipeline(candidate=self.ready)
        self.seed_parent(pipeline)
        original = self.parent_reply()
        runtime = pipeline.assistant_runtime
        admitted = [True, False]
        runtime.waiting = lambda sources, **_: ('NEW', admitted.pop(0) if admitted else True)
        with patch.object(rules_intake, '_reserve_pulled_group', return_value=False):
            self.assertEqual('rules_hold', pipeline.process(original).reason)
        self.assertEqual([[7133, 7134]], [job['data']['source_ids'] for job in jobs])
        self.assertEqual('SUPERSEDED', audit.rows()[0]['final_state'])
        child = f'batch:{CHAT}:7134:no-parent-pull:0'
        self.assertEqual([child], pipeline.rules_pending.hold_topics())
        self.assertEqual(42, rules_intake.sweep(pipeline)[0].issue_number)
        self.assertEqual([[7133, 7134], [7134]], [job['data']['source_ids'] for job in jobs])
        self.assertEqual(('chat', 'F2'), tuple(pipeline.store.get(CHAT, 7133)[key] for key in ('decision', 'reason')))

    def test_authority_switch_delegates_existing_child_retry_only_once(self):
        pipeline, _, jobs, _ = self.pipeline(candidate=self.ready)
        self.seed_parent(pipeline)
        parent = dict(pipeline.store.get(CHAT, 7133))
        original = self.parent_reply()
        runtime = pipeline.assistant_runtime
        admission = [True, False]
        runtime.waiting = lambda sources, **_: ('NEW', admission.pop(0) if admission else True)
        with (patch.object(rules_intake, '_reserve_pulled_group', return_value=False),
              patch.object(rules_intake, '_complete_group_without_effect', side_effect=SystemExit)):
            with self.assertRaises(SystemExit):
                pipeline.process(original)
        batch = f'batch:{CHAT}:7134'
        child = f'{batch}:no-parent-pull:0'
        self.assertEqual('PLANNED', pipeline.rules_pending.batch(batch)[1][0][2])
        self.assertIn(child, pipeline.rules_pending.hold_topics())
        # Startup clears the reserved attempt on the parent just as in the real-store cases.
        pipeline.rules_pending.finish_attempt(batch, 1)
        pipeline.config = replace(pipeline.config, assistant=replace(pipeline.config.assistant, authority='propose'))
        self.assertEqual([42], [result.issue_number for result in rules_intake.sweep(pipeline)
                               if result.issue_number is not None])
        self.assertEqual([], rules_intake.sweep(pipeline))
        self.assertEqual([[7134]], [json.loads(body['messages'][1]['content'])['source_ids']
                                   for body in pipeline.classifier.calls])
        self.assertEqual(1, len(jobs))
        self.assertEqual(1, len(pipeline.github.calls))
        self.assertEqual(parent, dict(pipeline.store.get(CHAT, 7133)))
        self.assertEqual([], pipeline.rules_pending.hold_topics())

    def test_authority_switch_does_not_repeat_a_completed_ordinal(self):
        def candidate(job):
            independent = self.ready(job, message_ids=(7135,))
            fields = {key: dict(value, source_ids=[7135]) for key, value in independent.issue['fields'].items()}
            return [replace(independent, issue=dict(independent.issue, fields=fields)),
                    self.ready(job, message_ids=(7133, 7134))]

        pipeline, _, jobs, _ = self.pipeline(candidate=candidate)
        self.seed_parent(pipeline)
        old = dict(pipeline.store.get(CHAT, 7133))
        sources = [self.parent_reply(), message(7135, 'POS kẹt giấy')]
        create = pipeline.github.create_issue
        def create_distinct(issue):
            create(issue)
            return 41 + len(pipeline.github.calls), None
        pipeline.github.create_issue = create_distinct
        for source in sources:
            pipeline.store.enqueue(source)
        with patch.object(rules_intake, '_reserve_pulled_group', side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                rules_intake._process_batch(pipeline, sources)
        batch = f'batch:{CHAT}:7134'
        self.assertEqual(['DONE', 'PLANNED'], [status for _, _, status in pipeline.rules_pending.batch(batch)[1]])
        done = dict(pipeline.store.get(CHAT, 7135))
        pipeline.rules_pending.finish_attempt(batch, 1)
        pipeline.config = replace(pipeline.config, assistant=replace(pipeline.config.assistant, authority='propose'))
        self.assertEqual(43, rules_intake.sweep(pipeline)[0].issue_number)
        self.assertEqual([], rules_intake.sweep(pipeline))
        self.assertEqual([[7134]], [json.loads(body['messages'][1]['content'])['source_ids']
                                   for body in pipeline.classifier.calls])
        self.assertEqual(1, len(jobs))
        self.assertEqual(2, len(pipeline.github.calls))
        self.assertEqual(old, dict(pipeline.store.get(CHAT, 7133)))
        self.assertEqual(done, dict(pipeline.store.get(CHAT, 7135)))

    def test_blocked_sibling_of_pulled_checkpoint_uses_the_same_guard_in_propose(self):
        def candidate(job):
            return [self.group(job, 'CHAT', 'R7', message_ids=(7135,)),
                    self.ready(job, message_ids=(7133, 7134))]

        for authority in ('decide', 'propose'):
            with self.subTest(authority=authority):
                pipeline, _, jobs, sent = self.pipeline(candidate=candidate)
                self.seed_parent(pipeline)
                parent = dict(pipeline.store.get(CHAT, 7133))
                originals = [self.parent_reply(), message(7135, 'POS kẹt giấy')]
                for source in originals:
                    pipeline.store.enqueue(source)
                batch = f'batch:{CHAT}:7134'
                save = pipeline.rules_pending.save_batch

                def crash_after_save(*args):
                    self.assertTrue(save(*args))
                    raise SystemExit

                with patch.object(pipeline.rules_pending, 'save_batch', side_effect=crash_after_save):
                    with self.assertRaises(SystemExit):
                        rules_intake._process_batch(pipeline, originals)
                self.assertTrue(pipeline.rules_pending.transition_group(batch, 0, 'PLANNED', 'RESERVED'))
                self.assertTrue(pipeline.rules_pending.transition_group(batch, 0, 'RESERVED', 'BLOCKED'))
                pipeline.rules_pending.finish_attempt(batch, 1)
                pipeline.config = replace(pipeline.config, assistant=replace(pipeline.config.assistant,
                                                                              authority=authority))
                result, = rules_intake.sweep(pipeline)
                self.assertEqual(('pending_review', 'guard_rules_batch_blocked'), (result.decision, result.reason))
                self.assertEqual(['BLOCKED', 'BLOCKED'],
                                 [status for _, _, status in pipeline.rules_pending.batch(batch)[1]])
                self.assertEqual(parent, dict(pipeline.store.get(CHAT, 7133)))
                self.assertEqual([], pipeline.github.calls)
                self.assertEqual([], pipeline.classifier.calls)
                self.assertEqual(1, len(jobs))
                self.assertEqual(1, len(sent))
                self.assertEqual([], rules_intake.sweep(pipeline))

    def test_two_planned_batches_claim_same_parent_only_once(self):
        def candidate(job):
            ids = job['data']['source_ids']
            if ids == [7133, 7135, 7136]:
                current = self.ready(job, message_ids=(7135,))
                fields = {key: dict(value, source_ids=[7135]) for key, value in current.issue['fields'].items()}
                return [replace(current, issue=dict(current.issue, fields=fields)),
                        self.ready(job, message_ids=(7133, 7136))]
            if ids == [7136]:
                return self.group(job, 'APPEND', 'R6', targets=(job['guard_ctx'].reply_proofs[7136],))
            return self.ready(job)

        pipeline, audit, jobs, _ = self.pipeline(candidate=candidate)
        self.seed_parent(pipeline)
        first = self.parent_reply()
        independent = message(7135, 'POS kẹt giấy')
        second = self.parent_reply(7136)
        create = pipeline.github.create_issue
        def create_distinct(issue):
            create(issue)
            return 41 + len(pipeline.github.calls), None
        pipeline.github.create_issue = create_distinct
        save = pipeline.rules_pending.save_batch

        def crash_after_save(*args):
            self.assertTrue(save(*args))
            raise SystemExit

        for sources in ([first], [independent, second]):
            for source in sources:
                pipeline.store.enqueue(source)
            with patch.object(pipeline.rules_pending, 'save_batch', side_effect=crash_after_save):
                with self.assertRaises(SystemExit):
                    rules_intake._process_batch(pipeline, sources)
        self.assertEqual([{7134: [7133]}, {7136: [7133]}],
                         [job['data']['parent_pull'] for job in jobs])
        # The stub normally records only its first group; include both audit outcomes for this replay.
        speculative_id = audit.rows()[1]['run_id']
        with pipeline.store.connection:
            pipeline.store.connection.execute(
                "UPDATE classification_runs SET final_state='MULTI',outcomes_json=? WHERE run_id=?",
                (json.dumps([dict(state='READY', message_ids=[7135]),
                             dict(state='READY', message_ids=[7133, 7136])]), speculative_id))
        self.assertEqual(42, rules_intake._process_batch(pipeline, [first], f'batch:{CHAT}:7134').issue_number)
        winner = dict(pipeline.store.get(CHAT, 7133))
        result = rules_intake._process_batch(pipeline, [independent, second], f'batch:{CHAT}:7135')
        self.assertEqual((43, ((CHAT, 7135),)), (result.issue_number, result.reaction_targets))
        self.assertEqual('followup', pipeline.store.get(CHAT, 7136)['decision'])
        pipeline.github.add_comment.assert_called_once()
        self.assertEqual(42, pipeline.github.add_comment.call_args.args[0])
        speculative = next(row for row in audit.rows() if json.loads(row['message_ids']) == [7133, 7135, 7136])
        self.assertEqual('https://github.com/sandbox/triage/issues/43', speculative['ticket_ref'])
        self.assertEqual('MULTI', speculative['final_state'])
        self.assertEqual(['READY', 'SUPERSEDED'], [outcome['state'] for outcome in json.loads(speculative['outcomes_json'])])
        self.assertEqual(winner, dict(pipeline.store.get(CHAT, 7133)))
        self.assertEqual(2, len(pipeline.github.calls))
        self.assertEqual([7136], jobs[-1]['data']['source_ids'])
        self.assertEqual(3, len(jobs))

    def test_prefilter_single_and_batch_write_one_code_row_each(self):
        pipeline, audit, jobs, sent = self.pipeline()
        source = message(1, 'https://example.com')
        self.assertEqual(pipeline.process(source).reason, 'F3')
        batch = [message(i, 'https://example.com') for i in (2, 3)]
        for member in batch:
            pipeline.store.enqueue(member)
        self.assertEqual(rules_intake._process_batch(pipeline, batch).reason, 'F3')
        self.assertEqual(jobs, [])
        self.assertEqual(sent, [])
        rows = audit.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row['phase'] for row in rows], ['early', 'extract'])
        rule_text = next(line for line in (ROOT / 'rules' / 'readiness.md').read_text(encoding='utf-8').splitlines()
                         if line.startswith('| W4 |'))
        for row in rows:
            trace = json.loads(row['code_precheck_json'])
            self.assertTrue(trace['handled_by_code'])
            self.assertEqual(trace['rule_text'], rule_text)
            self.assertEqual(trace['checks'][-1]['rule'], 'F3')
            self.assertTrue(trace['checks'][-1]['match'])

    def test_handoff_includes_all_checks_and_opens_no_second_row(self):
        pipeline, audit, jobs, sent = self.pipeline()
        result = pipeline.process(message(1, 'Chờ sau golive nha e', mentions_listener=False))
        self.assertEqual(result.reason, 'R7')
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(audit.rows()), 1)
        self.assertEqual(audit.legacy_calls, [])
        self.assertEqual(sent, [])
        trace = jobs[0]['code_precheck']
        self.assertEqual(trace['handoff'], 'assistant')
        self.assertFalse(trace['handled_by_code'])
        self.assertEqual(tuple(row['rule'] for row in trace['checks']), CHECKS)
        self.assertTrue(all(not row['match'] and row['why'] for row in trace['checks']))
        self.assertEqual(jobs[0]['data']['sources'][0]['text'], 'Chờ sau golive nha e')
        self.assertEqual(jobs[0]['data']['images_order'], [])
        self.assertEqual(jobs[0]['guard_ctx'].rule_ids, frozenset(
            [f'R{i}' for i in range(1, 14)] + [f'F{i}' for i in range(1, 7)]))
        readiness = (ROOT / 'rules' / 'readiness.md').read_text(encoding='utf-8').splitlines()
        self.assertEqual(jobs[0]['guard_ctx'].rule_text('R7'),
                         next(line for line in readiness if line.startswith('| R7 |')))
        _, multi = rules_intake.early_decision_trace(self.snapshot, [message(2), message(3)])
        self.assertEqual([row['why'] for row in multi['checks'][-3:]], ['batch nhiều tin'] * 3)

    def test_merge_single_and_mixed_prefilter_batch_skip_decide_and_render_f7(self):
        pipeline, audit, jobs, sent = self.pipeline()
        merge = message(1, '3 PR của e merge rồi nha @vomaingoc', mentions_listener=False)
        review = message(3, 'https://github.com/vlink-group/vlink-nexora/pull/3048\nreview giúp em pr nha anh @PmqQuan',
                         mentions_listener=False)
        self.assertEqual('F1', rules_intake.early_decision(self.snapshot, [review]).rule)
        self.assertEqual('F7', pipeline.process(merge).reason)
        batch = [review, replace(merge, message_id=4)]
        for source in batch:
            pipeline.store.enqueue(source)
        self.assertEqual('F7', rules_intake._process_batch(pipeline, batch).reason)
        self.assertEqual(([], [], [], []), (jobs, pipeline.classifier.calls, pipeline.github.calls, sent))
        single, mixed = (json.loads(row['code_precheck_json']) for row in audit.rows())
        self.assertEqual(['F7'], [row['rule'] for row in single['checks']])
        self.assertEqual([('F7', [4]), ('F1', [3])],
                         [(row['rule'], row['message_ids']) for row in mixed['checks']])
        for trace in (single, mixed):
            self.assertEqual('F7', trace['decided_rule'])
            self.assertTrue(trace['rule_text'].startswith('| M3 |'))
            run = dict(code_precheck=trace, decision_rule='F7', outcomes=[])
            admin._present_rules(run)
            self.assertIn('F7 · chứa từ merge', run['precheck_summary'])

    def test_merge_batch_with_unmatched_pr_chat_reaches_decide(self):
        pipeline, _, jobs, _ = self.pipeline()
        batch = [
            message(1, '3 PR của e merge rồi nha @vomaingoc', mentions_listener=False),
            message(2, '@NguyenLe171120 PR BE check comment của con copilot nha', mentions_listener=False),
            message(3, 'https://github.com/vlink-group/vlink-nexora/pull/3048\nreview giúp em pr nha anh @PmqQuan',
                    mentions_listener=False),
        ]
        self.assertIsNone(rules_intake.early_decision(self.snapshot, [batch[1]]))
        for source in batch:
            pipeline.store.enqueue(source)
        rules_intake._process_batch(pipeline, batch)
        self.assertEqual(1, len(jobs))
        self.assertEqual(batch, list(jobs[0]['sources']))
        trace = jobs[0]['code_precheck']
        self.assertFalse(trace['handled_by_code'])
        self.assertEqual('assistant', trace['handoff'])
        self.assertEqual([('F7', [1]), ('F1', [3])],
                         [(row['rule'], row['message_ids']) for row in trace['checks'] if row['match']])

    def test_review_merge_reply_skips_both_model_routes_and_side_effects(self):
        for authority in ('decide', 'propose'):
            with self.subTest(authority=authority):
                pipeline, audit, jobs, sent = self.pipeline(authority=authority)
                parent = message(10, PR_REVIEW, sender_id=201, mentions_listener=False)
                acknowledgement = message(11, MERGE_ACK, sender_id=202, reply_to_id=10, mentions_listener=False)
                self.assertEqual('F1', pipeline.process(parent).reason)
                result = pipeline.process(acknowledgement)
                self.assertEqual(('chat', 'F6'), (result.decision, result.reason))
                self.assertEqual(([], [], [], []), (jobs, pipeline.classifier.calls, pipeline.github.calls, sent))
                self.assertEqual(('CHAT', 'F6'), tuple(pipeline.store.connection.execute(
                    'SELECT state, decision_rule FROM rules_decisions ORDER BY id DESC LIMIT 1').fetchone()))
                if authority == 'decide':
                    trace = json.loads(audit.rows()[-1]['code_precheck_json'])
                    self.assertTrue(trace['handled_by_code'])
                    self.assertEqual('F6', trace['decided_rule'])
                    self.assertIsNone(trace['handoff'])
                    self.assertTrue(trace['rule_text'].startswith('| K4 |'))

    def test_review_merge_reply_batch_skips_both_model_routes(self):
        for authority in ('decide', 'propose'):
            with self.subTest(authority=authority):
                pipeline, _, jobs, sent = self.pipeline(authority=authority)
                parent = message(10, PR_REVIEW, sender_id=201, mentions_listener=False)
                self.assertEqual('F1', pipeline.process(parent).reason)
                replies = [message(mid, text, sender_id=202, reply_to_id=10, mentions_listener=False)
                           for mid, text in ((11, MERGE_ACK), (12, 'Anh đã merge rồi nhé!'))]
                for source in replies:
                    pipeline.store.enqueue(source)
                rules_intake._process_batch(pipeline, replies)
                for source in replies:
                    stored = pipeline.store.get(source.chat_id, source.message_id)
                    self.assertEqual(('chat', 'F6'), (stored['decision'], stored['reason']))
                self.assertEqual(([], [], [], []), (jobs, pipeline.classifier.calls, pipeline.github.calls, sent))

    def test_review_merge_reply_requires_plain_acknowledgement_and_eligible_parent(self):
        pipeline, _, jobs, _ = self.pipeline()
        parent = message(10, PR_REVIEW, sender_id=201, mentions_listener=False)
        foreign_parent = replace(parent, chat_id=CHAT - 1, message_id=8)
        issue_parent = replace(parent, message_id=9, text=PR_REVIEW.replace('/pull/', '/issues/'))
        for target in (foreign_parent, issue_parent, parent):
            self.assertEqual('F1', pipeline.process(target).reason)
        acknowledgement = message(11, MERGE_ACK, sender_id=202, reply_to_id=10, mentions_listener=False)
        for changes in (
            {'text': 'a merge rồi nha?'}, {'text': 'a merge rồi nha？'}, {'text': 'a chưa merge nha'},
            {'text': 'a merge rồi nha nhưng conflict'}, {'text': 'a merge rồi nha, thêm nút export giúp em'},
            {'text': 'anh merge nha'}, {'text': '/task a merge rồi nha'}, {'mentions_listener': True},
            {'has_image': True}, {'has_document': True}, {'is_forward': True}, {'sender_id': 201},
            {'sender_id': None}, {'reply_to_id': 8}, {'reply_to_id': 9},
        ):
            with self.subTest(changes=changes):
                source = replace(acknowledgement, **changes)
                decision = rules_intake.early_decision(
                    self.snapshot, [source], chat_replies=rules_intake._chat_reply_ids(pipeline, [source]))
                self.assertIsNone(decision)
        request = replace(acknowledgement, text='a merge rồi nha, thêm nút export giúp em')
        pipeline.process(request)
        self.assertEqual(1, len(jobs))
        self.assertEqual((request,), jobs[0]['sources'])

    def test_content_only_batch_without_merge_uses_first_messages_rule(self):
        small_talk = message(1, 'cảm ơn')
        link = message(2, 'https://example.com')
        for first, second, expected in ((small_talk, link, 'F4'), (link, small_talk, 'F3')):
            decision, trace = rules_intake.early_decision_trace(self.snapshot, [first, second])
            self.assertEqual(('CHAT', expected), (decision.state, decision.rule))
            self.assertEqual([('F3', [2]), ('F4', [1])],
                             [(row['rule'], row['message_ids']) for row in trace['checks'] if row['match']])

    def test_guard_holds_without_github_or_telegram(self):
        pipeline, audit, jobs, sent = self.pipeline(outcome='guard')
        source = message(1, 'Chờ sau golive nha e')
        result = pipeline.process(source)
        self.assertEqual((result.decision, result.reason), ('pending_review', 'guard_grounding'))
        self.assertEqual(pipeline.github.calls, [])
        pipeline.github.lifecycle_snapshot.assert_not_called()
        pipeline.github.add_comment.assert_not_called()
        pipeline.reactor.assert_not_called()
        self.assertEqual(sent, [])
        self.assertEqual(len(audit.rows()), 1)
        self.assertEqual(pipeline.store.connection.execute('SELECT state FROM rules_decisions').fetchone()[0], 'HOLD')

    def test_infra_reuses_wait_without_spending_an_attempt(self):
        pipeline, audit, jobs, sent = self.pipeline(outcome='infra')
        with patch.object(rules_intake, '_assistant_wait', wraps=rules_intake._assistant_wait) as wait:
            result = pipeline.process(message(1))
        self.assertEqual((result.decision, result.reason), ('pending_review', 'rules_hold'))
        self.assertEqual(wait.call_args.kwargs, {'force': True})
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(audit.rows()), 1)
        row = pipeline.store.connection.execute('SELECT phase,attempts,attempt_reserved FROM rules_topics').fetchone()
        self.assertEqual(tuple(row), ('HOLD', 0, 0))
        self.assertEqual(sent, [])

    def test_model_errors_escalate_after_retry_budget(self):
        pipeline, _, jobs, sent = self.pipeline(outcome='model_error')
        self.assertEqual(pipeline.process(message(1)).reason, 'rules_hold')
        self.assertEqual(rules_intake.sweep(pipeline)[0].reason, 'rules_hold')
        self.assertEqual(rules_intake.sweep(pipeline)[0].reason, 'rules_hold_escalated')
        self.assertEqual(len(jobs), 3)
        self.assertEqual([chat for chat, _, _ in sent], [OPS])

    def test_reply_after_30_minutes_reuses_original_thread(self):
        def candidate(job):
            if job['sources'][0].message_id == 1:
                return self.ready(job)
            return self.group(job, 'APPEND', 'R6', targets=(job['guard_ctx'].reply_proofs[22],))

        pipeline, audit, jobs, sent = self.pipeline(candidate=candidate)
        original = message(1, 'POS kẹt giấy')
        self.assertEqual(pipeline.process(original).issue_number, 42)
        first_run = audit.rows()[0]
        reply = message(22, 'Vẫn bị kẹt', reply_to_id=1, received_at=original.received_at + timedelta(minutes=30))
        self.assertEqual(pipeline.process(reply).decision, 'followup')
        self.assertEqual(jobs[1]['log_thread_id'], first_run['log_thread_id'])
        self.assertEqual(jobs[1]['guard_ctx'].reply_targets, {22: (42, 'open')})
        self.assertEqual([row['ticket_ref'] for row in audit.rows()], ['https://github.com/sandbox/triage/issues/42'] * 2)
        audit.set_thread.assert_called_once_with(pipeline.store, audit.rows()[1]['run_id'], first_run['log_thread_id'])
        self.assertEqual([chat for chat, _, _ in sent], [OPS, OPS])

    def test_search_append_uses_guard_passed_target_and_rechecks_state(self):
        def candidate(job):
            return self.group(job, 'APPEND', 'R6', targets=({'number': 17, 'state': 'open', 'basis': 'search'},))

        pipeline, _, _, _ = self.pipeline(candidate=candidate)
        self.assertEqual(pipeline.process(message(1)).issue_number, 17)
        self.assertEqual(pipeline.github.calls, [])
        pipeline.github.add_comment.assert_called_once()
        self.assertEqual(pipeline.github.add_comment.call_args.args[0], 17)
        self.assertEqual(pipeline.github.lifecycle_snapshot.call_count, 2)

    def test_search_append_without_guard_evidence_cannot_write(self):
        def candidate(job):
            return self.group(job, 'APPEND', 'R6', target={'number': 17, 'basis': 'search'})

        pipeline, _, _, sent = self.pipeline(candidate=candidate)
        self.assertEqual(pipeline.process(message(1)).reason, 'guard_target')
        self.assertEqual(pipeline.github.calls, [])
        pipeline.github.add_comment.assert_not_called()
        self.assertEqual([chat for chat, _, _ in sent], [OPS])
        self.assertEqual(pipeline.rules_pending.batch(f'batch:{CHAT}:1')[1][0][2], 'BLOCKED')

    def test_saved_plan_resumes_after_authority_flag_changes(self):
        pipeline, _, jobs, _ = self.pipeline(candidate=self.ready)
        source = message(1, 'POS kẹt giấy')
        batch_id = f'batch:{CHAT}:1'
        with patch.object(pipeline.rules_pending, 'transition_group', side_effect=RuntimeError('interrupted')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                pipeline.process(source)
        self.assertEqual(pipeline.rules_pending.batch(batch_id)[0], self.snapshot.fingerprint)
        pipeline.config = replace(pipeline.config, assistant=replace(pipeline.config.assistant, authority='propose'))
        self.assertEqual(rules_intake._process_batch(pipeline, [source], batch_id).issue_number, 42)
        self.assertEqual(len(jobs), 1)

    def test_question_renders_in_ops_and_topic_ref_stays_legacy(self):
        def candidate(job):
            return self.group(job, 'NEED_INFO', 'R12',
                              question={'block_id': 'ask.object', 'fields': ['object']})

        pipeline, audit, jobs, sent = self.pipeline(candidate=candidate)
        self.assertEqual(pipeline.process(message(42, 'bị lỗi')).decision, 'pending_review')
        self.assertEqual(sent[0][:2], (OPS, None))
        self.assertIn(active().render('group-replies.md', 'ask.object'), sent[0][2])
        topic = pipeline.rules_pending.topic_snapshot(f'{CHAT}:42')
        self.assertEqual(json.loads(topic['asked']), {'object': 1})
        answer = message(150, 'POS kẹt giấy', chat_id=OPS, reply_to_id=101, sender_id=7)
        self.assertTrue(pipeline.rules_pending.accept_ops_message(answer, self.snapshot.limits['pending_expiry_days']))
        result = pipeline.process(answer)
        self.assertEqual((result.decision, result.issue_number), ('issue', 42))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(pipeline.classifier.calls), 1)
        self.assertEqual([row['id_space'] for row in audit.rows()], ['message', 'topic_ref'])

    def capture_legacy_output(self, **options):
        pipeline, audit, jobs, sent = self.pipeline(**options)
        results = [pipeline.process(message(1, 'https://example.com')),
                   pipeline.process(message(2, 'POS kẹt giấy'))]
        self.assertEqual(jobs, [])
        return json.dumps({
            'results': [asdict(result) for result in results],
            'rows': [tuple(row) for row in audit.rows()],
            'github': [asdict(issue) for issue in pipeline.github.calls],
            'telegram': sent, 'reactor': pipeline.reactor.call_args_list,
            'provider': pipeline.classifier.calls,
        }, ensure_ascii=False, separators=(',', ':')).encode('utf-8')

    def test_propose_and_excluded_canary_have_byte_identical_output(self):
        self.assertEqual(self.capture_legacy_output(authority='propose'),
                         self.capture_legacy_output(canary=(CHAT - 1,)))

    def test_propose_prefilter_handles_snapshot_missing_filter_row(self):
        pipeline, audit, jobs, _ = self.pipeline(authority='propose')
        snapshot = RulesSnapshot(ROOT / 'rules', ROOT / 'templates' / 'github-ticket')
        readiness = snapshot.tables['readiness']
        table, key = rules_intake._FILTER_ROWS['F3']
        rows = tuple(row for row in readiness[table] if row.cells['#'] != key)
        source = message(1, 'https://example.com')
        tables = dict(snapshot.tables, readiness=dict(readiness, **{table: rows}))
        object.__setattr__(snapshot, 'tables', tables)
        with patch.object(pipeline, 'rules_snapshot', snapshot):
            self.assertEqual(rules_intake._rule_row_text(
                rules_intake._snapshot_rule_rows(pipeline.rules_snapshot).get('F3')), '')
            self.assertEqual(pipeline.process(source).decision, 'chat')
        self.assertEqual(jobs, [])
        self.assertIsNone(audit.rows()[0]['code_precheck_json'])


class RealDecideIntakeTests(unittest.TestCase):
    def pipeline(self, chat, *, downloader=None, uploader=None, pack_mode='full'):
        base = config('live')
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        db_path = Path(directory.name) / 'triage.db'
        settings = replace(base, db_path=db_path, autonomous_enabled=True, decision_rules_enabled=True,
                           assistant=replace(base.assistant, enabled=True, mode='live', authority='decide',
                                             pack_mode=pack_mode, router_key='test', github_repos=(base.gh_repo,)),
                           telegram=replace(base.telegram, ops_chat_id=OPS))
        self.clock = Clock()
        store = Store(db_path, clock=self.clock)
        self.addCleanup(store.close)
        github = FakeGitHub()
        github.lifecycle_snapshot = Mock(side_effect=lambda n: dict(
            number=n, repo=settings.gh_repo, state='open', observed_at='2026-11-19'))
        github.add_comment = Mock(return_value=101)
        self.sent = Mock(side_effect=range(101, 110))
        provider = LegacyProvider()
        provider.supports_vision = downloader is not None
        pipeline = Pipeline(settings, store, provider, github, rules_snapshot=_load_decision_rules(),
                            assistant_pack=_load_assistant_pack(pack_mode=pack_mode), downloader=downloader,
                            uploader=uploader, notice_sender=self.sent, reactor=Mock(return_value=True))
        pipeline.assistant_runtime.chat = chat
        return pipeline

    @staticmethod
    def rows(pipeline):
        return pipeline.store.connection.execute('SELECT * FROM classification_runs ORDER BY run_id').fetchall()

    @staticmethod
    def icon_reply():
        return message(7134, 'Cần sửa lỗi icon trên menu trang khách', reply_to_id=7133,
                       sender_id=2, mentions_listener=False)

    @staticmethod
    def icon_decision(ids):
        return reply(7134, message_ids=ids, state='READY', rule='R10', template='task',
                     issue=issue('object', 'work', value='menu trang khách', source_ids=(7134,)))

    def test_pulled_batch_sends_j4_guidance_in_full_and_core_prompts(self):
        from tg_triage.rules_extraction import partition_and_extract
        from tg_triage.thinking import LOCAL_REQUEST_BUDGET_BYTES, request_text_bytes

        for mode in ('full', 'core'):
            with self.subTest(mode=mode):
                chat = Mock(return_value=self.icon_decision([7133, 7134, 7135]))
                pipeline = self.pipeline(chat, pack_mode=mode)
                DecideIntakeTests.seed_parent(pipeline)
                sources = [replace(self.icon_reply(), reply_to_id=None),
                           replace(self.icon_reply(), message_id=7135)]
                for source in sources:
                    pipeline.store.enqueue(source)
                with patch('tg_triage.rules_extraction.partition_and_extract', wraps=partition_and_extract) as pack:
                    result = rules_intake._process_batch(pipeline, sources)
                self.assertEqual((42, ((CHAT, 7134),)), (result.issue_number, result.reaction_targets))
                self.assertIn(f'<!-- tg:{CHAT}:7134 -->', pipeline.github.calls[0].body)
                system, user = chat.call_args.args[1]
                packet = json.loads(user['content'].split('UNTRUSTED BATCH DATA:\n', 1)[1])
                self.assertEqual({'7135': [7133]}, packet['sources'][0]['batch_context']['parent_pull'])
                j4 = pipeline.rules_snapshot.tables['readiness']['parent_pull'][3].cells['Bước']
                guidance = ('«Tin kéo là yêu cầu gốc, chỉ để hiểu ngữ cảnh. Chỉ ra READY khi các tin Reply mới '
                            'chốt việc cần làm hoặc cách làm (vd người giao: "sửa cho đồng bộ đi Mai cùng ý nghĩa"). '
                            'Lời nhận việc, hứa làm, báo đã xong, hỏi lại hoặc nhờ review PR của người được giao '
                            'KHÔNG biến tin kéo thành việc mới — khi đó giữ CHAT/F2. Trợ lý quyết cuối.»')
                self.assertTrue(j4.endswith(guidance))
                self.assertIn(guidance, system['content'])
                self.assertLessEqual(request_text_bytes(pack.call_args.kwargs['body_out'][0]),
                                     LOCAL_REQUEST_BUDGET_BYTES)

    def test_single_live_shaped_batch_leaves_grouping_and_f2_to_the_assistant(self):
        ids = [7132, 7133, 7134, 7135]
        for split in (False, True):
            with self.subTest(split=split):
                grouped_chat = reply(7132, message_ids=[7132] if split else ids, rule='F2')
                if split:
                    body = json.loads(grouped_chat.content)
                    ready = self.icon_decision(ids[1:])
                    body['groups'].extend(json.loads(ready.content)['groups'])
                    response = replace(grouped_chat, content=json.dumps(body))
                else:
                    response = grouped_chat

                def download(chat_id, mid, directory):
                    path = directory / 'menu.png'
                    path.write_bytes(PNG)
                    return path

                chat = Mock(return_value=response)
                pipeline = self.pipeline(chat, downloader=download, uploader=FakeUploader())
                self.assertEqual('F1', pipeline.process(message(7131, PR_REVIEW)).reason)
                sources = [
                    message(7132, 'Đổi icon menu @devmember', reply_to_id=7131, sender_id=1, mentions_listener=False),
                    message(7133, '', has_image=True, sender_id=1, mentions_listener=False),
                    self.icon_reply(),
                    message(7135, 'Đổi icon cho đồng bộ', sender_id=1, mentions_listener=False),
                ]
                saved = [replace(source, received_at=self.clock() + timedelta(seconds=gap))
                         for source, gap in zip(sources, (0, 27, 73, 97))]
                for source in saved:
                    pipeline.store.enqueue(source)
                result = rules_intake._process_batch(pipeline, saved)
                chat.assert_called_once()
                content = chat.call_args.args[1][1]['content']
                packet = json.loads(content[0]['text'].split('UNTRUSTED BATCH DATA:\n', 1)[1])
                self.assertEqual(ids, [row['message_id'] for row in packet['sources']])
                self.assertNotIn('parent_pull', packet['sources'][0]['batch_context'])
                self.assertTrue(packet['sources'][1]['pixels_available'])
                trace = json.loads(self.rows(pipeline)[-1]['code_precheck_json'])
                self.assertFalse(trace['handled_by_code'])
                self.assertEqual(('chat', 'F2'), tuple(pipeline.store.get(CHAT, 7132)[key]
                                                       for key in ('decision', 'reason')))
                if split:
                    self.assertEqual((42, 1, 1), (result.issue_number, len(pipeline.github.calls), result.images))
                    self.assertEqual([42] * 3, [pipeline.store.get(CHAT, mid)['issue_number'] for mid in ids[1:]])
                    self.assertNotIn('/7132)', pipeline.github.calls[0].body)
                else:
                    self.assertEqual('F2', result.reason)
                    self.assertEqual([], pipeline.github.calls)

    def test_parent_pull_claim_recovers_before_generic_store_queue_recovery(self):
        for failure in ('after_claim', 'before_post'):
            with self.subTest(failure=failure):
                chat = Mock(return_value=self.icon_decision([7133, 7134]))
                pipeline = self.pipeline(chat)
                DecideIntakeTests.seed_parent(pipeline)
                source = self.icon_reply()
                prior = dict(pipeline.store.get(CHAT, 7133))
                old_topic = pipeline.rules_pending.topic_snapshot(f'{CHAT}:7133')
                if failure == 'after_claim':
                    reserve = rules_intake._reserve_pulled_group
                    def crash_after_commit(*args):
                        self.assertTrue(reserve(*args))
                        self.assertEqual(PARENT_PULL_CLAIM, pipeline.store.get(CHAT, 7133)['reason'])
                        self.assertEqual({1: (CHAT, 7134), 2: (CHAT, 7133)},
                                         pipeline.rules_pending.sources(f'{CHAT}:7134'))
                        raise SystemExit
                    with patch.object(rules_intake, '_reserve_pulled_group', side_effect=crash_after_commit):
                        with self.assertRaises(SystemExit):
                            pipeline.process(source)
                else:
                    # _create_single has allocated the current case, but has not reserved an issue.
                    with patch.object(pipeline, '_run', side_effect=RuntimeError('before POST')):
                        with self.assertRaisesRegex(RulesHalt, 'batch can resume'):
                            pipeline.process(source)
                self.assertEqual([], pipeline.github.calls)
                path = pipeline.config.db_path
                raw = Path(path).read_bytes()
                check_before_recovery(path)
                self.assertEqual(raw, Path(path).read_bytes())
                recovered = Store(path, clock=self.clock)
                self.addCleanup(recovered.close)
                self.assertEqual(prior, dict(recovered.get(CHAT, 7133)))
                self.assertEqual([], recovered.queued_messages())
                self.assertIsNone(recovered.get_case(f'{CHAT}:7134'))
                restarted = Pipeline(pipeline.config, recovered, pipeline.classifier, pipeline.github,
                                     rules_snapshot=pipeline.rules_snapshot, assistant_pack=_load_assistant_pack(),
                                     notice_sender=self.sent)
                restarted.assistant_runtime.chat = chat
                self.assertEqual(old_topic, restarted.rules_pending.topic_snapshot(f'{CHAT}:7133'))
                self.assertEqual({1: (CHAT, 7134)}, restarted.rules_pending.sources(f'{CHAT}:7134'))
                self.assertEqual(42, rules_intake.sweep(restarted)[0].issue_number)
                chat.assert_called_once()
                self.assertEqual(1, len(pipeline.github.calls))

    def test_restart_in_propose_reclassifies_original_batch_after_prepost_interruption(self):
        from contextlib import ExitStack

        old_ids, new_ids = (7132, 7133), (7134, 7135)
        for stage in ('after_save', 'after_claim', 'before_post', 'fallback_not_started'):
            with self.subTest(stage=stage):
                chat = Mock(return_value=self.icon_decision([*old_ids, *new_ids]))
                pipeline = self.pipeline(chat)
                DecideIntakeTests.seed_parent(pipeline, message_id=7132, open_topic=True)
                picture = message(7133, '', sender_id=1, has_image=True, mentions_listener=False)
                pipeline.store.enqueue(picture)
                pipeline.store.finish(picture, Decision('chat', 'rule', reason='F2'))
                previous = f'{CHAT}:7132'
                pipeline.rules_pending.add_source(previous, CHAT, 7133)
                pipeline.rules_pending.close(previous, 'CLOSED')
                old_topic = pipeline.rules_pending.topic_snapshot(previous)
                before = [dict(pipeline.store.get(CHAT, mid)) for mid in old_ids]
                originals = [self.icon_reply(), message(7135, 'sửa cho đồng bộ đi Mai cùng ý nghĩa')]
                for source in originals:
                    pipeline.store.enqueue(source)

                def interrupt(func):
                    def crash(*args, **kwargs):
                        self.assertTrue(func(*args, **kwargs))
                        raise SystemExit
                    return crash

                with ExitStack() as stack:
                    if stage == 'after_save':
                        stack.enter_context(patch.object(pipeline.rules_pending, 'save_batch',
                                                        side_effect=interrupt(pipeline.rules_pending.save_batch)))
                    elif stage == 'after_claim':
                        stack.enter_context(patch.object(rules_intake, '_reserve_pulled_group',
                                                        side_effect=interrupt(rules_intake._reserve_pulled_group)))
                    elif stage == 'before_post':
                        stack.enter_context(patch.object(pipeline, '_run', side_effect=RuntimeError('before POST')))
                    else:
                        stack.enter_context(patch.object(rules_intake, '_reserve_pulled_group', return_value=False))
                        stack.enter_context(patch.object(rules_intake, '_defer_pull_group_without_parents',
                                                        side_effect=interrupt(rules_intake._defer_pull_group_without_parents)))
                    with self.assertRaises(RulesHalt if stage == 'before_post' else SystemExit):
                        rules_intake._process_batch(pipeline, originals)

                self.assertEqual([], pipeline.github.calls)
                path = pipeline.config.db_path
                check_before_recovery(path)
                store = Store(path, clock=self.clock)
                self.addCleanup(store.close)
                settings = replace(pipeline.config, assistant=replace(pipeline.config.assistant, authority='propose'))
                restarted = Pipeline(settings, store, pipeline.classifier, pipeline.github,
                                     rules_snapshot=pipeline.rules_snapshot, assistant_pack=_load_assistant_pack(),
                                     notice_sender=self.sent)
                facts = {name: dict(value=value, source_ids=[7135]) for name, value in
                         (('intent', 'task'), ('object', 'menu trang khách'), ('work', 'sửa icon cho đồng bộ'))}
                proposal = json.dumps({'groups': [dict(message_ids=list(new_ids), facts=facts)]})
                restarted.assistant_runtime.chat = Mock(return_value=replace(reply(7134), content=proposal))
                outcome, = rules_intake.sweep(restarted)
                self.assertEqual(('issue', 'R10', 42), (outcome.decision, outcome.reason, outcome.issue_number))
                restarted.assistant_runtime.chat.assert_called_once()
                packet = json.loads(restarted.assistant_runtime.chat.call_args.args[1][1]['content'].split(
                    'UNTRUSTED BATCH DATA:\n', 1)[1])
                self.assertEqual(list(new_ids), [row['message_id'] for row in packet['sources']])
                self.assertNotIn('parent_pull', packet['sources'][0]['batch_context'])
                self.assertEqual(before, [dict(store.get(CHAT, mid)) for mid in old_ids])
                self.assertEqual(old_topic, restarted.rules_pending.topic_snapshot(previous))
                self.assertEqual({1: (CHAT, 7134), 2: (CHAT, 7135)}, restarted.rules_pending.sources(f'{CHAT}:7134'))
                self.assertEqual([], store.connection.execute("SELECT 1 FROM rules_topics WHERE status='OPEN'").fetchall())
                self.assertEqual([], rules_intake.sweep(restarted))
                self.assertEqual(1, len(pipeline.github.calls))
                self.assertEqual([42, 42], [store.get(CHAT, mid)['issue_number'] for mid in new_ids])
                check_before_recovery(path)

    def test_parent_pull_rate_limited_releases_prepost_claim_and_topic_membership(self):
        pipeline = self.pipeline(Mock(return_value=self.icon_decision([7133, 7134])))
        DecideIntakeTests.seed_parent(pipeline)
        prior = dict(pipeline.store.get(CHAT, 7133))
        old_topic = pipeline.rules_pending.topic_snapshot(f'{CHAT}:7133')
        pipeline.config = replace(pipeline.config, max_issues_per_hour=0)

        self.assertEqual('rate_limited', pipeline.process(self.icon_reply()).decision)
        self.assertEqual([], pipeline.github.calls)
        self.assertIsNone(pipeline.store.get(CHAT, 7134)['github_called_at'])
        self.assertEqual(prior, dict(pipeline.store.get(CHAT, 7133)))
        self.assertEqual(old_topic, pipeline.rules_pending.topic_snapshot(f'{CHAT}:7133'))
        self.assertEqual({1: (CHAT, 7134)}, pipeline.rules_pending.sources(f'{CHAT}:7134'))
        self.assertIsNone(pipeline.rules_pending.topic_for_source_reply(CHAT, 7133, 7))
        child = replace(self.icon_reply(), message_id=7135)
        self.assertEqual({7135: [7133]}, rules_intake._pull_parents(pipeline, [child])[1])

    def test_parent_pull_ambiguous_post_keeps_sources_bound_and_never_repulls(self):
        from tg_triage.rules_pending_store import PARENT_PULL_AMBIGUOUS

        pipeline = self.pipeline(Mock(return_value=self.icon_decision([7133, 7134])))
        DecideIntakeTests.seed_parent(pipeline)
        prior = dict(pipeline.store.get(CHAT, 7133))
        old_topic = pipeline.rules_pending.topic_snapshot(f'{CHAT}:7133')
        pipeline.github.error = OSError('unknown POST outcome')

        with self.assertRaisesRegex(RulesHalt, 'ambiguous operation'):
            pipeline.process(self.icon_reply())
        self.assertEqual(1, len(pipeline.github.calls))
        self.assertIsNotNone(pipeline.store.get(CHAT, 7134)['github_called_at'])
        self.assertEqual(dict(prior, reason=PARENT_PULL_AMBIGUOUS), dict(pipeline.store.get(CHAT, 7133)))
        self.assertEqual(old_topic, pipeline.rules_pending.topic_snapshot(f'{CHAT}:7133'))
        self.assertEqual({1: (CHAT, 7134), 2: (CHAT, 7133)}, pipeline.rules_pending.sources(f'{CHAT}:7134'))
        with self.assertRaisesRegex(RulesHalt, r'ambiguous operation \(parent pull\).*#owner-runbook-for-ambiguous-parent-pulls'):
            check_before_recovery(pipeline.config.db_path)

        # Even a closed topic (after inspecting the original) cannot make that source pullable.
        pipeline.rules_pending.close(f'{CHAT}:7134', 'CLOSED')
        child = replace(self.icon_reply(), message_id=7135)
        self.assertEqual(([], {}), rules_intake._pull_parents(pipeline, [child]))
        recovered = Store(pipeline.config.db_path, clock=self.clock)
        self.addCleanup(recovered.close)
        self.assertEqual(dict(prior, reason=PARENT_PULL_AMBIGUOUS), dict(recovered.get(CHAT, 7133)))

    def test_parent_pull_retries_originals_on_router_size_or_coverage_failure(self):
        for failure in ('too_large', 'guard_coverage'):
            with self.subTest(failure=failure):
                packets = []

                def reply_from_model(link, messages, **options):
                    first = messages[1]['content']
                    if isinstance(first, list):
                        first = first[0]['text']
                    packet = json.loads(first.split('UNTRUSTED BATCH DATA:\n', 1)[1])
                    ids = [source['message_id'] for source in packet['sources']]
                    packets.append(ids)
                    if len(ids) > 1 and failure == 'too_large':
                        raise RouterError('too_large')
                    return self.icon_decision([7134])

                pipeline = self.pipeline(Mock(side_effect=reply_from_model))
                DecideIntakeTests.seed_parent(pipeline)
                prior = dict(pipeline.store.get(CHAT, 7133))
                self.assertEqual(42, pipeline.process(self.icon_reply()).issue_number)
                self.assertEqual([7133, 7134], packets[0])
                self.assertEqual([[7134]], [ids for ids in packets if ids == [7134]])
                self.assertEqual(1, len(pipeline.github.calls))
                self.assertEqual(prior, dict(pipeline.store.get(CHAT, 7133)))
                self.sent.assert_called_once()
                discarded = [row for row in self.rows(pipeline) if json.loads(row['message_ids']) == [7133, 7134]]
                self.assertTrue(discarded)
                self.assertTrue(all(row['final_state'] == 'SUPERSEDED' and row['ticket_ref'] is None
                                    for row in discarded))
                if failure == 'guard_coverage':
                    self.assertGreaterEqual(packets.count([7133, 7134]), 2)
                    self.assertTrue(any(row['error_class'] == 'guard_coverage' for row in self.rows(pipeline)))

    def test_parent_pull_read_context_respects_reply_age(self):
        from tg_triage.rules_extraction import partition_and_extract
        from tg_triage.thinking import LOCAL_REQUEST_BUDGET_BYTES, request_text_bytes

        chat = Mock(return_value=self.icon_decision([7133, 7134, 7150]))
        pipeline = self.pipeline(chat)
        reply_at = self.clock()
        for mid, age in ((7130, timedelta(hours=24, seconds=1)), (7132, timedelta(hours=24))):
            pipeline.store.enqueue(message(mid, 'Ngữ cảnh menu', received_at=reply_at - age))
        DecideIntakeTests.seed_parent(pipeline, received_at=reply_at - timedelta(hours=23))
        # This older timestamp is only reachable via the added between-message scan.
        pipeline.store.enqueue(message(7138, 'Tin đồng bộ muộn',
                                       received_at=reply_at - timedelta(hours=24, seconds=1)))
        middle = message(7140, 'em fix xong icon menu rồi nha anh, đã lên staging',
                         sender_id=5, received_at=reply_at - timedelta(minutes=10))
        pipeline.store.enqueue(middle)
        pipeline.store.finish(middle, Decision('chat', 'rule', reason='R7'))
        # Original 7134 is earlier than 7140. History must be anchored on new
        # Reply 7150, not 7133 (pulled) or 7134 (earliest original in the batch).
        originals = [replace(self.icon_reply(), reply_to_id=None, received_at=reply_at - timedelta(minutes=11)),
                     replace(self.icon_reply(), message_id=7150, received_at=reply_at)]
        for source in originals:
            pipeline.store.enqueue(source)
        with patch('tg_triage.rules_extraction.partition_and_extract', wraps=partition_and_extract) as pack:
            self.assertEqual(42, rules_intake._process_batch(pipeline, originals).issue_number)
        content = chat.call_args.args[1][1]['content']
        packet = json.loads(content.split('UNTRUSTED BATCH DATA:\n', 1)[1])
        context = packet['sources'][0]['batch_context']['read_only_context']
        self.assertEqual([7133, 7134, 7150], [row['message_id'] for row in packet['sources']])
        self.assertEqual([7130, 7132, 7140], [row['message_id'] for row in context])
        self.assertLessEqual(request_text_bytes(pack.call_args.kwargs['body_out'][0]), LOCAL_REQUEST_BUDGET_BYTES)

    def test_pulled_batch_prioritizes_other_originals_reply_targets_even_after_24h(self):
        chat = Mock(return_value=reply(7134, message_ids=[7133, 7134, 7135], rule='R7'))
        pipeline = self.pipeline(chat)
        now = self.clock()
        stored = [message(7050, 'đơn A12 in hóa đơn bị lỗi khi in 2 liên',
                          received_at=now - timedelta(hours=30), sender_id=3)]
        stored += [message(7100 + i, f'đang họp phòng số {i}', sender_id=4,
                           received_at=now - timedelta(hours=2) + timedelta(minutes=i)) for i in range(10)]
        for source in stored:
            pipeline.store.enqueue(source)
            pipeline.store.finish(source, Decision('chat', 'rule', reason='R7'))
        DecideIntakeTests.seed_parent(pipeline, received_at=now - timedelta(minutes=30))
        originals = [replace(self.icon_reply(), received_at=now),
                     message(7135, 'vụ in 2 liên này xử lý chưa em', reply_to_id=7050,
                             sender_id=5, received_at=now, mentions_listener=False)]
        for source in originals:
            pipeline.store.enqueue(source)
        self.assertEqual('chat', rules_intake._process_batch(pipeline, originals).decision)
        content = chat.call_args.args[1][1]['content']
        packet = json.loads(content.split('UNTRUSTED BATCH DATA:\n', 1)[1])
        self.assertEqual([7133, 7134, 7135], [row['message_id'] for row in packet['sources']])
        context = packet['sources'][0]['batch_context']['read_only_context']
        self.assertEqual(7050, context[-1]['message_id'])
        self.assertIn('đơn A12', context[-1]['text'])
        self.assertNotIn(7133, [row['message_id'] for row in context])
        self.assertEqual([], pipeline.github.calls)

    def test_parent_pull_preserves_normal_mood_without_extra_examples(self):
        from tg_triage.rules_extraction import partition_and_extract
        from tg_triage.thinking import request_text_bytes

        for limit in (8000, 24000):
            with self.subTest(limit=limit):
                chat = Mock(return_value=reply(7150, message_ids=[7133, 7150], rule='R7'))
                pipeline = self.pipeline(chat)
                pipeline.classifier.request_budget_bytes = limit
                now = self.clock()
                for mid, age, sender, text in ((7130, 5, 6, 'hahaha 😂😂'), (7131, 4, 7, 'chết cười 🤣')):
                    source = message(mid, text, sender_id=sender, mentions_listener=False,
                                     received_at=now - timedelta(minutes=age))
                    pipeline.store.enqueue(source)
                    pipeline.store.finish(source, Decision('chat', 'rule', reason='R7'))
                DecideIntakeTests.seed_parent(pipeline, received_at=now - timedelta(seconds=90))
                original = message(7150, 'icon mà lỗi font thì đổi thành hình con mèo luôn anh',
                                   reply_to_id=7133, sender_id=2, mentions_listener=False, received_at=now)
                pipeline.store.enqueue(original)
                reference = []
                partition_and_extract(pipeline.classifier, pipeline.rules_snapshot, [original],
                                      read_context=pipeline.store.autonomous_read_context([original]),
                                      body_out=reference)
                ordinary = json.loads(reference[0]['messages'][1]['content'])
                with patch('tg_triage.rules_extraction.partition_and_extract', wraps=partition_and_extract) as pack:
                    self.assertEqual('chat', pipeline.process(original).decision)
                content = chat.call_args.args[1][1]['content']
                packet = json.loads(content.split('UNTRUSTED BATCH DATA:\n', 1)[1])
                self.assertEqual([7133, 7150], [row['message_id'] for row in packet['sources']])
                context = packet['sources'][0]['batch_context']
                self.assertEqual(ordinary['group_mood'], context['group_mood'])
                self.assertEqual((True, 2, 2), tuple(context['group_mood'][key] for key in
                                                     ('joking', 'laugh_messages', 'laugh_senders')))
                if limit == 24000:
                    self.assertEqual([(7130, 5, True), (7131, 4, True)],
                                     [(r['message_id'], r['min_ago'], r['laugh']) for r in context['read_only_context']])
                self.assertNotIn('mood_examples', context)
                self.assertEqual((set(ordinary) - {'source_ids', 'sources', 'images_order'})
                                 | {'parent_pull', 'decision_context'}, set(context))
                self.assertLessEqual(request_text_bytes(pack.call_args.kwargs['body_out'][0]), limit)
                self.assertEqual([], pipeline.github.calls)

    def _run_protected_context_budget(self, limit, chars, *, image, key, pull):
        from tg_triage.document_reader import request_fits

        sent_packets = []
        rejected = []

        def model(link, messages, **options):
            content = messages[1]['content']
            if isinstance(content, list):
                content = content[0]['text']
            sent_packets.append(content)
            packet = json.loads(content.split('UNTRUSTED BATCH DATA:\n', 1)[1])
            return reply(7134, message_ids=[row['message_id'] for row in packet['sources']], rule='R7')

        def download(chat_id, mid, directory):
            path = directory / 'menu.png'
            path.write_bytes(PNG)
            return path

        pipeline = self.pipeline(Mock(side_effect=model), downloader=download if image else None,
                                 uploader=FakeUploader() if image else None)
        pipeline.classifier.request_budget_bytes = limit
        now = self.clock()
        if key == 'group_mood':
            for mid, age, sender, text in ((7100, 5, 8, 'hahaha 😂😂'), (7101, 4, 9, 'chet cuoi 🤣')):
                source = message(mid, text, sender_id=sender, mentions_listener=False,
                                 received_at=now - timedelta(minutes=age))
                pipeline.store.enqueue(source)
                pipeline.store.finish(source, Decision('chat', 'rule', reason='R7'))
        else:
            target = message(7050, 'don A12 in hoa don bi loi khi in 2 lien ' * 8, sender_id=3,
                             received_at=now - timedelta(hours=30), mentions_listener=False)
            pipeline.store.enqueue(target)
            pipeline.store.finish(target, Decision('chat', 'rule', reason='R7'))

        text = ('Mai xem chinh lai icon menu o thanh dieu huong trang chu giup anh, hien dang dung unicode icon '
                'nen tren Android hien thi lech, khac font voi iOS; doi sang bo svg trong figma ban moi nhat. ')
        DecideIntakeTests.seed_parent(pipeline, message_id=7132, open_topic=True, text=(text * 200)[:chars] + ' @vomaingoc',
                                      received_at=now - timedelta(seconds=97))
        parent = message(7133, '' if image else 'anh chup thanh menu', sender_id=1, has_image=image,
                         received_at=now - timedelta(seconds=70), mentions_listener=False)
        pipeline.store.enqueue(parent)
        pipeline.store.finish(parent, Decision('chat', 'rule', reason='F2'))
        self.assertIsNotNone(pipeline.rules_pending.add_source(f'{CHAT}:7132', CHAT, 7133))
        pipeline.rules_pending.close(f'{CHAT}:7132', 'CLOSED')
        c = replace(self.icon_reply(), text='a nay la phia nguoi chua dang nhap, trang khach van con icon cu',
                    received_at=now - timedelta(seconds=24))
        d = message(7135, 'sua cho dong bo di Mai cung y nghia', sender_id=1, mentions_listener=False,
                    received_at=now, reply_to_id=7050 if key != 'group_mood' else None)
        for source in (c, d):
            pipeline.store.enqueue(source)

        fit = rules_intake._fit_pulled_batch_context

        def inspect(provider, snapshot, body, packet, sources, context, **options):
            fitted = fit(provider, snapshot, body, packet, sources, context, **options)
            rejected.append(not fitted)
            self.assertFalse(fitted)
            # With only the protected evidence removed, the same pulled packet
            # WOULD fit. This distinguishes the fallback from an already oversize batch.
            user = body['messages'][1]
            without_evidence = json.loads(user['content'])
            if key == 'group_mood':
                self.assertTrue(without_evidence.pop(key)['joking'])
            else:
                self.assertEqual([7050], [row['message_id'] for row in without_evidence[key]])
                without_evidence[key] = []
            smaller = dict(body, messages=[body['messages'][0], dict(user, content=type(user['content'])(
                json.dumps(without_evidence, ensure_ascii=False, separators=(',', ':'))))])
            self.assertTrue(request_fits(smaller, limit))
            return fitted

        with patch.object(rules_intake, '_fit_pulled_batch_context', side_effect=inspect):
            result = rules_intake._process_batch(pipeline, [c, d], allow_parent_pull=pull)
        self.assertEqual(('chat', 'R7'), (result.decision, result.reason))
        self.assertEqual([True] if pull else [], rejected)
        self.assertEqual([], pipeline.github.calls)
        self.assertEqual('F2', pipeline.store.get(CHAT, 7133)['reason'])
        self.assertEqual(1, len(sent_packets))
        content = sent_packets[0]
        packet = json.loads(content.split('UNTRUSTED BATCH DATA:\n', 1)[1])
        self.assertEqual([7134, 7135], [row['message_id'] for row in packet['sources']])
        context = packet['sources'][0]['batch_context']
        self.assertNotIn('parent_pull', context)
        self.assertIn(key, context)
        if key == 'read_only_context':
            self.assertIn(7050, [row['message_id'] for row in context[key]])
        return content

    def test_pulled_batch_reruns_to_keep_group_mood_at_both_budgets(self):
        for limit, chars, image in ((8000, 600, True), (8000, 600, False), (24000, 16600, False)):
            with self.subTest(limit=limit, image=image):
                kwargs = dict(image=image, key='group_mood')
                ordinary = self._run_protected_context_budget(limit, chars, pull=False, **kwargs)
                self.assertEqual(ordinary, self._run_protected_context_budget(limit, chars, pull=True, **kwargs))

    def test_pulled_batch_reruns_to_keep_other_originals_reply_target(self):
        for limit, chars in ((8000, 600), (24000, 16600)):
            with self.subTest(limit=limit):
                kwargs = dict(image=False, key='read_only_context')
                ordinary = self._run_protected_context_budget(limit, chars, pull=False, **kwargs)
                self.assertEqual(ordinary, self._run_protected_context_budget(limit, chars, pull=True, **kwargs))

    def test_parent_pull_live_shape_uploads_image_and_survives_restart_after_save(self):
        def download(chat_id, mid, directory):
            self.assertEqual((CHAT, 7133), (chat_id, mid))
            path = directory / 'menu.png'
            path.write_bytes(PNG)
            return path

        picked = dict(title='Đổi icon menu', fields={
            'object': dict(value='menu', source_ids=[7132]),
            'work': dict(value='đổi icon', source_ids=[7132, 7135]),
        })
        chat = Mock(side_effect=[
            reply(7132, message_ids=[7132, 7133], rule='F2'),
            reply(7134, message_ids=[7132, 7133, 7134, 7135], state='READY', rule='R10',
                  template='task', issue=picked),
        ])
        uploader = FakeUploader()
        pipeline = self.pipeline(chat, downloader=download, uploader=uploader)
        now = self.clock()
        review = message(7131, PR_REVIEW, sender_id=2, mentions_listener=False, received_at=now)
        old = [message(7132, 'Đổi icon menu @devmember', sender_id=1, reply_to_id=7131,
                       mentions_listener=False, received_at=now + timedelta(seconds=2)),
               message(7133, '', sender_id=1, has_image=True,
                       mentions_listener=False, received_at=now + timedelta(seconds=29))]
        sources = [message(7134, 'Đây là menu của trang khách', sender_id=2, reply_to_id=7133,
                           mentions_listener=False, received_at=now + timedelta(seconds=75)),
                   message(7135, 'Đổi icon cho đồng bộ', sender_id=1,
                           mentions_listener=False, received_at=now + timedelta(seconds=99))]

        self.assertEqual('F1', pipeline.process(review).reason)
        untouched = dict(pipeline.store.get(CHAT, 7131))
        for source in old:
            pipeline.store.enqueue(source)
        self.assertEqual('F2', rules_intake._process_batch(pipeline, old).reason)
        old_topic, old_batch = f'{CHAT}:7132', f'batch:{CHAT}:7132'
        old_snapshot = pipeline.rules_pending.topic_snapshot(old_topic)
        old_checkpoint = pipeline.rules_pending.batch(old_batch)
        original_rows = [dict(pipeline.store.get(CHAT, mid)) for mid in (7132, 7133)]

        save = pipeline.rules_pending.save_batch
        def crash_after_save(*args):
            self.assertTrue(save(*args))
            raise SystemExit

        for source in sources:
            pipeline.store.enqueue(source)
        with patch.object(pipeline.rules_pending, 'save_batch', side_effect=crash_after_save):
            with self.assertRaises(SystemExit):
                rules_intake._process_batch(pipeline, sources)
        self.assertEqual(original_rows, [dict(pipeline.store.get(CHAT, mid)) for mid in (7132, 7133)])
        self.assertEqual([], pipeline.github.calls)
        saved_id = f'batch:{CHAT}:7134'
        (_, plan, status), = pipeline.rules_pending.batch(saved_id)[1]
        self.assertEqual(('PLANNED', [[CHAT, 7134], [CHAT, 7135]], [[CHAT, 7132], [CHAT, 7133]]),
                         (status, plan['source_keys'], plan['pulled_keys']))
        content = chat.call_args.args[1][1]['content']
        packet = json.loads(content[0]['text'].split('UNTRUSTED BATCH DATA:\n', 1)[1])
        self.assertEqual([7132, 7133, 7134, 7135], [row['message_id'] for row in packet['sources']])
        self.assertEqual({'7134': [7132, 7133]}, packet['sources'][0]['batch_context']['parent_pull'])
        self.assertTrue(packet['sources'][1]['pixels_available'])

        restarted = Store(pipeline.config.db_path, clock=self.clock)
        self.addCleanup(restarted.close)
        recovered = Pipeline(pipeline.config, restarted, pipeline.classifier, pipeline.github,
                             rules_snapshot=pipeline.rules_snapshot, assistant_pack=_load_assistant_pack(),
                             downloader=download, uploader=uploader, notice_sender=self.sent,
                             reactor=pipeline.reactor)
        recovered.assistant_runtime.chat = chat
        result, = rules_intake.sweep(recovered)
        self.assertEqual((42, ((CHAT, 7134),), 1), (result.issue_number, result.reaction_targets, result.images))
        self.assertEqual(2, chat.call_count)
        self.assertEqual(1, len(recovered.github.calls))
        self.assertEqual(1, len(uploader.uploads))
        body = recovered.github.calls[0].body.replace('\u200b', '')
        self.assertIn(f'<!-- tg:{CHAT}:7134 -->', body)
        self.assertIn(uploader.uploads[0]['asset'], body)
        self.assertIn(old[0].text, body)
        self.assertIn(sources[0].text, body)
        self.assertIn(sources[1].text, body)
        self.assertNotIn(review.text, body)
        self.assertEqual([7134, 7132, 7133, 7135],
                         [mid for _, mid in recovered.rules_pending.sources(f'{CHAT}:7134').values()])
        self.assertEqual(old_snapshot, recovered.rules_pending.topic_snapshot(old_topic))
        self.assertEqual(old_checkpoint, recovered.rules_pending.batch(old_batch))
        self.assertEqual(untouched, dict(restarted.get(CHAT, 7131)))
        self.assertEqual([42] * 4, [restarted.get(CHAT, mid)['issue_number'] for mid in range(7132, 7136)])
        self.assertEqual([], rules_intake.sweep(recovered))

    @staticmethod
    def search(mid, number, confidence, **fields):
        tool = ToolCall('search', 'search_tickets', json.dumps(dict(repo='sandbox/triage', query='POS')))
        return [reply(mid, calls=(tool,)),
                reply(mid, state='APPEND', rule='R6', target=dict(number=number, basis='search'),
                      relation='duplicate', confidence=confidence, **fields)]

    def test_search_duplicate_below_threshold_creates_related_ticket(self):
        chat = Mock(side_effect=self.search(1, 17, .6, template='bug', issue=issue(
            'object', 'observed', source_ids=(1,))))
        pipeline = self.pipeline(chat)
        with patch('tg_triage.assistant_github.GitHubReader.search_tickets',
                   return_value=[dict(repo='sandbox/triage', number=17, state='open')]):
            self.assertEqual(pipeline.process(message(1, 'POS kẹt giấy')).decision, 'issue')
        ticket, = pipeline.github.calls
        self.assertIn('Related to #17', ticket.body)
        pipeline.github.add_comment.assert_not_called()
        row, = self.rows(pipeline)
        group, = json.loads(row['outcomes_json'])
        self.assertEqual((row['phase'], row['final_state'], row['decision_rule'], row['error_class']),
                         (PHASE_DECIDE, 'READY', 'R9', None))
        self.assertEqual(group['related'], [17])
        self.assertEqual([v['guard'] for v in group['violations']], ['guard_target'])
        self.assertEqual(chat.call_count, 2)

    def test_pipeline_core_keeps_snapshot_rule_validation_and_audit_text(self):
        chat = Mock(return_value=reply(1, state='READY', rule='R9', template='bug',
                                       issue=issue('object', 'observed', source_ids=(1,))))
        pipeline = self.pipeline(chat, pack_mode='core')
        self.assertEqual(pipeline.process(message(1, 'POS kẹt giấy')).decision, 'issue')
        self.assertIn('RULE INDEX', chat.call_args.args[1][0]['content'])
        row, = self.rows(pipeline)
        outcome, = json.loads(row['outcomes_json'])
        self.assertEqual((outcome['rule'], row['rules_fingerprint']), ('R9', pipeline.rules_snapshot.fingerprint))
        self.assertTrue(outcome['rule_text'].startswith('| R9 |'))

    def test_core_x14_holds_when_router_actually_fails(self):
        chat = Mock(side_effect=RouterError('transient'))
        pipeline = self.pipeline(chat, pack_mode='core')
        pipeline.process(message(1, 'Màn thanh toán bị treo'))
        row, = self.rows(pipeline)
        self.assertEqual(row['final_state'], 'HOLD')
        self.assertEqual(pipeline.github.calls, [])
        self.assertGreater(chat.call_count, 0)

    def test_reply_after_30_minutes_and_search_append_join_real_thread(self):
        chat = Mock(side_effect=[
            reply(1, state='READY', rule='R9', template='bug',
                  issue=issue('object', 'observed', source_ids=(1,))),
            reply(22, state='APPEND', rule='R6', target=dict(number=42, basis='reply')),
            *self.search(23, 42, .9),
        ])
        pipeline = self.pipeline(chat)
        self.assertEqual(pipeline.process(message(1, 'POS kẹt giấy')).issue_number, 42)
        first, = self.rows(pipeline)
        root = first['run_id']
        self.assertEqual(first['ticket_ref'], 'https://github.com/sandbox/triage/issues/42')
        self.assertEqual(classification_log.find_thread(pipeline.store, CHAT, ticket_ref=42), root)
        self.clock.advance(30)
        self.assertEqual(pipeline.process(message(
            22, 'Vẫn bị kẹt', reply_to_id=1, received_at=self.clock())).decision, 'followup')
        self.assertEqual([row['log_thread_id'] for row in self.rows(pipeline)], [root, root])
        # This round has no reply proof or matching topic. It joins only when
        # set_thread runs after the verified comment using the stored issue URL.
        with patch('tg_triage.assistant_github.GitHubReader.search_tickets',
                   return_value=[dict(repo='sandbox/triage', number=42, state='open')]):
            self.assertEqual(pipeline.process(message(
                23, 'POS lại kẹt giấy', received_at=self.clock())).decision, 'followup')
        self.assertEqual([row['log_thread_id'] for row in self.rows(pipeline)], [root] * 3)
        self.assertEqual(pipeline.github.add_comment.call_count, 2)
        self.assertEqual(len(pipeline.github.calls), 1)

    def test_unproven_search_holds_with_real_guard_name(self):
        chat = Mock(return_value=reply(1, state='APPEND', rule='R6', relation='duplicate',
                                       target=dict(number=17, basis='search'), confidence=.6))
        pipeline = self.pipeline(chat)
        result = pipeline.process(message(1, 'POS kẹt giấy'))
        row, = self.rows(pipeline)
        outcomes = json.loads(row['outcomes_json'])
        self.assertEqual((result.decision, result.reason), ('pending_review', 'guard_target'))
        self.assertEqual((row['final_state'], row['decision_rule']), ('HOLD', 'guard_target'))
        self.assertEqual(outcomes[0]['violations'][0]['guard'], 'guard_target')
        self.assertEqual(chat.call_count, 4)
        self.assertEqual(pipeline.github.calls, [])
        pipeline.github.add_comment.assert_not_called()
        self.sent.assert_not_called()

    def test_phase_one_provenance_holds_whole_batch_including_ready_sibling(self):
        for state in ('ANALYZE', 'RELATED', 'LIFECYCLE_QUESTION', 'APPEND_CLOSED'):
            with self.subTest(state=state):
                blocked = reply(1, state=state)
                groups = json.loads(blocked.content)
                ready = reply(2, state='READY', rule='R9', template='bug',
                              issue=issue('object', 'observed', source_ids=(2,)))
                groups['groups'].extend(json.loads(ready.content)['groups'])
                chat = Mock(return_value=replace(blocked, content=json.dumps(groups)))
                pipeline = self.pipeline(chat)
                sources = [message(1, 'co ai xem giup cai nay khong'), message(2, 'POS kẹt giấy')]
                for source in sources:
                    pipeline.store.enqueue(source)
                result = rules_intake._process_batch(pipeline, sources)
                row, = self.rows(pipeline)
                self.assertEqual((result.reason, row['final_state'], row['decision_rule']),
                                 ('guard_provenance', 'HOLD', 'guard_provenance'))
                self.assertEqual(json.loads(row['outcomes_json'])[0]['violations'][0]['guard'],
                                 'guard_provenance')
                self.assertEqual([g[2] for g in pipeline.rules_pending.batch(f'batch:{CHAT}:1')[1]], ['DONE'])
                self.assertTrue(all(pipeline.store.get(CHAT, source.message_id)['reason'] == 'guard_provenance'
                                    for source in sources))
                chat.assert_called_once()
                self.assertEqual(pipeline.github.calls, [])
                pipeline.github.add_comment.assert_not_called()
                self.sent.assert_not_called()

    def test_search_downgrade_cannot_append_to_closed_reply(self):
        ready = reply(1, state='READY', rule='R9', template='bug',
                      issue=issue('object', 'observed', source_ids=(1,)))
        chat = Mock(side_effect=[ready, *self.search(22, 17, .6)])
        pipeline = self.pipeline(chat)
        self.assertEqual(pipeline.process(message(1, 'POS kẹt giấy')).issue_number, 42)
        self.sent.reset_mock()
        pipeline.github.lifecycle_snapshot.side_effect = None
        pipeline.github.lifecycle_snapshot.return_value = dict(
            number=42, repo=pipeline.config.gh_repo, state='closed',
            updated_at='2026-11-19T09:00:00Z', observed_at='2026-11-19T09:01:00Z')
        with patch('tg_triage.assistant_github.GitHubReader.search_tickets',
                   return_value=[dict(repo=pipeline.config.gh_repo, number=17, state='open')]):
            result = pipeline.process(message(22, 'Vẫn bị kẹt', reply_to_id=1))
        row = self.rows(pipeline)[-1]
        self.assertEqual((result.reason, row['final_state']), ('guard_provenance', 'HOLD'))
        self.assertEqual(len(pipeline.github.calls), 1)
        pipeline.github.add_comment.assert_not_called()
        self.sent.assert_not_called()

    def test_status_sends_one_ops_notice_and_completes_plan(self):
        chat = Mock(return_value=reply(1, state='STATUS'))
        pipeline = self.pipeline(chat)
        source = message(1, 'Cho em biết tình trạng của ticket POS')
        result = pipeline.process(source)
        self.assertEqual(result.reason, 'semantic_status')
        batch_id = f'batch:{CHAT}:1'
        self.assertEqual(pipeline.rules_pending.batch(batch_id)[1][0][2], 'DONE')
        rules_intake._process_batch(pipeline, [source], batch_id)
        self.sent.assert_called_once()
        self.assertEqual(self.sent.call_args.args[:2], (OPS, None))
        self.assertEqual(pipeline.github.calls, [])
        pipeline.github.add_comment.assert_not_called()

    def test_nonretryable_saved_plan_rejection_records_hold_and_notifies_ops_once(self):
        chat = Mock(return_value=reply(1, state='STATUS', message_ids=[1, 2]))
        pipeline = self.pipeline(chat)
        sources = [message(1, 'Trạng thái ticket POS'), message(2, 'Cần biết tiến độ')]
        for source in sources:
            pipeline.store.enqueue(source)
        result = rules_intake._process_batch(pipeline, sources)
        batch_id = f'batch:{CHAT}:1'
        self.assertEqual(result.reason, 'guard_rules_authority_changed')
        self.assertEqual(pipeline.rules_pending.batch(batch_id)[1][0][2], 'BLOCKED')
        self.assertEqual(pipeline.rules_pending.topic_snapshot(batch_id)['status'], 'ESCALATED')
        row, = self.rows(pipeline)
        self.assertEqual((row['final_state'], row['decision_rule']), ('HOLD', result.reason))
        rules_intake._process_batch(pipeline, sources, batch_id)
        self.sent.assert_called_once()
        self.assertEqual(self.sent.call_args.args[:2], (OPS, None))
        self.assertEqual(pipeline.github.calls, [])

    def test_done_turn_with_failed_plan_save_escalates_without_another_model_call(self):
        for error in (None, RuntimeError('save interrupted')):
            with self.subTest(error=error):
                chat = Mock(return_value=reply(1))
                pipeline = self.pipeline(chat)
                source = message(1, 'POS kẹt giấy')
                with patch.object(pipeline.rules_pending, 'save_batch', return_value=False, side_effect=error):
                    if error:
                        with self.assertRaisesRegex(RuntimeError, 'save interrupted'):
                            pipeline.process(source)
                    else:
                        self.assertEqual(pipeline.process(source).reason, 'rules_pending_changed')
                for _ in range(pipeline.rules_snapshot.limits['hold_max_retries']):
                    results = rules_intake.sweep(pipeline)
                    if results and results[-1].reason == 'rules_hold_escalated':
                        break
                self.assertEqual(results[-1].reason, 'rules_hold_escalated')
                self.assertEqual(pipeline.rules_pending.topic_snapshot(f'batch:{CHAT}:1')['status'], 'ESCALATED')
                chat.assert_called_once()
                self.sent.assert_called_once()
                self.assertEqual(len(self.rows(pipeline)), 1)

    def test_unproven_refs_do_not_reach_ticket_body_and_are_audited(self):
        chat = Mock(return_value=reply(
            1, state='READY', rule='R9', template='bug',
            issue=issue('object', 'observed', source_ids=(1,)),
            refs=[{'number': n} for n in (1, 2, 3)]))
        pipeline = self.pipeline(chat)
        self.assertEqual(pipeline.process(message(1, 'POS kẹt giấy #1 #2 #3')).decision, 'issue')
        ticket, = pipeline.github.calls
        self.assertNotIn('Related to #', ticket.body)
        row, = self.rows(pipeline)
        group, = json.loads(row['outcomes_json'])
        self.assertEqual(group['related'], [])
        self.assertEqual(group['violations'], [
            dict(guard='guard_target', detail='dropped 3 unproven ticket reference(s)')])

    def test_need_info_without_question_sends_default_for_direct_and_downgraded_groups(self):
        for search in (False, True):
            with self.subTest(search=search):
                chat = (Mock(side_effect=self.search(1, 17, .6)) if search else
                        Mock(return_value=reply(1, state='NEED_INFO', rule='R13')))
                pipeline = self.pipeline(chat)
                with patch('tg_triage.assistant_github.GitHubReader.search_tickets',
                           return_value=[dict(repo=pipeline.config.gh_repo, number=17, state='open')]):
                    self.assertEqual(pipeline.process(message(1, 'POS kẹt giấy')).decision, 'pending_review')
                self.sent.assert_called_once()
                self.assertEqual(self.sent.call_args.args[:2], (OPS, None))
                self.assertIn(active().render('group-replies.md', 'question.fallback'), self.sent.call_args.args[2])
                row, = self.rows(pipeline)
                self.assertEqual(json.loads(row['outcomes_json'])[0]['question_block'], 'question.fallback')

    def test_rejected_packet_logs_one_standalone_decide_row(self):
        for reason in ('too_large', 'invalid_packet'):
            with self.subTest(reason=reason):
                chat = Mock()
                pipeline = self.pipeline(chat)
                source = message(1, 'POS kẹt giấy')
                if reason == 'too_large':
                    pipeline.classifier.request_budget_bytes = 1
                    pipeline.process(source)
                else:
                    with patch.object(pipeline.store, 'autonomous_read_context', return_value=(source,)):
                        pipeline.process(source)
                row, = self.rows(pipeline)
                self.assertEqual((row['phase'], row['final_state'], row['error_class'], row['model_calls']),
                                 (PHASE_DECIDE, 'HOLD', reason, 0))
                self.assertEqual(row['log_thread_id'], row['run_id'])
                chat.assert_not_called()

    def test_intake_passes_images_documents_and_read_context_to_both_links(self):
        def download(chat_id, mid, directory):
            path = directory / ('pos.png' if mid == 2 else 'pos.txt')
            path.write_bytes(PNG if mid == 2 else 'Papel de prueba'.encode())
            return path

        chat = Mock(side_effect=[RouterError('transient'), reply(2, message_ids=[2, 3])])
        pipeline = self.pipeline(chat, downloader=download)
        pipeline.process(message(1, 'https://example.com'))
        sources = [message(2, 'POS kẹt giấy trong ảnh', has_image=True),
                   message(3, 'Nhờ xem tài liệu', has_document=True,
                           document_name='pos.txt', document_mime='text/plain')]
        for source in sources:
            pipeline.store.enqueue(source)
        self.assertEqual(rules_intake._process_batch(pipeline, sources).decision, 'chat')
        self.assertEqual(chat.call_count, 2)
        for call in chat.call_args_list:
            content = call.args[1][1]['content']
            payload = json.loads(content[0]['text'].split('UNTRUSTED BATCH DATA:\n', 1)[1])
            self.assertTrue(payload['sources'][0]['pixels_available'])
            context = payload['sources'][0]['batch_context']
            self.assertEqual(context['DOCUMENT_CONTENTS'][0]['text'], 'Papel de prueba')
            self.assertEqual(context['read_only_context'][0]['text'], 'https://example.com')
            self.assertTrue(content[-1]['image_url']['url'].startswith('data:image/png;base64,'))
