"""Offline ticket replies, group clarification and durable at-most-once transport tests."""
import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from tg_triage import group_notices, telegram_io
from tg_triage.models import CaseAnalysis, Decision
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import (Clock, FakeCaseModel, FakeClassifier, FakeGitHub, FakeTopicModel,
                           NumberedGitHub, classification, config, message, topic)
from tests.test_telegram_io import event

class NoticeTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.calls = []
    def send(self, chat, target, text):
        row = self.store.connection.execute("SELECT status FROM group_notices WHERE message_id=?", (target,)).fetchone()
        self.assertEqual(row[0], "reserved")
        self.calls.append((chat, target, text))
        return 900 + target
    def pipeline(self, *, mode="live", model=None, github=None, limit=20, window=0, sender=None):
        return Pipeline(config(mode, limit, window=window), self.store,
                        model or FakeClassifier(None), github or FakeGitHub(), notice_sender=sender or self.send)
    def test_needs_human_link_and_owner_request(self):
        github = FakeGitHub()
        github.create_issue = lambda issue: (42, "https://evil.test/leak?token=secret")
        result = self.pipeline(github=github).process(message())
        self.assertEqual(result.decision, "needs_human")
        text = self.calls[0][2]
        self.assertIn("Owner", text)
        self.assertIn("ngay trong group", text)
        self.assertIn("https://github.com/sandbox/triage/issues/42", text)
        self.assertNotIn("evil", text)
        self.assertEqual(self.store.group_notice_reply_target(message().chat_id, 901), 1)
        self.assertEqual(self.store.group_notice_reply_target(-999, 901), 901)
    def test_questions_bounded_redacted_and_mentions_disabled(self):
        question = "Xác nhận @everyone mail a@example.com 0901234567 https://evil.test/x " + "a" * 500
        model = FakeTopicModel((topic([1], verdict="needs_human", missing_info=(question, None, "Bước nào?")),))
        self.pipeline(model=model).process(message())
        text = self.calls[0][2]
        for value in ("@everyone", "a@example.com", "0901234567", "https://evil"):
            self.assertNotIn(value, text)
        self.assertIn("Bước nào?", text)
        self.assertLessEqual(len(text), group_notices.MAX_NOTICE_CHARS)
        self.assertNotIn("\u202e", group_notices.clean_question("\u202eHi\x00"))
        self.assertEqual(group_notices.clean_question("\ud800"), "")
    def test_legacy_missing_questions(self):
        model = FakeCaseModel(CaseAnalysis("needs_human", None, missing_info=("Bước tái hiện nào?",)))
        self.pipeline(model=model).process(message())
        self.assertIn("Bước tái hiện nào?", self.calls[0][2])

    def test_untagged_uncertainty_always_asks_once(self):
        answers = (topic([1], actionable=False, missing_info=("Màn hình nào?",)),
                   topic([1], confidence=0.65),
                   topic([1], verdict="needs_human", missing_info=(None, " ", "\u200b", "\ud800")), None)
        for index, answer in enumerate(answers):
            with self.subTest(index=index):
                store = Store(":memory:")
                self.addCleanup(store.close)
                github, calls = NumberedGitHub(), []
                model = FakeTopicModel((answer,)) if answer else FakeTopicModel(None)
                p = Pipeline(config("live"), store, model, github,
                             notice_sender=lambda chat, target, text: calls.append((chat, target, text)) or 900)
                result = p.process(message(1, mentions_listener=False))
                self.assertEqual((result.decision, result.reason), ("needs_human", "auto_needs_human"))
                self.assertEqual(len(github.calls), 1)
                self.assertEqual(github.calls[0].labels, ("needs-human", "source:telegram"))
                self.assertEqual(calls[0][:2], (message().chat_id, 1))
                self.assertIn("https://github.com/sandbox/triage/issues/101", calls[0][2])
                self.assertIn("?", calls[0][2])
                if index == 0:
                    self.assertIn("Màn hình nào?", calls[0][2])
                    self.assertNotIn(group_notices.FALLBACK_QUESTION, calls[0][2])
                else:
                    self.assertIn(group_notices.FALLBACK_QUESTION, calls[0][2])
                self.assertIsNone(p.process(message(1, mentions_listener=False)))
                self.assertEqual((len(github.calls), len(calls)), (1, 1))
                tally = store.reconcile(result.case_key)
                self.assertTrue(tally.balanced)
                self.assertEqual((tally.received, tally.needs_human, tally.pending_review), (1, 1, 0))

    def test_two_untagged_low_confidence_topics_keep_questions_targets_and_links(self):
        self.store.clock = Clock()
        questions = ("Phần đặt lịch cần thay đổi thế nào?", "Lỗi thanh toán ở bước nào?")
        groups = tuple(topic(ids, confidence=0.65, missing_info=(question,))
                       for ids, question in zip(((429, 430), (431, 432)), questions))
        github = NumberedGitHub()
        p = self.pipeline(window=5, model=FakeTopicModel(groups), github=github)
        for mid in range(429, 433):
            p.process(message(mid, sender_id=7, mentions_listener=False))
        self.store.clock.advance(6)
        result = p.close_due_cases()[0]
        self.assertEqual([call[1] for call in self.calls], [429, 431])
        self.assertEqual(len(github.calls), 2)
        for offset, target in enumerate((429, 431)):
            number, text = 101 + offset, self.calls[offset][2]
            self.assertIn(questions[offset], text)
            self.assertNotIn(questions[1 - offset], text)
            self.assertNotIn(group_notices.FALLBACK_QUESTION, text)
            self.assertIn(f"https://github.com/sandbox/triage/issues/{number}", text)
            self.assertEqual(github.calls[offset].labels, ("needs-human", "source:telegram"))
            for mid in (target, target + 1):
                row = self.store.get(message().chat_id, mid)
                self.assertEqual((row["decision"], row["reason"], row["issue_number"]),
                                 ("needs_human", "auto_needs_human", number))
                self.assertTrue(row["issue_url"].endswith(f"/issues/{number}"))
        tally = self.store.reconcile(result.case_key)
        self.assertTrue(tally.balanced)
        self.assertEqual((tally.received, tally.needs_human, tally.pending_review), (4, 4, 0))
        self.assertEqual(p.close_due_cases(), [])
        self.assertEqual(len(self.calls), 2)

    def test_legacy_uncertain_information_question_stays_silent(self):
        analysis = CaseAnalysis("ticket", classification(type="question"), category="feedback", actionable=False)
        github = NumberedGitHub()
        result = self.pipeline(model=FakeCaseModel(analysis), github=github).process(
            message(1, text="Phiên bản hiện tại là bao nhiêu?", mentions_listener=False))
        self.assertEqual((result.decision, result.reason), ("chat", "question_no_ticket"))
        self.assertEqual((github.calls, self.calls), ([], []))

    def test_usable_question_after_empty_entries_prevents_generic_fallback(self):
        text = group_notices.notice_text(Decision("needs_human", "model", issue_number=42),
                                        (None, " ", "\u200b", "Ở màn hình nào?"), "sandbox/triage")
        self.assertIn("Ở màn hình nào?", text)
        self.assertNotIn(group_notices.FALLBACK_QUESTION, text)
    def test_success_replies_to_logged_message_once_with_trusted_link(self):
        github = FakeGitHub()
        github.create_issue = lambda issue: (42, "https://evil.test/leak?token=secret")
        p = self.pipeline(model=FakeClassifier(classification()), github=github)
        result = p.process(message(1))
        self.assertEqual(result.decision, "issue")
        self.assertEqual(len(self.calls), 1)
        chat, target, text = self.calls[0]
        self.assertEqual((chat, target), (message().chat_id, 1))
        self.assertEqual(text, "Em đã ghi nhận nội dung này và tạo ticket: "
                         "https://github.com/sandbox/triage/issues/42 rồi nhé 😊")
        self.assertIn("https://github.com/sandbox/triage/issues/42", text)
        self.assertNotIn("evil", text)
        self.assertIsNone(p.process(message(1)))
        group_notices.notify(self.store, self.send, message(1), result, (), "sandbox/triage")
        self.assertEqual(len(self.calls), 1)

    def test_three_messages_one_ticket_replies_only_to_first(self):
        self.store.clock = Clock()
        github = NumberedGitHub()
        p = self.pipeline(window=5, model=FakeTopicModel((topic([1, 2, 3]),)), github=github)
        for index in (1, 2, 3):
            p.process(message(index, sender_id=7))
        self.store.clock.advance(6)
        p.close_due_cases()
        self.assertEqual(len(github.calls), 1)
        self.assertEqual([call[1] for call in self.calls], [1])
        self.assertIn("https://github.com/sandbox/triage/issues/101", self.calls[0][2])

    def test_three_messages_three_tickets_reply_to_each_with_matching_link(self):
        self.store.clock = Clock()
        github = NumberedGitHub()
        p = self.pipeline(window=5, model=FakeTopicModel(tuple(topic([i]) for i in (1, 2, 3))),
                          github=github)
        for index in (1, 2, 3):
            p.process(message(index, sender_id=7))
        self.store.clock.advance(6)
        p.close_due_cases()
        self.assertEqual(len(github.calls), 3)
        self.assertEqual([call[1] for call in self.calls], [1, 2, 3])
        for index, (_, _, text) in enumerate(self.calls, 101):
            self.assertIn(f"https://github.com/sandbox/triage/issues/{index}", text)
        self.assertEqual(p.close_due_cases(), [])
        self.assertEqual(len(self.calls), 3)

    def test_dry_run_noise_chat_question_do_not_send(self):
        self.pipeline(mode="dry-run").process(message(2))
        self.pipeline(mode="dry-run", model=FakeClassifier(classification())).process(message(6))
        self.pipeline().process(message(3, text="123"))
        self.pipeline(model=FakeTopicModel((topic([4], verdict="needs_human", category="feedback", type="question", actionable=False),))).process(message(4, mentions_listener=False))
        self.pipeline(model=FakeTopicModel((topic([5], category="chat", type="chat"),))).process(
            message(5, text="Đây là cuộc trao đổi giữa mọi thành viên hôm nay", mentions_listener=False))
        self.assertEqual(self.calls, [])
    def test_error_safe_and_ambiguous_warning(self):
        result = self.pipeline(github=FakeGitHub(error=OSError("token=secret raw user text"))).process(message())
        self.assertEqual(result.decision, "error")
        text = self.calls[0][2]
        self.assertIn("kiểm tra repo trước", text)
        self.assertNotIn("secret", text)
        self.assertNotIn("OSError", text)
    def test_rate_limit_without_issue(self):
        result = self.pipeline(limit=0).process(message())
        self.assertEqual(result.decision, "rate_limited")
        self.assertIsNone(result.reaction_message_id)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("giới hạn", self.calls[0][2])
    def test_multiple_groups_sweep_and_replay(self):
        clock = Clock()
        self.store.clock = clock
        model = FakeTopicModel((topic([1], verdict="needs_human", missing_info=("Nhóm một?",)),
                                topic([2], verdict="needs_human", missing_info=("Nhóm hai?",))))
        p = self.pipeline(window=5, model=model, github=NumberedGitHub(), limit=1)
        p.process(message(1, sender_id=7))
        p.process(message(2, sender_id=7))
        clock.advance(6)
        self.assertEqual(len(p.close_due_cases()), 1)
        self.assertEqual([x[1] for x in self.calls], [1, 2])
        self.assertIn("Nhóm một?", self.calls[0][2])
        self.assertIn("giới hạn", self.calls[1][2])
        self.assertEqual(p.close_due_cases(), [])
        self.assertIsNone(p.process(message(2)))
        self.assertEqual(len(self.calls), 2)
    def test_restart_reserved_failed_sent_never_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.sqlite"
            for index, status in enumerate(("reserved", "sent", "failed"), 1):
                store = Store(path)
                first = message(index)
                store.reserve_group_notice(first.chat_id, first.message_id, "needs_human")
                if status != "reserved":
                    store.finish_group_notice(first.chat_id, first.message_id, status, 990 if status == "sent" else None)
                store.close()
                store = Store(path)
                try:
                    group_notices.notify(store, self.send, first, Decision("needs_human", "model"), (), "sandbox/triage")
                finally:
                    store.close()
            self.assertEqual(self.calls, [])
    def test_send_failure_terminal_and_preserves_issue(self):
        def fail(*args):
            raise TimeoutError("private diagnostic")
        result = self.pipeline(sender=fail).process(message())
        self.assertEqual(result.decision, "needs_human")
        self.assertEqual(result.issue_number, 42)
        self.assertEqual(self.store.connection.execute("SELECT status FROM group_notices").fetchone()[0], "failed")
        group_notices.notify(self.store, self.send, message(), result, (), "sandbox/triage")
        self.assertEqual(self.calls, [])
    def test_reservation_failure_fails_closed(self):
        with patch.object(self.store, "reserve_group_notice", side_effect=OSError("private")):
            result = self.pipeline().process(message())
        self.assertEqual(result.issue_number, 42)
        self.assertEqual(self.calls, [])
    def test_url_requires_valid_repo_and_positive_integer(self):
        for repo, number in (("../x", 1), ("sandbox/triage", True), ("sandbox/triage", -1),
                             ("sandbox/triage", None)):
            text = group_notices.notice_text(Decision("needs_human", "model", issue_number=number), (), repo)
            self.assertNotIn("https://", text)
            self.assertIsNone(group_notices.notice_text(Decision("issue", "model", issue_number=number), (), repo))

class NoticeTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        class FakeChat:
            pass
        tl = SimpleNamespace(types=SimpleNamespace(Chat=FakeChat))
        patcher = patch.dict("sys.modules", {"telethon.tl": tl})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_sender_plain_reply_group_only(self):
        entity = SimpleNamespace(megagroup=True)
        client = SimpleNamespace(get_entity=AsyncMock(return_value=entity),
                                 send_message=AsyncMock(return_value=SimpleNamespace(id=77)))
        send = telegram_io.make_group_notice_sender(client)
        self.assertEqual(await send(-100123, 9, "Text"), 77)
        client.send_message.assert_awaited_once_with(entity, "Text", reply_to=9, parse_mode=None, link_preview=False)
        client.send_message.reset_mock()
        for entity in (SimpleNamespace(megagroup=False), SimpleNamespace()):
            client.get_entity.return_value = entity
            self.assertFalse(await send(-100123, 9, "Text"))
        client.send_message.assert_not_called()
    async def test_bridge_existing_loop_timeout_bounded(self):
        loop = asyncio.get_running_loop()
        threads = []
        async def send(*args):
            threads.append(threading.get_ident())
            return 321
        sender = telegram_io.make_notice_sender(send, loop, timeout=0.05)
        self.assertEqual(await asyncio.to_thread(sender, -1, 1, "Text"), 321)
        self.assertEqual(threads, [threading.get_ident()])
        async def blocked(*args):
            await asyncio.sleep(100)
        sender = telegram_io.make_notice_sender(blocked, loop, timeout=0.01)
        self.assertFalse(await asyncio.to_thread(sender, -1, 1, "Text"))
    async def test_owner_reply_mapping_followup_and_outgoing_ignored(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        github = FakeGitHub()
        p = Pipeline(config("live"), store, FakeClassifier(None), github, notice_sender=lambda *args: 900)
        p.process(message())
        queue = asyncio.Queue()
        await telegram_io.enqueue_event(event(id=2, reply_to_msg_id=900, raw_text="Owner bổ sung thông tin"),
                                        queue, config().telegram.chat_ids, store)
        reply = await queue.get()
        self.assertEqual(reply.reply_to_id, 1)
        self.assertEqual(p.process(reply).decision, "followup")
        self.assertEqual(len(github.calls), 1)
        self.assertEqual(store.get(message().chat_id, 1)["decision"], "needs_human")
        await telegram_io.enqueue_event(event(id=900, out=True), queue, config().telegram.chat_ids, store)
        self.assertTrue(queue.empty())
    async def test_notice_reply_never_retries_full_error_or_limited_case(self):
        for outcome in ("issue", "needs_human", "error", "rate_limited"):
            with self.subTest(outcome=outcome):
                store = Store(":memory:")
                try:
                    github = FakeGitHub(error=OSError("ambiguous") if outcome == "error" else None)
                    calls = []
                    def send(*args):
                        calls.append(args)
                        return 900
                    cfg = config("live", limit=0 if outcome == "rate_limited" else 20,
                                 max_messages=1)
                    model = FakeClassifier(classification() if outcome == "issue" else None)
                    p = Pipeline(cfg, store, model, github, notice_sender=send)
                    first = p.process(message())
                    self.assertEqual(first.decision, outcome)
                    before_calls = len(github.calls)
                    original = store.get(message().chat_id, 1)
                    case_before = dict(store.get_case(original["case_key"]))
                    queue = asyncio.Queue()
                    # Even an explicit command in a clarification reply is only support data.
                    await telegram_io.enqueue_event(event(id=2, reply_to_msg_id=900,
                                                          raw_text="/bug Owner confirms details"),
                                                    queue, cfg.telegram.chat_ids, store)
                    reply = await queue.get()
                    self.assertEqual(p.process(reply).decision, "followup")
                    row = store.get(reply.chat_id, 2)
                    self.assertEqual(row["case_key"], original["case_key"])
                    self.assertEqual(row["group_key"], original["group_key"])
                    self.assertEqual(row["issue_number"], original["issue_number"])
                    self.assertEqual(row["issue_url"], original["issue_url"])
                    self.assertEqual(store.get_case(original["case_key"])["status"], case_before["status"])
                    self.assertEqual(store.get(reply.chat_id, 1)["decision"], outcome)
                    self.assertEqual(store.connection.execute("SELECT count(*) FROM cases").fetchone()[0], 1)
                    self.assertEqual(len(github.calls), before_calls)
                    self.assertEqual(len(calls), 2)
                    self.assertEqual(calls[-1][1], 2)
                    self.assertIn("Em", calls[-1][2])
                    self.assertEqual(p.close_due_cases(), [])
                finally:
                    store.close()

    async def test_notice_reply_retains_own_topic_issue_in_mixed_window(self):
        store = Store(":memory:", clock=Clock())
        self.addCleanup(store.close)
        cfg = config("live", limit=1, window=5, max_messages=2)
        github = NumberedGitHub()
        p = Pipeline(cfg, store, FakeTopicModel((topic([1], verdict="needs_human"),
                                                topic([2], verdict="needs_human"))), github,
                     notice_sender=lambda chat, target, text: 900 + target)
        p.process(message(1, sender_id=7))
        p.process(message(2, sender_id=7))
        queue = asyncio.Queue()
        for original, notice, incoming in ((1, 901, 3), (2, 902, 4)):
            await telegram_io.enqueue_event(event(id=incoming, reply_to_msg_id=notice),
                                            queue, cfg.telegram.chat_ids, store)
            self.assertEqual(p.process(await queue.get()).decision, "followup")
            row = store.get(message().chat_id, incoming)
            source = store.get(message().chat_id, original)
            self.assertEqual(row["group_key"], source["group_key"])
            self.assertEqual(row["issue_number"], source["issue_number"])
        self.assertEqual(store.get(message().chat_id, 3)["issue_number"], 101)
        self.assertIsNone(store.get(message().chat_id, 4)["issue_number"])
        self.assertEqual(len(github.calls), 1)
        self.assertEqual(store.connection.execute("SELECT count(*) FROM cases").fetchone()[0], 1)

    async def test_worker_sweep_with_no_reaction_target(self):
        store = Store(":memory:", clock=Clock())
        self.addCleanup(store.close)
        p = Pipeline(config("live", limit=0, window=5), store, FakeClassifier(None), FakeGitHub())
        p.process(message())
        store.clock.advance(6)
        stop = asyncio.Event()
        calls = []
        async def send(chat, target, text):
            calls.append((chat, target, text))
            stop.set()
            return 900
        with patch.object(telegram_io, "SWEEP_SECONDS", 0.01):
            await asyncio.wait_for(telegram_io.worker(asyncio.Queue(), p, stop, notice_send=send), 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], 1)
        self.assertIsNone(p.notice_sender)
