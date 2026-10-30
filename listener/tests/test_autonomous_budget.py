"""One event budget survives autonomous routing/case/follow-up phases."""
import json
import unittest
from types import SimpleNamespace

from tg_triage.store import Store
from tg_triage.thinking import BudgetExhausted, CURRENT, decision_turn


class AutonomousBudgetTests(unittest.TestCase):
    def test_nested_phases_share_attempts_deadline_and_revision(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        from tg_triage.thinking import init_store
        init_store(store)
        pipeline = SimpleNamespace(store=store, config=SimpleNamespace(thinking_enabled=True, autonomous_enabled=True))
        body = {'messages': [{'role': 'user', 'content': 'synthetic context'}], 'format': {}}
        with decision_turn(pipeline, 'ingress:1', 'root:1', 1) as outer:
            outer.attempt(body)
            deadline = outer.deadline
            with decision_turn(pipeline, 'followup:1', 'root:1', 1) as inner:
                self.assertIs(inner, outer)
                self.assertEqual(inner.deadline, deadline)
                inner.attempt(body)
                with self.assertRaises(BudgetExhausted):
                    inner.attempt(body)
                inner.record('clarify', {'source_ids': [1]})
            self.assertIs(CURRENT.get(), outer)
            self.assertEqual(outer.calls, 2)
        self.assertIsNone(CURRENT.get())
        rows = store.connection.execute('SELECT turn_key,record FROM thinking_decisions').fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], 'ingress:1')
        self.assertEqual(json.loads(rows[0][1])['budget']['max_reads'], 6)

    def test_autonomous_reads_are_six_legacy_reads_remain_three(self):
        from tg_triage.thinking import init_store
        for enabled, bound in ((False, 3), (True, 6)):
            with self.subTest(enabled=enabled):
                store = Store(':memory:')
                self.addCleanup(store.close)
                init_store(store)
                pipeline = SimpleNamespace(store=store, config=SimpleNamespace(thinking_enabled=True, autonomous_enabled=enabled))
                with decision_turn(pipeline, 'reads', 'root', 1) as turn:
                    for _ in range(bound):
                        turn.lookup()
                    with self.assertRaises(BudgetExhausted):
                        turn.lookup()
