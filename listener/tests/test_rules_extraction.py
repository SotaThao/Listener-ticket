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
