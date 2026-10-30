"""Behavioral regressions for engineering section B.14, entirely offline."""

import asyncio
import contextlib
import csv
import io
import json
import logging
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tg_triage import telegram_io
from tg_triage.__main__ import main
from tg_triage.classifier import Classifier
from tg_triage.issue_format import format_issue, sanitize
from tg_triage.pipeline import Pipeline
from tg_triage.rules import evaluate
from tg_triage.runtime import AlreadyRunning
from tg_triage.store import Store
from tests.helpers import FakeClassifier, FakeHTTP, NOW, classification, config, message
from tests.test_telegram_io import event


class RoundTwoTests(unittest.TestCase):
    def test_f12_model_surrogate_is_retried_then_rejected(self):
        bad = json.dumps(vars(classification(summary="bad\ud83d")))
        http = FakeHTTP({"message": {"content": bad}}, {"message": {"content": bad}})
        self.assertIsNone(Classifier(config().ollama, opener=http).classify(message().text))
        self.assertEqual(len(http.calls), 2)

    def test_f12_ingress_strings_are_utf8_safe(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        msg = message(text="text\ud83d", sender_name="name\udfff", chat_username="group\ud800")
        store.enqueue(msg)
        store.update_metadata(msg)
        row = store.get(msg.chat_id, msg.message_id)
        self.assertEqual((row["text"], row["sender_name"], row["chat_username"]),
                         ("text\ufffd", "name\ufffd", "group\ufffd"))

    def test_f12_three_startups_bound_classifying_and_queued_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "triage.sqlite3"
            store = Store(path)
            store.enqueue(message())
            store.close()
            for startup in range(3):
                store = Store(path)
                try:
                    replay = store.queued_messages()
                    self.assertEqual(len(replay), 1 if startup < 2 else 0)
                    if replay:
                        store.start_classifying(replay[0])
                        # Simulate a crash, including a queued row with spent attempts.
                        if startup == 1:
                            store.connection.execute("UPDATE messages SET decision='queued'")
                            store.connection.commit()
                    else:
                        row = store.get(message().chat_id, 1)
                        self.assertEqual(row["attempts"], 2)
                        self.assertEqual(row["decision"], "error")
                        self.assertEqual(row["error"], "Lặp lỗi khi xử lý, cần người xem")
                finally:
                    store.close()

    def test_f14_all_fields_neutralize_github_references(self):
        attack = "GH-77 gh-88 https://github.com/o/r/issues/1"
        issue = format_issue(message(text=attack, sender_name=attack),
                             classification(title=attack, summary=attack), "model", False, model=attack)
        link_lines = [line for line in issue.body.split('\n') if line.startswith('- 🔗 ')]
        self.assertEqual(link_lines, ['- 🔗 [github\u200b.com/o/r/issues/1](https://github.com/o/r/issues/1)'])
        body_without_source_links = '\n'.join(line for line in issue.body.split('\n') if line not in link_lines)
        for field in (issue.title, body_without_source_links):
            self.assertNotRegex(field, r"(?i)gh-\d|github\.com/")
            self.assertIn("GH-\u200b77", field)
            self.assertIn("github\u200b.com/", field)

    def test_f15_links_images_and_bare_urls_neutralized_in_every_field(self):
        attack = "[x](https://evil) ![](http://x) https://evil www.evil.com WWW.evil.com"
        for inline in (False, True):
            with self.subTest(inline=inline):
                clean = sanitize(attack, inline=inline)
                self.assertNotIn("://", clean)
                self.assertNotRegex(clean, r"(?i)www\.")
                self.assertIn(r"\[x\]\(https:\u200b//evil\)".replace(r"\u200b", "\u200b"), clean)
                self.assertIn(r"\!\[\]\(http:\u200b//x\)".replace(r"\u200b", "\u200b"), clean)
        issue = format_issue(message(text=attack, sender_name=attack),
                             classification(title=attack, summary=attack), "model", False, model=attack)
        self.assertIn("[mở trên Telegram](https://t.me/c/1234567890/1)", issue.body)
        link_lines = [line for line in issue.body.split('\n') if line.startswith('- 🔗 ')]
        self.assertEqual(link_lines, ['- 🔗 [evil](https://evil)', '- 🔗 [x](http://x)'])
        body_without_source_links = '\n'.join(line for line in issue.body.split('\n') if line not in link_lines)
        self.assertEqual(body_without_source_links.count("://"), 1)
        self.assertNotIn("://", issue.title)
        self.assertNotRegex(issue.title + issue.body, r"(?i)www\.")
        self.assertIn("> \\[x\\]", issue.body)

    def test_f16_clock_rollback_keeps_future_reservations_counted(self):
        store = Store(":memory:", clock=lambda: NOW)
        self.addCleanup(store.close)
        store.start_classifying(message())
        self.assertTrue(store.reserve_github_call(message(), 1))
        self.assertEqual(store.count_recent_issues(NOW - timedelta(minutes=5)), 1)
        self.assertEqual(store.count_recent_issues(NOW + timedelta(minutes=61)), 0)

    def test_f17_missing_export_database_exits_two_without_creating_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "db.sqlite3"
            out = Path(directory) / "out.csv"
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(["export-csv", "--db", str(path), "--out", str(out)]), 2)
            self.assertIn("database", stderr.getvalue().lower())
            self.assertFalse(path.parent.exists())
            self.assertFalse(out.exists())

    def test_f17_export_connection_is_readonly_and_does_not_recover_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path, out = Path(directory) / "db.sqlite3", Path(directory) / "out.csv"
            store = Store(path)
            store.start_classifying(message())
            store.close()
            connections = []
            original = sqlite3.connect

            def connect(*args, **kwargs):
                connection = original(*args, **kwargs)
                try:
                    connection.execute("CREATE TABLE should_be_readonly(x)")
                except sqlite3.OperationalError:
                    connections.append("readonly")
                else:
                    connections.append("writable")
                    connection.rollback()
                return connection

            with patch("tg_triage.store.sqlite3.connect", side_effect=connect):
                self.assertEqual(main(["export-csv", "--db", str(path), "--out", str(out)]), 0)
            self.assertEqual(connections, ["readonly"])
            with contextlib.closing(original(path)) as connection:
                self.assertEqual(connection.execute("SELECT decision FROM messages").fetchone()[0], "classifying")

    def test_f18_login_contended_lock_exits_three_without_starting_client(self):
        with patch("tg_triage.__main__.load_config", return_value=config()), \
                patch("tg_triage.__main__.validate_data_directories"), \
                patch("tg_triage.__main__.ProcessLock", side_effect=AlreadyRunning()), \
                patch("tg_triage.telegram_io.login", new_callable=AsyncMock) as login, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["login"]), 3)
            login.assert_not_called()

    def test_f18_main_missing_session_and_bot_are_permanent_errors(self):
        for authorized, bot, expected in ((False, False, "chưa đăng nhập: chạy lệnh login"),
                                           (True, True, "không hỗ trợ tài khoản bot")):
            with self.subTest(authorized=authorized, bot=bot):
                client = fake_run_client(authorized=authorized, bot=bot)
                with patch("tg_triage.__main__.load_config", return_value=config()), \
                        patch("tg_triage.__main__.validate_data_directories"), \
                        patch("tg_triage.__main__.ProcessLock", return_value=contextlib.nullcontext()), \
                        patch.dict("sys.modules", {"telethon": fake_events()}), \
                        patch("tg_triage.telegram_io.make_client", return_value=client), \
                        patch("tg_triage.telegram_io.install_signal_handlers", return_value=lambda: None), \
                        contextlib.redirect_stderr(io.StringIO()) as stderr:
                    self.assertEqual(main(["run"]), 2)
                self.assertIn(expected, stderr.getvalue())
                client.disconnect.assert_awaited_once()

    def test_f19_csv_whitespace_and_fullwidth_formulas_are_quoted(self):
        prefixes = (" =", "\n+", " \t-", "\n @", "＝", "＋", "－", "＠", " \n＝")
        store = Store(":memory:")
        self.addCleanup(store.close)
        for index, prefix in enumerate(prefixes, 1):
            store.enqueue(message(index, prefix + "TEST", sender_name=prefix + "NAME"))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "out.csv"
            store.export_csv(out)
            with out.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
        for row, prefix in zip(rows, prefixes):
            self.assertEqual(row["text"], "'" + prefix + "TEST")
            self.assertEqual(row["sender_name"], "'" + prefix + "NAME")

    def test_f20_real_client_setup_filters_library_root_children(self):
        def client(session, api_id, api_hash, *, connection_retries=0, auto_reconnect=False,
                   catch_up=False, sequential_updates=False, **kwargs):
            return SimpleNamespace()

        library = logging.getLogger("telethon")
        original_state = (library.handlers[:], library.level, library.propagate)
        self.addCleanup(lambda: restore_logger(library, original_state))
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(config(), telegram=replace(config().telegram,
                               session_path=str(Path(directory) / "gateway.session")))
            fake = SimpleNamespace(TelegramClient=client, connection=SimpleNamespace())
            with patch.dict("sys.modules", {"telethon": fake}):
                telegram_io.make_client(settings)
            logging.getLogger("telethon.crypto.test").warning(
                "PRIVATE_CONTENT ghp_TEST", exc_info=(ValueError, ValueError("PRIVATE_CONTENT"), None))
        self.assertNotIn("PRIVATE_CONTENT", output.getvalue())
        self.assertNotIn("ghp_TEST", output.getvalue())
        self.assertIn("exception=ValueError", output.getvalue())

    def test_f21_main_run_and_login_really_validate_directories(self):
        for command in ("run", "login"):
            with self.subTest(command=command):
                with patch("tg_triage.__main__.load_config", return_value=config()), \
                        patch("tg_triage.__main__.ProcessLock", return_value=contextlib.nullcontext()), \
                        patch("tg_triage.__main__.validate_data_directories") as validate, \
                        patch("tg_triage.telegram_io.run", new_callable=AsyncMock), \
                        patch("tg_triage.telegram_io.login", new_callable=AsyncMock):
                    self.assertEqual(main([command]), 0)
                    validate.assert_called_once_with(config())

    def test_f21_run_acquires_lock_before_opening_store(self):
        order = []
        store = Store(":memory:")
        self.addCleanup(store.close)

        @contextlib.contextmanager
        def lock(directory):
            order.append("locked")
            yield
            order.append("unlocked")

        def open_store(path):
            order.append("opened")
            return SimpleNamespace(close=lambda: order.append("closed"))

        with patch("tg_triage.__main__.load_config", return_value=config()), \
                patch("tg_triage.__main__.validate_data_directories"), \
                patch("tg_triage.__main__.ProcessLock", side_effect=lock), \
                patch("tg_triage.__main__.Store", side_effect=open_store), \
                patch("tg_triage.telegram_io.run", new_callable=AsyncMock):
            self.assertEqual(main(["run"]), 0)
        self.assertEqual(order, ["locked", "opened", "closed", "unlocked"])

    def test_f21_rule_summary_ends_at_early_newline(self):
        value = evaluate("/bug Dòng đầu tiên\nDòng thứ hai không phải tóm tắt")
        self.assertEqual(value.summary, "Dòng đầu tiên")


def restore_logger(logger, state):
    logger.handlers, logger.level, logger.propagate = state


def fake_events():
    return SimpleNamespace(events=SimpleNamespace(NewMessage=lambda **kwargs: kwargs))


def fake_run_client(*, authorized=True, bot=False):
    return SimpleNamespace(add_event_handler=lambda *args: None, remove_event_handler=lambda *args: None,
                           connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=authorized),
                           get_me=AsyncMock(return_value=SimpleNamespace(bot=bot) if authorized else None),
                           run_until_disconnected=AsyncMock(), disconnect=AsyncMock())


class RoundTwoAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def process_queue(self, store, pipeline):
        queue = asyncio.Queue()
        for index in (1, 2):
            queue.put_nowait(message(index))
        task = asyncio.create_task(telegram_io.worker(queue, pipeline))
        try:
            join = asyncio.create_task(queue.join())
            done, _ = await asyncio.wait((task, join), timeout=3, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                task.result()
            self.assertTrue(join.done(), "Worker must finish both messages")
        finally:
            task.cancel()
            join.cancel()
            await asyncio.gather(task, join, return_exceptions=True)

    async def test_f12_surrogate_model_does_not_block_next_message(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        await self.process_queue(store, Pipeline(config(), store, FakeClassifier(classification(title="bad\ud83d"))))
        for index in (1, 2):
            row = store.get(message().chat_id, index)
            self.assertEqual(row["decision"], "dry_run_needs_human")
            self.assertIsNone(row["title"])

    async def test_f12_unexpected_message_error_is_terminal_worker_continues(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        pipeline = Pipeline(config(), store, FakeClassifier(classification()))
        with patch("tg_triage.pipeline.rules.evaluate", side_effect=(ValueError("PRIVATE_CONTENT"), None)), \
                self.assertLogs("tg_triage", level="ERROR") as logs:
            await self.process_queue(store, pipeline)
        first = store.get(message().chat_id, 1)
        self.assertEqual(first["decision"], "error")
        self.assertEqual(first["error"], "ValueError")
        self.assertIsNone(first["title"])
        self.assertNotIn("PRIVATE_CONTENT", "\n".join(logs.output))
        self.assertEqual(store.get(message().chat_id, 2)["decision"], "dry_run_issue")

    async def test_f13_stop_still_persists_new_event_without_memory_queue(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        stop, queue = asyncio.Event(), asyncio.Queue()
        stop.set()
        await telegram_io.enqueue_event(event(), queue, config().telegram.chat_ids, store, stop=stop)
        row = store.get(message().chat_id, 1)
        self.assertIsNotNone(row)
        self.assertEqual(row["decision"], "queued")
        self.assertTrue(queue.empty())

    async def test_f13_handler_survives_until_disconnect_and_saves_drain_event(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        captured, order = {}, []
        client = fake_run_client()

        def install(stop):
            captured["stop"] = stop
            stop.set()
            return lambda: None

        def add(handler, builder):
            captured["handler"] = handler

        def remove(handler):
            order.append("remove")
            captured.pop("handler", None)

        async def disconnect():
            order.append("disconnect")
            handler = captured.get("handler")
            if handler:
                await handler(event())

        client.add_event_handler, client.remove_event_handler, client.disconnect = add, remove, disconnect
        with patch.dict("sys.modules", {"telethon": fake_events()}), \
                patch("tg_triage.telegram_io.make_client", return_value=client), \
                patch("tg_triage.telegram_io.install_signal_handlers", side_effect=install):
            await telegram_io.run(config(), SimpleNamespace(store=store))
        self.assertEqual(order, ["disconnect", "remove"])
        self.assertEqual(store.get(message().chat_id, 1)["decision"], "queued")

    async def test_f21_durability_observations_asserted_outside_lookup(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        queue, observations = asyncio.Queue(), []
        incoming = event()

        async def sender():
            row = store.get(incoming.chat_id, incoming.id)
            observations.append((dict(row) if row else None, queue.qsize()))
            return SimpleNamespace(first_name="Tên")

        incoming.get_sender = sender
        await telegram_io.enqueue_event(incoming, queue, config().telegram.chat_ids, store)
        row, size = observations[0]
        self.assertIsNotNone(row)
        self.assertEqual(row["decision"], "queued")
        self.assertEqual(row["text"], incoming.raw_text)
        self.assertEqual(size, 0)
        self.assertEqual(queue.qsize(), 1)
