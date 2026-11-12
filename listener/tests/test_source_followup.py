"""Offline source_followup acceptance tests using real messages/store and fake IO."""
import json
import unittest
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tg_triage import rules_intake
from tg_triage.decision_rules import RulesSnapshot
from tg_triage.pipeline import Pipeline
from tg_triage.rules_pending_store import check_connection_before_recovery
from tg_triage.store import Store
from tests.helpers import Clock, FakeDownloader, FakeGitHub, FakeUploader, JPG, config, message


ROOT = Path(__file__).resolve().parents[1]


class FakeRulesProvider:
    """Same _ask/parse contract used by test_rules_intake; no network."""
    settings = SimpleNamespace(model='test')
    think = False
    supports_vision = False

    def __init__(self):
        self.responses = []
        self.calls = []
        self.packets = []
        self.pixel_counts = []
        self.before_answer = None

    def _ask(self, body, parse, attempts):
        if 'reviews' in body.get('format', {}).get('properties', {}):
            from tests.test_rules_extensions import fixture_analysis_review
            proposals = json.loads(body['messages'][0]['content'].split('Proposals:\n', 1)[1])
            return parse(fixture_analysis_review(body) if all(p.get('task') for p in proposals) else '{"reviews":[]}')
        packet = json.loads(body['messages'][1]['content'])
        self.packets.append(packet)
        self.pixel_counts.append(len(body['messages'][1].get('images', [])))
        ids = packet['source_ids']
        self.calls.append(tuple(ids))
        if self.before_answer is not None:
            self.before_answer(ids)
        response = self.responses.pop(0)
        if isinstance(response, BaseException): raise response
        if response is None: return None
        if response == 'split':
            groups = [{'message_ids': [i], 'facts': {}} for i in ids]
        else:
            intent = 'task' if response == 'team_task' else 'analyze' if response == 'analyze_task' else 'bug' if response in ('bug_object', 'bug_missing') else response
            facts = {'intent': {'value': intent, 'source_ids': [ids[0]]}}
            if response == 'team_task':
                facts['work'] = dict(value='check log dang nhap',source_ids=[ids[-1]])
            if response == 'lifecycle':
                facts['intent']['value'] = 'unknown'
                facts['lifecycle'] = dict(kind='unclear',knownworking_source_ids=[],recurrence_source_ids=[])
            if response == 'analyze_task':
                facts['task'] = dict(title='Check display',work='Inspect clipping',acceptance=['Name clipping'])
            if response == 'bug_object':
                facts['object'] = {'value': 'POS', 'source_ids': [ids[-1]]}
            if response == 'bug':
                facts.update(object={'value': 'POS', 'source_ids': [ids[-1]]},
                             observed={'value': 'ket giay', 'source_ids': [ids[-1]]})
            groups = [{'message_ids': ids, 'facts': facts}]
        return parse(json.dumps({'groups': groups}))


class SourceFollowupTests(unittest.TestCase):
    def make_pipeline(self, *, extensions=False, mode='live', path=':memory:', vision=False, files=None, attachments=None):
        clock = Clock()
        store = Store(path, clock=clock)
        self.addCleanup(store.close)
        provider = FakeRulesProvider()
        provider.supports_vision = vision
        settings = replace(config(attachments=attachments), mode=mode, autonomous_enabled=True, decision_rules_enabled=True,
                           telegram=replace(config().telegram, ops_chat_id=-1009876543210))
        snapshot = RulesSnapshot(ROOT / 'rules', ROOT / 'templates/github-ticket', extensions=extensions)
        sent = []
        github = FakeGitHub()
        notice_base = max(1000, store.connection.execute('SELECT COALESCE(MAX(notice_id),0) FROM rules_notices').fetchone()[0]) if store.connection.execute("SELECT 1 FROM sqlite_master WHERE name='rules_notices'").fetchone() else 1000
        uploader = FakeUploader()
        pipeline = Pipeline(settings, store, provider, github, rules_snapshot=snapshot,
                            downloader=FakeDownloader(files or {}), uploader=uploader,
                            notice_sender=lambda *args: sent.append(args) or notice_base + len(sent))
        return SimpleNamespace(pipeline=pipeline, store=store, provider=provider, clock=clock,
                               sent=sent, github=github, pending=pipeline.rules_pending, uploader=uploader)

    def source(self, env, mid=42, **extra):
        fields = dict(sender_id=10, mentions_listener=False, text='Nho xem giup cho nay',
                      received_at=env.clock())
        fields.update(extra)
        return message(mid, **fields)

    def seed(self, env, mid=42, *, response='unknown', **extra):
        source = self.source(env, mid, **extra)
        env.provider.responses.append(response)
        self.assertEqual(env.pipeline.process(source).decision, 'pending_review')
        topic = f'{source.chat_id}:{source.message_id}'
        self.assertEqual(env.pending.topic_snapshot(topic)['status'], 'OPEN')
        return source, topic

    def old_bytes(self, env, topic):
        """Snapshot plus every old-topic durable row, including attempts/notices."""
        state = {'snapshot': env.pending.topic_snapshot(topic)}
        for table in ('rules_topics', 'rules_sources', 'rules_decisions', 'rules_notices', 'rules_inflight_topics'):
            state[table] = [tuple(row) for row in env.store.connection.execute(
                f'SELECT * FROM {table} WHERE topic_id=? ORDER BY rowid', (topic,))]
        return json.dumps(state, sort_keys=True, separators=(',', ':')).encode('utf8')

    def assert_new(self, env, topic, incoming, response='unknown'):
        before = self.old_bytes(env, topic)
        env.provider.responses.append(response)
        result = env.pipeline.process(incoming)
        self.assertEqual(self.old_bytes(env, topic), before)
        self.assertEqual(env.pending.sources(f'{incoming.chat_id}:{incoming.message_id}'),
                         {1: (incoming.chat_id, incoming.message_id)})
        self.assertIsNotNone(result)
        return result

    def test_link_ready_creates_exactly_one_ticket_with_both_messages(self):
        for extensions in (False, True):
            with self.subTest(extensions=extensions):
                env = self.make_pipeline(extensions=extensions)
                source, topic = self.seed(env)
                env.clock.advance(5)
                env.provider.responses.append('bug')
                result = env.pipeline.process(self.source(env, 43, text='POS ket giay'))
                self.assertEqual((result.decision, result.issue_number), ('issue', 42))
                self.assertEqual(len(env.github.calls), 1)
                self.assertIn('/42', env.github.calls[0].body)
                self.assertIn('/43', env.github.calls[0].body)
                self.assertIn('Nho xem giup cho nay', env.github.calls[0].body)
                self.assertIn('POS ket giay', env.github.calls[0].body)
                self.assertEqual(env.pending.sources(topic), {1: (source.chat_id, 42), 2: (source.chat_id, 43)})
                self.assertEqual(env.pending.topic_snapshot(topic)['status'], 'CLOSED')
                self.assertEqual(env.provider.calls, [(42,), (1, 2)])
                for mid in (42, 43):
                    row = env.store.get(source.chat_id, mid)
                    self.assertEqual((row['decision'], row['issue_number']), ('issue', 42))
                    self.assertEqual(row['issue_url'], 'https://github.com/sandbox/triage/issues/42')
                self.assertFalse(env.pending.unresolved_ambiguity())
                check_connection_before_recovery(env.store.connection)

    def test_link_need_info_sends_no_second_fallback(self):
        for extensions in (False, True):
            with self.subTest(extensions=extensions):
                env = self.make_pipeline(extensions=extensions)
                source, topic = self.seed(env)
                self.assertEqual(len(env.sent), 1)
                self.assertIn('Anh/Chị muốn team xử lý gì', env.sent[0][2])
                env.clock.advance(5)
                env.provider.responses.append('unknown')
                result = env.pipeline.process(self.source(env, 43))
                self.assertEqual(result.decision, 'pending_review')
                self.assertEqual(len(env.sent), 1)
                self.assertEqual(env.pending.sources(topic)[2], (source.chat_id, 43))
                self.assertEqual(env.pending.topic_snapshot(topic)['fallback_asked'], 1)
                self.assertEqual(env.github.calls, [])
                check_connection_before_recovery(env.store.connection)

    def test_model_splits_new_topic_old_snapshot_and_attempts_byte_identical(self):
        for extensions in (False, True):
            with self.subTest(extensions=extensions):
                env = self.make_pipeline(extensions=extensions)
                _, topic = self.seed(env)
                env.clock.advance(5)
                env.provider.responses.append('split')
                self.assert_new(env, topic, self.source(env, 43))
                self.assertEqual(env.provider.calls, [(42,), (1, 2), (43,)])
                self.assertEqual(len(env.sent), 2)
                self.assertIn(f'{self.source(env).chat_id}:43', env.sent[-1][2])

    def test_older_than_window_and_exact_boundary_create_new_topic(self):
        for minutes in (30, 31):
            with self.subTest(minutes=minutes):
                env = self.make_pipeline()
                _, topic = self.seed(env)
                env.clock.advance(minutes)
                self.assert_new(env, topic, self.source(env, 43))
                self.assertEqual(env.provider.calls, [(42,), (43,)])

    def test_different_sender_creates_new_topic(self):
        env = self.make_pipeline()
        _, topic = self.seed(env)
        self.assert_new(env, topic, self.source(env, 43, sender_id=11))
        self.assertEqual(env.provider.calls, [(42,), (43,)])

    def test_received_timestamps_control_window_despite_processing_backlog(self):
        from datetime import timedelta
        for gap in (5,120,-5):
            with self.subTest(gap=gap):
                env=self.make_pipeline(extensions=True)
                received=env.clock()-timedelta(hours=3)
                _,topic=self.seed(env,received_at=received)
                before=self.old_bytes(env,topic)
                env.provider.responses.append('unknown')
                env.pipeline.process(self.source(env,43,received_at=received+timedelta(minutes=gap)))
                if gap==5:
                    self.assertEqual(len(env.pending.sources(topic)),2)
                    self.assertEqual(env.provider.calls,[(42,),(1,2)])
                else:
                    self.assertEqual(self.old_bytes(env,topic),before)
                    self.assertEqual(env.provider.calls,[(42,),(43,)])

    def test_team_request_matches_different_sender_without_confirmation(self):
        for extensions in (False,True):
            results=[]
            for sender in (10,11):
                with self.subTest(extensions=extensions,sender=sender):
                    env=self.make_pipeline(extensions=extensions); _,topic=self.seed(env)
                    before=self.old_bytes(env,topic); env.provider.responses.append('team_task')
                    incoming=self.source(env,43,sender_id=sender,text='@tuan_dev check log dang nhap giup anh')
                    with patch.object(env.pending,'topic_for_source_followup',side_effect=AssertionError('team request confirmation')):
                        result=env.pipeline.process(incoming)
                    results.append((result.decision,result.reason,result.issue_number,env.sent[1:]))
                    self.assertEqual((result.decision,result.reason),('chat','F2'))
                    self.assertEqual(self.old_bytes(env,topic),before)
                    self.assertEqual(env.provider.calls,[(42,),(43,)])
                    self.assertEqual(env.github.calls,[])
            self.assertEqual(results[0],results[1])

    def test_two_matching_open_topics_create_new_topic(self):
        env = self.make_pipeline()
        _, first = self.seed(env)
        # A genuine split creates a second matching topic through the normal path.
        env.provider.responses.append('split')
        _, second = self.seed(env, 43)
        before = self.old_bytes(env, second)
        env.provider.calls.clear()
        self.assert_new(env, first, self.source(env, 44))
        self.assertEqual(self.old_bytes(env, second), before)
        self.assertEqual(env.provider.calls, [(44,)])

    def test_reply_uses_existing_path_even_with_other_sender_after_window(self):
        for extensions in (False, True):
            with self.subTest(extensions=extensions):
                env = self.make_pipeline(extensions=extensions)
                _, topic = self.seed(env)
                env.clock.advance(31)
                env.provider.responses.extend(['bug', 'bug'])
                with patch.object(env.pending, 'topic_for_source_followup', side_effect=AssertionError('new hook on Reply')):
                    result = env.pipeline.process(self.source(env, 43, sender_id=11, reply_to_id=42, text='POS ket giay'))
                self.assertEqual(result.decision, 'issue')
                self.assertEqual(len(env.github.calls), 1)
                self.assertEqual(len(env.pending.sources(topic)), 2)

    def test_courtesy_filtered_before_candidate_or_model(self):
        env = self.make_pipeline()
        _, topic = self.seed(env)
        before = self.old_bytes(env, topic)
        with patch.object(env.pending, 'topic_for_source_followup', side_effect=AssertionError('candidate before F4')):
            result = env.pipeline.process(self.source(env, 43, text='ok anh'))
        self.assertEqual((result.decision, result.reason), ('chat', 'F4'))
        self.assertEqual(env.provider.calls, [(42,)])
        self.assertEqual(self.old_bytes(env, topic), before)
        self.assertEqual(len(env.sent), 1)

    def test_unseen_bare_image_does_not_link_or_call_model(self):
        for extensions in (False, True):
            with self.subTest(extensions=extensions):
                env = self.make_pipeline(extensions=extensions)
                _, topic = self.seed(env)
                result = self.assert_new(env,topic,self.source(env,43,text='',has_image=True))
                self.assertEqual((result.decision,result.reason),('pending_review','R3'))
                self.assertEqual(env.provider.calls, [(42,)])
                self.assertEqual(len(env.sent),1)

    def test_dry_run_sends_nothing_for_need_info_or_ready(self):
        for response in ('unknown', 'bug'):
            with self.subTest(response=response):
                env = self.make_pipeline(extensions=True, mode='dry-run')
                _, topic = self.seed(env)
                env.provider.responses.append(response)
                result = env.pipeline.process(self.source(env, 43, text='POS ket giay'))
                self.assertEqual(result.decision, 'dry_run_issue' if response == 'bug' else 'pending_review')
                self.assertEqual(len(env.pending.sources(topic)), 2)
                self.assertEqual(env.sent, [])
                self.assertEqual(env.github.calls, [])

    def test_model_failure_or_exception_leaves_old_topic_unchanged(self):
        for response in (None, RuntimeError('fake provider unavailable')):
            with self.subTest(response=response):
                env = self.make_pipeline()
                _, topic = self.seed(env)
                env.provider.responses.append(response)
                self.assert_new(env, topic, self.source(env, 43))
                self.assertEqual(env.provider.calls, [(42,), (1, 2), (43,)])

    def test_request_too_large_does_not_hold_old_topic(self):
        env = self.make_pipeline()
        _, topic = self.seed(env)
        before = self.old_bytes(env, topic)
        env.provider.request_budget_bytes = 1
        result = env.pipeline.process(self.source(env, 43))
        self.assertEqual(self.old_bytes(env, topic), before)
        self.assertEqual(result.reason, 'rules_request_too_large')
        self.assertIsNotNone(env.pending.topic_snapshot(f'{self.source(env).chat_id}:43'))
        self.assertEqual(env.provider.calls, [(42,)])

    def hold_trace(self, env, topic, result):
        """Compare durable old-path state and effects, excluding only the sender."""
        rows = [tuple(row) for row in env.store.connection.execute(
            'SELECT * FROM rules_topics WHERE topic_id!=? ORDER BY topic_id', (topic,))]
        incoming = env.store.get(self.source(env).chat_id,43)
        return ([(r.decision,r.reason,r.case_key) for r in result], rows,
                tuple(incoming[k] for k in ('decision','reason','case_key','attempts')),
                [n[2] for n in env.sent[1:]], env.pending.hold_topics())

    def compare_hold_sweeps(self, response, *, extensions=True, too_large=False, death=False):
        class Died(BaseException): pass
        traces=[]
        for sender in (10,11):
            temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
            path=Path(temp.name)/'store.db'
            env=self.make_pipeline(extensions=extensions,path=path)
            _,topic=self.seed(env); before=self.old_bytes(env,topic)
            incoming=self.source(env,43,sender_id=sender)
            if death and sender==10:
                env.provider.responses.append(Died())
                with self.assertRaises(Died): env.pipeline.process(incoming)
                check_connection_before_recovery(env.store.connection)
                sent_before=list(env.sent)
                env.store.close()
                env=self.make_pipeline(extensions=extensions,path=path)
                env.sent.extend(sent_before)
                env.provider.responses.append(response)
                first=env.pipeline.close_due_cases()
            else:
                if too_large: env.provider.request_budget_bytes=1
                env.provider.responses.extend([response]*(2 if sender==10 else 1))
                first=[env.pipeline.process(incoming)]
            trace=[self.hold_trace(env,topic,first)]
            self.assertEqual(self.old_bytes(env,topic),before)
            for _ in range(5):
                env.provider.responses[:]=[response]*10
                calls=len(env.provider.calls)
                result=env.pipeline.close_due_cases()
                trace.append((len(env.provider.calls)-calls,self.hold_trace(env,topic,result)))
                self.assertEqual(self.old_bytes(env,topic),before)
                check_connection_before_recovery(env.store.connection)
            traces.append(trace)
        self.assertEqual(traces[0],traces[1])

    def test_provider_down_multi_sweep_matches_different_sender(self):
        for extensions in (False,True):
            with self.subTest(extensions=extensions): self.compare_hold_sweeps(None,extensions=extensions)

    def test_analyze_without_task_multi_sweep_matches_different_sender(self):
        self.compare_hold_sweeps('analyze')

    def test_request_too_large_multi_sweep_matches_different_sender(self):
        for extensions in (False,True):
            with self.subTest(extensions=extensions): self.compare_hold_sweeps(None,extensions=extensions,too_large=True)

    def test_confirmation_death_retry_hold_multi_sweep_matches_different_sender(self):
        for extensions in (False,True):
            with self.subTest(extensions=extensions): self.compare_hold_sweeps(None,extensions=extensions,death=True)

    def test_rejected_confirmation_other_results_match_different_sender(self):
        for extensions in (False,True):
            for response in ('unknown','chat','bug')+(('status','analyze_task') if extensions else ()):
                traces=[]
                for sender in (10,11):
                    with self.subTest(extensions=extensions,response=response,sender=sender):
                        env=self.make_pipeline(extensions=extensions); _,topic=self.seed(env)
                        before=self.old_bytes(env,topic)
                        env.provider.responses.extend((['split'] if sender==10 else [])+[response])
                        result=env.pipeline.process(self.source(env,43,sender_id=sender,text='POS ket giay. Check display. Inspect clipping. Name clipping.'))
                        self.assertEqual(self.old_bytes(env,topic),before)
                        traces.append(self.hold_trace(env,topic,[result]))
                        self.assertEqual(env.pipeline.close_due_cases(),[])
                        check_connection_before_recovery(env.store.connection)
                self.assertEqual(traces[0],traces[1])

    def test_document_is_excluded_before_confirmation(self):
        env = self.make_pipeline(extensions=True, files={43:(b'POS ket giay','proof.txt')})
        _, topic = self.seed(env)
        with patch.object(env.pending,'topic_for_source_followup',side_effect=AssertionError('document candidate')):
            self.assert_new(env,topic,self.source(env,43,has_document=True,document_name='proof.txt',document_mime='text/plain'))
        self.assertEqual(env.provider.calls,[(42,),(43,)])

    def test_candidate_query_excludes_ineligible_topics_and_is_read_only(self):
        for exclusion in ('closed', 'expired', 'legacy', 'hold', 'inflight', 'batch', 'latest_chat', 'other_chat', 'unknown_sender'):
            with self.subTest(exclusion=exclusion):
                env = self.make_pipeline()
                source, topic = self.seed(env)
                with env.store.connection:
                    if exclusion in ('closed', 'expired'):
                        env.pending.close(topic, exclusion.upper())
                    elif exclusion in ('legacy', 'hold'):
                        env.store.connection.execute('UPDATE rules_topics SET phase=? WHERE topic_id=?',
                                                     ('LEGACY_REVIEW' if exclusion == 'legacy' else 'HOLD', topic))
                    elif exclusion == 'inflight':
                        self.assertTrue(env.pending.claim_topic(topic, env.pending.topic_snapshot(topic)))
                    elif exclusion == 'batch':
                        env.pending.create('batch:test', source.chat_id, source.message_id)
                        env.store.connection.execute('UPDATE rules_decisions SET topic_id=? WHERE topic_id=?', ('batch:test', topic))
                    elif exclusion == 'latest_chat':
                        env.pending.record_decision(topic, env.pipeline.rules_snapshot.prefilter_decision())
                before = self.old_bytes(env, topic)
                chat_id = source.chat_id + 1 if exclusion == 'other_chat' else source.chat_id
                sender_id = None if exclusion == 'unknown_sender' else source.sender_id
                self.assertIsNone(env.pending.topic_for_source_followup(chat_id, sender_id, 30))
                self.assertEqual(self.old_bytes(env, topic), before)

    def test_forward_command_and_unknown_sender_skip_candidate(self):
        for extra in ({'is_forward': True}, {'text': '/bug POS ket giay'}, {'sender_id': None}):
            with self.subTest(extra=extra):
                env = self.make_pipeline()
                _, topic = self.seed(env)
                with patch.object(env.pending, 'topic_for_source_followup', side_effect=AssertionError('ineligible source')):
                    self.assert_new(env, topic, self.source(env, 43, **extra), response='bug')

    def test_query_uses_supplied_limit_instead_of_fixed_thirty(self):
        env = self.make_pipeline()
        source, topic = self.seed(env)
        self.assertEqual(env.pipeline.rules_snapshot.limits['followup_window_minutes'], 30)
        env.clock.advance(5)
        self.assertIsNone(env.pending.topic_for_source_followup(source.chat_id, source.sender_id, 4))
        self.assertEqual(env.pending.topic_for_source_followup(source.chat_id, source.sender_id, 6), topic)

    def test_changed_snapshot_or_new_ambiguity_during_model_is_not_overwritten(self):
        for change in ('snapshot', 'second_candidate', 'claim'):
            with self.subTest(change=change):
                env = self.make_pipeline()
                source, topic = self.seed(env)
                def race(ids):
                    if ids != [1, 2]: return
                    if change == 'snapshot':
                        env.pending.reserve_notice(topic, env.pipeline.config.telegram.ops_chat_id, 'question', ('object',))
                    elif change == 'claim':
                        env.pending.claim_topic(topic, env.pending.topic_snapshot(topic))
                    else:
                        other = self.source(env, 99)
                        env.store.enqueue(other)
                        env.pending.create('other', other.chat_id, other.message_id)
                        env.pending.record_decision('other', env.pipeline.rules_snapshot.evaluate({}, rules_intake.code_facts([other])))
                env.provider.before_answer = race
                env.provider.responses.extend(['bug', 'unknown'])
                env.pipeline.process(self.source(env, 43))
                self.assertEqual(env.pending.sources(topic), {1: (source.chat_id, 42)})
                self.assertEqual(env.github.calls, [])
                self.assertEqual(env.provider.calls, [(42,), (1, 2), (43,)])

    def test_linked_chat_closes(self):
        env = self.make_pipeline()
        _, topic = self.seed(env)
        env.provider.responses.append('chat')
        self.assertEqual(env.pipeline.process(self.source(env, 43)).decision, 'chat')
        self.assertEqual(env.pending.topic_snapshot(topic)['status'], 'CLOSED')
        self.assertEqual(len(env.sent), 1)
        check_connection_before_recovery(env.store.connection)

    def test_linked_need_info_only_asks_remaining_fields_then_fallback_once(self):
        env = self.make_pipeline(extensions=True)
        _, topic = self.seed(env, response='bug_object')
        self.assertEqual(json.loads(env.pending.topic_snapshot(topic)['asked']), {'observed': 1})
        env.provider.responses.append('bug_missing')
        env.pipeline.process(self.source(env, 43))
        self.assertEqual(json.loads(env.pending.topic_snapshot(topic)['asked']), {'observed': 1, 'object': 1})
        self.assertEqual(len(env.sent), 2)
        for mid in (44, 45):
            env.provider.responses.append('bug_missing')
            env.pipeline.process(self.source(env, mid))
        self.assertEqual(len(env.sent), 3)
        self.assertEqual(env.pending.topic_snapshot(topic)['fallback_asked'], 1)
        self.assertEqual(len(env.pending.sources(topic)), 4)

    def test_readonly_intents_preserve_old_topic_and_match_different_sender(self):
        for response in ('status','analyze_task','analyze'):
            results=[]; notices=[]
            for sender in (10,11):
                with self.subTest(response=response,sender=sender):
                    env=self.make_pipeline(extensions=True)
                    _,topic=self.seed(env)
                    before=self.old_bytes(env,topic)
                    env.provider.responses.extend([response,response])
                    incoming=self.source(env,43,sender_id=sender,text='Check display. Inspect clipping. Name clipping.')
                    result=env.pipeline.process(incoming)
                    self.assertEqual(self.old_bytes(env,topic),before)
                    self.assertEqual(env.pending.sources(topic),{1:(incoming.chat_id,42)})
                    results.append((result.decision,result.reason))
                    notices.append([n[2] for n in env.sent[1:]])
                    if response != 'analyze':
                        self.assertEqual(result.reason,'semantic_analyze' if response=='analyze_task' else 'semantic_status')
                        self.assertEqual(len(env.sent),2)
                    else:
                        self.assertEqual(result.reason,'rules_hold')
                    self.assertIn((43,),env.provider.calls)
            self.assertEqual(results[0],results[1])
            self.assertEqual(notices[0],notices[1])

    def test_slash_text_that_is_not_command_can_link(self):
        env=self.make_pipeline(); _,topic=self.seed(env)
        env.provider.responses.append('unknown')
        result=env.pipeline.process(self.source(env,43,text='/unknown payload'))
        self.assertEqual(result.decision,'pending_review')
        self.assertEqual(len(env.pending.sources(topic)),2)
        self.assertEqual(env.provider.calls,[(42,),(1,2)])

    def seed_image(self, env, mid=42):
        incoming=self.source(env,mid,text='',has_image=True,image_mime='image/jpeg',image_size=len(JPG))
        result=env.pipeline.process(incoming)
        self.assertEqual((result.decision,result.reason),('pending_review','R3'))
        return incoming,f'{incoming.chat_id}:{mid}'

    def test_image_first_text_second_seen_image_ready_uploads_one_ticket(self):
        for extensions in (False,True):
            with self.subTest(extensions=extensions):
                env=self.make_pipeline(extensions=extensions,vision=True,files={42:(JPG,'proof.jpg')})
                _,topic=self.seed_image(env)
                self.assertEqual(env.provider.calls,[])
                env.provider.responses.append('bug')
                result=env.pipeline.process(self.source(env,43,text='POS ket giay'))
                self.assertEqual(result.decision,'issue')
                self.assertEqual(len(env.pending.sources(topic)),2)
                self.assertEqual(env.provider.pixel_counts,[1])
                self.assertEqual(len(env.github.calls),1)
                self.assertEqual(len(env.uploader.uploads),1)
                self.assertIn('![',env.github.calls[0].body)
                check_connection_before_recovery(env.store.connection)

    def test_image_first_without_vision_text_is_new(self):
        env=self.make_pipeline(); _,topic=self.seed_image(env)
        self.assert_new(env,topic,self.source(env,43))
        self.assertEqual(env.provider.calls,[(43,)])

    def test_text_first_seen_image_is_linked(self):
        env=self.make_pipeline(vision=True,files={43:(JPG,'proof.jpg')})
        _,topic=self.seed(env); env.provider.responses.append('bug')
        result=env.pipeline.process(self.source(env,43,text='',has_image=True,image_size=len(JPG),image_mime='image/jpeg'))
        self.assertEqual(result.decision,'issue')
        self.assertEqual(len(env.pending.sources(topic)),2)
        self.assertEqual(env.provider.pixel_counts[-1],1)
        self.assertEqual(len(env.uploader.uploads),1)

    def test_rejected_or_missing_image_is_not_linked(self):
        from tg_triage.config import AttachmentsConfig
        for files,extra in (({},{}),({43:(b'invalid','proof.jpg')},{}),({43:(JPG,'proof.jpg')},{'image_size':10**9})):
            with self.subTest(files=bool(files),extra=extra):
                env=self.make_pipeline(vision=True,files=files)
                _,topic=self.seed(env)
                self.assert_new(env,topic,self.source(env,43,text='',has_image=True,image_mime='image/jpeg',**extra))
                self.assertEqual(env.provider.calls,[(42,)])

    def test_two_bare_images_then_text_are_confirmed_together(self):
        env=self.make_pipeline(vision=True,files={42:(JPG,'one.jpg'),43:(JPG,'two.jpg')})
        _,topic=self.seed_image(env)
        env.provider.responses.append('unknown')
        result=env.pipeline.process(self.source(env,43,text='',has_image=True,image_size=len(JPG),image_mime='image/jpeg'))
        self.assertEqual(result.reason,'R3')
        self.assertEqual(len(env.pending.sources(topic)),2)
        env.provider.responses.append('bug')
        result=env.pipeline.process(self.source(env,44,text='POS ket giay'))
        self.assertEqual(result.decision,'issue')
        self.assertEqual(env.provider.calls,[(1,2),(1,2,3)])
        self.assertEqual(env.provider.pixel_counts,[2,2])
        self.assertEqual(len(env.github.calls),1)
        self.assertEqual(len(env.uploader.uploads),2)

    def test_need_info_and_media_only_count_together_for_ambiguity(self):
        env=self.make_pipeline(); _,topic=self.seed(env)
        _,image_topic=self.seed_image(env,43)
        before=self.old_bytes(env,image_topic)
        self.assert_new(env,topic,self.source(env,44))
        self.assertEqual(self.old_bytes(env,image_topic),before)
        self.assertEqual(env.provider.calls,[(42,),(44,)])

    def test_no_candidate_bare_image_stays_r3_without_model(self):
        env=self.make_pipeline(vision=True,files={42:(JPG,'proof.jpg')})
        self.seed_image(env)
        self.assertEqual(env.provider.calls,[])

    def test_actual_projection_without_pixels_cannot_confirm(self):
        env=self.make_pipeline(vision=True,files={43:(JPG,'proof.jpg')})
        _,topic=self.seed(env)
        original=env.provider._ask
        def drop_pixels(body,parse,attempts):
            value=original(body,parse,attempts)
            projected=[dict(m) for m in body['messages']]
            projected[1].pop('images',None)
            body['_evidence_view'].update(messages=projected,kind='local')
            return value
        env.provider._ask=drop_pixels
        env.provider.responses.append('bug')
        self.assert_new(env,topic,self.source(env,43,text='',has_image=True))
        self.assertEqual(len(env.github.calls),0)

    def test_confirmation_death_leaves_recoverable_new_hold_old_unchanged(self):
        class Died(BaseException): pass
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        folder=temp.name
        path=Path(folder)/'store.db'
        env=self.make_pipeline(path=path,extensions=True)
        _,topic=self.seed(env); before=self.old_bytes(env,topic)
        env.provider.responses.append(Died())
        with self.assertRaises(Died): env.pipeline.process(self.source(env,43))
        self.assertEqual(self.old_bytes(env,topic),before)
        check_connection_before_recovery(env.store.connection)
        env.store.close()
        restarted=self.make_pipeline(path=path,extensions=True)
        restarted.provider.responses.append('unknown')
        restarted.pipeline.close_due_cases()
        self.assertEqual(self.old_bytes(restarted,topic),before)
        self.assertEqual(restarted.store.connection.execute('SELECT phase FROM rules_topics WHERE topic_id=?',(f'{self.source(restarted).chat_id}:43',)).fetchone()[0],'NEW')
        self.assertEqual(restarted.pipeline.close_due_cases(),[])
        check_connection_before_recovery(restarted.store.connection)

    def test_committed_link_replays_without_model_or_duplicate_effect(self):
        class Died(BaseException): pass
        for response in ('unknown','bug','chat'):
            with self.subTest(response=response):
                temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
                folder=temp.name
                path=Path(folder)/'store.db'
                env=self.make_pipeline(path=path,extensions=True)
                _,topic=self.seed(env); incoming=self.source(env,43,text='POS ket giay')
                env.provider.responses.append(response)
                with patch.object(rules_intake,'_complete_source_followup',side_effect=Died()):
                    with self.assertRaises(Died): env.pipeline.process(incoming)
                self.assertEqual(len(env.pending.sources(topic)),2)
                check_connection_before_recovery(env.store.connection)
                env.store.close()
                resumed=self.make_pipeline(path=path,extensions=True)
                results=resumed.pipeline.close_due_cases()
                self.assertEqual(len(results),1)
                self.assertEqual(resumed.provider.calls,[])
                self.assertEqual(len(resumed.github.calls),int(response=='bug'))
                self.assertEqual(resumed.store.connection.execute('SELECT count(*) FROM rules_inflight_topics').fetchone()[0],0)
                self.assertEqual(resumed.pipeline.close_due_cases(),[])
                with resumed.store.connection:
                    resumed.store.connection.execute("UPDATE messages SET decision='queued',attempts=0 WHERE message_id=43")
                resumed.pipeline.process(incoming)
                self.assertEqual(len(resumed.github.calls),int(response=='bug'))
                self.assertEqual(len(resumed.pending.sources(topic)),2)
                check_connection_before_recovery(resumed.store.connection)

    def test_atomic_link_rolls_back_on_death_before_checkpoint(self):
        class Died(BaseException): pass
        env=self.make_pipeline(); _,topic=self.seed(env)
        before=self.old_bytes(env,topic); env.provider.responses.append('bug')
        original=json.dumps
        def fail_plan(value,*args,**kwargs):
            if isinstance(value,dict) and value.get('topic')==topic and 'sources' in value: raise Died()
            return original(value,*args,**kwargs)
        with patch('tg_triage.rules_pending_store.json.dumps',side_effect=fail_plan):
            with self.assertRaises(Died): env.pipeline.process(self.source(env,43))
        self.assertEqual(self.old_bytes(env,topic),before)
        check_connection_before_recovery(env.store.connection)

    def test_lifecycle_confirmation_is_explicitly_not_linked(self):
        env=self.make_pipeline(extensions=True); _,topic=self.seed(env)
        env.provider.responses.append('lifecycle')
        self.assert_new(env,topic,self.source(env,43))
        self.assertEqual(env.provider.calls,[(42,),(1,2),(43,)])

    def test_image_budget_does_not_allow_unseen_bare_image_link(self):
        from tg_triage.config import AttachmentsConfig
        env=self.make_pipeline(vision=True,files={42:(JPG,'one.jpg'),43:(JPG,'two.jpg')},
                               attachments=replace(AttachmentsConfig(),max_per_case=1))
        _,topic=self.seed_image(env)
        self.assert_new(env,topic,self.source(env,43,text='',has_image=True,image_size=len(JPG),image_mime='image/jpeg'))
        self.assertEqual(env.provider.calls,[])

    def test_replay_after_effects_before_checkpoint_done_does_not_repeat(self):
        class Died(BaseException): pass
        for response in ('unknown','bug','chat'):
            with self.subTest(response=response):
                env=self.make_pipeline(extensions=True); _,topic=self.seed(env)
                env.provider.responses.append(response)
                incoming=self.source(env,43,text='POS ket giay')
                with patch.object(env.pending,'finish_source_followup',side_effect=Died()):
                    with self.assertRaises(Died): env.pipeline.process(incoming)
                check_connection_before_recovery(env.store.connection)
                sent=len(env.sent); tickets=len(env.github.calls)
                result=env.pipeline.close_due_cases()
                self.assertEqual(len(result),1)
                self.assertEqual(len(env.sent),sent)
                self.assertEqual(len(env.github.calls),tickets)
                self.assertEqual(env.store.connection.execute('SELECT count(*) FROM rules_inflight_topics').fetchone()[0],0)
                check_connection_before_recovery(env.store.connection)

    def test_document_in_old_topic_excludes_candidate(self):
        env=self.make_pipeline(files={42:(b'proof','proof.txt')})
        _,topic=self.seed(env,has_document=True,document_name='proof.txt',document_mime='text/plain')
        self.assert_new(env,topic,self.source(env,43))
        self.assertEqual(env.provider.calls,[(42,),(43,)])

    def test_documented_followup_case_coverage(self):
        from tests.test_rules_markdown import parse, records
        _,tables=parse(ROOT/'rules/examples.md')
        cases={row['#']:row for row in records(tables['source_followup_cases'])}
        methods={
            'SF1': ('test_link_ready_creates_exactly_one_ticket_with_both_messages',),
            'SF2': ('test_link_need_info_sends_no_second_fallback',),
            'SF3': ('test_model_splits_new_topic_old_snapshot_and_attempts_byte_identical',),
            'SF4': ('test_different_sender_creates_new_topic',),
            'SF5': ('test_older_than_window_and_exact_boundary_create_new_topic',),
            'SF6': ('test_two_matching_open_topics_create_new_topic',),
            'SF7': ('test_reply_uses_existing_path_even_with_other_sender_after_window',),
            'SF8': ('test_courtesy_filtered_before_candidate_or_model',),
            'SF9': ('test_dry_run_sends_nothing_for_need_info_or_ready',),
            'SF10': ('test_readonly_intents_preserve_old_topic_and_match_different_sender',),
            'SF11': ('test_readonly_intents_preserve_old_topic_and_match_different_sender',),
            'SF12': ('test_analyze_without_task_multi_sweep_matches_different_sender',),
            'SF13': ('test_image_first_text_second_seen_image_ready_uploads_one_ticket',),
            'SF14': ('test_image_first_without_vision_text_is_new',),
            'SF15': ('test_text_first_seen_image_is_linked',),
            'SF16': ('test_rejected_or_missing_image_is_not_linked','test_unseen_bare_image_does_not_link_or_call_model'),
            'SF17': ('test_two_bare_images_then_text_are_confirmed_together',),
            'SF18': ('test_document_is_excluded_before_confirmation',),
            'SF19': ('test_slash_text_that_is_not_command_can_link',),
            'SF20': ('test_confirmation_death_leaves_recoverable_new_hold_old_unchanged','test_confirmation_death_retry_hold_multi_sweep_matches_different_sender'),
            'SF21': ('test_committed_link_replays_without_model_or_duplicate_effect',),
            'SF22': ('test_image_budget_does_not_allow_unseen_bare_image_link',),
            'SF23': ('test_need_info_and_media_only_count_together_for_ambiguity',),
            'SF24': ('test_lifecycle_confirmation_is_explicitly_not_linked',),
            'SF25': ('test_team_request_matches_different_sender_without_confirmation',),
            'SF26': ('test_received_timestamps_control_window_despite_processing_backlog',),
            'SF27': ('test_provider_down_multi_sweep_matches_different_sender','test_request_too_large_multi_sweep_matches_different_sender','test_confirmation_death_retry_hold_multi_sweep_matches_different_sender'),
            'SF28': ('test_sweep_yields_queued_committed_group_to_worker_and_expires','test_transient_replay_failure_is_isolated_after_expiry_and_hold'),
            'SF29': ('test_done_checkpoint_survives_rules_fingerprint_change',),
            'SF30': ('test_followup_operational_error_halts_worker_and_restart_gate_immediately',),
            'SF31': ('test_two_crashes_quarantined_link_member_halts_instead_of_deferring',),
            'SF32': ('test_followup_sources_change_halts_gate_and_worker_before_sweep','test_followup_gate_rejects_other_unreplayable_stored_states'),
            'SF33': ('test_healthy_followup_plan_passes_gate_and_replays_once','test_no_followup_gate_and_ambiguity_match_head_for_all_batch_states'),
            'SF34': ('test_followup_fingerprint_comparison_remains_at_worker_and_sweep',),
        }
        import re
        documented={case:tuple(names.split(',')) for case,names in re.findall(
            r'<!-- SF-test (SF\d+): ([\w,]+) -->',(ROOT/'rules/examples.md').read_text(encoding='utf8'))}
        self.assertEqual(set(cases),set(methods))
        self.assertEqual(set(documented),set(cases))
        for case,names in methods.items():
            with self.subTest(case=case):
                self.assertEqual(documented[case],names)
                for method in names: self.assertTrue(callable(getattr(self,method)))

    def test_replay_after_local_case_assignment_before_post(self):
        class Died(BaseException): pass
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'store.db'
        env=self.make_pipeline(path=path,extensions=True)
        self.seed(env); env.provider.responses.append('bug')
        with patch.object(env.pipeline,'_run',side_effect=Died()):
            with self.assertRaises(Died): env.pipeline.process(self.source(env,43,text='POS ket giay'))
        check_connection_before_recovery(env.store.connection)
        self.assertEqual(env.github.calls,[])
        env.store.close()
        resumed=self.make_pipeline(path=path,extensions=True)
        results=resumed.pipeline.close_due_cases()
        self.assertEqual(results[0].decision,'issue')
        self.assertEqual(len(resumed.github.calls),1)
        self.assertEqual(resumed.provider.calls,[])
        self.assertEqual(resumed.store.connection.execute('SELECT count(*) FROM rules_inflight_topics').fetchone()[0],0)
        check_connection_before_recovery(resumed.store.connection)

    def test_not_linked_confirmation_and_old_path_have_separate_bounded_turns(self):
        from tg_triage.thinking import CURRENT
        env=self.make_pipeline(extensions=True); _,topic=self.seed(env)
        turns=[]
        def observe(ids):
            turn=CURRENT.get()
            turns.append((tuple(ids),turn,turn.max_calls,turn.deadline-turn.started))
        env.provider.before_answer=observe
        env.provider.responses.append('split')
        self.assert_new(env,topic,self.source(env,43))
        self.assertEqual([t[0] for t in turns],[(1,2),(43,)])
        self.assertIsNot(turns[0][1],turns[1][1])
        self.assertEqual([(t[2],t[3]) for t in turns],[(3,40),(3,40)])

    def test_queued_replay_after_ticket_before_done_restores_message(self):
        class Died(BaseException): pass
        env=self.make_pipeline(extensions=True); self.seed(env)
        env.provider.responses.append('bug'); incoming=self.source(env,43,text='POS ket giay')
        with patch.object(env.pending,'finish_source_followup',side_effect=Died()):
            with self.assertRaises(Died): env.pipeline.process(incoming)
        with env.store.connection:
            env.store.connection.execute("UPDATE messages SET decision='queued',attempts=0 WHERE message_id=43")
        result=env.pipeline.process(incoming)
        self.assertEqual(result.decision,'issue')
        self.assertEqual(env.store.get(incoming.chat_id,43)['decision'],'issue')
        self.assertEqual(len(env.github.calls),1)
        self.assertEqual(len(env.sent),2)
        self.assertEqual(env.store.connection.execute('SELECT count(*) FROM rules_inflight_topics').fetchone()[0],0)
        check_connection_before_recovery(env.store.connection)

    def test_done_checkpoint_survives_rules_fingerprint_change(self):
        env=self.make_pipeline(extensions=True); self.seed(env)
        incoming=self.source(env,43,text='POS ket giay'); env.provider.responses.append('bug')
        expected=env.pipeline.process(incoming)
        import shutil
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        directory=Path(temp.name)
        for name in ('states','facts','readiness','questions','channels','examples','lifecycle','analysis-status'):
            shutil.copyfile(ROOT/f'rules/{name}.md',directory/f'{name}.md')
        questions=directory/'questions.md'
        questions.write_text(questions.read_text(encoding='utf8')+'\n',encoding='utf8')
        env.pipeline.rules_snapshot=RulesSnapshot(directory,ROOT/'templates/github-ticket',extensions=True)
        self.assertNotEqual(env.pipeline.rules_snapshot.fingerprint,env.pending.source_followup_checkpoint(incoming)[2])
        with env.store.connection:
            env.store.connection.execute("UPDATE messages SET decision='queued',attempts=0 WHERE message_id=43")
        actual=env.pipeline.process(incoming)
        for name in ('decision','reason','tier','case_key','issue_number','issue_url','classification'):
            self.assertEqual(getattr(actual,name),getattr(expected,name))
        self.assertEqual(len(env.github.calls),1)
        self.assertEqual(len(env.sent),2)
        self.assertEqual(env.provider.calls,[(42,),(1,2)])

    def test_sweep_yields_queued_committed_group_to_worker_and_expires(self):
        class Died(BaseException): pass
        for sweep_first in (False,True):
            with self.subTest(sweep_first=sweep_first):
                temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
                path=Path(temp.name)/'store.db'
                env=self.make_pipeline(path=path,extensions=True); self.seed(env)
                env.provider.responses.append('bug')
                with patch.object(rules_intake,'_create_single',side_effect=Died()):
                    with self.assertRaises(Died): env.pipeline.process(self.source(env,43,text='POS ket giay'))
                check_connection_before_recovery(env.store.connection)
                env.store.close(); resumed=self.make_pipeline(path=path,extensions=True)
                queued=resumed.store.queued_messages()
                self.assertEqual([m.message_id for m in queued],[42,43])
                if sweep_first:
                    with patch.object(resumed.pending,'expire_due',wraps=resumed.pending.expire_due) as expiry:
                        self.assertEqual(resumed.pipeline.close_due_cases(),[])
                        expiry.assert_called_once_with(resumed.pipeline.rules_snapshot.limits['pending_expiry_days'])
                    self.assertEqual([m.message_id for m in resumed.store.queued_messages()],[42,43])
                    self.assertEqual(resumed.github.calls,[])
                # Pipeline.process performs start_classifying itself, as the real worker does.
                for member in queued: resumed.pipeline.process(member)
                self.assertEqual(resumed.pipeline.close_due_cases(),[])
                self.assertEqual(len(resumed.github.calls),1)
                self.assertEqual(resumed.provider.calls,[])
                self.assertEqual(resumed.store.connection.execute('SELECT count(*) FROM rules_inflight_topics').fetchone()[0],0)
                for mid in (42,43): self.assertEqual(resumed.store.get(queued[0].chat_id,mid)['decision'],'issue')
                check_connection_before_recovery(resumed.store.connection)

    def test_transient_replay_failure_is_isolated_after_expiry_and_hold(self):
        class Died(BaseException): pass
        env=self.make_pipeline(extensions=True); self.seed(env)
        env.provider.responses.append(None)
        env.pipeline.process(self.source(env,44,sender_id=11))
        env.provider.responses.append('bug')
        with patch.object(rules_intake,'_complete_source_followup',side_effect=Died()):
            with self.assertRaises(Died): env.pipeline.process(self.source(env,43,text='POS ket giay'))
        order=[]
        expire=env.pending.expire_due; complete=rules_intake._complete_source_followup
        def expiry(*args): order.append('expiry'); return expire(*args)
        def replay(*args,**kwargs): order.append('replay'); raise RuntimeError('transient fake failure')
        env.provider.before_answer=lambda ids: order.append('hold')
        env.provider.responses.append(None)
        with patch.object(env.pending,'expire_due',side_effect=expiry), patch.object(rules_intake,'_complete_source_followup',side_effect=replay):
            with self.assertLogs('tg_triage.rules_intake',level='ERROR'):
                results=env.pipeline.close_due_cases()
        self.assertEqual(order,['expiry','hold','replay'])
        self.assertEqual([r.reason for r in results],['rules_hold'])
        self.assertEqual(len(env.pending.source_followup_replays()),1)
        env.provider.responses.append(None)
        results=env.pipeline.close_due_cases()
        self.assertEqual([r.decision for r in results],['pending_review','issue'])
        self.assertEqual(len(env.github.calls),1)
        self.assertEqual(env.pending.source_followup_replays(),[])
        self.assertEqual(env.store.connection.execute('SELECT count(*) FROM rules_inflight_topics').fetchone()[0],0)

    def test_death_between_rejected_confirmation_and_old_path_is_recoverable(self):
        class Died(BaseException): pass
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'store.db'
        env=self.make_pipeline(path=path,extensions=True)
        _,topic=self.seed(env); before=self.old_bytes(env,topic)
        env.provider.responses.append('split')
        with patch.object(rules_intake,'_process_batch',side_effect=Died()):
            with self.assertRaises(Died): env.pipeline.process(self.source(env,43))
        self.assertEqual(self.old_bytes(env,topic),before)
        check_connection_before_recovery(env.store.connection)
        env.store.close(); resumed=self.make_pipeline(path=path,extensions=True)
        resumed.provider.responses.append('unknown'); resumed.pipeline.close_due_cases()
        self.assertEqual(self.old_bytes(resumed,topic),before)
        self.assertEqual(resumed.store.connection.execute('SELECT phase FROM rules_topics WHERE topic_id=?',(f'{self.source(resumed).chat_id}:43',)).fetchone()[0],'NEW')
        self.assertEqual(resumed.pipeline.close_due_cases(),[])
        check_connection_before_recovery(resumed.store.connection)

    def assert_replay_halts_after_change(self, change):
        from tg_triage.rules_pending_store import RulesHalt
        from tg_triage.telegram_io import _sweep_cases
        class Died(BaseException): pass
        env=self.make_pipeline(extensions=True); _,topic=self.seed(env)
        incoming=self.source(env,43,text='POS ket giay'); env.provider.responses.append('bug')
        with patch.object(rules_intake,'_complete_source_followup',side_effect=Died()):
            with self.assertRaises(Died): env.pipeline.process(incoming)
        with env.store.connection:
            if change=='fingerprint':
                env.store.connection.execute("UPDATE rules_batches SET fingerprint='changed' WHERE batch_id=?",(f'source-followup:{incoming.chat_id}:43',))
            else:
                env.store.enqueue(self.source(env,60,sender_id=99))
                env.store.connection.execute('INSERT INTO rules_sources VALUES(?,?,?,?)',(topic,9,incoming.chat_id,60))
        for sweep in (env.pipeline.close_due_cases,lambda: _sweep_cases(env.pipeline)):
            reason=f'followup checkpoint {change}.*changed' if change=='fingerprint' else 'unresolved ambiguous operation; Owner inspection required'
            with self.assertRaisesRegex(RulesHalt,reason):
                sweep()
        if change=='sources':
            with self.assertRaisesRegex(RulesHalt,'source-followup checkpoint .*sources changed'):
                check_connection_before_recovery(env.store.connection)
        self.assertEqual(env.pending.source_followup_checkpoint(incoming)[1],'PLANNED')
        self.assertEqual(env.store.get(incoming.chat_id,43)['decision'],'pending_review')
        self.assertEqual(env.provider.calls,[(42,),(1,2)])
        self.assertEqual(env.github.calls,[])

    def test_replay_fingerprint_change_propagates_rules_halt(self):
        self.assert_replay_halts_after_change('fingerprint')

    def test_replay_sources_change_propagates_rules_halt(self):
        self.assert_replay_halts_after_change('sources')

    def paced_link_replay(self, *, extensions=True, image=False, extras=()):
        from tg_triage.pacing import RELEASED_SOURCES
        from tg_triage.rules_pending_store import check_before_recovery
        from tg_triage.telegram_io import _process_message
        class Died(BaseException): pass
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'store.db'
        files={42:(JPG,'proof.jpg')} if image else None
        env=self.make_pipeline(path=path,extensions=extensions,vision=image,files=files)
        _,topic=self.seed_image(env) if image else self.seed(env)
        env.provider.responses.append('bug')
        with patch.object(rules_intake,'_create_single',side_effect=Died()):
            with self.assertRaises(Died): env.pipeline.process(self.source(env,43,text='POS ket giay'))
        self.assertEqual(env.github.calls,[])
        env.store.close(); check_before_recovery(path)
        resumed=self.make_pipeline(path=path,extensions=extensions,vision=image,files=files)
        for mid in extras: resumed.store.enqueue(self.source(resumed,mid,sender_id=99,text='Separate conversation'))
        queued=resumed.store.queued_messages()
        self.assertEqual([m.message_id for m in queued],[42,43,*extras])
        # If extraction were incorrectly called on the link, this response would split it.
        resumed.provider.responses.append('chat' if extras else 'split')
        token=RELEASED_SOURCES.set(frozenset((m.chat_id,m.message_id) for m in queued))
        try:
            # Also cover the unrelated message being dequeued first.
            order=queued[-len(extras):]+queued[:-len(extras)] if extras else queued
            with patch.object(rules_intake,'_process_batch',wraps=rules_intake._process_batch) as batch:
                for member in order: _process_message(resumed.pipeline,member)
            self.assertEqual(batch.call_count,int(bool(extras)))
            if extras: self.assertEqual([m.message_id for m in batch.call_args.args[1]],list(extras))
        finally:
            RELEASED_SOURCES.reset(token)
        self.assertEqual(resumed.provider.calls,[tuple(extras)] if extras else [])
        if not extras: self.assertEqual(resumed.provider.responses,['split'])
        self.assertEqual(len(resumed.github.calls),1)
        self.assertEqual(len(resumed.uploader.uploads),int(image))
        body=resumed.github.calls[0].body
        self.assertIn('/42',body); self.assertIn('/43',body)
        for mid in extras:
            self.assertNotIn('/'+str(mid),body)
            self.assertEqual(resumed.store.get(queued[0].chat_id,mid)['decision'],'chat')
        for mid in (42,43):
            row=resumed.store.get(queued[0].chat_id,mid)
            self.assertEqual((row['decision'],row['issue_number']),('issue',42))
        self.assertEqual(resumed.pending.topic_snapshot(topic)['status'],'CLOSED')
        self.assertEqual(resumed.pending.source_followup_checkpoint(queued[1])[1],'DONE')
        self.assertEqual(resumed.pending.source_followup_replays(),[])
        self.assertEqual(resumed.store.connection.execute('SELECT count(*) FROM rules_inflight_topics').fetchone()[0],0)
        self.assertEqual(resumed.pipeline.close_due_cases(),[])
        check_connection_before_recovery(resumed.store.connection)

    def test_paced_link_members_complete_without_reextraction(self):
        for extensions in (False,True):
            with self.subTest(extensions=extensions): self.paced_link_replay(extensions=extensions)

    def test_paced_image_link_ignores_model_would_split(self):
        for extensions in (False,True):
            with self.subTest(extensions=extensions): self.paced_link_replay(extensions=extensions,image=True)

    def test_paced_link_with_unrelated_third_message_uses_old_batch_path(self):
        self.paced_link_replay(image=True,extras=(44,))

    def test_paced_link_with_unrelated_remaining_batch_uses_old_batch_path(self):
        self.paced_link_replay(image=True,extras=(44,45))

    def test_paced_batch_without_link_members_preserves_old_batch_bytes(self):
        from tg_triage.pacing import RELEASED_SOURCES
        for extensions in (False,True):
            traces=[]
            for control in (False,True):
                with self.subTest(extensions=extensions,control=control):
                    env=self.make_pipeline(extensions=extensions)
                    sources=[self.source(env,42,text='First conversation'),self.source(env,43,sender_id=11,text='Second conversation')]
                    for member in sources: env.store.enqueue(member)
                    env.provider.responses.append('chat')
                    token=RELEASED_SOURCES.set(frozenset((m.chat_id,m.message_id) for m in sources))
                    try:
                        # Bypass only the new lookup for the control; both use the real HEAD batch path.
                        lookup=env.pending.source_followup_for_member
                        with patch.object(env.pending,'source_followup_for_member',side_effect=(lambda m: None) if control else lookup):
                            with patch.object(rules_intake,'_process_batch',wraps=rules_intake._process_batch) as batch:
                                result=env.pipeline.process(sources[0])
                        self.assertEqual(batch.call_count,1)
                        self.assertEqual(batch.call_args.args[1],sources)
                    finally: RELEASED_SOURCES.reset(token)
                    traces.append((result,env.provider.packets,list(env.store.connection.iterdump()),env.sent))
            self.assertEqual(traces[0],traces[1])

    def test_two_crashes_quarantined_link_member_halts_instead_of_deferring(self):
        from tg_triage.rules_pending_store import RulesHalt, check_before_recovery
        class Died(BaseException): pass
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'store.db'
        env=self.make_pipeline(path=path,extensions=True); self.seed(env)
        env.provider.responses.append('bug')
        with patch.object(rules_intake,'_create_single',side_effect=Died()):
            with self.assertRaises(Died): env.pipeline.process(self.source(env,43,text='POS ket giay'))
        env.store.close(); check_before_recovery(path)
        again=self.make_pipeline(path=path,extensions=True)
        with patch.object(rules_intake,'_create_single',side_effect=Died()):
            with self.assertRaises(Died): again.pipeline.process(again.store.queued_messages()[0])
        again.store.close()
        with self.assertRaisesRegex(RulesHalt,'attempt limit'): check_before_recovery(path)
        # Inspect the ordinary Store quarantine without constructing a serving Pipeline.
        resumed=Store(path); self.addCleanup(resumed.close)
        states=[tuple(row) for row in resumed.connection.execute('SELECT message_id,decision,attempts FROM messages ORDER BY message_id')]
        self.assertEqual(states,[(42,'error',2),(43,'error',2)])
        with self.assertRaisesRegex(RulesHalt,'quarantined'): check_connection_before_recovery(resumed.connection)
        self.assertEqual(resumed.queued_messages(),[])
        with self.assertRaisesRegex(RulesHalt,'unresolved ambiguous operation'):
            Pipeline(again.pipeline.config,resumed,again.provider,again.github,rules_snapshot=again.pipeline.rules_snapshot)
        self.assertEqual(again.provider.calls,[])
        self.assertEqual(again.github.calls,[])
        self.assertEqual([tuple(row) for row in resumed.connection.execute('SELECT message_id,decision,attempts FROM messages ORDER BY message_id')],states)

    def unfinished_followup(self, *, path=':memory:'):
        class Died(BaseException): pass
        env=self.make_pipeline(path=path,extensions=True); _,topic=self.seed(env)
        incoming=self.source(env,43,text='POS ket giay'); env.provider.responses.append('bug')
        with patch.object(rules_intake,'_complete_source_followup',side_effect=Died()):
            with self.assertRaises(Died): env.pipeline.process(incoming)
        return env,topic,incoming

    def test_followup_operational_error_halts_worker_and_restart_gate_immediately(self):
        import sqlite3
        from tg_triage.rules_pending_store import RulesHalt, check_before_recovery
        from tg_triage.telegram_io import _process_message
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'store.db'
        env=self.make_pipeline(path=path,extensions=True); self.seed(env)
        incoming=self.source(env,43,text='POS ket giay'); env.provider.responses.append('bug')
        resume=rules_intake._resume_source; attempts=[]
        def once(*args):
            attempts.append(1)
            if len(attempts)==1: raise sqlite3.OperationalError('database is locked')
            return resume(*args)
        with patch.object(rules_intake,'_resume_source',side_effect=once):
            with self.assertRaisesRegex(RulesHalt,'source-followup completion failed'):
                _process_message(env.pipeline,incoming)
        self.assertTrue(env.pending.unresolved_ambiguity())
        self.assertEqual(env.store.get(incoming.chat_id,43)['decision'],'error')
        before=list(env.store.connection.iterdump()); changes=env.store.connection.total_changes
        with self.assertRaisesRegex(RulesHalt,'member .* is quarantined'):
            check_connection_before_recovery(env.store.connection)
        self.assertEqual(list(env.store.connection.iterdump()),before)
        self.assertEqual(env.store.connection.total_changes,changes)
        env.store.close()
        with self.assertRaisesRegex(RulesHalt,'member .* is quarantined'): check_before_recovery(path)
        from tg_triage.legacy_preflight import inspect
        report=inspect(path)
        self.assertEqual(report['recovery_gate'],'HOLD')
        self.assertIn('RECOVERY_REQUIRES_RECONCILIATION',report['recovery_codes'])
        self.assertEqual(env.github.calls,[])

    def test_followup_sources_change_halts_gate_and_worker_before_sweep(self):
        from tg_triage.rules_pending_store import RulesHalt
        from tg_triage.telegram_io import _process_message
        env,topic,incoming=self.unfinished_followup()
        env.store.enqueue(self.source(env,60,sender_id=99))
        with env.store.connection:
            env.store.connection.execute('INSERT INTO rules_sources VALUES(?,?,?,?)',(topic,9,incoming.chat_id,60))
        self.assertTrue(env.pending.unresolved_ambiguity())
        with self.assertRaisesRegex(RulesHalt,'sources changed'): check_connection_before_recovery(env.store.connection)
        with self.assertRaisesRegex(RulesHalt,'unresolved ambiguous operation'): _process_message(env.pipeline,incoming)
        self.assertEqual(env.github.calls,[])

    def test_healthy_followup_plan_passes_gate_and_replays_once(self):
        from tg_triage.rules_pending_store import check_before_recovery
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        path=Path(temp.name)/'store.db'
        env,topic,incoming=self.unfinished_followup(path=path)
        self.assertFalse(env.pending.unresolved_ambiguity())
        check_connection_before_recovery(env.store.connection)
        env.store.close(); check_before_recovery(path)
        resumed=self.make_pipeline(path=path,extensions=True)
        self.assertFalse(resumed.pending.unresolved_ambiguity())
        results=resumed.pipeline.close_due_cases()
        self.assertEqual([r.decision for r in results],['issue'])
        self.assertEqual(resumed.pending.source_followup_checkpoint(incoming)[1],'DONE')
        self.assertFalse(resumed.pending.unresolved_ambiguity())
        self.assertEqual(resumed.provider.calls,[])
        self.assertEqual(len(resumed.github.calls),1)
        self.assertEqual(resumed.pipeline.close_due_cases(),[])
        check_connection_before_recovery(resumed.store.connection)

    def test_followup_fingerprint_comparison_remains_at_worker_and_sweep(self):
        from tg_triage.rules_pending_store import RulesHalt
        env,topic,incoming=self.unfinished_followup()
        with env.store.connection:
            env.store.connection.execute('UPDATE rules_batches SET fingerprint=? WHERE batch_id=?',('0'*64,f'source-followup:{incoming.chat_id}:43'))
        self.assertFalse(env.pending.unresolved_ambiguity())
        check_connection_before_recovery(env.store.connection)
        with self.assertRaisesRegex(RulesHalt,'fingerprint changed'): env.pipeline.close_due_cases()

    def test_followup_gate_rejects_other_unreplayable_stored_states(self):
        from tg_triage.rules_pending_store import RulesHalt, _source_followup_blocker
        for problem in ('claim','case_members','unavailable','missing_message','invalid_plan'):
            with self.subTest(problem=problem):
                env,topic,incoming=self.unfinished_followup()
                with env.store.connection:
                    if problem=='claim': env.store.connection.execute("UPDATE rules_inflight_topics SET batch_id='other' WHERE topic_id=?",(topic,))
                    elif problem=='case_members': env.store.connection.execute("UPDATE messages SET case_key='foreign' WHERE message_id=42")
                    elif problem=='unavailable': env.store.connection.execute("UPDATE messages SET decision='chat' WHERE message_id=42")
                    elif problem=='missing_message': env.store.connection.execute('DELETE FROM messages WHERE message_id=42')
                    else: env.store.connection.execute("UPDATE rules_batch_groups SET plan='{}' WHERE batch_id=?",(f'source-followup:{incoming.chat_id}:43',))
                tables={r[0] for r in env.store.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertIn('Owner inspection required',_source_followup_blocker(env.store.connection,tables,before_recovery=True))
                self.assertTrue(env.pending.unresolved_ambiguity())
                with self.assertRaises(RulesHalt): check_connection_before_recovery(env.store.connection)
                self.assertEqual(env.github.calls,[])

    def test_no_followup_gate_and_ambiguity_match_head_for_all_batch_states(self):
        import ast,subprocess,textwrap
        from tg_triage import rules_pending_store as module
        path='listener/src/tg_triage/rules_pending_store.py'
        base=subprocess.check_output(['git','show','HEAD:'+path],cwd=ROOT.parent,text=True,encoding='utf8')
        tree=ast.parse(base); namespace=dict(vars(module))
        gate=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='check_connection_before_recovery')
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='RulesPendingStore')
        ambiguity=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='unresolved_ambiguity')
        exec(ast.get_source_segment(base,gate),namespace)
        head_gate=namespace['check_connection_before_recovery']
        exec(textwrap.dedent(ast.get_source_segment(base,ambiguity)),namespace)
        head_ambiguity=namespace['unresolved_ambiguity']
        def outcome(gate,connection):
            try: return gate(connection)
            except module.RulesHalt as error: return type(error),str(error)
        for status in (None,'PLANNED','RESERVED','AMBIGUOUS','DONE','BLOCKED'):
            with self.subTest(status=status):
                env=self.make_pipeline(extensions=True)
                if status:
                    env.provider.responses.append('chat'); env.pipeline.process(self.source(env,43,text='Separate conversation'))
                    with env.store.connection: env.store.connection.execute('UPDATE rules_batch_groups SET status=?',(status,))
                before=list(env.store.connection.iterdump()); changes=env.store.connection.total_changes; factory=env.store.connection.row_factory
                self.assertEqual(env.pending.unresolved_ambiguity(),head_ambiguity(env.pending))
                self.assertEqual(outcome(check_connection_before_recovery,env.store.connection),outcome(head_gate,env.store.connection))
                self.assertEqual(list(env.store.connection.iterdump()),before)
                self.assertEqual(env.store.connection.total_changes,changes)
                self.assertIs(env.store.connection.row_factory,factory)
