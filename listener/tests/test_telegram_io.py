"""Transport orchestration tests using fake events and clients only."""

import asyncio
import io
import logging
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tg_triage import telegram_io
from tg_triage.config import ConfigError
from tg_triage.store import Store
from tests.helpers import NOW, config, message


def event(**extra):
    fields = dict(out=False, is_private=False, chat_id=-1001234567890, id=1,
                  date=NOW, raw_text="Nội dung tin thử nghiệm",
                  get_sender=AsyncMock(return_value=SimpleNamespace(first_name="Tên", last_name="Người gửi")),
                  get_chat=AsyncMock(return_value=SimpleNamespace(username="Team_Group")))
    fields.update(extra)
    return SimpleNamespace(**fields)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    async def test_own_private_and_unconfigured_messages_are_ignored(self):
        queue = asyncio.Queue()
        for extra in ({"out": True}, {"is_private": True}, {"chat_id": -999}):
            incoming = event(**extra)
            await telegram_io.enqueue_event(incoming, queue, config().telegram.chat_ids, self.store)
            incoming.get_sender.assert_not_called()
        self.assertEqual(queue.qsize(), 0)

    async def test_event_preserves_text_sender_date_and_username(self):
        queue = asyncio.Queue()
        await telegram_io.enqueue_event(event(), queue, config().telegram.chat_ids, self.store)
        msg = await queue.get()
        self.assertEqual(msg.sender_name, "Tên Người gửi")
        self.assertEqual(msg.text, "Nội dung tin thử nghiệm")
        self.assertEqual(msg.received_at, NOW)
        self.assertEqual(msg.chat_username, "Team_Group")

    async def test_metadata_failure_keeps_message_and_empty_media_text(self):
        queue = asyncio.Queue()
        incoming = event(raw_text=None, get_sender=AsyncMock(side_effect=OSError("fake offline")),
                         get_chat=AsyncMock(side_effect=OSError("fake offline")))
        await telegram_io.enqueue_event(incoming, queue, config().telegram.chat_ids, self.store)
        msg = await queue.get()
        self.assertEqual(msg.text, "")
        self.assertEqual(msg.sender_name, "không rõ người gửi")
        self.assertIsNone(msg.chat_username)

    async def test_backlog_warning_has_queue_length_and_no_message_text(self):
        queue = asyncio.Queue()
        for _ in range(20):
            queue.put_nowait(message())
        with self.assertLogs("tg_triage.telegram_io", level="WARNING") as logs:
            await telegram_io.enqueue_event(event(), queue, config().telegram.chat_ids, self.store)
        self.assertIn("queue_length=21", logs.output[0])
        self.assertNotIn("Nội dung tin", logs.output[0])

    async def test_one_worker_processes_in_order_off_event_loop(self):
        queue = asyncio.Queue()
        loop_thread = threading.get_ident()
        calls = []

        def process(msg):
            calls.append((msg.message_id, threading.get_ident()))

        for index in range(1, 4):
            queue.put_nowait(message(index))
        task = asyncio.create_task(telegram_io.worker(queue, SimpleNamespace(process=process)))
        await asyncio.wait_for(queue.join(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual([call[0] for call in calls], [1, 2, 3])
        self.assertTrue(all(call[1] != loop_thread for call in calls))

    async def test_cancellation_waits_for_active_thread_before_shutdown(self):
        queue = asyncio.Queue()
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def process(msg):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("Test thread timed out")
            finished.set()

        queue.put_nowait(message())
        task = asyncio.create_task(telegram_io.worker(queue, SimpleNamespace(process=process)))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())
            await asyncio.wait_for(queue.join(), 5)
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_heartbeat_logs_alive_after_ten_minutes(self):
        sleep = AsyncMock(side_effect=(None, asyncio.CancelledError()))
        with patch("tg_triage.telegram_io.asyncio.sleep", sleep), \
                self.assertLogs("tg_triage.telegram_io", level="INFO") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await telegram_io.heartbeat(asyncio.Queue())
        self.assertEqual(sleep.call_args_list[0].args, (600,))
        self.assertIn("alive queue_length=0", logs.output[0])

    async def test_login_uses_hidden_prompts_and_sets_session_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.session"
            path.write_text("FAKE_SESSION", encoding="utf-8")
            settings = config()
            settings = replace(settings, telegram=replace(settings.telegram, session_path=str(path)))
            client = SimpleNamespace(start=AsyncMock(), disconnect=AsyncMock())
            with patch("tg_triage.telegram_io.make_client", return_value=client), \
                    patch("tg_triage.telegram_io.getpass.getpass", return_value="TEST") as prompt, \
                    patch.object(Path, "chmod") as chmod:
                await telegram_io.login(settings)
                for callback in client.start.call_args.kwargs.values():
                    self.assertEqual(callback(), "TEST")
                self.assertEqual(prompt.call_count, 3)
                chmod.assert_called_once_with(0o600)
                client.disconnect.assert_awaited_once()

    async def test_terminal_disconnect_raises_and_registers_chat_filter(self):
        events = SimpleNamespace(NewMessage=lambda **kwargs: kwargs)
        client = SimpleNamespace(
            add_event_handler=lambda handler, builder: builders.append(builder),
            connect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True),
            get_me=AsyncMock(return_value=SimpleNamespace(bot=False)),
            run_until_disconnected=AsyncMock(), disconnect=AsyncMock())
        builders = []
        with patch.dict("sys.modules", {"telethon": SimpleNamespace(events=events)}), \
                patch("tg_triage.telegram_io.make_client", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "disconnected"):
                await telegram_io.run(config(), SimpleNamespace(store=self.store, process=lambda msg: None))
        self.assertEqual(builders, [{"chats": [-1001234567890]}])
        client.disconnect.assert_awaited_once()

    async def test_missing_session_does_not_prompt_or_start_worker(self):
        events = SimpleNamespace(NewMessage=lambda **kwargs: kwargs)
        client = SimpleNamespace(add_event_handler=lambda *args: None, connect=AsyncMock(),
                                 is_user_authorized=AsyncMock(return_value=False),
                                 get_me=AsyncMock(return_value=None), disconnect=AsyncMock())
        with patch.dict("sys.modules", {"telethon": SimpleNamespace(events=events)}), \
                patch("tg_triage.telegram_io.make_client", return_value=client):
            with self.assertRaisesRegex(ConfigError, "chưa đăng nhập: chạy lệnh login"):
                await telegram_io.run(config(), SimpleNamespace(store=self.store))
        client.disconnect.assert_awaited_once()


class ClientConstructionTests(unittest.TestCase):
    def test_reconnect_options_and_mtproxy_match_verified_signature(self):
        calls = []

        def fake_client(session, api_id, api_hash, *, connection_retries=5,
                        auto_reconnect=True, catch_up=False, sequential_updates=False,
                        base_logger=None, connection=None, proxy=None):
            calls.append(dict(connection_retries=connection_retries, auto_reconnect=auto_reconnect,
                              catch_up=catch_up, sequential_updates=sequential_updates,
                              connection=connection, proxy=proxy))
            output = io.StringIO()
            handler = logging.StreamHandler(output)
            root_logger = logging.getLogger()
            root_logger.addHandler(handler)
            try:
                base_logger.getChild("network.sender").warning("PRIVATE_MESSAGE ghp_TEST")
                self.assertIn("tg_triage.transport.network.sender", output.getvalue())
                self.assertNotIn("PRIVATE_MESSAGE", output.getvalue())
                self.assertNotIn("ghp_TEST", output.getvalue())
            finally:
                root_logger.removeHandler(handler)
            return "FAKE_CLIENT"

        fake = SimpleNamespace(TelegramClient=fake_client, connection=SimpleNamespace(
            ConnectionTcpMTProxyRandomizedIntermediate="FAKE_CONNECTION"))
        settings = config()
        proxy = replace(settings.telegram.proxy, host="proxy.example.invalid", secret="TEST_PROXY")
        with tempfile.TemporaryDirectory() as directory:
            settings = replace(settings, telegram=replace(settings.telegram, proxy=proxy,
                               session_path=str(Path(directory) / "gateway.session")))
            with patch.dict("sys.modules", {"telethon": fake}):
                self.assertEqual(telegram_io.make_client(settings), "FAKE_CLIENT")
        self.assertEqual(calls[0], dict(connection_retries=-1, auto_reconnect=True,
                                      catch_up=True, sequential_updates=True,
                                      connection="FAKE_CONNECTION",
                                      proxy=("proxy.example.invalid", 443, "TEST_PROXY")))
