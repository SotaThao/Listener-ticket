import csv
import io
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path

from tg_triage.models import Decision
from tg_triage.store import COLUMNS, Store
from tests.helpers import NOW, classification, message


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "triage.sqlite3"

    def test_claim_deduplicates_all_decisions_and_preserves_text(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        decisions = ("noise", "issue", "needs_human", "dry_run_issue", "dry_run_needs_human",
                     "rate_limited", "error", "pending")
        for index, action in enumerate(decisions, 1):
            msg = message(index)
            self.assertTrue(store.claim(msg))
            store.finish(msg, Decision(action, "model", classification()))
            self.assertFalse(store.claim(msg))
            row = store.get(msg.chat_id, msg.message_id)
            self.assertEqual(row["text"], msg.text)
            self.assertEqual(row["sender_name"], msg.sender_name)
            self.assertEqual(row["decision"], action)

    def test_restart_recovers_pending_as_error_and_never_reclaims(self):
        store = Store(self.path, clock=lambda: NOW)
        msg = message()
        store.claim(msg)
        store.start_classifying(msg)
        store.reserve_github_call(msg, 20)
        store.close()
        reopened = Store(self.path, clock=lambda: NOW)
        self.addCleanup(reopened.close)
        row = reopened.get(msg.chat_id, msg.message_id)
        self.assertEqual(row["decision"], "error")
        self.assertIn("Interrupted", row["error"])
        self.assertFalse(reopened.claim(msg))

    def test_completed_results_survive_restart(self):
        store = Store(self.path)
        msg = message()
        store.claim(msg)
        store.finish(msg, Decision("issue", "rule", classification(), 42, "https://example.invalid/42"))
        store.close()
        reopened = Store(self.path)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.claim(msg))
        self.assertEqual(reopened.get(msg.chat_id, 1)["issue_number"], 42)

    def test_hour_count_uses_only_reserved_calls_and_handles_offsets(self):
        current = [NOW]
        store = Store(self.path, clock=lambda: current[0])
        self.addCleanup(store.close)
        cases = ((-61, 1), (-59, 2), (0, None), (1, 3))
        for index, (minutes, issue_number) in enumerate(cases, 1):
            current[0] = (NOW + timedelta(minutes=minutes)).astimezone(timezone(timedelta(hours=7)))
            msg = message(index)
            store.enqueue(msg)
            store.start_classifying(msg)
            if issue_number:
                store.reserve_github_call(msg, 20)
            store.finish(msg, Decision("issue" if issue_number else "dry_run_issue", "rule",
                                       classification(), issue_number))
        self.assertEqual(store.count_recent_issues(NOW), 2)
        self.assertEqual(store.count_recent_issues(NOW + timedelta(minutes=1)), 2)

    def test_different_chats_have_independent_message_keys(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        self.assertTrue(store.claim(message()))
        self.assertTrue(store.claim(message(chat_id=-999)))

    def test_export_csv_bom_columns_original_text_and_blank_owner_fields(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        msg = message(text='Dữ liệu, "có dấu"\nxuống dòng')
        store.claim(msg)
        store.finish(msg, Decision("dry_run_issue", "model", classification()))
        out = Path(self.temp.name) / "review.csv"
        store.export_csv(out)
        raw = out.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        self.assertEqual(reader.fieldnames, [*COLUMNS, "owner_label", "owner_should_create"])
        rows = list(reader)
        self.assertEqual(rows[0]["text"], msg.text)
        self.assertEqual(rows[0]["owner_label"], "")
        self.assertEqual(rows[0]["owner_should_create"], "")

    def test_empty_export_still_has_header_and_bom(self):
        store = Store(self.path)
        self.addCleanup(store.close)
        out = Path(self.temp.name) / "empty.csv"
        store.export_csv(out)
        self.assertTrue(out.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertIn("owner_label", out.read_text(encoding="utf-8-sig"))
