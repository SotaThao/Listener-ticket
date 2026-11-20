import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import struct
import zlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tg_triage.__main__ import _load_decision_rules, _load_assistant_pack
from tg_triage.assistant_contract import GroupDecision, PHASE_DECIDE
from tg_triage.router_transport import RouterReply, RouterError
from tg_triage.rules_extraction import partition_and_extract
from tg_triage.store import Store
from tests import helpers

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'eval-assistant-p8.py'


@unittest.skipUnless(SCRIPT.is_file(), 'scripts absent in deploy stage')
class EvalAssistantP8Tests(unittest.TestCase):
    def module(self, filename=SCRIPT.name):
        spec = importlib.util.spec_from_file_location(SCRIPT.with_name(filename).stem.replace('-', '_'),
                                                     SCRIPT.with_name(filename))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_reuses_all_27_parsed_canonical_examples(self):
        module = self.module()
        rows = module.cases(_load_decision_rules())
        self.assertEqual([r.cells['#'] for r in rows], ['X' + str(n) for n in range(1, 28)])
        self.assertEqual(len(module.code_facts(rows[1]).seen_image_ids), 1)
        self.assertEqual(module.template('READY', 'task'), 'task.md')
        self.assertIsNone(module.template('HOLD', 'bug'))
        self.assertEqual(module.AssistantConfig.models,
                         ('ag/gemini-3.8-flash-medium', 'cx/gpt-6.1-sol'))

    def test_all_media_sources_captions_pixels_and_ids_match_canonical_table(self):
        module = self.module()
        snapshot = _load_decision_rules()
        expected = {'X2': ('Invalid username or password', [2], []),
                    'X3': ('Salon information & visibility', [2], []),
                    'X8': ('', [2], []),
                    'X13': ('COMMON_VALIDATION_ERROR', [2], []),
                    'X20': ('', [2, 3, 4, 5, 6], [7, 8]),
                    'X21': ('', [], [2])}
        found = set()
        with tempfile.TemporaryDirectory() as directory:
            for row in module.cases(snapshot):
                if row.cells['#'] not in expected: continue
                case = row.cells['#']; found.add(case)
                with self.subTest(case=case):
                    description, seen, unseen = expected[case]
                    sources, images, fixture = module.build_sources(row, 1, directory)
                    facts = module.code_facts(row)
                    self.assertEqual([m.message_id for m in sources], sorted(facts.topic_ids))
                    self.assertEqual({m.message_id for m in sources if m.has_image}, set(seen + unseen))
                    self.assertEqual(set(images), set(seen))
                    self.assertEqual({m.sender_id for m in sources}, {1})
                    for m in sources:
                        if m.message_id in seen:
                            self.assertIn(description, m.text)
                        if m.message_id in unseen: self.assertEqual(m.text, '')
                    bodies = []
                    partition_and_extract(module._BodyProvider(), snapshot, sources, images=images, body_out=bodies)
                    packet = json.loads(bodies[0]['messages'][1]['content'])
                    self.assertEqual([m['message_id'] for m in packet['sources'] if m['pixels_available']], seen)
                    self.assertEqual(packet['images_order'], seen)
                    self.assertEqual(fixture['seen_ids'], seen)
                    self.assertEqual(fixture['unseen_ids'], unseen)
                    if case == 'X21':
                        self.assertNotIn('màn thanh toán', json.dumps(packet, ensure_ascii=False))
                    if case == 'X8':
                        self.assertEqual(len(sources), 1)
                        self.assertEqual(sources[0].text, '')
                    if case == 'X20':
                        self.assertEqual(sources[0].text, 'Máy in POS in ra giấy trắng')
                        self.assertTrue(all(not m.text for m in sources if m.has_image))
                        self.assertEqual(len(sources), 8)
                    for path in images.values():
                        data = path.read_bytes()
                        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
                        offset = 8; compressed = b''
                        while offset < len(data):
                            size = struct.unpack('!I', data[offset:offset+4])[0]
                            kind = data[offset+4:offset+8]; payload = data[offset+8:offset+8+size]
                            self.assertEqual(struct.unpack('!I', data[offset+8+size:offset+12+size])[0], zlib.crc32(kind + payload))
                            if kind == b'IDAT': compressed += payload
                            offset += size + 12
                        self.assertEqual(len(zlib.decompress(compressed)), 4)
        self.assertEqual(found, set(expected))

    def test_media_description_is_narrative_only_and_missing_description_fails_loudly(self):
        module = self.module()
        from types import SimpleNamespace
        def row(text, facts='∅', case='X28'):
            return SimpleNamespace(cells={'#': case, 'Tin (rút gọn)': text, 'Dữ kiện': facts})
        self.assertEqual(module.media_description(row('“text + inside” + Ảnh “A + B”')), 'A + B')
        with self.assertRaisesRegex(module.FixtureError, 'X28: seen image lacks narrative description'):
            module.media_description(row('"POS" + 7 ảnh (5 đã xem, 2 chưa xem)',
                'object=secret expected fact; observed=answer key'))
        self.assertEqual(module.media_description(row('"Máy in POS in ra giấy trắng" + 7 ảnh (5 đã xem, 2 chưa xem)',
            'object=answer key must not be read', case='X20')), '')
        buffer = io.StringIO()
        with patch.object(module, 'run_eval', side_effect=module.FixtureError('P8 fixture X28: seen image lacks narrative description')), contextlib.redirect_stderr(buffer):
            self.assertEqual(module.main(['--router-url', 'http://127.0.0.1:20128/v1', '--key-file', '/abs/key', '--output', 'unused']), 2)
        self.assertIn('X28', buffer.getvalue())

    def test_fake_chat_temp_db_jsonl_key_never_printed(self):
        module = self.module()
        calls = []
        def chat(model, messages, **kwargs):
            calls.append(model)
            data = json.loads(messages[1]['content'].split('UNTRUSTED BATCH DATA:\n')[1])
            ids = [r['message_id'] for r in data['sources']]
            proposal = {'groups': [{'message_ids': ids, 'facts': {
                name: {'value': value, 'source_ids': [1]} for name, value in
                (('intent', 'bug'), ('object', 'máy in POS'), ('observed', 'không in được receipt'))}}]}
            return RouterReply('```json\n' + json.dumps(proposal) + '\n```', (), 'stop', 5, 6, None, 0, model, 1)
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key'; key.write_text('never-print-this-key', encoding='utf8')
            os.chmod(key, 0o600)
            output = Path(directory) / 'p8.jsonl'
            buffer = io.StringIO()
            snapshot = _load_decision_rules()
            with patch.object(module, 'cases', return_value=(module.cases(snapshot)[0],)), contextlib.redirect_stdout(buffer):
                rows = module.run_eval(router_url='http://127.0.0.1:20128/v1', key_file=key,
                                       output=output, chat=chat)
            self.assertEqual(len(calls), 1)
            self.assertTrue(rows[0]['passed'])
            self.assertEqual(rows[0]['actual_template'], 'bug.md')
            self.assertEqual(rows[0]['proposal_link'], 'gemini')
            self.assertEqual(rows[0]['calls'][0]['link'], 'gemini')
            self.assertGreaterEqual(rows[0]['calls'][0]['latency_ms'], 0)
            self.assertIsNone(rows[0]['links']['gemini']['error_class'])
            self.assertEqual(json.loads(output.read_text(encoding='utf8')), rows[0])
            self.assertIn('1/1', buffer.getvalue())
            self.assertNotIn('never-print-this-key', buffer.getvalue() + output.read_text(encoding='utf8'))

    def test_schema_diagnostics_classify_without_recording_reply_content(self):
        module = self.module()
        row = module.cases(_load_decision_rules())[0]
        replies = iter(['```json\n{private-message-text\n```', 'private-message-text: {}', '{}'])
        def chat(model, messages, **kwargs):
            return RouterReply(next(replies), (), 'stop', 5, 6, None, 0, model, 1)
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key'; key.write_text('never-print-key', encoding='utf8'); os.chmod(key, 0o600)
            output = Path(directory) / 'p8.jsonl'
            with patch.object(module, 'cases', return_value=(row,)), contextlib.redirect_stdout(io.StringIO()):
                result, = module.run_eval(router_url='http://127.0.0.1:20128/v1', key_file=key, output=output, chat=chat)
            self.assertEqual([c['schema_error_kind'] for c in result['calls']], ['fenced', 'prose', 'invalid_shape'])
            self.assertEqual([c['error_class'] for c in result['calls']], ['schema'] * 3)
            self.assertNotIn('private-message-text', output.read_text(encoding='utf8'))
            self.assertEqual(result['model_calls'], 3)

    def test_media_fake_chat_multimodal_diagnostics_and_two_groups_still_fail(self):
        module = self.module()
        snapshot = _load_decision_rules()
        row = next(r for r in module.cases(snapshot) if r.cells['#'] == 'X2')
        calls = []
        def chat(model, messages, **kwargs):
            content = messages[1]['content']
            self.assertIsInstance(content, list)
            packet = json.loads(content[0]['text'].split('UNTRUSTED BATCH DATA:\n')[1])
            self.assertIn('Invalid username or password', packet['sources'][1]['text'])
            self.assertTrue(packet['sources'][1]['pixels_available'])
            self.assertTrue(content[-1]['image_url']['url'].startswith('data:image/png;base64,'))
            calls.append(model)
            if len(calls) == 1: raise RouterError('transient')
            facts = {'intent': {'value': 'bug', 'source_ids': [1]},
                     'object': {'value': 'đăng nhập', 'source_ids': [1]},
                     'observed': {'value': 'báo sai mật khẩu dù nhập đúng', 'source_ids': [2]}}
            proposal = {'groups': [{'message_ids': [1, 2], 'facts': facts}]}
            if len(calls) > 2:
                proposal = {'groups': [{'message_ids': [1], 'facts': {}}, {'message_ids': [2], 'facts': {}}]}
            return RouterReply(json.dumps(proposal), (), 'stop', 5, 6, None, 0, model, 1)
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key'; key.write_text('never-print-media-key', encoding='utf8'); os.chmod(key, 0o600)
            output = Path(directory) / 'p8.jsonl'
            with patch.object(module, 'cases', return_value=(row, row)), contextlib.redirect_stdout(io.StringIO()):
                results = module.run_eval(router_url='http://127.0.0.1:20128/v1', key_file=key, output=output, chat=chat)
            self.assertTrue(results[0]['passed'])
            self.assertEqual(results[0]['proposal_link'], 'gpt')
            self.assertEqual(results[0]['fallback_reason'], 'transient')
            self.assertEqual(results[0]['links']['gemini']['error_class'], 'transient')
            self.assertEqual(results[0]['links']['gemini']['fallback_reason'], 'transient')
            self.assertEqual(len(results[0]['calls']), 2)
            # Second case must actually execute, not hit fingerprint dedupe.
            self.assertFalse(results[1]['passed'])
            self.assertNotIn('never-print-media-key', output.read_text(encoding='utf8'))

    def test_matching_state_and_template_with_two_groups_still_fails(self):
        module = self.module()
        snapshot = _load_decision_rules()
        row = next(r for r in module.cases(snapshot) if r.cells['#'] == 'X21')
        def chat(model, messages, **kwargs):
            self.assertIsInstance(messages[1]['content'], str)  # Unseen: no image_url.
            packet = json.loads(messages[1]['content'].split('UNTRUSTED BATCH DATA:\n')[1])
            self.assertFalse(packet['sources'][1]['pixels_available'])
            self.assertEqual(packet['sources'][1]['text'], '')
            proposal = {'groups': [{'message_ids': [1], 'facts': {}}, {'message_ids': [2], 'facts': {}}]}
            return RouterReply(json.dumps(proposal), (), 'stop', 5, 6, None, 0, model, 1)
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key'; key.write_text('never-print-key', encoding='utf8'); os.chmod(key, 0o600)
            with patch.object(module, 'cases', return_value=(row,)), contextlib.redirect_stdout(io.StringIO()):
                result, = module.run_eval(router_url='http://127.0.0.1:20128/v1', key_file=key,
                                          output=Path(directory) / 'p8.jsonl', chat=chat)
            self.assertEqual(result['expected_state'], result['actual_state'])
            self.assertEqual(result['expected_template'], result['actual_template'])
            self.assertFalse(result['passed'])

    def test_error_output_never_contains_exception_secret(self):
        module = self.module()
        buffer = io.StringIO()
        with patch.object(module, 'run_eval', side_effect=ValueError('secret-key')), contextlib.redirect_stderr(buffer):
            self.assertEqual(module.main(['--router-url', 'http://127.0.0.1:20128/v1',
                                         '--key-file', '/abs/key', '--output', 'unused']), 2)
        self.assertNotIn('secret-key', buffer.getvalue())

    def test_decide_compares_directly_and_uses_audit_metrics(self):
        module = self.module()
        snapshot = _load_decision_rules()
        case = module.cases(snapshot)[0]
        jobs = []

        def factory(config, store, pack, *, chat):
            self.assertEqual((config.mode, config.assistant.authority), ('dry-run', 'decide'))
            self.assertEqual(config.assistant.pack_mode, 'core')
            self.assertIn('RULE INDEX', pack)
            columns = {row['name'] for row in store.connection.execute('PRAGMA table_info(classification_runs)')}
            with store.connection:
                if 'log_thread_id' not in columns:
                    store.connection.execute('ALTER TABLE classification_runs ADD COLUMN log_thread_id INTEGER')
                if 'code_precheck_json' not in columns:
                    store.connection.execute('ALTER TABLE classification_runs ADD COLUMN code_precheck_json TEXT')

            class FakeRuntime:
                def decide(self, job):
                    jobs.append(job)
                    with store.connection:
                        run_id = store.connection.execute(
                            """INSERT INTO classification_runs
                               (created_at, turn_key, phase, batch_fingerprint, chat_id, message_ids,
                                id_space, rules_fingerprint, outcomes_json)
                               VALUES (0, 'test', ?, ?, ?, '[1]', 'message', 'rules', ?)""",
                            (PHASE_DECIDE, job['fingerprint'], job['chat_id'],
                             '[{"violations":[{"guard":"guard_repo"}]}]' if len(jobs) == 2 else '[]')
                        ).lastrowid
                        store.connection.execute(
                            """INSERT INTO classification_model_calls
                               (run_id, seq, link, started_at, prompt_tokens, completion_tokens, cached_tokens)
                               VALUES (?, 1, 'gemini', 0, 20, 4, 10)""", (run_id,))
                        store.connection.execute(
                            """INSERT INTO classification_tool_calls (run_id,seq,tool,result_json)
                               VALUES (?,1,'get_rule',?)""",
                            (run_id, json.dumps(dict(ok=True, untrusted_data=(
                                dict(rule_id='facts:object') if len(jobs) == 1 else dict(rules=[
                                    dict(ok=True, rule_id='R9'),
                                    dict(ok=False, rule_id='unknown'),
                                    dict(ok=True, rule_id='R10'),
                                ]))))))
                    if len(jobs) == 2:
                        return None, run_id, 'guard'
                    decision = GroupDecision((1,), 'READY', 'R9', 'bug', None, None, None,
                                             None, (), .9, '', job['chat_id'])
                    return [decision], run_id, 'done'

            return FakeRuntime()

        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key'
            key.write_text('fixture-key', encoding='utf-8')
            key.chmod(0o600)
            output = Path(directory) / 'decide.jsonl'
            recorded = io.StringIO()
            run_eval = module.run_eval
            with (patch.object(module, 'cases', return_value=(case, case)),
                  patch.object(type(snapshot), 'evaluate', side_effect=AssertionError('legacy evaluator used')),
                  patch.object(module, '_load_decision_rules', return_value=snapshot),
                  patch.object(module, 'run_eval',
                               side_effect=lambda **options: run_eval(**options, runtime_factory=factory)),
                  contextlib.redirect_stdout(recorded)):
                self.assertEqual(module.main(['--router-url', 'http://127.0.0.1:20128/v1',
                                              '--key-file', str(key), '--output', str(output),
                                              '--authority', 'decide', '--pack-mode', 'core']), 1)
            first, second = map(json.loads, output.read_text(encoding='utf-8').splitlines())
        self.assertTrue(first['passed'])
        self.assertTrue(first['ticket_agrees'])
        self.assertEqual(first['actual_template'], 'bug.md')
        self.assertFalse(second['passed'])
        self.assertEqual(second['guard_violations'], ['guard_repo'])
        self.assertEqual((first['tokens_in'], first['tokens_out'], first['cached_tokens']), (20, 4, 10))
        self.assertEqual((first['pack_mode'], first['get_rule_calls'], first['get_rule_ids']),
                         ('core', 1, ['facts:object']))
        self.assertEqual((second['get_rule_calls'], second['get_rule_ids']), (1, ['R9', 'R10']))
        self.assertTrue(first['creation_agrees'])
        self.assertEqual(jobs[0]['guard_ctx'].message_ids, frozenset({1}))
        self.assertIn('Ticket agreement (READY/APPEND) 1/2', recorded.getvalue())
        self.assertIn('20/40 (50.0%)', recorded.getvalue())

    def test_replay_refuses_the_live_path_before_opening_any_file(self):
        replay = self.module('replay-assistant-decide.py')
        error = io.StringIO()
        with patch.object(replay.sqlite3, 'connect') as connect, contextlib.redirect_stderr(error):
            status = replay.main(['--db', '/var/lib/agentfleet/tg/db.sqlite3', '--config', 'unused',
                                  '--out', 'unused'])
        connect.assert_not_called()
        self.assertEqual(status, 2)
        self.assertIn('refuses the live database', error.getvalue())

    def test_replay_reconstructs_batches_without_writing_to_copy(self):
        replay = self.module('replay-assistant-decide.py')
        requested = []

        def factory(config, store, pack):
            self.assertEqual(config.mode, 'dry-run')
            self.assertEqual(config.assistant.pack_mode, 'core')
            self.assertIn('RULE INDEX', pack)
            self.assertEqual(config.telegram.chat_ids, ())
            self.assertFalse(config.gh_token)
            self.assertIsNone(config.assistant.github_token)
            self.assertNotEqual(Path(config.db_path), source)

            class FakeRuntime:
                def decide(self, job):
                    requested.append(tuple(message.message_id for message in job['sources']))
                    decision = GroupDecision(requested[-1], 'APPEND', 'R6', None, None,
                                             {'number': 42, 'repo': 'sandbox/triage'}, None,
                                             None, (), .9, '', job['chat_id'])
                    return [decision], None, 'done'

            return FakeRuntime()

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'copy.sqlite3'
            output = Path(directory) / 'result.jsonl'
            store = Store(str(source))
            try:
                older, latest = helpers.message(10), helpers.message(11, text='xin chào', reply_to_id=10)
                for message in older, latest:
                    store.enqueue(message)
                    with store.connection:
                        store.connection.execute(
                            """INSERT INTO classification_runs
                               (created_at, turn_key, phase, batch_fingerprint, chat_id, message_ids,
                                id_space, rules_fingerprint, final_state, ticket_ref)
                               VALUES (0, 'test', 'extract', 'batch', ?, ?, 'message', 'rules', 'APPEND', ?)""",
                            (message.chat_id, json.dumps([message.message_id]),
                             'https://github.com/sandbox/triage/issues/42'))
            finally:
                store.close()
            original = source.read_bytes(), source.stat().st_mtime_ns
            report = io.StringIO()
            run_replay = replay.run_replay
            with (patch.object(replay.p8, 'offline_config', return_value=helpers.config()),
                  contextlib.redirect_stdout(report)):
                with patch.object(replay, 'run_replay',
                                  side_effect=lambda **options: run_replay(**options, runtime_factory=factory)):
                    self.assertEqual(replay.main(['--db', str(source), '--config', 'unused.toml', '--limit', '1',
                                                  '--out', str(output), '--pack-mode', 'core']), 0)
                row = json.loads(output.read_text(encoding='utf-8'))
                with self.assertRaises(replay.ReplayInputError):
                    replay.run_replay(db=source, config='unused.toml', out=source, runtime_factory=factory)
            self.assertEqual((source.read_bytes(), source.stat().st_mtime_ns), original)
            self.assertEqual({item.name for item in source.parent.iterdir()}, {source.name, output.name})
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), row)
        self.assertEqual(requested, [(11,)])
        self.assertTrue(row['passed'])
        self.assertTrue(row['ticket_ref_agrees'])
        self.assertTrue(row['creation_agrees'])
        self.assertIn('APPEND\t1', report.getvalue())

    def test_probe_repeats_each_link_and_never_prints_key_even_on_error(self):
        probe = self.module('probe-prompt-cache.py')
        secret = 'probe-secret-value'
        calls = []

        def chat(url, key, model, messages, **kwargs):
            self.assertEqual(key, secret)
            calls.append((model, json.dumps(messages), kwargs['tools'], kwargs['tool_choice'], kwargs['timeout']))
            if len(calls) == 4:
                raise ValueError(secret)
            return RouterReply(secret, (), 'stop', 20, 4, None, 10, model, 17)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'key'
            path.write_text(secret, encoding='utf-8')
            path.chmod(0o600)
            output, error = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
                status = probe.run_probe(router_url='http://127.0.0.1:20128/v1', key_file=path, chat=chat)
        self.assertEqual(status, 1)
        self.assertEqual(calls[0][:4], calls[1][:4])
        self.assertEqual(calls[2][:4], calls[3][:4])
        self.assertEqual(len(calls[0][2]), 7)
        system = json.loads(calls[0][1])[0]['content']
        pack = probe._load_assistant_pack()
        self.assertTrue(system.startswith(pack.text))
        self.assertIn('decide/v1', system)
        self.assertTrue(all(0 < call[4] <= 60 for call in calls))
        self.assertIn('prompt_tokens', output.getvalue())
        self.assertIn('request failed', output.getvalue())
        self.assertNotIn(secret, output.getvalue() + error.getvalue())

    def test_probe_cli_compares_full_and_core_prefixes_offline(self):
        probe = self.module('probe-prompt-cache.py')
        calls = []

        def chat(url, key, model, messages, **kwargs):
            system = messages[0]['content']
            calls.append(system)
            content = json.dumps(dict(contract='decide/v1', groups=[dict(state='CHAT', template=None)]))
            return RouterReply(content, (), 'stop', 10 if 'RULE INDEX' in system else 20,
                               4, None, 5, model, 4)

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / 'key'
            key.write_text('test-key', encoding='utf-8')
            key.chmod(0o600)
            with patch.object(probe.router_transport, 'chat', side_effect=chat), contextlib.redirect_stdout(output):
                self.assertEqual(probe.main(['--router-url', 'http://127.0.0.1:20128/v1',
                                              '--key-file', str(key), '--compare']), 0)
        self.assertEqual(len(set(calls[:4])), 1)
        self.assertEqual(len(set(calls[4:])), 1)
        self.assertNotEqual(calls[0], calls[4])
        self.assertIn('Input tokens\t80\t40', output.getvalue())
        self.assertIn('State agreement\t4/4\t4/4', output.getvalue())


if __name__ == '__main__': unittest.main()
