"""Explicit commands and empty messages: the only things code decides about a message's content.

Assistant definition 12.12: no keyword list and no regex decides whether talk is chat or a ticket.
The model reads the context and decides; this module only parses an explicit /command and spots a
message that has no content to judge (no letter at all).
"""

import re

from .models import Classification

COMMAND = re.compile(r"^/(bug|feature|question|task|feedback)(?:@[A-Za-z0-9_]+)?(?=\s|$)", re.I)


def is_command(text: str) -> bool:
    """True khi tin bắt đầu bằng lệnh /bug|/feature|/question|/task|/feedback (cùng quy tắc `evaluate`).

    12.11: a command is also a direct call to the listener, like a tag."""
    return COMMAND.match(text or "") is not None


def command_type(text: str) -> str | None:
    """The lower-case command name (bug, feature, question, task, feedback) at the start, else None."""
    match = COMMAND.match(text or "")
    return match[1].lower() if match else None


def _noise():
    return Classification("noise", 1.0, "Không cần xử lý", "Tin không cần tạo issue", "low")


def evaluate(text: str, min_text_length: int = 0) -> Classification | None:
    """A command gives its type; an empty command or a message with no letter is noise; else None.

    `min_text_length` is kept for call compatibility and is ignored: a short message is not
    dropped by code, the model decides (12.12). Greetings and acknowledgements are not matched
    here either.
    """
    match = COMMAND.match(text)
    if match:
        content = text[match.end():].strip()
        if not content:
            return _noise()
        return Classification(match[1].lower(), 1.0, content[:80], content.splitlines()[0][:200], "medium")
    if not any(ch.isalpha() for ch in text):
        return _noise()
    return None
