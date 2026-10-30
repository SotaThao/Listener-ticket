"""Blind repair/diagnostic mechanics with fake transport; no semantic-model claim."""
import copy
import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from tg_triage.classifier import Classifier
from tg_triage.document_reader import read_documents, valid_task_details
from tg_triage.ingress_model import parse_ingress
from tg_triage.pipeline import Pipeline
from tg_triage.provider import CloudProvider
from tg_triage.store import Store
from tg_triage.thinking import CURRENT, Turn, request_text_bytes
from tg_triage.trace import trace
from tests.helpers import FakeDownloader, FakeHTTP, FakeUploader, config, message, NOW
from tests.test_analysis_reply_contract import AuditedGitHub, Router
from tests.test_document_content_contract import EN, VI, doc, new_wire

SENTINEL = 'PRIVATE_MODEL_SENTINEL_7d194'
CODES = {'required', 'type', 'max_length', 'max_items', 'additional_field',
         'invalid_value', 'task_essentials', 'candidate_unknown', 'json_invalid', 'canonical_invalid'}
TYPES = {'missing', 'null', 'boolean', 'integer', 'number', 'string', 'array', 'object', 'unknown'}


class DocumentRepairContractTests(TestCase):
    def rows(self, messages=None):
        messages = messages or [doc(1)]
        return read_documents(messages, FakeDownloader({m.message_id: ((EN if m.message_id == 1 else VI).encode(),
                                                                     f'{m.message_id}.md') for m in messages}))

    def cfg(self):
        cfg = replace(config('live', window=2), autonomous_enabled=True, thinking_enabled=True)
        return replace(cfg, model=replace(cfg.model, provider='openai', name='test', think=False,
            cloud_consent=True, base_url='https://api.openai.com/v1'))

    def cloud(self):
        return CloudProvider(self.cfg())

    def store(self, path=':memory:'):
        store = Store(path)
        self.addCleanup(store.close)
        return store

    def with_turn(self, store=None, clock=None):
        store = store or self.store()
        turn = Turn(store, 'repair:test', 'root:test', 1, **({'clock': clock} if clock else {}))
        token = CURRENT.set(turn)
        self.addCleanup(CURRENT.reset, token)
        return turn

    def patches(self, outputs):
        requests = []
        results = iter(outputs)
        def complete(*args, **kwargs):
            requests.append(copy.deepcopy(args[3]))
            return next(results)
        self.enterContext(patch('tg_triage.ai_transport.read_key', return_value='fake'))
        self.enterContext(patch('tg_triage.thinking_transport.complete', side_effect=complete))
        return requests

    def assert_metadata(self, errors):
        self.assertTrue(errors)
        self.assertLessEqual(len(errors), 8)
        for row in errors:
            self.assertLessEqual(set(row), {'code', 'path', 'actual_type', 'length'})
            self.assertIn(row['code'], CODES)
            self.assertIn(row['actual_type'], TYPES)
            self.assertIsInstance(row['path'], str)
            self.assertLessEqual(len(row['path']), 180)
            self.assertNotIn(SENTINEL, row['path'])
            if 'length' in row:
                self.assertIs(type(row['length']), int)
                self.assertGreaterEqual(row['length'], 0)
                self.assertLessEqual(row['length'], 1000000)
        self.assertNotIn(SENTINEL, json.dumps(errors))

    def test_parser_diagnostics_never_echo_unknown_names_values_or_reasoning(self):
        wire = new_wire([1])
        wire['groups'][0][SENTINEL] = {'reasoning_content': SENTINEL, 'text': SENTINEL}
        errors = []
        self.assertIsNone(parse_ingress(json.dumps(wire), diagnostics=errors))
        self.assert_metadata(errors)
        self.assertIn('additional_field', [e['code'] for e in errors])

    def test_malformed_json_reports_metadata_without_raw_exception_content(self):
        errors = []
        self.assertIsNone(parse_ingress('{"' + SENTINEL + '":', diagnostics=errors))
        self.assert_metadata(errors)
        self.assertIn('json_invalid', [e['code'] for e in errors])

    def test_document_detail_diagnostics_cover_known_path_without_untrusted_key(self):
        analysis = new_wire([1])['groups'][0]['analysis']
        analysis['details'][SENTINEL] = SENTINEL
        errors = []
        self.assertFalse(valid_task_details(analysis, diagnostics=errors))
        self.assert_metadata(errors)
        self.assertIn('additional_field', [e['code'] for e in errors])

    def test_known_field_type_error_carries_type_not_invalid_value(self):
        analysis = new_wire([1])['groups'][0]['analysis']
        analysis['details']['scope_out'] = [SENTINEL]
        errors = []
        self.assertFalse(valid_task_details(analysis, diagnostics=errors))
        self.assert_metadata(errors)
        self.assertTrue(any(e['code'] == 'type' and e['path'].endswith('.scope_out')
                            and e['actual_type'] == 'array' for e in errors))

    def test_diagnostic_count_and_extreme_length_are_bounded(self):
        analysis = new_wire([1])['groups'][0]['analysis']
        analysis['details'] = {SENTINEL + str(i): SENTINEL for i in range(30)}
        analysis['details'].update(work='x' * 1000001, done=[SENTINEL] * 30)
        errors = []
        self.assertFalse(valid_task_details(analysis, diagnostics=errors))
        self.assert_metadata(errors)

    def test_valid_output_has_no_diagnostics_and_no_second_call(self):
        errors = []
        self.assertIsNotNone(parse_ingress(json.dumps(new_wire([1])), diagnostics=errors))
        self.assertEqual([], errors)
        turn = self.with_turn()
        requests = self.patches([new_wire([1])])
        self.assertIsNotNone(self.cloud().analyze_ingress([doc(1)], [], document_contents=self.rows()))
        self.assertEqual(1, len(requests))
        self.assertEqual(1, turn.calls)

    def test_invalid_then_valid_repair_keeps_full_documents_ids_and_all_caps(self):
        sources = [doc(1), doc(2)]
        bad = new_wire([1, 2])
        bad['groups'][0]['analysis']['details']['scope_out'] = [SENTINEL]
        turn = self.with_turn()
        requests = self.patches([bad, new_wire([1, 2])])
        result = self.cloud().analyze_ingress(sources, [], document_contents=self.rows(sources))
        self.assertIsNotNone(result)
        self.assertEqual(2, len(requests))
        self.assertEqual(2, turn.calls)
        self.assertNotEqual(requests[0], requests[1])
        documents = []
        for messages in requests:
            self.assertNotIn(SENTINEL, json.dumps(messages))
            self.assertLessEqual(request_text_bytes({'messages': messages}), 48 * 1024)
            base = copy.deepcopy(messages)
            found = []
            for item in base:
                if item['role'] != 'user':
                    continue
                packet = json.loads(item['content'])
                self.assertEqual([1, 2], packet['NEW_SOURCE_IDS'])
                found.extend(packet.pop('DOCUMENT_CONTENTS', []))
                item['content'] = json.dumps(packet, ensure_ascii=False, separators=(',', ':'))
            documents.append(found)
            self.assertLessEqual(request_text_bytes({'messages': base}), 8000)
        self.assertEqual(documents[0], documents[1])
        self.assertEqual([EN, VI], [row['text'] for row in documents[1]])
        first = json.dumps(requests[0], ensure_ascii=False)
        second = json.dumps(requests[1], ensure_ascii=False)
        self.assertLessEqual(len(second.encode()) - len(first.encode()), 600)

    def test_repair_under_history_pressure_preserves_current_floor_and_full_documents(self):
        sources = [replace(doc(i), text='Current document request ' * 80) for i in (100, 101)]
        history = [message(i, text=('Earlier discussion ' * 600), received_at=NOW - timedelta(minutes=1))
                   for i in range(1, 9)]
        bad = new_wire([100, 101])
        bad['groups'][0]['analysis']['details']['done'] = 'wrong shape'
        self.with_turn()
        requests = self.patches([bad, new_wire([100, 101])])
        self.assertIsNotNone(self.cloud().analyze_ingress(sources, [], read_context=history,
                                                         document_contents=self.rows(sources)))
        self.assertEqual(2, len(requests))
        seen_documents = []
        for messages in requests:
            base = copy.deepcopy(messages)
            for item in base:
                if item['role'] != 'user':
                    continue
                packet = json.loads(item['content'])
                self.assertEqual([100, 101], packet['NEW_SOURCE_IDS'])
                text_index = packet['source_columns'].index('text')
                self.assertTrue(all(len(row[text_index]) >= 30 for row in packet['sources']))
                if len(packet['READ_ONLY_CONTEXT']) < len(history):
                    self.assertTrue(packet['read_context_truncated'])
                seen_documents.append(packet.pop('DOCUMENT_CONTENTS'))
                item['content'] = json.dumps(packet, ensure_ascii=False, separators=(',', ':'))
            self.assertLessEqual(request_text_bytes({'messages': base}), 8000)
            self.assertLessEqual(request_text_bytes({'messages': messages}), 48 * 1024)
        self.assertEqual(seen_documents[0], seen_documents[1])
        self.assertEqual([100, 101], [row['message_id'] for row in seen_documents[1]])

    def test_untrusted_document_text_stays_user_data_not_repair_system_instruction(self):
        # Real downloaded bytes and their extraction proof must agree before testing the data lane.
        row = read_documents([doc(1)],FakeDownloader({1:((EN+SENTINEL).encode(),'request.md')}))[0]
        # The source is untrusted data; the host must retain it in both requests.
        bad = new_wire([1])
        bad['groups'][0]['analysis']['details']['done'] = SENTINEL
        self.with_turn()
        requests = self.patches([bad, new_wire([1])])
        self.assertIsNotNone(self.cloud().analyze_ingress([doc(1)], [], document_contents=[row]))
        self.assertEqual(2, len(requests))
        for messages in requests:
            self.assertTrue(any(SENTINEL in str(m['content']) for m in messages if m['role'] == 'user'))
            self.assertNotIn(SENTINEL, json.dumps([m for m in messages if m['role'] != 'user']))

    def test_two_invalid_outputs_are_two_attempts_without_fallback_bonus(self):
        turn = self.with_turn()
        requests = self.patches([{}, {}, new_wire([1])])
        self.assertIsNone(self.cloud().analyze_ingress([doc(1)], [], document_contents=self.rows()))
        self.assertEqual(2, len(requests))
        self.assertEqual(2, turn.calls)

    def test_repair_does_not_reset_shared_deadline(self):
        now = [0.0]
        turn = self.with_turn(clock=lambda: now[0])
        requests = []
        def slow_invalid(*args, **kwargs):
            requests.append(args[3])
            now[0] = 41.0
            return {}
        self.enterContext(patch('tg_triage.ai_transport.read_key', return_value='fake'))
        self.enterContext(patch('tg_triage.thinking_transport.complete', side_effect=slow_invalid))
        self.assertIsNone(self.cloud().analyze_ingress([doc(1)], [], document_contents=self.rows()))
        self.assertEqual(1, len(requests))
        self.assertEqual(1, turn.calls)

    def test_local_invalid_output_does_not_acquire_cloud_repair_bonus(self):
        http = FakeHTTP({'message': {'content': '{}'}})
        model = Classifier(config().ollama, opener=http)
        self.assertIsNone(model.analyze_ingress([doc(1)], [], document_contents=self.rows()))
        self.assertEqual(1, len(http.calls))

    def pipeline(self, store=None):
        return Pipeline(self.cfg(), store or self.store(), self.cloud(), AuditedGitHub(),
            downloader=FakeDownloader({1: (EN.encode(), 'one.md')}), uploader=FakeUploader())

    def test_diagnostic_telemetry_and_trace_do_not_persist_model_prose(self):
        bad = new_wire([1])
        bad['groups'][0]['analysis']['details'][SENTINEL] = {'reasoning_content': SENTINEL}
        requests = self.patches([bad, new_wire([1])])
        p = self.pipeline()
        recorded = []
        original = Turn.record
        def record(turn, *args, **kwargs):
            recorded.append(copy.deepcopy((args, kwargs)))
            return original(turn, *args, **kwargs)
        with patch.object(Turn, 'record', record):
            result = p.process(doc(1))
        self.assertEqual('issue', result.decision)
        self.assertEqual(2, len(requests))
        self.assertTrue(any(args and args[0] == 'ingress_validation' for args, kwargs in recorded))
        self.assertNotIn(SENTINEL, json.dumps(recorded))
        dump = '\n'.join(p.store.connection.iterdump())
        self.assertNotIn(SENTINEL, dump)
        self.assertIn('validation_errors', dump)
        report = trace(p.store, repo=p.config.gh_repo, chat_id=doc(1).chat_id, message_id=1)
        self.assertNotIn(SENTINEL, json.dumps(report))
        self.assertIn('validation_errors', json.dumps(report))

    def test_repaired_foreign_evidence_ids_never_create_or_borrow_readproof(self):
        repaired = new_wire([1])
        repaired['groups'][0]['analysis']['key_message_ids'] = [999]
        self.patches([{}, repaired])
        p = self.pipeline()
        self.assertEqual('pending_review', p.process(doc(1)).decision)
        self.assertEqual([], p.github.calls)

    def test_repaired_output_cannot_forge_host_readproof_or_candidate_authority(self):
        for change in ({'host_document_reads': [{'message_id': 1, 'read_status': 'read'}]},
                       {'action': 'CONTINUE', 'candidate_id': 'foreign:999'}):
            with self.subTest(change=change):
                bad = new_wire([1])
                bad['groups'][0].update(change)
                # Distinct patch contexts prevent a nested subtest from reusing a prior fake response.
                with patch('tg_triage.ai_transport.read_key', return_value='fake'), \
                     patch('tg_triage.thinking_transport.complete', side_effect=[{}, bad]) as request:
                    p = self.pipeline()
                    self.assertEqual('pending_review', p.process(doc(1)).decision)
                    self.assertEqual([], p.github.calls)
                    self.assertEqual(2, request.call_count)

    def test_repair_cannot_shrink_raw_work_done_caps_into_accepted_task(self):
        for details in ({'work': 'x' * 501, 'done': ['Done']},
                        {'work': 'Work', 'done': ['x' * 201]},
                        {'work': 'Work', 'done': ['Done'] * 11}):
            with self.subTest(details=details):
                wire = new_wire([1])
                wire['groups'][0]['analysis']['details'] = details
                with patch('tg_triage.ai_transport.read_key', return_value='fake'), \
                     patch('tg_triage.thinking_transport.complete', side_effect=[{}, wire]) as request:
                    p = self.pipeline()
                    self.assertEqual('pending_review', p.process(doc(1)).decision)
                    self.assertEqual([], p.github.calls)
                    self.assertEqual(2, request.call_count)

    def test_cached_invalid_task_is_not_repaired_rewritten_or_created(self):
        directory = self.enterContext(TemporaryDirectory())
        path = str(Path(directory) / 'pending.sqlite3')
        store = self.store(path)
        source = doc(1)
        wire = new_wire([1])
        wire['groups'][0]['analysis']['details'] = {}
        plan = parse_ingress(json.dumps(wire))[0]
        plan['host_document_reads'] = [{k: v for k, v in row.items() if k not in ('text', 'name')}
                                       for row in self.rows()]
        store.enable_autonomy()
        store.start_classifying(source)
        store.autonomy_case([source])
        store.save_autonomous_plans([source], [plan], self.cfg().gh_repo)
        before = store.autonomous_plan(source.chat_id, 1)
        store.close()
        resumed = self.pipeline(self.store(path))
        with patch('tg_triage.thinking_transport.complete') as request:
            results = resumed.close_due_cases()
        self.assertEqual(1, len(results))
        self.assertEqual('cached_document_invalid', results[0].reason)
        self.assertEqual([], resumed.github.calls)
        request.assert_not_called()
        self.assertEqual(before, resumed.store.autonomous_plan(source.chat_id, 1))
