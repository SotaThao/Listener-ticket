"""Topic split when a window closes (Assistant definition 12.9). Offline: fake model/GitHub/uploader."""

import asyncio
import json
import re
import sqlite3
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from tg_triage import classifier as classifier_module
from tg_triage import telegram_io
from tg_triage.classifier import (TOPIC_SCHEMA, Classifier, build_transcript, load_prompt,
                                  parse_topic_groups)
from tg_triage.attachments import release_tag
from tg_triage.issue_format import format_issue
from tg_triage.models import TopicGroup
from tg_triage.pipeline import FALLBACK_NOTE, Pipeline, validate_partition
from tg_triage.store import GITHUB_AMBIGUOUS_ERROR, RECONCILIATION_ERROR, Store
from tests.helpers import (JPG, NOW, Clock, FakeDownloader, FakeGitHub, FakeHTTP, FakeTopicModel,
                           FakeUploader, NumberedGitHub, at, classification, config, message, topic)
from tests.test_cases import CHAT, msg
from tests.test_reactions import HEART, OK, Recorder

LISTENER = Path(__file__).resolve().parents[1]
ZWSP = "​"


class Crash(BaseException):
    """Simulates a process kill: not an Exception, so no `except Exception` can swallow it."""


class TopicBase(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)

    def pipeline(self, model, mode="live", github=None, limit=20, max_messages=20, max_window=40,
                 **extra):
        self.model = model
        self.github = NumberedGitHub() if github is None else github
        return Pipeline(config(mode, limit, window=15, max_messages=max_messages, max_window=max_window),
                        self.store, model, self.github, **extra)

    def window(self, pipeline, texts):
        """Feed one sender's messages numbered from 1; they all land in one window."""
        for index, text in enumerate(texts, 1):
            pipeline.process(msg(index, index, text=text))

    def close(self, pipeline):
        self.clock.advance(60)
        return pipeline.close_due_cases()

    def row(self, message_id):
        return self.store.get(CHAT, message_id)

    def numbers(self, *ids):
        return [self.row(item)["issue_number"] for item in ids]

    def decisions(self, *ids):
        return [self.row(item)["decision"] for item in ids]

    def groups(self, case_key=f"{CHAT}:1"):
        return {item["group_key"]: item for item in self.store.case_groups(case_key)}

    def reconciliation(self, case_key=f"{CHAT}:1"):
        return self.store.connection.execute(
            "SELECT * FROM reconciliation WHERE case_key = ?", (case_key,)).fetchone()


FOUR = ("QRALPHA quét mã không được", "LOGINBETA không đăng nhập được",
        "QRALPHA máy Android", "LOGINBETA máy iPhone")


class TwoTopicsTests(TopicBase):
    """Spec test 24 and the reactions/keys that go with it."""

    def test_24_two_topics_in_one_window_make_two_issues_each_with_only_its_own_messages(self):
        pipeline = self.pipeline(FakeTopicModel((
            topic([1, 3], title="Quét mã QR không được"), topic([2, 4], title="Không đăng nhập được"))))
        self.window(pipeline, FOUR)
        results = self.close(pipeline)
        self.assertEqual(len(results), 1)
        self.assertEqual(self.model.calls, [(1, 2, 3, 4)])
        self.assertEqual(len(self.github.calls), 2)
        qr, login = self.github.calls
        self.assertEqual((qr.body.count("QRALPHA"), qr.body.count("LOGINBETA")), (2, 0))
        self.assertEqual((login.body.count("LOGINBETA"), login.body.count("QRALPHA")), (2, 0))
        self.assertTrue(qr.body.startswith(f"<!-- tg:{CHAT}:1 -->"))
        self.assertTrue(login.body.startswith(f"<!-- tg:{CHAT}:2 -->"))
        self.assertIn("[bug] Quét mã QR không được", qr.title)
        self.assertEqual(self.numbers(1, 2, 3, 4), [101, 102, 101, 102])
        self.assertEqual(self.decisions(1, 2, 3, 4), ["issue"] * 4)
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "done")
        # Each message is in exactly one group, and each group carries its own issue.
        self.assertEqual([self.row(i)["group_key"] for i in (1, 2, 3, 4)],
                         [f"{CHAT}:1", f"{CHAT}:2", f"{CHAT}:1", f"{CHAT}:2"])
        self.assertEqual({key: (item["message_count"], item["issue_number"])
                          for key, item in self.groups().items()},
                         {f"{CHAT}:1": (2, 101), f"{CHAT}:2": (2, 102)})
        # The window's decision reports the first issue and every group that needs an OK hand.
        self.assertEqual((results[0].decision, results[0].issue_number), ("issue", 101))
        self.assertEqual(results[0].reaction_message_ids, (1, 2))
        self.assertEqual(results[0].reaction_message_id, 1)

    def test_groups_are_stored_and_reconciled_in_sqlite(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1, 3]), topic([2, 4]))))
        self.window(pipeline, FOUR)
        self.close(pipeline)
        tally = self.reconciliation()
        self.assertEqual((tally["received"], tally["issue"], tally["noise"], tally["needs_human"],
                          tally["failed"], tally["groups"], tally["issues_created"], tally["balanced"],
                          tally["status"]), (4, 4, 0, 0, 0, 2, 2, 1, "done"))

    def test_group_keys_are_unique_between_groups_of_one_window_and_enforced_by_sqlite(self):
        pipeline = self.pipeline(FakeTopicModel((topic([2, 4]), topic([1, 3]))))
        self.window(pipeline, FOUR)
        self.close(pipeline)
        keys = list(self.groups())
        self.assertEqual(keys, [f"{CHAT}:1", f"{CHAT}:2"])   # smallest message id of each group
        self.assertEqual(len(set(keys)), len(keys))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO groups (group_key, case_key, chat_id, first_message_id, message_count, "
                "status, created_at) VALUES (?, ?, ?, 1, 1, 'planned', 'x')",
                (keys[0], f"{CHAT}:1", CHAT))

    def test_a_message_can_never_be_planned_into_two_groups(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1, 2]),)))
        pipeline.process(msg(1, 0))
        pipeline.process(msg(2, 1))
        with self.assertRaises(ValueError):
            self.store.plan_groups(f"{CHAT}:1", [("a", [1, 2]), ("b", [2])])
        self.assertEqual(self.store.case_groups(f"{CHAT}:1"), [])    # all or nothing
        self.assertEqual([self.row(1)["group_key"], self.row(2)["group_key"]], [None, None])

    def test_chronological_order_is_what_the_model_sees_and_the_issue_shows(self):
        pipeline = self.pipeline(FakeTopicModel((topic([4, 5]),)))
        pipeline.process(msg(5, 0, text="THUHAI trả lời sau"))
        pipeline.process(msg(4, 1, text="THUBA nói sau trong dữ liệu"))
        self.close(pipeline)
        self.assertEqual(self.model.calls, [(5, 4)])
        body = self.github.calls[0].body
        self.assertLess(body.index("THUHAI"), body.index("THUBA"))

    def test_logs_carry_counts_only(self):
        pipeline = self.pipeline(FakeTopicModel((
            topic([1, 3], title="TIEUDEBIMAT"), topic([2, 4], summary="TOMTATBIMAT"))))
        self.window(pipeline, ("NOIDUNGBIMAT một", "NOIDUNGBIMAT hai", "NOIDUNGBIMAT ba", "NOIDUNGBIMAT bốn"))
        self.clock.advance(60)
        with self.assertLogs("tg_triage.pipeline", level="INFO") as logs:
            pipeline.close_due_cases()
        output = "\n".join(logs.output)
        for field in ("groups=2", "group_sizes=2,2", "issues=2", "received=4", "accounted=4",
                      "reconciliation=balanced", "decision=issue", "case_status=done"):
            self.assertIn(field, output)
        for secret in ("NOIDUNGBIMAT", "TIEUDEBIMAT", "TOMTATBIMAT", "Người thử nghiệm", "ghp_TEST"):
            self.assertNotIn(secret, output)


class PartitionTests(TopicBase):
    """Spec tests 25 and 26: the code never trusts the model's partition."""

    TEXTS = ("ALPHAONE đăng nhập lỗi", "BETATWO quét mã lỗi", "GAMMATHREE máy iPhone")

    def test_25_missing_id_is_retried_then_the_whole_chunk_is_one_needs_human_ticket(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1, 2]),)))    # id 3 is never returned
        self.window(pipeline, self.TEXTS)
        results = self.close(pipeline)
        self.assertEqual(self.model.calls, [(1, 2, 3), (1, 2, 3)])    # exactly one retry
        self.assertEqual(len(self.github.calls), 1)
        issue = self.github.calls[0]
        self.assertEqual(issue.labels, ("needs-human", "source:telegram"))
        for word in ("ALPHAONE", "BETATWO", "GAMMATHREE"):
            self.assertIn(word, issue.body)
        self.assertIn("### ❓ Cần làm rõ", issue.body)
        self.assertIn("không tách được chủ đề", issue.body)
        self.assertEqual(self.decisions(1, 2, 3), ["needs_human"] * 3)
        self.assertEqual(self.numbers(1, 2, 3), [101, 101, 101])
        self.assertEqual(len(self.groups()), 1)
        self.assertEqual(results[0].decision, "needs_human")
        self.assertEqual(self.reconciliation()["balanced"], 1)

    def test_a_valid_second_answer_is_used(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1, 2]),), (topic([1]), topic([2, 3]))))
        self.window(pipeline, self.TEXTS)
        self.close(pipeline)
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(len(self.github.calls), 2)
        self.assertEqual(self.numbers(1, 2, 3), [101, 102, 102])

    def test_26_duplicate_foreign_or_malformed_ids_never_reach_an_issue_twice(self):
        cases = {
            "duplicate across groups": (topic([1, 2]), topic([2, 3])),
            "duplicate inside a group": (topic([1, 1, 2, 3]),),
            "foreign id": (topic([1, 2, 3, 999]),),
            "boolean id": (topic([1, 2, 3, True]),),
            "empty group": (topic([1, 2, 3]), topic([])),
            "no groups": (),
        }
        for name, answer in cases.items():
            with self.subTest(name):
                self.setUp()
                self.store.enqueue(msg(999, 0, sender=9, text="OTHERWINDOW tin của cửa sổ khác"))
                pipeline = self.pipeline(FakeTopicModel(answer))
                self.window(pipeline, self.TEXTS)
                self.close(pipeline)
                self.assertEqual(len(self.model.calls), 2)
                self.assertEqual(len(self.github.calls), 1)
                self.assertNotIn("OTHERWINDOW", self.github.calls[0].body)
                self.assertEqual(self.decisions(1, 2, 3), ["needs_human"] * 3)
                owners = self.store.connection.execute(
                    "SELECT group_key, COUNT(*) FROM messages WHERE case_key IS NOT NULL "
                    "GROUP BY group_key").fetchall()
                self.assertEqual([tuple(item) for item in owners], [(f"{CHAT}:1", 3)])
                self.assertEqual(self.row(999)["decision"], "queued")      # never touched
                self.assertIsNone(self.row(999)["group_key"])

    def test_model_exception_is_contained_logged_by_class_and_falls_back(self):
        pipeline = self.pipeline(FakeTopicModel(RuntimeError("PRIVATE ghp_TEST")))
        self.window(pipeline, self.TEXTS)
        self.clock.advance(60)
        with self.assertLogs("tg_triage.pipeline", level="INFO") as logs:
            pipeline.close_due_cases()
        self.assertEqual(len(self.model.calls), 2)
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("exception=RuntimeError", "\n".join(logs.output))
        self.assertNotIn("PRIVATE", "\n".join(logs.output))

    def test_validate_partition_accepts_only_an_exact_cover(self):
        ids = {1, 2, 3}
        self.assertIsNotNone(validate_partition((topic([3]), topic([1, 2])), ids))
        for bad in (None, (), [], "x", (topic([1, 2]),), (topic([1, 2]), topic([2, 3])),
                    (topic([1, 2, 3, 4]),), (topic([1, 2, 3]), topic([])), (object(),)):
            with self.subTest(bad=bad):
                self.assertIsNone(validate_partition(bad, ids))


class ChunkTests(TopicBase):
    """Spec test 27."""

    def test_27_window_of_100_messages_is_cut_into_chunks_of_at_most_40(self):
        pipeline = self.pipeline(
            FakeTopicModel(lambda items: (topic([item.message_id for item in items]),)),
            max_messages=100, max_window=40)
        for index in range(1, 101):
            result = pipeline.process(msg(index, index / 10, text=f"login app nexora lần {index}"))
        self.assertEqual(result.decision, "issue")          # the 100th message fills the window
        self.assertEqual([len(call) for call in self.model.calls], [40, 40, 20])
        self.assertEqual([call[0] for call in self.model.calls], [1, 41, 81])
        self.assertEqual(sorted(i for call in self.model.calls for i in call), list(range(1, 101)))
        self.assertEqual(len(self.github.calls), 3)
        self.assertIn("Tin gốc (40 tin)", self.github.calls[0].body)
        self.assertIn("login app nexora lần 40", self.github.calls[0].body)   # every message is rendered
        self.assertNotIn("còn", self.github.calls[0].body)
        self.assertIn("Tin gốc (20 tin)", self.github.calls[2].body)
        tally = self.reconciliation()
        self.assertEqual((tally["received"], tally["issue"], tally["balanced"], tally["status"]),
                         (100, 100, 1, "done"))
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "done")
        self.assertTrue(all(self.row(i)["group_key"] is not None for i in range(1, 101)))
        self.assertEqual(sorted(self.groups()), [f"{CHAT}:1", f"{CHAT}:41", f"{CHAT}:81"])

    def test_a_chunk_that_fails_is_judged_alone_and_never_drops_the_others(self):
        answers = (lambda items: (topic([item.message_id for item in items]),),
                   lambda items: (topic([items[0].message_id]),))       # second chunk is short
        pipeline = self.pipeline(FakeTopicModel(*answers), max_messages=6, max_window=3)
        for index in range(1, 7):
            pipeline.process(msg(index, index / 10, text=f"login app nexora lần {index}"))
        # chunk 1 = 1..3 fine; chunk 2 = 4..6 answers only id 4 twice -> one needs-human ticket
        self.assertEqual(len(self.github.calls), 2)
        self.assertEqual(self.decisions(1, 2, 3), ["issue"] * 3)
        self.assertEqual(self.decisions(4, 5, 6), ["needs_human"] * 3)
        self.assertEqual(self.reconciliation()["balanced"], 1)


class CommandTests(TopicBase):
    """Spec test 28."""

    TEXTS = ("LOGINBETA đăng nhập lỗi", "QRALPHA đề xuất thêm quét mã", "/bug LOGINBETA lỗi trên iPhone")

    def run_window(self, command_group):
        pipeline = self.pipeline(FakeTopicModel((command_group, topic(
            [2], type="feature", title="Đề xuất quét mã"))))
        for index, text in enumerate(self.TEXTS, 1):
            result = pipeline.process(msg(index, index, text=text))
        return result

    def test_28_a_command_fixes_the_type_of_its_group_and_the_model_cannot_change_it(self):
        result = self.run_window(topic([1, 3], type="feature", title="Tiêu đề của model"))
        self.assertEqual(result.decision, "issue")
        self.assertEqual(self.model.calls, [(1, 2, 3)])
        bug, other = self.github.calls
        self.assertEqual(bug.labels, ("bug", "triage", "source:telegram"))
        self.assertTrue(bug.title.startswith("[bug] Tiêu đề của model"))
        self.assertEqual((self.row(1)["type"], self.row(3)["type"]), ("bug", "bug"))
        # The other group in the same window keeps the model's own type.
        # 12.11: the model's `feature` is folded into `feedback` in code (label and stored type).
        self.assertEqual(other.labels, ("feedback", "triage", "source:telegram"))
        self.assertEqual(self.row(2)["type"], "feedback")
        self.assertEqual(result.reaction_message_ids, (1, 2))

    def test_a_command_group_stays_a_ticket_when_the_model_calls_it_noise(self):
        result = self.run_window(topic([1, 3], verdict="noise", type="task", title="Lời cảm ơn"))
        self.assertEqual(result.decision, "issue")
        bug = self.github.calls[0]
        self.assertEqual(bug.labels, ("bug", "triage", "source:telegram"))
        self.assertEqual(bug.title, "[bug] LOGINBETA lỗi trên iPhone")      # wording from the command
        self.assertEqual(self.row(1)["tier"], "rule")

    def test_a_command_group_the_model_cannot_judge_is_needs_human_but_keeps_its_type(self):
        self.run_window(topic([1, 3], verdict="needs_human", missing_info=("Máy nào?",)))
        bug = self.github.calls[0]
        self.assertEqual(bug.labels, ("bug", "triage", "needs-human", "source:telegram"))
        self.assertIn("- Máy nào?", bug.body)
        self.assertEqual((self.row(1)["decision"], self.row(1)["type"]), ("needs_human", "bug"))

    def test_lone_command_needs_no_model_and_an_empty_command_is_noise(self):
        pipeline = self.pipeline(FakeTopicModel(AssertionError("model must not run")))
        result = pipeline.process(msg(1, 0, text="/task cập nhật tài liệu"))
        self.assertEqual((result.decision, result.tier), ("issue", "rule"))
        empty = pipeline.process(msg(2, 5, sender=2, text="/bug"))
        self.assertEqual(empty.decision, "noise")
        self.assertEqual(self.model.calls, [])
        self.assertEqual(len(self.github.calls), 1)


class NeedsHumanTests(TopicBase):
    """Spec test 29."""

    def test_29_missing_info_makes_a_needs_human_ticket_with_a_clarify_section(self):
        pipeline = self.pipeline(FakeTopicModel((topic(
            [1], missing_info=("Thiết bị nào? @all", "<img src=x> phiên bản app?", "[x](https://evil.invalid)")),)))
        self.window(pipeline, ("app nexora bị lỗi",))
        results = self.close(pipeline)
        issue = self.github.calls[0]
        self.assertIn("needs-human", issue.labels)
        self.assertEqual(issue.labels, ("bug", "triage", "needs-human", "source:telegram"))
        self.assertIn("### ❓ Cần làm rõ", issue.body)
        self.assertIn(f"- Thiết bị nào? @{ZWSP}all", issue.body)
        self.assertNotIn("<img", issue.body)
        self.assertNotIn("://evil", issue.body)
        self.assertEqual(results[0].decision, "needs_human")
        self.assertEqual(self.decisions(1), ["needs_human"])
        self.assertEqual(self.reconciliation()["needs_human"], 1)

    def test_unusable_wording_without_questions_gets_a_fixed_clarify_line(self):
        # 12.11: a needs_human VERDICT on a direct call is `pending_review` now (see the next test);
        # unusable model wording is no judgment at all and still gives a needs-human issue.
        pipeline = self.pipeline(FakeTopicModel((topic(
            [1, 2], title="", summary="Thiếu ngữ cảnh."),)))
        self.window(pipeline, ("cái này sao vậy", "xem giúp mình"))
        self.close(pipeline)
        issue = self.github.calls[0]
        self.assertEqual(issue.labels, ("needs-human", "source:telegram"))    # the type is not guessed
        self.assertIn("### ❓ Cần làm rõ\n- Chưa đủ thông tin để phân loại", issue.body)
        self.assertTrue(issue.title.startswith("[cần phân loại] cái này sao vậy"))
        self.assertIn("Thiếu ngữ cảnh.", issue.body)
        self.assertIsNone(self.row(1)["type"])

    # 12.13 changed this test (was: a needs_human verdict on a direct call is kept for review). A tag
    # forces an issue: a needs-human one. The untagged variant below still waits for review.
    def test_needs_human_verdict_on_a_direct_call_is_a_needs_human_issue(self):
        pipeline = self.pipeline(FakeTopicModel((topic(
            [1, 2], verdict="needs_human", title="Chưa rõ", summary="Thiếu ngữ cảnh."),)))
        self.window(pipeline, ("cái này sao vậy", "xem giúp mình"))
        self.close(pipeline)
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.decisions(1, 2), ["needs_human"] * 2)
        self.assertEqual(self.row(1)["reason"], "forced_mention")
        self.assertEqual(self.reconciliation()["needs_human"], 2)

    def test_needs_human_verdict_without_a_tag_creates_a_clarification_issue(self):
        pipeline = self.pipeline(FakeTopicModel((topic(
            [1, 2], verdict="needs_human", title="Chưa rõ", summary="Thiếu ngữ cảnh."),)))
        for index, text in enumerate(("cái này sao vậy", "xem giúp mình"), 1):
            pipeline.process(msg(index, index, text=text, mentions_listener=False))
        self.close(pipeline)
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("needs-human", self.github.calls[0].labels)
        self.assertEqual(self.decisions(1, 2), ["needs_human"] * 2)
        self.assertEqual(self.row(1)["reason"], "auto_needs_human")
        self.assertEqual(self.reconciliation()["needs_human"], 2)

    def test_a_ticket_without_missing_info_has_no_clarify_section(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1], details={
            "environment": "Nexora build 42 · Android 14 · Chrome",
            "steps": ["Mở màn hình đăng nhập", "Nhấn Đăng nhập"],
            "expected": "Vào trang chủ", "actual": "Không đăng nhập được",
            "error_text": "Lỗi E42",
        }),)))
        self.window(pipeline, ("app nexora bị lỗi",))
        self.close(pipeline)
        self.assertNotIn("Cần làm rõ", self.github.calls[0].body)
        self.assertEqual(self.github.calls[0].labels, ("bug", "triage", "source:telegram"))

    def test_questions_are_capped_and_cleaned(self):
        many = tuple(f"Câu hỏi số {n}?" for n in range(9)) + ("   ",)
        parsed = parse_topic_groups(json.dumps({"groups": [{
            "topic": "t", "message_ids": [1], "verdict": "ticket", "missing_info": list(many) + ["x" * 500]}]}))
        self.assertEqual(len(parsed[0].missing_info), 5)
        parsed = parse_topic_groups(json.dumps({"groups": [{
            "topic": "t", "message_ids": [1], "verdict": "ticket", "missing_info": ["x" * 500]}]}))
        self.assertEqual(len(parsed[0].missing_info[0]), 200)

    # 12.13 changed this test (was: a low-confidence group on a direct call is kept for review): a tag
    # forces an issue, so it is a needs-human issue; the model's guess stays visible for the reviewer.
    def test_low_confidence_group_on_a_direct_call_is_a_needs_human_issue(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1], confidence=0.5),)))
        self.window(pipeline, ("app nexora bị lỗi",))
        self.close(pipeline)
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.decisions(1), ["needs_human"])
        self.assertEqual(self.row(1)["type"], "bug")
        self.assertEqual(self.row(1)["reason"], "forced_mention")

    def test_low_confidence_group_without_a_tag_creates_a_clarification_issue(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1], confidence=0.5),)))
        pipeline.process(msg(1, 1, text="app nexora bị lỗi", mentions_listener=False))
        self.close(pipeline)
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("needs-human", self.github.calls[0].labels)
        self.assertEqual(self.decisions(1), ["needs_human"])
        self.assertEqual(self.row(1)["type"], "bug")

    def test_an_unusable_group_becomes_needs_human_without_losing_the_partition(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1], title=""), topic([2], type="admin"))))
        self.window(pipeline, ("TIN MOT lỗi đăng nhập", "TIN HAI lỗi quét mã"))
        self.close(pipeline)
        self.assertEqual(len(self.github.calls), 2)
        self.assertEqual([call.labels for call in self.github.calls], [("needs-human", "source:telegram")] * 2)


class NoiseTests(TopicBase):
    # 12.12 changed this test (was: code drops a courtesy "ok" as tier-1 noise, the model never sees
    # it). Now the short message goes to the model with the rest, and the model's chat verdict
    # (not a keyword list) is what keeps it out of the issue.
    def test_a_short_courtesy_inside_a_window_goes_to_the_model_and_its_chat_verdict_keeps_it_out(self):
        pipeline = self.pipeline(FakeTopicModel((
            topic([1, 3]), topic([2], verdict="noise", type="task", category="chat", title="Xã giao"))))
        pipeline.process(msg(1, 1, text="COURTESYTEST app nexora đăng nhập lỗi"))
        pipeline.process(msg(2, 2, text="ok", mentions_listener=False))
        pipeline.process(msg(3, 3, text="máy iPhone bị văng ra"))
        self.close(pipeline)
        self.assertEqual(self.model.calls, [(1, 2, 3)])            # the model sees the courtesy
        self.assertEqual(self.decisions(1, 2, 3), ["issue", "chat", "issue"])
        self.assertEqual((self.row(2)["reason"], self.row(2)["tier"]), ("model_chat", "model"))
        self.assertNotIn("> ok", self.github.calls[0].body)
        self.assertEqual(self.groups()[f"{CHAT}:2"]["status"], "chat")
        tally = self.reconciliation()
        self.assertEqual((tally["received"], tally["issue"], tally["chat"], tally["balanced"]), (3, 2, 1, 1))
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "done")

    def test_a_message_with_no_letter_inside_a_window_is_still_set_aside_by_code(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1, 3]),)))
        self.window(pipeline, ("COURTESYTEST app nexora đăng nhập lỗi", "👍", "máy iPhone bị văng ra"))
        self.close(pipeline)
        self.assertEqual(self.model.calls, [(1, 3)])
        self.assertEqual(self.decisions(1, 2, 3), ["issue", "noise", "issue"])
        self.assertEqual((self.row(2)["reason"], self.row(2)["tier"]), ("rule_noise", "rule"))
        self.assertEqual(self.reconciliation()["balanced"], 1)

    def test_a_noise_group_from_the_model_creates_no_issue_and_records_the_reason(self):
        pipeline = self.pipeline(FakeTopicModel((
            topic([1], title="Sửa lỗi"), topic([2, 3], verdict="noise", type="task", title="Cảm ơn"))))
        self.window(pipeline, ("app nexora đăng nhập lỗi", "cảm ơn mọi người nhiều", "mọi người giỏi lắm"))
        self.close(pipeline)
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.decisions(1, 2, 3), ["issue", "noise", "noise"])
        self.assertEqual([self.row(i)["reason"] for i in (2, 3)], ["model_noise"] * 2)
        self.assertEqual(self.numbers(2, 3), [None, None])

    def test_noise_with_empty_wording_is_still_noise(self):
        pipeline = self.pipeline(FakeTopicModel((topic(
            [1], verdict="noise", title="", summary="", priority=None),)))
        self.window(pipeline, ("cảm ơn mọi người nhiều",))
        results = self.close(pipeline)
        self.assertEqual(results[0].decision, "noise")
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "noise")

    # 12.12 changed this test (was: a lone "ok" needs no model). Only a message with no letter does.
    def test_a_window_of_only_no_letter_messages_needs_no_model(self):
        pipeline = self.pipeline(FakeTopicModel(AssertionError("model must not run")))
        result = pipeline.process(msg(1, 0, text="👍"))
        self.assertEqual(result.decision, "noise")
        self.assertEqual((self.model.calls, self.github.calls), ([], []))
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "noise")
        self.assertEqual(self.row(1)["reason"], "rule_noise")

    def test_a_photo_with_a_short_caption_stays_in_a_group(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1]),)))
        pipeline.process(msg(1, 0, text="ok", has_image=True, image_size=10))
        self.close(pipeline)
        self.assertEqual(self.model.calls, [(1,)])


class FailureTests(TopicBase):
    def test_a_failing_group_does_not_stop_the_other_groups(self):
        class FirstFails(NumberedGitHub):
            def create_issue(self, issue):
                if not self.calls:
                    self.calls.append(issue)
                    raise RuntimeError("PRIVATE ghp_TEST")
                return super().create_issue(issue)

        pipeline = self.pipeline(FakeTopicModel((topic([1, 3]), topic([2, 4]))), github=FirstFails())
        self.window(pipeline, FOUR)
        self.clock.advance(60)
        with self.assertLogs("tg_triage.pipeline", level="INFO") as logs:
            result = pipeline.close_due_cases()[0]
        self.assertEqual(len(self.github.calls), 2)
        self.assertEqual(self.decisions(1, 3), ["error", "error"])
        self.assertEqual(self.decisions(2, 4), ["issue", "issue"])
        self.assertEqual(self.row(1)["error"], GITHUB_AMBIGUOUS_ERROR + " (RuntimeError)")
        self.assertEqual((result.decision, result.issue_number, result.reaction_message_ids), ("error", 102, (2,)))
        case = self.store.get_case(f"{CHAT}:1")
        self.assertEqual(case["status"], "error")
        self.assertEqual(self.reconciliation()["failed"], 2)
        self.assertNotIn("PRIVATE", "\n".join(logs.output))
        self.assertEqual(pipeline.close_due_cases(), [])

    def test_rate_limit_is_per_group_and_leaves_the_window_not_done(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1, 3]), topic([2, 4]))), limit=1)
        self.window(pipeline, FOUR)
        self.clock.advance(60)
        with self.assertLogs("tg_triage.pipeline", level="WARNING") as logs:
            result = pipeline.close_due_cases()[0]
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.decisions(1, 3), ["issue", "issue"])
        self.assertEqual(self.decisions(2, 4), ["rate_limited", "rate_limited"])
        self.assertEqual(result.decision, "rate_limited")
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "error")
        self.assertIn("Rate limit", logs.output[0])

    def test_a_group_that_cannot_be_formatted_fails_alone_and_nothing_is_left_half_done(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1]), topic([2]))))
        self.window(pipeline, FOUR[:2])
        self.clock.advance(60)
        with patch("tg_triage.pipeline.format_issue", side_effect=ValueError("PRIVATE")):
            results = pipeline.close_due_cases()
        self.assertEqual(self.github.calls, [])
        self.assertEqual([item.decision for item in results], ["error"])
        self.assertEqual(self.decisions(1, 2), ["error", "error"])
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "error")
        self.assertTrue(all(item["status"] == "error" for item in self.groups().values()))

    def test_an_error_that_escapes_after_issues_were_posted_quarantines_the_window_without_reposting(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1, 3]), topic([2, 4]))))
        self.window(pipeline, FOUR)
        self.clock.advance(60)
        with patch.object(self.store, "close_case", side_effect=OSError("PRIVATE")), \
                self.assertLogs("tg_triage.pipeline", level="ERROR") as logs:
            self.assertEqual(pipeline.close_due_cases(), [])
        self.assertEqual(len(self.github.calls), 2)
        case = self.store.get_case(f"{CHAT}:1")
        self.assertEqual(case["status"], "error")
        self.assertIn(GITHUB_AMBIGUOUS_ERROR, case["error"])
        self.assertNotIn("PRIVATE", "\n".join(logs.output))
        self.assertEqual(pipeline.close_due_cases(), [])
        self.assertEqual(len(self.github.calls), 2)

    def test_30_a_reconciliation_mismatch_never_marks_the_window_done_and_warns(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1, 3]), topic([2, 4]))))
        self.window(pipeline, FOUR)
        self.clock.advance(60)
        original = self.store.close_case

        def tamper(case_key, result, latency_ms):
            # A message whose state was lost: received (4) no longer equals what is accounted for (3).
            self.store.connection.execute(
                "UPDATE messages SET decision='queued' WHERE chat_id=? AND message_id=3", (CHAT,))
            self.store.connection.commit()
            return original(case_key, result, latency_ms)

        with patch.object(self.store, "close_case", side_effect=tamper), \
                self.assertLogs("tg_triage.pipeline", level="WARNING") as logs:
            pipeline.close_due_cases()
        case = self.store.get_case(f"{CHAT}:1")
        self.assertNotEqual(case["status"], "done")
        self.assertEqual((case["status"], case["error"]), ("error", RECONCILIATION_ERROR))
        tally = self.reconciliation()
        self.assertEqual((tally["received"], tally["issue"], tally["balanced"]), (4, 3, 0))
        output = "\n".join(logs.output)
        self.assertIn("Reconciliation mismatch", output)
        self.assertIn("received=4 accounted=3", output)
        self.assertNotIn("QRALPHA", output)
        self.assertEqual(len(self.github.calls), 2)         # the issues already made stay made

    def test_a_balanced_window_is_the_only_one_that_reaches_done(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1, 3]), topic([2, 4]))))
        self.window(pipeline, FOUR)
        self.close(pipeline)
        self.assertEqual(self.reconciliation()["status"], "done")
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "done")


class DryRunTests(TopicBase):
    def test_31_dry_run_keeps_groups_and_results_in_sqlite_and_never_calls_github(self):
        github = FakeGitHub(error=AssertionError("GitHub must not be called"))
        uploader = FakeUploader()
        downloader = FakeDownloader({1: (JPG, "x.jpg")})
        pipeline = self.pipeline(FakeTopicModel((topic([1, 3]), topic([2, 4], missing_info=("Máy nào?",)))),
                                 mode="dry-run", github=github, uploader=uploader, downloader=downloader)
        pipeline.process(msg(1, 0, text=FOUR[0], has_image=True, image_size=2048))
        for index, text in enumerate(FOUR[1:], 2):
            pipeline.process(msg(index, index, text=text))
        results = self.close(pipeline)
        self.assertEqual(github.calls, [])
        self.assertEqual((uploader.releases, uploader.uploads), ([], []))     # no gh in dry-run
        self.assertEqual(self.decisions(1, 2, 3, 4),
                         ["dry_run_issue", "dry_run_needs_human", "dry_run_issue", "dry_run_needs_human"])
        groups = self.groups()
        self.assertEqual(len(groups), 2)
        self.assertEqual([(g["status"], g["decision"], g["issue_number"]) for g in groups.values()],
                         [("done", "dry_run_issue", None), ("done", "dry_run_needs_human", None)])
        self.assertEqual(self.numbers(1, 2, 3, 4), [None] * 4)
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "done")
        self.assertEqual(self.reconciliation()["balanced"], 1)
        self.assertEqual(results[0].reaction_message_ids, ())
        self.assertEqual(results[0].images, 1)


class AttachmentTests(TopicBase):
    def test_images_travel_only_with_their_own_group_under_a_release_per_group(self):
        uploader = FakeUploader()
        downloader = FakeDownloader({1: (JPG, "a.jpg"), 2: (JPG, "b.jpg")})
        pipeline = self.pipeline(FakeTopicModel((topic([1, 3]), topic([2, 4]))),
                                 uploader=uploader, downloader=downloader)
        pipeline.process(msg(1, 0, text=FOUR[0], has_image=True, image_size=2048))
        pipeline.process(msg(2, 1, text=FOUR[1], has_image=True, image_size=2048))
        pipeline.process(msg(3, 2, text=FOUR[2]))
        pipeline.process(msg(4, 3, text=FOUR[3]))
        self.close(pipeline)
        first_tag, second_tag = release_tag(f"{CHAT}:1"), release_tag(f"{CHAT}:2")
        self.assertNotEqual(first_tag, second_tag)
        self.assertEqual([(item["tag"], item["asset"]) for item in uploader.uploads],
                         [(first_tag, "1-1.jpg"), (second_tag, "2-1.jpg")])
        qr, login = self.github.calls
        self.assertIn(f"/{first_tag}/1-1.jpg", qr.body)
        self.assertNotIn("2-1.jpg", qr.body)
        self.assertIn(f"/{second_tag}/2-1.jpg", login.body)
        self.assertNotIn("1-1.jpg", login.body)


class FollowupTests(TopicBase):
    def test_a_reply_to_a_message_of_the_second_issue_follows_that_issue(self):
        pipeline = self.pipeline(FakeTopicModel((topic([1, 3]), topic([2, 4]))))
        self.window(pipeline, FOUR)
        self.close(pipeline)
        late = pipeline.process(msg(5, 61, sender=7, reply_to_id=4, text="mình cũng bị vậy"))
        also = pipeline.process(msg(6, 62, sender=8, reply_to_id=1, text="bên mình cũng thế"))
        self.assertEqual((late.decision, also.decision), ("followup", "followup"))
        self.assertEqual(self.numbers(5, 6), [102, 101])
        self.assertEqual(len(self.github.calls), 2)


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "triage.sqlite3"
        self.clock = Clock()

    def open(self):
        store = Store(self.path, clock=self.clock)
        self.addCleanup(store.close)
        return store

    def seed(self, github):
        store = Store(self.path, clock=self.clock)
        model = FakeTopicModel((topic([1, 3]), topic([2, 4])))
        pipeline = Pipeline(config("live", window=15), store, model, github)
        for index, text in enumerate(FOUR, 1):
            pipeline.process(msg(index, index // 2, text=text))
        self.clock.advance(60)
        return store, pipeline

    def test_restart_after_one_group_posted_never_posts_again(self):
        posted = []

        def crash_on_second_issue():
            posted.append(1)
            if len(posted) == 2:
                raise Crash()

        store, pipeline = self.seed(NumberedGitHub(before_create=crash_on_second_issue))
        with self.assertRaises(Crash):
            pipeline.close_due_cases()
        store.close()
        reopened = self.open()
        github = NumberedGitHub()
        again = Pipeline(config("live", window=15), reopened, FakeTopicModel((topic([1, 3]), topic([2, 4]))),
                         github)
        self.clock.advance(60)
        self.assertEqual(again.close_due_cases(), [])
        self.assertEqual(github.calls, [])
        row = lambda i: reopened.get(CHAT, i)
        self.assertEqual([(row(i)["decision"], row(i)["issue_number"]) for i in (1, 3)],
                         [("issue", 101), ("issue", 101)])      # the posted group keeps its issue
        self.assertEqual([row(i)["decision"] for i in (2, 4)], ["error", "error"])   # for a human
        self.assertIn(GITHUB_AMBIGUOUS_ERROR, row(2)["error"])
        self.assertEqual(reopened.get_case(f"{CHAT}:1")["status"], "error")
        self.assertEqual({item["status"] for item in reopened.case_groups(f"{CHAT}:1")} - {"done"}, {"error"})

    def test_restart_before_anything_was_posted_replans_and_posts_each_group_once(self):
        store, pipeline = self.seed(NumberedGitHub())
        with patch("tg_triage.pipeline.format_issue", side_effect=Crash()):
            with self.assertRaises(Crash):
                pipeline.close_due_cases()
        self.assertEqual(len(store.case_groups(f"{CHAT}:1")), 2)       # the plan had been saved
        store.close()
        reopened = self.open()
        self.assertEqual(reopened.case_groups(f"{CHAT}:1"), [])         # discarded: nothing was sent
        self.assertEqual(reopened.get_case(f"{CHAT}:1")["status"], "open")
        github = NumberedGitHub()
        again = Pipeline(config("live", window=15), reopened, FakeTopicModel((topic([1, 3]), topic([2, 4]))),
                         github)
        self.clock.advance(60)
        self.assertEqual(len(again.close_due_cases()), 1)
        self.assertEqual(len(github.calls), 2)
        self.assertEqual(len(reopened.case_groups(f"{CHAT}:1")), 2)
        self.assertEqual([reopened.get(CHAT, i)["issue_number"] for i in (1, 2, 3, 4)], [101, 102, 101, 102])
        self.assertEqual(again.close_due_cases(), [])
        self.assertEqual(len(github.calls), 2)

    def test_a_finished_window_is_not_analyzed_or_posted_again_after_restart(self):
        store, pipeline = self.seed(NumberedGitHub())
        pipeline.close_due_cases()
        store.close()
        reopened = self.open()
        github = NumberedGitHub()
        again = Pipeline(config("live", window=15), reopened, FakeTopicModel((topic([1, 3]), topic([2, 4]))),
                         github)
        self.clock.advance(60)
        self.assertEqual(again.close_due_cases(), [])
        self.assertEqual(github.calls, [])


class MigrationTests(unittest.TestCase):
    def test_database_from_before_topic_groups_gains_the_new_columns_and_tables(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute('''CREATE TABLE messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, received_at TEXT NOT NULL,
                sender_name TEXT NOT NULL, text TEXT NOT NULL, tier TEXT NOT NULL,
                type TEXT, confidence REAL, title TEXT, summary TEXT, priority TEXT,
                decision TEXT NOT NULL, issue_number INTEGER, issue_url TEXT, error TEXT,
                processed_at TEXT, latency_ms INTEGER, github_called_at TEXT, chat_username TEXT,
                attempts INTEGER NOT NULL DEFAULT 0, case_key TEXT, sender_id INTEGER,
                reply_to_id INTEGER, is_forward INTEGER NOT NULL DEFAULT 0,
                has_image INTEGER NOT NULL DEFAULT 0, image_size INTEGER, image_mime TEXT,
                PRIMARY KEY (chat_id, message_id))''')
            connection.commit()
            connection.close()
            store = Store(path)
            try:
                columns = {row[1] for row in store.connection.execute("PRAGMA table_info(messages)")}
                self.assertTrue({"group_key", "reason"} <= columns)
                tables = {row[0] for row in store.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                self.assertTrue({"groups", "reconciliation"} <= tables)
            finally:
                store.close()
            Store(path).close()      # idempotent

    def test_owner_csv_exports_the_group_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(":memory:")
            out = Path(directory) / "out.csv"
            try:
                store.export_csv(out)
            finally:
                store.close()
            header = out.read_text(encoding="utf-8-sig").splitlines()[0]
            self.assertIn("group_key", header)
            self.assertIn("reason", header)


class ReactionTests(unittest.IsolatedAsyncioTestCase):
    """The OK hand goes on the first message of EACH group that got an issue, live only."""

    def setUp(self):
        self.clock = Clock()
        self.store = Store(":memory:", clock=self.clock)
        self.addCleanup(self.store.close)

    async def drive(self, pipeline, send, items, *, advance=True, until=lambda: False):
        queue = asyncio.Queue()
        for item in items:
            queue.put_nowait(item)

        async def later():
            await asyncio.sleep(0.1)
            self.clock.advance(60)

        advancer = asyncio.create_task(later()) if advance else None
        with patch.object(telegram_io, "SWEEP_SECONDS", 0.02):
            task = asyncio.create_task(telegram_io.worker(queue, pipeline, react=send))
            try:
                await asyncio.wait_for(queue.join(), 5)
                for _ in range(250):
                    if until():
                        break
                    await asyncio.sleep(0.02)
                await asyncio.sleep(0.1)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if advancer:
            await advancer

    async def test_sweep_closing_a_window_reacts_once_on_each_issue_group_first_message(self):
        send, github = Recorder(), NumberedGitHub()
        # 12.12: "ok" now reaches the model, which calls it chat (a tagged chat group gets no reaction).
        model = FakeTopicModel((topic([1, 3]), topic([2, 4]),
                                topic([5], verdict="noise", type="task", category="chat")))
        pipeline = Pipeline(config("live", window=15), self.store, model, github)
        items = [msg(1, 0, text=FOUR[0]), msg(2, 1, text=FOUR[1]), msg(3, 2, text=FOUR[2]),
                 msg(4, 3, text=FOUR[3]), msg(5, 4, text="ok")]
        await self.drive(pipeline, send, items, until=lambda: (CHAT, 2, OK) in send.calls)
        self.assertEqual(len(github.calls), 2)
        # 12.10: hearts go on each group first message while the window runs, OK hands after it; not on 3, 4 or the courtesy.
        self.assertEqual(send.calls, [(CHAT, 1, HEART), (CHAT, 2, HEART), (CHAT, 1, OK), (CHAT, 2, OK)])

    async def test_a_command_closing_the_window_reacts_on_every_group_first_message(self):
        send, github = Recorder(), NumberedGitHub()
        pipeline = Pipeline(config("live", window=15), self.store,
                            FakeTopicModel((topic([1, 3]), topic([2]))), github)
        items = [msg(1, 0, text="LOGINBETA đăng nhập lỗi"), msg(2, 1, text="QRALPHA quét mã không được"),
                 msg(3, 2, text="/bug LOGINBETA iPhone")]
        await self.drive(pipeline, send, items, advance=False, until=lambda: (CHAT, 2, OK) in send.calls)
        # 12.10: hearts (during the run) then OK hands (after it) on each group first message; nothing on the command (3).
        self.assertEqual(send.calls, [(CHAT, 1, HEART), (CHAT, 2, HEART), (CHAT, 1, OK), (CHAT, 2, OK)])

    async def test_dry_run_sends_no_ok_hand_for_any_group(self):
        send = Recorder()
        pipeline = Pipeline(config("dry-run", window=15), self.store,
                            FakeTopicModel((topic([1, 3]), topic([2, 4]))), None)
        items = [msg(i, i, text=text) for i, text in enumerate(FOUR, 1)]
        await self.drive(pipeline, send, items,
                         until=lambda: self.store.get_case(f"{CHAT}:1")["status"] == "done")
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "done")
        # 12.10: dry-run hearts each ticket group's first message and never adds an OK hand.
        self.assertEqual(send.calls, [(CHAT, 1, HEART), (CHAT, 2, HEART)])

    async def test_a_group_that_hit_the_rate_limit_gets_no_ok_hand(self):
        send, github = Recorder(), NumberedGitHub()
        pipeline = Pipeline(config("live", 1, window=15), self.store,
                            FakeTopicModel((topic([1, 3]), topic([2, 4]))), github)
        items = [msg(i, i, text=text) for i, text in enumerate(FOUR, 1)]
        with self.assertLogs("tg_triage.pipeline", level="WARNING"):
            await self.drive(pipeline, send, items, until=lambda: (CHAT, 1, OK) in send.calls)
        # 12.10: the rate-limited group (2) is still a ticket, so it is hearted, but gets no OK hand.
        self.assertEqual(send.calls, [(CHAT, 1, HEART), (CHAT, 2, HEART), (CHAT, 1, OK)])


class ClassifierTests(unittest.TestCase):
    def split(self, http, items):
        client = Classifier(config().ollama, opener=http, prompt="fixed", case_prompt="CASE",
                            topics_prompt="TOPICS PROMPT")
        return client.split_topics(items)

    @staticmethod
    def envelope(content):
        return {"message": {"content": content}}

    @staticmethod
    def answer(**extra):
        group = {"topic": "Đăng nhập", "message_ids": [1, 2], "verdict": "ticket", "type": "bug",
                 "title": "Lỗi đăng nhập", "summary": "Không đăng nhập được.", "priority": "high",
                 "confidence": 0.9, "missing_info": ["Máy nào?"]}
        group.update(extra)
        return json.dumps({"groups": [group]}, ensure_ascii=False)

    def test_request_has_the_topics_prompt_first_then_the_numbered_transcript_in_order(self):
        from tg_triage.classifier import DETAILS_PROMPT

        http = FakeHTTP(self.envelope(self.answer()))
        items = [message(1, "đăng nhập lỗi", received_at=at(0)), message(2, "bấm là văng ra", received_at=at(1))]
        groups = self.split(http, items)
        self.assertEqual(len(http.calls), 1)
        request, timeout = http.calls[0]
        body = json.loads(request.data)
        self.assertEqual((request.full_url, timeout), ("http://127.0.0.1:11434/api/chat", 60))
        # 12.12: reasoning is on by default (was off).
        self.assertEqual((body["stream"], body["think"], body["options"]), (False, True, {"temperature": 0}))
        self.assertEqual(body["format"], TOPIC_SCHEMA)
        group_schema = body["format"]["properties"]["groups"]["items"]
        self.assertIn("details", group_schema["properties"])
        self.assertNotIn("details", group_schema["required"])
        self.assertEqual([item["role"] for item in body["messages"]], ["system", "user"])
        self.assertEqual(body["messages"][0]["content"], "TOPICS PROMPT\n" + DETAILS_PROMPT)
        user = body["messages"][1]["content"]
        self.assertEqual(user, build_transcript(items))
        self.assertLess(user.index("[1] "), user.index("[2] "))
        self.assertEqual((groups[0].message_ids, groups[0].verdict, groups[0].missing_info),
                         ((1, 2), "ticket", ("Máy nào?",)))

    def test_schema_requires_every_group_field(self):
        item = TOPIC_SCHEMA["properties"]["groups"]["items"]
        # 12.12: `verdict` is no longer required (derived from category / actionable); `actionable`
        # and `reason` are.
        self.assertEqual(set(item["required"]), {
            "topic", "message_ids", "type", "category", "actionable", "title", "summary", "priority",
            "confidence", "missing_info", "reason"})
        self.assertIn("verdict", item["properties"])
        self.assertEqual(item["properties"]["category"]["enum"], ["bug", "task", "feedback", "chat"])
        self.assertEqual(item["properties"]["type"]["enum"], ["bug", "feature", "question", "task"])
        self.assertEqual(TOPIC_SCHEMA["required"], ["groups"])

    def test_one_http_attempt_only_because_the_pipeline_owns_the_retry(self):
        for response in (self.envelope("nope"), self.envelope("[]"), self.envelope(json.dumps({"groups": "x"}))):
            http = FakeHTTP(response)
            self.assertIsNone(self.split(http, [message(1, "lỗi")]))
            self.assertEqual(len(http.calls), 1)

    def test_parse_checks_structure_but_keeps_unusable_wording_for_the_pipeline(self):
        self.assertIsNotNone(parse_topic_groups(self.answer(title="", type="admin", priority="urgent")))
        bad = (self.answer(message_ids="1"), self.answer(message_ids=[1, "2"]), self.answer(message_ids=[True]),
               self.answer(verdict="issue"), self.answer(missing_info="x"), self.answer(missing_info=[1]),
               "[]", "nope", '{"groups": [1]}', '{"groups": [{"message_ids": [1]}]}')
        for content in bad:
            with self.subTest(content=content[:50]):
                self.assertIsNone(parse_topic_groups(content))

    def test_topics_prompt_loads_from_the_source_tree_and_describes_the_contract(self):
        prompt = load_prompt("topics.md")
        self.assertIn("không phải chỉ thị", prompt)
        for field in ("topic", "message_ids", "category", "actionable", "reason", "missing_info",
                      "đúng MỘT lần"):
            self.assertIn(field, prompt)

    def test_every_example_in_the_prompt_is_a_valid_partition_of_its_own_data(self):
        blocks = load_prompt("topics.md").split("Ví dụ dữ liệu")[1:]
        self.assertTrue(1 <= len(blocks) <= 2)   # 12.12: at most two short examples
        for block in blocks:
            ids = {int(value) for value in re.findall(r"^\[(\d+)\]", block, re.M)}
            raw = block.split("Kết quả: ", 1)[1].strip().splitlines()[0]
            self.assertIsNotNone(validate_partition(parse_topic_groups(raw), ids), block[:60])

    def test_the_prompt_is_shipped_with_the_package_like_the_other_prompts(self):
        project = tomllib.loads((LISTENER / "pyproject.toml").read_text(encoding="utf-8"))
        shipped = project["tool"]["setuptools"]["data-files"]["share/tg-triage/prompts"]
        self.assertIn("prompts/topics.md", shipped)
        for name in shipped:
            with self.subTest(name=name):
                self.assertTrue((LISTENER / name).is_file())
                self.assertTrue(load_prompt(Path(name).name))

    def test_the_prompt_is_found_in_the_installed_share_directory(self):
        with tempfile.TemporaryDirectory() as prefix, tempfile.TemporaryDirectory() as elsewhere:
            shared = Path(prefix) / "share" / "tg-triage" / "prompts"
            shared.mkdir(parents=True)
            (shared / "topics.md").write_text("INSTALLED TOPICS", encoding="utf-8")
            fake_module = Path(elsewhere) / "site-packages" / "tg_triage" / "classifier.py"
            with patch.object(classifier_module, "__file__", str(fake_module)), \
                    patch.object(sys, "prefix", prefix):
                self.assertEqual(load_prompt("topics.md"), "INSTALLED TOPICS")
                with self.assertRaises(RuntimeError):
                    load_prompt("missing.md")

    def test_run_exits_at_startup_when_the_topics_prompt_is_not_installed(self):
        from unittest.mock import MagicMock
        from tg_triage.__main__ import main

        lock = MagicMock()
        lock.__enter__.return_value = lock
        with patch("tg_triage.__main__.load_config", return_value=config()), \
                patch("tg_triage.__main__.validate_data_directories"), \
                patch("tg_triage.__main__.ProcessLock", return_value=lock), \
                patch("tg_triage.__main__.Store"), \
                patch("tg_triage.__main__.load_prompt", side_effect=RuntimeError("PRIVATE")), \
                patch("tg_triage.__main__.asyncio.run") as run, \
                self.assertLogs("tg_triage.__main__", level="ERROR") as logs:
            self.assertEqual(main(["run"]), 1)
        run.assert_not_called()
        self.assertIn("exception=RuntimeError", "\n".join(logs.output))
        self.assertNotIn("PRIVATE", "\n".join(logs.output))

    def test_the_topics_prompt_is_loaded_lazily_and_only_once(self):
        client = Classifier(config().ollama, opener=FakeHTTP(self.envelope(self.answer())), prompt="fixed")
        self.assertIsNone(client.topics_prompt)
        client.split_topics([message(1, "lỗi")])
        self.assertEqual(client.topics_prompt, load_prompt("topics.md"))


class FormatTests(unittest.TestCase):
    def render(self, *, needs_human=True, **extra):
        return format_issue(message(1, "app nexora lỗi"), classification(), "model", needs_human, **extra)

    def render_untrusted(self, **extra):
        """The pipeline passes no classification for a needs-human issue whose type is a guess."""
        return format_issue(message(1, "app nexora lỗi"), None, "model", True, **extra)

    def test_single_case_rendering_is_unchanged_without_clarify(self):
        issue = self.render()
        self.assertNotIn("Cần làm rõ", issue.body)
        self.assertEqual(issue.labels, ("needs-human", "source:telegram"))
        self.assertTrue(issue.title.startswith("[cần phân loại] app nexora lỗi"))

    def test_clarify_section_lists_cleaned_questions_after_the_summary(self):
        issue = self.render_untrusted(clarify=("Một câu hỏi?", "@all <b>hai</b>", "", 5),
                                      text=("Tiêu đề", "Tóm tắt model"))
        self.assertIn("### ❓ Cần làm rõ\n- Một câu hỏi?\n- @" + ZWSP + "all &lt;b&gt;hai&lt;/b&gt;\n\n", issue.body)
        self.assertLess(issue.body.index("Tóm tắt"), issue.body.index("Cần làm rõ"))
        self.assertLess(issue.body.index("Cần làm rõ"), issue.body.index("<details>"))
        self.assertEqual(issue.title, "[cần phân loại] Tiêu đề")
        self.assertEqual(issue.labels, ("needs-human", "source:telegram"))

    def test_a_trusted_type_adds_its_label_and_title_prefix(self):
        issue = self.render(clarify=("Máy nào?",))
        self.assertEqual(issue.labels, ("bug", "triage", "needs-human", "source:telegram"))
        self.assertTrue(issue.title.startswith("[bug] "))

    def test_max_quoted_renders_every_message_of_the_group(self):
        items = [message(index, f"tin số {index}", received_at=at(index)) for index in range(1, 31)]
        capped = format_issue(items[0], classification(), "model", False, case_messages=items)
        full = format_issue(items[0], classification(), "model", False, case_messages=items, max_quoted=30)
        self.assertIn("(còn 20 tin khác", capped.body)
        self.assertNotIn("còn", full.body)
        self.assertIn("tin số 30", full.body)


if __name__ == "__main__":
    unittest.main()
