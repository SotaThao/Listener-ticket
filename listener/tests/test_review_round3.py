"""Offline regressions for engineering section B.15."""

import asyncio
import contextlib
import io
import unittest
from unittest.mock import AsyncMock, patch

from tg_triage import telegram_io
from tg_triage.__main__ import main
from tg_triage.issue_format import format_issue, sanitize
from tg_triage.models import Decision
from tg_triage.pipeline import Pipeline
from tg_triage.store import Store
from tests.helpers import FakeClassifier, FakeGitHub, classification, config, message
from tests.test_review_round2 import fake_events, fake_run_client


AMBIGUOUS = "GitHub có thể đã tạo issue — kiểm tra repo trước khi tạo lại"
CLASS_FIELDS = ("type", "title", "summary", "priority", "confidence")


class FakeFloodWaitError(Exception):
    """Fake RPC failure; no transport package is needed."""


class RoundThreeTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_f22_www_links_neutralized_in_all_user_and_model_fields(self):
        attack = "www.evil.com/phish WWW.evil.com/path"
        for inline in (False, True):
            clean = sanitize(attack, inline=inline)
            self.assertNotRegex(clean, r"(?i)www\.")
            self.assertIn("www\u200b.evil.com", clean)
            self.assertIn("WWW\u200b.evil.com", clean)
        issue = format_issue(message(text=attack, sender_name=attack),
                             classification(title=attack, summary=attack), "model", False, model=attack)
        self.assertNotRegex(issue.title + issue.body, r"(?i)www\.")
        self.assertIn("[mở trên Telegram](https://t.me/c/1234567890/1)", issue.body)

    def run_fake_client(self, client):
        with patch("tg_triage.__main__.load_config", return_value=config()), \
                patch("tg_triage.__main__.validate_data_directories"), \
                patch("tg_triage.__main__.ProcessLock", return_value=contextlib.nullcontext()), \
                patch.dict("sys.modules", {"telethon": fake_events()}), \
                patch("tg_triage.telegram_io.make_client", return_value=client), \
                patch("tg_triage.telegram_io.install_signal_handlers", return_value=lambda: None), \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = main(["run"])
        return code, stderr.getvalue()

    def test_f23_floodwait_and_network_errors_exit_one_without_missing_login(self):
        for error in (FakeFloodWaitError("PRIVATE_RPC"), OSError("PRIVATE_NETWORK")):
            with self.subTest(exception=type(error).__name__):
                client = fake_run_client(authorized=False)
                client.get_me = AsyncMock(side_effect=error)
                with self.assertLogs("tg_triage.__main__", level="ERROR") as logs:
                    code, stderr = self.run_fake_client(client)
                self.assertEqual(code, 1)
                self.assertNotIn("chưa đăng nhập", stderr)
                self.assertIn(type(error).__name__, "\n".join(logs.output))
                self.assertNotIn("PRIVATE_", "\n".join(logs.output))
                client.get_me.assert_awaited_once()
                client.is_user_authorized.assert_not_called()
                client.disconnect.assert_awaited_once()

    def test_f23_only_none_user_is_missing_login_and_bot_stays_permanent(self):
        for me, expected in ((None, "chưa đăng nhập: chạy lệnh login"),
                             (type("FakeUser", (), {"bot": True})(), "không hỗ trợ tài khoản bot")):
            with self.subTest(expected=expected):
                client = fake_run_client(authorized=True)
                client.get_me = AsyncMock(return_value=me)
                code, stderr = self.run_fake_client(client)
                self.assertEqual(code, 2)
                self.assertIn(expected, stderr)
                client.is_user_authorized.assert_not_called()
                client.disconnect.assert_awaited_once()

    def test_f24_format_and_reserve_errors_before_call_keep_exception_identity(self):
        for index, target in enumerate(("tg_triage.pipeline.format_issue",
                                        "tg_triage.store.Store.reserve_github_call"), 1):
            with self.subTest(target=target):
                github = FakeGitHub()
                pipeline = Pipeline(config("live"), self.store, FakeClassifier(classification()), github)
                with patch(target, side_effect=ValueError("PRIVATE_CONTENT")), \
                        self.assertLogs("tg_triage.pipeline", level="ERROR") as logs:
                    result = pipeline.process(message(index))
                row = self.store.get(message().chat_id, index)
                self.assertEqual(result.decision, "error")
                self.assertEqual(result.error, "ValueError")
                self.assertEqual(row["error"], "ValueError")
                self.assertIsNone(row["github_called_at"])
                self.assertEqual(github.calls, [])
                self.assertNotIn("GitHub issue creation failed", "\n".join(logs.output))
                self.assertNotIn("PRIVATE_CONTENT", "\n".join(logs.output))

    def test_f24_github_failure_warns_about_possible_issue_without_raw_error(self):
        github = FakeGitHub(error=TimeoutError("PRIVATE_CONTENT ghp_TEST"))
        pipeline = Pipeline(config("live"), self.store, FakeClassifier(classification()), github)
        with self.assertLogs("tg_triage.pipeline", level="ERROR") as logs:
            result = pipeline.process(message())
        row = self.store.get(message().chat_id, 1)
        self.assertEqual(result.error, AMBIGUOUS + " (TimeoutError)")
        self.assertEqual(row["error"], result.error)
        self.assertIsNotNone(row["github_called_at"])
        self.assertEqual(len(github.calls), 1)
        self.assertNotIn("PRIVATE_CONTENT", row["error"] + "\n".join(logs.output))
        self.assertNotIn("ghp_TEST", row["error"] + "\n".join(logs.output))


class RoundThreeAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    async def one_message(self, pipeline):
        queue = asyncio.Queue()
        queue.put_nowait(message())
        task = asyncio.create_task(telegram_io.worker(queue, pipeline))
        try:
            await asyncio.wait_for(queue.join(), timeout=3)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_f24_result_write_failure_after_call_warns_before_manual_retry(self):
        github = FakeGitHub()
        pipeline = Pipeline(config("live"), self.store, FakeClassifier(classification()), github)
        original_finish = self.store.finish

        def finish(msg, result):
            if result.decision == "issue":
                raise OSError("PRIVATE_WRITE_FAILURE")
            return original_finish(msg, result)

        with patch.object(self.store, "finish", side_effect=finish), \
                self.assertLogs("tg_triage.telegram_io", level="ERROR") as logs:
            await self.one_message(pipeline)
        row = self.store.get(message().chat_id, 1)
        self.assertEqual(row["decision"], "error")
        self.assertEqual(row["error"], AMBIGUOUS + " (OSError)")
        self.assertIsNotNone(row["github_called_at"])
        self.assertEqual(len(github.calls), 1)
        self.assertNotIn("PRIVATE_WRITE_FAILURE", "\n".join(logs.output))

    async def test_f25_unexpected_format_failure_quarantines_populated_classification(self):
        pipeline = Pipeline(config("live"), self.store, FakeClassifier(classification()), FakeGitHub())
        with patch("tg_triage.pipeline.format_issue", side_effect=ValueError("PRIVATE_CONTENT")), \
                patch.object(self.store, "fail_message", wraps=self.store.fail_message) as fail, \
                self.assertLogs("tg_triage.pipeline", level="ERROR"):
            await self.one_message(pipeline)
        row = self.store.get(message().chat_id, 1)
        self.assertEqual(row["decision"], "error")
        for field in CLASS_FIELDS:
            with self.subTest(field=field):
                self.assertIsNone(row[field])
        fail.assert_called_once_with(message(), "ValueError")

    async def test_f25_worker_fail_message_clears_every_preexisting_classification_field(self):
        msg = message()
        self.store.enqueue(msg)
        self.store.finish(msg, Decision("queued", "model", classification()))
        before = self.store.get(msg.chat_id, msg.message_id)
        for field in CLASS_FIELDS:
            self.assertIsNotNone(before[field])
        pipeline = Pipeline(config(), self.store, FakeClassifier(classification()))
        with patch("tg_triage.pipeline.rules.evaluate", side_effect=ValueError("PRIVATE_CONTENT")), \
                self.assertLogs("tg_triage.telegram_io", level="ERROR"):
            await self.one_message(pipeline)
        row = self.store.get(msg.chat_id, msg.message_id)
        self.assertEqual(row["decision"], "error")
        for field in CLASS_FIELDS:
            with self.subTest(field=field):
                self.assertIsNone(row[field])
