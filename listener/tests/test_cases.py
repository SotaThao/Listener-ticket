"""Case aggregation (Assistant definition 12.1-12.6): one case, one issue. Offline, fakes only."""

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tg_triage import telegram_io
from tg_triage.models import CaseAnalysis, Decision
from tg_triage.pipeline import Pipeline
from tg_triage.store import GITHUB_AMBIGUOUS_ERROR, Store
from tests.helpers import (NOW, Clock, FakeCaseModel, FakeClassifier, FakeGitHub, at, classification,
                           config, message, ticket)
from tests.test_reactions import HEART, OK, Recorder

CHAT = -1001234567890


def msg(message_id, minutes=0, sender=1, text=None, **extra):
    text = text or f"login app nexora lần {message_id}"
    return message(message_id, text, sender_id=sender, received_at=at(minutes), **extra)


class CaseBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)

    def pipeline(self, mode="live", model=None, github=None, limit=20, window=15, max_messages=20, **extra):
        self.github = FakeGitHub() if github is None else github
        self.model = FakeCaseModel(ticket([1, 2, 3])) if model is None else model
        return Pipeline(config(mode, limit, window=window, max_messages=max_messages),
                        self.store, self.model, self.github, **extra)

    def row(self, message_id):
        return self.store.get(CHAT, message_id)

    def case_key(self, message_id):
        return self.row(message_id)["case_key"]


class GroupingTests(CaseBase):
    def test_same_sender_within_window_is_one_case_and_twenty_minutes_apart_is_two(self):
        pipeline = self.pipeline()
        for item in (msg(1, 0), msg(2, 10), msg(3, 30)):
            self.assertEqual(pipeline.process(item).decision, "case_open")
        self.assertEqual(self.case_key(1), self.case_key(2))
        self.assertEqual(self.case_key(1), f"{CHAT}:1")
        self.assertNotEqual(self.case_key(2), self.case_key(3))
        self.assertEqual(self.case_key(3), f"{CHAT}:3")
        self.assertEqual((self.row(1)["decision"], self.row(3)["decision"]), ("in_case", "in_case"))
        self.assertEqual(self.github.calls, [])

    def test_reply_joins_the_case_even_beyond_the_window(self):
        pipeline = self.pipeline()
        pipeline.process(msg(1, 0, sender=1))
        pipeline.process(msg(2, 20, sender=2, reply_to_id=1))
        self.assertEqual(self.case_key(2), self.case_key(1))
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["message_count"], 2)

    def test_message_from_another_sender_without_reply_is_a_separate_case(self):
        pipeline = self.pipeline()
        pipeline.process(msg(1, 0, sender=1))
        pipeline.process(msg(2, 1, sender=2))
        self.assertNotEqual(self.case_key(1), self.case_key(2))

    def test_unknown_sender_ids_never_group(self):
        pipeline = self.pipeline()
        pipeline.process(message(1, "login app nexora một", received_at=at(0)))
        pipeline.process(message(2, "login app nexora hai", received_at=at(1)))
        self.assertNotEqual(self.case_key(1), self.case_key(2))

    def test_forward_requires_same_sender_or_explicit_reply(self):
        pipeline = self.pipeline()
        pipeline.process(msg(1, 0, sender=1))
        pipeline.process(msg(2, 5, sender=2, is_forward=True))
        pipeline.process(msg(3, 6, sender=3, is_forward=True, chat_id=-777))
        pipeline.process(msg(4, 40, sender=4, is_forward=True))
        self.assertNotEqual(self.case_key(2), self.case_key(1))
        self.assertEqual(self.store.get(-777, 3)["case_key"], "-777:3")
        self.assertEqual(self.case_key(4), f"{CHAT}:4")

    def test_case_reaching_max_messages_closes_and_next_message_opens_a_new_case(self):
        pipeline = self.pipeline(max_messages=3)
        self.assertEqual(pipeline.process(msg(1, 0)).decision, "case_open")
        self.assertEqual(pipeline.process(msg(2, 1)).decision, "case_open")
        closing = pipeline.process(msg(3, 2))
        self.assertEqual(closing.decision, "issue")
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(pipeline.process(msg(4, 3)).decision, "case_open")
        self.assertEqual(self.case_key(4), f"{CHAT}:4")
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["message_count"], 3)

    def test_command_never_waits_and_gathers_earlier_messages_of_the_sender(self):
        pipeline = self.pipeline(model=FakeCaseModel(ticket([1, 2], title="Lỗi đăng nhập app Nexora")))
        pipeline.process(msg(1, 0, text="đăng nhập app nexora không được"))
        result = pipeline.process(msg(2, 1, text="/bug mình dùng iPhone"))
        self.assertEqual(result.decision, "issue")
        self.assertEqual(self.model.calls, [((1, 2), "bug")])
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.case_key(2), f"{CHAT}:1")
        self.assertEqual(self.github.calls[0].labels, ("bug", "triage", "source:telegram"))
        self.assertIn("Lỗi đăng nhập app Nexora", self.github.calls[0].title)
        self.assertEqual(result.reaction_message_id, 1)

    def test_lone_command_uses_rule_tier_without_model(self):
        pipeline = self.pipeline(model=FakeCaseModel(AssertionError("model must not run")))
        result = pipeline.process(msg(1, 0, text="/task cập nhật tài liệu"))
        self.assertEqual((result.decision, result.tier), ("issue", "rule"))
        self.assertEqual(self.model.calls, [])

    def test_command_case_falls_back_to_rule_wording_when_model_fails(self):
        pipeline = self.pipeline(model=FakeCaseModel(None))
        pipeline.process(msg(1, 0, text="đăng nhập app nexora không được"))
        result = pipeline.process(msg(2, 1, text="/bug app nexora lỗi đăng nhập"))
        self.assertEqual(result.decision, "issue")
        self.assertEqual(self.github.calls[0].title, "[bug] app nexora lỗi đăng nhập")

    def test_message_after_done_case_is_followup_and_never_a_second_issue(self):
        pipeline = self.pipeline()
        pipeline.process(msg(1, 0, text="/bug đăng nhập app nexora lỗi"))
        late = pipeline.process(msg(2, 5, text="mình cũng bị y hệt vậy"))
        self.assertEqual((late.decision, late.issue_number), ("followup", None))
        self.assertEqual(len(self.github.calls), 1)
        row = self.row(2)
        self.assertEqual((row["case_key"], row["decision"], row["issue_number"]), (f"{CHAT}:1", "followup", 42))
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["message_count"], 2)

    def test_followup_by_reply_works_even_beyond_the_window(self):
        pipeline = self.pipeline()
        pipeline.process(msg(1, 0, text="/bug đăng nhập app nexora lỗi"))
        late = pipeline.process(msg(2, 500, sender=9, reply_to_id=1, text="có tin gì mới không"))
        self.assertEqual(late.decision, "followup")
        self.assertEqual(len(self.github.calls), 1)

    def test_new_command_after_done_case_is_a_new_request(self):
        pipeline = self.pipeline()
        pipeline.process(msg(1, 0, text="/bug đăng nhập app nexora lỗi"))
        second = pipeline.process(msg(2, 2, text="/feature thêm đăng nhập bằng vân tay"))
        self.assertEqual(second.decision, "issue")
        self.assertEqual(len(self.github.calls), 2)
        self.assertEqual(self.case_key(2), f"{CHAT}:2")

    # 12.12 changed this test (was: a lone "ok" closes at once as noise without the model). Code no
    # longer matches courtesy words: only a message with no letter at all closes at once.
    def test_lone_message_without_a_letter_closes_at_once_as_noise_without_model(self):
        pipeline = self.pipeline(model=FakeCaseModel(AssertionError("model must not run")))
        result = pipeline.process(msg(1, 0, text="👍"))
        self.assertEqual(result.decision, "noise")
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "noise")
        self.assertEqual(self.github.calls, [])

    def test_a_lone_short_courtesy_waits_for_the_model_instead_of_being_dropped_by_code(self):
        pipeline = self.pipeline(model=FakeCaseModel(
            CaseAnalysis("noise", classification(type="noise", confidence=0.9), (1,), "chat")))
        self.assertEqual(pipeline.process(msg(1, 0, text="ok")).decision, "case_open")
        self.clock.advance(60)
        results = pipeline.close_due_cases()
        # 12.13 changed this: tagged + model chat was pending_review. A tag forces an issue, but a lone
        # "ok" has no product content (<= 3 words, model chat), so the safety block stops it: noise.
        self.assertEqual([item.decision for item in results], ["noise"])
        self.assertEqual(self.row(1)["reason"], "safety_block")
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.model.calls, [((1,), None)])

    # 12.12 changed this test: the closing message is a no-letter message instead of "ok".
    def test_noise_case_is_not_reopened_by_later_messages(self):
        pipeline = self.pipeline()
        pipeline.process(msg(1, 0, text="👍"))
        self.assertEqual(pipeline.process(msg(2, 1, text="app nexora đăng nhập báo lỗi")).decision, "case_open")
        self.assertNotEqual(self.case_key(2), self.case_key(1))


class AnalysisTests(CaseBase):
    def close_all(self, pipeline):
        self.clock.advance(60)
        return pipeline.close_due_cases()

    def test_noise_verdict_creates_no_issue_and_marks_the_case(self):
        pipeline = self.pipeline(model=FakeCaseModel(
            CaseAnalysis("noise", classification(type="noise", confidence=0.9), (1,))))
        pipeline.process(msg(1, 0))
        pipeline.process(msg(2, 1))
        results = self.close_all(pipeline)
        self.assertEqual([item.decision for item in results], ["noise"])
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "noise")
        self.assertEqual({self.row(1)["decision"], self.row(2)["decision"]}, {"noise"})

    # 12.13 changed these (12.11 had: a tagged group the model is unsure about waits as `pending_review`).
    # A tag forces an issue: an unsure model gives a needs-human issue (`forced_mention`). Without a
    # tag or command the unsure group still waits as `pending_review` (`pending_untagged`).
    def test_needs_human_verdict_on_a_direct_call_is_a_needs_human_issue(self):
        pipeline = self.pipeline(model=FakeCaseModel(CaseAnalysis("needs_human", None, (1,))))
        pipeline.process(msg(1, 0))
        pipeline.process(msg(2, 1))
        results = self.close_all(pipeline)
        self.assertEqual([item.decision for item in results], ["needs_human"])
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]), ("needs_human", "forced_mention"))
        self.assertIsNone(self.row(1)["type"])

    def test_needs_human_verdict_without_a_tag_creates_a_clarification_issue(self):
        pipeline = self.pipeline(model=FakeCaseModel(CaseAnalysis("needs_human", None, (1,))))
        pipeline.process(msg(1, 0, mentions_listener=False))
        results = self.close_all(pipeline)
        self.assertEqual([item.decision for item in results], ["needs_human"])
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("needs-human", self.github.calls[0].labels)
        self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]), ("needs_human", "auto_needs_human"))

    def test_confidence_below_threshold_with_a_tag_is_a_needs_human_issue(self):
        pipeline = self.pipeline(model=FakeCaseModel(ticket([1], confidence=0.69)))
        pipeline.process(msg(1, 0))
        self.assertEqual(self.close_all(pipeline)[0].decision, "needs_human")
        self.assertEqual(len(self.github.calls), 1)

    def test_confidence_below_threshold_without_a_tag_creates_a_clarification_issue(self):
        pipeline = self.pipeline(model=FakeCaseModel(ticket([1], confidence=0.69)))
        pipeline.process(msg(1, 0, mentions_listener=False))
        self.assertEqual(self.close_all(pipeline)[0].decision, "needs_human")
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("needs-human", self.github.calls[0].labels)

    def test_ticket_verdict_with_invalid_type_is_needs_human(self):
        pipeline = self.pipeline(model=FakeCaseModel(ticket([1], type="noise")))
        pipeline.process(msg(1, 0))
        self.assertEqual(self.close_all(pipeline)[0].decision, "needs_human")

    def test_model_failure_still_creates_exactly_one_needs_human_issue(self):
        pipeline = self.pipeline(model=FakeCaseModel(TimeoutError("offline")))
        pipeline.process(msg(1, 0))
        pipeline.process(msg(2, 1))
        results = self.close_all(pipeline)
        self.assertEqual([item.decision for item in results], ["needs_human"])
        self.assertEqual(len(self.github.calls), 1)

    def test_key_message_ids_outside_the_case_are_filtered(self):
        outsider = msg(77, 0, sender=5, text="SECRET_OTHER_CASE nội dung của vụ khác")
        quiet = CaseAnalysis("noise", classification(type="noise", confidence=0.9), (77,))
        pipeline = self.pipeline(model=FakeCaseModel(
            lambda items: ticket([2, 77, 999, True]) if items[0].message_id == 1 else quiet))
        pipeline.process(outsider)
        pipeline.process(msg(1, 0, text="NOISE_BEFORE bình luận không quan trọng"))
        pipeline.process(msg(2, 1, text="KEY_MESSAGE app nexora không đăng nhập được"))
        self.close_all(pipeline)
        body = self.github.calls[0].body
        # Changed: the ticket now quotes the WHOLE case (the model's key ids only add a
        # "tin chính" marker), so the non-key message 1 is rendered too. Only a message
        # of ANOTHER case (the outsider) must still never appear.
        self.assertIn("KEY\\_MESSAGE", body)
        self.assertIn("NOISE\\_BEFORE", body)
        self.assertNotIn("SECRET\\_OTHER\\_CASE", body)
        self.assertEqual(body.count("Tin gốc (2 tin)"), 1)   # one collapsed block for the whole case
        self.assertEqual(body.count("tin chính"), 1)

    def test_filtering_all_ids_still_quotes_the_whole_case_without_marker(self):
        pipeline = self.pipeline(model=FakeCaseModel(ticket([500, 600])))
        pipeline.process(msg(1, 0, text="FIRST_ONE app nexora"))
        pipeline.process(msg(2, 1, text="SECOND_ONE app nexora"))
        self.close_all(pipeline)
        # Changed: no valid key id no longer narrows the quote to the first message.
        self.assertIn("FIRST\\_ONE", self.github.calls[0].body)
        self.assertIn("SECOND\\_ONE", self.github.calls[0].body)
        self.assertNotIn("tin chính", self.github.calls[0].body)

    def test_model_text_goes_through_the_sanitizer(self):
        pipeline = self.pipeline(model=FakeCaseModel(ticket(
            [1], title="@all <img src=x>", summary="[x](https://evil.invalid) -->")))
        pipeline.process(msg(1, 0))
        self.close_all(pipeline)
        issue = self.github.calls[0]
        self.assertNotIn("<img", issue.title + issue.body)
        self.assertNotIn("://evil", issue.body)
        self.assertEqual(issue.body.count("-->"), 1)
        self.assertIn("@​all", issue.title)

    def test_title_and_body_keep_the_first_message_marker_and_case_size(self):
        pipeline = self.pipeline(model=FakeCaseModel(ticket([1, 2, 3])))
        for index in (1, 2, 3):
            pipeline.process(msg(index, index))
        self.close_all(pipeline)
        body = self.github.calls[0].body
        self.assertTrue(body.startswith(f"<!-- tg:{CHAT}:1 -->"))
        self.assertIn("case 3 tin", body)
        self.assertIn("Tin gốc (3 tin)", body)
        self.assertEqual(body.count(" · tin chính"), 3)   # one header per message, all three marked

    def test_window_not_yet_expired_means_nothing_is_closed(self):
        pipeline = self.pipeline()
        pipeline.process(msg(1, 0))
        self.clock.advance(14)
        self.assertEqual(pipeline.close_due_cases(), [])
        self.clock.advance(2)
        self.assertEqual(len(pipeline.close_due_cases()), 1)


class OneIssueTests(CaseBase):
    def test_three_login_messages_with_ticket_verdict_make_exactly_one_issue(self):
        pipeline = self.pipeline(model=FakeCaseModel(ticket([1, 2, 3])))
        texts = ("login app nexora bị lỗi", "không vào được app nexora", "ai xem giúp login app nexora")
        for index, text in enumerate(texts, 1):
            pipeline.process(msg(index, index, text=text))
        self.clock.advance(60)
        results = pipeline.close_due_cases() + pipeline.close_due_cases()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual([self.row(i)["issue_number"] for i in (1, 2, 3)], [42, 42, 42])
        self.assertEqual(self.row(1)["github_called_at"] is not None, True)
        self.assertEqual([self.row(i)["github_called_at"] for i in (2, 3)], [None, None])
        self.assertEqual(self.store.count_recent_issues(), 1)

    def test_case_key_is_unique_in_the_cases_table(self):
        pipeline = self.pipeline()
        pipeline.process(msg(1, 0))
        columns = ("case_key, chat_id, first_message_id, status, created_at, last_message_at")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                f"INSERT INTO cases ({columns}) VALUES (?, ?, ?, 'open', 'x', 'x')",
                (f"{CHAT}:1", CHAT, 1))

    def test_only_one_caller_wins_a_case(self):
        pipeline = self.pipeline()
        pipeline.process(msg(1, 0))
        key = f"{CHAT}:1"
        self.assertTrue(self.store.begin_analysis(key))
        self.assertFalse(self.store.begin_analysis(key))
        self.assertTrue(self.store.reserve_github_call(msg(1), 20))
        self.assertFalse(self.store.reserve_github_call(msg(1), 20))

    def test_two_workers_racing_on_the_same_case_create_one_issue(self):
        entered, release = threading.Event(), threading.Event()
        github = FakeGitHub(before_create=lambda: (entered.set(), release.wait(5)))
        first = self.pipeline(github=github)
        second = Pipeline(config("live", window=15), self.store, self.model, github)
        first.process(msg(1, 0))
        first.process(msg(2, 1))
        self.clock.advance(60)
        results = []
        worker = threading.Thread(target=lambda: results.append(first.close_due_cases()))
        worker.start()
        self.assertTrue(entered.wait(5))
        results.append(second.close_due_cases())   # case is already `analyzing`
        release.set()
        worker.join(5)
        self.assertEqual(len(github.calls), 1)
        self.assertEqual(sorted(len(item) for item in results), [0, 1])

    def test_one_case_consumes_one_unit_of_the_hourly_limit(self):
        pipeline = self.pipeline(limit=1, model=FakeCaseModel(ticket([1, 2, 3])))
        for index in (1, 2, 3):
            pipeline.process(msg(index, index))
        self.clock.advance(60)
        self.assertEqual(pipeline.close_due_cases()[0].decision, "issue")
        pipeline.process(msg(10, 100, sender=2))
        self.clock.advance(60)
        with self.assertLogs("tg_triage.pipeline", level="WARNING"):
            self.assertEqual(pipeline.close_due_cases()[0].decision, "rate_limited")
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.store.get_case(f"{CHAT}:10")["status"], "error")

    def test_dry_run_records_cases_but_never_calls_github(self):
        github = FakeGitHub(error=AssertionError("GitHub must not be called"))
        pipeline = self.pipeline("dry-run", github=github)
        pipeline.process(msg(1, 0))
        pipeline.process(msg(2, 1))
        self.clock.advance(60)
        self.assertEqual([item.decision for item in pipeline.close_due_cases()], ["dry_run_issue"])
        self.assertEqual(github.calls, [])
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "done")
        self.assertIsNone(self.row(1)["issue_number"])
        # A late message still joins the dry-run case: grouping can be judged in dry-run.
        self.assertEqual(pipeline.process(msg(3, 61)).decision, "case_open")
        self.assertEqual(pipeline.process(msg(4, 62, reply_to_id=1, sender=8)).decision, "followup")

    def test_github_failure_quarantines_the_whole_case_without_retry(self):
        github = FakeGitHub(error=RuntimeError("PRIVATE ghp_TEST"))
        pipeline = self.pipeline(github=github)
        pipeline.process(msg(1, 0))
        pipeline.process(msg(2, 1))
        self.clock.advance(60)
        with self.assertLogs("tg_triage.pipeline", level="INFO") as logs:
            result = pipeline.close_due_cases()[0]
        self.assertEqual(result.decision, "error")
        self.assertEqual(result.error, GITHUB_AMBIGUOUS_ERROR + " (RuntimeError)")
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "error")
        self.assertEqual([self.row(i)["decision"] for i in (1, 2)], ["error", "error"])
        self.assertEqual(pipeline.close_due_cases(), [])
        self.assertEqual(len(github.calls), 1)
        self.assertNotIn("PRIVATE", "\n".join(logs.output))
        self.assertNotIn("ghp_TEST", "\n".join(logs.output))

    def test_logs_carry_ids_status_counts_and_latency_but_no_content(self):
        pipeline = self.pipeline(model=FakeCaseModel(ticket([1], title="TITLE_FROM_MODEL")))
        pipeline.process(msg(1, 0, text="PRIVATE_MESSAGE app nexora"))
        self.clock.advance(60)
        with self.assertLogs("tg_triage.pipeline", level="INFO") as logs:
            pipeline.close_due_cases()
        output = "\n".join(logs.output)
        for field in ("chat_id=", "message_id=", "decision=issue", "case_status=done", "images=0",
                      "attachments_dry_run=0", "latency_ms="):
            self.assertIn(field, output)
        for secret in ("PRIVATE_MESSAGE", "TITLE_FROM_MODEL", "ghp_TEST"):
            self.assertNotIn(secret, output)


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "triage.sqlite3"

    def seed(self, clock):
        store = Store(self.path, clock=clock)
        pipeline = Pipeline(config("live", window=15), store, FakeCaseModel(ticket([1])), FakeGitHub())
        pipeline.process(msg(1, 0))
        pipeline.process(msg(2, 1))
        return store

    def test_restart_with_analyzing_case_that_called_github_never_calls_it_again(self):
        clock = Clock()
        store = self.seed(clock)
        key = f"{CHAT}:1"
        self.assertTrue(store.begin_analysis(key))
        self.assertTrue(store.reserve_github_call(msg(1), 20))   # crash right after this
        store.close()
        reopened = Store(self.path, clock=clock)
        self.addCleanup(reopened.close)
        github = FakeGitHub()
        pipeline = Pipeline(config("live", window=15), reopened, FakeCaseModel(ticket([1])), github)
        clock.advance(60)
        self.assertEqual(pipeline.close_due_cases(), [])
        self.assertEqual(github.calls, [])
        case = reopened.get_case(key)
        self.assertEqual(case["status"], "error")
        self.assertIn(GITHUB_AMBIGUOUS_ERROR, case["error"])
        self.assertEqual([reopened.get(CHAT, i)["decision"] for i in (1, 2)], ["error", "error"])

    def test_restart_with_analyzing_case_that_never_called_github_is_analyzed_once_more(self):
        clock = Clock()
        store = self.seed(clock)
        key = f"{CHAT}:1"
        self.assertTrue(store.begin_analysis(key))
        store.close()
        reopened = Store(self.path, clock=clock)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.get_case(key)["status"], "open")
        github = FakeGitHub()
        pipeline = Pipeline(config("live", window=15), reopened, FakeCaseModel(ticket([1])), github)
        clock.advance(60)
        self.assertEqual([item.decision for item in pipeline.close_due_cases()], ["issue"])
        self.assertEqual(len(github.calls), 1)

    def test_case_analyzed_twice_without_github_call_is_quarantined(self):
        clock = Clock()
        store = self.seed(clock)
        key = f"{CHAT}:1"
        for _ in range(2):
            self.assertTrue(store.begin_analysis(key))
            store.connection.execute("UPDATE cases SET status='analyzing' WHERE case_key=?", (key,))
            store.connection.commit()
            store.close()
            store = Store(self.path, clock=clock)
        self.addCleanup(store.close)
        self.assertEqual(store.get_case(key)["status"], "error")
        self.assertEqual(store.get_case(key)["error"], "Lặp lỗi khi xử lý, cần người xem")

    def test_waiting_case_survives_restart_and_is_closed_by_the_window(self):
        clock = Clock()
        self.seed(clock).close()
        reopened = Store(self.path, clock=clock)
        self.addCleanup(reopened.close)
        github = FakeGitHub()
        pipeline = Pipeline(config("live", window=15), reopened, FakeCaseModel(ticket([1])), github)
        self.assertEqual(pipeline.close_due_cases(), [])
        clock.advance(60)
        self.assertEqual(len(pipeline.close_due_cases()), 1)
        self.assertEqual(len(github.calls), 1)


class MigrationTests(unittest.TestCase):
    def test_database_from_before_cases_opens_and_keeps_its_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute('''CREATE TABLE messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, received_at TEXT NOT NULL,
                sender_name TEXT NOT NULL, text TEXT NOT NULL, tier TEXT NOT NULL,
                type TEXT, confidence REAL, title TEXT, summary TEXT, priority TEXT,
                decision TEXT NOT NULL, issue_number INTEGER, issue_url TEXT, error TEXT,
                processed_at TEXT, latency_ms INTEGER, github_called_at TEXT, chat_username TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (chat_id, message_id))''')
            connection.execute('''INSERT INTO messages (chat_id, message_id, received_at, sender_name,
                text, tier, decision, issue_number, github_called_at)
                VALUES (-5, 1, ?, 'Cũ', 'tin cũ', 'rule', 'issue', 9, ?)''',
                               (NOW.isoformat(), NOW.isoformat()))
            connection.execute('''INSERT INTO messages (chat_id, message_id, received_at, sender_name,
                text, tier, decision) VALUES (-5, 2, ?, 'Cũ', 'tin cũ đang chờ xử lý', 'none', 'queued')''',
                               (NOW.isoformat(),))
            connection.commit()
            connection.close()
            store = Store(path, clock=lambda: NOW)
            try:
                columns = {row[1] for row in store.connection.execute("PRAGMA table_info(messages)")}
                self.assertTrue({"case_key", "sender_id", "reply_to_id", "is_forward", "has_image",
                                 "image_size", "image_mime", "mentions_listener"} <= columns)
                tables = {row[0] for row in store.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"cases", "attachments"} <= tables)
                old = store.get(-5, 1)
                self.assertEqual((old["decision"], old["issue_number"], old["case_key"]), ("issue", 9, None))
                self.assertEqual(store.count_recent_issues(), 1)
                self.assertEqual([item.message_id for item in store.queued_messages()], [2])
                # An old queued row can be processed with the new pipeline. The old database never
                # recorded a tag (so the row is untagged). 12.13 changed this test (was: pending_review):
                # a confident bug needs no tag and becomes an automatic issue.
                github = FakeGitHub()
                pipeline = Pipeline(config("live", window=0), store, FakeClassifier(classification()), github)
                self.assertEqual(pipeline.process(store.queued_messages()[0]).decision, "issue")
                self.assertEqual(len(github.calls), 1)
                self.assertEqual(store.get(-5, 2)["case_key"], "-5:2")
            finally:
                store.close()
            reopened = Store(path)   # reopening an already migrated database is a no-op
            reopened.close()

    def test_export_csv_of_an_old_read_only_database_still_works(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute('''CREATE TABLE messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, received_at TEXT NOT NULL,
                sender_name TEXT NOT NULL, text TEXT NOT NULL, tier TEXT NOT NULL,
                decision TEXT NOT NULL, PRIMARY KEY (chat_id, message_id))''')
            connection.commit()
            connection.close()
            store = Store(path, read_only=True)
            try:
                out = Path(directory) / "out.csv"
                store.export_csv(out)
                self.assertIn("case_key", out.read_text(encoding="utf-8-sig"))
            finally:
                store.close()


class CaseWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)

    async def drive(self, pipeline, send, items, *, until):
        queue = asyncio.Queue()
        for item in items:
            queue.put_nowait(item)
        with patch.object(telegram_io, "SWEEP_SECONDS", 0.02):
            task = asyncio.create_task(telegram_io.worker(queue, pipeline, react=send))
            try:
                await asyncio.wait_for(queue.join(), 5)
                for _ in range(250):
                    if until():
                        break
                    await asyncio.sleep(0.02)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_three_messages_one_issue_and_one_ok_hand_on_the_first_message_only(self):
        model, github, send = FakeCaseModel(ticket([1, 2, 3])), FakeGitHub(), Recorder()
        pipeline = Pipeline(config("live", window=15), self.store, model, github)
        items = [msg(index, index) for index in (1, 2, 3)]

        async def advance_after_queue():
            await asyncio.sleep(0.1)
            self.clock.advance(60)

        advancer = asyncio.create_task(advance_after_queue())
        await self.drive(pipeline, send, items, until=lambda: (CHAT, 1, OK) in send.calls)
        await advancer
        self.assertEqual(len(github.calls), 1)
        # 12.10: the heart now precedes the issue on the same first message; still one OK hand.
        self.assertEqual(send.calls, [(CHAT, 1, HEART), (CHAT, 1, OK)])
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "done")

    async def test_command_in_the_middle_reacts_ok_on_the_first_message_not_the_command(self):
        github, send = FakeGitHub(), Recorder()
        pipeline = Pipeline(config("live", window=15), self.store, FakeCaseModel(ticket([1, 2])), github)
        items = [msg(1, 0, text="đăng nhập app nexora lỗi"), msg(2, 1, text="/bug iPhone 15")]
        await self.drive(pipeline, send, items, until=lambda: (CHAT, 1, OK) in send.calls)
        # 12.10: heart (verdict ticket) then OK hand, both on the first message, none on the command.
        self.assertEqual(send.calls, [(CHAT, 1, HEART), (CHAT, 1, OK)])
        self.assertEqual(len(github.calls), 1)

    async def test_followup_and_waiting_messages_get_no_reaction(self):
        github, send = FakeGitHub(), Recorder()
        pipeline = Pipeline(config("live", window=15), self.store, FakeCaseModel(ticket([1])), github)
        items = [msg(1, 0, text="/bug đăng nhập app nexora lỗi"), msg(2, 1), msg(3, 2)]
        await self.drive(pipeline, send, items, until=lambda: (CHAT, 1, OK) in send.calls)
        await asyncio.sleep(0.1)
        # 12.10: heart + OK hand on message 1 only; follow-ups 2 and 3 get nothing.
        self.assertEqual(send.calls, [(CHAT, 1, HEART), (CHAT, 1, OK)])
        self.assertEqual(len(github.calls), 1)

    async def test_dry_run_never_sends_ok_even_when_a_case_closes(self):
        send = Recorder()
        pipeline = Pipeline(config("dry-run", window=15), self.store, FakeCaseModel(ticket([1, 2])), None)

        async def advance():
            await asyncio.sleep(0.1)
            self.clock.advance(60)

        advancer = asyncio.create_task(advance())
        await self.drive(pipeline, send, [msg(1, 0), msg(2, 1)],
                         until=lambda: self.store.get_case(f"{CHAT}:1")["status"] == "done")
        await advancer
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "done")
        self.assertEqual(send.calls, [(CHAT, 1, HEART)])   # 12.10: heart in dry-run, never the OK hand

    async def test_sweep_failure_does_not_stop_the_worker(self):
        class Broken:
            config = config("live")

            def close_due_cases(self):
                raise ValueError("PRIVATE_CONTENT")

            def process(self, item):
                return Decision("noise", "rule")

        queue = asyncio.Queue()
        with patch.object(telegram_io, "SWEEP_SECONDS", 0.01), \
                self.assertLogs("tg_triage.telegram_io", level="ERROR") as logs:
            task = asyncio.create_task(telegram_io.worker(queue, Broken()))
            await asyncio.sleep(0.1)
            queue.put_nowait(msg(1, 0))
            await asyncio.wait_for(queue.join(), 5)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertIn("exception=ValueError", logs.output[0])
        self.assertNotIn("PRIVATE_CONTENT", "\n".join(logs.output))


class TransportMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_carries_sender_reply_forward_and_image_flags(self):
        from tests.test_telegram_io import event
        store = Store(":memory:")
        self.addCleanup(store.close)
        queue = asyncio.Queue()
        incoming = event(sender_id=555, reply_to_msg_id=41, fwd_from=object(),
                         photo=object(), file=SimpleNamespace(size=2048, mime_type="image/jpeg"))
        await telegram_io.enqueue_event(incoming, queue, (incoming.chat_id,), store)
        item = await queue.get()
        self.assertEqual((item.sender_id, item.reply_to_id, item.is_forward), (555, 41, True))
        self.assertEqual((item.has_image, item.image_size, item.image_mime), (True, 2048, None))
        row = store.get(incoming.chat_id, incoming.id)
        self.assertEqual((row["sender_id"], row["reply_to_id"], row["is_forward"], row["has_image"],
                          row["image_size"]), (555, 41, 1, 1, 2048))
        replay = store.queued_messages()[0]
        self.assertEqual((replay.sender_id, replay.reply_to_id, replay.is_forward, replay.has_image),
                         (555, 41, True, True))

    async def test_sticker_and_non_image_documents_are_not_images_and_plain_event_is_unflagged(self):
        info = telegram_io._image_info
        self.assertEqual(info(SimpleNamespace(sticker=object(), photo=None, document=object(),
                                              file=SimpleNamespace(size=1, mime_type="image/webp"))),
                         (False, None, None))
        self.assertEqual(info(SimpleNamespace(document=object(),
                                              file=SimpleNamespace(size=1, mime_type="video/mp4"))),
                         (False, None, None))
        self.assertEqual(info(SimpleNamespace(document=object(),
                                              file=SimpleNamespace(size=5, mime_type="image/gif"))),
                         (True, 5, "image/gif"))
        self.assertEqual(info(SimpleNamespace()), (False, None, None))

    async def test_forum_topic_root_is_not_a_reply(self):
        topic = SimpleNamespace(forum_topic=True, reply_to_top_id=None)
        threaded = SimpleNamespace(forum_topic=True, reply_to_top_id=10)
        self.assertIsNone(telegram_io._reply_target(SimpleNamespace(reply_to_msg_id=10, reply_to=topic)))
        self.assertEqual(telegram_io._reply_target(SimpleNamespace(reply_to_msg_id=12, reply_to=threaded)), 12)
        self.assertEqual(telegram_io._reply_target(SimpleNamespace(reply_to_msg_id=3, reply_to=None)), 3)
        self.assertIsNone(telegram_io._reply_target(SimpleNamespace()))

    async def test_media_downloader_runs_telethon_on_the_event_loop_from_a_thread(self):
        seen = []

        class Client:
            async def get_messages(self, chat_id, ids):
                seen.append(("get", chat_id, ids, threading.current_thread().name))
                return SimpleNamespace(media=object())

            async def download_media(self, target, file):
                seen.append(("download", file))
                return str(Path(file) / "name.jpg")

        loop = asyncio.get_running_loop()
        download = telegram_io.make_media_downloader(Client(), loop)
        with tempfile.TemporaryDirectory() as directory:
            path = await asyncio.to_thread(download, CHAT, 5, Path(directory))
        self.assertEqual(path, Path(directory) / "name.jpg")
        self.assertEqual(seen[0][:3], ("get", CHAT, 5))
        self.assertEqual(seen[0][3], threading.main_thread().name)
        self.assertEqual(seen[1], ("download", directory))

    async def test_media_downloader_returns_none_without_media(self):
        class Client:
            async def get_messages(self, chat_id, ids):
                return SimpleNamespace(media=None)

        download = telegram_io.make_media_downloader(Client(), asyncio.get_running_loop())
        self.assertIsNone(await asyncio.to_thread(download, CHAT, 5, Path(".")))


if __name__ == "__main__":
    unittest.main()
