"""Reusable offline fixtures, containing only obvious fake credentials."""

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tg_triage.config import (AggregationConfig, AttachmentsConfig, Config, OllamaConfig,
                              ProxyConfig, TelegramConfig)
from tg_triage.models import CaseAnalysis, Classification, IncomingMessage, TopicGroup

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "config" / "listener.example.toml"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
FAKE_HASH = "0123" * 8
FAKE_TOKEN = "ghp_TEST"


def config(mode="dry-run", limit=20, *, window=0, max_messages=20, attachments=None, max_window=40):
    """`window=0` (default) means no waiting: every message is its own case, which is
    the per-message behavior the pre-aggregation tests were written for."""
    return Config(mode, TelegramConfig("gateway.session", (-1001234567890,), 8, ProxyConfig()),
                  OllamaConfig("http://127.0.0.1:11434", "qwen3.5:9b", 60, 0.7),
                  limit, ":memory:", 123, FAKE_HASH, FAKE_TOKEN, "sandbox/triage",
                  AggregationConfig(window, max_messages, max_window),
                  attachments if attachments is not None else AttachmentsConfig())


def message(message_id=1, text="Đăng nhập bị lỗi trên trình duyệt", **extra):
    """A test message. 12.11: it tags the listener by default, because most tests exercise the
    ticket path, which needs a direct call; pass `mentions_listener=False` for plain group talk."""
    fields = dict(chat_id=-1001234567890, message_id=message_id, received_at=NOW,
                  sender_name="Người thử nghiệm", text=text, mentions_listener=True)
    fields.update(extra)
    return IncomingMessage(**fields)


def classification(**extra):
    fields = dict(type="bug", confidence=0.83, title="Lỗi đăng nhập",
                  summary="Đăng nhập thất bại.", priority="medium")
    fields.update(extra)
    return Classification(**fields)


def result_json(**extra):
    value = classification(**extra)
    return json.dumps(vars(value), ensure_ascii=False)


class FakeHTTP:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if isinstance(response, dict):
            response = json.dumps(response).encode("utf-8")
        elif isinstance(response, str):
            response = response.encode("utf-8")
        return io.BytesIO(response)


class FakeClassifier:
    def __init__(self, value=None):
        self.value = value
        self.calls = []

    def classify(self, text):
        self.calls.append(text)
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


class FakeGitHub:
    def __init__(self, error=None, before_create=None):
        self.calls = []
        self.error = error
        self.before_create = before_create

    def create_issue(self, issue):
        self.calls.append(issue)
        if self.before_create:
            self.before_create()
        if self.error:
            raise self.error
        return 42, "https://github.com/sandbox/triage/issues/42"


# ---- case aggregation and attachment doubles (Assistant definition 12) ------------------

JPG = b"\xff\xd8\xff\xe0" + b"0" * 64
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"0" * 32
ASSET_BASE = "https://github.com/sandbox/triage/releases/download"


class Clock:
    """Mutable store clock so tests can move past a case window."""

    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now = self.now + timedelta(minutes=minutes)


def at(minutes):
    return NOW + timedelta(minutes=minutes)


def ticket(ids, **extra):
    """Model verdict `ticket` for the given Telegram message ids."""
    return CaseAnalysis("ticket", classification(**extra), tuple(ids))


class FakeCaseModel:
    """Case-aware classifier: records (message ids, forced type) per call."""

    def __init__(self, analysis=None):
        self.analysis = analysis
        self.calls = []

    def analyze_case(self, messages, forced_type=None):
        self.calls.append((tuple(item.message_id for item in messages), forced_type))
        if isinstance(self.analysis, BaseException):
            raise self.analysis
        return self.analysis(messages) if callable(self.analysis) else self.analysis


class FakeUploader:
    """Stands in for `gh`: never touches the network, records what the pipeline asked for."""

    def __init__(self, error=None, release_error=None):
        self.error = error
        self.release_error = release_error
        self.releases, self.uploads = [], []

    def ensure_release(self, tag):
        self.releases.append(tag)
        if self.release_error:
            raise self.release_error

    def upload(self, tag, path, asset_name):
        # Record facts that must hold at call time: the file exists, in a private directory.
        self.uploads.append({"tag": tag, "asset": asset_name, "path": Path(path),
                             "existed": Path(path).is_file(), "name_matches": Path(path).name == asset_name,
                             "mode": Path(path).parent.stat().st_mode & 0o777})
        if self.error:
            raise self.error
        return f"{ASSET_BASE}/{tag}/{asset_name}"


class FakeDownloader:
    """`download(chat_id, message_id, directory)`: writes canned bytes under a Telegram-like name."""

    def __init__(self, files=None, error=None):
        self.files = files or {}
        self.error = error
        self.calls, self.directories = [], []

    def __call__(self, chat_id, message_id, directory):
        self.calls.append((chat_id, message_id))
        self.directories.append(Path(directory))
        if self.error:
            raise self.error
        if message_id not in self.files:
            return None
        data, name = self.files[message_id]
        path = Path(directory) / name
        path.write_bytes(data)
        return path


# ---- topic split doubles (Assistant definition 12.9) --------------------------------------


def topic(ids, **extra):
    """A model topic group for the given Telegram message ids (ticket / bug unless overridden)."""
    fields = dict(topic="Chủ đề", message_ids=tuple(ids), verdict="ticket", type="bug",
                  title="Lỗi đăng nhập", summary="Đăng nhập thất bại.", priority="medium",
                  confidence=0.9, missing_info=())
    fields.update(extra)
    return TopicGroup(**fields)


class FakeTopicModel:
    """Topic-splitting classifier: answers are consumed one per call (the last one repeats).

    An answer is a tuple of TopicGroup, None, an exception to raise, or a function of the messages.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def split_topics(self, messages):
        self.calls.append(tuple(item.message_id for item in messages))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer(messages) if callable(answer) else answer


class NumberedGitHub(FakeGitHub):
    """Like FakeGitHub but every created issue gets its own number (101, 102, ...)."""

    def create_issue(self, issue):
        super().create_issue(issue)
        number = 100 + len(self.calls)
        return number, f"https://github.com/sandbox/triage/issues/{number}"
