"""Reaction (heart / ok) va listen_all_groups: gia lap hoan toan, khong mang, khong telethon that."""

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tg_triage import reactions, rules, telegram_io
from tg_triage.config import ConfigError, load_config
from tg_triage.models import Decision
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import (EXAMPLE, FAKE_HASH, NOW, Clock, FakeClassifier, FakeGitHub,
                           FakeTopicModel, NumberedGitHub, at, classification, config, message,
                           topic)

CHAT = -1001234567890
HEART, OK = "❤", "\U0001F44C"


class Recorder:
    """Gia `send(chat_id, message_id, emoji)`; ghi lai moi lan goi."""

    def __init__(self, error=None):
        self.calls = []
        self.error = error

    async def __call__(self, chat_id, message_id, emoji):
        self.calls.append((chat_id, message_id, emoji))
        if self.error:
            raise self.error


def event(**extra):
    fields = dict(out=False, is_private=False, is_group=True, chat_id=CHAT, id=7,
                  date=NOW, raw_text="/bug nút lưu không bấm được",
                  get_sender=AsyncMock(return_value=SimpleNamespace(first_name="A", last_name="B")),
                  get_chat=AsyncMock(return_value=SimpleNamespace(username=None)))
    fields.update(extra)
    return SimpleNamespace(**fields)


class IsCommandTests(unittest.TestCase):
    def test_commands_true(self):
        for text in ("/bug x", "/Bug@Bot x", "/feature", "/question y", "/task z"):
            with self.subTest(text=text):
                self.assertTrue(rules.is_command(text))

    def test_non_commands_false(self):
        for text in ("bug x", "hello /bug", "", "/bugs x", " /bug x"):
            with self.subTest(text=text):
                self.assertFalse(rules.is_command(text))


class ReceivedReactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.queue = asyncio.Queue()

    async def enqueue(self, incoming, send, **extra):
        await telegram_io.enqueue_event(incoming, self.queue, config().telegram.chat_ids,
                                        self.store, react=send, **extra)

    async def test_plain_message_gets_no_reaction(self):
        send = Recorder()
        await self.enqueue(event(raw_text="Đăng nhập bị lỗi trên trình duyệt"), send)
        self.assertEqual(send.calls, [])
        self.assertEqual(self.queue.qsize(), 1)

    async def test_new_command_gets_exactly_one_heart_and_duplicate_none(self):
        send = Recorder()
        await self.enqueue(event(), send)
        self.assertEqual(send.calls, [(CHAT, 7, HEART)])
        await self.enqueue(event(), send)  # cung chat_id/id: store.enqueue tra False
        self.assertEqual(send.calls, [(CHAT, 7, HEART)])

    async def test_reaction_failure_does_not_break_enqueue(self):
        send = Recorder(error=RuntimeError("SECRET_CONTENT"))
        with self.assertLogs("tg_triage.reactions", level="WARNING") as logs:
            await self.enqueue(event(), send)
        self.assertEqual(self.queue.qsize(), 1)
        self.assertEqual(self.store.get(CHAT, 7)["decision"], "queued")
        self.assertIn("exception=RuntimeError", logs.output[0])
        self.assertNotIn("SECRET_CONTENT", logs.output[0])
        self.assertNotIn("nút lưu", logs.output[0])

    async def test_no_send_callable_means_no_reaction_and_no_error(self):
        await self.enqueue(event(), None)
        self.assertEqual(self.queue.qsize(), 1)

    async def test_hanging_reaction_is_bounded_by_timeout(self):
        async def hang(*args):
            await asyncio.sleep(60)

        with self.assertLogs("tg_triage.reactions", level="WARNING") as logs:
            self.assertFalse(await reactions.react(hang, CHAT, 1, HEART, timeout=0.01))
        self.assertIn("exception=TimeoutError", logs.output[0])


class WorkerReactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:", clock=lambda: NOW)
        self.addCleanup(self.store.close)

    async def run_worker(self, pipeline, send, *messages):
        queue = asyncio.Queue()
        for item in messages:
            queue.put_nowait(item)
        task = asyncio.create_task(telegram_io.worker(queue, pipeline, react=send))
        await asyncio.wait_for(queue.join(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def pipeline(self, mode, github=None):
        return Pipeline(config(mode), self.store, FakeClassifier(classification()), github)

    async def test_dry_run_never_sends_ok_even_with_issue_number(self):
        send = Recorder()
        fake = SimpleNamespace(
            config=config("dry-run"), store=self.store,
            process=lambda msg: Decision("dry_run_issue", "rule", None, issue_number=99))
        await self.run_worker(fake, send, message(1, "/bug x"))
        self.assertEqual(send.calls, [])

    async def test_dry_run_pipeline_only_heart_from_enqueue(self):
        send = Recorder()
        queue = asyncio.Queue()
        await telegram_io.enqueue_event(event(), queue, config().telegram.chat_ids,
                                        self.store, react=send)
        await self.run_worker(self.pipeline("dry-run"), send, await queue.get())
        self.assertEqual(send.calls, [(CHAT, 7, HEART)])

    async def test_live_issue_created_sends_one_ok_on_right_message(self):
        send, github = Recorder(), FakeGitHub()
        await self.run_worker(self.pipeline("live", github), send, message(5, "/bug x"))
        # 12.10: a command that skipped enqueue gets its heart when the group becomes a ticket.
        self.assertEqual(send.calls, [(CHAT, 5, HEART), (CHAT, 5, OK)])
        self.assertEqual(self.store.get(CHAT, 5)["decision"], "issue")

    async def test_ok_is_sent_after_result_is_stored(self):
        seen = []

        async def send(chat_id, message_id, emoji):
            seen.append(self.store.get(chat_id, message_id)["decision"])

        await self.run_worker(self.pipeline("live", FakeGitHub()), send, message(5, "/bug x"))
        # 12.10: the heart precedes the issue (row still in_case); the OK hand follows the result.
        self.assertEqual(seen, ["in_case", "issue"])

    async def test_live_create_issue_failure_sends_no_ok(self):
        send = Recorder()
        github = FakeGitHub(error=RuntimeError("boom"))
        await self.run_worker(self.pipeline("live", github), send, message(5, "/bug x"))
        # 12.10: the heart comes before the failed issue call; the OK hand is never sent.
        self.assertEqual(send.calls, [(CHAT, 5, HEART)])
        self.assertEqual(self.store.get(CHAT, 5)["decision"], "error")

    async def test_ok_not_sent_from_worker_thread(self):
        loop_thread = threading.get_ident()
        threads = []

        async def send(chat_id, message_id, emoji):
            threads.append(threading.get_ident())

        await self.run_worker(self.pipeline("live", FakeGitHub()), send, message(5, "/bug x"))
        self.assertEqual(threads, [loop_thread, loop_thread])   # heart and OK hand, both on the loop

    async def test_send_failure_keeps_result_and_next_message_processed(self):
        send = Recorder(error=OSError("SECRET_CONTENT"))
        github = FakeGitHub()
        with self.assertLogs("tg_triage.reactions", level="WARNING") as logs:
            await self.run_worker(self.pipeline("live", github), send,
                                  message(1, "/bug một"), message(2, "/bug hai"))
        self.assertEqual(len(github.calls), 2)
        # khong retry: moi tin dung 1 lan cho tim (12.10) va 1 lan cho OK, du moi lan deu loi
        self.assertEqual(len(send.calls), 4)
        for number in (1, 2):
            row = self.store.get(CHAT, number)
            self.assertEqual(row["decision"], "issue")
            self.assertEqual(row["issue_number"], 42)
        self.assertTrue(all("exception=OSError" in line and "SECRET_CONTENT" not in line
                            for line in logs.output))

    async def test_failed_message_still_quarantined_and_task_done(self):
        send = Recorder()
        quarantined = []

        def boom(msg):
            raise ValueError("x")

        fake = SimpleNamespace(config=config("live"), process=boom, store=SimpleNamespace(
            fail_message=lambda msg, name: quarantined.append(name)))
        await self.run_worker(fake, send, message(1, "/bug x"))
        self.assertEqual(send.calls, [])
        self.assertEqual(quarantined, ["ValueError"])


class TicketReactionTests(unittest.IsolatedAsyncioTestCase):
    """Assistant definition 12.10 (tests 32-36 and extras): a heart for every ticket / needs-human
    group before its issue, an OK hand after a live issue, nothing for noise and follow-ups."""

    def setUp(self):
        self.store = Store(":memory:", clock=lambda: NOW)
        self.addCleanup(self.store.close)

    async def run_worker(self, pipeline, send, *messages):
        queue = asyncio.Queue()
        for item in messages:
            queue.put_nowait(item)
        task = asyncio.create_task(telegram_io.worker(queue, pipeline, react=send))
        await asyncio.wait_for(queue.join(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    def pipeline(self, mode, github=None, classifier=None, store=None, **settings):
        return Pipeline(config(mode, **settings), store or self.store,
                        classifier or FakeClassifier(classification()), github)

    async def receive(self, incoming, send, store=None):
        queue = asyncio.Queue()
        await telegram_io.enqueue_event(incoming, queue, config().telegram.chat_ids,
                                        store or self.store, react=send)
        return await queue.get()

    @staticmethod
    def at_minute(message_id, minute, text=None):
        return message(message_id, text or f"Lỗi số {message_id} khác nhau", received_at=at(minute),
                       sender_id=1)

    async def sweep(self, pipeline, send, clock, items, *, until):
        queue = asyncio.Queue()
        for item in items:
            queue.put_nowait(item)

        async def later():
            await asyncio.sleep(0.1)
            clock.advance(60)

        advancer = asyncio.create_task(later())
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
        await advancer

    # -- 32 ---------------------------------------------------------------------------------
    async def test_32_ticket_without_command_gets_one_heart_then_ok_after_issue(self):
        send, github = Recorder(), FakeGitHub()
        order = []

        async def spy(chat_id, message_id, emoji):
            order.append((emoji, len(github.calls)))   # issues created so far when the reaction lands
            await send(chat_id, message_id, emoji)

        await self.run_worker(self.pipeline("live", github), spy, message(5))
        self.assertEqual(send.calls, [(CHAT, 5, HEART), (CHAT, 5, OK)])
        self.assertEqual(order, [(HEART, 0), (OK, 1)])   # heart before the issue, OK hand after it
        row = self.store.get(CHAT, 5)
        self.assertEqual((row["decision"], row["reacted_received"], row["reacted_ok"]), ("issue", 1, 1))

    async def test_needs_human_group_is_hearted_and_gets_ok_when_the_issue_is_created(self):
        send, github = Recorder(), FakeGitHub()
        # 12.11: a low confidence is now `pending_review`; a model that cannot answer at all
        # (None) is still a needs-human ticket on a direct call, and that is hearted and OK-handed.
        pipeline = self.pipeline("live", github, FakeClassifier(None))
        await self.run_worker(pipeline, send, message(5))
        self.assertEqual(self.store.get(CHAT, 5)["decision"], "needs_human")
        self.assertEqual(send.calls, [(CHAT, 5, HEART), (CHAT, 5, OK)])

    # -- 33 ---------------------------------------------------------------------------------
    async def test_33_noise_gets_no_reaction_at_all(self):
        for mode in ("dry-run", "live"):
            with self.subTest(mode=mode):
                store = Store(":memory:", clock=lambda: NOW)
                self.addCleanup(store.close)
                send, github = Recorder(), FakeGitHub()
                # 12.12: the (fake) model says noise; code no longer drops "ok cảm ơn" by itself.
                noise = FakeClassifier(classification(type="noise", confidence=0.95))
                await self.run_worker(self.pipeline(mode, github, noise, store=store), send,
                                      message(1, "ok cảm ơn"))
                self.assertEqual(store.get(CHAT, 1)["decision"], "noise")
                self.assertEqual((send.calls, github.calls), ([], []))

    async def test_33_model_noise_group_gets_none_while_its_ticket_sibling_does(self):
        clock = Clock()
        store = Store(":memory:", clock=clock)
        self.addCleanup(store.close)
        model = FakeTopicModel((topic([1], verdict="noise", type="noise"), topic([2])))
        pipeline = self.pipeline("live", NumberedGitHub(), model, store, window=15)
        send = Recorder()
        await self.sweep(pipeline, send, clock, [self.at_minute(1, 0), self.at_minute(2, 1)],
                         until=lambda: (CHAT, 2, OK) in send.calls)
        self.assertEqual(send.calls, [(CHAT, 2, HEART), (CHAT, 2, OK)])
        self.assertEqual(store.get(CHAT, 1)["decision"], "noise")

    # -- 34 ---------------------------------------------------------------------------------
    async def test_34_message_hearted_on_receipt_is_not_hearted_again(self):
        send, github = Recorder(), FakeGitHub()
        queued = await self.receive(event(), send)
        self.assertEqual(send.calls, [(CHAT, 7, HEART)])
        self.assertEqual(self.store.get(CHAT, 7)["reacted_received"], 1)
        await self.run_worker(self.pipeline("live", github), send, queued)
        self.assertEqual(send.calls, [(CHAT, 7, HEART), (CHAT, 7, OK)])   # no second heart

    async def test_failed_heart_on_receipt_is_not_recorded_so_the_ticket_heart_is_tried_once(self):
        flaky = Recorder(error=OSError("x"))
        with self.assertLogs("tg_triage.reactions", level="WARNING"):
            queued = await self.receive(event(), flaky)
        self.assertEqual(self.store.get(CHAT, 7)["reacted_received"], 0)
        send = Recorder()
        await self.run_worker(self.pipeline("live", FakeGitHub()), send, queued)
        self.assertEqual(send.calls, [(CHAT, 7, HEART), (CHAT, 7, OK)])

    # -- 35 ---------------------------------------------------------------------------------
    async def test_35_dry_run_hearts_only_and_never_calls_github(self):
        send, github = Recorder(), FakeGitHub()
        await self.run_worker(self.pipeline("dry-run", github), send, message(5))
        self.assertEqual(send.calls, [(CHAT, 5, HEART)])
        self.assertEqual(github.calls, [])
        row = self.store.get(CHAT, 5)
        self.assertEqual((row["decision"], row["reacted_received"], row["reacted_ok"]),
                         ("dry_run_issue", 1, 0))

    # -- 36 ---------------------------------------------------------------------------------
    async def test_36_reaction_errors_never_stop_the_pipeline_or_lose_the_group(self):
        for mode in ("dry-run", "live"):
            with self.subTest(mode=mode):
                store = Store(":memory:", clock=lambda: NOW)
                self.addCleanup(store.close)
                send, github = Recorder(error=OSError("SECRET_CONTENT")), FakeGitHub()
                with self.assertLogs("tg_triage.reactions", level="WARNING") as logs:
                    await self.run_worker(self.pipeline(mode, github, store=store), send,
                                          message(1, "Lỗi đăng nhập một"), message(2, "Lỗi đăng nhập hai"))
                expected = "issue" if mode == "live" else "dry_run_issue"
                self.assertEqual([store.get(CHAT, n)["decision"] for n in (1, 2)], [expected] * 2)
                self.assertEqual(len(github.calls), 2 if mode == "live" else 0)
                # one attempt per reaction, no retry: heart (+ OK hand when live) for each message
                self.assertEqual(len(send.calls), 4 if mode == "live" else 2)
                self.assertTrue(all("exception=OSError" in line and "SECRET_CONTENT" not in line
                                    and "đăng nhập" not in line for line in logs.output))
                self.assertEqual(sum(store.reaction_done(CHAT, n, kind) for n in (1, 2)
                                     for kind in ("received", "ok")), 0)

    async def test_36_reactor_that_never_answers_is_bounded_and_the_issue_is_still_created(self):
        github = FakeGitHub()
        pipeline = self.pipeline("live", github)

        async def hang(chat_id, message_id, emoji):
            await asyncio.sleep(30)

        loop = asyncio.get_running_loop()
        # `react` has its own 10 s bound; the pipeline-side wait must also be bounded.
        pipeline.set_reactor(telegram_io.make_reactor(hang, loop, timeout=0.05))
        with self.assertLogs("tg_triage", level="WARNING") as logs:
            result = await asyncio.to_thread(pipeline.process, message(5))
        self.assertEqual((result.decision, len(github.calls)), ("issue", 1))
        self.assertTrue(any("Reaction wait failed" in line and "exception=TimeoutError" in line
                            for line in logs.output))
        self.assertFalse(self.store.reaction_done(CHAT, 5, "received"))

    # -- one heart and one OK hand per message ---------------------------------------------
    async def test_each_message_gets_at_most_one_heart_and_one_ok_across_groups(self):
        clock = Clock()
        store = Store(":memory:", clock=clock)
        self.addCleanup(store.close)
        store.enqueue(self.at_minute(1, 1))
        store.mark_reaction(CHAT, 1, "received")   # message 1 was hearted when it arrived
        model = FakeTopicModel((topic([1, 3]), topic([2, 4])))
        github, send = NumberedGitHub(), Recorder()
        pipeline = self.pipeline("live", github, model, store, window=15)
        items = [self.at_minute(i, i) for i in (1, 2, 3, 4)]
        await self.sweep(pipeline, send, clock, items, until=lambda: (CHAT, 2, OK) in send.calls)
        self.assertEqual(len(github.calls), 2)
        counts = Counter(send.calls)
        self.assertTrue(all(value == 1 for value in counts.values()), counts)
        self.assertEqual(set(send.calls), {(CHAT, 1, OK), (CHAT, 2, HEART), (CHAT, 2, OK)})  # none on 3, 4

    async def test_followup_in_a_group_that_has_an_issue_gets_no_new_reaction(self):
        clock = Clock()
        store = Store(":memory:", clock=clock)
        self.addCleanup(store.close)
        github, send = FakeGitHub(), Recorder()
        pipeline = self.pipeline("live", github, FakeClassifier(classification()), store, window=15)
        first = self.at_minute(1, 0, "/bug đăng nhập app lỗi")
        later = self.at_minute(2, 1, "iPhone 15 bản mới nhất")
        await self.run_worker(pipeline, send, first, later)
        self.assertEqual((store.get(CHAT, 2)["decision"], len(github.calls)), ("followup", 1))
        self.assertEqual(send.calls, [(CHAT, 1, HEART), (CHAT, 1, OK)])

    # -- restart ------------------------------------------------------------------------------
    async def test_live_ok_is_sent_once_per_issue_group_and_never_again_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            first = Store(path, clock=lambda: NOW)
            try:
                send, github = Recorder(), FakeGitHub()
                await self.run_worker(self.pipeline("live", github, store=first), send, message(5))
                self.assertEqual(send.calls, [(CHAT, 5, HEART), (CHAT, 5, OK)])
            finally:
                first.close()
            restarted = Store(path, clock=lambda: NOW)
            try:
                again = Recorder()
                self.assertEqual(restarted.queued_messages(), [])
                self.assertEqual((restarted.get(CHAT, 5)["reacted_received"],
                                  restarted.get(CHAT, 5)["reacted_ok"]), (1, 1))
                # Telegram catch-up redelivers the same message: a duplicate, so nothing happens.
                await self.run_worker(self.pipeline("live", github, store=restarted), again, message(5))
                self.assertEqual((again.calls, len(github.calls)), ([], 1))
                # Even a decision that still lists the group is answered only once.
                stale = SimpleNamespace(config=config("live"), store=restarted,
                                        process=lambda msg: Decision(
                                            "issue", "rule", None, issue_number=42,
                                            reaction_message_id=5, reaction_message_ids=(5,)))
                await self.run_worker(stale, again, message(6))
                self.assertEqual(again.calls, [])
            finally:
                restarted.close()

    async def test_old_database_without_reaction_columns_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            connection = sqlite3.connect(path)
            connection.execute("""CREATE TABLE messages (
                chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, received_at TEXT NOT NULL,
                sender_name TEXT NOT NULL, text TEXT NOT NULL, tier TEXT NOT NULL, type TEXT,
                confidence REAL, title TEXT, summary TEXT, priority TEXT, decision TEXT NOT NULL,
                issue_number INTEGER, issue_url TEXT, error TEXT, processed_at TEXT, latency_ms INTEGER,
                github_called_at TEXT, chat_username TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (chat_id, message_id))""")
            connection.execute("INSERT INTO messages (chat_id, message_id, received_at, sender_name, text,"
                               " tier, decision) VALUES (1, 1, 'x', 'n', 't', 'rule', 'noise')")
            connection.commit()
            connection.close()
            store = Store(path)
            try:
                self.assertFalse(store.reaction_done(1, 1, "received"))
                store.mark_reaction(1, 1, "received")
                self.assertTrue(store.reaction_done(1, 1, "received"))
                self.assertFalse(store.reaction_done(1, 1, "ok"))
            finally:
                store.close()


class ListenAllGroupsConfigTests(unittest.TestCase):
    def load(self, telegram_lines):
        source = EXAMPLE.read_text(encoding="utf-8")
        head, rest = source.split("[telegram]", 1)
        body = rest.split("[telegram.proxy]", 1)[1]
        text = (head + "[telegram]\nsession_path = \"s.session\"\nmin_text_length = 8\n"
                + telegram_lines + "\n[telegram.proxy]" + body)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "c.toml"
            path.write_text(text, encoding="utf-8")
            return load_config(path, {"TG_API_ID": "1", "TG_API_HASH": FAKE_HASH})

    def test_default_is_false_and_example_loads(self):
        value = load_config(EXAMPLE, {"TG_API_ID": "1", "TG_API_HASH": FAKE_HASH})
        self.assertFalse(value.telegram.listen_all_groups)

    def test_all_groups_with_empty_or_absent_chat_ids_is_valid(self):
        for lines in ("listen_all_groups = true\nchat_ids = []", "listen_all_groups = true"):
            with self.subTest(lines=lines):
                value = self.load(lines)
                self.assertTrue(value.telegram.listen_all_groups)
                self.assertEqual(value.telegram.chat_ids, ())

    def test_all_groups_with_chat_ids_is_error(self):
        with self.assertRaisesRegex(ConfigError, "listen_all_groups"):
            self.load("listen_all_groups = true\nchat_ids = [-1001234567890]")

    def test_not_all_groups_with_empty_chat_ids_is_error(self):
        for lines in ("listen_all_groups = false\nchat_ids = []", "chat_ids = []", ""):
            with self.subTest(lines=lines):
                with self.assertRaisesRegex(ConfigError, "chat_ids"):
                    self.load(lines)

    def test_flag_must_be_boolean(self):
        with self.assertRaisesRegex(ConfigError, "listen_all_groups"):
            self.load('listen_all_groups = "yes"\nchat_ids = [-1001234567890]')


class ListenAllGroupsTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    async def test_filters_by_is_group(self):
        queue = asyncio.Queue()
        cases = (({"is_group": True, "chat_id": -5}, 1),          # basic group
                 ({"is_group": True, "chat_id": -1009999}, 1),    # supergroup
                 ({"is_group": False, "chat_id": -1008888}, 0),   # kenh broadcast
                 ({"is_group": None, "chat_id": -1008888}, 0),
                 ({"is_group": False, "is_private": True, "chat_id": 55}, 0),
                 ({"is_group": True, "is_private": True, "chat_id": 56}, 0),
                 ({"is_group": True, "out": True, "chat_id": -6}, 0))
        for index, (extra, expected) in enumerate(cases, 1):
            before = queue.qsize()
            await telegram_io.enqueue_event(event(id=index, **extra), queue, (), self.store,
                                            listen_all_groups=True)
            self.assertEqual(queue.qsize() - before, expected, extra)

    async def test_default_still_requires_configured_chat(self):
        queue = asyncio.Queue()
        await telegram_io.enqueue_event(event(chat_id=-999), queue, (CHAT,), self.store)
        self.assertEqual(queue.qsize(), 0)

    async def test_run_registers_unfiltered_handler_and_replays_all_chats(self):
        events = SimpleNamespace(NewMessage=lambda **kwargs: kwargs)
        builders, processed = [], []
        done = threading.Event()
        self.store.enqueue(message(1, "/bug a", chat_id=-4242))

        def process(msg):
            processed.append(msg.chat_id)
            done.set()

        async def until_processed():
            await asyncio.to_thread(done.wait, 5)

        client = SimpleNamespace(
            add_event_handler=lambda handler, builder: builders.append(builder),
            connect=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(bot=False)),
            run_until_disconnected=until_processed, disconnect=AsyncMock())
        settings = config()
        settings = replace(settings, telegram=replace(
            settings.telegram, chat_ids=(), listen_all_groups=True))
        pipeline = SimpleNamespace(store=self.store, process=process, config=settings)
        with patch.dict("sys.modules", {"telethon": SimpleNamespace(events=events)}), \
                patch("tg_triage.telegram_io.make_client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                await telegram_io.run(settings, pipeline)
        self.assertEqual(builders, [{}])
        self.assertEqual(processed, [-4242])


class TelethonSenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_sender_builds_send_reaction_request(self):
        class Request:
            def __init__(self, *, peer, msg_id, reaction):
                self.peer, self.msg_id, self.reaction = peer, msg_id, reaction

        class Emoji:
            def __init__(self, emoticon):
                self.emoticon = emoticon

        functions = SimpleNamespace(messages=SimpleNamespace(SendReactionRequest=Request))
        types = SimpleNamespace(ReactionEmoji=Emoji)
        tl = SimpleNamespace(functions=functions, types=types)
        sent = []

        async def client(request):
            sent.append(request)

        with patch.dict("sys.modules", {"telethon": SimpleNamespace(tl=tl), "telethon.tl": tl}):
            await telegram_io.make_reaction_sender(client)(CHAT, 9, HEART)
        self.assertEqual((sent[0].peer, sent[0].msg_id), (CHAT, 9))
        self.assertEqual([item.emoticon for item in sent[0].reaction], [HEART])


if __name__ == "__main__":
    unittest.main()
