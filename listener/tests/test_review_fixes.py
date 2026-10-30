"""Regression tests for Engineering Doc B.13, initially red on round one."""

import asyncio
import contextlib
import errno
import csv
import io
import logging
import os
import signal
import sqlite3
import stat
import tempfile
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tg_triage.__main__ import main
from tg_triage.config import load_config
from tg_triage.github_issues import GitHubIssues
from tg_triage.issue_format import format_issue, sanitize
from tg_triage.pipeline import Pipeline
from tg_triage.rules import evaluate
from tg_triage.store import Store
from tg_triage import telegram_io
from tests.helpers import EXAMPLE, FAKE_HASH, FAKE_TOKEN, FakeClassifier, FakeGitHub, FakeHTTP, NOW, ROOT, classification, config, message
from tests.test_telegram_io import event


class ReviewFixTests(unittest.TestCase):
    def test_f1_case_variant_url_keeps_created_issue(self):
        url = "https://github.com/Sandbox/Triage/issues/42"
        client = GitHubIssues("sandbox/triage", FAKE_TOKEN, opener=FakeHTTP({"number": 42, "html_url": url}))
        issue = format_issue(message(), classification(), "model", False)
        self.assertEqual(client.create_issue(issue), (42, url))

    def test_f1_mismatched_url_warns_but_keeps_number(self):
        url = "https://github.com/sandbox/renamed/issues/42"
        client = GitHubIssues("sandbox/triage", FAKE_TOKEN, opener=FakeHTTP({"number": 42, "html_url": url}))
        with self.assertLogs("tg_triage.github_issues", level="WARNING") as logs:
            result = client.create_issue(format_issue(message(), classification(), "model", False))
        self.assertEqual(result, (42, url))
        self.assertNotIn(url, "\n".join(logs.output))

    def test_f1_legacy_database_migration_preserves_ambiguous_quota(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute('''CREATE TABLE messages (
                chat_id INTEGER, message_id INTEGER, received_at TEXT, sender_name TEXT,
                text TEXT, tier TEXT, type TEXT, confidence REAL, title TEXT, summary TEXT,
                priority TEXT, decision TEXT, issue_number INTEGER, issue_url TEXT,
                error TEXT, processed_at TEXT, latency_ms INTEGER,
                PRIMARY KEY(chat_id, message_id))''')
            connection.execute('''INSERT INTO messages
                (chat_id,message_id,received_at,sender_name,text,tier,decision,processed_at)
                VALUES(-123,1,?,'TEST','TEST','model','error',?)''', (NOW.isoformat(), NOW.isoformat()))
            connection.commit()
            connection.close()
            store = Store(path, clock=lambda: NOW)
            try:
                self.assertEqual(store.count_recent_issues(), 1)
                self.assertIsNotNone(store.get(-123, 1)["github_called_at"])
                self.assertEqual(store.get(-123, 1)["decision"], "error")
            finally:
                store.close()

    def test_f1_failed_calls_consume_quota_and_survive_restart(self):
        from urllib.error import HTTPError
        for error in (TimeoutError("ghp_TEST"), HTTPError("https://api.github.com", 503, "ghp_TEST", {}, None)):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "db.sqlite3"
                store = Store(path, clock=lambda: NOW)
                try:
                    client = GitHubIssues("sandbox/triage", FAKE_TOKEN, opener=FakeHTTP(error))
                    result = Pipeline(config("live", 1), store, FakeClassifier(classification()), client).process(message())
                    self.assertEqual(result.decision, "error")
                    self.assertEqual(store.count_recent_issues(), 1)
                finally:
                    store.close()
                reopened = Store(path, clock=lambda: NOW)
                try:
                    github = FakeGitHub()
                    result = Pipeline(config("live", 1), reopened, FakeClassifier(classification()), github).process(message(2))
                    self.assertEqual(result.decision, "rate_limited")
                    self.assertEqual(github.calls, [])
                finally:
                    reopened.close()

    def test_f2_html_markdown_newlines_references_and_zwj(self):
        attack = '<!-- <img src=x> [x](https://evil.invalid) ! * _ ` | \\ @user #12 owner/repo#34 &#64;user'
        value = format_issue(message(text=attack + "\nsecond", sender_name=attack + "\nname"),
                             classification(title=attack, summary=attack + "\nsummary\tend"), "model", False)
        self.assertNotIn("<img", value.body)
        self.assertEqual(value.body.count("<!--"), 1)
        lines = value.body.splitlines()
        summary = lines[lines.index("### 📝 Tóm tắt") + 1]
        self.assertIn("\\[x\\]\\(https:\u200b//evil.invalid\\)", summary)
        self.assertIn("summary end", summary)
        self.assertIn("&amp;#\u200b64;user", value.body)
        self.assertIn("#\u200b12", value.body)
        self.assertIn("owner/repo#\u200b34", value.body)
        self.assertIn("\n> second", value.body)
        self.assertNotIn("\nname", value.body)
        emoji = "👩\u200d💻"
        self.assertEqual(sanitize(emoji + "\u202e\u2067\x00"), emoji)

    def test_f2_rule_summary_first_line_capped(self):
        value = evaluate("/bug " + "x" * 250 + "\n[evil](url)")
        self.assertEqual(value.summary, "x" * 200)

    def test_f3_csv_formula_prefixes_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(":memory:")
            try:
                for index, prefix in enumerate(("=", "+", "-", "@", "\t", "\r"), 1):
                    msg = message(index, prefix + "TEST", sender_name=prefix + "NAME")
                    store.claim(msg)
                out = Path(directory) / "review.csv"
                with patch.object(Path, "chmod") as chmod:
                    store.export_csv(out)
                    chmod.assert_called_with(0o600)
                with out.open(encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.DictReader(stream))
                for row in rows:
                    self.assertTrue(row["text"].startswith("'"))
                    self.assertTrue(row["sender_name"].startswith("'"))
                    self.assertTrue(row["chat_id"].startswith("'"))
            finally:
                store.close()

    def test_f4_export_from_db_without_any_credentials_or_toml(self):
        with tempfile.TemporaryDirectory() as directory:
            path, out = Path(directory) / "db.sqlite3", Path(directory) / "review.csv"
            Store(path).close()
            with patch.dict(os.environ, {}, clear=True), patch("tg_triage.__main__.load_config") as loader:
                self.assertEqual(main(["export-csv", "--db", str(path), "--out", str(out)]), 0)
                loader.assert_not_called()
            self.assertTrue(out.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_f4_export_reads_only_storage_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path, out = Path(directory) / "db.sqlite3", Path(directory) / "review.csv"
            Store(path).close()
            toml = Path(directory) / "export.toml"
            toml.write_text('[storage]\ndb_path = "' + path.as_posix() + '"\n', encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), patch("tg_triage.__main__.load_config") as loader:
                self.assertEqual(main(["export-csv", "--config", str(toml), "--out", str(out)]), 0)
                loader.assert_not_called()

    def test_f5_restart_replays_queued_and_classifying_but_not_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.sqlite3"
            store = Store(path)
            for index, state in enumerate(("queued", "classifying", "pending"), 1):
                store.claim(message(index, chat_username="Team_Group"))
                store.connection.execute("UPDATE messages SET decision=? WHERE message_id=?", (state, index))
            store.connection.commit()
            store.close()
            reopened = Store(path)
            try:
                replay = reopened.queued_messages()
                self.assertEqual([msg.message_id for msg in replay], [1, 2])
                self.assertEqual(replay[0].chat_username, "Team_Group")
                model, github = FakeClassifier(classification()), FakeGitHub()
                pipeline = Pipeline(config("live"), reopened, model, github)
                for msg in replay:
                    pipeline.process(msg)
                self.assertEqual(len(github.calls), 2)
                self.assertEqual(reopened.get(-1001234567890, 3)["decision"], "error")
            finally:
                reopened.close()

    def test_f6_main_sets_umask_and_checks_directory_mode_owner(self):
        from tg_triage.runtime import validate_data_directories
        with patch("tg_triage.__main__.os.umask") as umask, \
                patch("tg_triage.__main__.load_config", return_value=config()), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(["check-config"]), 0)
            umask.assert_called_once_with(0o077)
        good = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=7)
        validate_data_directories(config(), stat_fn=lambda path: good, uid_fn=lambda: 7)
        for bad in (replace_stat(good, st_mode=stat.S_IFDIR | 0o755), replace_stat(good, st_uid=8)):
            with self.assertRaisesRegex(ValueError, "0700|owner"):
                validate_data_directories(config(), stat_fn=lambda path: bad, uid_fn=lambda: 7)
        unit = (ROOT / "systemd/tg-triage.service").read_text()
        self.assertIn("UMask=0077", unit)

    def test_f7_proxy_secret_environment_only(self):
        source = EXAMPLE.read_text(encoding="utf-8").replace('host = ""', 'host = "proxy.example.invalid"')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "proxy.toml"
            path.write_text(source, encoding="utf-8")
            env = {"TG_API_ID": "123", "TG_API_HASH": FAKE_HASH, "TG_PROXY_SECRET": "TEST_PROXY"}
            value = load_config(path, env)
            self.assertEqual(value.telegram.proxy.secret, "TEST_PROXY")
            env.pop("TG_PROXY_SECRET")
            with self.assertRaisesRegex(ValueError, "TG_PROXY_SECRET"):
                load_config(path, env)
        self.assertNotIn("secret", tomllib.loads(EXAMPLE.read_text())["telegram"]["proxy"])

    def test_f7_toml_secret_is_rejected_and_proxy_redaction_uses_length(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            source = EXAMPLE.read_text().replace("port = 443", 'port = 443\nsecret = "TEST_PROXY"')
            path.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "TG_PROXY_SECRET"):
                load_config(path, {"TG_API_ID": "123", "TG_API_HASH": FAKE_HASH})
        value = load_config(EXAMPLE, {"TG_API_ID": "123", "TG_API_HASH": FAKE_HASH, "TG_PROXY_SECRET": "TEST_PROXY"})
        self.assertEqual(value.redacted()["telegram"]["proxy"]["secret"], {"length": 10})

    def test_f8_blank_mode_error_classes_and_unit_restart_policy(self):
        value = load_config(EXAMPLE, {"TG_API_ID": "123", "TG_API_HASH": FAKE_HASH, "TG_TRIAGE_MODE": ""})
        self.assertEqual(value.mode, "dry-run")
        with patch("tg_triage.__main__.load_config", return_value=config()), \
                patch("tg_triage.__main__.validate_data_directories"), \
                patch("tg_triage.__main__.ProcessLock", return_value=contextlib.nullcontext()), \
                patch("tg_triage.telegram_io.run", new_callable=AsyncMock, side_effect=TimeoutError("ghp_TEST")), \
                self.assertLogs("tg_triage.__main__", level="ERROR") as logs:
            self.assertEqual(main(["run"]), 1)
        self.assertIn("TimeoutError", "\n".join(logs.output))
        self.assertNotIn(FAKE_TOKEN, "\n".join(logs.output))
        self.assertIn("RestartPreventExitStatus=2", (ROOT / "systemd/tg-triage.service").read_text())
        self.assertNotIn("\nTG_TRIAGE_MODE=", (ROOT / ".env.example").read_text())

    def test_f8_transport_warning_preserves_class_not_message(self):
        from tg_triage.telegram_io import SafeTransportHandler
        record = logging.LogRecord("transport.network", logging.WARNING, "", 0, "ghp_TEST %s", ("PRIVATE_MESSAGE",),
                                   (TimeoutError, TimeoutError("ghp_TEST"), None))
        with self.assertLogs("tg_triage.telegram_io", level="WARNING") as logs:
            SafeTransportHandler().handle(record)
        output = "\n".join(logs.output)
        self.assertIn("TimeoutError", output)
        self.assertIn("transport.network", output)
        self.assertNotIn("ghp_TEST", output)
        self.assertNotIn("PRIVATE_MESSAGE", output)

    def test_f9_exclusive_nonblocking_lock_and_exit_three(self):
        from tg_triage.runtime import AlreadyRunning, ProcessLock
        calls = []
        module = SimpleNamespace(LOCK_EX=2, LOCK_NB=4, LOCK_UN=8,
                                 flock=lambda fd, flags: calls.append(flags))
        with tempfile.TemporaryDirectory() as directory:
            with ProcessLock(directory, lock_module=module):
                self.assertEqual(calls, [6])
            self.assertEqual(calls, [6, 8])
            module.flock = lambda fd, flags: (_ for _ in ()).throw(BlockingIOError())
            with self.assertRaises(AlreadyRunning):
                with ProcessLock(directory, lock_module=module):
                    self.fail("Contended lock must not succeed")
            module.flock = lambda fd, flags: (_ for _ in ()).throw(PermissionError(errno.EACCES, "TEST"))
            with self.assertRaises(AlreadyRunning):
                with ProcessLock(directory, lock_module=module):
                    self.fail("EACCES contention must exit as already running")
        with patch("tg_triage.__main__.load_config", return_value=config()), \
                patch("tg_triage.__main__.validate_data_directories"), \
                patch("tg_triage.__main__.ProcessLock", side_effect=AlreadyRunning()), \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(["run"]), 3)
        self.assertIn("already running", errors.getvalue())

    def test_f10_dependency_version_bound(self):
        project = tomllib.loads((ROOT / "listener/pyproject.toml").read_text())
        self.assertEqual(project["project"]["dependencies"], ["telethon>=1.45,<2"])

    def test_f11_readme_documents_signals_deadline_and_replay(self):
        text = (ROOT / "listener/README.md").read_text(encoding="utf-8")
        for term in ("SIGTERM", "SIGINT", "120", "queued", "classifying"):
            self.assertIn(term, text)


def replace_stat(value, **extra):
    return SimpleNamespace(**{**vars(value), **extra})


class ReviewAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_f5_run_replays_queue_and_stops_before_next_message(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.enqueue(message(1))
        store.enqueue(message(2))
        captured = {}
        loop = asyncio.get_running_loop()
        order = []

        def install(stop):
            captured["stop"] = stop
            return lambda: None

        def classify(text):
            order.append("active")
            loop.call_soon_threadsafe(captured["stop"].set)
            return classification()

        def disconnected():
            self.assertEqual(store.get(-1001234567890, 1)["decision"], "issue")
            order.append("disconnect")

        client = SimpleNamespace(add_event_handler=lambda *args: None, remove_event_handler=lambda *args: None,
                                 connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
                                 get_me=AsyncMock(return_value=SimpleNamespace(bot=False)),
                                 run_until_disconnected=AsyncMock(side_effect=lambda: None),
                                 disconnect=AsyncMock(side_effect=disconnected))

        async def wait_disconnect():
            await asyncio.Event().wait()

        client.run_until_disconnected = wait_disconnect
        fake = SimpleNamespace(events=SimpleNamespace(NewMessage=lambda **kwargs: kwargs))
        pipeline = Pipeline(config("live"), store, SimpleNamespace(classify=classify), FakeGitHub())
        with patch.dict("sys.modules", {"telethon": fake}), \
                patch("tg_triage.telegram_io.make_client", return_value=client), \
                patch("tg_triage.telegram_io.install_signal_handlers", install):
            await asyncio.wait_for(telegram_io.run(config(), pipeline), timeout=5)
        self.assertEqual(order, ["active", "disconnect"])
        self.assertEqual(store.get(-1001234567890, 2)["decision"], "queued")

    async def test_f5_receive_is_durable_before_metadata_or_queue(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        queue = asyncio.Queue()
        incoming = event()
        observations = []

        async def sender():
            row = store.get(incoming.chat_id, incoming.id)
            observations.append((dict(row) if row else None, queue.qsize()))
            return SimpleNamespace(first_name="Tên", last_name="Người gửi")

        incoming.get_sender = sender
        await telegram_io.enqueue_event(incoming, queue, config().telegram.chat_ids, store)
        row, size = observations[0]
        self.assertIsNotNone(row)
        self.assertEqual(row["decision"], "queued")
        self.assertEqual(row["text"], incoming.raw_text)
        self.assertEqual(size, 0)
        self.assertEqual(queue.qsize(), 1)

    async def test_f5_signal_stop_waits_for_active_worker_then_disconnects(self):
        from tg_triage.telegram_io import graceful_shutdown, install_signal_handlers
        stop = asyncio.Event()
        callbacks = {}
        fake_loop = SimpleNamespace(add_signal_handler=lambda sig, callback: callbacks.update({sig: callback}),
                                    remove_signal_handler=lambda sig: callbacks.pop(sig, None))
        with patch("tg_triage.telegram_io.signal.signal"):
            cleanup = install_signal_handlers(stop, loop=fake_loop)
            callbacks[signal.SIGTERM]()
            self.assertTrue(stop.is_set())
            cleanup()
        order = []

        async def active():
            await asyncio.sleep(0)
            order.append("finished")

        client = SimpleNamespace(disconnect=AsyncMock(side_effect=lambda: order.append("disconnect")))
        task = asyncio.create_task(active())
        await graceful_shutdown(client, task, timeout=120)
        self.assertEqual(order, ["finished", "disconnect"])

    async def test_f5_shutdown_deadline_never_waits_indefinitely(self):
        from tg_triage.telegram_io import graceful_shutdown
        client = SimpleNamespace(disconnect=AsyncMock())
        task = asyncio.create_task(asyncio.Event().wait())
        exits = []
        try:
            with self.assertRaises(SystemExit):
                await graceful_shutdown(client, task, timeout=0.01, force_exit=exits.append)
            self.assertEqual(exits, [1])
            client.disconnect.assert_awaited_once()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
