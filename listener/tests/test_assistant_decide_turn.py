"""Offline checks for the real decide runtime and its transport boundary."""
import hashlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tg_triage import assistant, router_transport
from tg_triage.__main__ import _load_assistant_pack, _load_decision_rules
from tg_triage.assistant_contract import CONTRACT, PHASE_DECIDE, GuardContext, json_schema
from tg_triage.assistant_extractor import AssistantExtractor, batch_fingerprint
from tg_triage.config import AssistantConfig
from tg_triage.decision_rules import RulesSnapshot
from tg_triage.router_transport import RouterError, RouterReply, ToolCall
from tg_triage.rules_extraction import partition_and_extract
from tg_triage.store import Store
from tests import test_assistant_turn as propose
from tests.helpers import NOW, PNG, config, message
from tests.test_assistant_integration import Clock, Provider
from tests.test_router_transport import Opener, Response


def reply(mid=42, *, calls=(), cache=3, **overrides):
    group = dict(message_ids=[mid], state='CHAT', rule='R7', template=None, issue=None,
                 target=None, relation=None, question=None, refs=[], confidence=.9,
                 reasoning_summary='Conversation')
    group.update(overrides)
    return RouterReply(json.dumps(dict(contract=CONTRACT, groups=[group])), tuple(calls),
                       'stop', 12, 5, 1, cache, 'model', 7)


class AssistantDecideTurnTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'test.db'
        self.store = Store(self.path, clock=lambda: NOW)
        self.addCleanup(self.store.close)
        self.snapshot = _load_decision_rules()
        self.clock = Clock()
        self.pack = 'Full rule pack\n' + 'example text\n' * 3000
        base = config()
        self.settings = replace(base, db_path=self.path, assistant=AssistantConfig(
            enabled=True, mode='live', authority='decide', router_key='test',
            github_repos=(base.gh_repo, 'other/repo')))

    def runtime(self, chat, *, hosted=False, **assistant):
        cfg = replace(self.settings, assistant=replace(self.settings.assistant, **assistant))
        if hosted:
            cfg = replace(cfg, model=replace(cfg.model, provider='deepseek', cloud_consent=True,
                                            base_url='https://api.deepseek.com', name='hosted'))
        return AssistantExtractor(cfg, self.store, self.pack, chat=chat,
                                  clock=self.clock, wall_clock=self.clock)

    def job(self, mid=42, *, image=False, thread=None):
        source = message(mid, text=f'Batch {mid}', has_image=image)
        image_paths = {}
        if image:
            path = self.path.with_suffix('.png')
            path.write_bytes(PNG)
            image_paths[mid] = path
        bodies = []
        partition_and_extract(Provider(), self.snapshot, [source], images=image_paths, body_out=bodies)
        packet = json.loads(bodies[0]['messages'][1]['content'])
        return dict(sources=(source,), snapshot=self.snapshot,
                    data=dict(packet, images=image_paths, documents=[], read_context=(message(mid - 1),)),
                    fingerprint=batch_fingerprint([source]), id_space='message',
                    topic_id=f'topic:{mid}', chat_id=source.chat_id,
                    guard_ctx=GuardContext(frozenset({mid}), source.chat_id, self.settings.gh_repo,
                        {}, {}, frozenset({'R7', 'R6'}), lambda rule: f'{rule} nguyên văn',
                        frozenset(), seen_image_ids=frozenset(image_paths)),
                    code_precheck=dict(handled_by_code=False, checks=[], decided_rule=None,
                                       rule_text=None, handoff='assistant', reason='Không khớp'),
                    log_thread_id=thread, deadline=self.clock() + 180)

    def rows(self, table='classification_runs'):
        return [dict(row) for row in self.store.connection.execute(f'SELECT * FROM {table} ORDER BY 1,2')]

    def assert_prefixes(self, requests):
        prefixes = []
        for args, kwargs in requests:
            self.assertEqual(len(kwargs['tools']), 7)
            self.assertEqual([t['function']['name'] for t in kwargs['tools']],
                ['search_tickets', 'get_ticket', 'get_topic', 'list_open_topics',
                 'get_messages', 'get_image', 'get_rule'])
            system = args[1][0]['content']
            self.assertTrue(system.startswith(self.pack))
            self.assertIn(json.dumps(json_schema(), ensure_ascii=False), system)
            prefixes.append((system + json.dumps(kwargs['tools'])).encode())
        self.assertTrue(all(prefix == prefixes[0] for prefix in prefixes))
        return hashlib.sha256(prefixes[0]).hexdigest()

    def test_stable_prefix_across_batches_and_schema_repair(self):
        malformed = replace(reply(cache=5), content='broken')
        chat = Mock(side_effect=[malformed, reply(), reply(43, cache=9)])
        runtime = self.runtime(chat)
        first = self.job()
        with patch('tg_triage.rules_intake._evaluate_batch', side_effect=AssertionError('propose path')):
            groups, run_id, outcome = runtime.decide(first)
            _, second_run, second_outcome = runtime.decide(self.job(43, thread=run_id))
        self.assertEqual((outcome, second_outcome, groups[0].run_id), ('done', 'done', run_id))
        self.assertEqual([c.kwargs['tool_choice'] for c in chat.call_args_list], ['auto', 'none', 'auto'])
        self.assertIn('invalid JSON decision', chat.call_args_list[1].args[1][-1]['content'])
        self.assertNotEqual(chat.call_args_list[0].args[1][1], chat.call_args_list[2].args[1][1])
        self.assertIn('Batch 42', chat.call_args_list[0].args[1][-1]['content'])
        prefix_hash = self.assert_prefixes(chat.call_args_list)
        first_row, second_row = self.rows()
        self.assertEqual([r['rules_version'] for r in (first_row, second_row)], [prefix_hash] * 2)
        self.assertEqual((first_row['phase'], first_row['log_thread_id'], second_row['log_thread_id']),
                         (PHASE_DECIDE, run_id, run_id))
        self.assertEqual((first_row['run_id'], second_row['run_id']), (run_id, second_run))
        self.assertEqual(json.loads(first_row['code_precheck_json']), first['code_precheck'])
        self.assertEqual(json.loads(first_row['outcomes_json']), [dict(
            message_ids=[42], state='CHAT', rule='R7', rule_text='R7 nguyên văn', template=None,
            ticket_ref=None, confidence=.9, reasoning_summary='Conversation', violations=[],
            related=[], question_block=None)])
        self.assertEqual([r['cached_tokens'] for r in self.rows('classification_model_calls')], [5, 3, 9])
        self.assertEqual(first_row['cache_hit_tokens'], 8)

    def test_core_prefix_stable_across_batches_repair_and_links_and_audits_lookup(self):
        pack = _load_assistant_pack(pack_mode='core')
        full = assistant.decide_system(pack.text)
        core = assistant.decide_system(pack.text, core_pack=pack.core)
        self.assertEqual(hashlib.sha256(full.encode()).hexdigest(),
                         '667b166870f86adb64949e6a564eccfac6fb3e41d6d25d8bea8e876c4722d68e')
        self.assertLessEqual(2 * len(core.encode()), len(full.encode()))
        for number in range(1, 14):
            self.assertIn(f'| R{number} |', core)
        self.assertIn('RULE INDEX', core)
        self.assertIn('examples:bug ·', core)
        self.assertNotIn('POS không in được receipt, cả app và web', core)
        self.assertNotIn('Phiếu bật tính năng (chép vào PR release)', core)
        tool = ToolCall('lookup', 'get_rule', '{"rule_id":"facts:object"}')
        chat = Mock(side_effect=[RouterError('transient'), reply(calls=[tool]),
                                 reply(rule='X1'), reply(), reply(43)])
        settings = replace(self.settings, assistant=replace(self.settings.assistant, pack_mode='core'))
        runtime = AssistantExtractor(settings, self.store, pack.text, core_pack=pack.core,
                                     chat=chat, clock=self.clock, wall_clock=self.clock)
        self.assertEqual(runtime.decide(self.job())[2], 'done')
        self.assertEqual(runtime.decide(self.job(43))[2], 'done')
        self.assertEqual([call.args[1][0]['content'] for call in chat.call_args_list], [core] * 5)
        self.assertTrue(all(call.kwargs['tools'] == chat.call_args_list[0].kwargs['tools']
                            for call in chat.call_args_list))
        self.assertIn('guard_unknown_rule', chat.call_args_list[3].args[1][-1]['content'])
        self.assertEqual([row['rules_version'] for row in self.rows()],
                         [assistant.decide_prefix_hash(pack.text, core_pack=pack.core)] * 2)
        lookup, = self.rows('classification_tool_calls')
        captured = json.loads(lookup['result_json'])
        self.assertEqual(captured['untrusted_data']['rule_id'], 'facts:object')
        self.assertIn('Đối tượng cụ thể', captured['untrusted_data']['text'])

    def test_gemini_falls_back_to_gpt(self):
        chat = Mock(side_effect=[RouterError('transient'), reply()])
        self.assertEqual(self.runtime(chat).decide(self.job())[2], 'done')
        self.assertEqual([r['link'] for r in self.rows('classification_model_calls')], ['gemini', 'gpt'])
        self.assert_prefixes(chat.call_args_list)

    def core_runtime(self, chat, **settings):
        pack = _load_assistant_pack(pack_mode='core')
        cfg = replace(self.settings, assistant=replace(self.settings.assistant, pack_mode='core', **settings))
        return AssistantExtractor(cfg, self.store, pack.text, core_pack=pack.core,
                                  chat=chat, clock=self.clock, wall_clock=self.clock)

    def test_core_decides_after_eight_general_calls_and_four_rule_lookups(self):
        general = [ToolCall(f'msg-{n}', 'get_messages', json.dumps({'ids': [n]})) for n in range(1, 9)]
        # Distinct misses must not spend the three-error allowance.
        lookups = [ToolCall(f'rule-{n}', 'get_rule', json.dumps({'rule_id': key}))
                   for n, key in enumerate(('unknown-1', 'unknown-2', 'unknown-3', 'r9'))]
        chat = Mock(side_effect=[*(reply(calls=[call]) for call in (*general, *lookups)), reply()])
        self.assertEqual(self.core_runtime(chat).decide(self.job())[2], 'done')
        self.assertEqual(chat.call_count, 13)
        self.assertTrue(all(request.kwargs['tool_choice'] == 'auto' for request in chat.call_args_list))
        self.assertEqual([spec['function']['name'] for spec in chat.call_args_list[8].kwargs['tools']],
                         ['get_rule'])
        rows = self.rows('classification_tool_calls')
        self.assertEqual([row['tool'] for row in rows], ['get_messages'] * 8 + ['get_rule'] * 4)
        results = [json.loads(row['result_json']) for row in rows[8:]]
        self.assertEqual([entry['ok'] for entry in results], [False, False, False, True])
        self.assertEqual(results[-1]['untrusted_data']['rule_id'], 'R9')

    def test_core_lookup_cap_reports_exhaustion_and_keeps_general_tools(self):
        lookup = ToolCall('rules', 'get_rule', '{"ids":["R9"]}')
        denied = ToolCall('denied', 'get_rule', '{"ids":["R10"]}')
        general = ToolCall('general', 'get_messages', '{"ids":[42]}')
        chat = Mock(side_effect=[reply(calls=[lookup]), reply(calls=[denied]),
                                 reply(calls=[general]), reply()])
        self.assertEqual(self.core_runtime(chat, max_rule_lookups=1).decide(self.job())[2], 'done')
        first, blocked, last = self.rows('classification_tool_calls')
        self.assertTrue(json.loads(first['result_json'])['ok'])
        self.assertTrue(json.loads(last['result_json'])['ok'])
        after_denial = chat.call_args_list[2]
        self.assertEqual(after_denial.kwargs['tool_choice'], 'auto')
        self.assertNotIn('get_rule', [spec['function']['name'] for spec in after_denial.kwargs['tools']])
        self.assertEqual(json.loads(after_denial.args[1][-1]['content']), json.loads(blocked['result_json']))
        self.assertEqual(json.loads(blocked['result_json'])['error'], 'lookup_budget_exhausted')

    def test_core_repeated_ids_force_a_decision(self):
        lookup = ToolCall('rules', 'get_rule', '{"ids":["R9","R10"]}')
        repeated = ToolCall('repeat', 'get_rule', '{"ids":["r10"," r9 "]}')
        chat = Mock(side_effect=[reply(calls=[lookup]), reply(calls=[repeated]), reply()])
        self.assertEqual(self.core_runtime(chat).decide(self.job())[2], 'done')
        self.assertEqual(chat.call_args.kwargs['tool_choice'], 'none')
        self.assertEqual(json.loads(chat.call_args.args[1][-2]['content'])['error'], 'duplicate_tool')
        self.assertEqual(len(self.rows('classification_tool_calls')), 1)

    def test_both_router_links_fail_then_deepseek_uses_full_prefix_and_tools(self):
        chat = Mock(side_effect=RouterError('transient'))
        tool = ToolCall('ticket', 'get_ticket', '{"repo":"sandbox/triage","number":17}')
        decision = reply(state='APPEND', rule='R6', target=dict(number=17, basis='search'),
                         relation='duplicate', cache=17)
        with patch('tg_triage.assistant_extractor.read_key', return_value='test'), \
             patch('tg_triage.router_transport.deepseek_chat', side_effect=[reply(calls=[tool]), decision]) as hosted, \
             patch('tg_triage.assistant_github.GitHubReader.get_ticket',
                   return_value=dict(repo='sandbox/triage', number=17, state='open')):
            groups, _, outcome = self.runtime(chat, hosted=True).decide(self.job())
        self.assertEqual(outcome, 'done')
        self.assertEqual(dict(groups[0].targets[0]), dict(number=17, state='open', basis='search'))
        self.assert_prefixes([*chat.call_args_list, *((c.args[2:], c.kwargs) for c in hosted.call_args_list)])
        self.assertEqual(hosted.call_count, 2)
        self.assertGreater(len(hosted.call_args.args[3][0]['content'].encode()), 24000)
        self.assertIn('"number": 17', hosted.call_args.args[3][-1]['content'])
        calls = self.rows('classification_model_calls')
        self.assertEqual([r['link'] for r in calls], ['gemini', 'gpt', 'deepseek', 'deepseek'])
        self.assertEqual([r['cached_tokens'] for r in calls], [0, 0, 3, 17])
        self.assertEqual(len(self.rows('classification_tool_calls')), 1)
        self.assertEqual(len(self.rows()), 1)

    def test_image_outage_never_reaches_deepseek(self):
        chat = Mock(side_effect=RouterError('transient'))
        runtime = self.runtime(chat, hosted=True)
        job = self.job(image=True)
        with patch('tg_triage.router_transport.deepseek_chat') as hosted:
            self.assertEqual(runtime.decide(job), (None, 1, 'infra'))
            self.assertEqual(runtime.decide(job), (None, None, 'infra'))
        hosted.assert_not_called()
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(runtime.attempts.status(job['fingerprint']), 'WAIT_INFRA')
        row, = self.rows()
        self.assertEqual(row['final_state'], 'HOLD')
        self.assertIsNotNone(row['retry_at'])
        self.assertEqual(chat.call_args.args[1][1]['content'][-1]['type'], 'image_url')

    def test_owner_get_ticket_evidence_qualifies_at_append_threshold(self):
        tool = ToolCall('ticket', 'get_ticket', '{"repo":"sandbox/triage","number":120}')
        append = reply(state='APPEND', rule='R6', target=dict(number=120, basis='search'),
                       relation='duplicate', confidence=.8)
        chat = Mock(side_effect=[reply(calls=[tool]), append])
        with patch('tg_triage.assistant_github.GitHubReader.get_ticket',
                   return_value=dict(repo='sandbox/triage', number=120, state='open')):
            groups, _, outcome = self.runtime(chat).decide(self.job())
        self.assertEqual(outcome, 'done')
        self.assertEqual((groups[0].state, groups[0].target['number']), ('APPEND', 120))

    def test_other_repo_number_and_collision_cannot_prove_append_or_refs(self):
        repos = ('other/repo', 'sandbox/triage')
        for observed in (repos[:1], repos):
            with self.subTest(observed=observed):
                tools = [ToolCall(str(i), 'get_ticket', json.dumps(dict(repo=repo, number=120)))
                         for i, repo in enumerate(observed)]
                tickets = [dict(repo=repo, number=120, state='open') for repo in observed]
                append = reply(state='APPEND', rule='R6', relation='duplicate',
                               target=dict(number=120, basis='search'), refs=[dict(number=120)])
                chat = Mock(side_effect=[reply(calls=tools), append, append, append, append])
                with patch('tg_triage.assistant_github.GitHubReader.get_ticket', side_effect=tickets):
                    groups, run_id, outcome = self.runtime(chat).decide(self.job())
                self.assertEqual((groups, outcome), (None, 'guard'))
                row = self.store.connection.execute(
                    'SELECT * FROM classification_runs WHERE run_id=?', (run_id,)).fetchone()
                self.assertEqual(row['decision_rule'], 'guard_repo')
                group, = json.loads(row['outcomes_json'])
                self.assertEqual(group['related'], [])
                self.assertTrue(any('dropped 1' in v['detail'] for v in group['violations']))

    def test_guard_repairs_once_then_next_link_and_holds(self):
        unproven = reply(state='APPEND', rule='R6', target=dict(number=17, basis='search'),
                         relation='duplicate')
        chat = Mock(side_effect=[unproven, unproven, RouterError('transient')])
        runtime = self.runtime(chat)
        job = self.job()
        # Only results from the ToolBox of this run can prove a search target.
        job['guard_ctx'] = replace(job['guard_ctx'], tool_targets={17: 'open'})
        self.assertEqual(runtime.decide(job), (None, 1, 'guard'))
        self.assertEqual(runtime.attempts.status(job['fingerprint']), 'DONE')
        self.assertEqual([c.kwargs['tool_choice'] for c in chat.call_args_list], ['auto', 'none', 'auto'])
        self.assertIn('guard_target', chat.call_args_list[1].args[1][-1]['content'])
        self.assert_prefixes(chat.call_args_list)
        row, = self.rows()
        self.assertEqual((row['final_state'], row['error_class']), ('HOLD', 'guard_target'))
        group, = json.loads(row['outcomes_json'])
        self.assertEqual(group['violations'][0]['guard'], 'guard_target')
        self.assertEqual(group['rule_text'], 'R6 nguyên văn')

    def test_terminal_provenance_completes_half_open_probe_without_fallback(self):
        chat = Mock(return_value=reply(state='ANALYZE'))
        runtime = self.runtime(chat)
        breaker = runtime.breakers['gemini']
        for _ in range(3):
            breaker.record_failure('transient')
        self.clock.advance(900)
        self.assertEqual(breaker.state, 'half_open')
        self.assertEqual(runtime.decide(self.job()), (None, 1, 'guard'))
        self.assertEqual(breaker.state, 'closed')
        chat.assert_called_once()

    def test_forced_request_keeps_tools_and_disables_calls(self):
        tool = ToolCall('ticket', 'get_ticket', '{"repo":"sandbox/triage","number":17}')
        chat = Mock(side_effect=[reply(calls=[tool, tool]), reply()])
        with patch('tg_triage.assistant_github.GitHubReader.get_ticket',
                   return_value=dict(repo='sandbox/triage', number=17, state='closed')):
            self.assertEqual(self.runtime(chat).decide(self.job())[2], 'done')
        self.assert_prefixes(chat.call_args_list)
        self.assertEqual(chat.call_args.kwargs['tool_choice'], 'none')

    def test_propose_and_non_canary_cannot_call_decide(self):
        chat = Mock()
        for settings in ({'authority': 'propose'}, {'decide_chat_ids': (-99,)}, {'mode': 'shadow'}):
            with self.assertRaisesRegex(ValueError, 'not enabled'):
                self.runtime(chat, **settings).decide(self.job())
        chat.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_propose_wire_bytes_match_028e2cb(self):
        call = propose.tool()
        chat = propose.Chat(propose.reply(calls=(call, call)), propose.reply('invalid'), propose.valid())
        fixture = propose.AssistantTurnTests()
        fixture.setUp()
        extraction_schema = RulesSnapshot.extraction_schema

        def fixed_schema(snapshot):
            # Stabilize the fixture across processes. The legacy schema builds keys from a set.
            return json.loads(json.dumps(extraction_schema(snapshot), sort_keys=True))

        with patch.object(RulesSnapshot, 'extraction_schema', fixed_schema):
            result = fixture.run_chat(chat)
        self.assertEqual(result.outcome, 'ok')
        serialized = json.dumps({'calls': chat.calls, 'proposal': result.proposal}, sort_keys=True)
        self.assertEqual(hashlib.sha256(serialized.encode()).hexdigest(),
                         '0ee67f732b5a69cd03acef7d0e178017fbf6f9f07179f4029e4f4b3bfba0d774')

    def test_model_error_retries_and_escalates(self):
        chat = Mock(return_value=replace(reply(), content='malformed'))
        runtime = self.runtime(chat)
        job = self.job()
        for number, advance in enumerate((0, 120, 600), 1):
            self.clock.advance(advance)
            job['deadline'] = self.clock() + 180
            self.assertEqual(runtime.decide(job), (None, number, 'model_error'))
        self.assertEqual(runtime.attempts.status(job['fingerprint']), 'ESCALATED')
        self.assertEqual(self.rows()[-1]['final_state'], 'ESCALATED')
        self.assertEqual(chat.call_count, 12)

    def test_transport_serializes_disabled_choice_and_deepseek_cache_usage(self):
        envelope = dict(choices=[dict(message=dict(content='{}'), finish_reason='stop')],
                        usage=dict(prompt_tokens=40, completion_tokens=3, prompt_cache_hit_tokens=37))
        tools = ({'type': 'function', 'function': {'name': 'example'}},)
        opener = Opener(Response(envelope))
        router_transport.chat('http://localhost:20128/v1', 'test', 'model', [], tools=tools,
                              tool_choice='none', timeout=1, opener=opener)
        request, _ = opener.requests[0]
        self.assertEqual(json.loads(request.data)['tool_choice'], 'none')
        self.assertEqual(json.loads(request.data)['tools'], list(tools))

        connection = Mock()
        response = io.BytesIO(json.dumps(envelope).encode())
        response.status = 200
        connection.getresponse.return_value = response
        with patch('tg_triage.thinking_transport._addresses', return_value=['1.1.1.1']), \
             patch('tg_triage.thinking_transport.DeadlineHTTPS', return_value=connection):
            result = router_transport.deepseek_chat('https://api.deepseek.com', 'test', 'model', [],
                tools=tools, tool_choice='none', timeout=5, max_output=2048)
        self.assertEqual(result.cached_tokens, 37)
        _, path, body, _ = connection.request.call_args.args
        self.assertEqual(path, '/chat/completions')
        self.assertEqual(json.loads(body)['tools'], list(tools))
        self.assertEqual(json.loads(body)['tool_choice'], 'none')
