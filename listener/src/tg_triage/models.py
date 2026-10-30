"""Shared records and validation for the triage boundary."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

ISSUE_TYPES = ("bug", "feature", "question", "task")   # what the model / the commands may say
# 12.11: only bug, task and feedback are ticket categories; feature and question fold into feedback.
TICKET_CATEGORIES = ("bug", "task", "feedback")
CATEGORIES = (*TICKET_CATEGORIES, "chat")
TICKET_TYPES = (*ISSUE_TYPES, "feedback")   # every type a classification may carry before folding
TYPES = (*TICKET_TYPES, "noise")
_FEEDBACK_ALIASES = frozenset(("feature", "question", "feedback"))
PRIORITIES = ("low", "medium", "high")
VERDICTS = ("ticket", "noise", "needs_human")
LABELS = frozenset((*TICKET_TYPES, "triage", "needs-human", "source:telegram"))


def canonical_category(value):
    """Fold a model `category` or a legacy `type` into bug | task | feedback | chat, else None (12.11).

    `feature` and `question` are feedback. Anything else (noise, unknown strings, non-strings) is
    None: the caller decides what an unreadable category means.
    """
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value in _FEEDBACK_ALIASES:
        return "feedback"
    return value if value in ("bug", "task", "chat") else None


MAX_REASON_CHARS = 300


def clean_reason(value) -> str | None:
    """The model's short `reason` (12.12): one line, bounded, or None when absent or unusable.

    It is stored for the reviewer only; it never reaches an issue, a log line or a GitHub call.
    """
    if not isinstance(value, str) or not utf8_valid(value):
        return None
    value = " ".join(value.split())[:MAX_REASON_CHARS]
    return value or None


def utf8_valid(value):
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# 12.15: only these optional fields can cross the model boundary.
DETAIL_STRING_KEYS = (
    "environment", "expected", "actual", "error_text", "problem", "user_story", "proposal",
    "alternatives", "context", "work", "reason_context", "scope_in", "scope_out", "due",
)
DETAIL_LIST_KEYS = ("steps", "acceptance", "links", "done")


def immutable_details(value):
    """Ignore malformed optional details; copy into sorted immutable key/value pairs.

    Text is bounded after sanitizing/redacting in the renderer, so truncation cannot expose
    part of a contact address. Unknown keys and wrong field/item types are discarded.
    """
    # Internal records use tuples; JSON arrays are deliberately not accepted as details.
    if isinstance(value, tuple):
        value = {pair[0]: pair[1] for pair in value
                 if isinstance(pair, tuple) and len(pair) == 2 and isinstance(pair[0], str)}
    result = {}
    if isinstance(value, Mapping):
        for key in DETAIL_STRING_KEYS:
            entry = value.get(key)
            if utf8_valid(entry):
                result[key] = entry
        for key in DETAIL_LIST_KEYS:
            entries = value.get(key)
            if isinstance(entries, (list, tuple)):
                result[key] = tuple(entry for entry in entries if utf8_valid(entry))[:10]
    return tuple(sorted(result.items()))


@dataclass(frozen=True)
class IncomingMessage:
    chat_id: int
    message_id: int
    received_at: datetime
    sender_name: str
    text: str
    chat_username: str | None = None
    # Aggregation metadata (Assistant definition 12.1) and media flags (12.8).
    sender_id: int | None = None
    reply_to_id: int | None = None
    is_forward: bool = False
    has_image: bool = False
    image_size: int | None = None
    image_mime: str | None = None
    # 12.14: a document (PDF, DOCX...) attached to the message. Only declared metadata is kept:
    # size, MIME and the sender's file name (shown in the issue only after cleaning).
    has_document: bool = False
    document_size: int | None = None
    document_mime: str | None = None
    document_name: str | None = None
    # 12.11: True when Telegram says this message tags the listener account (Telethon
    # `message.mentioned`). Never guessed from the text.
    mentions_listener: bool = False

    @property
    def has_attachment(self) -> bool:
        """An image or a document: media that is content in itself, even without any text."""
        return self.has_image or self.has_document

    def __post_init__(self):
        # Python strings can contain lone UTF-16 surrogates, which SQLite and
        # UTF-8 HTTP cannot encode. Normalize all ingress text before persistence.
        for name in ("sender_name", "text", "chat_username", "document_name"):
            value = getattr(self, name)
            if isinstance(value, str) and not utf8_valid(value):
                object.__setattr__(self, name, re.sub(r"[\ud800-\udfff]", "\ufffd", value))


@dataclass(frozen=True)
class Classification:
    type: str
    confidence: float
    title: str
    summary: str
    priority: str

    def is_valid(self) -> bool:
        """Reject untrusted values, including booleans and non-finite numbers."""
        return (
            all(utf8_valid(value) for value in (self.type, self.title, self.summary, self.priority))
            and
            isinstance(self.type, str) and self.type in TYPES
            and type(self.confidence) in (int, float)
            and 0 <= self.confidence <= 1 and math.isfinite(self.confidence)
            and isinstance(self.title, str) and bool(self.title.strip())
            and isinstance(self.summary, str) and bool(self.summary.strip())
            and isinstance(self.priority, str) and self.priority in PRIORITIES
        )


@dataclass(frozen=True)
class CaseAnalysis:
    """Untrusted model verdict for a whole case; the pipeline validates and decides."""

    verdict: str
    classification: Classification | None
    key_message_ids: tuple = ()
    category: str | None = None   # 12.11: bug | task | feedback | chat as the model said it
    actionable: bool | None = None   # 12.12: False = the model says the request is not clear enough
    reason: str | None = None        # 12.12: short why, for the reviewer (SQLite / CSV only)
    missing_info: tuple = ()         # 12.12: clarifying questions for the reader
    details: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "details", immutable_details(self.details))


@dataclass(frozen=True)
class TopicGroup:
    """Untrusted model output for one topic of a closed window (Assistant definition 12.9).

    Only the structure is checked at parse time; the pipeline verifies that the groups
    partition the window's message ids and decides every action itself.
    """

    topic: str
    message_ids: tuple
    verdict: str
    type: str | None = None
    title: str = ""
    summary: str = ""
    priority: str | None = None
    confidence: float | None = None
    missing_info: tuple = ()
    category: str | None = None   # 12.11: bug | task | feedback | chat; None when absent or unusable
    actionable: bool | None = None   # 12.12: False = not clear enough to act on; None = not said
    reason: str | None = None        # 12.12: short why, for the reviewer (SQLite / CSV only)

    details: tuple = ()

    def __post_init__(self):
        object.__setattr__(self, "details", immutable_details(self.details))

    def classification(self) -> Classification:
        """The model's wording as a Classification; `is_valid()` tells whether to trust it."""
        return Classification(self.type, self.confidence, self.title, self.summary, self.priority)


@dataclass(frozen=True)
class Decision:
    decision: str
    tier: str
    classification: Classification | None = None
    issue_number: int | None = None
    issue_url: str | None = None
    error: str | None = None
    latency_ms: int = 0
    # Case-level context. `reaction_message_id` is set only when this call created
    # the issue, and names the case's first message (the only one that gets the OK hand).
    case_key: str | None = None
    reaction_message_id: int | None = None
    images: int | None = None
    documents: int | None = None
    # 12.9: a window can create several issues; every group's first message that got an issue
    # (live only) is listed here, `reaction_message_id` being the earliest of them.
    reaction_message_ids: tuple = ()
    # Why a noise group was set aside (`rule_noise` = tier 1, `model_noise`); stored per message.
    reason: str | None = None
    # 12.12: the model's own short reason for its verdict; stored per message, never sent anywhere.
    model_reason: str | None = None
