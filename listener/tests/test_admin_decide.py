import json
import sqlite3
import unittest

from tg_triage import admin
from tg_triage.assistant_contract import PHASE_DECIDE
from tests import test_admin_service as fixtures


class AdminDecideTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(fixtures.BASE_SCHEMA)

    def add_columns(self):
        self.db.execute("ALTER TABLE classification_runs ADD COLUMN code_precheck_json TEXT")
        self.db.execute("ALTER TABLE classification_runs ADD COLUMN log_thread_id INTEGER")

    def add_run(self, **fields):
        row = dict(created_at=fixtures.NOW, phase="early", turn_key="turn",
                   batch_fingerprint="batch", chat_id=fixtures.CHAT, message_ids="[1]",
                   id_space="message", rules_fingerprint="rules", final_state="CHAT")
        row.update(fields)
        marks = ", ".join("?" for _ in row)
        return self.db.execute(f"INSERT INTO classification_runs ({', '.join(row)}) VALUES ({marks})",
                               tuple(row.values())).lastrowid

    def test_thread_grouping_timeline_and_precheck_cards(self):
        self.add_columns()
        code_text = "| W4 | Khớp W1–W3 ⇒ CHAT |"
        assistant_text = "| R7 | chat | CHAT |"
        code = dict(handled_by_code=True, decided_rule="F3", rule_text=code_text,
                    reason="tin chỉ có link", handoff=None, checks=[
                        dict(rule="F3", name="Tin chỉ có link", match=True, why="không có chữ ngoài link")])
        handoff = dict(handled_by_code=False, decided_rule=None, rule_text=None, handoff="assistant",
                       reason="cần hiểu nội dung", checks=[
                           dict(rule="F1", name="Nhờ review PR/issue", match=False, why="không có link")])
        first = self.add_run(code_precheck_json=json.dumps(code), decision_rule="F3", ticket_ref="#42")
        self.db.execute("UPDATE classification_runs SET log_thread_id=? WHERE run_id=?", (first, first))
        second = self.add_run(log_thread_id=first, message_ids="[1,2]", created_at=fixtures.NOW + 60,
                              phase=PHASE_DECIDE, decision_rule="R7", code_precheck_json=json.dumps(handoff),
                              outcomes_json=json.dumps([dict(rule="R7", rule_text=assistant_text)]))

        runs = admin._runs(self.db, "", fixtures.NOW + 60)["runs"]
        self.assertEqual(len(runs), 1)
        run, = runs
        self.assertEqual((run["run_id"], run["latest_run_id"], run["round_count"], run["message_count"]),
                         (first, second, 2, 2))
        self.assertEqual((run["updated_at"], run["ticket_ref"], run["decided_by"]),
                         (fixtures.NOW + 60, "#42", "Trợ lý"))
        self.assertEqual(admin._runs(self.db, f"before={second}", fixtures.NOW + 60)["runs"], [])

        result = admin._format_detail(admin._detail(self.db, first, fixtures.NOW + 60))
        a, b = [round["run"] for round in result["rounds"]]
        self.assertEqual([a["run_id"], b["run_id"]], [first, second])
        self.assertEqual(a["precheck_summary"], "Code đã quyết — F3 · Tin chỉ có link: tin chỉ có link")
        self.assertEqual(b["precheck_summary"],
                         "Code không xử lý được — Lý do: cần hiểu nội dung ⇒ Gửi lên trợ lý")
        self.assertEqual(a["rule_display"], f"F3 · Tin chỉ có link · {code_text}")
        self.assertEqual(a["precheck_checks"][0]["rule_display"], a["rule_display"])
        self.assertEqual(b["rule_display"], f"R7 · Tin trò chuyện · {assistant_text}")
        self.assertEqual(b["outcome_rule_displays"], [b["rule_display"]])
        self.assertIn("không có link", b["precheck_checks"][0]["why"])
        self.assertFalse(result["truncated"])

    def test_sixty_round_thread_shows_latest_fifty_and_list_stays_paged(self):
        self.add_columns()
        first = self.add_run(ticket_ref="#42")
        rounds = [first] + [
            self.add_run(log_thread_id=first, message_ids=json.dumps([1, index]),
                         created_at=fixtures.NOW + index)
            for index in range(2, 61)
        ]
        now = fixtures.NOW + 60
        summary, = admin._runs(self.db, "", now)["runs"]
        self.assertEqual((summary["round_count"], summary["message_count"], summary["updated_at"],
                          summary["ticket_ref"]), (60, 60, now, "#42"))
        detail = admin._format_detail(admin._detail(self.db, first, now))
        self.assertTrue(detail["truncated"])
        self.assertEqual([item["run"]["run_id"] for item in detail["rounds"]], rounds[-50:])
        self.assertEqual(detail["run"]["run_id"], rounds[-1])
        for _ in range(admin.PAGE_SIZE):
            self.add_run()
        page = admin._runs(self.db, "limit=200", now)
        self.assertEqual(len(page["runs"]), admin.PAGE_SIZE)
        self.assertEqual([run["run_id"] for run in admin._runs(
            self.db, f"before={page['next_before']}", now)["runs"]], [first])

    def test_old_database_uses_one_round_per_thread(self):
        code = self.add_run(decision_rule="F3")
        assistant = self.add_run(phase="assistant_shadow")
        latest, previous = admin._runs(self.db, "", fixtures.NOW)["runs"]
        self.assertEqual((latest["run_id"], latest["round_count"], latest["decided_by"]), (assistant, 1, "Trợ lý"))
        self.assertEqual((previous["run_id"], previous["round_count"], previous["decided_by"]), (code, 1, "Code"))
        result = admin._format_detail(admin._detail(self.db, code, fixtures.NOW))
        self.assertIsNone(result["run"].get("code_precheck"))
        self.assertEqual(result["run"]["rule_display"], "F3 · Tin chỉ có link · Không có nguyên văn trong nhật ký")

    def test_sse_new_round_updates_thread_and_advances_raw_cursor(self):
        self.add_columns()
        first = self.add_run()
        unrelated = self.add_run()
        known = admin._recent_signatures(self.db, unrelated, fixtures.NOW)
        follow_up = self.add_run(log_thread_id=first, phase=PHASE_DECIDE, message_ids="[2]")

        events, known = admin._Handler._poll(None, self.db, unrelated, known, fixtures.NOW)
        ((event, summary, cursor),) = events
        self.assertEqual((event, summary["run_id"], cursor), ("run.updated", first, follow_up))
        self.assertEqual(summary["round_count"], 2)
        self.assertEqual(admin._Handler._poll(None, self.db, cursor, known, fixtures.NOW)[0], [])
        self.assertEqual([row["run_id"] for row in admin._runs(self.db, "", fixtures.NOW)["runs"]],
                         [first, unrelated])


if __name__ == "__main__":
    unittest.main()
