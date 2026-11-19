import json
import tempfile
import unittest
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tg_triage import classification_log, rules_intake
from tg_triage.__main__ import _load_assistant_pack, _load_decision_rules
from tg_triage.assistant_contract import GroupDecision, PHASE_DECIDE
from tg_triage.assistant_extractor import batch_fingerprint
from tg_triage.decision_rules import RulesSnapshot
from tg_triage.pipeline import Pipeline
from tg_triage.router_transport import RouterError, ToolCall
from tg_triage.store import Store
from tg_triage.ticket_templates import active
from tests.helpers import PNG, Clock, FakeGitHub, config, message
from tests.test_assistant_contract import issue
from tests.test_assistant_decide_turn import reply


ROOT = Path(__file__).resolve().parents[1]
OPS = -1009876543210
CHAT = message().chat_id
CHECKS = ('F1', 'F3', 'F4', 'F6', 'F5', 'R1', 'R2', 'R3')


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
            code_precheck=None, log_thread_id=None):
        self.legacy_calls.append((phase, code_precheck))
        if code_precheck is None:
            with self.legacy_run(snapshot, sources, images, phase, topic_id=topic_id) as attempt:
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
            groups = [candidate(job) if candidate else self.group(job)] if outcome == 'done' else None
            run_id = audit.write(job['snapshot'], job['sources'], job['topic_id'], PHASE_DECIDE,
                                 trace=job['code_precheck'], thread=job['log_thread_id'],
                                 state=groups[0].state if groups else 'HOLD',
                                 rule=groups[0].rule if groups else 'guard_grounding' if outcome == 'guard' else None)
            return groups, run_id, outcome

        runtime = SimpleNamespace(decide=decide, publish=lambda: None, settings=settings.assistant,
                                  waiting=lambda sources, *, admit: ('NEW', True))
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
    def pipeline(self, chat, *, downloader=None, pack_mode='full'):
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
                            notice_sender=self.sent, reactor=Mock(return_value=True))
        pipeline.assistant_runtime.chat = chat
        return pipeline

    @staticmethod
    def rows(pipeline):
        return pipeline.store.connection.execute('SELECT * FROM classification_runs ORDER BY run_id').fetchall()

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
