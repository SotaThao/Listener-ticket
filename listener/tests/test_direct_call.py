"""Assistant definition 12.11 / 12.13: bug, task and feedback become issues (a tag or a /command with
content forces one, a confident model verdict makes one on its own); questions and chat, noise and
unsure untagged groups are kept in SQLite as chat / noise / pending_review.

Offline: fake Telegram events, fake model, fake GitHub, in-memory or temporary SQLite.
"""

import asyncio
import csv
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from tg_triage import rules, telegram_io
from tg_triage.classifier import (TOPIC_SCHEMA, Classifier, build_transcript, load_prompt,
                                  parse_case_analysis, parse_topic_groups)
from tg_triage.config import OllamaConfig
from tg_triage.models import CaseAnalysis, canonical_category
from tg_triage.pipeline import Pipeline
from tg_triage.store import COLUMNS, Store
from tests.helpers import (NOW, FakeCaseModel, FakeGitHub, FakeHTTP, FakeTopicModel, NumberedGitHub,
                           at, classification, config, message, topic)
from tests.test_cases import CHAT, CaseBase, msg
from tests.test_reactions import HEART, OK, Recorder
from tests.test_topics import TopicBase

INVITE = "invite anh vào repo đi"
REAL_BUG = "không đăng nhập được app nexora, báo sai mật khẩu"


def chat_group(ids, **extra):
    """What the new prompt asks for plain talk: verdict noise, category chat."""
    fields = dict(verdict="noise", type="task", category="chat", title="Trao đổi thường",
                  summary="Trao đổi giữa thành viên.", priority="low")
    fields.update(extra)
    return topic(ids, **fields)


class Beats:
    """Sync stand-in for the pipeline's reactor: records every reaction the pipeline asks for."""

    def __init__(self):
        self.calls = []

    def __call__(self, chat_id, message_id, emoji):
        self.calls.append((chat_id, message_id, emoji))
        return True


class GateBase(TopicBase):
    def run_window(self, texts, *answers, mode="live", tagged=(), beats=None, github=None):
        """One sender's messages numbered from 1; `tagged` = 1-based indexes that tag the listener."""
        pipeline = self.pipeline(FakeTopicModel(*answers), mode=mode, github=github, reactor=beats)
        for index, text in enumerate(texts, 1):
            pipeline.process(msg(index, index, text=text, mentions_listener=index in tagged))
        return self.close(pipeline)

    def total(self, decision):
        return self.store.connection.execute(
            "SELECT COUNT(*) FROM messages WHERE decision = ?", (decision,)).fetchone()[0]


class Test37To42(GateBase):
    def test_37_plain_talk_without_a_tag_is_chat_no_ticket_no_reaction(self):
        beats = Beats()
        self.run_window((INVITE,), (chat_group([1]),), beats=beats)
        row = self.row(1)
        self.assertEqual((row["decision"], row["type"], row["reason"]), ("chat", "chat", "model_chat"))
        self.assertEqual((row["issue_number"], self.github.calls, beats.calls), (None, [], []))
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "chat")

    # 12.13 changed this test (was: untagged talk the model calls a task is still not a ticket ->
    # pending_review / no_direct_call). Task and feedback no longer need a tag: a confident task is an
    # automatic issue (`auto_task`), hearted like any issue. Not sure (below the threshold) => pending_review.
    def test_37_untagged_talk_the_model_calls_a_task_is_an_automatic_issue(self):
        beats = Beats()
        self.run_window((INVITE,), (topic([1], type="task", category="task"),), beats=beats)
        self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]), ("issue", "auto_task"))
        self.assertEqual((len(self.github.calls), beats.calls), (1, [(CHAT, 1, HEART)]))

    def test_37_untagged_task_or_feedback_below_the_threshold_asks_for_clarification(self):
        for category in ("task", "feedback"):
            with self.subTest(category=category):
                self.setUp()
                beats = Beats()
                self.run_window((INVITE,), (topic([1], type=category, category=category, confidence=0.5),),
                                beats=beats)
                self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]),
                                 ("needs_human", "auto_needs_human"))
                self.assertEqual((len(self.github.calls), beats.calls), (1, [(CHAT, 1, HEART)]))
                self.assertEqual(self.github.calls[0].labels, ("needs-human", "source:telegram"))

    def test_37_untagged_feedback_is_an_automatic_issue(self):
        self.run_window(("nên thêm nút xuất Excel ở màn báo cáo",),
                        (topic([1], type="feature", category="feedback", title="Thêm nút xuất Excel"),))
        self.assertEqual((self.row(1)["decision"], self.row(1)["reason"], self.row(1)["type"]),
                         ("issue", "auto_feedback", "feedback"))
        self.assertEqual(self.github.calls[0].labels, ("feedback", "triage", "source:telegram"))

    def test_38_the_same_text_with_a_tag_is_a_task_and_one_issue(self):
        beats = Beats()
        results = self.run_window(
            (INVITE,), (topic([1], type="task", category="task", title="Mời anh vào repo"),),
            tagged=(1,), beats=beats)
        self.assertEqual(len(self.github.calls), 1)
        issue = self.github.calls[0]
        self.assertEqual(issue.labels, ("task", "triage", "source:telegram"))
        self.assertTrue(issue.title.startswith("[task] Mời anh vào repo"))
        self.assertEqual(self.decisions(1), ["issue"])
        self.assertEqual(beats.calls, [(CHAT, 1, HEART)])          # the heart; the OK hand is the worker's
        self.assertEqual(results[0].reaction_message_ids, (1,))

    # 12.13 changed this test (was: a real bug without a tag waits for review): a confident bug is an
    # automatic issue (`auto_bug`), hearted and (live) OK-handed; below the threshold it waits, in the CSV.
    def test_39_a_real_bug_without_a_tag_is_an_automatic_issue(self):
        beats = Beats()
        results = self.run_window((REAL_BUG,), (topic([1], type="bug", category="bug"),), beats=beats)
        row = self.row(1)
        self.assertEqual((row["decision"], row["reason"], row["type"]), ("issue", "auto_bug", "bug"))
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(beats.calls, [(CHAT, 1, HEART)])        # the heart; the OK hand is the worker's
        self.assertEqual(results[0].reaction_message_ids, (1,))
        self.assertFalse(row["mentions_listener"])

    def test_39_a_bug_without_a_tag_below_the_threshold_asks_for_clarification_and_is_in_the_csv(self):
        beats = Beats()
        self.run_window((REAL_BUG,), (topic([1], type="bug", category="bug", confidence=0.6),), beats=beats)
        row = self.row(1)
        self.assertEqual((row["decision"], row["reason"], row["type"]),
                         ("needs_human", "auto_needs_human", "bug"))
        self.assertEqual((len(self.github.calls), beats.calls, row["issue_number"]), (1, [(CHAT, 1, HEART)], 101))
        self.assertEqual(self.github.calls[0].labels, ("needs-human", "source:telegram"))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "review.csv"
            self.store.export_csv(out)
            rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
        self.assertEqual([(item["decision"], item["reason"], item["type"], item["mentions_listener"])
                          for item in rows], [("needs_human", "auto_needs_human", "bug", "0")])
        self.assertEqual(rows[0]["text"], REAL_BUG)

    def test_39_an_automatic_bug_in_dry_run_is_hearted_but_never_ok_handed(self):
        beats = Beats()
        results = self.run_window((REAL_BUG,), (topic([1], type="bug", category="bug"),), mode="dry-run",
                                  beats=beats)
        self.assertEqual((self.decisions(1), self.row(1)["reason"]), (["dry_run_issue"], "auto_bug"))
        self.assertEqual((self.github.calls, beats.calls, results[0].reaction_message_ids),
                         ([], [(CHAT, 1, HEART)], ()))

    def test_39_an_email_with_an_at_sign_is_not_a_tag_the_bug_is_automatic_not_forced(self):
        text = "đăng nhập lỗi, báo cho em qua linh@example.com nhé"
        self.run_window((text,), (topic([1], type="bug", category="bug"),))
        row = self.row(1)
        self.assertEqual((row["decision"], row["reason"], row["mentions_listener"]), ("issue", "auto_bug", 0))
        self.assertNotIn("linh@example.com", self.github.calls[0].title + self.github.calls[0].body)

    def test_39_the_same_email_group_the_model_calls_chat_is_chat_not_forced(self):
        self.run_window(("em gửi mail linh@example.com cho anh rồi nhé",), (chat_group([1]),))
        row = self.row(1)
        self.assertEqual((row["decision"], row["reason"], self.github.calls), ("chat", "model_chat", []))

    def test_40_a_bug_command_needs_no_tag(self):
        self.run_window(("/bug nút lưu không bấm được",), (topic([1], type="bug", category="bug"),))
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.github.calls[0].labels, ("bug", "triage", "source:telegram"))
        self.assertFalse(self.row(1)["mentions_listener"])
        self.assertEqual(self.decisions(1), ["issue"])

    # 12.13 changed this test (was: a tagged email / greeting the model calls chat is kept as
    # pending_review / direct_call_chat). A tag now forces an issue, EXCEPT when the group has no product
    # content: the safety block (`safety_block`) keeps it out of GitHub, structurally and without keywords
    # (no word once e-mail / phone / @handle are removed; or a few words when the model said chat).
    def test_41_a_tagged_email_or_greeting_the_model_calls_chat_is_the_safety_block_not_an_issue(self):
        cases = (("linh@example.com",), ("chào anh",), ("Số em là 0901234567 nhé",),
                 ("xin chào mọi người ạ", "linh@example.com"), ("@vlink_bot ok",), ("linh@example.com", "0901234567"))
        for texts in cases:
            with self.subTest(texts=texts):
                self.setUp()
                beats = Beats()
                self.run_window(texts, (chat_group(list(range(1, len(texts) + 1))),),
                                tagged=range(1, 9), beats=beats)
                self.assertEqual(self.github.calls, [])
                self.assertEqual(beats.calls, [])
                self.assertEqual(set(self.decisions(1)), {"noise"})
                self.assertEqual(self.row(1)["reason"], "safety_block")

    def test_41_a_tagged_group_that_is_only_contact_data_is_blocked_even_when_the_model_says_bug(self):
        for texts in (("linh@example.com",), ("linh@example.com", "0901234567")):
            with self.subTest(texts=texts):
                self.setUp()
                self.run_window(texts, (topic(list(range(1, len(texts) + 1)), type="bug", category="bug"),),
                                tagged=range(1, 9))
                self.assertEqual(self.github.calls, [])
                self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]), ("noise", "safety_block"))

    def test_41_an_empty_command_is_not_an_issue_even_with_a_tag(self):
        self.run_window(("/bug",), (topic([1], type="bug", category="bug"),), tagged=(1,))
        self.assertEqual((self.github.calls, self.decisions(1), self.row(1)["reason"]),
                         ([], ["noise"], "safety_block"))

    def test_41_the_safety_block_never_touches_a_real_bug(self):
        # A short real report, and a bug that also shares contact data, stay issues.
        for texts in (("app crash",), ("lỗi đăng nhập", "mail em linh@example.com")):
            with self.subTest(texts=texts):
                self.setUp()
                self.run_window(texts, (topic(list(range(1, len(texts) + 1)), type="bug", category="bug"),),
                                tagged=range(1, 9))
                self.assertEqual(len(self.github.calls), 1)
                self.assertEqual(self.row(1)["decision"], "issue")

    # 12.13: a tag forces an issue whatever the model said; when the model said chat the type is not
    # trusted, so it is a needs-human issue (label `needs-human`, no type), hearted once.
    def test_41_a_tagged_group_the_model_calls_chat_is_a_forced_needs_human_issue(self):
        for text in ("@vlink_bot ok em cảm ơn nhé", "hôm nay họp mấy giờ nhỉ", INVITE):
            with self.subTest(text=text):
                self.setUp()
                beats = Beats()
                self.run_window((text,), (chat_group([1]),), tagged=(1,), beats=beats)
                self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]),
                                 ("needs_human", "forced_mention"))
                self.assertEqual(len(self.github.calls), 1)
                self.assertEqual(self.github.calls[0].labels, ("needs-human", "source:telegram"))
                self.assertEqual(beats.calls, [(CHAT, 1, HEART)])

    # The model, not a word list, now decides; the one lock left is "no tag and no command".
    def test_41_a_tagged_group_the_model_calls_a_task_is_a_ticket_whatever_its_words(self):
        self.run_window(("@vlink_bot ok em cảm ơn nhé, nhờ anh mở lại quyền cho em",),
                        (topic([1], type="task", category="task", title="Mở lại quyền"),), tagged=(1,))
        self.assertEqual(self.decisions(1), ["issue"])
        self.assertEqual(len(self.github.calls), 1)

    def test_41_a_lone_command_with_only_an_email_is_still_a_ticket_with_the_email_hidden(self):
        # Was: noise by the hard block. An explicit command is a ticket (12.12); the address is redacted.
        self.run_window(("/bug linh@example.com",), (topic([1], type="bug", category="bug"),))
        self.assertEqual(self.decisions(1), ["issue"])
        self.assertEqual(len(self.github.calls), 1)
        self.assertNotIn("linh@example.com", self.github.calls[0].title + self.github.calls[0].body)

    def test_41_one_real_word_keeps_the_group_out_of_the_hard_block(self):
        self.run_window(("xin chào anh ạ", "app lỗi đăng nhập", "linh@example.com"),
                        (topic([1, 2, 3]),), tagged=(1,))
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.decisions(1, 2, 3), ["issue"] * 3)

    def test_a_short_tag_only_message_still_makes_the_window_a_direct_call(self):
        # "@Mybot" is short but carries the tag: it must stay in the group.
        self.run_window((REAL_BUG, "@Mybot"), (topic([1, 2]),), tagged=(2,))
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.decisions(1, 2), ["issue", "issue"])

    # 12.12 changed this test (was: a short "ok" is tier-1 noise, never sent to the model). Now it is
    # sent to the model with the rest of the window, and the model's answer decides.
    def test_a_short_message_reaches_the_model_instead_of_being_dropped_by_code(self):
        self.run_window((REAL_BUG, "ok"), (topic([1]), chat_group([2])), tagged=(1,))
        self.assertEqual(self.model.calls, [(1, 2)])
        self.assertEqual(self.decisions(1, 2), ["issue", "chat"])
        self.assertEqual((self.row(2)["reason"], self.row(2)["tier"]), ("model_chat", "model"))

    def test_a_message_with_no_letter_is_still_set_aside_by_code_as_tier_one_noise(self):
        self.run_window((REAL_BUG, "👍"), (topic([1]),), tagged=(1, 2))
        self.assertEqual(self.decisions(1, 2), ["issue", "noise"])
        self.assertEqual(self.row(2)["reason"], "rule_noise")

    def test_42_dry_run_records_the_states_and_never_calls_github(self):
        beats = Beats()
        results = self.run_window(
            (REAL_BUG, INVITE, "app crash khi mở"),
            (topic([1], type="bug", category="bug"), chat_group([2]),
             topic([3], type="bug", category="bug", confidence=0.5)),   # 12.13: an unsure untagged bug waits
            mode="dry-run", tagged=(1,), beats=beats)
        self.assertEqual(self.github.calls, [])
        self.assertEqual(self.decisions(1, 2, 3), ["dry_run_issue", "chat", "dry_run_needs_human"])
        self.assertEqual(beats.calls, [(CHAT, 1, HEART), (CHAT, 3, HEART)])
        self.assertEqual(results[0].reaction_message_ids, ())  # dry-run: never an OK hand
        self.assertEqual(self.numbers(1, 2, 3), [None] * 3)


class WorkerReactionGate(unittest.IsolatedAsyncioTestCase):
    """Chat and pending_review get neither heart nor OK hand; a ticket gets both (live)."""

    async def test_only_the_ticket_is_acknowledged_end_to_end(self):
        from tests.helpers import Clock
        clock = Clock()
        store = Store(":memory:", clock=clock)
        self.addCleanup(store.close)
        send = Recorder()
        github = NumberedGitHub()
        model = FakeTopicModel((topic([1], type="bug", category="bug"), chat_group([2]),
                                topic([3], type="bug", category="bug", confidence=0.5)))   # 12.13: unsure
        pipeline = Pipeline(config("live", window=15), store, model, github)
        queue = asyncio.Queue()
        for item in (msg(1, 1, text=REAL_BUG, mentions_listener=True), msg(2, 2, text=INVITE,
                     mentions_listener=False), msg(3, 3, text="app crash khi mở", mentions_listener=False)):
            queue.put_nowait(item)

        async def later():
            await asyncio.sleep(0.1)
            clock.advance(60)

        advancer = asyncio.create_task(later())
        from unittest.mock import patch
        with patch.object(telegram_io, "SWEEP_SECONDS", 0.02):
            task = asyncio.create_task(telegram_io.worker(queue, pipeline, react=send))
            await asyncio.wait_for(queue.join(), 5)
            for _ in range(250):
                if github.calls:
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await advancer
        self.assertEqual(len(github.calls), 2)
        self.assertEqual(send.calls, [(CHAT, 1, HEART), (CHAT, 3, HEART), (CHAT, 1, OK), (CHAT, 3, OK)])
        self.assertEqual([store.get(CHAT, i)["decision"] for i in (1, 2, 3)],
                         ["issue", "chat", "needs_human"])


class CategoryMappingTests(GateBase):
    def test_feature_and_question_commands_are_feedback(self):
        # 12.13 changed this test: /question is no longer a ticket (see the question tests below).
        for command in ("/feature", "/feedback", "/Feedback@Bot"):
            with self.subTest(command=command):
                self.setUp()
                self.run_window((f"{command} thêm nút xuất Excel",), (topic([1], type="bug"),))
                self.assertEqual(self.github.calls[0].labels, ("feedback", "triage", "source:telegram"))
                self.assertTrue(self.github.calls[0].title.startswith("[feedback]"))
                self.assertEqual(self.row(1)["type"], "feedback")

    def test_bug_and_task_commands_keep_their_type(self):
        for command in ("/bug", "/task"):
            with self.subTest(command=command):
                self.setUp()
                self.run_window((f"{command} làm việc này",), (topic([1], type="feature"),))
                self.assertEqual(self.github.calls[0].labels[0], command[1:])

    def test_model_feature_is_folded_into_feedback(self):
        self.run_window(("nhờ a thêm nút xuất Excel",), (topic([1], type="feature"),), tagged=(1,))
        self.assertEqual(self.github.calls[0].labels, ("feedback", "triage", "source:telegram"))
        self.assertEqual(self.row(1)["type"], "feedback")

    # 12.13 (coordinator update): a question is never a ticket, tagged or not, and gets no reaction.
    def test_a_question_command_is_chat_without_an_issue_or_a_reaction(self):
        for command in ("/question", "/Question@Bot"):
            with self.subTest(command=command):
                self.setUp()
                beats = Beats()
                self.run_window((f"{command} khi nào ra bản mới?",), (topic([1], type="bug"),), beats=beats)
                row = self.row(1)
                self.assertEqual((row["decision"], row["reason"], row["type"]),
                                 ("chat", "question_no_ticket", "chat"))
                self.assertEqual((self.github.calls, beats.calls), ([], []))

    def test_a_model_question_is_chat_even_with_a_tag_and_gets_no_reaction(self):
        for category in ("feedback", None):
            for tagged in ((), (1,)):
                with self.subTest(category=category, tagged=tagged):
                    self.setUp()
                    beats = Beats()
                    self.run_window(("khi nào ra bản mới vậy?",),
                                    (topic([1], type="question", category=category),), tagged=tagged,
                                    beats=beats)
                    self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]),
                                     ("chat", "question_no_ticket"))
                    self.assertEqual((self.github.calls, beats.calls), ([], []))
                    self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "chat")

    def test_a_question_only_wins_under_feedback_not_over_a_bug_category(self):
        self.run_window((REAL_BUG,), (topic([1], type="question", category="bug"),))
        self.assertEqual((self.decisions(1), self.github.calls[0].labels[0]), (["issue"], "bug"))

    def test_a_question_command_is_not_hearted_on_receipt(self):
        async def run(text):
            store = Store(":memory:")
            self.addCleanup(store.close)
            send = Recorder()
            event = SimpleNamespace(
                out=False, is_private=False, chat_id=CHAT, id=1, date=NOW, raw_text=text,
                message=SimpleNamespace(mentioned=False),
                get_sender=AsyncMock(return_value=SimpleNamespace(first_name="A", last_name="B")),
                get_chat=AsyncMock(return_value=SimpleNamespace(username=None)))
            await telegram_io.enqueue_event(event, asyncio.Queue(), config().telegram.chat_ids, store,
                                            react=send)
            return send.calls
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        self.assertEqual(loop.run_until_complete(run("/question khi nào xong?")), [])
        self.assertEqual(loop.run_until_complete(run("/bug lỗi đăng nhập")), [(CHAT, 1, HEART)])

    def test_the_model_category_wins_over_a_conflicting_type(self):
        self.run_window(("nhờ a thêm nút xuất Excel",), (topic([1], type="bug", category="feedback"),),
                        tagged=(1,))
        self.assertEqual(self.github.calls[0].labels[0], "feedback")

    def test_a_usable_category_rescues_an_unusable_type(self):
        self.run_window((REAL_BUG,), (topic([1], type="admin", category="bug"),), tagged=(1,))
        self.assertEqual(self.github.calls[0].labels, ("bug", "triage", "source:telegram"))

    def test_canonical_category(self):
        self.assertEqual([canonical_category(v) for v in
                          ("bug", "Task", " feedback ", "feature", "QUESTION", "chat")],
                         ["bug", "task", "feedback", "feedback", "feedback", "chat"])
        self.assertEqual([canonical_category(v) for v in ("noise", "other", "", None, 3, True)], [None] * 6)


class ChatAndReviewStateTests(GateBase):
    def test_category_chat_is_the_chat_state_and_never_calls_github(self):
        for verdict in ("noise", "ticket"):
            with self.subTest(verdict=verdict):
                self.setUp()
                self.run_window(("hôm nay họp mấy giờ nhỉ",), (chat_group([1], verdict=verdict),))
                self.assertEqual(self.github.calls, [])
                self.assertEqual(self.decisions(1), ["chat"])
                self.assertEqual(self.row(1)["type"], "chat")

    # 12.13 changed these two (was: a tag / a command on a group the model calls chat is kept for review
    # and logged). Both are forced issues now; the log carries the reason code only, never the content.
    def test_a_tag_on_a_chat_group_is_a_forced_needs_human_issue_and_the_log_has_no_content(self):
        beats = Beats()
        with self.assertLogs("tg_triage.pipeline", level="INFO") as logs:
            self.run_window(("hôm nay họp mấy giờ nhỉ",), (chat_group([1]),), tagged=(1,), beats=beats)
        self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]), ("needs_human", "forced_mention"))
        self.assertEqual((len(self.github.calls), beats.calls), (1, [(CHAT, 1, HEART)]))
        self.assertTrue(any("reason=forced_mention" in line for line in logs.output))
        self.assertNotIn("họp mấy giờ", "\n".join(logs.output))

    def test_a_command_the_model_calls_chat_is_a_forced_issue_with_the_commands_type(self):
        beats = Beats()
        self.run_window(("hôm nay họp", "/bug app crash khi mở"), (chat_group([1, 2]),), beats=beats)
        self.assertEqual(self.decisions(1, 2), ["issue"] * 2)
        self.assertEqual(self.row(2)["reason"], "forced_command")
        self.assertEqual(len(self.github.calls), 1)
        self.assertEqual(self.github.calls[0].labels[0], "bug")
        self.assertEqual(beats.calls, [(CHAT, 1, HEART)])

    def test_a_command_beats_a_tag_for_the_reason_and_both_beat_a_model_chat(self):
        self.run_window(("/task làm báo cáo tuần",), (chat_group([1]),), tagged=(1,))
        self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]), ("issue", "forced_command"))

    def test_a_command_stays_a_ticket_when_the_model_picks_another_ticket_category(self):
        self.run_window(("/bug app crash khi mở",), (topic([1], type="task", category="task"),))
        self.assertEqual(self.github.calls[0].labels[0], "bug")

    # 12.13 changed this test (was: a tagged group below the threshold waits for review).
    def test_a_tagged_group_below_the_confidence_threshold_is_a_needs_human_issue(self):
        self.run_window((REAL_BUG,), (topic([1], category="bug", confidence=0.4),), tagged=(1,))
        self.assertEqual((len(self.github.calls), self.decisions(1)), (1, ["needs_human"]))
        self.assertEqual(self.row(1)["reason"], "forced_mention")

    def test_an_untagged_group_below_the_confidence_threshold_asks_for_clarification(self):
        self.run_window((REAL_BUG,), (topic([1], category="bug", confidence=0.4),))
        self.assertEqual((len(self.github.calls), self.decisions(1)), (1, ["needs_human"]))
        self.assertEqual(self.row(1)["reason"], "auto_needs_human")

    def test_a_tagged_ticket_that_only_misses_information_is_still_a_ticket(self):
        self.run_window((REAL_BUG,), (topic([1], category="bug", missing_info=("Máy nào?",)),),
                        tagged=(1,))
        self.assertEqual(self.decisions(1), ["needs_human"])
        self.assertIn("needs-human", self.github.calls[0].labels)

    def test_a_failed_model_on_a_direct_call_still_gives_a_needs_human_issue(self):
        # No judgment exists, so the tag is the only signal: the team must see it.
        self.run_window((REAL_BUG,), (RuntimeError("offline"), RuntimeError("offline")), tagged=(1,))
        self.assertEqual(self.decisions(1), ["needs_human"])
        self.assertEqual(len(self.github.calls), 1)

    def test_a_failed_model_without_a_tag_asks_for_clarification(self):
        self.run_window((REAL_BUG,), (RuntimeError("offline"), RuntimeError("offline")))
        self.assertEqual((self.decisions(1), len(self.github.calls)), (["needs_human"], 1))

    def test_reconciliation_counts_every_state_and_balances(self):
        # 12.12: "ok" is no longer tier-1 noise; the model puts it in a chat group. A no-letter
        # message (5th) is still noise by code, so every state is counted.
        self.run_window(
            (REAL_BUG, INVITE, "app crash khi mở", "ok", "👍"),
            (topic([1], type="bug", category="bug"), chat_group([2]),
             topic([3], type="bug", category="bug", confidence=0.5),   # 12.13: unsure and untagged => waits
             chat_group([4])),
            tagged=(1,))
        self.assertEqual(self.decisions(1, 2, 3, 4, 5), ["issue", "chat", "needs_human", "chat", "noise"])
        tally = self.reconciliation()
        self.assertEqual((tally["received"], tally["issue"], tally["chat"], tally["pending_review"],
                          tally["noise"], tally["needs_human"], tally["failed"]), (5, 1, 2, 0, 1, 1, 0))
        self.assertEqual((tally["balanced"], tally["status"]), (1, "done"))
        self.assertEqual(self.store.reconcile(f"{CHAT}:1").accounted, 5)
        self.assertEqual(self.store.get_case(f"{CHAT}:1")["status"], "done")   # it holds an issue

    def test_a_chat_only_window_is_balanced_and_closed_to_follow_ups(self):
        pipeline = self.pipeline(FakeTopicModel((chat_group([1]),)))
        pipeline.process(msg(1, 1, text="hôm nay họp mấy giờ nhỉ", mentions_listener=False))
        self.close(pipeline)
        tally = self.reconciliation()
        self.assertEqual((tally["received"], tally["chat"], tally["balanced"]), (1, 1, 1))
        # A later message of the same sender must open a new window, not vanish as a "follow-up".
        late = pipeline.process(msg(2, 30, text=REAL_BUG, mentions_listener=True))
        self.assertNotEqual(late.decision, "followup")

    def test_a_missing_group_state_would_unbalance_the_window(self):
        # The tally is the safety net: if one message had no counted state it must show up.
        self.run_window((REAL_BUG,), (topic([1], category="bug"),), tagged=(1,))
        self.store.connection.execute("UPDATE messages SET decision = 'mystery' WHERE message_id = 1")
        self.assertFalse(self.store.reconcile(f"{CHAT}:1").balanced)


class SingleCaseModelTests(CaseBase):
    """A classifier that cannot split topics judges the whole window; the gate applies there too."""

    def run_case(self, analysis, tagged):
        pipeline = self.pipeline(model=FakeCaseModel(analysis))
        pipeline.process(msg(1, 1, text=REAL_BUG, mentions_listener=tagged))
        self.clock.advance(60)
        return pipeline.close_due_cases()

    # 12.13 changed this test (was: an untagged ticket waits for review): a confident bug is an issue.
    def test_untagged_confident_ticket_is_an_automatic_issue(self):
        self.assertEqual(self.run_case(CaseAnalysis("ticket", classification(), (1,)), False)[0].decision,
                         "issue")
        self.assertEqual(len(self.github.calls), 1)

    def test_untagged_unsure_ticket_asks_for_clarification(self):
        analysis = CaseAnalysis("ticket", classification(confidence=0.4), (1,))
        self.assertEqual(self.run_case(analysis, False)[0].decision, "needs_human")
        self.assertEqual(len(self.github.calls), 1)
        self.assertIn("needs-human", self.github.calls[0].labels)

    def test_tagged_ticket_is_an_issue_and_feature_is_feedback(self):
        analysis = CaseAnalysis("ticket", classification(type="feature"), (1,))
        self.assertEqual(self.run_case(analysis, True)[0].decision, "issue")
        self.assertEqual(self.github.calls[0].labels[0], "feedback")

    # 12.13 changed this test (was: ... and pending_review when tagged): a tag forces an issue.
    def test_category_chat_is_chat_untagged_and_a_needs_human_issue_when_tagged(self):
        chat = CaseAnalysis("noise", classification(type="noise", confidence=0.9), (1,), "chat")
        self.assertEqual(self.run_case(chat, False)[0].decision, "chat")
        self.assertEqual(self.github.calls, [])
        self.setUp()
        self.assertEqual(self.run_case(chat, True)[0].decision, "needs_human")
        self.assertEqual(len(self.github.calls), 1)

    def test_a_case_model_question_is_chat_even_when_tagged(self):
        analysis = CaseAnalysis("ticket", classification(type="question"), (1,), "feedback")
        self.assertEqual(self.run_case(analysis, True)[0].decision, "chat")
        self.assertEqual(self.github.calls, [])


class TelegramMentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    @staticmethod
    def event(**extra):
        fields = dict(out=False, is_private=False, chat_id=CHAT, id=1, date=NOW,
                      raw_text="@vlink_bot invite anh vào repo đi",
                      get_sender=AsyncMock(return_value=SimpleNamespace(first_name="A", last_name="B")),
                      get_chat=AsyncMock(return_value=SimpleNamespace(username=None)))
        fields.update(extra)
        return SimpleNamespace(**fields)

    async def received(self, event):
        queue = asyncio.Queue()
        await telegram_io.enqueue_event(event, queue, config().telegram.chat_ids, self.store)
        return await queue.get()

    async def test_the_flag_is_read_from_event_message_mentioned(self):
        incoming = await self.received(self.event(message=SimpleNamespace(mentioned=True)))
        self.assertTrue(incoming.mentions_listener)
        self.assertEqual(self.store.get(CHAT, 1)["mentions_listener"], 1)
        replay = self.store.queued_messages()
        self.assertEqual([item.mentions_listener for item in replay], [True])   # survives a restart

    async def test_a_message_without_the_flag_is_not_a_call(self):
        incoming = await self.received(self.event(message=SimpleNamespace(mentioned=False)))
        self.assertFalse(incoming.mentions_listener)
        self.assertEqual(self.store.get(CHAT, 1)["mentions_listener"], 0)

    async def test_the_username_in_the_text_is_never_used(self):
        for index, extra in enumerate(({}, {"message": SimpleNamespace()}, {"message": None}), 1):
            incoming = await self.received(self.event(id=index, **extra))
            self.assertFalse(incoming.mentions_listener, extra)

    async def test_only_a_real_true_counts(self):
        incoming = await self.received(self.event(id=5, message=MagicMock()))   # a truthy stand-in
        self.assertFalse(incoming.mentions_listener)
        incoming = await self.received(self.event(id=6, message=SimpleNamespace(mentioned=1)))
        self.assertFalse(incoming.mentions_listener)


class MigrationAndExportTests(unittest.TestCase):
    OLD_MESSAGES = '''CREATE TABLE messages (
        chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, received_at TEXT NOT NULL,
        sender_name TEXT NOT NULL, text TEXT NOT NULL, tier TEXT NOT NULL,
        type TEXT, confidence REAL, title TEXT, summary TEXT, priority TEXT,
        decision TEXT NOT NULL, issue_number INTEGER, issue_url TEXT, error TEXT,
        processed_at TEXT, latency_ms INTEGER, github_called_at TEXT, chat_username TEXT,
        attempts INTEGER NOT NULL DEFAULT 0, case_key TEXT, sender_id INTEGER, reply_to_id INTEGER,
        is_forward INTEGER NOT NULL DEFAULT 0, has_image INTEGER NOT NULL DEFAULT 0,
        image_size INTEGER, image_mime TEXT, group_key TEXT, reason TEXT,
        reacted_received INTEGER NOT NULL DEFAULT 0, reacted_ok INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, message_id))'''
    OLD_RECONCILIATION = '''CREATE TABLE reconciliation (
        case_key TEXT PRIMARY KEY, received INTEGER NOT NULL, issue INTEGER NOT NULL,
        noise INTEGER NOT NULL, needs_human INTEGER NOT NULL, failed INTEGER NOT NULL,
        groups INTEGER NOT NULL, issues_created INTEGER NOT NULL, balanced INTEGER NOT NULL,
        status TEXT NOT NULL, checked_at TEXT NOT NULL)'''

    def old_database(self, directory):
        path = Path(directory) / "old.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute(self.OLD_MESSAGES)
        connection.execute(self.OLD_RECONCILIATION)
        connection.execute('''INSERT INTO messages (chat_id, message_id, received_at, sender_name, text,
            tier, decision, issue_number) VALUES (-5, 1, ?, 'Cũ', 'tin cũ', 'rule', 'issue', 9)''',
                           (NOW.isoformat(),))
        connection.execute("INSERT INTO reconciliation VALUES ('-5:1', 1, 1, 0, 0, 0, 1, 1, 1, 'done', 'x')")
        connection.commit()
        connection.close()
        return path

    def test_an_old_database_gets_the_new_columns_and_keeps_its_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.old_database(directory)
            store = Store(path, clock=lambda: NOW)
            try:
                columns = {row[1] for row in store.connection.execute("PRAGMA table_info(messages)")}
                tally = {row[1] for row in store.connection.execute("PRAGMA table_info(reconciliation)")}
                self.assertIn("mentions_listener", columns)
                self.assertTrue({"chat", "pending_review"} <= tally)
                old = store.get(-5, 1)
                self.assertEqual((old["decision"], old["issue_number"], old["mentions_listener"]),
                                 ("issue", 9, 0))
                kept = store.connection.execute("SELECT * FROM reconciliation").fetchone()
                self.assertEqual((kept["received"], kept["issue"], kept["chat"], kept["pending_review"]),
                                 (1, 1, 0, 0))
                # The migrated tables accept the new states.
                tagged = message(2, "tin mới", chat_id=-5, mentions_listener=True)
                self.assertTrue(store.enqueue(tagged))
                self.assertTrue(store.get(-5, 2)["mentions_listener"])
                self.assertEqual(store.reconcile("-5:1").received, 0)   # no case rows: nothing to count
            finally:
                store.close()
            Store(path).close()   # reopening an already migrated database is a no-op

    def test_read_only_export_of_an_old_database_has_an_empty_flag_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = self.old_database(directory)
            reader = Store(path, read_only=True)
            try:
                out = Path(directory) / "out.csv"
                reader.export_csv(out)
            finally:
                reader.close()
            rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
        self.assertEqual((rows[0]["decision"], rows[0]["mentions_listener"]), ("issue", ""))
        self.assertIn("mentions_listener", COLUMNS)


class ParserTests(unittest.TestCase):
    @staticmethod
    def answer(**fields):
        group = {"topic": "t", "message_ids": [1], "verdict": "ticket", "type": "bug", "title": "T",
                 "summary": "S", "priority": "low", "confidence": 0.9, "missing_info": []}
        group.update(fields)
        return json.dumps({"groups": [group]})

    def test_category_is_parsed_and_folded(self):
        for given, expected in (("bug", "bug"), ("task", "task"), ("feedback", "feedback"),
                                ("chat", "chat"), ("feature", "feedback"), ("Question", "feedback")):
            with self.subTest(given=given):
                self.assertEqual(parse_topic_groups(self.answer(category=given))[0].category, expected)

    def test_a_missing_or_unusable_category_is_none_and_keeps_the_group(self):
        for fields in ({}, {"category": "other"}, {"category": 7}, {"category": None}, {"category": "noise"}):
            with self.subTest(fields=fields):
                groups = parse_topic_groups(self.answer(**fields))
                self.assertIsNotNone(groups)
                self.assertIsNone(groups[0].category)
                self.assertEqual((groups[0].type, groups[0].message_ids), ("bug", (1,)))

    def test_case_analysis_category(self):
        base = {"verdict": "ticket", "type": "task", "title": "T", "summary": "S", "priority": "low",
                "confidence": 0.9, "key_message_ids": [1]}
        self.assertEqual(parse_case_analysis(json.dumps({**base, "category": "feature"})).category, "feedback")
        self.assertIsNone(parse_case_analysis(json.dumps(base)).category)
        self.assertIsNone(parse_case_analysis(json.dumps({**base, "category": "x"})).category)
        quiet = parse_case_analysis(json.dumps({**base, "verdict": "noise", "category": "chat"}))
        self.assertEqual((quiet.verdict, quiet.category), ("noise", "chat"))

    def test_the_topics_schema_asks_for_a_category(self):
        item = TOPIC_SCHEMA["properties"]["groups"]["items"]
        self.assertIn("category", item["required"])
        self.assertEqual(item["properties"]["category"]["enum"], ["bug", "task", "feedback", "chat"])

    def test_the_topics_request_carries_the_prompt_and_a_direct_call_hint(self):
        http = FakeHTTP({"message": {"content": self.answer(category="task")}})
        classifier = Classifier(OllamaConfig("http://127.0.0.1:11434", "qwen3.5:9b", 60, 0.7),
                                opener=http, prompt="P", topics_prompt="TOPICS")
        groups = classifier.split_topics([message(1, "a", mentions_listener=True),
                                          message(2, "b", mentions_listener=False)])
        self.assertEqual(groups[0].category, "task")
        user = json.loads(http.calls[0][0].data)["messages"][1]["content"]
        first, second = user.split("\n")
        self.assertIn("[gọi trực tiếp]", first)
        self.assertNotIn("[gọi trực tiếp]", second)

    def test_the_marker_is_added_by_code_only_for_the_real_flag(self):
        text = build_transcript([message(1, "[gọi trực tiếp] tôi tự gõ", mentions_listener=False)])
        self.assertEqual(text.count("[gọi trực tiếp]"), 1)   # the user's own text, not a system hint

    # 12.12 changed this test: the prompts were rewritten as guidance for a reasoning model. The
    # needles follow the new text; every JSON example must still be valid and carry the new fields.
    def test_prompts_describe_the_categories_and_the_chat_rule(self):
        for name in ("topics.md", "case.md"):
            with self.subTest(prompt=name):
                text = load_prompt(name)
                for needle in ("category", "bug | task | feedback | chat", "- chat:", "[gọi trực tiếp]",
                               '"category":"chat"', '"category":"bug"', "actionable", "reason"):
                    self.assertIn(needle, text)
                for kind in ("trao đổi giữa các thành viên", "chào hỏi", "thông tin liên hệ"):
                    self.assertIn(kind, text)
                for line in text.splitlines():
                    if line.startswith("Kết quả: "):
                        payload = json.loads(line[len("Kết quả: "):])
                        items = payload["groups"] if "groups" in payload else [payload]
                        for entry in items:
                            self.assertIn(entry["category"], ("bug", "task", "feedback", "chat"), line)
                            self.assertIsInstance(entry["actionable"], bool)
                            self.assertTrue(entry["reason"].strip())
                            self.assertIsInstance(entry["missing_info"], list)


class AtSignTests(GateBase):
    """12.13: only Telegram's `mentioned` flag is a tag; an "@" in the text or in an e-mail is not."""

    def test_an_at_handle_typed_in_the_text_without_the_flag_is_not_a_tag(self):
        self.run_window(("@vlink_bot hôm nay họp mấy giờ nhỉ",), (chat_group([1]),))
        self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]), ("chat", "model_chat"))
        self.assertEqual(self.github.calls, [])

    def test_an_email_in_a_chat_group_does_not_force_an_issue(self):
        self.run_window(("mail của em là linh@example.com nha anh",), (chat_group([1]),))
        self.assertEqual((self.row(1)["decision"], self.row(1)["reason"]), ("chat", "model_chat"))

    def test_the_reason_of_an_untagged_email_bug_is_auto_bug_and_of_a_tagged_one_forced_mention(self):
        text = "lỗi đăng nhập, mail em linh@example.com"
        self.run_window((text,), (topic([1], type="bug", category="bug"),))
        self.assertEqual(self.row(1)["reason"], "auto_bug")
        self.setUp()
        self.run_window((text,), (topic([1], type="bug", category="bug"),), tagged=(1,))
        self.assertEqual(self.row(1)["reason"], "forced_mention")


class RulesTests(unittest.TestCase):
    def test_feedback_is_a_command_like_the_others(self):
        for text in ("/feedback x", "/Feedback@Bot x", "/feature", "/question y"):
            self.assertTrue(rules.is_command(text), text)
        self.assertFalse(rules.is_command("/feedbacks x"))
        self.assertEqual(rules.evaluate("/feedback góp ý").type, "feedback")
        self.assertEqual(rules.evaluate("/feedback").type, "noise")   # an empty command is noise

    # 12.12 removed `test_hard_block_unit` with the hard block itself (rules.is_contact_or_courtesy_only
    # and its ACK / CONTACT word lists). test_rules.py asserts the lists and helpers no longer exist.
    def test_a_message_that_tags_the_listener_is_not_set_aside_by_code(self):
        self.assertIsNone(rules.evaluate("@vlink_bot ok"))


if __name__ == "__main__":
    unittest.main()
