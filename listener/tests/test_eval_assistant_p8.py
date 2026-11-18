import contextlib
import importlib.util
import io
import json
import os
import struct
import zlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tg_triage.__main__ import _load_decision_rules, _load_assistant_pack
from tg_triage.router_transport import RouterReply, RouterError
from tg_triage.rules_extraction import partition_and_extract

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'eval-assistant-p8.py'


@unittest.skipUnless(SCRIPT.is_file(), 'scripts absent in deploy stage')
class EvalAssistantP8Tests(unittest.TestCase):
    def module(self):
        spec = importlib.util.spec_from_file_location('eval_assistant_p8', SCRIPT)
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


if __name__ == '__main__': unittest.main()
