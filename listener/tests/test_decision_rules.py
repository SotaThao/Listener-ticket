import shutil
import tempfile
import unittest
from pathlib import Path

from tg_triage.decision_rules import CodeFacts, RulesError, RulesSnapshot

ROOT = Path(__file__).resolve().parents[1]
FILES = ('states', 'facts', 'readiness', 'questions', 'channels', 'examples')


class DecisionRulesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.rules = Path(self.temp.name)
        for name in FILES:
            text = (ROOT / 'rules' / f'{name}.md').read_text(encoding='utf8')
            (self.rules / f'{name}.md').write_text(text.replace('status DRAFT', 'status ACTIVE'), encoding='utf8')
        self.templates = ROOT / 'templates' / 'github-ticket'

    def load(self):
        return RulesSnapshot(self.rules, self.templates)

    def mutate(self, name, old, new):
        path = self.rules / f'{name}.md'
        text = path.read_text(encoding='utf8')
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding='utf8')

    def facts(self, **changes):
        values = dict(command='∅', command_payload='none', text='có', extraction_ok='true', reply_target='none', topic_ids=frozenset({1,2,3}), image_ids=frozenset({2,3}), seen_image_ids=frozenset({2}), text_source_ids=frozenset({1}), document_ids=frozenset())
        values.update(changes)
        return CodeFacts(**values)

    def test_draft_fails_closed(self):
        self.mutate('facts', 'status ACTIVE', 'status DRAFT')
        with self.assertRaisesRegex(RulesError, r'facts.md:1:.*ACTIVE'): self.load()

    def test_all_examples_execute_on_load_and_snapshot_is_stable(self):
        snapshot = self.load()
        self.mutate('examples', '| R9 | — |', '| R10 | — |')
        with self.assertRaisesRegex(RulesError, r'examples.md:\d+:.*mismatch'): self.load()
        self.assertEqual(snapshot.fingerprint, snapshot.fingerprint)
        with self.assertRaises(AttributeError): snapshot.fingerprint = 'changed'
        with self.assertRaises(AttributeError): del snapshot._frozen
        with self.assertRaises(AttributeError): snapshot.__dict__

    def test_prompt_excludes_decisions_and_schema_uses_fields(self):
        snapshot = self.load()
        context = snapshot.model_context()
        self.assertEqual(set(context['examples'][0]), {'Tin (rút gọn)', 'intent (model)', 'Dữ kiện'})
        self.assertNotIn('decision', str(context))
        self.assertEqual(set(snapshot.extraction_schema()['properties']), set(snapshot.text_fields) | {'intent', 'media_ids'})

    def test_sources_and_asked_once(self):
        s = self.load()
        model = {'intent': {'value': 'bug', 'source_ids': [1]}, 'object': {'value': 'POS', 'source_ids': [3]}, 'observed': {'value': 'không in được', 'source_ids': [2]}, 'extra': 'ignored'}
        result = s.evaluate(model, self.facts())
        self.assertEqual((result.state, result.rule, result.questions), ('NEED_INFO', 'R12', ('ask.object',)))
        model['object']['source_ids'] = [2]
        result = s.evaluate(model, self.facts())
        self.assertEqual((result.state, result.rule, result.present['object'].source_ids), ('READY', 'R9', (2,)))
        model['object']['source_ids'] = [9]
        result = s.evaluate(model, self.facts(), {'object': 1})
        self.assertEqual(result.questions, ('question.fallback',))
        self.assertEqual(result.asked_fields, ())

    def test_command_overrides_model_and_confidence_does_not_decide(self):
        s = self.load()
        model = {'intent': {'value': 'chat', 'source_ids': [1]}, 'object': {'value': 'POS', 'source_ids': [1]}, 'observed': {'value': 'kẹt giấy', 'source_ids': [1]}, 'confidence': 0}
        result = s.evaluate(model, self.facts(command='/bug', command_payload='text'))
        self.assertEqual((result.intent_rule, result.rule, result.state), ('I3', 'R9', 'READY'))

    def test_document_only_and_unseen_image_caption(self):
        s = self.load()
        result = s.evaluate({}, self.facts(text='không', image_ids=frozenset(), seen_image_ids=frozenset(), document_ids=frozenset({3})))
        self.assertEqual((result.state, result.rule), ('MEDIA_ONLY', 'R3'))
        model = {'intent': {'value': 'bug', 'source_ids': [3]}, 'object': {'value': 'POS', 'source_ids': [3]}, 'observed': {'value': 'kẹt giấy', 'source_ids': [3]}}
        result = s.evaluate(model, self.facts(text_source_ids=frozenset({3})))
        self.assertEqual((result.state, result.rule), ('READY', 'R9'))

    def test_corruption_is_located_and_rejected(self):
        corruptions = [
            ('readiness', '| R13 | * |', '| R13 | `∅` |'),
            ('readiness', '| R9 | * |', '| R8 | * |'),
            ('readiness', '| R9 | * |', '| R9 | `bogus` |'),
            ('facts', '| `object` | chuỗi', '| `intent` | chuỗi'),
            ('states', '## Bảng: limits', '## Bảng: states'),
            ('channels', '| `READY` | ❤️', '| `ALIEN` | ❤️'),
        ]
        for name, old, new in corruptions:
            with self.subTest(name=name, new=new):
                path = self.rules / f'{name}.md'; original = path.read_text(encoding='utf8')
                self.mutate(name, old, new)
                with self.assertRaisesRegex(RulesError, rf'{name}.md:\d+:'): self.load()
                path.write_text(original, encoding='utf8')

    def test_malformed_inputs_fail(self):
        s = self.load()
        with self.assertRaises(ValueError): s.evaluate({}, self.facts(seen_image_ids=frozenset({9})))
        with self.assertRaises(ValueError): s.evaluate({'object': {'value': 'POS', 'source_ids': ['1']}}, self.facts())
        with self.assertRaises(ValueError): s.evaluate({}, self.facts(reply_target='ticket_closed'))
        with self.assertRaises(ValueError): s.evaluate({'expected': None}, self.facts())
        for bad in ([], '', 0):
            with self.subTest(asked_counts=bad), self.assertRaises(ValueError): s.evaluate({}, self.facts(), bad)


if __name__ == '__main__': unittest.main()
