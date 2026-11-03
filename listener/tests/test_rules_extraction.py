import json
import unittest
from types import SimpleNamespace

from tg_triage.rules_extraction import parse_facts, parse_grouped_facts, partition_and_extract
from tg_triage.thinking import request_text_bytes
from tests.helpers import message
from tests.test_rules_bootstrap import RulesBootstrapTests


class RulesExtractionTests(RulesBootstrapTests):
    def test_bad_schema_is_rejected_and_unknown_fields_are_discarded(self):
        snapshot = self.snapshot()
        for text in ('null', '{"expected":null}', '{"object":{"value":"POS","source_ids":[true]}}', '{"object":{},"object":{}}'):
            with self.subTest(text=text): self.assertIsNone(parse_facts(text, snapshot))
        self.assertEqual(parse_facts('{"extra":"ignored"}', snapshot), {})

    def test_grouped_facts_validate_full_partition_and_group_sources(self):
        snapshot = self.snapshot()
        class Provider:
            settings = SimpleNamespace(model='test')
            think = False
            def _ask(self, body, parse, attempts):
                self.body = body
                return parse('{"groups":[{"message_ids":[1],"facts":{"object":{"value":"POS","source_ids":[1]}}},{"message_ids":[2],"facts":{}}]}')
        provider = Provider()
        self.assertEqual(len(partition_and_extract(provider, snapshot, [message(1),message(2)])), 2)
        self.assertLessEqual(request_text_bytes(provider.body), 8000)
        context = json.loads(provider.body['messages'][0]['content'].split('\n',1)[1])
        self.assertTrue(all(set(row) == {'Tin (rút gọn)', 'intent (model)', 'Dữ kiện'} for row in context['examples']))
        self.assertNotIn('generic_values', str(context))
        self.assertEqual(json.loads(provider.body['messages'][1]['content'])['source_ids'], [1,2])
        sources=json.loads(provider.body['messages'][1]['content'])['sources']
        self.assertTrue(all('sender_ref' in row and row['text_truncated'] is False for row in sources))
        self.assertIsNone(partition_and_extract(provider, snapshot, [message(1,text='x' * 12000)]))
        for payload in ('{"groups":[{"message_ids":[1],"facts":{}}]}',
                        '{"groups":[{"message_ids":[1],"facts":{}},{"message_ids":[1,2],"facts":{}}]}'):
            with self.subTest(payload=payload): self.assertIsNone(parse_grouped_facts(payload,snapshot,[1,2]))
        foreign='{"groups":[{"message_ids":[1,2],"facts":{"object":{"value":"POS","source_ids":[3]}}}]}'
        self.assertEqual(parse_grouped_facts(foreign,snapshot,[1,2])[0][1],{})

    def test_extended_seven_sources_five_pixels_fit_real_provider_request_without_losing_evidence(self):
        import base64
        from pathlib import Path
        from dataclasses import replace
        from unittest.mock import patch
        from tg_triage.decision_rules import RulesSnapshot
        from tg_triage.provider import OllamaProvider
        from tg_triage.store import Store
        from tg_triage.thinking import CURRENT,Turn
        from tests.helpers import config,FakeHTTP,PNG
        from tests.test_rules_extensions import ExtensionRulesTests
        root=Path(__file__).resolve().parents[1]
        snapshot=RulesSnapshot(root/'rules',root/'templates/github-ticket',extensions=True)
        members=[message(9400+i,text='Máy in POS in ra giấy trắng' if i==0 else '',has_image=True,
                         reply_to_id=9400 if i else None,is_forward=i==6) for i in range(7)]
        pixels=self.root/'source.png';pixels.write_bytes(PNG)
        images={m.message_id:pixels for m in members[:5]}
        value={'groups':[{'message_ids':[m.message_id for m in members],'facts':{
            'intent':{'value':'bug','source_ids':[9400]},'object':{'value':'Máy in POS','source_ids':[9400]},
            'observed':{'value':'in ra giấy trắng','source_ids':[9400]}}}]}
        wire=FakeHTTP({'message':{'content':json.dumps(value)}})
        provider=OllamaProvider(config().ollama,opener=wire,think=False)
        store=Store(':memory:');self.addCleanup(store.close);turn=Turn(store,'p8:media','synthetic',9400)
        token=CURRENT.set(turn)
        try:
            with patch('socket.create_connection',side_effect=AssertionError('network forbidden')):
                result=partition_and_extract(provider,snapshot,members,images=images)
        finally:CURRENT.reset(token)
        self.assertIsNotNone(result)
        self.assertEqual(1,len(wire.calls));self.assertEqual(1,turn.calls);self.assertEqual(2,turn.max_calls)
        body=json.loads(wire.calls[0][0].data)
        self.assertLessEqual(request_text_bytes(body),8000)
        packet=json.loads(body['messages'][1]['content'])
        self.assertEqual([m.message_id for m in members],packet['source_ids'])
        self.assertEqual([m.message_id for m in members[:5]],packet['images_order'])
        self.assertEqual([m.reply_to_id for m in members],[row['reply_to_id'] for row in packet['sources']])
        self.assertEqual([m.is_forward for m in members],[row['is_forward'] for row in packet['sources']])
        self.assertEqual([m.text for m in members],[row['text'] for row in packet['sources']])
        self.assertEqual([True]*5+[False]*2,[row['pixels_available'] for row in packet['sources']])
        self.assertEqual([PNG]*5,[base64.b64decode(v) for v in body['messages'][1]['images']])
        self.assertEqual(snapshot.extraction_schema(),body['format']['properties']['groups']['items']['properties']['facts'])
        ExtensionRulesTests().assert_wire_schema(value,body['format'])
        # Compaction never buys permission to truncate mandatory oversized source text.
        refused=FakeHTTP();provider=OllamaProvider(config().ollama,opener=refused,think=False)
        self.assertIsNone(partition_and_extract(provider,snapshot,[replace(members[0],text='x'*12000)],images={9400:pixels}))
        self.assertEqual([],refused.calls)

    def test_teaching_balances_intents_without_decisions_or_input_specific_routing(self):
        from pathlib import Path
        from tg_triage.decision_rules import RulesSnapshot
        root=Path(__file__).resolve().parents[1]
        snapshot=RulesSnapshot(root/'rules',root/'templates/github-ticket',extensions=True)
        class Capture:
            settings=SimpleNamespace(model='test');think=False
            def _ask(self,body,parse,attempts):
                self.body=body
                return parse('{"groups":[{"message_ids":[42],"facts":{}}]}')
        contexts=[]
        for text in ('Sửa nút Lưu không phản hồi','Đề nghị thêm bảng tổng hợp'):
            provider=Capture();self.assertIsNotNone(partition_and_extract(provider,snapshot,[message(42,text=text)]))
            context=json.loads(provider.body['messages'][0]['content'].split('\n',1)[1]);contexts.append(context)
            intents=[row['intent (model)'].strip('`') for row in context['examples']]
            self.assertEqual(set(snapshot.intents),set(intents))
            self.assertGreaterEqual(intents.count('task'),2);self.assertGreaterEqual(intents.count('feedback'),2)
            original=snapshot.model_context()
            self.assertEqual(original['instructions'],context['instructions'])
            self.assertEqual(original['additional_instructions'],context['additional_instructions'])
            self.assertEqual([{k:v for k,v in row.items() if k!='Ví dụ'} for row in original['fields']],context['fields'])
            self.assertTrue(all(set(row)=={'Tin (rút gọn)','intent (model)','Dữ kiện'} and row in original['examples'] for row in context['examples']))
            self.assertNotIn('generic_values',context);self.assertNotIn('decision',context)
        self.assertEqual(contexts[0],contexts[1])

    def test_question_teaching_contrasts_information_with_requested_work_without_routing_literals(self):
        from pathlib import Path
        from tg_triage.decision_rules import RulesSnapshot
        root=Path(__file__).resolve().parents[1]
        snapshot=RulesSnapshot(root/'rules',root/'templates/github-ticket',extensions=True)
        class Capture:
            settings=SimpleNamespace(model='test');think=False
            def _ask(self,body,parse,attempts):
                self.body=body
                return parse('{"groups":[{"message_ids":[42],"facts":{}}]}')
        contexts=[]
        for text in ('API dùng giao thức gì?','Thêm chức năng xuất CSV ở báo cáo'):
            provider=Capture();self.assertIsNotNone(partition_and_extract(provider,snapshot,[message(42,text=text)]))
            self.assertLessEqual(request_text_bytes(provider.body),8000)
            context=json.loads(provider.body['messages'][0]['content'].split('\n',1)[1]);contexts.append(context)
            questions=[row for row in context['examples'] if row['intent (model)']=='`question`']
            self.assertTrue(any(row['Dữ kiện']=='∅' for row in questions),'information question needs a no-work extraction example')
            self.assertTrue(any('work=' in row['Dữ kiện'] for row in questions),'requested-work question must remain represented')
            allowed=snapshot.model_context()['examples']
            self.assertTrue(all(set(row)=={'Tin (rút gọn)','intent (model)','Dữ kiện'} and row in allowed for row in questions))
            self.assertEqual(snapshot.model_instructions,context['instructions'])
            self.assertNotIn('generic_values',context);self.assertNotIn('decision',context)
        self.assertEqual(contexts[0],contexts[1])
